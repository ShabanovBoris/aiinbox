from collections.abc import Sequence

from app.bot.provenance import forward_source_label
from app.domain.enums import SourceType
from app.domain.models import UserProfile
from app.services.attention_ranking import AttentionRank
from app.storage.models import Item, ItemSource

_TELEGRAM_MAX_MESSAGE_LENGTH = 4096


def format_instagram_failure_reason(error_code: str | None) -> str:
    """Project an Instagram source error into concise user-facing Russian copy.

    ItemSource remains the canonical owner of the failure code; this shared
    projection keeps both partial-result and all-failed Telegram messages aligned.
    """
    return {
        "AUTH_REQUIRED": "доступ требует авторизации Instagram",
        "RATE_LIMITED": "обработка временно ограничена Instagram",
        "UNSUPPORTED_SOURCE": "ссылка на этот Reel не поддерживается",
    }.get(error_code or "", "обработать Reel не удалось")


def _fit_message(lines: list[str]) -> str:
    """Ограничивает выдачу Telegram без отдельного pagination framework."""
    output: list[str] = []
    used = 0
    for line in lines:
        separator = 1 if output else 0
        available = _TELEGRAM_MAX_MESSAGE_LENGTH - used - separator
        if available <= 0:
            break
        if len(line) > available:
            if available == 1:
                output.append("…")
            else:
                output.append(line[: available - 1] + "…")
            break
        output.append(line)
        used += separator + len(line)
    return "\n".join(output)


def format_ready_item(item: Item, sources: Sequence[ItemSource] | None = None) -> str:
    """Компактный результат анализа для Telegram (PRODUCT_SPEC §14): без перегруза."""
    lines = ["✓ Сохранено", "", f"🎯 {item.title}"]
    lines.append(f"Категория: {item.category}")
    lines.append(f"Тип: {item.item_type.value if item.item_type else '—'}")
    if item.priority_score is not None:
        lines.append(f"Приоритет: {item.priority_score}/100")
    lines.append(f"Интерес: {item.interest_level}/3")
    source_label = forward_source_label(item.source_metadata_json)
    if source_label:
        lines.append(f"Источник: {source_label}")
    if item.analysis_completeness == "VISUAL_ONLY":
        lines.append("Анализ: только по визуальным кадрам — транскрипт недоступен")
    elif item.analysis_completeness == "CAPTION_ONLY":
        lines.append("Анализ: только по подписи Reel — транскрипт речи недоступен")
    elif (
        item.source_type in (SourceType.YOUTUBE, SourceType.VIDEO, SourceType.INSTAGRAM)
        or any(
            source.source_type in (SourceType.YOUTUBE, SourceType.VIDEO, SourceType.INSTAGRAM)
            for source in sources or ()
        )
    ) and item.analysis_completeness == "TRANSCRIPT_ONLY":
        lines.append("Анализ: по транскрипту, без визуальной части")
    elif item.analysis_completeness == "PARTIAL":
        lines.append("Анализ: частичный — не весь вложенный контент удалось обработать")
        instagram_failure = next(
            (
                source
                for source in sources or ()
                if source.source_type is SourceType.INSTAGRAM
                and source.extraction_status == "FAILED"
            ),
            None,
        )
        if instagram_failure:
            reason = format_instagram_failure_reason(instagram_failure.error_code)
            lines.append(f"Reel: {reason}.")
    if item.summary:
        lines.append("")
        lines.append(item.summary)
    if item.next_action:
        lines.append("")
        lines.append(f"Следующее действие: {item.next_action}")
    if item.priority_reason:
        lines.append("")
        lines.append(f"Почему: {item.priority_reason}")
    return _fit_message(lines)


def format_profile(profile: UserProfile) -> str:
    """Компактный показ профиля для /profile."""
    lines = ["👤 Профиль"]
    if profile.profession:
        lines.append(f"Профессия: {profile.profession}")
    if profile.domains:
        lines.append(f"Домены: {', '.join(profile.domains)}")
    if profile.goals:
        lines.append("Цели: " + "; ".join(f"{g.name} ({g.weight})" for g in profile.goals))
    if profile.interests:
        lines.append(f"Интересы: {', '.join(profile.interests)}")
    if profile.constraints:
        lines.append(
            "Constraints: " + "; ".join(f"{k}: {v}" for k, v in profile.constraints.items())
        )
    if profile.free_text:
        lines.append(f"Заметки: {profile.free_text}")
    if len(lines) == 1:
        lines.append("Профиль пуст — используйте /profile_update <описание>.")
    lines.append(f"Язык ответа: {profile.preferred_language}")
    return _fit_message(lines)


