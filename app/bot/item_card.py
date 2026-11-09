from collections.abc import Sequence

from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.formatting import Bold, Code, Italic, Text, sizeof

from app.bot.formatting import item_card_content
from app.bot.keyboards import item_keyboard, item_more_keyboard, plain_button_label
from app.domain.enums import ProcessingStatus
from app.storage.models import Item, ItemSource

_STYLES = {"done": "success", "archive": "danger", "retry": "primary"}
_MESSAGE_LIMIT = 4096


def build_item_card(
    item: Item,
    sources: Sequence[ItemSource],
    *,
    original_available: bool,
) -> tuple[Text, InlineKeyboardMarkup | None]:
    content = item_card_content(item, sources)
    body = Text(Bold(content.title))
    if content.badge:
        body = Text(body, "\n", Code(content.badge))
    if content.warnings:
        body = Text(body, "\n", Italic("\n".join(content.warnings)))
    if content.text:
        heading = (
            Text(Bold("Сводка"), "\n") if item.processing_status is ProcessingStatus.READY else ""
        )
        body = Text(body, "\n\n", heading, content.text)
    if len(body) > _MESSAGE_LIMIT:
        text, entities = body.render()
        # Telegram entity offsets are UTF-16. A cut can land between an emoji's surrogates.
        preview = text.encode("utf-16-le")[: 2 * (_MESSAGE_LIMIT - 1)]
        preview = preview.decode("utf-16-le", errors="ignore").rstrip()
        end = sizeof(preview)
        body = Text.from_entities(
            f"{preview}…",
            [
                entity.model_copy(update={"length": min(entity.length, end - entity.offset)})
                for entity in entities
                if entity.offset < end
            ],
        )

    primary = item_keyboard(item, sources, original_available=original_available)
    more = item_more_keyboard(item)
    buttons = {}
    for row in [*more.inline_keyboard[:-1], *primary.inline_keyboard[:-1]]:
        for button in row:
            key = button.callback_data or button.url
            if key is None or key in buttons:
                continue
            action = key.split(":", 2)[1] if button.callback_data else ""
            buttons[key] = button.model_copy(
                update={"text": plain_button_label(button.text), "style": _STYLES.get(action)}
            )
    actions = list(buttons.values())
    keyboard = [actions[index : index + 2] for index in range(0, len(actions), 2)]
    return body, InlineKeyboardMarkup(inline_keyboard=keyboard) if keyboard else None
