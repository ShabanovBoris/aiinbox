import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from sqlalchemy import func, select

from app.api.app import create_api_app
from app.config import Settings
from app.domain.enums import ContentKind, ItemState, ItemType, ProcessingStatus, SourceType
from app.domain.models import AskInboxCitation, AskInboxResult
from app.main import _build_http_api_server, _ensure_http_api_user
from app.services.ask_inbox import (
    AskInboxService,
    claim_oldest_ask_job,
    clear_expired_http_ask_results,
)
from app.storage.models import AskJob, Content, Delivery, Event, Item, ItemSource, Reminder, User
from app.workers.ask import AskWorker

API_TOKEN = "pm18-test-token-with-at-least-32-characters"


@pytest.fixture
def api_settings(settings):
    """Give adapter tests the same validated single-user auth configuration as runtime."""
    return settings.model_copy(
        update={
            "http_api_enabled": True,
            "http_api_token": API_TOKEN,
            "http_api_user_telegram_id": 42,
            "http_ask_result_ttl_seconds": 120,
        }
    )


@pytest.fixture
async def api_client(api_settings, session_factory):
    """Exercise the ASGI boundary without binding a socket or enabling Telegram."""
    app = create_api_app(api_settings, session_factory)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        yield client


async def create_user(session_factory, telegram_user_id: int = 42) -> int:
    """Create a persisted identity so HTTP tests use the same canonical owner key."""
    async with session_factory() as session:
        user = User(telegram_user_id=telegram_user_id, telegram_chat_id=telegram_user_id)
        session.add(user)
        await session.commit()
        return user.id


def auth_headers(*, key: str | None = None) -> dict[str, str]:
    """Build the bearer and optional durable retry identity accepted by writes."""
    headers = {"Authorization": f"Bearer {API_TOKEN}"}
    if key is not None:
        headers["Idempotency-Key"] = key
    return headers


class FakeAskProvider:
    """Stand in for the replaceable LLM boundary while exercising AskWorker storage."""

    def __init__(self, item_id: int):
        self.item_id = item_id

    async def answer_inbox(self, question, context, *, preferred_language):
        return AskInboxResult(
            answer="The saved note covers climate adaptation.",
            citations=[AskInboxCitation(item_id=self.item_id)],
            insufficient_context=False,
        )


@pytest.mark.asyncio
async def test_capture_is_canonical_idempotent_and_does_not_echo_private_content(
    api_client, session_factory, caplog
):
    user_id = await create_user(session_factory)
    body = {
        "text": "Read this article https://example.org/story?secret=query-token",
        "user_note": "Compare it with the book",
    }

    caplog.set_level(logging.INFO)
    first = await api_client.post("/v1/items", headers=auth_headers(key="capture-1"), json=body)
    retry = await api_client.post("/v1/items", headers=auth_headers(key="capture-1"), json=body)
    conflict = await api_client.post(
        "/v1/items",
        headers=auth_headers(key="capture-1"),
        json={"text": "a different request"},
    )

    assert first.status_code == 202
    assert first.json() == retry.json()
    assert first.headers["location"] == f"/v1/items/{first.json()['id']}"
    assert first.json()["processing_status"] == "QUEUED"
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert "query-token" not in caplog.text

    async with session_factory() as session:
        items = list((await session.scalars(select(Item))).all())
        sources = list((await session.scalars(select(ItemSource))).all())
        contents = list((await session.scalars(select(Content))).all())
        events = list((await session.scalars(select(Event))).all())
        deliveries = list((await session.scalars(select(Delivery))).all())
        item = items[0]
        assert len(items) == len(sources) == len(contents) == len(events) == 1
        assert not deliveries
        assert item.user_id == user_id
        assert item.telegram_message_id is None
        assert item.external_idempotency_key == "capture-1"
        assert item.source_metadata_json == {"ingest_channel": "http"}
        assert item.user_note == body["user_note"]
        assert sources[0].source_type == SourceType.WEB
        assert contents[0].kind == ContentKind.USER_TEXT
        assert contents[0].text == body["text"]
        assert events[0].event_type == "CREATED"

    detail = await api_client.get(first.headers["location"], headers=auth_headers())
    assert detail.status_code == 200
    assert detail.json()["sources"][0]["source_url"] == body["text"].split()[-1]
    assert "content" not in detail.json()
    assert "USER_TEXT" not in detail.text


