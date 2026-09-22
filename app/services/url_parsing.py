"""Разбор сообщения на surrounding text и URL-компоненты одного Item.

ТЗ §13: Telegram message остаётся одним Item; normalized URL становятся ordered
ItemSource rows, а окружающий direct-message text проецируется в user_note.
"""

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Tracking-параметры удаляются при нормализации; неизвестные query-параметры
# сохраняются — они могут быть значимы (ТЗ §62).
TRACKING_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "gclid",
    "fbclid",
}

_URL_RE = re.compile(r"https?://[^\s<>\"']+")

# Пунктуация, приклеенная к URL в конце предложения, в заметку не попадает.
_TRAILING_PUNCT = "[.,;:!?)\\]\"']*"


def normalize_url(url: str) -> str:
    """Минимальная нормализация (ТЗ §62): fragment, lowercase host, tracking-параметры.
    Порт, path (включая trailing slash) и остальные query-параметры не меняются —
    они часть идентичности ресурса."""
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    hostname = (parts.hostname or "").lower()
    default_port = {"http": 80, "https": 443}.get(scheme)
    try:
        port = parts.port
    except ValueError:
        port = None
    netloc = f"[{hostname}]" if ":" in hostname else hostname
    if port is not None and port != default_port:
        netloc = f"{netloc}:{port}"
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in TRACKING_PARAMS
    ]
    return urlunsplit((scheme, netloc, parts.path, urlencode(query), ""))


def find_urls(text: str) -> list[str]:
    """URL'ы в порядке появления; хвостовая пунктуация сообщения отбрасывается."""
    urls = []
    for match in _URL_RE.finditer(text):
        url = match.group(0).rstrip(".,;:!?)]}\"'")
        urls.append(url)
    return urls


def parse_message(text: str) -> tuple[str, list[str]]:
    """Возвращает (user_note, url'ы). Заметка — окружающий текст без URL;
    пунктуация, приклеенная к URL, убирается вместе с ним."""
    urls = find_urls(text)
    note = text
    for url in urls:
        note = re.sub(re.escape(url) + _TRAILING_PUNCT, " ", note)
    return " ".join(note.split()), urls
