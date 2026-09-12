import socket

import httpx
import pytest
from sqlalchemy import select

from app.domain.enums import ProcessingStatus, SourceType
from app.domain.priority import PriorityEngine
from app.errors import AppError
from app.extractors.web import WebPageExtractor
from app.services.analysis import Analyzer
from app.services.ingestion import ingest_message
from app.services.processing import ProcessingPipeline
from app.services.url_security import validate_url_security
from app.storage.models import Content, Item
from app.workers.processing import ProcessingWorker
from tests.fakes import FakeLlmProvider, make_analysis
from tests.fixtures_html import ARTICLE_HTML, TINY_HTML

URL = "https://example.com/article"


def mock_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=False, timeout=5
    )


def make_extractor(handler, **overrides) -> WebPageExtractor:
    defaults = dict(
        min_text_length=100,
        timeout_seconds=5,
        max_download_bytes=1_000_000,
        client_factory=lambda: mock_client(handler),
    )
    defaults.update(overrides)
    return WebPageExtractor(**defaults)


def make_web_item(source_url: str = URL) -> Item:
    return Item(
        id=1,
        user_id=1,
        source_type=SourceType.WEB,
        source_url=source_url,
        processing_status=ProcessingStatus.PROCESSING,
        user_note="заметка",
    )


class _FakeLoop:
    def __init__(self, ip: str):
        self.ip = ip

    async def getaddrinfo(self, host, port, **kwargs):
        return [(socket.AF_INET, None, None, "", (self.ip, 0))]


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost/page",
        "http://127.0.0.1/page",
        "http://10.0.0.5/page",
        "http://192.168.1.10/page",
        "http://[::1]/page",
        "http://169.254.169.254/latest/meta-data/",
        "ftp://example.com/file",
        "file:///etc/passwd",
    ],
)
async def test_private_and_non_http_targets_rejected(url, monkeypatch):
    monkeypatch.setattr("asyncio.get_running_loop", lambda: _FakeLoop("93.184.216.34"))
    with pytest.raises(AppError) as exc_info:
        await validate_url_security(url)
    assert exc_info.value.code == "SECURITY_REJECTED"


async def test_public_https_accepted(monkeypatch):
    monkeypatch.setattr("asyncio.get_running_loop", lambda: _FakeLoop("93.184.216.34"))
    await validate_url_security(URL)  # не должно бросать


async def test_dns_to_private_ip_rejected(monkeypatch):
    monkeypatch.setattr("asyncio.get_running_loop", lambda: _FakeLoop("10.1.2.3"))
    with pytest.raises(AppError) as exc_info:
        await validate_url_security("https://internal.example.com/")
    assert exc_info.value.code == "SECURITY_REJECTED"


async def test_dns_failure_is_download_failed(monkeypatch):
    class _DeadLoop:
        async def getaddrinfo(self, host, port, **kwargs):
            raise socket.gaierror("resolution failed")

    monkeypatch.setattr("asyncio.get_running_loop", lambda: _DeadLoop())
    with pytest.raises(AppError) as exc_info:
        await validate_url_security("https://nonexistent.example.com/")
    assert exc_info.value.code == "DOWNLOAD_FAILED"


async def test_article_extraction():
    extractor = make_extractor(lambda request: httpx.Response(200, text=ARTICLE_HTML))
    content = await extractor.extract(make_web_item())
    assert "оркестратор" in content.text.lower()
    assert content.title == "Архитектура AI-агентов"
    assert content.url == URL
    assert content.user_note == "заметка"


async def test_redirect_to_private_ip_rejected():
    def handler(request):
        return httpx.Response(302, headers={"location": "http://169.254.169.254/latest"})

    extractor = make_extractor(handler)
    with pytest.raises(AppError) as exc_info:
        await extractor.extract(make_web_item())
    assert exc_info.value.code == "SECURITY_REJECTED"


async def test_http_404_is_download_failed():
    extractor = make_extractor(lambda request: httpx.Response(404, text="nope"))
    with pytest.raises(AppError) as exc_info:
        await extractor.extract(make_web_item())
    assert exc_info.value.code == "DOWNLOAD_FAILED"


async def test_oversized_response_is_too_large():
    def handler(request):
        return httpx.Response(200, headers={"content-length": str(10_000_000)}, text="x")

    extractor = make_extractor(handler)
    with pytest.raises(AppError) as exc_info:
        await extractor.extract(make_web_item())
    assert exc_info.value.code == "TOO_LARGE"


async def test_timeout_maps_to_timeout_code():
    def handler(request):
        raise httpx.ConnectTimeout("timeout")

    extractor = make_extractor(handler)
    with pytest.raises(AppError) as exc_info:
        await extractor.extract(make_web_item())
    assert exc_info.value.code == "TIMEOUT"


async def test_insufficient_static_text_uses_renderer_fallback():
    calls = {"render": 0}

    async def renderer(url):
        calls["render"] += 1
        return ARTICLE_HTML

    extractor = make_extractor(lambda request: httpx.Response(200, text=TINY_HTML))
    extractor._renderer = renderer
    content = await extractor.extract(make_web_item())
    assert calls["render"] == 1
    assert len(content.text) >= 100


async def test_insufficient_after_fallback_is_extraction_failed():
    async def renderer(url):
        return TINY_HTML

    extractor = make_extractor(
        lambda request: httpx.Response(200, text=TINY_HTML), renderer=renderer
    )
    with pytest.raises(AppError) as exc_info:
        await extractor.extract(make_web_item())
    assert exc_info.value.code == "EXTRACTION_FAILED"


class CountingWebExtractor(WebPageExtractor):
    def __init__(self, handler, **kwargs):
        super().__init__(client_factory=lambda: mock_client(handler), **kwargs)
        self.calls = 0

    async def extract(self, item):
        self.calls += 1
        return await super().extract(item)


async def test_web_pipeline_persists_content_and_resumes_without_redownload(session_factory):
    ingested = await ingest_message(
        session_factory,
        telegram_user_id=42,
        chat_id=42,
        message_id=1,
        text="Полезная статья https://example.com/article",
    )
    item = ingested.items[0]

    extractor = CountingWebExtractor(
        lambda request: httpx.Response(200, text=ARTICLE_HTML), min_text_length=100
    )
    provider = FakeLlmProvider()
    pipeline = ProcessingPipeline(Analyzer(provider), PriorityEngine(), extractor)
    worker = ProcessingWorker(session_factory, pipeline, poll_seconds=0.01)

    assert await worker.process_one() is True
    stored = await _get_item(session_factory, item.id)
    assert stored.title == make_analysis().title
    # LLM получил текст страницы и заметку пользователя как сигнал намерения
    assert provider.calls[0][0].text != ""
    assert provider.calls[0][0].user_note == "Полезная статья"
    contents = await _select_contents(session_factory, item.id)
    assert len(contents) == 1 and contents[0].text != ""

    # Restart-семантика: WEB_TEXT персистен — resume из ANALYZING не перекачивает
    async with session_factory() as session:
        row = await session.get(Item, item.id)
        row.processing_status = ProcessingStatus.PROCESSING
        row.processing_stage = "ANALYZING"
        await session.commit()
    from app.workers.processing import requeue_stale

    assert await requeue_stale(session_factory) == 1
    assert await worker.process_one() is True
    assert extractor.calls == 1  # extraction не повторялся


async def _get_item(session_factory, item_id):
    async with session_factory() as session:
        return await session.get(Item, item_id)


async def _select_contents(session_factory, item_id):
    async with session_factory() as session:
        return (await session.scalars(select(Content).where(Content.item_id == item_id))).all()