@pytest.mark.asyncio
async def test_auth_is_required_and_item_reads_are_owner_scoped(api_client, session_factory):
    owner_id = await create_user(session_factory)
    other_user_id = await create_user(session_factory, 1000)
    foreign = Item(
        user_id=other_user_id,
        telegram_message_id=None,
        source_index=0,
        processing_status=ProcessingStatus.READY,
        state=ItemState.ACTIVE,
        source_type=SourceType.TEXT,
        processing_stage="READY",
        user_note="",
        title="Private item",
    )
    async with session_factory() as session:
        session.add(foreign)
        await session.commit()
        foreign_id = foreign.id

    unauthenticated = await api_client.get("/v1/items")
    assert unauthenticated.status_code == 401
    assert unauthenticated.headers["www-authenticate"] == "Bearer"
    assert unauthenticated.json()["error"]["code"] == "UNAUTHORIZED"
    malformed_bearer = await api_client.get(
        "/v1/items", headers=[(b"authorization", b"Bearer " + b"\xff" * 32)]
    )
    assert malformed_bearer.status_code == 401
    health = await api_client.get("/healthz")
    assert health.status_code == 200
    assert health.json() == {"status": "ok"}

    response = await api_client.get(f"/v1/items/{foreign_id}", headers=auth_headers())
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "ITEM_NOT_FOUND"
    async with session_factory() as session:
        assert await session.get(User, owner_id) is not None


@pytest.mark.asyncio
async def test_cursor_pages_use_created_at_then_id_and_exclude_other_users(
    api_client, session_factory
):
    owner_id = await create_user(session_factory)
    other_user_id = await create_user(session_factory, 1000)
    shared_time = datetime(2026, 9, 25, 12, 0, 0)
    async with session_factory() as session:
        for index in range(4):
            session.add(
                Item(
                    user_id=owner_id,
                    telegram_message_id=None,
                    source_index=0,
                    processing_status=ProcessingStatus.READY,
                    state=ItemState.ACTIVE,
                    source_type=SourceType.TEXT,
                    processing_stage="READY",
                    user_note="",
                    title=f"Owned {index}",
                    created_at=shared_time,
                )
            )
        session.add(
            Item(
                user_id=other_user_id,
                telegram_message_id=None,
                source_index=0,
                processing_status=ProcessingStatus.READY,
                state=ItemState.ACTIVE,
                source_type=SourceType.TEXT,
                processing_stage="READY",
                user_note="",
                title="Foreign",
                created_at=shared_time,
            )
        )
        await session.commit()
        expected_ids = list(
            (
                await session.scalars(
                    select(Item.id)
                    .where(Item.user_id == owner_id)
                    .order_by(Item.created_at.desc(), Item.id.desc())
                )
            ).all()
        )

    first = await api_client.get("/v1/items?limit=2", headers=auth_headers())
    second = await api_client.get(
        f"/v1/items?limit=2&cursor={first.json()['next_cursor']}",
        headers=auth_headers(),
    )
    received_ids = [item["id"] for item in first.json()["items"] + second.json()["items"]]
    assert received_ids == expected_ids
    assert len(set(received_ids)) == 4
    assert second.json()["next_cursor"] is None
    malformed = await api_client.get("/v1/items?cursor=not-a-valid-cursor", headers=auth_headers())
    assert malformed.status_code == 422
    assert malformed.json()["error"]["code"] == "INVALID_CURSOR"
    too_long = await api_client.get(f"/v1/items?cursor={'a' * 513}", headers=auth_headers())
    assert too_long.status_code == 422
    assert too_long.json()["error"]["code"] == "INVALID_CURSOR"


