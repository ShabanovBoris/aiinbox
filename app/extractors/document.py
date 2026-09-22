"""Bounded parsing for Telegram and URL document sources.

The extractor owns format validation and conversion to NormalizedContent; the
processing pipeline remains the only owner of durable checkpoints and analysis.
"""

import asyncio
import logging
import shutil
import struct
import zipfile
from io import BytesIO
from pathlib import Path
from uuid import uuid4

from docx import Document as load_docx
from docx.table import Table
from docx.text.paragraph import Paragraph
from pypdf import PdfReader, apply_configuration

from app.bot.files import FileDownloader
from app.domain.enums import SourceType
from app.domain.models import NormalizedContent
from app.errors import AppError
from app.storage.models import ItemSource

log = logging.getLogger(__name__)

_EXTENSION_FORMATS = {
    ".pdf": "pdf",
    ".txt": "txt",
    ".md": "markdown",
    ".markdown": "markdown",
    ".docx": "docx",
}
_MIME_FORMATS = {
    "application/pdf": "pdf",
    "text/plain": "txt",
    "text/markdown": "markdown",
    "text/x-markdown": "markdown",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
}
_UNSUPPORTED_EXTENSIONS = {".doc", ".docm", ".zip", ".xls", ".xlsx", ".ppt", ".pptx"}
_MAX_PDF_PAGES = 1000
_MAX_PDF_STREAM_BYTES = 10_000_000
_MAX_DOCX_ENTRIES = 1000
_MAX_DOCX_UNCOMPRESSED_BYTES = 100_000_000
_MAX_DOCX_EXPANSION_RATIO = 100
_DOCX_REQUIRED_ENTRIES = {"[Content_Types].xml", "word/document.xml"}


def document_format_hint(file_name: str | None, mime_type: str | None) -> str | None:
    """Return a supported cheap Telegram hint; bytes are still validated by the parser."""
    extension = Path((file_name or "").replace("\\", "/").rsplit("/", 1)[-1]).suffix.lower()
    if extension in _UNSUPPORTED_EXTENSIONS:
        return None
    extension_format = _EXTENSION_FORMATS.get(extension)
    media_type = (mime_type or "").split(";", 1)[0].strip().lower()
    mime_format = _MIME_FORMATS.get(media_type)
    compatible_plain_text = {extension_format, mime_format} <= {"txt", "markdown", None}
    if (
        extension_format
        and mime_format
        and extension_format != mime_format
        and not compatible_plain_text
    ):
        return None
    return extension_format or mime_format


def safe_document_file_name(file_name: str | None) -> str | None:
    """Keep Telegram filenames as display metadata only, never as filesystem paths."""
    if not file_name:
        return None
    name = file_name.replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(character for character in name if character.isprintable()).strip()
    return name[:255] or None


