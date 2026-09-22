import re
from datetime import UTC, datetime

from aiogram.types import (
    MessageEntity,
    MessageOriginChannel,
    MessageOriginChat,
    MessageOriginHiddenUser,
    MessageOriginUser,
)

ForwardOrigin = (
    MessageOriginUser | MessageOriginHiddenUser | MessageOriginChat | MessageOriginChannel
)

_PUBLIC_USERNAME = re.compile(r"^[A-Za-z0-9_]+$")


def text_with_entity_urls(text: str | None, entities: list[MessageEntity] | None) -> str | None:
    """Expose Telegram hidden text-link targets to the canonical URL parser.

    Telegram captions can render a link as a label (for example ``Review-prompts``)
    while the actual URL exists only in ``MessageEntity.url``. Appending those targets
    at the transport edge lets ingestion keep one parser/dedup implementation; the
    appended URL is stripped again when forwarded source text is persisted.
    """
    if not text:
        return None
    urls: list[str] = []
    for entity in entities or []:
        url = entity.url
        if url and url not in text and url not in urls:
            urls.append(url)
    if not urls:
        return text
    return "\n".join([text, *urls])


def normalize_forward_origin(origin: ForwardOrigin | None) -> dict[str, str | int | bool] | None:
    """Convert Telegram SDK provenance into a small persisted transport-neutral envelope.

    The application stores only fields Telegram actually supplied. Telegram user/chat
    ids are deliberately omitted because PM-02 needs provenance for the user interface,
    not a second identity graph inside the domain model.
    """
    if origin is None:
        return None

    metadata: dict[str, str | int | bool] = {
        "forwarded": True,
        "forward_origin_type": getattr(origin.type, "value", origin.type),
        "original_sent_at": _utc_iso(origin.date),
    }
    if isinstance(origin, MessageOriginUser):
        metadata["forward_source_name"] = origin.sender_user.full_name
        if origin.sender_user.username:
            metadata["forward_source_username"] = origin.sender_user.username
    elif isinstance(origin, MessageOriginHiddenUser):
        metadata["forward_source_name"] = origin.sender_user_name
    elif isinstance(origin, MessageOriginChat):
        if origin.sender_chat.title:
            metadata["forward_source_name"] = origin.sender_chat.title
        if origin.sender_chat.username:
            metadata["forward_source_username"] = origin.sender_chat.username
    elif isinstance(origin, MessageOriginChannel):
        if origin.chat.title:
            metadata["forward_source_name"] = origin.chat.title
        if origin.chat.username:
            metadata["forward_source_username"] = origin.chat.username
        metadata["forward_message_id"] = origin.message_id
    return metadata


def forward_source_label(metadata: dict | None) -> str | None:
    """Return a compact safe display label without exposing internal Telegram ids."""
    if not metadata or metadata.get("forwarded") is not True:
        return None
    name = _display_text(metadata.get("forward_source_name"))
    if name:
        return name
    username = _public_username(metadata.get("forward_source_username"))
    return f"@{username}" if username else None


def forward_original_url(metadata: dict | None) -> str | None:
    """Build a public Telegram message link only from a channel username + message id.

    No lookup or inference is allowed here: private chats, hidden users and channels
    without a public username intentionally return no link.
    """
    if not metadata or metadata.get("forward_origin_type") != "channel":
        return None
    username = _public_username(metadata.get("forward_source_username"))
    message_id = metadata.get("forward_message_id")
    if username is None or not isinstance(message_id, int) or isinstance(message_id, bool):
        return None
    if message_id <= 0:
        return None
    return f"https://t.me/{username}/{message_id}"


def _utc_iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _display_text(value: object, max_length: int = 120) -> str | None:
    if not isinstance(value, str):
        return None
    compact = " ".join(value.split())
    return compact[:max_length] or None


def _public_username(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    username = value.removeprefix("@")
    return username if _PUBLIC_USERNAME.fullmatch(username) else None
