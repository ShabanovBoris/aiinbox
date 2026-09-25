from collections.abc import Sequence

from app.bot.provenance import forward_source_label
from app.domain.enums import SourceType
from app.domain.models import AskReference, UserProfile
from app.services.attention_ranking import AttentionRank
from app.services.weekly_review import WeeklyReview
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


def _bounded_weekly_label(value: str, limit: int = 120) -> str:
    """Keep user-authored/model-derived labels on one line within report space."""
    normalized = " ".join(value.split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 1] + "…"


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


def format_weekly_review(review: WeeklyReview) -> str:
    """Render the read model as one compact message, keeping actions ahead of themes."""
    flow = review.flow
    backlog = review.backlog
    reminders = review.reminder_outcomes
    has_data = any(
        (
            flow.created,
            flow.completed,
            flow.archived,
            backlog.active_actionable,
            backlog.high_priority,
            backlog.high_interest,
            backlog.stale,
            backlog.old_important_unrevisited,
            bool(review.created_categories),
            bool(review.completed_categories),
            review.most_postponed is not None,
            review.strongest_progress is not None,
            reminders is not None and reminders.has_activity,
            bool(review.recommendations),
        )
    )
    if not has_data:
        return "Неделя\n\nПока недостаточно данных для недельного обзора."

    lines = ["📊 Неделя"]
    has_flow = any((flow.created, flow.completed, flow.archived))
    if has_flow:
        lines.append("")
        if flow.created:
            lines.append(f"Добавлено: {flow.created}")
        if flow.completed:
            lines.append(f"Готово: {flow.completed}")
        if flow.archived:
            lines.append(f"Архивировано: {flow.archived}")
        if flow.net_change > 0:
            lines.append(f"Backlog вырос на {flow.net_change}")
        elif flow.net_change < 0:
            lines.append(f"Backlog сократился на {abs(flow.net_change)}")
        else:
            lines.append("Поток примерно сбалансирован")

    if (
        any(
            (
                backlog.active_actionable,
                backlog.high_priority,
                backlog.high_interest,
            )
        )
        or has_flow
    ):
        lines.extend(("", "Сейчас:"))
        lines.append(f"Активных actionable: {backlog.active_actionable}")
        if backlog.high_priority:
            lines.append(f"Высокий приоритет: {backlog.high_priority}")
        if backlog.high_interest:
            lines.append(f"Интерес 3/3: {backlog.high_interest}")

    if backlog.stale or backlog.old_important_unrevisited:
        lines.extend(("", "Старый backlog:"))
        if backlog.stale:
            lines.append(f"{backlog.stale} Item старше 30 дней")
        if backlog.old_important_unrevisited:
            lines.append(
                f"{backlog.old_important_unrevisited} важных давно не возвращались в фокус"
            )

    if review.recommendations:
        lines.extend(("", "На следующую неделю:"))
        for index, recommendation in enumerate(review.recommendations[:3], start=1):
            title = _bounded_weekly_label(recommendation.title)
            if recommendation.kind == "RETURN_OLD_IMPORTANT":
                lines.append(f"{index}. Вернуться: {title}")
            elif recommendation.kind == "QUICK_WIN":
                estimate = (
                    f" (~{recommendation.estimated_action_minutes} мин)"
                    if recommendation.estimated_action_minutes is not None
                    else ""
                )
                lines.append(f"{index}. Быстрый шаг: {title}{estimate}")
            elif recommendation.kind == "CLEANUP_REVIEW":
                lines.append(f"{index}. Проверить актуальность: {title}")

    if reminders is not None and reminders.has_activity:
        lines.extend(("", "Attention:"))
        reminder_labels = (
            ("отправлено", reminders.sent),
            ("открыто", reminders.opened),
            ("отложено", reminders.snoozed),
            ("готово", reminders.done),
            ("не сейчас", reminders.dismissed),
            ("меньше таких", reminders.disliked),
        )
        lines.append(" · ".join(f"{label} {count}" for label, count in reminder_labels if count))

    if review.created_categories:
        lines.extend(("", "Чаще добавлял:"))
        lines.extend(
            f"{_bounded_weekly_label(category.category)} — {category.count}"
            for category in review.created_categories[:3]
        )

    completed_categories = list(review.completed_categories[:3])
    progress = review.strongest_progress
    if progress is not None:
        progress_category = _bounded_weekly_label(progress.category)
        progress_line = f"Больше всего завершений: {progress_category} — {progress.count}"
        lines.extend(("", progress_line))
        completed_categories = [
            category for category in completed_categories if category.category != progress.category
        ]
    if completed_categories:
        lines.extend(("", "Темы завершений:"))
        lines.extend(
            f"{_bounded_weekly_label(category.category)} — {category.count}"
            for category in completed_categories
        )

    if review.most_postponed is not None:
        postponed = review.most_postponed
        lines.extend(
            (
                "",
                f"Чаще откладывал: {_bounded_weekly_label(postponed.category)} — {postponed.count}",
            )
        )

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


def format_ask_answer(answer: str, references: Sequence[AskReference]) -> str:
    """Render one bounded synthesis with titles supplied by validated persisted rows."""
    answer = answer.strip()
    lines = [answer or "В найденных материалах недостаточно данных для уверенного ответа."]
    if references:
        lines.extend(("", "Источники:"))
        for index, reference in enumerate(references[:5], start=1):
            title = _bounded_weekly_label(reference.title or "Без названия", 120)
            if reference.source_type:
                title += f" — {reference.source_type[:16]}"
            lines.append(f"[{index}] {title}")
    return _fit_message(lines)


def format_categories(categories: list[tuple[str, int]]) -> str:
    """Форматирует список категорий; выбор категории остаётся текстовой командой."""
    if not categories:
        return "Категории пока пусты."
    return _fit_message(["Категории:"] + [f"{name} — {count}" for name, count in categories])
