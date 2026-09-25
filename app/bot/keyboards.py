"""Small Telegram keyboard projections for the Item action surface."""

from collections.abc import Sequence

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.bot.presentation import (
    ItemReferenceProjection,
    bound_source_button_label,
    item_reference_projection,
    item_type_label,
    source_url_label,
)
from app.domain.category_tokens import category_token as category_callback_token
from app.domain.enums import ItemState, ItemType, ProcessingStatus, SourceType
from app.domain.models import AskReference
from app.storage.models import Item, ItemSource

_MAX_CATEGORY_CHOICES = 20
_MAX_CATEGORY_LABEL_LENGTH = 64
_MAX_PRIMARY_SOURCE_ACTIONS = 2
_MAX_SOURCE_MENU_ACTIONS = 12


def item_keyboard(
    item: Item,
    sources: Sequence[ItemSource] | None = None,
    focus_source_id: int | None = None,
    *,
    original_available: bool | None = None,
) -> InlineKeyboardMarkup:
    """Keep the primary surface focused on source access and explicit recovery."""
    # ❌ Удалены постоянные lifecycle, interest and feedback rows: они открываются
    # из ephemeral More/Feedback projections, а их durable semantics остаются прежними.
    reference = item_reference_projection(
        item,
        sources,
        owner_chat_available=(
            item.telegram_message_id is not None
            if original_available is None
            else original_available
        ),
        focus_source_id=focus_source_id,
    )
    rows = []
    if reference.original_available:
        rows.append(
            [InlineKeyboardButton(text="↩️ Оригинал", callback_data=f"item:original:{item.id}")]
        )
    source_actions = _reference_action_buttons(
        reference, video_callback_prefix=f"item:video:{item.id}:"
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
        rows.append(
            [InlineKeyboardButton(text="🔁 Повторить", callback_data=f"item:retry:{item.id}")]
        )

    if len(source_actions) <= _MAX_PRIMARY_SOURCE_ACTIONS:
        rows.extend([[button] for button in source_actions])
    else:
        # A source-specific resend is the most useful action on overflowing video Items.
        preferred_video = next(
            (
                button
                for button in source_actions
                if button.callback_data
                and button.callback_data.startswith(f"item:video:{item.id}:")
            ),
            None,
        )
        if preferred_video is not None:
            rows.append([preferred_video])
        rows.append(
            [InlineKeyboardButton(text="🔗 Источники", callback_data=f"item:sources:{item.id}")]
        )
    rows.append([InlineKeyboardButton(text="••• Ещё", callback_data=f"item:more:{item.id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def main_menu_keyboard() -> InlineKeyboardMarkup:
    """Expose existing bot surfaces as a compact presentation-only projection."""
    actions = (
        ("🎯 Сегодня", "nav:today"),
        ("✨ Внимание", "nav:attention"),
        ("📥 Inbox", "nav:inbox"),
        ("🔎 Поиск", "nav:search"),
        ("🧠 Ask", "nav:ask"),
        ("📊 Неделя", "nav:weekly"),
        ("🏷 Категории", "nav:categories"),
        ("👤 Профиль", "nav:profile"),
        ("⚙️ Настройки", "nav:settings"),
        ("📦 Экспорт", "nav:export"),
    )
    buttons = [InlineKeyboardButton(text=label, callback_data=data) for label, data in actions]
    rows = [buttons[index : index + 2] for index in range(0, len(buttons), 2)]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def input_cancel_keyboard() -> InlineKeyboardMarkup:
    """Give one-shot Ask/Search input an exit without adding durable chat state."""
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data="nav:input:cancel")]]
    )


