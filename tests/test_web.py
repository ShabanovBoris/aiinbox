import httpx
import pytest
from sqlalchemy import select

from app.domain.enums import ProcessingStatus, SourceType
from app.domain.models import AnalysisResult
from app.domain.priority import PriorityEngine
from app.errors import AppError
from app.extractors.web import PinningTransport, WebPageExtractor
from app.services.analysis import Analyzer
from app.services.ingestion import ingest_message
from app.services.processing import ProcessingPipeline
from app.services.url_security import validate_url_security
from app.storage.models import Content, Item
from app.workers.processing import ProcessingWorker, requeue_stale
from tests.fakes import FakeLlmProvider, make_analysis
from tests.fixtures_html import ARTICLE_HTML, TINY_HTML

URL = "https://example.com/article"

FAKE_RESOLVER = {"example.com": ["93.184.216.34"], "internal.example.com": ["10.1.2.3"]}


async def fake_resolver(host: str) -> list[str]:
    if host not in FAKE_RESOLVER:
        raise OSError(f"unexpected host {host}")
    return FAKE_RESOLVER[host]


async def broken_resolver(host: str) -> list[str]:
    raise OSError(f"unexpected host {host}")


def mock_client(handler) -> httpx.AsyncClient:
    """Клиент с DNS-pinning транспортом: как в проде, но с fake-resolver'ом
    и MockTransport вместо реальной сети."""
    return httpx.AsyncClient(
        transport=PinningTransport(inner=httpx.MockTransport(handler), resolver=fake_resolver),
        follow_redirects=False,
        timeout=5,
    )


def make_extractor(handler, **overrides) -> WebPageExtractor:
    defaults = dict(
        min_text_length=100,
        timeout_seconds=5,
        max_download_bytes=1_000_000,
        resolver=fake_resolver,
    )
    defaults.update(overrides)
    resolver = defaults.pop("resolver")
    defaults["client_factory"] = lambda: httpx.AsyncClient(
        transport=PinningTransport(inner=httpx.MockTransport(handler), resolver=resolver),
        follow_redirects=False,
        timeout=defaults.get("timeout_seconds", 5),
    )
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
async def test_private_and_non_http_targets_rejected(url):
    # IP-литералы и localhost отвергаются до DNS; не-http схемы — по allowlist.
    with pytest.raises(AppError) as exc_info:
        await validate_url_security(url)
    assert exc_info.value.code == "SECURITY_REJECTED"


async def test_public_https_accepted():
    await validate_url_security(URL, resolver=fake_resolver)  # не должно бросать


async def test_dns_to_private_ip_rejected():
    with pytest.raises(AppError) as exc_info:
        await validate_url_security("https://internal.example.com/", resolver=fake_resolver)
    assert exc_info.value.code == "SECURITY_REJECTED"


async def test_dns_failure_is_download_failed():
    import socket

    async def dead_resolver(host):
        raise socket.gaierror("resolution failed")

    with pytest.raises(AppError) as exc_info:
        await validate_url_security("https://nonexistent.example.com/", resolver=dead_resolver)
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
    assert await requeue_stale(session_factory) == 1
    assert await worker.process_one() is True
    assert extractor.calls == 1  # extraction не повторялся


async def _get_item(session_factory, item_id):
    async with session_factory() as session:
        return await session.get(Item, item_id)


async def _select_contents(session_factory, item_id):
    async with session_factory() as session:
        return (await session.scalars(select(Content).where(Content.item_id == item_id))).all()


async def test_transient_5xx_retried_then_success():
    attempts = {"count": 0}

    def handler(request):
        attempts["count"] += 1
        if attempts["count"] == 1:
            return httpx.Response(503, text="temporarily down")
        return httpx.Response(200, text=ARTICLE_HTML)

    extractor = make_extractor(handler)
    content = await extractor.extract(make_web_item())
    assert attempts["count"] == 2
    assert len(content.text) >= 100


async def test_permanent_4xx_not_retried():
    attempts = {"count": 0}

    def handler(request):
        attempts["count"] += 1
        return httpx.Response(404, text="nope")

    extractor = make_extractor(handler)
    with pytest.raises(AppError) as exc_info:
        await extractor.extract(make_web_item())
    assert exc_info.value.code == "DOWNLOAD_FAILED"
    assert attempts["count"] == 1  # permanent: ровно одна попытка


async def test_security_rejection_not_retried():
    async def rejecting_resolver(host):
        raise AppError("SECURITY_REJECTED", "forbidden")

    extractor = make_extractor(
        lambda request: httpx.Response(200, text=ARTICLE_HTML),
        resolver=rejecting_resolver,
    )
    with pytest.raises(AppError) as exc_info:
        await extractor.extract(make_web_item())
    assert exc_info.value.code == "SECURITY_REJECTED"


async def test_oversized_streaming_without_content_length_is_too_large():
    async def body():
        yield b"x" * 700_000
        yield b"x" * 700_000

    def handler(request):
        # Нет Content-Length: единственная защита — инкрементальный byte-cap.
        return httpx.Response(200, content=body())

    extractor = make_extractor(handler)
    with pytest.raises(AppError) as exc_info:
        await extractor.extract(make_web_item())
    assert exc_info.value.code == "TOO_LARGE"


def test_custom_timeout_applied_to_default_client():
    extractor = WebPageExtractor(timeout_seconds=7.5)
    client = extractor._default_client()
    assert client.timeout.connect == 7.5


async def test_normalize_url_preserves_port_and_trailing_slash():
    from app.services.url_parsing import normalize_url

    assert normalize_url("https://Example.com:8443/a/") == "https://example.com:8443/a/"
    assert normalize_url("https://example.com") == "https://example.com"
    # default-порт избыточен и может убираться
    assert normalize_url("https://example.com:443/x") == "https://example.com/x"


async def test_web_resume_restores_full_normalized_content(session_factory):
    # Регрессия: после restart из ANALYZING анализатор получает эквивалентный
    # NormalizedContent (text/title/url/user_note/author/language), а не голый text.
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
    worker = ProcessingWorker(
        session_factory, ProcessingPipeline(Analyzer(provider), PriorityEngine(), extractor)
    )

    async def process_and_crash_after_analysis():
        await worker.process_one()

    # Смерть после успешного анализа: статус -> PROCESSING/ANALYZING вручную
    assert await worker.process_one() is True
    first_content = provider.calls[0][0]

    async with session_factory() as session:
        row = await session.get(Item, item.id)
        row.processing_status = ProcessingStatus.PROCESSING
        row.processing_stage = "ANALYZING"
        await session.commit()
    assert await requeue_stale(session_factory) == 1
    assert await worker.process_one() is True

    second_content = provider.calls[1][0]
    assert second_content == first_content  # pydantic equality по всем полям
    assert isinstance(make_analysis(), AnalysisResult)  # sanity: типы стабильны
