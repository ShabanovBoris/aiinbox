from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from aiogram.types import Chat, Document, Message, MessageOriginChannel
from aiogram.types import User as TgUser
from sqlalchemy import func, select

import app.extractors.document as document_module
from app.bot.handlers import make_router, on_document
from app.domain.enums import ContentKind, ProcessingStatus, SourceType
from app.domain.models import NormalizedContent
from app.domain.priority import PriorityEngine
from app.errors import AppError
from app.extractors.document import DocumentExtractor
from app.extractors.web import PinningTransport, WebPageExtractor
from app.services.actions import apply_item_action
from app.services.analysis import Analyzer
from app.services.ingestion import ingest_media, ingest_message
from app.services.processing import ProcessingPipeline
from app.services.retrieval import search_items
from app.storage.models import Content, Item, ItemSource
from app.workers.processing import ProcessingWorker
from tests.fakes import FakeLlmProvider
from tests.fixtures_documents import make_docx_bytes, make_pdf_bytes, make_zip_bytes

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
PUBLIC_URL = "https://example.com/report.pdf"


async def _document_resolver(host: str) -> list[str]:
    """Resolve only named fake hosts so URL tests never use external DNS."""
    if host == "example.com":
        return ["93.184.216.34"]
    if host == "private.example.com":
        return ["10.0.0.9"]
    raise OSError(f"unexpected host: {host}")


def _secure_web_extractor(handler, **overrides) -> WebPageExtractor:
    """Exercise the production pinning transport over deterministic MockTransport responses."""
    options = {
        "min_text_length": 100,
        "max_download_bytes": 1_000_000,
        "max_attempts": 1,
        "resolver": _document_resolver,
        "client_factory": lambda: httpx.AsyncClient(
            transport=PinningTransport(
                inner=httpx.MockTransport(handler), resolver=_document_resolver
            ),
            follow_redirects=False,
            timeout=5,
        ),
    }
    options.update(overrides)
    return WebPageExtractor(**options)


def _web_item(url: str = PUBLIC_URL) -> Item:
    """Supply the minimal source identity accepted by WebPageExtractor."""
    return Item(
        id=1,
        user_id=1,
        source_type=SourceType.WEB,
        source_url=url,
        processing_status=ProcessingStatus.PROCESSING,
        processing_stage="EXTRACTING",
        user_note="",
    )


def _document_message(
    *,
    message_id: int = 1,
    file_name: str = "paper.pdf",
    mime_type: str | None = "application/pdf",
    file_size: int = 100,
    caption: str | None = None,
    forwarded: bool = False,
) -> Message:
    """Represent Telegram's direct and forwarded Document update shapes."""
    return Message(
        message_id=message_id,
        date=datetime.now(UTC),
        chat=Chat(id=42, type="private"),
        from_user=TgUser(id=42, is_bot=False, first_name="Owner"),
        caption=caption,
        document=Document(
            file_id=f"file-{message_id}",
            file_unique_id=f"unique-{message_id}",
            file_name=file_name,
            mime_type=mime_type,
            file_size=file_size,
        ),
        forward_origin=(
            MessageOriginChannel(
                type="channel",
                date=datetime.now(UTC),
                chat=Chat(id=-100123, type="channel", title="Research", username="research"),
                message_id=message_id,
            )
            if forwarded
            else None
        ),
    )


class PayloadDownloader:
    """Supply deterministic document bytes through the same narrow downloader contract."""

    def __init__(self, payload: bytes, failures: int = 0):
        self.payload = payload
        self.failures = failures
        self.calls = 0

    async def download(self, file_id: str, dest_dir: Path) -> Path:
        self.calls += 1
        if self.calls <= self.failures:
            raise AppError("DOWNLOAD_FAILED", "temporary download failure")
        path = Path(dest_dir) / f"download-{self.calls}.bin"
        path.write_bytes(self.payload)
        return path