@pytest.mark.asyncio
async def test_read_projections_do_not_create_events_or_reminders(
    api_client, session_factory, caplog
):
    user_id = await create_user(session_factory)
    async with session_factory() as session:
        session.add(
            Item(
                user_id=user_id,
                telegram_message_id=None,
                source_index=0,
                processing_status=ProcessingStatus.READY,
                state=ItemState.ACTIVE,
                source_type=SourceType.TEXT,
                processing_stage="READY",
                user_note="",
                title="Climate adaptation evidence",
                summary="A saved note about climate adaptation.",
                category="Climate",
                item_type=ItemType.READ,
                priority_score=60,
            )
        )
        await session.commit()

    caplog.set_level(logging.INFO)
    for path in (
        "/v1/today",
        "/v1/attention",
        "/v1/weekly",
        "/v1/search?q=private-search-term",
    ):
        response = await api_client.get(path, headers=auth_headers())
        assert response.status_code == 200, response.text
    api_log_messages = "\n".join(
        record.getMessage() for record in caplog.records if record.name == "app.api.app"
    )
    assert "private-search-term" not in api_log_messages

    async with session_factory() as session:
        assert await session.scalar(select(func.count(Event.id))) == 0
        assert await session.scalar(select(func.count(Reminder.id))) == 0


@pytest.mark.asyncio
async def test_interest_snooze_and_settings_use_shared_services(api_client, session_factory):
    user_id = await create_user(session_factory)
    async with session_factory() as session:
        item = Item(
            user_id=user_id,
            telegram_message_id=None,
            source_index=0,
            processing_status=ProcessingStatus.READY,
            state=ItemState.ACTIVE,
            source_type=SourceType.TEXT,
            processing_stage="READY",
            user_note="",
            title="Plan an adaptation project",
        )
        session.add(item)
        await session.commit()
        item_id = item.id

    interest = await api_client.patch(
        f"/v1/items/{item_id}/interest",
        headers=auth_headers(),
        json={"level": 3},
    )
    assert interest.status_code == 200
    assert interest.json()["interest_level"] == 3
    async with session_factory() as session:
        event = await session.scalar(select(Event).where(Event.event_type == "INTEREST_CHANGED"))
        assert event.payload_json == {"from": 2, "to": 3, "source": "http"}

    target = (datetime.now(UTC) + timedelta(days=2)).isoformat().replace("+00:00", "Z")
    naive_snooze = await api_client.post(
        f"/v1/items/{item_id}/snooze",
        headers=auth_headers(),
        json={"snoozed_until": "2026-09-30T12:00:00"},
    )
    assert naive_snooze.status_code == 422
    for timestamp in (int((datetime.now(UTC) + timedelta(days=2)).timestamp()), 1_800_000_000.5):
        epoch_snooze = await api_client.post(
            f"/v1/items/{item_id}/snooze",
            headers=auth_headers(),
            json={"snoozed_until": timestamp},
        )
        assert epoch_snooze.status_code == 422
    snooze = await api_client.post(
        f"/v1/items/{item_id}/snooze",
        headers=auth_headers(),
        json={"snoozed_until": target},
    )
    repeated_snooze = await api_client.post(
        f"/v1/items/{item_id}/snooze",
        headers=auth_headers(),
        json={"snoozed_until": target},
    )
    assert snooze.status_code == 200
    assert repeated_snooze.status_code == 200
    assert snooze.json()["state"] == "SNOOZED"
    assert snooze.json()["snoozed_until"].endswith("Z")
    async with session_factory() as session:
        assert (
            await session.scalar(select(func.count(Event.id)).where(Event.event_type == "SNOOZED"))
            == 1
        )
        assert await session.scalar(select(func.count(Reminder.id))) == 1

    offset_target = (datetime.now(UTC) + timedelta(days=3)).astimezone(timezone(timedelta(hours=3)))
    async with session_factory() as session:
        offset_item = Item(
            user_id=user_id,
            telegram_message_id=None,
            source_index=1,
            processing_status=ProcessingStatus.READY,
            state=ItemState.ACTIVE,
            source_type=SourceType.TEXT,
            processing_stage="READY",
            user_note="",
            title="Snooze with offset",
        )
        session.add(offset_item)
        await session.commit()
        offset_item_id = offset_item.id
    offset_snooze = await api_client.post(
        f"/v1/items/{offset_item_id}/snooze",
        headers=auth_headers(),
        json={"snoozed_until": offset_target.isoformat()},
    )
    assert offset_snooze.status_code == 200
    assert offset_snooze.json()["snoozed_until"].endswith("Z")

    done_responses = [
        await api_client.post(f"/v1/items/{item_id}/done", headers=auth_headers()) for _ in range(2)
    ]
    assert [response.json()["state"] for response in done_responses] == ["DONE", "DONE"]
    async with session_factory() as session:
        assert (
            await session.scalar(select(func.count(Event.id)).where(Event.event_type == "DONE"))
            == 1
        )

    updated_settings = await api_client.patch(
        "/v1/settings",
        headers=auth_headers(),
        json={"timezone": "Europe/Moscow", "attention_intensity": 4},
    )
    assert updated_settings.status_code == 200
    assert updated_settings.json()["timezone"] == "Europe/Moscow"
    assert updated_settings.json()["attention_intensity"] == 4


