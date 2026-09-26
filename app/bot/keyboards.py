"""Small Telegram keyboard projections for the Item action surface."""

from collections.abc import Sequence

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.bot.presentation import (
    ItemNavigationEntry,
    ItemReferenceProjection,
    bound_item_button_label,
    bound_source_button_label,
    item_reference_projection,
    item_type_label,
    source_url_label,
)
from app.domain.category_tokens import category_token as category_callback_token
from app.domain.enums import ItemState, ItemType, ProcessingStatus, SourceType
from app.domain.models import AskReference
from app.storage.models import Item, ItemSource

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
        ("📥 Сохранённое", "nav:inbox"),
        ("🔎 Поиск", "nav:search"),
        ("🧠 Спросить", "nav:ask"),
        ("📌 На этой неделе", "nav:weekly"),
        ("🏷 Категории", "nav:categories"),
        ("👤 Профиль", "nav:profile"),
        ("⚙️ Настройки", "nav:settings"),
        ("📦 Экспорт", "nav:export"),
    )
    buttons = [InlineKeyboardButton(text=label, callback_data=data) for label, data in actions]
    rows = [buttons[index : index + 2] for index in range(0, len(buttons), 2)]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def help_keyboard() -> InlineKeyboardMarkup:
    """Keep explicit format choice in Help while the common menu action stays compact."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📦 Компактный экспорт", callback_data="export:mode:COMPACT"
                )
            ],
            [InlineKeyboardButton(text="📦 Полный экспорт", callback_data="export:mode:FULL")],
            [InlineKeyboardButton(text="☰ Главное меню", callback_data="nav:menu")],
        ]
    )


def export_chooser_keyboard() -> InlineKeyboardMarkup:
    """Keep the two durable export modes reachable from both menu and command."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📦 Компактный", callback_data="export:mode:COMPACT")],
            [InlineKeyboardButton(text="🗃 Полный", callback_data="export:mode:FULL")],
            [InlineKeyboardButton(text="← Меню", callback_data="nav:menu")],
        ]
    )


def attention_chooser_keyboard() -> InlineKeyboardMarkup:
    """Expose the manual result limit and read-only scheduler status as finite choices."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="1", callback_data="nav:attention:show:1"),
                InlineKeyboardButton(text="3", callback_data="nav:attention:show:3"),
                InlineKeyboardButton(text="5", callback_data="nav:attention:show:5"),
            ],
            [
                InlineKeyboardButton(
                    text="📊 Статус Attention", callback_data="nav:attention:status"
                )
            ],
            [InlineKeyboardButton(text="⚙️ Настройки", callback_data="settings:attention:open")],
            [InlineKeyboardButton(text="← Меню", callback_data="nav:menu")],
        ]
    )


def attention_status_keyboard(
    back_callback: str = "nav:attention",
    refresh_callback: str = "nav:attention:status",
) -> InlineKeyboardMarkup:
    """Return from a read-only scheduler projection to its originating surface."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="↻ Обновить", callback_data=refresh_callback)],
            [InlineKeyboardButton(text="← Назад", callback_data=back_callback)],
        ]
    )


def search_empty_keyboard() -> InlineKeyboardMarkup:
    """Offer a fresh lexical query and a clear exit when FTS has no result."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔎 Новый поиск", callback_data="nav:search")],
            [InlineKeyboardButton(text="← Меню", callback_data="nav:menu")],
        ]
    )


def profile_keyboard() -> InlineKeyboardMarkup:
    """Expose the existing durable ProfileUpdateJob flow from the profile projection."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✏️ Настроить профиль", callback_data="nav:profile:edit")],
            [InlineKeyboardButton(text="← Меню", callback_data="nav:menu")],
        ]
    )


def input_cancel_keyboard() -> InlineKeyboardMarkup:
    """Give one-shot Ask/Search input an exit without adding durable chat state."""
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data="nav:input:cancel")]]
    )


