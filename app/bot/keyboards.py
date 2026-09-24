"""Small Telegram keyboard projections for the Item action surface."""

from collections.abc import Sequence

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.bot.provenance import forward_original_url
from app.domain.category_tokens import category_token as category_callback_token
from app.domain.enums import ItemType, ProcessingStatus, SourceType
from app.storage.models import Item, ItemSource

_MAX_CATEGORY_CHOICES = 20
_MAX_CATEGORY_LABEL_LENGTH = 64


def item_keyboard(
    item: Item,
    sources: Sequence[ItemSource] | None = None,
    focus_source_id: int | None = None,
) -> InlineKeyboardMarkup:
    """Keep action intent in callback data; lifecycle state stays in SQLite."""
    # ❌ Удалена фиксированная rows-разметка без interest controls: READY-клавиатура
    # теперь должна проецировать canonical interest_level перед lifecycle actions.
    rows = []
    if item.processing_status is ProcessingStatus.READY:
        # Это presentation-проекция canonical interest_level: callback хранит
        # только intent, а изменение и Event остаются в application service.
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{level}{' ✓' if item.interest_level == level else ''}",
                    callback_data=f"item:interest:{item.id}:{level}",
                )
                for level in (1, 2, 3)
            ]
        )
        rows.append(
            [
                InlineKeyboardButton(text="👍 Полезно", callback_data=f"feedback:useful:{item.id}"),
                InlineKeyboardButton(
                    text="👎 Не моё", callback_data=f"feedback:not_interesting:{item.id}"
                ),
                InlineKeyboardButton(text="⚙ Исправить", callback_data=f"feedback:menu:{item.id}"),
            ]
        )
    rows.extend(
        [
            [
                InlineKeyboardButton(text="✅ Done", callback_data=f"item:done:{item.id}"),
                InlineKeyboardButton(text="⏰ Later", callback_data=f"item:later:{item.id}"),
            ],
            [InlineKeyboardButton(text="🗄 Archive", callback_data=f"item:archive:{item.id}")],
        ]
    )
    rows.extend(
        _source_action_rows(
            item,
            sources,
            focus_source_id,
            video_callback_prefix=f"item:video:{item.id}:",
        )
    )
    failed_sources = [source for source in sources or () if source.extraction_status == "FAILED"]
    has_retryable_source_failure = any(not source.failure_is_permanent for source in failed_sources)
    # ❌ Удалено дублирующее построение source actions из этого метода: общий
    # projection сохраняет обычный Item UI и позволяет reminder заменить callback identity.
    # ❌ Удалено безусловное Retry для PARTIAL/FAILED: permanent extraction failure
    # нельзя исправить повтором, но более поздний LLM/priority failure retryable.
    failed_item_retryable = (
        sources is None
        or item.processing_stage != "EXTRACTING"
        or not failed_sources
        or has_retryable_source_failure
    )
    partial_item_retryable = sources is None or has_retryable_source_failure
    if (item.processing_status is ProcessingStatus.FAILED and failed_item_retryable) or (
        item.processing_status is ProcessingStatus.READY
        and item.analysis_completeness == "PARTIAL"
        and partial_item_retryable
    ):
        rows.append([InlineKeyboardButton(text="🔁 Retry", callback_data=f"item:retry:{item.id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _source_action_rows(
    item: Item,
    sources: Sequence[ItemSource] | None,
    focus_source_id: int | None,
    *,
    video_callback_prefix: str,
) -> list[list[InlineKeyboardButton]]:
    """Share source actions while keeping URL clicks distinct from observable callbacks."""
    rows = []
    if item.processing_status is ProcessingStatus.READY:
        video_sources = [
            source
            for source in sources or ()
            if source.id is not None
            and source.source_type in {SourceType.YOUTUBE, SourceType.INSTAGRAM}
            and source.extraction_status == "READY"
            and source.source_url
        ]
        totals = {
            source_type: sum(source.source_type is source_type for source in video_sources)
            for source_type in (SourceType.YOUTUBE, SourceType.INSTAGRAM)
        }
        ranks = {SourceType.YOUTUBE: 0, SourceType.INSTAGRAM: 0}
        source_ranks = {}
        for source in video_sources:
            ranks[source.source_type] += 1
            source_ranks[id(source)] = ranks[source.source_type]
        if focus_source_id is not None and any(
            source.id == focus_source_id for source in video_sources
        ):
            video_sources.sort(key=lambda source: source.id != focus_source_id)
        for source in video_sources:
            label = "YouTube" if source.source_type is SourceType.YOUTUBE else "Instagram Reel"
            if totals[source.source_type] > 1:
                label += f" {source_ranks[id(source)]}"
            # A composite Item may contain several clips; each callback names its
            # exact ItemSource so the delivery worker never guesses which one to send.
            rows.append(
                [
                    InlineKeyboardButton(
                        text=f"📹 Отправить {label}",
                        callback_data=f"{video_callback_prefix}{source.id}",
                    )
                ]
            )
    source_urls = []
    if sources is not None:
        ordered_sources = list(sources)
        if focus_source_id is not None and any(
            source.id == focus_source_id for source in ordered_sources
        ):
            ordered_sources.sort(key=lambda source: source.id != focus_source_id)
        source_urls = list(
            dict.fromkeys(source.source_url for source in ordered_sources if source.source_url)
        )
    # ❌ Удалена проекция ссылки только из parent Item.source_url: canonical URL
    # composite Item принадлежат child ItemSource и должны оставаться открываемыми.
    if item.source_url and item.source_url not in source_urls:
        source_urls.insert(0, item.source_url)
    for index, source_url in enumerate(source_urls, start=1):
        label = "🔗 Открыть" if len(source_urls) == 1 else f"🔗 Открыть {index}"
        rows.append([InlineKeyboardButton(text=label, url=source_url)])
    original_url = forward_original_url(item.source_metadata_json)
    if original_url:
        rows.append([InlineKeyboardButton(text="↗ Открыть оригинал", url=original_url)])
    return rows


def proactive_reminder_keyboard(
    reminder_id: int,
    item: Item,
    sources: Sequence[ItemSource] | None = None,
    *,
    focus_source_id: int | None = None,
) -> InlineKeyboardMarkup:
    """Project a focused reaction surface whose callbacks retain Reminder identity."""
    rows = _source_action_rows(
        item,
        sources,
        focus_source_id,
        video_callback_prefix=f"reminder:open:{reminder_id}:",
    )
    rows.extend(
        [
            [
                InlineKeyboardButton(
                    text="⏰ Позже", callback_data=f"reminder:later:{reminder_id}"
                ),
                InlineKeyboardButton(
                    text="✅ Готово", callback_data=f"reminder:done:{reminder_id}"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🙈 Не сейчас", callback_data=f"reminder:dismiss:{reminder_id}"
                )
            ],
            [
                InlineKeyboardButton(
                    text="👎 Меньше таких", callback_data=f"reminder:less:{reminder_id}"
                )
            ],
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def motivation_reminder_keyboard(reminder_id: int) -> InlineKeyboardMarkup:
    """Expose only the two factual nudge reactions; OK intentionally records no positive Event."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="👍 Ок", callback_data=f"reminder:ok:{reminder_id}"),
                InlineKeyboardButton(
                    text="👎 Меньше таких", callback_data=f"reminder:less:{reminder_id}"
                ),
            ]
        ]
    )


def reminder_snooze_keyboard(reminder_id: int) -> InlineKeyboardMarkup:
    """Keep the originating Reminder id through every explicit snooze choice."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Завтра",
                    callback_data=f"reminder:snooze:{reminder_id}:tomorrow",
                ),
                InlineKeyboardButton(
                    text="Через неделю",
                    callback_data=f"reminder:snooze:{reminder_id}:week",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="Через месяц",
                    callback_data=f"reminder:snooze:{reminder_id}:month",
                ),
                InlineKeyboardButton(text="Отмена", callback_data=f"reminder:cancel:{reminder_id}"),
            ],
        ]
    )


