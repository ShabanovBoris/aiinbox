"""SSRF-защита web-загрузки (обязательное требование, ТЗ §20).

Разрешены только http/https на публичные адреса; DNS резолвится до запроса,
каждый redirect-хоп проверяется заново.
"""

import asyncio
import ipaddress
import socket
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


async def validate_url_security(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise AppError("SECURITY_REJECTED", f"scheme {parts.scheme!r} is not allowed")
    host = parts.hostname
    if not host:
        raise AppError("SECURITY_REJECTED", "URL has no host")
    if host == "localhost" or host.endswith(".localhost"):
        # localhost запрещён по имени: его резолюция не должна зависеть от DNS.
        raise AppError("SECURITY_REJECTED", "localhost is not allowed")

    # IP-литерал: DNS не нужен, проверяем адрес напрямую (иначе литерал частного
    # адреса прошёл бы через любую подмену/кэш DNS-ответов).
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if _is_blocked_ip(literal):
            raise AppError("SECURITY_REJECTED", f"host {host!r} is a forbidden address")
        return

    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise AppError("DOWNLOAD_FAILED", f"cannot resolve host {host!r}") from exc

    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if _is_blocked_ip(ip):
            raise AppError("SECURITY_REJECTED", f"host {host!r} resolves to forbidden address")
