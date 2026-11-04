"""Use yt-dlp's Instagram parser with a bounded, pinned metadata transport."""

import asyncio
from http.cookiejar import CookieJar
from io import BytesIO
from urllib.parse import urljoin, urlsplit

import httpx
from yt_dlp.networking import Request, Response
from yt_dlp.networking.exceptions import HTTPError, TransportError

from app.errors import AppError
from app.extractors.web import PinningTransport

_METADATA_HOSTS = {"instagram.com", "www.instagram.com", "i.instagram.com"}
_METADATA_MAX_BYTES = 8_000_000


def _validate_metadata_url(url: str) -> None:
    try:
        parts = urlsplit(url)
        if (
            parts.scheme == "https"
            and parts.hostname in _METADATA_HOSTS
            and parts.username is None
            and parts.port is None
        ):
            return
    except ValueError:
        pass
    raise AppError("SECURITY_REJECTED", "Instagram metadata URL or redirect rejected", True)


async def _fetch_metadata(
    client: httpx.AsyncClient,
    request: Request,
    *,
    byte_limit: int = _METADATA_MAX_BYTES,
) -> Response:
    url, method, content = request.url, request.method, request.data
    headers = httpx.Headers(request.headers)
    # Scope cookies to each hostname through the jar, including after redirects.
    headers.pop("Cookie", None)
    headers["Accept-Encoding"] = "identity"

    for _ in range(6):
        _validate_metadata_url(url)
        try:
            async with client.stream(method, url, content=content, headers=headers) as response:
                if response.is_redirect:
                    if not (location := response.headers.get("location")):
                        raise TransportError("Instagram metadata redirect has no target")
                    url = urljoin(url, location)
                    if response.status_code in {301, 302, 303} and method == "POST":
                        method, content = "GET", None
                        for name in ("Content-Type", "Content-Length"):
                            headers.pop(name, None)
                    continue
                if response.status_code == 429:
                    raise AppError("RATE_LIMITED", "Instagram temporarily rate-limited metadata")
                if response.headers.get("content-encoding", "identity") != "identity":
                    raise TransportError("Instagram metadata returned an unexpected encoding")
                body = BytesIO()
                length = response.headers.get("content-length", "")
                if length.isdecimal() and int(length) > byte_limit:
                    raise AppError("TOO_LARGE", "Instagram metadata exceeds the byte limit", True)
                async for chunk in response.aiter_raw(chunk_size=65_536):
                    if body.tell() + len(chunk) > byte_limit:
                        raise AppError(
                            "TOO_LARGE", "Instagram metadata exceeds the byte limit", True
                        )
                    body.write(chunk)
                body.seek(0)
                result = Response(
                    body,
                    url,
                    dict(response.headers),
                    status=response.status_code,
                    reason=response.reason_phrase,
                )
                if not response.is_success:
                    raise HTTPError(result)
                return result
        except httpx.TimeoutException:
            raise TransportError("Instagram metadata request timed out") from None
        except httpx.HTTPError:
            raise TransportError("Instagram metadata request failed") from None
    raise AppError("DOWNLOAD_FAILED", "Instagram metadata redirect limit exceeded", True)


def open_post_metadata(request: Request, cookiejar: CookieJar, headers: dict, timeout: float):
    """Adapt pinned HTTPS and yt-dlp's existing cookie jar to its response contract."""

    async def fetch() -> Response:
        async with httpx.AsyncClient(
            transport=PinningTransport(),
            follow_redirects=False,
            trust_env=False,
            cookies=cookiejar,
            headers=headers,
            timeout=timeout,
        ) as client:
            return await _fetch_metadata(client, request)

    return asyncio.run(fetch())