class DocumentExtractor:
    """Turn a bounded Telegram file or already-fetched bytes into normalized text.

    Telegram acquisition uses the existing capped downloader; URL PDFs call the
    same byte parser after WebPageExtractor's SSRF-safe fetch has completed.
    """

    def __init__(
        self,
        downloader: FileDownloader | None = None,
        temp_dir: Path | None = None,
        max_file_bytes: int = 20_000_000,
        max_text_chars: int = 500_000,
    ):
        self.downloader = downloader
        self.temp_dir = Path(temp_dir or "./temp/documents")
        self.max_file_bytes = max_file_bytes
        self.max_text_chars = max_text_chars

    async def extract(self, source: ItemSource) -> NormalizedContent:
        """Download and parse one Telegram document, cleaning its owned temp directory."""
        if self.downloader is None or not source.source_file_id:
            raise AppError("UNSUPPORTED_SOURCE", "Telegram document downloader is unavailable")
        file_name = safe_document_file_name((source.metadata_json or {}).get("file_name"))
        mime_type = (source.metadata_json or {}).get("mime_type")
        work_dir = self.temp_dir / f"document-{uuid4().hex}"
        try:
            work_dir.mkdir(parents=True, exist_ok=True)
            path = await self.downloader.download(source.source_file_id, work_dir)
            if path.stat().st_size > self.max_file_bytes:
                raise AppError(
                    "TOO_LARGE",
                    f"document exceeds {self.max_file_bytes} bytes",
                    permanent=True,
                )
            data = await asyncio.to_thread(path.read_bytes)
            return await asyncio.to_thread(self.extract_bytes, data, file_name, mime_type)
        except asyncio.CancelledError:
            raise
        except OSError as exc:
            raise AppError("DOWNLOAD_FAILED", "temporary document storage failed") from exc
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

    def extract_bytes(
        self,
        data: bytes,
        file_name: str | None,
        mime_type: str | None,
    ) -> NormalizedContent:
        """Validate actual file structure and produce the shared analysis payload."""
        if len(data) > self.max_file_bytes:
            raise AppError(
                "TOO_LARGE", f"document exceeds {self.max_file_bytes} bytes", permanent=True
            )
        hint = document_format_hint(file_name, mime_type)
        pdf_signature = b"%PDF-" in data[:1024]
        if pdf_signature:
            if hint not in (None, "pdf"):
                raise AppError("UNSUPPORTED_SOURCE", "document hints conflict with PDF bytes", True)
            text, pages = self._extract_pdf(data)
            document_format = "pdf"
        elif hint == "pdf":
            raise AppError("UNSUPPORTED_SOURCE", "file is not a PDF document", permanent=True)
        elif hint == "docx":
            text = self._extract_docx(data)
            pages = None
            document_format = "docx"
        elif hint in ("txt", "markdown"):
            text = self._extract_plain_text(data)
            pages = None
            document_format = hint
        else:
            raise AppError("UNSUPPORTED_SOURCE", "document format is unsupported", permanent=True)

        text = text.strip()
        if not text:
            message = (
                "PDF has no selectable text; OCR for scanned documents is not supported"
                if document_format == "pdf"
                else "document contains no extractable text"
            )
            raise AppError("EXTRACTION_FAILED", message, permanent=True)
        metadata = {
            "document_format": document_format,
            "file_name": safe_document_file_name(file_name),
            "mime_type": (mime_type or "").split(";", 1)[0].strip().lower() or None,
        }
        if pages is not None:
            metadata["pages"] = pages
        return NormalizedContent(
            source_type=SourceType.DOCUMENT,
            title=metadata["file_name"],
            text=text,
            metadata={key: value for key, value in metadata.items() if value is not None},
        )

    def _extract_pdf(self, data: bytes) -> tuple[str, int]:
        """Extract bounded page-order text while rejecting image-only PDFs honestly."""
        parts: list[str] = []
        char_count = 0

        def append_page(page_number: int, page_text: str) -> None:
            """Account for separators and enforce the text bound page by page."""
            nonlocal char_count
            page_text = page_text.strip()
            if not page_text:
                return
            part = f"--- page {page_number} ---\n{page_text}"
            added = len(part) + (2 if parts else 0)
            if char_count + added > self.max_text_chars:
                raise AppError(
                    "TOO_LARGE",
                    f"document text exceeds {self.max_text_chars} characters",
                    permanent=True,
                )
            parts.append(part)
            char_count += added

        try:
            with apply_configuration(
                page_tree_maximum_entries=10_000,
                page_tree_maximum_depth=100,
                array_based_stream_maximum_output_length=_MAX_PDF_STREAM_BYTES,
                jbig2_maximum_output_length=_MAX_PDF_STREAM_BYTES,
                lzw_maximum_output_length=_MAX_PDF_STREAM_BYTES,
                run_length_maximum_output_length=_MAX_PDF_STREAM_BYTES,
                zlib_maximum_output_length=_MAX_PDF_STREAM_BYTES,
                image_maximum_buffer_size=_MAX_PDF_STREAM_BYTES,
            ):
                reader = PdfReader(BytesIO(data), strict=False, root_object_recovery_limit=10_000)
                if reader.is_encrypted:
                    raise AppError(
                        "UNSUPPORTED_SOURCE", "password-protected PDFs are not supported", True
                    )
                page_count = len(reader.pages)
                if page_count > _MAX_PDF_PAGES:
                    raise AppError("TOO_LARGE", "PDF page limit exceeded", permanent=True)
                for index, page in enumerate(reader.pages, start=1):
                    append_page(index, page.extract_text() or "")
                if sum(character.isalnum() for character in " ".join(parts)) < 20:
                    raise AppError(
                        "EXTRACTION_FAILED",
                        "PDF has no selectable text; OCR for scanned documents is not supported",
                        permanent=True,
                    )
                return "\n\n".join(parts), page_count
        except AppError:
            raise
        except Exception as exc:
            log.warning("PDF parsing failed error_type=%s", type(exc).__name__)
            raise AppError("EXTRACTION_FAILED", "could not extract text from PDF", True) from exc

    def _extract_docx(self, data: bytes) -> str:
        """Read paragraph and table text in document order without materializing files."""
        self._validate_docx_archive(data)
        parts: list[str] = []
        char_count = 0

        def append_block(text: str) -> None:
            """Apply the same hard character limit before retaining each Word block."""
            nonlocal char_count
            text = text.strip()
            if not text:
                return
            added = len(text) + (1 if parts else 0)
            if char_count + added > self.max_text_chars:
                raise AppError(
                    "TOO_LARGE",
                    f"document text exceeds {self.max_text_chars} characters",
                    permanent=True,
                )
            parts.append(text)
            char_count += added

        try:
            document = load_docx(BytesIO(data))
            for block in document.iter_inner_content():
                if isinstance(block, Paragraph):
                    append_block(block.text)
                elif isinstance(block, Table):
                    for row in block.rows:
                        append_block(" | ".join(cell.text for cell in row.cells))
        except AppError:
            raise
        except Exception as exc:
            log.warning("DOCX parsing failed error_type=%s", type(exc).__name__)
            raise AppError("UNSUPPORTED_SOURCE", "invalid DOCX document", True) from exc
        return "\n".join(parts)

    def _validate_docx_archive(self, data: bytes) -> None:
        """Reject ZIP expansion and structure risks before python-docx opens XML parts."""
        entry_count = _zip_entry_count(data)
        if entry_count > _MAX_DOCX_ENTRIES:
            raise AppError("TOO_LARGE", "DOCX archive contains too many entries", True)
        try:
            with zipfile.ZipFile(BytesIO(data)) as archive:
                entries = archive.infolist()
                names = [entry.filename for entry in entries]
                if len(entries) != entry_count or len(set(names)) != entry_count:
                    raise AppError("UNSUPPORTED_SOURCE", "invalid DOCX archive entries", True)
                if not _DOCX_REQUIRED_ENTRIES.issubset(names):
                    raise AppError("UNSUPPORTED_SOURCE", "file is not a DOCX document", True)
                total_uncompressed = 0
                for entry in entries:
                    if (
                        entry.filename.startswith("/")
                        or "\\" in entry.filename
                        or ".." in Path(entry.filename).parts
                    ):
                        raise AppError("UNSUPPORTED_SOURCE", "invalid DOCX archive path", True)
                    if entry.flag_bits & 1:
                        raise AppError("UNSUPPORTED_SOURCE", "encrypted DOCX is unsupported", True)
                    total_uncompressed += entry.file_size
                    if total_uncompressed > _MAX_DOCX_UNCOMPRESSED_BYTES:
                        raise AppError("TOO_LARGE", "DOCX uncompressed size limit exceeded", True)
                    ratio = entry.file_size / max(entry.compress_size, 1)
                    if ratio > _MAX_DOCX_EXPANSION_RATIO:
                        raise AppError("TOO_LARGE", "DOCX expansion ratio is too high", True)
        except AppError:
            raise
        except (OSError, zipfile.BadZipFile, struct.error) as exc:
            raise AppError("UNSUPPORTED_SOURCE", "invalid DOCX archive", True) from exc

    def _extract_plain_text(self, data: bytes) -> str:
        """Treat TXT/Markdown as UTF-8 data and reject binary-like payloads."""
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise AppError("UNSUPPORTED_SOURCE", "text document must use UTF-8", True) from exc
        if "\x00" in text or _binary_control_ratio(text) > 0.01:
            raise AppError("UNSUPPORTED_SOURCE", "file contains binary data", True)
        if len(text) > self.max_text_chars:
            raise AppError(
                "TOO_LARGE",
                f"document text exceeds {self.max_text_chars} characters",
                permanent=True,
            )
        return text


def _zip_entry_count(data: bytes) -> int:
    """Bound ZIP directory allocation before ZipFile materializes every entry."""
    lower_bound = max(0, len(data) - 65_557)
    position = data.rfind(b"PK\x05\x06", lower_bound)
    while position >= lower_bound:
        if position + 22 <= len(data):
            fields = struct.unpack_from("<HHHHIIH", data, position + 4)
            disk, directory_disk, disk_entries, total_entries, _, _, comment_length = fields
            if position + 22 + comment_length == len(data):
                if disk or directory_disk or disk_entries != total_entries:
                    raise AppError("UNSUPPORTED_SOURCE", "multi-disk ZIP is unsupported", True)
                return total_entries
        position = data.rfind(b"PK\x05\x06", lower_bound, position)
    raise AppError("UNSUPPORTED_SOURCE", "invalid DOCX archive directory", permanent=True)


def _binary_control_ratio(text: str) -> float:
    """Reject decoded strings whose control-byte share is inconsistent with text."""
    if not text:
        return 0.0
    controls = sum(ord(character) < 32 and character not in "\n\r\t\f\b" for character in text)
    return controls / len(text)