def feedback_menu_keyboard(item_id: int) -> InlineKeyboardMarkup:
    """Project secondary correction intents without changing Item state."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Категория", callback_data=f"feedback:category_menu:{item_id}"
                ),
                InlineKeyboardButton(text="Тип", callback_data=f"feedback:type_menu:{item_id}"),
            ],
            [
                InlineKeyboardButton(
                    text="⬆ Приоритет",
                    callback_data=f"feedback:priority_higher:{item_id}",
                ),
                InlineKeyboardButton(
                    text="⬇ Приоритет",
                    callback_data=f"feedback:priority_lower:{item_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="Summary неверный",
                    callback_data=f"feedback:summary_wrong:{item_id}",
                )
            ],
            [InlineKeyboardButton(text="← Назад", callback_data=f"feedback:back:{item_id}")],
        ]
    )


def _category_button_label(category: str) -> str:
    """Fit dynamic labels into Telegram's inline-button text bound."""
    if len(category) <= _MAX_CATEGORY_LABEL_LENGTH:
        return category
    prefix_length = _MAX_CATEGORY_LABEL_LENGTH - 13
    return f"{category[:prefix_length]}…{category[-12:]}"


def feedback_category_keyboard(item_id: int, categories: Sequence[str]) -> InlineKeyboardMarkup:
    """Expose bounded, user-scoped category choices using short stable tokens."""
    rows = [
        [
            InlineKeyboardButton(
                text=_category_button_label(category),
                callback_data=(f"feedback:category:{item_id}:{category_callback_token(category)}"),
            )
        ]
        for category in categories[:_MAX_CATEGORY_CHOICES]
    ]
    rows.append([InlineKeyboardButton(text="← Назад", callback_data=f"feedback:menu:{item_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def feedback_type_keyboard(item_id: int) -> InlineKeyboardMarkup:
    """Use stable ItemType values in callback data and localized labels in Telegram."""
    labels = {
        ItemType.ACTION: "Действие",
        ItemType.LEARN: "Изучить",
        ItemType.READ: "Прочитать",
        ItemType.WATCH: "Посмотреть",
        ItemType.IDEA: "Идея",
        ItemType.REFERENCE: "Справка",
        ItemType.SOMEDAY: "Когда-нибудь",
    }
    buttons = [
        InlineKeyboardButton(
            text=labels[item_type],
            callback_data=f"feedback:type:{item_id}:{item_type.value}",
        )
        for item_type in ItemType
    ]
    rows = [buttons[index : index + 2] for index in range(0, len(buttons), 2)]
    rows.append([InlineKeyboardButton(text="← Назад", callback_data=f"feedback:menu:{item_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def snooze_keyboard(item_id: int) -> InlineKeyboardMarkup:
    """The Later choices are explicit callback values, not free-form timestamps."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Завтра", callback_data=f"item:snooze:{item_id}:tomorrow"
                ),
                InlineKeyboardButton(
                    text="Через неделю", callback_data=f"item:snooze:{item_id}:week"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="Через месяц", callback_data=f"item:snooze:{item_id}:month"
                ),
                InlineKeyboardButton(text="Отмена", callback_data=f"item:cancel:{item_id}"),
            ],
        ]
    )


def settings_keyboard(enabled: bool) -> InlineKeyboardMarkup:
    """Minimal settings projection: the common digest toggle is one tap."""
    label = "🔕 Выключить digest" if enabled else "🔔 Включить digest"
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=label, callback_data="settings:digest")]]
    )


def attention_settings_keyboard(
    enabled: bool, level: int, motivation_enabled: bool = True
) -> InlineKeyboardMarkup:
    """Project PM-08 intensity and the independent PM-10 toggle into Telegram controls."""
    labels = ("Calm", "Light", "Normal", "Active", "Aggressive")
    levels = [
        InlineKeyboardButton(
            text=f"{number} {label}{' ✓' if number == level else ''}",
            callback_data=f"settings:attention:level:{number}",
        )
        for number, label in enumerate(labels, start=1)
    ]
    toggle = InlineKeyboardButton(
        text="🔕 Attention OFF" if enabled is True else "🔔 Attention ON",
        callback_data="settings:attention:toggle",
    )
    motivation_toggle = InlineKeyboardButton(
        text="💬 Motivation OFF" if motivation_enabled is True else "💬 Motivation ON",
        callback_data="settings:attention:motivation",
    )
    return InlineKeyboardMarkup(
        inline_keyboard=[levels[:2], levels[2:4], levels[4:], [toggle], [motivation_toggle]]
    )
