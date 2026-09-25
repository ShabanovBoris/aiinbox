"""Pure display labels shared by independent Telegram text and keyboard projections."""

import re
from ipaddress import ip_address
from urllib.parse import urlsplit

from app.domain.enums import ItemType, SourceType

_MAX_SOURCE_LABEL_LENGTH = 64

ITEM_TYPE_LABELS = {
    ItemType.ACTION: "Действие",
    ItemType.LEARN: "Изучить",
    ItemType.READ: "Прочитать",
    ItemType.WATCH: "Посмотреть",
    ItemType.IDEA: "Идея",
    ItemType.REFERENCE: "Справка",
    ItemType.SOMEDAY: "Когда-нибудь",
}


def item_type_label(item_type: ItemType | None) -> str | None:
    """Share one user-facing vocabulary between Details and type-correction buttons."""
    return ITEM_TYPE_LABELS.get(item_type)


def bound_source_button_label(label: str) -> str:
    """Keep source and citation button copy within Telegram's text limit."""
    if len(label) <= _MAX_SOURCE_LABEL_LENGTH:
        return label
    return label[: _MAX_SOURCE_LABEL_LENGTH - 1] + "…"


def source_url_label(source_type: SourceType, url: str) -> str | None:
    """Project a persisted HTTP(S) destination to a bounded label without fetching it."""
    if not isinstance(url, str) or not url:
        return None
    try:
        parsed = urlsplit(url)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            return None
        if parsed.username is not None or parsed.password is not None:
            return None
        _ = parsed.port
    except ValueError:
        return None

    host = parsed.hostname.rstrip(".").lower()
    if not host:
        return None
    try:
        ascii_host = host.encode("idna").decode("ascii")
    except UnicodeError:
        return None
    try:
        ip_address(host)
    except ValueError:
        if not ascii_host or len(ascii_host) > 253:
            return None
        host_labels = ascii_host.split(".")
        if any(
            not label
            or len(label) > 63
            or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", label)
            for label in host_labels
        ):
            return None
    if host.startswith("www."):
        host = host[4:]
    if not host:
        return None

    if source_type is SourceType.YOUTUBE:
        label = "↗ YouTube"
    elif source_type is SourceType.INSTAGRAM:
        label = "↗ Instagram Reel"
    elif source_type is SourceType.WEB and (host == "github.com" or host.endswith(".github.com")):
        label = "↗ GitHub"
    elif source_type is SourceType.WEB:
        label = f"↗ Статья — {host}"
    elif source_type is SourceType.DOCUMENT:
        label = f"↗ Документ — {host}"
    else:
        label = f"↗ {host}"
    return bound_source_button_label(label)