@pytest.mark.asyncio
async def test_http_ask_uses_shared_worker_and_expires_transient_result(
    api_client, session_factory, api_settings
):
    user_id = await create_user(session_factory)
    async with session_factory() as session:
        item = Item(
            user_id=user_id,
            telegram_message_id=None,
            source_index=0,
            processing_status=ProcessingStatus.READY,
            state=ItemState.ACTIVE,
            source_type=SourceType.TEXT,
            processing_stage="READY",
            user_note="",
            title="Climate adaptation notes",
            summary="How climate adaptation can protect a city.",
        )
        session.add(item)
        await session.flush()
        session.add(
            Content(
                item_id=item.id,
                kind=ContentKind.USER_TEXT,
                text="A climate adaptation plan protects the city from heat.",
            )
        )
        await session.commit()
        item_id = item.id

    accepted = await api_client.post(
        "/v1/ask",
        headers=auth_headers(key="ask-1"),
        json={"question": "What did I save about climate adaptation?"},
    )
    retried = await api_client.post(
        "/v1/ask",
        headers=auth_headers(key="ask-1"),
        json={"question": "  What did I save about climate adaptation?  "},
    )
    conflict = await api_client.post(
        "/v1/ask",
        headers=auth_headers(key="ask-1"),
        json={"question": "A different question"},
    )
    assert accepted.status_code == 202
    assert retried.json() == accepted.json()
    assert conflict.status_code == 409
    job_id = accepted.json()["id"]
    worker = AskWorker(
        session_factory,
        FakeAskProvider(item_id),
        poll_seconds=0.01,
        http_result_ttl_seconds=api_settings.http_ask_result_ttl_seconds,
    )
    assert await worker.process_one()

    async with session_factory() as session:
        job = await session.get(AskJob, job_id)
        deliveries = list((await session.scalars(select(Delivery))).all())
        assert job.status == "DONE"
        assert job.response_channel == "HTTP"
        assert job.result_json["answer"] == "The saved note covers climate adaptation."
        assert job.result_expires_at is not None
        assert not deliveries

    done = await api_client.get(accepted.headers["location"], headers=auth_headers())
    assert done.status_code == 200
    assert done.json()["status"] == "DONE"
    assert done.json()["references"][0]["item_id"] == item_id
    assert done.json()["references"][0]["title"] == "Climate adaptation notes"

    async with session_factory() as session:
        job = await session.get(AskJob, job_id)
        expires_at = job.result_expires_at
    assert (
        await clear_expired_http_ask_results(session_factory, now=expires_at + timedelta(seconds=1))
        == 1
    )
    expired = await api_client.get(accepted.headers["location"], headers=auth_headers())
    assert expired.status_code == 410
    assert expired.json()["error"]["code"] == "ASK_RESULT_EXPIRED"


@pytest.mark.asyncio
async def test_http_ask_retention_runs_inside_the_existing_worker(session_factory):
    user_id = await create_user(session_factory)
    async with session_factory() as session:
        session.add(
            AskJob(
                user_id=user_id,
                telegram_message_id=None,
                question="expired question",
                response_channel="HTTP",
                external_idempotency_key="expired-ask",
                external_request_hash="f" * 64,
                status="DONE",
                result_json={"answer": "expired", "references": []},
                result_expires_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=1),
            )
        )
        await session.commit()

    worker = AskWorker(
        session_factory,
        provider=None,
        poll_seconds=0.01,
        result_cleanup_poll_seconds=0.01,
    )
    stop = asyncio.Event()
    ask_task = asyncio.create_task(worker.run_forever(stop))
    try:
        for _ in range(20):
            async with session_factory() as session:
                result_json = await session.scalar(
                    select(AskJob.result_json).where(
                        AskJob.external_idempotency_key == "expired-ask"
                    )
                )
            if result_json is None:
                break
            await asyncio.sleep(0.01)
    finally:
        stop.set()
        await ask_task
    assert result_json is None


