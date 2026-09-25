"""Pure, persisted-data-only projections shared by Telegram surfaces."""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from ipaddress import ip_address
from urllib.parse import urlsplit

from app.bot.provenance import forward_original_url
from app.domain.enums import ItemType, ProcessingStatus, SourceType
from app.services.url_security import is_public_ip_address
from app.storage.models import Item, ItemSource

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


@dataclass(frozen=True, slots=True)
class SourceReferenceAction:
    """A trusted URL or source-specific resend derived from persisted Item data."""

    source_id: int | None
    source_type: SourceType
    label: str
    url: str | None = None
    can_resend_media: bool = False


@dataclass(frozen=True, slots=True)
class ItemReferenceProjection:
    """One immutable navigation view shared by cards, lists, and reminders."""

    item_id: int
    title: str
    original_available: bool
    source_actions: tuple[SourceReferenceAction, ...]


def item_reference_projection(
    item: Item,
    sources: Sequence[ItemSource] | None = None,
    *,
    owner_chat_available: bool,
    focus_source_id: int | None = None,
) -> ItemReferenceProjection:
    """Project only safe persisted provenance; never fetch or infer a destination."""
    ordered_sources = sorted(
        sources or (),
        key=lambda source: (source.source_index, source.id if source.id is not None else 0),
    )
    if focus_source_id is not None and any(
        source.id == focus_source_id for source in ordered_sources
    ):
        ordered_sources.sort(key=lambda source: source.id != focus_source_id)

    security_rejected_urls = {
        source.source_url
        for source in ordered_sources
        if source.error_code == "SECURITY_REJECTED" and source.source_url
    }
    actions: list[SourceReferenceAction] = []
    if item.processing_status is ProcessingStatus.READY:
        video_sources = [
            source
            for source in ordered_sources
            if source.id is not None
            and source.source_type in {SourceType.YOUTUBE, SourceType.INSTAGRAM}
            and source.extraction_status == "READY"
            and source.source_url
            and source_url_label(source.source_type, source.source_url)
        ]
        totals = {
            source_type: sum(source.source_type is source_type for source in video_sources)
            for source_type in (SourceType.YOUTUBE, SourceType.INSTAGRAM)
        }
        ranks = {SourceType.YOUTUBE: 0, SourceType.INSTAGRAM: 0}
        for source in video_sources:
            ranks[source.source_type] += 1
            label = "YouTube" if source.source_type is SourceType.YOUTUBE else "Reel"
            if totals[source.source_type] > 1:
                label += f" {ranks[source.source_type]}"
            actions.append(
                SourceReferenceAction(
                    source_id=source.id,
                    source_type=source.source_type,
                    label=f"📩 Прислать {label}",
                    can_resend_media=True,
                )
            )

    source_urls: list[tuple[str, SourceType, int | None]] = []
    seen_urls: set[str] = set()
    for source in ordered_sources:
        if (
            source.source_url
            and source.source_url not in security_rejected_urls
            and source.source_url not in seen_urls
            and source_url_label(source.source_type, source.source_url)
        ):
            source_urls.append((source.source_url, source.source_type, source.id))
            seen_urls.add(source.source_url)
    if (
        item.source_url
        and item.source_url not in security_rejected_urls
        and item.source_url not in seen_urls
        and source_url_label(item.source_type, item.source_url)
    ):
        source_urls.insert(0, (item.source_url, item.source_type, None))

    public_forward_url = forward_original_url(item.source_metadata_json)
    if public_forward_url:
        source_urls = [entry for entry in source_urls if entry[0] != public_forward_url]

    labels = [source_url_label(source_type, url) for url, source_type, _ in source_urls]
    label_counts = {label: labels.count(label) for label in labels}
    label_indexes: dict[str, int] = {}
    for (url, source_type, source_id), base_label in zip(source_urls, labels, strict=True):
        if base_label is None:
            continue
        label = base_label
        if label_counts[base_label] > 1:
            label_indexes[base_label] = label_indexes.get(base_label, 0) + 1
            label = bound_source_button_label(f"{label} {label_indexes[base_label]}")
        actions.append(
            SourceReferenceAction(
                source_id=source_id,
                source_type=source_type,
                label=label,
                url=url,
            )
        )
    if public_forward_url:
        actions.append(
            SourceReferenceAction(
                source_id=None,
                source_type=SourceType.TEXT,
                label="↗ Оригинальный пост",
                url=public_forward_url,
            )
        )

    return ItemReferenceProjection(
        item_id=item.id,
        title=item.title or "Без названия",
        original_available=owner_chat_available and item.telegram_message_id is not None,
        source_actions=tuple(actions),
    )


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
    # Source buttons are pure projections, so they cannot safely resolve DNS.
    # Reject local names and non-global IP literals that can be classified here.
    if host == "localhost" or host.endswith((".localhost", ".local")):
        return None
    try:
        ip_literal = ip_address(host)
    except ValueError:
        ip_literal = None

    if ip_literal is not None:
        if not is_public_ip_address(ip_literal):
            return None
    else:
        try:
            ascii_host = host.encode("idna").decode("ascii")
        except UnicodeError:
            return None
        if not ascii_host or len(ascii_host) > 253:
            return None
        if "." not in ascii_host:
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
