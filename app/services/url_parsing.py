"""Разбор входящего сообщения: URL'ы отделяются от пользовательской заметки.

ТЗ §13: text + URL → один Item (source_url, user_note); text + несколько URL →
Item на каждый URL с общим user_note; source_index обеспечивает идемпотентность.
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
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in TRACKING_PARAMS
    ]
    return urlunsplit((scheme, host, parts.path.rstrip("/") or "/", urlencode(query), ""))


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
