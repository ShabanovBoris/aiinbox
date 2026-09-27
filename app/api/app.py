"""FastAPI interface adapter over the existing AIInbox application services."""

import hashlib
import json
import logging
import secrets
import time
import traceback
from datetime import UTC, datetime
from typing import Annotated
from zoneinfo import ZoneInfoNotFoundError

from fastapi import Depends, FastAPI, Header, Path, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.models import (
    MAX_API_SEARCH_QUERY_CHARS,
    APIErrorDetail,
    APIErrorResponse,
    AskAcceptedResponse,
    AskDoneResponse,
    AskFailedResponse,
    AskPendingResponse,
    AskPollResponse,
    AskReferenceResponse,
    AskRequest,
    AttentionItemResponse,
    AttentionResponse,
    CaptureAcceptedResponse,
    CaptureRequest,
    CategoryCountResponse,
    HealthResponse,
    InterestRequest,
    ItemDetailResponse,
    ItemListEntryResponse,
    ItemListResponse,
    ItemPageResponse,
    ItemSourceResponse,
    NotificationSettingsResponse,
    SettingsPatchRequest,
    SnoozeRequest,
    WeeklyBacklogResponse,
    WeeklyFlowResponse,
    WeeklyRecommendationResponse,
    WeeklyReminderOutcomesResponse,
    WeeklyResponse,
)
from app.bot.presentation import item_display_title
from app.config import Settings
from app.domain.enums import ItemState, ItemType, ProcessingStatus
from app.domain.models import AskDeliveryPayload
from app.errors import AppError
from app.services.actions import apply_item_action_for_user_id, set_item_interest_for_user_id
from app.services.ask_inbox import enqueue_http_ask, safe_http_url
from app.services.attention_ranking import AttentionRankingService
from app.services.delivery import resolve_ask_references
from app.services.ingestion import ingest_external_text
from app.services.notifications import (
    get_notification_settings_for_user_id,
    parse_timezone,
    update_notification_settings_for_user_id,
)
from app.services.retrieval import (
    MAX_API_CURSOR_CHARS,
    TodayService,
    list_api_item_page,
    load_item_sources_by_item,
    search_items,
)
from app.services.weekly_review import WeeklyReviewService
from app.storage.models import AskJob, Item, ItemSource, User

log = logging.getLogger(__name__)

MAX_HTTP_BODY_BYTES = 64 * 1024
MAX_IDEMPOTENCY_KEY_CHARS = 128


class APIError(Exception):
    """Carry only a deliberately chosen HTTP status and safe public error text."""

    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(code)
        self.status_code = status_code
        self.code = code
        self.message = message


class BodySizeLimitMiddleware:
    """Bound ASGI request buffering before FastAPI or Pydantic sees the payload."""

    def __init__(self, app, max_bytes: int = MAX_HTTP_BODY_BYTES):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            if message["type"] != "http.request":
                continue
            chunk = message.get("body", b"")
            if len(body) + len(chunk) > self.max_bytes:
                response = JSONResponse(
                    status_code=413,
                    content={
                        "error": {
                            "code": "REQUEST_TOO_LARGE",
                            "message": "Request body exceeds the allowed size",
                        }
                    },
                )
                await response(scope, receive, send)
                return
            body.extend(chunk)
            if not message.get("more_body", False):
                break

        content = bytes(body)
        replayed = False

        async def replay_receive():
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": content, "more_body": False}
            return {"type": "http.disconnect"}

        await self.app(scope, replay_receive, send)


