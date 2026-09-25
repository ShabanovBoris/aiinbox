"""Small Telegram keyboard projections for the Item action surface."""

from collections.abc import Sequence

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.bot.presentation import bound_source_button_label, item_type_label, source_url_label
from app.bot.provenance import forward_original_url
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
) -> InlineKeyboardMarkup:
    """Keep the primary surface focused on source access and explicit recovery."""
    # ❌ Удалены постоянные lifecycle, interest and feedback rows: они открываются
    # из ephemeral More/Feedback projections, а их durable semantics остаются прежними.
    rows = []
    source_actions = _source_action_buttons(
        item,
        sources,
        focus_source_id,
        video_callback_prefix=f"item:video:{item.id}:",
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
    for index, reference in enumerate(references[:5], start=1):
        if not reference.source_url:
            continue
        try:
            source_type = SourceType(reference.source_type)
        except (TypeError, ValueError):
            source_type = SourceType.TEXT
        label = source_url_label(source_type, reference.source_url)
        if label is None:
            continue
        rows.append(
            [
                InlineKeyboardButton(
                    text=bound_source_button_label(f"[{index}] {label}"),
                    url=reference.source_url,
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def _source_action_buttons(
    item: Item,
    sources: Sequence[ItemSource] | None,
    focus_source_id: int | None,
    *,
    video_callback_prefix: str,
) -> list[InlineKeyboardButton]:
    """Build safe, destination-labeled source actions from the persisted projection."""
    buttons = []
    ordered_sources = sorted(
        sources or (),
        key=lambda source: (source.source_index, source.id if source.id is not None else 0),
    )
    if focus_source_id is not None and any(
        source.id == focus_source_id for source in ordered_sources
    ):
        ordered_sources.sort(key=lambda source: source.id != focus_source_id)

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
        source_ranks = {}
        for source in video_sources:
            ranks[source.source_type] += 1
            source_ranks[id(source)] = ranks[source.source_type]
        for source in video_sources:
            label = "YouTube" if source.source_type is SourceType.YOUTUBE else "Reel"
            if totals[source.source_type] > 1:
                label += f" {source_ranks[id(source)]}"
            # A composite Item may contain several clips; each callback names its
            # exact ItemSource so the delivery worker never guesses which one to send.
            buttons.append(
                InlineKeyboardButton(
                    text=f"📩 Прислать {label}",
                    callback_data=f"{video_callback_prefix}{source.id}",
                )
            )

    source_urls: list[tuple[str, SourceType]] = []
    seen_urls: set[str] = set()
    for source in ordered_sources:
        if (
            source.source_url
            and source.source_url not in seen_urls
            and source_url_label(source.source_type, source.source_url)
        ):
            source_urls.append((source.source_url, source.source_type))
            seen_urls.add(source.source_url)
    # ❌ Удалено общее имя «Открыть N»: URL действия теперь называют назначение,
    # а идентичные URL схлопываются без потери отдельных source-specific resend actions.
    if item.source_url and item.source_url not in seen_urls:
        if source_url_label(item.source_type, item.source_url):
            source_urls.insert(0, (item.source_url, item.source_type))

    base_labels = [source_url_label(source_type, url) for url, source_type in source_urls]
    label_counts = {label: base_labels.count(label) for label in base_labels}
    label_indexes: dict[str, int] = {}
    for (source_url, source_type), base_label in zip(source_urls, base_labels, strict=True):
        if base_label is None:
            continue
        label = base_label
        if label_counts[base_label] > 1:
            label_indexes[base_label] = label_indexes.get(base_label, 0) + 1
            label = bound_source_button_label(f"{label} {label_indexes[base_label]}")
        buttons.append(InlineKeyboardButton(text=label, url=source_url))
    original_url = forward_original_url(item.source_metadata_json)
    if original_url:
        buttons.append(InlineKeyboardButton(text="↗ Оригинальный пост", url=original_url))
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