@pytest.mark.asyncio
async def test_failed_http_ask_is_pollable_without_telegram_delivery(api_client, session_factory):
    user_id = await create_user(session_factory)
    accepted = await api_client.post(
        "/v1/ask",
        headers=auth_headers(key="ask-failure"),
        json={"question": "A question that will fail"},
    )
    assert accepted.status_code == 202
    claimed = await claim_oldest_ask_job(session_factory)
    assert claimed is not None
    await AskInboxService(session_factory, provider=None).fail(claimed.id, user_id, "LLM_TIMEOUT")

    response = await api_client.get(accepted.headers["location"], headers=auth_headers())
    assert response.status_code == 200
    assert response.json()["status"] == "FAILED"
    assert response.json()["error"]["code"] == "LLM_TIMEOUT"
    async with session_factory() as session:
        assert await session.scalar(select(func.count(Delivery.id))) == 0


@pytest.mark.asyncio
async def test_body_limit_validation_and_openapi_security(
    api_client, api_settings, session_factory
):
    await create_user(session_factory)
    too_large = await api_client.post(
        "/v1/items",
        headers=auth_headers(key="large"),
        json={"text": "x" * (64 * 1024)},
    )
    assert too_large.status_code == 413
    assert too_large.json()["error"]["code"] == "REQUEST_TOO_LARGE"

    invalid = await api_client.post(
        "/v1/items",
        headers=auth_headers(key="invalid"),
        json={"text": "secret-too-long-value" * 1200},
    )
    assert invalid.status_code == 422
    assert "secret-too-long-value" not in invalid.text

    app = create_api_app(api_settings, session_factory)
    security = app.openapi()["components"]["securitySchemes"]["BearerAuth"]
    assert security["type"] == "http"
    assert security["scheme"] == "bearer"
    assert API_TOKEN not in json.dumps(app.openapi())
    assert "CORSMiddleware" not in {middleware.cls.__name__ for middleware in app.user_middleware}


def test_http_listener_is_optional_and_disables_access_logging(settings, session_factory):
    disabled = settings.model_copy(update={"http_api_enabled": False})
    assert _build_http_api_server(disabled, session_factory) is None

    enabled = settings.model_copy(
        update={
            "http_api_enabled": True,
            "http_api_token": API_TOKEN,
            "http_api_user_telegram_id": 42,
            "http_api_host": "127.0.0.1",
            "http_api_port": 8080,
        }
    )
    server = _build_http_api_server(enabled, session_factory)
    assert server.config.access_log is False
    assert server.config.log_level == "critical"
    assert server.config.host == "127.0.0.1"
    assert server.config.port == 8080
    with server.capture_signals():
        pass


def test_http_api_configuration_requires_a_strong_token_and_owner():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, http_api_enabled=True, http_api_token="short")
    with pytest.raises(ValidationError):
        Settings(_env_file=None, http_api_enabled=True, http_api_token=API_TOKEN)


@pytest.mark.asyncio
async def test_runtime_creates_only_the_configured_api_user(settings, session_factory):
    disabled = settings.model_copy(update={"http_api_enabled": False})
    await _ensure_http_api_user(session_factory, disabled)
    async with session_factory() as session:
        assert await session.scalar(select(func.count(User.id))) == 0

    enabled = settings.model_copy(
        update={
            "http_api_enabled": True,
            "http_api_token": API_TOKEN,
            "http_api_user_telegram_id": 42,
        }
    )
    await _ensure_http_api_user(session_factory, enabled)
    async with session_factory() as session:
        user = await session.scalar(select(User).where(User.telegram_user_id == 42))
        assert user is not None
        assert user.telegram_chat_id is None
        assert user.timezone == settings.default_timezone