def export_mode_keyboard() -> InlineKeyboardMarkup:
    """Offer the two existing durable export modes from one chooser message."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Compact", callback_data="export:mode:COMPACT"),
                InlineKeyboardButton(text="Full", callback_data="export:mode:FULL"),
            ],
            [InlineKeyboardButton(text="← Меню", callback_data="nav:menu")],
        ]
    )


def category_navigation_keyboard(categories: Sequence[str]) -> InlineKeyboardMarkup:
    """Resolve bounded owner-supplied category labels through stable short tokens."""
    rows = [
        [
            InlineKeyboardButton(
                text=_category_button_label(category),
                callback_data=f"nav:category:{category_callback_token(category)}",
            )
        ]
        for category in categories[:_MAX_CATEGORY_CHOICES]
    ]
    rows.append([InlineKeyboardButton(text="← Меню", callback_data="nav:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def item_more_keyboard(item: Item) -> InlineKeyboardMarkup:
    """Expose lifecycle and READY-only controls one level below the content card."""
    rows = []
    if item.state in {ItemState.ACTIVE, ItemState.SNOOZED}:
        rows.extend(
            [
                [
                    InlineKeyboardButton(text="✅ Готово", callback_data=f"item:done:{item.id}"),
                    InlineKeyboardButton(text="⏰ Позже", callback_data=f"item:later:{item.id}"),
                ],
                [InlineKeyboardButton(text="🗄 Архив", callback_data=f"item:archive:{item.id}")],
            ]
        )
    if item.processing_status is ProcessingStatus.READY:
        if item.state in {ItemState.ACTIVE, ItemState.SNOOZED}:
            rows.append(
                [
                    InlineKeyboardButton(
                        text="⭐ Интерес", callback_data=f"item:interest_menu:{item.id}"
                    )
                ]
            )
        rows.extend(
            [
                [
                    InlineKeyboardButton(
                        text="🛠 Обратная связь", callback_data=f"feedback:menu:{item.id}"
                    )
                ],
                [InlineKeyboardButton(text="ℹ️ Детали", callback_data=f"item:details:{item.id}")],
            ]
        )
    rows.append([InlineKeyboardButton(text="← Назад", callback_data=f"item:back:{item.id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def item_interest_keyboard(item: Item) -> InlineKeyboardMarkup:
    """Render the current canonical interest level without mutating the Item."""
    labels = {1: "Низкий", 2: "Обычный", 3: "Высокий"}
    rows = [
        [
            InlineKeyboardButton(
                text=f"{level} — {labels[level]}{' ✓' if item.interest_level == level else ''}",
                callback_data=f"item:interest:{item.id}:{level}",
            )
        ]
        for level in (1, 2, 3)
    ]
    rows.append([InlineKeyboardButton(text="← Назад", callback_data=f"item:more:{item.id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def item_details_keyboard(item_id: int) -> InlineKeyboardMarkup:
    """Keep Details read-only and provide a direct return to the compact Item card."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="← Назад", callback_data=f"item:back:{item_id}")]
        ]
    )