class CountingDocumentExtractor(DocumentExtractor):
    """Count parser passes to verify that durable content prevents repeat work."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.parse_calls = 0

    def extract_bytes(self, data, file_name, mime_type):
        self.parse_calls += 1
        return super().extract_bytes(data, file_name, mime_type)


@pytest.mark.parametrize(
    ("file_name", "mime_type", "document_format", "data"),
    [
        ("report.pdf", "application/pdf", "pdf", make_pdf_bytes(["Selectable PDF page text."])),
        ("notes.txt", "text/plain", "txt", b"Plain UTF-8 text."),
        ("notes.markdown", "text/markdown", "markdown", b"# Markdown stays unrendered"),
        ("notes.md", "text/plain", "markdown", b"# Generic text MIME is valid for Markdown"),
        ("paper.docx", DOCX_MIME, "docx", make_docx_bytes()),
    ],
)
def test_document_extractor_normalizes_supported_formats(
    file_name, mime_type, document_format, data
):
    content = DocumentExtractor().extract_bytes(data, file_name, mime_type)

    assert content.source_type is SourceType.DOCUMENT
    assert content.text
    assert content.metadata["document_format"] == document_format
    assert content.metadata["file_name"] == file_name
    if document_format == "docx":
        assert content.text.index("paragraph before") < content.text.index("first cell")
        assert content.text.index("first cell") < content.text.index("paragraph after")


def test_pdf_extracts_text_in_page_order_and_saves_page_count():
    content = DocumentExtractor().extract_bytes(
        make_pdf_bytes(["First PDF page has selectable text.", "Second PDF page follows it."]),
        "ordered.pdf",
        "application/pdf",
    )

    assert content.text.index("First PDF page") < content.text.index("Second PDF page")
    assert content.metadata["pages"] == 2


def test_empty_or_scanned_like_pdf_fails_without_ocr():
    with pytest.raises(AppError, match="OCR") as error:
        DocumentExtractor().extract_bytes(make_pdf_bytes([""]), "scan.pdf", "application/pdf")

    assert error.value.code == "EXTRACTION_FAILED"
    assert error.value.permanent is True


@pytest.mark.parametrize(
    ("data", "file_name", "mime_type"),
    [
        (b"<html>fake PDF</html>", "fake.pdf", "application/pdf"),
        (b"not a ZIP archive", "fake.docx", DOCX_MIME),
        (make_zip_bytes({"unrelated.txt": b"renamed ZIP"}), "fake.docx", DOCX_MIME),
        (b"PK\x03\x04binary", "notes.txt", "text/plain"),
        (
            b"workbook",
            "sheet.xlsx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ),
    ],
)
def test_fake_or_unsupported_document_is_rejected(data, file_name, mime_type):
    with pytest.raises(AppError) as error:
        DocumentExtractor().extract_bytes(data, file_name, mime_type)

    assert error.value.code in {"UNSUPPORTED_SOURCE", "EXTRACTION_FAILED"}
    assert error.value.permanent is True


@pytest.mark.parametrize("file_name", ["old.doc", "macro.docm", "archive.zip"])
def test_unsupported_extensions_have_no_cheap_supported_hint(file_name):
    from app.extractors.document import document_format_hint

    assert document_format_hint(file_name, None) is None


def test_utf8_bom_is_supported_and_binary_controls_are_rejected():
    extractor = DocumentExtractor()
    content = extractor.extract_bytes(b"\xef\xbb\xbfhello world", "note.txt", "text/plain")
    assert content.text == "hello world"

    with pytest.raises(AppError, match="binary"):
        extractor.extract_bytes(b"prefix\x00binary", "note.txt", "text/plain")


def test_document_file_and_text_limits_are_permanent():
    with pytest.raises(AppError) as file_error:
        DocumentExtractor(max_file_bytes=4).extract_bytes(b"12345", "a.txt", "text/plain")
    assert file_error.value.code == "TOO_LARGE"
    assert file_error.value.permanent is True

    with pytest.raises(AppError) as text_error:
        DocumentExtractor(max_text_chars=4).extract_bytes(b"hello", "a.txt", "text/plain")
    assert text_error.value.code == "TOO_LARGE"


@pytest.mark.asyncio
async def test_public_url_pdf_uses_the_document_parser():
    payload = make_pdf_bytes(["This public URL PDF contains selectable research text."])
    extractor = _secure_web_extractor(
        lambda request: httpx.Response(
            200,
            content=payload,
            headers={
                "content-type": "application/pdf",
                "content-disposition": 'attachment; filename="report-final.pdf"',
            },
        ),
        document_extractor=DocumentExtractor(max_file_bytes=1_000_000),
    )

    content = await extractor.extract(_web_item())

    assert content.source_type is SourceType.DOCUMENT
    assert "selectable research text" in content.text
    assert content.url == PUBLIC_URL
    assert content.metadata["document_format"] == "pdf"
    assert content.metadata["file_name"] == "report-final.pdf"
    assert content.metadata["final_url"] == PUBLIC_URL


@pytest.mark.asyncio
async def test_octet_stream_with_pdf_signature_is_supported():
    payload = make_pdf_bytes(["An octet stream with a real PDF signature is a PDF."])
    extractor = _secure_web_extractor(
        lambda request: httpx.Response(
            200, content=payload, headers={"content-type": "application/octet-stream"}
        ),
        document_extractor=DocumentExtractor(max_file_bytes=1_000_000),
    )

    content = await extractor.extract(_web_item("https://example.com/download"))

    assert content.source_type is SourceType.DOCUMENT
    assert content.metadata["document_format"] == "pdf"


@pytest.mark.asyncio
async def test_pdf_url_returning_html_is_kept_on_the_web_path():
    from tests.fixtures_html import ARTICLE_HTML

    extractor = _secure_web_extractor(
        lambda request: httpx.Response(
            200,
            text=ARTICLE_HTML,
            headers={"content-type": "text/html; charset=utf-8"},
        )
    )

    content = await extractor.extract(_web_item())

    assert content.source_type is SourceType.WEB
    assert content.text


@pytest.mark.asyncio
async def test_pdf_response_with_fake_signature_is_rejected():
    extractor = _secure_web_extractor(
        lambda request: httpx.Response(
            200,
            text="<html>not a PDF</html>",
            headers={"content-type": "application/pdf"},
        )
    )

    with pytest.raises(AppError) as error:
        await extractor.extract(_web_item())

    assert error.value.code == "UNSUPPORTED_SOURCE"
    assert error.value.permanent is True


@pytest.mark.asyncio
async def test_url_pdf_redirect_to_private_address_is_rejected():
    requests = []

    def handler(request):
        requests.append(str(request.url))
        return httpx.Response(302, headers={"location": "http://169.254.169.254/latest"})

    extractor = _secure_web_extractor(handler)
    with pytest.raises(AppError) as error:
        await extractor.extract(_web_item())

    assert error.value.code == "SECURITY_REJECTED"
    assert requests == ["https://93.184.216.34/report.pdf"]


@pytest.mark.asyncio
async def test_url_pdf_with_private_dns_target_is_rejected_before_fetch():
    extractor = _secure_web_extractor(
        lambda request: pytest.fail("private PDF target must not reach HTTP transport")
    )

    with pytest.raises(AppError) as error:
        await extractor.extract(_web_item("https://private.example.com/report.pdf"))

    assert error.value.code == "SECURITY_REJECTED"


@pytest.mark.asyncio
async def test_pdf_without_content_length_still_obeys_streamed_byte_cap():
    payload = make_pdf_bytes(["A PDF payload larger than the configured byte cap."])
    extractor = _secure_web_extractor(
        lambda request: httpx.Response(
            200,
            headers={"content-type": "application/pdf"},
            stream=httpx.ByteStream(payload),
        ),
        max_download_bytes=20,
    )

    with pytest.raises(AppError) as error:
        await extractor.extract(_web_item())

    assert error.value.code == "TOO_LARGE"
    assert error.value.permanent is True


@pytest.mark.asyncio
async def test_url_pdf_checkpoint_reuses_safe_fetch_after_llm_retry(tmp_path, session_factory):
    payload = make_pdf_bytes(["The durable URL PDF phrase pdfcheckpointuranium is searchable."])
    request_count = 0

    def handler(request):
        nonlocal request_count
        request_count += 1
        return httpx.Response(200, content=payload, headers={"content-type": "application/pdf"})

    document_extractor = CountingDocumentExtractor(
        max_file_bytes=1_000_000,
        max_text_chars=10_000,
    )
    web_extractor = _secure_web_extractor(
        handler,
        document_extractor=document_extractor,
    )
    item = (
        await ingest_message(
            session_factory,
            telegram_user_id=42,
            chat_id=42,
            message_id=23,
            text="Read https://example.com/report.pdf",
        )
    ).items[0]
    provider = FakeLlmProvider(analyze_failures=1)
    worker = ProcessingWorker(
        session_factory,
        ProcessingPipeline(Analyzer(provider), PriorityEngine(), web_extractor=web_extractor),
        poll_seconds=0.01,
    )

    assert await worker.process_one() is True
    async with session_factory() as session:
        source = await session.scalar(select(ItemSource).where(ItemSource.item_id == item.id))
        checkpoint = await session.scalar(
            select(Content).where(
                Content.item_id == item.id,
                Content.kind == ContentKind.DOCUMENT_TEXT,
            )
        )
        assert source.source_type is SourceType.WEB
        assert source.metadata_json["document_format"] == "pdf"
        assert checkpoint.source_id == source.id

    assert await apply_item_action(session_factory, 42, item.id, "retry") is not None
    assert await worker.process_one() is True
    assert request_count == 1
    assert document_extractor.parse_calls == 1


def test_docx_uncompressed_and_entry_count_limits_are_checked_before_parsing(monkeypatch):
    valid_parts = {
        "[Content_Types].xml": b"<Types />",
        "word/document.xml": b"<document>" + b"x" * 2048 + b"</document>",
        "word/styles.xml": b"<styles />",
    }
    data = make_zip_bytes(valid_parts)
    monkeypatch.setattr(document_module, "_MAX_DOCX_UNCOMPRESSED_BYTES", 100)
    with pytest.raises(AppError, match="uncompressed size") as size_error:
        DocumentExtractor().extract_bytes(data, "paper.docx", DOCX_MIME)
    assert size_error.value.code == "TOO_LARGE"

    monkeypatch.setattr(document_module, "_MAX_DOCX_UNCOMPRESSED_BYTES", 1_000_000)
    monkeypatch.setattr(document_module, "_MAX_DOCX_ENTRIES", 2)
    with pytest.raises(AppError, match="too many entries") as entries_error:
        DocumentExtractor().extract_bytes(data, "paper.docx", DOCX_MIME)
    assert entries_error.value.code == "TOO_LARGE"


def test_docx_compression_ratio_is_bounded(monkeypatch):
    data = make_zip_bytes(
        {
            "[Content_Types].xml": b"x" * 2000,
            "word/document.xml": b"y" * 2000,
        }
    )
    monkeypatch.setattr(document_module, "_MAX_DOCX_EXPANSION_RATIO", 2)

    with pytest.raises(AppError, match="expansion ratio") as error:
        DocumentExtractor().extract_bytes(data, "paper.docx", DOCX_MIME)

    assert error.value.code == "TOO_LARGE"


@pytest.mark.asyncio
async def test_telegram_file_is_removed_after_document_extraction(tmp_path):
    downloader = PayloadDownloader(b"temporary document bytes")
    extractor = DocumentExtractor(downloader, tmp_path / "documents")
    source = ItemSource(
        item_id=1,
        source_index=0,
        source_type=SourceType.DOCUMENT,
        source_file_id="telegram-file",
        metadata_json={"file_name": "note.txt", "mime_type": "text/plain"},
    )

    content = await extractor.extract(source)

    assert content.text == "temporary document bytes"
    assert downloader.calls == 1
    assert list((tmp_path / "documents").iterdir()) == []


@pytest.mark.asyncio
async def test_document_handler_keeps_caption_and_url_in_one_item(
    settings, session_factory, monkeypatch
):
    sent = []

    async def fake_answer(self, text, **kwargs):
        sent.append(text)

    monkeypatch.setattr(Message, "answer", fake_answer)
    message = _document_message(
        caption="Сравни документ с https://example.com/article",
    )
    await on_document(message, settings, session_factory)

    assert sent == ["Принял документ paper.pdf. Разбираю…"]
    async with session_factory() as session:
        item = await session.scalar(select(Item))
        sources = list(
            await session.scalars(
                select(ItemSource)
                .where(ItemSource.item_id == item.id)
                .order_by(ItemSource.source_index)
            )
        )
        user_text = await session.scalar(
            select(Content.text).where(
                Content.item_id == item.id,
                Content.kind == ContentKind.USER_TEXT,
            )
        )
        assert await session.scalar(select(func.count()).select_from(Item)) == 1
        assert [source.source_type for source in sources] == [SourceType.DOCUMENT, SourceType.WEB]
        assert sources[0].source_file_id == "file-1"
        assert sources[0].metadata_json == {
            "file_name": "paper.pdf",
            "mime_type": "application/pdf",
            "document_format": "pdf",
        }
        assert sources[1].source_url == "https://example.com/article"
        assert user_text == "Сравни документ с https://example.com/article"
        assert item.user_note == "Сравни документ с"


@pytest.mark.parametrize(
    ("file_name", "mime_type", "expected_format"),
    [
        ("brief.pdf", "application/pdf", "pdf"),
        ("brief.txt", "text/plain", "txt"),
        ("brief.md", "text/markdown", "markdown"),
        ("brief.docx", DOCX_MIME, "docx"),
    ],
)
@pytest.mark.parametrize("forwarded", [False, True])
async def test_direct_and_forwarded_documents_use_same_source_path(
    settings, session_factory, monkeypatch, file_name, mime_type, expected_format, forwarded
):
    sent = []

    async def fake_answer(self, text, **kwargs):
        sent.append(text)

    monkeypatch.setattr(Message, "answer", fake_answer)
    message = _document_message(
        message_id=2,
        file_name=file_name,
        mime_type=mime_type,
        caption="Document context",
        forwarded=forwarded,
    )
    await on_document(message, settings, session_factory)

    assert sent == [f"Принял документ {file_name}. Разбираю…"]
    async with session_factory() as session:
        item = await session.scalar(select(Item))
        source = await session.scalar(select(ItemSource).where(ItemSource.item_id == item.id))
        assert item.source_type is SourceType.DOCUMENT
        assert item.user_note == ("" if forwarded else "Document context")
        if forwarded:
            assert item.source_metadata_json["forwarded"] is True
        assert source.source_type is SourceType.DOCUMENT
        assert source.metadata_json["document_format"] == expected_format
        assert source.source_file_id == "file-2"


@pytest.mark.parametrize("forwarded", [False, True])
async def test_document_router_handles_direct_and_forwarded_updates(
    settings, session_factory, monkeypatch, forwarded
):
    async def fake_answer(self, text, **kwargs):
        return None

    monkeypatch.setattr(Message, "answer", fake_answer)
    router = make_router(settings, session_factory)
    message = _document_message(message_id=4, forwarded=forwarded)
    matched = None
    for handler in router.message.handlers:
        passes, _ = await handler.check(message)
        if passes:
            matched = handler.callback.__name__
            await handler.callback(message)
            break

    assert matched == "document"
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(Item)) == 1


@pytest.mark.asyncio
async def test_unsupported_document_is_persisted_as_permanent_source_failure(
    settings, session_factory, monkeypatch
):
    sent = []

    async def fake_answer(self, text, **kwargs):
        sent.append(text)

    monkeypatch.setattr(Message, "answer", fake_answer)
    await on_document(
        _document_message(file_name="sheet.xlsx", mime_type="application/vnd.ms-excel"),
        settings,
        session_factory,
    )

    assert sent == ["Этот формат документа пока не поддерживается."]
    async with session_factory() as session:
        item = await session.scalar(select(Item))
        source = await session.scalar(select(ItemSource).where(ItemSource.item_id == item.id))
        assert item.processing_status is ProcessingStatus.FAILED
        assert item.error_code == "UNSUPPORTED_SOURCE"
        assert source.extraction_status == "FAILED"
        assert source.failure_is_permanent is True
        assert source.source_file_id == "file-1"


@pytest.mark.asyncio
async def test_unsupported_document_does_not_discard_useful_caption(
    settings, session_factory, monkeypatch
):
    async def fake_answer(self, text, **kwargs):
        return None

    monkeypatch.setattr(Message, "answer", fake_answer)
    await on_document(
        _document_message(
            file_name="sheet.xlsx", mime_type="application/vnd.ms-excel", caption="Сохранить вывод"
        ),
        settings,
        session_factory,
    )
    provider = FakeLlmProvider()
    worker = ProcessingWorker(
        session_factory,
        ProcessingPipeline(Analyzer(provider), PriorityEngine()),
        poll_seconds=0.01,
    )

    assert await worker.process_one() is True
    async with session_factory() as session:
        item = await session.scalar(select(Item))
        assert item.processing_status is ProcessingStatus.READY
        assert item.analysis_completeness == "PARTIAL"
        assert provider.calls[0][0].text == "Сохранить вывод"


@pytest.mark.asyncio
async def test_known_oversized_document_is_not_queued_for_download(
    settings, session_factory, monkeypatch
):
    sent = []

    async def fake_answer(self, text, **kwargs):
        sent.append(text)

    monkeypatch.setattr(Message, "answer", fake_answer)
    settings.max_document_bytes = 1_000_000
    await on_document(
        _document_message(file_size=1_500_000),
        settings,
        session_factory,
    )

    assert sent == ["Документ слишком большой (1.5 МБ > лимита 1 МБ). Файл не скачан."]
    async with session_factory() as session:
        item = await session.scalar(select(Item))
        source = await session.scalar(select(ItemSource).where(ItemSource.item_id == item.id))
        assert item.processing_status is ProcessingStatus.FAILED
        assert item.error_code == "TOO_LARGE"
        assert source.extraction_status == "FAILED"
        assert source.failure_is_permanent is True


@pytest.mark.asyncio
async def test_document_checkpoint_is_reused_after_analysis_retry(tmp_path, session_factory):
    payload = b"A durable document phrase docsearchquartz for search and analysis."
    downloader = PayloadDownloader(payload)
    extractor = CountingDocumentExtractor(downloader, tmp_path / "documents")
    item = (
        await ingest_media(
            session_factory,
            telegram_user_id=42,
            chat_id=42,
            message_id=20,
            file_id="document-file",
            duration_seconds=None,
            source_type=SourceType.DOCUMENT,
            source_details={"file_name": "note.txt", "mime_type": "text/plain"},
        )
    ).items[0]
    provider = FakeLlmProvider(analyze_failures=1)
    worker = ProcessingWorker(
        session_factory,
        ProcessingPipeline(Analyzer(provider), PriorityEngine(), document_extractor=extractor),
        poll_seconds=0.01,
    )

    assert await worker.process_one() is True
    async with session_factory() as session:
        failed = await session.get(Item, item.id)
        document_source = await session.scalar(
            select(ItemSource).where(ItemSource.item_id == item.id)
        )
        checkpoint = await session.scalar(
            select(Content).where(
                Content.item_id == item.id,
                Content.kind == ContentKind.DOCUMENT_TEXT,
            )
        )
        assert failed.processing_status is ProcessingStatus.FAILED
        assert document_source.extraction_status == "READY"
        assert checkpoint.source_id == document_source.id

    assert await apply_item_action(session_factory, 42, item.id, "retry") is not None
    assert await worker.process_one() is True
    assert downloader.calls == 1
    assert extractor.parse_calls == 1

    async with session_factory() as session:
        ready = await session.get(Item, item.id)
        assert ready.processing_status is ProcessingStatus.READY
        matches = await search_items(session, ready.user_id, "docsearchquartz")
        assert [match.id for match in matches] == [item.id]


class FlakyWebExtractor:
    """Fail once at the WEB source boundary, then provide deterministic content."""

    def __init__(self, failures: int = 1):
        self.failures = failures
        self.calls = 0

    async def extract(self, source):
        self.calls += 1
        if self.failures:
            self.failures -= 1
            raise AppError("DOWNLOAD_FAILED", "temporary web failure")
        return NormalizedContent(
            source_type=SourceType.WEB, text="A sibling web article.", url=source.source_url
        )


@pytest.mark.asyncio
async def test_partial_retry_reuses_ready_document_and_reextracts_only_web(
    tmp_path, session_factory
):
    downloader = PayloadDownloader(b"Document checkpoint survives web failure.")
    document_extractor = CountingDocumentExtractor(downloader, tmp_path / "documents")
    web_extractor = FlakyWebExtractor()
    item = (
        await ingest_media(
            session_factory,
            telegram_user_id=42,
            chat_id=42,
            message_id=21,
            file_id="document-file",
            duration_seconds=None,
            source_type=SourceType.DOCUMENT,
            source_text="Compare these sources https://example.com/article",
            source_details={"file_name": "note.txt", "mime_type": "text/plain"},
        )
    ).items[0]
    worker = ProcessingWorker(
        session_factory,
        ProcessingPipeline(
            Analyzer(FakeLlmProvider()),
            PriorityEngine(),
            web_extractor=web_extractor,
            document_extractor=document_extractor,
        ),
        poll_seconds=0.01,
    )

    assert await worker.process_one() is True
    async with session_factory() as session:
        partial = await session.get(Item, item.id)
        assert partial.analysis_completeness == "PARTIAL"
    assert downloader.calls == document_extractor.parse_calls == 1

    assert await apply_item_action(session_factory, 42, item.id, "retry") is not None
    assert await worker.process_one() is True
    assert downloader.calls == document_extractor.parse_calls == 1
    assert web_extractor.calls == 2


class SuccessfulWebExtractor:
    """Return stable URL text so document-only retry behavior is observable."""

    def __init__(self):
        self.calls = 0

    async def extract(self, source):
        self.calls += 1
        return NormalizedContent(
            source_type=SourceType.WEB,
            text="A sibling web article.",
            url=source.source_url,
        )


@pytest.mark.asyncio
async def test_document_retry_reuses_ready_web_checkpoint(tmp_path, session_factory):
    downloader = PayloadDownloader(b"Retry only the transient document source.", failures=1)
    document_extractor = CountingDocumentExtractor(downloader, tmp_path / "documents")
    web_extractor = SuccessfulWebExtractor()
    item = (
        await ingest_media(
            session_factory,
            telegram_user_id=42,
            chat_id=42,
            message_id=22,
            file_id="document-file",
            duration_seconds=None,
            source_type=SourceType.DOCUMENT,
            source_text="Compare these sources https://example.com/article",
            source_details={"file_name": "note.txt", "mime_type": "text/plain"},
        )
    ).items[0]
    worker = ProcessingWorker(
        session_factory,
        ProcessingPipeline(
            Analyzer(FakeLlmProvider()),
            PriorityEngine(),
            web_extractor=web_extractor,
            document_extractor=document_extractor,
        ),
        poll_seconds=0.01,
    )

    assert await worker.process_one() is True
    async with session_factory() as session:
        partial = await session.get(Item, item.id)
        assert partial.analysis_completeness == "PARTIAL"
    assert web_extractor.calls == 1
    assert downloader.calls == 1

    assert await apply_item_action(session_factory, 42, item.id, "retry") is not None
    assert await worker.process_one() is True
    assert web_extractor.calls == 1
    assert downloader.calls == 2
    assert document_extractor.parse_calls == 1
