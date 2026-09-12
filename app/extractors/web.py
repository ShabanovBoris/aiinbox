import asyncio
import ipaddress
from collections.abc import Awaitable, Callable
from urllib.parse import urljoin

import httpx
import trafilatura

from app.domain.enums import SourceType
from app.domain.models import NormalizedContent
from app.errors import AppError
from app.services.url_security import resolve_pinned_ip, resolve_validated_ips
from app.storage.models import Item

# trafilatura синхронный и CPU-зависимый — выполняется в thread, не блокируя loop.
_EXTRACT = trafilatura.extract
_EXTRACT_METADATA = trafilatura.extract_metadata

# Ошибки, которые имеет смысл ретраить (transient); permanent коды не ретраятся
# (PRODUCT_SPEC §58: 2-3 attempts, exponential backoff, без retry security/4xx).
_RETRYABLE_CODES = {"TIMEOUT", "DOWNLOAD_FAILED"}


def _resolve_ip_literal_safe(host: str):
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


class PinningTransport(httpx.AsyncBaseTransport):
    """Транспорт с DNS pinning: резолвит и валидирует host сам, затем соединяется
    с проверенным IP (SNI/Host сохраняют оригинал). Исключает DNS rebinding
    TOCTOU — между валидацией и connect нет второго резолва."""

    def __init__(
        self,
        inner: httpx.AsyncBaseTransport | None = None,
        resolver: Callable[[str], Awaitable[list[str]]] | None = None,
    ):
        # keep-alive запрещён на уровне запросов (Connection: close): pinning
        # подменяет host на IP, а httpcore переиспользует соединения по origin —
        # redirect A->B на один CDN IP мог бы переиспользовать TLS-сессию с SNI A.
        self._inner = inner if inner is not None else httpx.AsyncHTTPTransport()
        self._resolver = resolver

    async def aclose(self) -> None:
        # Wrapper обязан делегировать cleanup внутреннему транспорту (httpx docs):
        # иначе connection pool/sockets остаются незакрытыми.
        await self._inner.aclose()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        host = url.host
        if _resolve_ip_literal_safe(host) is None:
            # hostname: резолв+валидация, затем соединение с проверенным IP;
            # Host/SNI остаются оригинальными — сервер видит корректный vhost/TLS.
            pinned = await resolve_pinned_ip(str(url), self._resolver)
            request.url = url.copy_with(host=pinned)
            # httpx.URL.netloc — bytes; Host собираем из str-компонентов.
            request.headers["host"] = host if url.port is None else f"{host}:{url.port}"
            if url.scheme == "https":
                request.extensions["sni_hostname"] = host
        else:
            # IP-литерал: DNS не нужен, только валидация адреса и схемы.
            await resolve_validated_ips(str(url), self._resolver)
        # Connection: close — keep-alive пулла по IP-origin позволил бы redirect
        # A->B на один CDN IP переиспользовать TLS-сессию с SNI A.
        request.headers["connection"] = "close"
        return await self._inner.handle_async_request(request)


class WebPageExtractor:
    """Загрузка и очистка web-страницы: pinning-транспорт → streamed download
    с byte-cap → trafilatura → (недостаточно текста?) → Playwright fallback.

    SSRF: соединение выполняется на проверенный IP (DNS pinning); размер ответа
    ограничен инкрементально при чтении; extraction без содержательного текста —
    FAIL (ТЗ §18–20). Transient-ошибки ретраятся ограниченно (PRODUCT_SPEC §58),
    permanent (security/4xx/TOO_LARGE) — никогда.
    """

    def __init__(
        self,
        min_text_length: int = 300,
        timeout_seconds: float = 30.0,
        max_download_bytes: int = 5_000_000,
        max_redirects: int = 5,
        max_attempts: int = 3,
        backoff_seconds: float = 0.5,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
        renderer: Callable[[str], Awaitable[str]] | None = None,
        resolver: Callable[[str], Awaitable[list[str]]] | None = None,
    ):
        self.min_text_length = min_text_length
        self.timeout_seconds = timeout_seconds
        self.max_download_bytes = max_download_bytes
        self.max_redirects = max_redirects
        self.max_attempts = max_attempts
        self.backoff_seconds = backoff_seconds
        self._client_factory = client_factory or self._default_client
        # renderer(url) -> html; подменяется в тестах (Playwright не требуется)
        self._renderer = renderer
        self._resolver = resolver

    def _default_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            follow_redirects=False,
            timeout=self.timeout_seconds,
            headers={"User-Agent": "PersonalAIInbox/0.1 (+private bot)"},
            transport=PinningTransport(resolver=self._resolver),
        )

    async def extract(self, item: Item) -> NormalizedContent:
        html = await self._fetch_with_retries(item.source_url)
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

    async def _fetch_with_retries(self, url: str) -> str:
        last_error: AppError | None = None
        for attempt in range(self.max_attempts):
            try:
                return await self._fetch_with_redirects(url)
            except AppError as exc:
                if exc.code not in _RETRYABLE_CODES or exc.permanent:
                    raise
                last_error = exc
                await asyncio.sleep(self.backoff_seconds * (2**attempt))
        raise last_error  # pragma: no cover — цикл всегда завершается raise/return

    async def _fetch_with_redirects(self, url: str) -> str:
        current = url
        async with self._client_factory() as client:
            for _ in range(self.max_redirects):
                try:
                    async with client.stream("GET", current) as response:
                        if response.is_redirect:
                            location = response.headers.get("location")
                            if not location:
                                raise AppError("DOWNLOAD_FAILED", "redirect without location")
                            current = urljoin(current, location)
                            continue
                        if response.status_code >= 400:
                            # 4xx — permanent (повтор бессмыслен), 5xx — transient.
                            raise AppError(
                                "DOWNLOAD_FAILED",
                                f"HTTP {response.status_code} for {current}",
                                permanent=response.status_code < 500,
                            )
                        return await self._read_capped(response)
                except httpx.TimeoutException as exc:
                    raise AppError("TIMEOUT", f"request timed out: {current}") from exc
                except httpx.HTTPError as exc:
                    raise AppError("DOWNLOAD_FAILED", f"download failed: {exc}") from exc
        raise AppError("DOWNLOAD_FAILED", "redirect limit exceeded", permanent=True)

    async def _read_capped(self, response: httpx.Response) -> str:
        """Инкрементальное чтение с жёстким byte-cap: ответ без Content-Length
        не может заставить процесс буферизовать произвольный объём."""
        content_length = response.headers.get("content-length")
        if content_length and content_length.isdigit():
            if int(content_length) > self.max_download_bytes:
                raise AppError("TOO_LARGE", f"response exceeds {self.max_download_bytes} bytes")
        buffer = bytearray()
        async for chunk in response.aiter_bytes():
            if len(buffer) + len(chunk) > self.max_download_bytes:
                raise AppError("TOO_LARGE", f"response exceeds {self.max_download_bytes} bytes")
            buffer.extend(chunk)
        charset = response.charset_encoding or "utf-8"
        return buffer.decode(charset, errors="replace")

    async def _fallback(self, url: str):
        """Playwright fallback жёстко отключён (решение Orchestrator по Phase 3):
        route-deny не закрывает DNS TOCTOU/WebSockets — unrestricted browser не
        запускается. Вернётся отдельным изменением с настоящим network boundary;
        renderer — тестовый seam."""
        if self._renderer is None:
            raise AppError(
                "EXTRACTION_FAILED",
                "playwright fallback is disabled: no SSRF-safe browser boundary yet",
            )
        html = await self._renderer(url)
        return await self._text_from_html(html)

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
