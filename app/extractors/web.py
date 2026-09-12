import asyncio
from collections.abc import Awaitable, Callable
from urllib.parse import urljoin

import httpx
import trafilatura

from app.domain.enums import SourceType
from app.domain.models import NormalizedContent
from app.errors import AppError
from app.services.url_security import validate_url_security
from app.storage.models import Item

# trafilatura синхронный и CPU-зависимый — выполняется в thread, не блокируя loop.
_EXTRACT = trafilatura.extract
_EXTRACT_METADATA = trafilatura.extract_metadata


class WebPageExtractor:
    """Загрузка и очистка web-страницы: security → httpx → trafilatura →
    (недостаточно текста?) → Playwright fallback → trafilatura.

    SSRF: каждый redirect-хоп проходит validate_url_security; размер ответа и
    число redirect'ов ограничены; extraction без содержательного текста — FAIL
    (ТЗ §18–20), бесконечный обход защит не выполняется.
    """

    def __init__(
        self,
        min_text_length: int = 300,
        timeout_seconds: float = 30.0,
        max_download_bytes: int = 5_000_000,
        max_redirects: int = 5,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
        renderer: Callable[[str], Awaitable[str]] | None = None,
    ):
        self.min_text_length = min_text_length
        self.timeout_seconds = timeout_seconds
        self.max_download_bytes = max_download_bytes
        self.max_redirects = max_redirects
        self._client_factory = client_factory or self._default_client
        # renderer(url) -> html; подменяется в тестах (Playwright не требуется)
        self._renderer = renderer

    @staticmethod
    def _default_client() -> httpx.AsyncClient:
        return httpx.AsyncClient(
            follow_redirects=False,
            timeout=30.0,
            headers={"User-Agent": "PersonalAIInbox/0.1 (+private bot)"},
        )

    async def extract(self, item: Item) -> "NormalizedContent":
        html = await self._fetch_with_redirects(item.source_url)
        text, meta = await self._text_from_html(html)
        if len(text) < self.min_text_length:
            text, meta = await self._fallback(item.source_url)
        if len(text) < self.min_text_length:
            raise AppError(
                "EXTRACTION_FAILED",
                f"page text too short ({len(text)} < {self.min_text_length})",
            )
        return NormalizedContent(
            source_type=SourceType.WEB,
            title=meta.get("title"),
            text=text,
            url=item.source_url,
            user_note=item.user_note or None,
            author=meta.get("author"),
            language=meta.get("language"),
        )

    async def _fallback(self, url: str):
        """Playwright fallback; недоступен (нет пакета/браузера) — честный FAIL."""
        if self._renderer is not None:
            html = await self._renderer(url)
        else:
            try:
                html = await self._render_with_playwright(url)
            except AppError:
                raise
            except Exception as exc:
                raise AppError("EXTRACTION_FAILED", f"render failed: {exc}") from exc
        return await self._text_from_html(html)

    async def _render_with_playwright(self, url: str) -> str:
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise AppError("EXTRACTION_FAILED", "playwright fallback not installed") from exc
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            try:
                page = await browser.new_page()
                await page.goto(
                    url, timeout=self.timeout_seconds * 1000, wait_until="domcontentloaded"
                )
                return await page.content()
            finally:
                await browser.close()

    async def _fetch_with_redirects(self, url: str) -> str:
        current = url
        async with self._client_factory() as client:
            for _ in range(self.max_redirects):
                await validate_url_security(current)
                try:
                    response = await client.get(current)
                except httpx.TimeoutException as exc:
                    raise AppError("TIMEOUT", f"request timed out: {current}") from exc
                except httpx.HTTPError as exc:
                    raise AppError("DOWNLOAD_FAILED", f"download failed: {exc}") from exc
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        raise AppError("DOWNLOAD_FAILED", "redirect without location")
                    current = urljoin(current, location)
                    continue
                if response.status_code >= 400:
                    raise AppError("DOWNLOAD_FAILED", f"HTTP {response.status_code} for {current}")
                content_length = response.headers.get("content-length")
                if content_length and content_length.isdigit():
                    if int(content_length) > self.max_download_bytes:
                        raise AppError(
                            "TOO_LARGE", f"response exceeds {self.max_download_bytes} bytes"
                        )
                body = response.content
                if len(body) > self.max_download_bytes:
                    raise AppError("TOO_LARGE", f"response exceeds {self.max_download_bytes} bytes")
                return response.text
        raise AppError("DOWNLOAD_FAILED", "redirect limit exceeded")

    @staticmethod
    async def _text_from_html(html: str):
        def _extract():
            text = _EXTRACT(html) or ""
            metadata = {}
            try:
                meta = _EXTRACT_METADATA(html)
                if meta is not None:
                    as_dict = meta.as_dict()
                    metadata = {
                        "title": as_dict.get("title"),
                        "author": as_dict.get("author"),
                        "language": as_dict.get("language"),
                    }
            except Exception:
                metadata = {}
            return text.strip(), metadata

        return await asyncio.to_thread(_extract)