def _error_response(
    status_code: int,
    code: str,
    message: str,
    *,
    fields: list[str] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    """Project controlled failures without reflecting request or provider content."""
    error = {"code": code, "message": message}
    if fields:
        error["fields"] = fields
    return JSONResponse(status_code=status_code, content={"error": error}, headers=headers)


def _common_error_responses() -> dict[int, dict]:
    """Keep the generated OpenAPI contract explicit for shared HTTP failures."""
    return {
        401: {"model": APIErrorResponse, "description": "Bearer authentication failed"},
        404: {"model": APIErrorResponse, "description": "Resource not found"},
        409: {"model": APIErrorResponse, "description": "Idempotency conflict"},
        410: {"model": APIErrorResponse, "description": "Transient Ask result expired"},
        413: {"model": APIErrorResponse, "description": "Request body is too large"},
        422: {"model": APIErrorResponse, "description": "Request validation failed"},
        500: {"model": APIErrorResponse, "description": "Unexpected server error"},
        503: {"model": APIErrorResponse, "description": "Database is unavailable"},
    }


def _utc_timestamp(value: datetime | None) -> str | None:
    """Serialize SQLite naive UTC and aware application values as RFC3339 Z."""
    if value is None:
        return None
    instant = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return instant.isoformat().replace("+00:00", "Z")


def _canonical_hash(value: dict) -> str:
    """Hash only validated accepted fields using a deterministic UTF-8 encoding."""
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _source_url(source: ItemSource) -> str | None:
    """Expose only validated HTTP(S) identities and hide rejected extraction URLs."""
    if source.error_code == "SECURITY_REJECTED":
        return None
    value = safe_http_url(source.source_url)
    return value[:700] if value else None


def _item_list_entry(
    item: Item, sources: list[ItemSource] | tuple[ItemSource, ...] = ()
) -> ItemListEntryResponse:
    """Project an Item without transcripts, transport ids, or internal errors."""
    return ItemListEntryResponse(
        id=item.id,
        title=item_display_title(item, sources)[:300],
        summary=item.summary[:1200] if item.summary else None,
        category=item.category[:100] if item.category else None,
        item_type=item.item_type,
        processing_status=item.processing_status,
        state=item.state,
        priority_score=item.priority_score,
        interest_level=item.interest_level,
        created_at=_utc_timestamp(item.created_at) or "",
        updated_at=_utc_timestamp(item.updated_at) or "",
    )


def _item_detail(item: Item, sources: list[ItemSource]) -> ItemDetailResponse:
    """Project one bounded canonical Item and its safe source identities."""
    rejected_urls = {
        source.source_url
        for source in sources
        if source.error_code == "SECURITY_REJECTED" and source.source_url
    }
    primary_url = None if item.source_url in rejected_urls else safe_http_url(item.source_url)
    return ItemDetailResponse(
        **_item_list_entry(item, sources).model_dump(),
        processing_stage=(item.processing_stage or "")[:32],
        source_type=item.source_type,
        source_url=primary_url[:700] if primary_url else None,
        tags=[tag[:100] for tag in (item.tags_json or []) if isinstance(tag, str)][:8],
        priority_reason=item.priority_reason[:500] if item.priority_reason else None,
        next_action=item.next_action[:250] if item.next_action else None,
        estimated_action_minutes=item.estimated_action_minutes,
        suggested_due_at=_utc_timestamp(item.suggested_due_at),
        completed_at=_utc_timestamp(item.completed_at),
        archived_at=_utc_timestamp(item.archived_at),
        snoozed_until=_utc_timestamp(item.snoozed_until),
        error_code=item.error_code[:64] if item.error_code else None,
        sources=[
            ItemSourceResponse(
                id=source.id,
                source_index=source.source_index,
                source_type=source.source_type,
                source_url=_source_url(source),
                extraction_status=source.extraction_status[:16],
                error_code=source.error_code[:64] if source.error_code else None,
            )
            for source in sources
        ],
    )


async def _load_item_detail(
    session_factory: async_sessionmaker,
    user_id: int,
    item_id: int,
) -> ItemDetailResponse:
    """Load detail under authenticated ownership before projecting source metadata."""
    async with session_factory() as session:
        item = await session.scalar(select(Item).where(Item.id == item_id, Item.user_id == user_id))
        if item is None:
            raise APIError(404, "ITEM_NOT_FOUND", "Item not found")
        sources = list(
            (
                await session.scalars(
                    select(ItemSource)
                    .where(ItemSource.item_id == item.id)
                    .order_by(ItemSource.source_index, ItemSource.id)
                )
            ).all()
        )
        return _item_detail(item, sources)


def _category_count(value) -> CategoryCountResponse | None:
    """Convert one optional WeeklyReview category aggregate to its HTTP DTO."""
    if value is None:
        return None
    return CategoryCountResponse(category=value.category, count=value.count)


def create_api_app(settings: Settings, session_factory: async_sessionmaker) -> FastAPI:
    """Compose a testable HTTP adapter without starting Telegram or opening a socket."""
    app = FastAPI(
        title="Personal AI Inbox API",
        version="1.0.0",
        description=(
            "Authenticated /v1 interface over the canonical AIInbox services. "
            "Capture and Ask are asynchronous; poll their resource locations."
        ),
    )
    app.state.settings = settings
    app.state.session_factory = session_factory
    bearer_scheme = HTTPBearer(auto_error=False, scheme_name="BearerAuth")
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=MAX_HTTP_BODY_BYTES)

    @app.middleware("http")
    async def log_safe_request_metadata(request: Request, call_next):
        """Log route templates and timing while excluding query strings and bodies."""
        started = time.perf_counter()
        response = await call_next(request)
        route = request.scope.get("route")
        route_template = getattr(route, "path", "unmatched")
        user_id = getattr(request.state, "user_id", None)
        duration_ms = int((time.perf_counter() - started) * 1000)
        log.info(
            "http api request method=%s route=%s status=%s duration_ms=%s user_id=%s",
            request.method,
            route_template,
            response.status_code,
            duration_ms,
            user_id,
        )
        return response

    async def current_user_id(
        request: Request,
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    ) -> int:
        """Map a valid single-user bearer credential to the canonical persisted User.id."""
        configured_token = request.app.state.settings.http_api_token
        if (
            credentials is None
            or credentials.scheme.casefold() != "bearer"
            or not secrets.compare_digest(
                credentials.credentials.encode("utf-8"), configured_token.encode("utf-8")
            )
        ):
            raise APIError(401, "UNAUTHORIZED", "Unauthorized")

        configured_user_id = request.app.state.settings.http_api_user_telegram_id
        if configured_user_id is None:
            raise APIError(503, "API_USER_UNAVAILABLE", "API user is unavailable")
        async with request.app.state.session_factory() as session:
            user_id = await session.scalar(
                select(User.id).where(User.telegram_user_id == configured_user_id)
            )
        if user_id is None:
            raise APIError(503, "API_USER_UNAVAILABLE", "API user is unavailable")
        request.state.user_id = user_id
        return user_id

    async def required_idempotency_key(
        value: Annotated[
            str,
            Header(
                alias="Idempotency-Key",
                min_length=1,
                max_length=MAX_IDEMPOTENCY_KEY_CHARS,
            ),
        ],
    ) -> str:
        """Validate opaque create identity without ever logging or reflecting it."""
        if not value.strip() or not value.isprintable():
            raise APIError(422, "VALIDATION_ERROR", "Invalid Idempotency-Key")
        return value

    @app.exception_handler(APIError)
    async def api_error_handler(request: Request, exc: APIError):
        """Return only the controlled status/code/message selected by the adapter."""
        headers = {"WWW-Authenticate": "Bearer"} if exc.status_code == 401 else None
        return _error_response(exc.status_code, exc.code, exc.message, headers=headers)

    @app.exception_handler(AppError)
    async def application_error_handler(request: Request, exc: AppError):
        """Translate application boundary failures without exposing provider details."""
        if exc.code == "IDEMPOTENCY_CONFLICT":
            return _error_response(
                409,
                "IDEMPOTENCY_CONFLICT",
                "Idempotency key was already used for a different request",
            )
        if exc.code == "USER_NOT_FOUND":
            return _error_response(503, "API_USER_UNAVAILABLE", "API user is unavailable")
        return _error_response(422, "VALIDATION_ERROR", "Request could not be accepted")

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError):
        """Bound validation locations and omit Pydantic input values and raw messages."""
        if any(error.get("loc", ())[-1:] == ("cursor",) for error in exc.errors()):
            return _error_response(422, "INVALID_CURSOR", "Cursor is invalid")
        fields: list[str] = []
        for error in exc.errors():
            parts = [
                str(part)
                for part in error.get("loc", ())
                if str(part) not in {"body", "query", "path", "header"}
            ]
            if parts:
                fields.append(".".join(parts)[:80])
        bounded_fields = list(dict.fromkeys(fields))[:8]
        return _error_response(
            422,
            "VALIDATION_ERROR",
            "Request validation failed",
            fields=bounded_fields,
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException):
        """Keep framework-generated 404/405 responses inside the public envelope."""
        if exc.status_code == 404:
            return _error_response(404, "NOT_FOUND", "Not found")
        if exc.status_code == 405:
            return _error_response(405, "METHOD_NOT_ALLOWED", "Method not allowed")
        return _error_response(exc.status_code, "HTTP_ERROR", "Request failed")

    @app.exception_handler(Exception)
    async def internal_error_handler(request: Request, exc: Exception):
        """Log stack frames for operators without logging exception text or request data."""
        route = request.scope.get("route")
        log.error(
            "http api internal error route=%s user_id=%s exception_type=%s traceback=%s",
            getattr(route, "path", "unmatched"),
            getattr(request.state, "user_id", None),
            type(exc).__name__,
            "".join(traceback.format_tb(exc.__traceback__))[-8_000:],
        )
        return _error_response(500, "INTERNAL_ERROR", "Internal server error")

    @app.get(
        "/healthz",
        response_model=HealthResponse,
        responses={503: {"model": APIErrorResponse}},
    )
    async def healthz():
        """Probe only database connectivity; the public result contains no private state."""
        try:
            async with app.state.session_factory() as session:
                await session.execute(text("SELECT 1"))
        except SQLAlchemyError as exc:
            log.warning("http api health check failed exception_type=%s", type(exc).__name__)
            return _error_response(503, "SERVICE_UNAVAILABLE", "Service unavailable")
        return HealthResponse(status="ok")

    @app.post(
        "/v1/items",
        status_code=202,
        response_model=CaptureAcceptedResponse,
        responses={**_common_error_responses(), 202: {"model": CaptureAcceptedResponse}},
        summary="Capture text or URLs for asynchronous processing",
    )
    async def create_item(
        body: CaptureRequest,
        response: Response,
        user_id: Annotated[int, Depends(current_user_id)],
        idempotency_key: Annotated[str, Depends(required_idempotency_key)],
    ):
        """Persist one canonical Item and return before any extraction or LLM work."""
        session_factory = app.state.session_factory
        request_hash = _canonical_hash({"text": body.text, "user_note": body.user_note})
        try:
            result = await ingest_external_text(
                session_factory,
                user_id=user_id,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                text=body.text,
                user_note=body.user_note,
            )
        except ValueError:
            raise APIError(422, "VALIDATION_ERROR", "Capture request is invalid") from None
        item = result.items[0]
        response.headers["Location"] = f"/v1/items/{item.id}"
        return CaptureAcceptedResponse(
            id=item.id,
            processing_status=item.processing_status,
            state=item.state,
            created_at=_utc_timestamp(item.created_at) or "",
        )

    @app.get(
        "/v1/items",
        response_model=ItemPageResponse,
        responses=_common_error_responses(),
        summary="Browse Items with bounded cursor pagination",
    )
    async def get_items(
        request: Request,
        user_id: Annotated[int, Depends(current_user_id)],
        limit: Annotated[int, Query(ge=1, le=50)] = 20,
        cursor: Annotated[str | None, Query(max_length=MAX_API_CURSOR_CHARS)] = None,
        state: ItemState | None = None,
        processing_status: ProcessingStatus | None = None,
        category: Annotated[str | None, Query(max_length=100)] = None,
        item_type: ItemType | None = None,
    ):
        """Read a deterministic recent page through the retrieval application seam."""
        async with request.app.state.session_factory() as session:
            try:
                page = await list_api_item_page(
                    session,
                    user_id,
                    limit=limit,
                    cursor=cursor,
                    state=state,
                    processing_status=processing_status,
                    category=category,
                    item_type=item_type,
                )
            except ValueError:
                raise APIError(422, "INVALID_CURSOR", "Cursor is invalid") from None
            sources_by_item = await load_item_sources_by_item(
                session, [item.id for item in page.items]
            )
            entries = [
                _item_list_entry(item, sources_by_item.get(item.id, [])) for item in page.items
            ]
        return ItemPageResponse(items=entries, next_cursor=page.next_cursor)

    @app.get(
        "/v1/items/{item_id}",
        response_model=ItemDetailResponse,
        responses=_common_error_responses(),
    )
    async def get_item(
        item_id: Annotated[int, Path(gt=0)],
        user_id: Annotated[int, Depends(current_user_id)],
    ):
        """Return only the authenticated owner's bounded Item/source projection."""
        return await _load_item_detail(app.state.session_factory, user_id, item_id)

    @app.get(
        "/v1/today",
        response_model=ItemListResponse,
        responses=_common_error_responses(),
    )
    async def get_today(
        request: Request,
        user_id: Annotated[int, Depends(current_user_id)],
        limit: Annotated[int, Query(ge=1, le=5)] = 3,
    ):
        """Reuse TodayService as a read-only selection without recording exposure."""
        async with request.app.state.session_factory() as session:
            items = await TodayService().list_items(session, user_id, limit)
            sources_by_item = await load_item_sources_by_item(session, [item.id for item in items])
        return ItemListResponse(
            items=[_item_list_entry(item, sources_by_item.get(item.id, [])) for item in items]
        )

    @app.get(
        "/v1/attention",
        response_model=AttentionResponse,
        responses=_common_error_responses(),
    )
    async def get_attention(
        request: Request,
        user_id: Annotated[int, Depends(current_user_id)],
        limit: Annotated[int, Query(ge=1, le=5)] = 3,
    ):
        """Reuse PM-07 ranking without Telegram delivery or ATTENTION_SHOWN Events."""
        async with request.app.state.session_factory() as session:
            ranked = await AttentionRankingService().list_ranked(session, user_id, limit=limit)
            sources_by_item = await load_item_sources_by_item(
                session, [item.id for item, _rank in ranked]
            )
        return AttentionResponse(
            items=[
                AttentionItemResponse(
                    **_item_list_entry(item, sources_by_item.get(item.id, [])).model_dump(),
                    attention_score=rank.score,
                    interest_adjustment=rank.interest_adjustment,
                    age_bonus=rank.age_bonus,
                    neglect_bonus=rank.neglect_bonus,
                    due_bonus=rank.due_bonus,
                    stale_important_bonus=rank.stale_important_bonus,
                    recent_show_penalty=rank.recent_show_penalty,
                    reminder_preference_penalty=rank.reminder_preference_penalty,
                    notification_fatigue_penalty=rank.notification_fatigue_penalty,
                )
                for item, rank in ranked
            ]
        )

    @app.get(
        "/v1/search",
        response_model=ItemListResponse,
        responses=_common_error_responses(),
    )
    async def search(
        request: Request,
        user_id: Annotated[int, Depends(current_user_id)],
        q: Annotated[str, Query(max_length=MAX_API_SEARCH_QUERY_CHARS)] = "",
        limit: Annotated[int, Query(ge=1, le=20)] = 10,
    ):
        """Preserve current lexical FTS semantics and include all lifecycle states."""
        if not q.strip():
            return ItemListResponse(items=[])
        async with request.app.state.session_factory() as session:
            items = await search_items(session, user_id, q, limit)
            sources_by_item = await load_item_sources_by_item(session, [item.id for item in items])
            # FTS is a derived index; retaining its rebuild does not mutate Items,
            # Events, or Reminder state observed by this GET.
            await session.commit()
        return ItemListResponse(
            items=[_item_list_entry(item, sources_by_item.get(item.id, [])) for item in items]
        )

    @app.get(
        "/v1/weekly",
        response_model=WeeklyResponse,
        responses=_common_error_responses(),
    )
    async def get_weekly(
        request: Request,
        user_id: Annotated[int, Depends(current_user_id)],
    ):
        """Reuse the existing timezone-aware WeeklyReview read projection."""
        async with request.app.state.session_factory() as session:
            user = await session.get(User, user_id)
            if user is None:
                raise APIError(404, "USER_NOT_FOUND", "User not found")
            try:
                zone = parse_timezone(user.timezone)
            except (ValueError, ZoneInfoNotFoundError):
                zone = parse_timezone(request.app.state.settings.default_timezone)
            review = await WeeklyReviewService().build(session, user_id, zone=zone)
        return WeeklyResponse(
            flow=WeeklyFlowResponse(
                created=review.flow.created,
                completed=review.flow.completed,
                archived=review.flow.archived,
                net_change=review.flow.net_change,
            ),
            backlog=WeeklyBacklogResponse(
                active_actionable=review.backlog.active_actionable,
                high_priority=review.backlog.high_priority,
                high_interest=review.backlog.high_interest,
                stale=review.backlog.stale,
                old_important_unrevisited=review.backlog.old_important_unrevisited,
            ),
            created_categories=[
                CategoryCountResponse(category=value.category, count=value.count)
                for value in review.created_categories
            ],
            completed_categories=[
                CategoryCountResponse(category=value.category, count=value.count)
                for value in review.completed_categories
            ],
            most_postponed=_category_count(review.most_postponed),
            strongest_progress=_category_count(review.strongest_progress),
            reminder_outcomes=(
                WeeklyReminderOutcomesResponse(
                    sent=review.reminder_outcomes.sent,
                    opened=review.reminder_outcomes.opened,
                    snoozed=review.reminder_outcomes.snoozed,
                    done=review.reminder_outcomes.done,
                    dismissed=review.reminder_outcomes.dismissed,
                    disliked=review.reminder_outcomes.disliked,
                )
                if review.reminder_outcomes
                else None
            ),
            recommendations=[
                WeeklyRecommendationResponse(
                    kind=value.kind,
                    item_id=value.item_id,
                    title=value.title[:300],
                    estimated_action_minutes=value.estimated_action_minutes,
                )
                for value in review.recommendations
            ],
        )

    @app.post(
        "/v1/ask",
        status_code=202,
        response_model=AskAcceptedResponse,
        responses={**_common_error_responses(), 202: {"model": AskAcceptedResponse}},
    )
    async def create_ask(
        body: AskRequest,
        response: Response,
        user_id: Annotated[int, Depends(current_user_id)],
        idempotency_key: Annotated[str, Depends(required_idempotency_key)],
    ):
        """Queue one HTTP AskJob; synthesis and citations remain in the shared worker."""
        question = body.question.strip()
        request_hash = _canonical_hash({"question": question})
        try:
            job = await enqueue_http_ask(
                app.state.session_factory,
                user_id=user_id,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                question=question,
            )
        except ValueError:
            raise APIError(422, "VALIDATION_ERROR", "Ask request is invalid") from None
        response.headers["Location"] = f"/v1/asks/{job.id}"
        return AskAcceptedResponse(id=job.id, status=job.status)

    @app.get(
        "/v1/asks/{ask_job_id}",
        response_model=AskPollResponse,
        responses=_common_error_responses(),
    )
    async def get_ask(
        request: Request,
        ask_job_id: Annotated[int, Path(gt=0)],
        user_id: Annotated[int, Depends(current_user_id)],
    ):
        """Poll only HTTP Ask jobs and resolve display references from trusted rows."""
        async with request.app.state.session_factory() as session:
            job = await session.scalar(
                select(AskJob).where(
                    AskJob.id == ask_job_id,
                    AskJob.user_id == user_id,
                    AskJob.response_channel == "HTTP",
                )
            )
            if job is None:
                raise APIError(404, "ASK_NOT_FOUND", "Ask job not found")
            if job.status in {"PENDING", "RUNNING"}:
                return AskPendingResponse(id=job.id, status=job.status)
            if job.status == "FAILED":
                return AskFailedResponse(
                    id=job.id,
                    status="FAILED",
                    error=APIErrorDetail(
                        code=(job.error_code or "ASK_FAILED")[:64],
                        message=(job.error_message or "Ask failed")[:300],
                    ),
                )
            if job.status != "DONE":
                raise RuntimeError("Ask job has an unknown durable status")
            now = datetime.now(UTC).replace(tzinfo=None)
            if (
                job.result_json is None
                or job.result_expires_at is None
                or job.result_expires_at <= now
            ):
                raise APIError(410, "ASK_RESULT_EXPIRED", "Ask result has expired")
            payload = AskDeliveryPayload.model_validate(job.result_json)
            references = await resolve_ask_references(session, user_id, payload)
            expires_at = _utc_timestamp(job.result_expires_at)
            answer = payload.answer
        return AskDoneResponse(
            id=ask_job_id,
            status="DONE",
            answer=answer,
            references=[
                AskReferenceResponse(
                    item_id=value.item_id,
                    source_id=value.source_id,
                    title=value.title[:120],
                    source_type=value.source_type,
                    source_url=value.source_url,
                )
                for value in references
            ],
            expires_at=expires_at or "",
        )

    @app.post(
        "/v1/items/{item_id}/done",
        response_model=ItemDetailResponse,
        responses=_common_error_responses(),
    )
    async def mark_done(
        item_id: Annotated[int, Path(gt=0)],
        user_id: Annotated[int, Depends(current_user_id)],
    ):
        """Apply the canonical idempotent DONE compare-and-set for this owner."""
        item = await apply_item_action_for_user_id(
            app.state.session_factory, user_id, item_id, "done"
        )
        if item is None:
            raise APIError(404, "ITEM_NOT_FOUND", "Item not found")
        return await _load_item_detail(app.state.session_factory, user_id, item_id)

    @app.post(
        "/v1/items/{item_id}/archive",
        response_model=ItemDetailResponse,
        responses=_common_error_responses(),
    )
    async def archive_item(
        item_id: Annotated[int, Path(gt=0)],
        user_id: Annotated[int, Depends(current_user_id)],
    ):
        """Apply the canonical idempotent ARCHIVED compare-and-set for this owner."""
        item = await apply_item_action_for_user_id(
            app.state.session_factory, user_id, item_id, "archive"
        )
        if item is None:
            raise APIError(404, "ITEM_NOT_FOUND", "Item not found")
        return await _load_item_detail(app.state.session_factory, user_id, item_id)

    @app.post(
        "/v1/items/{item_id}/snooze",
        response_model=ItemDetailResponse,
        responses=_common_error_responses(),
    )
    async def snooze_item(
        item_id: Annotated[int, Path(gt=0)],
        body: SnoozeRequest,
        user_id: Annotated[int, Depends(current_user_id)],
    ):
        """Store one future absolute instant through the existing Reminder-aware CAS."""
        instant = body.snoozed_until.astimezone(UTC)
        if instant <= datetime.now(UTC):
            raise APIError(422, "VALIDATION_ERROR", "snoozed_until must be in the future")
        item = await apply_item_action_for_user_id(
            app.state.session_factory,
            user_id,
            item_id,
            "snooze",
            snoozed_until=instant.replace(tzinfo=None),
        )
        if item is None:
            raise APIError(404, "ITEM_NOT_FOUND", "Item not found")
        return await _load_item_detail(app.state.session_factory, user_id, item_id)

    @app.patch(
        "/v1/items/{item_id}/interest",
        response_model=ItemDetailResponse,
        responses=_common_error_responses(),
    )
    async def update_interest(
        item_id: Annotated[int, Path(gt=0)],
        body: InterestRequest,
        user_id: Annotated[int, Depends(current_user_id)],
    ):
        """Record a real interest transition with the HTTP source attribution."""
        result = await set_item_interest_for_user_id(
            app.state.session_factory, user_id, item_id, body.level
        )
        if result is None:
            raise APIError(404, "ITEM_NOT_FOUND", "Item not found")
        return await _load_item_detail(app.state.session_factory, user_id, item_id)

    @app.get(
        "/v1/settings",
        response_model=NotificationSettingsResponse,
        responses=_common_error_responses(),
    )
    async def get_settings(user_id: Annotated[int, Depends(current_user_id)]):
        """Return only allowlisted user-facing notification settings."""
        current = await get_notification_settings_for_user_id(app.state.session_factory, user_id)
        if current is None:
            raise APIError(404, "USER_NOT_FOUND", "User not found")
        user, values = current
        return NotificationSettingsResponse(timezone=user.timezone, **values)

    @app.patch(
        "/v1/settings",
        response_model=NotificationSettingsResponse,
        responses=_common_error_responses(),
    )
    async def patch_settings(
        body: SettingsPatchRequest,
        user_id: Annotated[int, Depends(current_user_id)],
    ):
        """Reuse existing validation and SQLite json_patch semantics for concurrent edits."""
        values = body.model_dump(exclude_unset=True)
        try:
            updated = await update_notification_settings_for_user_id(
                app.state.session_factory,
                user_id,
                **values,
            )
        except ValueError:
            raise APIError(422, "VALIDATION_ERROR", "Settings value is invalid") from None
        if updated is None:
            raise APIError(404, "USER_NOT_FOUND", "User not found")
        user, settings_values = updated
        return NotificationSettingsResponse(timezone=user.timezone, **settings_values)

    return app
