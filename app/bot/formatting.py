from collections.abc import Sequence

from app.bot.presentation import item_type_label
from app.domain.enums import SourceType
from app.domain.models import AskReference, UserProfile
from app.services.attention_ranking import AttentionRank
from app.services.weekly_review import WeeklyReview
from app.storage.models import Item, ItemSource

_TELEGRAM_MAX_MESSAGE_LENGTH = 4096
_ATTENTION_SUMMARY_PREVIEW_LENGTH = 520
_PROACTIVE_SUMMARY_PREVIEW_LENGTH = 700


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


def _analysis_completeness_warning(
    item: Item, sources: Sequence[ItemSource] | None = None
) -> list[str]:
    """Keep only trust-relevant extraction limits beside the user's saved summary."""
    completeness = item.analysis_completeness
    if completeness == "VISUAL_ONLY":
        return ["⚠️ Анализ только по кадрам — транскрипт недоступен."]
    if completeness == "CAPTION_ONLY":
        return ["⚠️ Анализ только по подписи — речь не распознана."]
    if completeness == "TRANSCRIPT_ONLY" and (
        item.source_type in (SourceType.YOUTUBE, SourceType.VIDEO, SourceType.INSTAGRAM)
        or any(
            source.source_type in (SourceType.YOUTUBE, SourceType.VIDEO, SourceType.INSTAGRAM)
            for source in sources or ()
        )
    ):
        return ["⚠️ Анализ по транскрипту — без визуальной части."]
    if completeness != "PARTIAL":
        return []

    warnings = ["⚠️ Анализ частичный — часть источников не обработана."]
    instagram_failure = next(
        (
            source
            for source in sources or ()
            if source.source_type is SourceType.INSTAGRAM and source.extraction_status == "FAILED"
        ),
        None,
    )
    if instagram_failure:
        reason = format_instagram_failure_reason(instagram_failure.error_code)
        warnings.append(f"Reel: {reason}.")
    return warnings


def format_ready_item_compact(item: Item, sources: Sequence[ItemSource] | None = None) -> str:
    """Project a READY Item as saved content plus only material trust warnings."""
    # ❌ Удалены строки категории, типа, рейтинга, интереса и внутренних пояснений:
    # эти canonical metadata доступны по запросу через Details и не заслоняют summary.
    lines = ["✓ Сохранено", "", f"🎯 {item.title or 'Без названия'}"]
    if item.summary and item.summary.strip():
        lines.extend(["", item.summary.strip()])
    warnings = _analysis_completeness_warning(item, sources)
    if warnings:
        lines.extend(["", *warnings])
    return _fit_message(lines)


def format_ready_item(item: Item, sources: Sequence[ItemSource] | None = None) -> str:
    """Compatibility name for callers that still use the pre-compact formatter API."""
    return format_ready_item_compact(item, sources)


def format_item_details(item: Item) -> str:
    """Render canonical system metadata only when the user explicitly opens Details."""
    lines = ["ℹ️ Детали"]
    if item.category and item.category.strip():
        lines.append(f"Категория: {item.category.strip()}")
    type_label = item_type_label(item.item_type)
    if type_label:
        lines.append(f"Тип: {type_label}")
    if item.priority_score is not None:
        lines.append(f"Приоритет: {item.priority_score}/100")
    if item.interest_level is not None:
        lines.append(f"Интерес: {item.interest_level}/3")

    completeness_labels = {
        "COMPLETE": "полный",
        "FULL_TEXT": "текст",
        "TRANSCRIPT_AND_VISUAL": "транскрипт и кадры",
        "TRANSCRIPT_ONLY": "только транскрипт",
        "VISUAL_ONLY": "только кадры",
        "CAPTION_ONLY": "только подпись",
        "PARTIAL": "частичный",
    }
    completeness_label = completeness_labels.get(item.analysis_completeness)
    if completeness_label:
        lines.append(f"Полнота анализа: {completeness_label}")
    if item.next_action and item.next_action.strip():
        lines.extend(["", "Следующее действие:", item.next_action.strip()])
    if item.priority_reason and item.priority_reason.strip():
        lines.extend(["", "Почему приоритет:", item.priority_reason.strip()])
    return _fit_message(lines)


def format_item_failure(item: Item, sources: Sequence[ItemSource]) -> str:
    """Rebuild the existing FAILED delivery text when a submenu returns to its card."""
    failed_sources = [source for source in sources if source.extraction_status == "FAILED"]
    instagram_failure = next(
        (source for source in failed_sources if source.source_type is SourceType.INSTAGRAM), None
    )
    error_code = instagram_failure.error_code if instagram_failure else item.error_code
    retryable = (
        item.processing_stage != "EXTRACTING"
        or not failed_sources
        or any(not source.failure_is_permanent for source in failed_sources)
    )
    if instagram_failure and error_code == "AUTH_REQUIRED":
        return (
            "Не удалось получить Reel: "
            f"{format_instagram_failure_reason(error_code)}. Ссылка сохранена; "
            "после настройки INSTAGRAM_COOKIES_FILE нажмите Retry."
        )
    if instagram_failure and error_code == "RATE_LIMITED":
        return (
            f"Не удалось получить Reel: {format_instagram_failure_reason(error_code)}. "
            "Ссылка сохранена; попробуйте Retry позже."
        )
    if instagram_failure and error_code == "UNSUPPORTED_SOURCE":
        return (
            f"Не удалось получить Reel: {format_instagram_failure_reason(error_code)}. "
            "Отправьте ссылку на конкретный Reel."
        )
    message = f"Не удалось обработать Item ({item.error_code or 'ошибка'})."
    if retryable:
        return (
            f"Не удалось обработать Item. Можно повторить попытку ({item.error_code or 'ошибка'})."
        )
    return message


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
    """Show ranked content with a bounded persisted summary, leaving scores internal."""
    # ❌ Удалены score, возраст и ranking reason из ручной карточки: это диагностика
    # ранжирования, а preview должен помогать узнать сохранённый материал.
    title = item.title or "Без названия"
    summary = " ".join((item.summary or "").split())
    if len(summary) > _ATTENTION_SUMMARY_PREVIEW_LENGTH:
        summary = summary[: _ATTENTION_SUMMARY_PREVIEW_LENGTH - 1].rstrip() + "…"
    lines = [f"{index}/{count} — {title}"]
    if summary:
        lines.extend(["", summary])
    return _fit_message(lines)


def format_proactive_attention_reminder(item: Item, hook_text: str | None = None) -> str:
    """Project an Item reminder as title plus hook, summary fallback, or title alone."""
    # ❌ Удалены wrapper, ranking reason, scores, interest and age from reminder copy:
    # они объясняли выбор планировщика вместо содержательной причины открыть материал.
    lines = [f"🎯 {item.title or 'Без названия'}"]
    content = (hook_text or "").strip()
    if not content:
        # Item.summary is presentation fallback only; AttentionHookService never reads it.
        content = " ".join((item.summary or "").split())
        if len(content) > _PROACTIVE_SUMMARY_PREVIEW_LENGTH:
            content = content[: _PROACTIVE_SUMMARY_PREVIEW_LENGTH - 1].rstrip() + "…"
    if content:
        lines.extend(("", content))
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