def category_navigation_keyboard(
    categories: Sequence[tuple[str, int]],
    *,
    page: int,
    has_previous: bool,
    has_next: bool,
) -> InlineKeyboardMarkup:
    """Keep category choices pageable while callbacks carry only owner-resolved tokens."""
    rows = [
        [
            InlineKeyboardButton(
                text=_category_button_label(category),
                callback_data=f"nav:category:{category_callback_token(category)}:page:0",
            )
        ]
        for category, _count in categories
    ]
    page_actions = []
    if has_previous:
        page_actions.append(
            InlineKeyboardButton(text="← Назад", callback_data=f"nav:categories:page:{page - 1}")
        )
    if has_next:
        page_actions.append(
            InlineKeyboardButton(text="Ещё →", callback_data=f"nav:categories:page:{page + 1}")
        )
    if page_actions:
        rows.append(page_actions)
    rows.append([InlineKeyboardButton(text="← Меню", callback_data="nav:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def item_more_keyboard(item: Item) -> InlineKeyboardMarkup:
    """Expose lifecycle and READY-only controls one level below the content card."""
    rows = []
    if item.state in {ItemState.ACTIVE, ItemState.SNOOZED}:
        rows.extend(
            [
                [
                    InlineKeyboardButton(text="✅ Сделано", callback_data=f"item:done:{item.id}"),
                    InlineKeyboardButton(text="⏰ Отложить", callback_data=f"item:later:{item.id}"),
                ],
                [InlineKeyboardButton(text="🗄 В архив", callback_data=f"item:archive:{item.id}")],
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
    # ❌ Удалены числовые уровни из кнопок: сохранены только понятные человеку слова.
    labels = {1: "Низкий", 2: "Обычный", 3: "Высокий"}
    rows = [
        [
            InlineKeyboardButton(
                text=f"{labels[level]}{' ✓' if item.interest_level == level else ''}",
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


# ❌ Удалена числовая сетка item_navigation_keyboard: заголовок сам открывает
# owner-scoped item:view, поэтому список остаётся понятным и ограниченным строками.
def item_list_keyboard(
    entries: Sequence[ItemNavigationEntry],
    *,
    previous_callback: str | None = None,
    next_callback: str | None = None,
    back_label: str | None = None,
    back_callback: str = "nav:menu",
) -> InlineKeyboardMarkup | None:
    """Render one bounded full-width Item action per visible result."""
    rows = [
        [
            InlineKeyboardButton(
                text=bound_item_button_label(entry.label),
                callback_data=f"item:view:{entry.item_id}",
            )
        ]
        for entry in entries
    ]
    page_actions = []
    if previous_callback:
        page_actions.append(InlineKeyboardButton(text="← Назад", callback_data=previous_callback))
    if next_callback:
        page_actions.append(InlineKeyboardButton(text="Ещё →", callback_data=next_callback))
    if page_actions:
        rows.append(page_actions)
    if back_label:
        rows.append([InlineKeyboardButton(text=back_label, callback_data=back_callback)])
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


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
                    text="⏰ Отложить", callback_data=f"reminder:later:{reminder_id}"
                ),
                InlineKeyboardButton(
                    text="✅ Сделано", callback_data=f"reminder:done:{reminder_id}"
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


# ❌ Удалена отдельная клавиатура общего нуджа: новые мотивационные сообщения
# используют source/actions клавиатуру конкретного сохранения.


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
                    text="👎 Неинтересно", callback_data=f"feedback:not_interesting:{item_id}"
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
                    text="⬆ Важнее",
                    callback_data=f"feedback:priority_higher:{item_id}",
                ),
                InlineKeyboardButton(
                    text="⬇ Менее важно",
                    callback_data=f"feedback:priority_lower:{item_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="📝 Неточная сводка",
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


def feedback_category_keyboard(
    item_id: int,
    categories: Sequence[str],
    *,
    page: int,
    has_previous: bool,
    has_next: bool,
) -> InlineKeyboardMarkup:
    """Expose pageable owner categories while correction keeps its existing token boundary."""
    rows = [
        [
            InlineKeyboardButton(
                text=_category_button_label(category),
                callback_data=(f"feedback:category:{item_id}:{category_callback_token(category)}"),
            )
        ]
        for category in categories
    ]
    page_actions = []
    if has_previous:
        page_actions.append(
            InlineKeyboardButton(
                text="← Назад", callback_data=f"feedback:category_menu:{item_id}:page:{page - 1}"
            )
        )
    if has_next:
        page_actions.append(
            InlineKeyboardButton(
                text="Ещё →", callback_data=f"feedback:category_menu:{item_id}:page:{page + 1}"
            )
        )
    if page_actions:
        rows.append(page_actions)
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
    """Expose notification settings as direct controls, keeping slash forms secondary."""
    label = "🔕 Выключить подборку" if enabled else "🔔 Включить подборку"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🌍 Часовой пояс", callback_data="settings:edit:timezone")],
            [
                InlineKeyboardButton(
                    text="🕘 Время подборки", callback_data="settings:edit:digest_time"
                )
            ],
            [InlineKeyboardButton(text="🌙 Тихие часы", callback_data="settings:edit:quiet_hours")],
            [InlineKeyboardButton(text=label, callback_data="settings:digest")],
            [InlineKeyboardButton(text="✨ Внимание", callback_data="settings:attention:open")],
            [InlineKeyboardButton(text="← Меню", callback_data="nav:menu")],
        ]
    )


def attention_settings_keyboard(
    enabled: bool, level: int, motivation_enabled: bool = True
) -> InlineKeyboardMarkup:
    """Project PM-08 intensity and the independent PM-10 toggle into Telegram controls."""
    labels = ("Спокойно", "Легко", "Обычно", "Активно", "Очень активно")
    levels = [
        InlineKeyboardButton(
            text=f"{number} {label}{' ✓' if number == level else ''}",
            callback_data=f"settings:attention:level:{number}",
        )
        for number, label in enumerate(labels, start=1)
    ]
    toggle = InlineKeyboardButton(
        text="🔕 Выключить внимание" if enabled is True else "🔔 Включить внимание",
        callback_data="settings:attention:toggle",
    )
    motivation_toggle = InlineKeyboardButton(
        text=(
            "💬 Выключить дополнительные напоминания"
            if motivation_enabled is True
            else "💬 Включить дополнительные напоминания"
        ),
        callback_data="settings:attention:motivation",
    )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            levels[:2],
            levels[2:4],
            levels[4:],
            [toggle],
            [motivation_toggle],
            [InlineKeyboardButton(text="📊 Статус", callback_data="settings:attention:status")],
            [InlineKeyboardButton(text="← Настройки", callback_data="settings:open")],
        ]
    )
