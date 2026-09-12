"""SSRF-защита web-загрузки (обязательное требование, ТЗ §20).

Разрешены только http/https на публичные адреса; DNS резолвится до запроса,
каждый redirect-хоп проверяется заново. Для исключения DNS rebinding/TOCTOU
фактическое соединение выполняется на закэшированный проверенный IP
(PinningTransport) — резолв и connect используют один и тот же адрес.
"""

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit

from app.errors import AppError

# Сети, которые ipaddress.is_private в части версий Python не покрывает.
_EXTRA_BLOCKED = [
    ipaddress.ip_network("100.64.0.0/10"),  # CGNAT
    ipaddress.ip_network("192.0.0.0/24"),
    ipaddress.ip_network("198.18.0.0/15"),
]


def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped  # ::ffff:127.0.0.1 — смотрим на реальный IPv4
    if any(ip in network for network in _EXTRA_BLOCKED):
        return True
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
        or not ip.is_global
    )


def _resolve_ip_literal(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


async def _resolve_dns(host: str) -> list[str]:
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise AppError("DOWNLOAD_FAILED", f"cannot resolve host {host!r}") from exc
    return [info[4][0] for info in infos]


async def resolve_validated_ips(
    url: str, resolver: Callable[[str], Awaitable[list[str]]] | None = None
) -> list[str]:
    """Все IP хоста после проверки; любой запрещённый адрес → SECURITY_REJECTED."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise AppError("SECURITY_REJECTED", f"scheme {parts.scheme!r} is not allowed")
    host = parts.hostname
    if not host:
        raise AppError("SECURITY_REJECTED", "URL has no host")
    if host == "localhost" or host.endswith(".localhost"):
        # localhost запрещён по имени: его резолюция не должна зависеть от DNS.
        raise AppError("SECURITY_REJECTED", "localhost is not allowed")

    literal = _resolve_ip_literal(host)
    if literal is not None:
        if _is_blocked_ip(literal):
            raise AppError("SECURITY_REJECTED", f"host {host!r} is a forbidden address")
        return [str(literal)]

    try:
        ips = await (resolver(host) if resolver else _resolve_dns(host))
    except socket.gaierror as exc:
        raise AppError("DOWNLOAD_FAILED", f"cannot resolve host {host!r}") from exc
    if not ips:
        raise AppError("DOWNLOAD_FAILED", f"host {host!r} resolved to no addresses")
    for ip_str in ips:
        if _is_blocked_ip(ipaddress.ip_address(ip_str)):
            raise AppError("SECURITY_REJECTED", f"host {host!r} resolves to forbidden address")
    return ips


async def resolve_pinned_ip(
    url: str, resolver: Callable[[str], Awaitable[list[str]]] | None = None
) -> str:
    """Один проверенный IP для фактического соединения (DNS pinning)."""
    return (await resolve_validated_ips(url, resolver))[0]


async def validate_url_security(
    url: str, resolver: Callable[[str], Awaitable[list[str]]] | None = None
) -> None:
    await resolve_validated_ips(url, resolver)
