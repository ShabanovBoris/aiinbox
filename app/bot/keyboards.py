"""Small Telegram keyboard projections for the Item action surface."""

from collections.abc import Sequence

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.bot.provenance import forward_original_url
from app.domain.enums import ProcessingStatus, SourceType
from app.storage.models import Item, ItemSource


def item_keyboard(item: Item, sources: Sequence[ItemSource] | None = None) -> InlineKeyboardMarkup:
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
    rows.extend(
        [
            [
                InlineKeyboardButton(text="✅ Done", callback_data=f"item:done:{item.id}"),
                InlineKeyboardButton(text="⏰ Later", callback_data=f"item:later:{item.id}"),
            ],
            [InlineKeyboardButton(text="🗄 Archive", callback_data=f"item:archive:{item.id}")],
        ]
    )
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
        for source in video_sources:
            ranks[source.source_type] += 1
            label = "YouTube" if source.source_type is SourceType.YOUTUBE else "Instagram Reel"
            if totals[source.source_type] > 1:
                label += f" {ranks[source.source_type]}"
            # A composite Item may contain several clips; each callback names its
            # exact ItemSource so the delivery worker never guesses which one to send.
            rows.append(
                [
                    InlineKeyboardButton(
                        text=f"📹 Отправить {label}",
                        callback_data=f"item:video:{item.id}:{source.id}",
                    )
                ]
            )
    source_urls = []
    if sources is not None:
        source_urls = list(
            dict.fromkeys(source.source_url for source in sources if source.source_url)
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
    failed_sources = [source for source in sources or () if source.extraction_status == "FAILED"]
    has_retryable_source_failure = any(not source.failure_is_permanent for source in failed_sources)
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