def item_sources_keyboard(
    item: Item, sources: Sequence[ItemSource] | None = None
) -> InlineKeyboardMarkup:
    """Expose the bounded full source-action projection without persistent menu state."""
    actions = _source_action_buttons(
        item, sources, None, video_callback_prefix=f"item:video:{item.id}:"
    )[:_MAX_SOURCE_MENU_ACTIONS]
    rows = [[button] for button in actions]
    rows.append([InlineKeyboardButton(text="← Назад", callback_data=f"item:back:{item.id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def ask_sources_keyboard(
    references: Sequence[AskReference],
) -> InlineKeyboardMarkup | None:
    """Expose only persisted HTTP(S) URLs for citations already validated by Ask."""
    rows = []
    original_item_ids = set()
    for index, reference in enumerate(references[:5], start=1):
        row = []
        if reference.source_url:
            try:
                source_type = SourceType(reference.source_type)
            except (TypeError, ValueError):
                source_type = SourceType.TEXT
            label = source_url_label(source_type, reference.source_url)
            if label is not None:
                row.append(
                    InlineKeyboardButton(
                        text=bound_source_button_label(f"[{index}] {label}"),
                        url=reference.source_url,
                    )
                )
        if reference.original_available and reference.item_id not in original_item_ids:
            row.append(
                InlineKeyboardButton(
                    text=f"[{index}] ↩️ Оригинал",
                    callback_data=f"item:original:{reference.item_id}",
                )
            )
            original_item_ids.add(reference.item_id)
        if row:
            rows.append(row)
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def item_navigation_keyboard(item_ids: Sequence[int]) -> InlineKeyboardMarkup | None:
    """Numbered selectors preserve the visible order without expanding list cards."""
    buttons = [
        InlineKeyboardButton(text=str(index), callback_data=f"item:view:{item_id}")
        for index, item_id in enumerate(item_ids[:20], start=1)
    ]
    if not buttons:
        return None
    rows = [buttons[index : index + 5] for index in range(0, len(buttons), 5)]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _source_action_buttons(
    item: Item,
    sources: Sequence[ItemSource] | None,
    focus_source_id: int | None,
    *,
    video_callback_prefix: str,
) -> list[InlineKeyboardButton]:
    """Keep Bot API callback construction at the keyboard edge."""
    # ❌ Удалена отдельная сборка URL/media действий из keyboards: единая
    # provenance projection не даёт READY, Ask и Reminder расходиться по labels.
    reference = item_reference_projection(
        item, sources, owner_chat_available=False, focus_source_id=focus_source_id
    )
    return _reference_action_buttons(
        reference,
        video_callback_prefix=video_callback_prefix,
        focus_source_id=focus_source_id,
    )


def _reference_action_buttons(
    reference: ItemReferenceProjection,
    *,
    video_callback_prefix: str,
    focus_source_id: int | None = None,
) -> list[InlineKeyboardButton]:
    """Translate stable provenance facts into this surface's callback namespace."""
    buttons = []
    actions = list(reference.source_actions)
    if focus_source_id is not None:
        actions.sort(key=lambda action: action.source_id != focus_source_id)
    for action in actions:
        if action.can_resend_media and action.source_id is not None:
            buttons.append(
                InlineKeyboardButton(
                    text=action.label,
                    callback_data=f"{video_callback_prefix}{action.source_id}",
                )
            )
        elif action.url is not None:
            buttons.append(InlineKeyboardButton(text=action.label, url=action.url))
    return buttons


def _source_action_rows(
    item: Item,
    sources: Sequence[ItemSource] | None,
    focus_source_id: int | None,
    *,
    video_callback_prefix: str,
) -> list[list[InlineKeyboardButton]]:
    """Preserve the reminder's existing full source projection with shared labels."""
    return [
        [button]
        for button in _source_action_buttons(
            item,
            sources,
            focus_source_id,
            video_callback_prefix=video_callback_prefix,
        )
    ]


def proactive_reminder_keyboard(
    reminder_id: int,
    item: Item,
    sources: Sequence[ItemSource] | None = None,
    *,
    focus_source_id: int | None = None,
    original_available: bool | None = None,
) -> InlineKeyboardMarkup:
    """Keep source access primary and move PM-11 reactions to an ephemeral More menu."""
    reference = item_reference_projection(
        item,
        sources,
        owner_chat_available=(
            item.telegram_message_id is not None
            if original_available is None
            else original_available
        ),
        focus_source_id=focus_source_id,
    )
    rows = []
    if reference.original_available:
        rows.append(
            [
                InlineKeyboardButton(
                    text="↩️ Оригинал", callback_data=f"reminder:original:{reminder_id}"
                )
            ]
        )
    source_actions = _reference_action_buttons(
        reference,
        video_callback_prefix=f"reminder:open:{reminder_id}:",
        focus_source_id=focus_source_id,
    )
    rows.extend([[button] for button in source_actions[:_MAX_PRIMARY_SOURCE_ACTIONS]])
    if len(source_actions) > _MAX_PRIMARY_SOURCE_ACTIONS:
        rows.append(
            [
                InlineKeyboardButton(
                    text="🔗 Источники", callback_data=f"reminder:sources:{reminder_id}"
                )
            ]
        )
    rows.append(
        [InlineKeyboardButton(text="••• Ещё", callback_data=f"reminder:more:{reminder_id}")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def reminder_more_keyboard(reminder_id: int) -> InlineKeyboardMarkup:
    """Project existing PM-11 reactions into a read-only Reminder submenu."""
    # Callback identities remain the same so this UI projection cannot change
    # the durable feedback semantics owned by ReminderFeedbackService.
    return InlineKeyboardMarkup(
        inline_keyboard=[
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
            [InlineKeyboardButton(text="← Назад", callback_data=f"reminder:back:{reminder_id}")],
        ]
    )


def reminder_sources_keyboard(
    reminder_id: int,
    item: Item,
    sources: Sequence[ItemSource] | None = None,
    *,
    focus_source_id: int | None = None,
) -> InlineKeyboardMarkup:
    """Show bounded source actions and return to the reminder without storing menu state."""
    rows = _source_action_rows(
        item,
        sources,
        focus_source_id,
        video_callback_prefix=f"reminder:open:{reminder_id}:",
    )[:_MAX_SOURCE_MENU_ACTIONS]
    rows.append(
        [InlineKeyboardButton(text="← Назад", callback_data=f"reminder:back:{reminder_id}")]
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
    """Keep every PM-05 signal in Feedback while More remains the parent menu."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="👍 Полезно", callback_data=f"feedback:useful:{item_id}"),
                InlineKeyboardButton(
                    text="👎 Не моё", callback_data=f"feedback:not_interesting:{item_id}"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🏷 Категория", callback_data=f"feedback:category_menu:{item_id}"
                ),
                InlineKeyboardButton(text="🧩 Тип", callback_data=f"feedback:type_menu:{item_id}"),
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
                    text="📝 Summary неверный",
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
    buttons = [
        InlineKeyboardButton(
            text=item_type_label(item_type) or item_type.value,
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
    """Expose digest and existing Attention settings in one compact projection."""
    label = "🔕 Выключить digest" if enabled else "🔔 Включить digest"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=label, callback_data="settings:digest")],
            [InlineKeyboardButton(text="⚡ Attention", callback_data="settings:attention:open")],
            [InlineKeyboardButton(text="← Меню", callback_data="nav:menu")],
        ]
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
        inline_keyboard=[
            levels[:2],
            levels[2:4],
            levels[4:],
            [toggle],
            [motivation_toggle],
            [InlineKeyboardButton(text="← Настройки", callback_data="settings:open")],
        ]
    )