def format_today(items: list[Item]) -> str:
    """Показывает actionable-срез с полями, нужными для решения «что делать»."""
    if not items:
        return "Сегодня нет подходящих задач."
    lines = ["Сегодня:"]
    for index, item in enumerate(items, start=1):
        lines.append(f"{index}. {item.title or 'Без названия'} — {item.priority_score or 0}/100")
        if item.estimated_action_minutes is not None:
            lines.append(f"   ~{item.estimated_action_minutes} мин")
        if item.next_action:
            lines.append(f"   {item.next_action}")
    return _fit_message(lines)


def format_attention_reason(rank: AttentionRank) -> str:
    """Explain only persisted ranking signals; generated prose never invents context."""
    reasons = []
    if rank.due_bonus:
        reasons.append("Срок прошёл" if rank.due_bonus == 10 else "Срок близко")
    if rank.stale_important_bonus:
        reasons.append("Важный Item давно не возвращался")
    elif rank.neglect_bonus:
        if rank.last_shown_at is None:
            reasons.append("Ещё не показывался")
        else:
            days = int(rank.days_since_shown or 0)
            reasons.append(f"Не показывался {days} дн.")
    if rank.priority_score >= 75:
        reasons.append("Высокий приоритет")
    if rank.interest_adjustment > 0:
        reasons.append("Высокий интерес")
    elif rank.interest_adjustment < 0:
        reasons.append("Низкий интерес")
    if rank.behaviour_rank.adjustment_points > 0:
        reasons.append("Подходит по вашим реакциям")
    elif rank.age_bonus > 0 and not rank.neglect_bonus:
        reasons.append("Давно сохранён")
    if rank.recent_show_penalty < 0:
        reasons.append("Показан недавно")
    return "; ".join(reasons[:3]) or "По рассчитанному рейтингу"


def format_attention_item(index: int, count: int, item: Item, rank: AttentionRank) -> str:
    """Project one ranked Item into its own Telegram card, preserving room for actions."""
    lines = [
        f"{index}/{count} — {item.title or 'Без названия'}",
        f"Внимание: {rank.score}/100",
        f"Приоритет: {rank.priority_score}/100",
        f"Интерес: {item.interest_level}/3",
        f"Возраст: {int(rank.age_days)} дн.",
        "",
        f"Почему сейчас: {format_attention_reason(rank)}",
    ]
    return _fit_message(lines)


def format_proactive_attention_reminder(
    item: Item, rank: AttentionRank, hook_block: str | None = None
) -> str:
    """Render a scheduled Item with PM-07's existing explainability signals.

    Keeping this as a presentation projection prevents PM-08 from inventing a
    second reason formula or copying the Item's saved content into Reminder data.
    """
    lines = [
        "⏳ Вернём это в фокус",
        item.title or "Без названия",
    ]
    # ❌ Удалён жёстко заданный блок без hook: он мешал вставить проверенный
    # контекстный блок; fallback по-прежнему собирает тот же текст ниже.
    if hook_block:
        lines.extend(["", hook_block])
    lines.extend(
        [
            "",
            f"Почему сейчас: {format_attention_reason(rank)}",
            "",
            f"Внимание: {rank.score}/100",
            f"Приоритет: {rank.priority_score}/100",
            f"Интерес: {item.interest_level}/3",
            f"Сохранён: {int(rank.age_days)} дн. назад",
        ]
    )
    return _fit_message(lines)


def format_item_list(items: list[Item], heading: str) -> str:
    """Общий компактный список для inbox/category/search."""
    if not items:
        return f"{heading}\n\nНичего не найдено."
    lines = [heading]
    for index, item in enumerate(items, start=1):
        score = f" — {item.priority_score}/100" if item.priority_score is not None else ""
        lines.append(f"{index}. {item.title or 'Без названия'}{score}")
    return _fit_message(lines)


def format_categories(categories: list[tuple[str, int]]) -> str:
    """Форматирует список категорий; выбор категории остаётся текстовой командой."""
    if not categories:
        return "Категории пока пусты."
    return _fit_message(["Категории:"] + [f"{name} — {count}" for name, count in categories])
