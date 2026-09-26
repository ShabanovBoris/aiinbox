from collections.abc import Mapping, Sequence

from app.bot.presentation import item_display_title, item_navigation_entries, item_type_label
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


def _summary_preview(item: Item, limit: int) -> str:
    """Keep persisted summaries readable at Telegram's presentation boundary."""
    summary = " ".join((item.summary or "").split())
    if len(summary) > limit:
        return summary[: limit - 1].rstrip() + "…"
    return summary


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
    lines = ["✓ Сохранено", "", f"🎯 {item_display_title(item, sources or ())}"]
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
    # ❌ Удалены числовой приоритет и внутренняя причина ранжирования: в Details
    # остаются метаданные, помогающие понять сохранение, а не оценку системы.
    if item.interest_level is not None:
        interest_labels = {1: "низкий", 2: "обычный", 3: "высокий"}
        interest_label = interest_labels.get(item.interest_level)
        if interest_label:
            lines.append(f"Интерес: {interest_label}")

    completeness_labels = {
        "COMPLETE": "по тексту",
        "FULL_TEXT": "по тексту",
        "TRANSCRIPT_AND_VISUAL": "по речи и кадрам",
        "TRANSCRIPT_ONLY": "по речи",
        "VISUAL_ONLY": "по кадрам",
        "CAPTION_ONLY": "по подписи",
        "PARTIAL": "частичный",
    }
    completeness_label = completeness_labels.get(item.analysis_completeness)
    if completeness_label:
        lines.append(f"Анализ: {completeness_label}")
    if item.next_action and item.next_action.strip():
        lines.extend(["", "Следующее действие:", item.next_action.strip()])
    return _fit_message(lines)


def format_item_failure(item: Item, sources: Sequence[ItemSource]) -> str:
    """Show a recognizable saved Item and explain failure without internal error codes."""
    failed_sources = [source for source in sources if source.extraction_status == "FAILED"]
    instagram_failure = next(
        (source for source in failed_sources if source.source_type is SourceType.INSTAGRAM), None
    )
    failure_source = instagram_failure or (failed_sources[0] if failed_sources else None)
    llm_failure = item.error_code in {
        "LLM_TIMEOUT",
        "LLM_RATE_LIMITED",
        "LLM_AUTH_FAILED",
        "LLM_CONFIG_FAILED",
        "LLM_FAILED",
        "INVALID_LLM_OUTPUT",
    }
    error_code = (
        item.error_code
        if llm_failure or failure_source is None
        else failure_source.error_code or item.error_code
    )
    source_type = (
        item.source_type if llm_failure or failure_source is None else failure_source.source_type
    )
    retryable = (
        item.processing_stage != "EXTRACTING"
        or not failed_sources
        or any(not source.failure_is_permanent for source in failed_sources)
    )
    reason = _failure_copy(error_code, source_type)
    if instagram_failure and error_code == "AUTH_REQUIRED":
        reason = (
            "Не удалось получить Reel: "
            f"{format_instagram_failure_reason(error_code)}. Ссылка сохранена; "
            "после настройки доступа к Instagram нажмите «Повторить»."
        )
    elif instagram_failure and error_code == "RATE_LIMITED":
        reason = (
            f"Не удалось получить Reel: {format_instagram_failure_reason(error_code)}. "
            "Ссылка сохранена; попробуйте «Повторить» позже."
        )
    elif instagram_failure and error_code == "UNSUPPORTED_SOURCE":
        reason = (
            f"Не удалось получить Reel: {format_instagram_failure_reason(error_code)}. "
            "Отправьте ссылку на конкретный Reel."
        )
    elif retryable:
        reason += "\nМожно повторить попытку."
    return f"⚠️ {item_display_title(item, sources)}\n\n{reason}"


def _failure_copy(error_code: str | None, source_type: SourceType) -> str:
    """Translate persisted technical outcomes into short user-facing explanations."""
    llm_failures = {
        "LLM_TIMEOUT",
        "LLM_RATE_LIMITED",
        "LLM_AUTH_FAILED",
        "LLM_CONFIG_FAILED",
        "LLM_FAILED",
        "INVALID_LLM_OUTPUT",
    }
    if error_code in llm_failures:
        return "Источник сохранён, но ИИ-анализ не завершился."
    if error_code == "TOO_LARGE":
        return "Файл слишком большой для обработки."
    if error_code == "UNSUPPORTED_SOURCE":
        return "Этот источник пока не удалось обработать."
    if error_code == "SECURITY_REJECTED":
        return "Ссылку не удалось безопасно открыть."
    if error_code == "TRANSCRIPTION_FAILED":
        return (
            "Не удалось распознать речь в видео."
            if source_type in {SourceType.VIDEO, SourceType.YOUTUBE}
            else "Не удалось распознать речь."
        )
    if error_code in {"DOWNLOAD_FAILED", "TIMEOUT"}:
        if source_type is SourceType.WEB:
            return "Не удалось загрузить содержимое ссылки."
        if source_type is SourceType.DOCUMENT:
            return "Не удалось обработать документ."
        if source_type is SourceType.VOICE:
            return "Не удалось получить голосовое сообщение."
        if source_type is SourceType.AUDIO:
            return "Не удалось получить аудиофайл."
        if source_type in {SourceType.VIDEO, SourceType.YOUTUBE, SourceType.INSTAGRAM}:
            return "Не удалось получить содержимое видео."
        return "Не удалось загрузить содержимое источника."
    if error_code == "EXTRACTION_FAILED" and source_type is SourceType.DOCUMENT:
        return "Не удалось обработать документ."
    return "Не удалось обработать сохранение."


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
            "Ограничения: " + "; ".join(f"{k}: {v}" for k, v in profile.constraints.items())
        )
    if profile.free_text:
        lines.append(f"Заметки: {profile.free_text}")
    if len(lines) == 1:
        lines.append("Профиль пуст — используйте /profile_update <описание>.")
    lines.append(f"Язык ответа: {profile.preferred_language}")
    return _fit_message(lines)


def format_today(
    items: list[Item],
    sources_by_item: Mapping[int, Sequence[ItemSource]] | None = None,
    *,
    heading: str = "🎯 Сегодня",
) -> str:
    """Share one concrete-content projection for /today and scheduled подборку."""
    if not items:
        return f"{heading}\n\nНа сегодня пока нечего вернуть в фокус."

    # ❌ Удалены позиции и числовые рейтинги из списка дня: человеку показываются
    # конкретные сохранения и следующий полезный шаг, а порядок остаётся у TodayService.
    lines = [heading]
    entries = item_navigation_entries(items, sources_by_item)
    for item, entry in zip(items, entries, strict=True):
        lines.extend(["", f"🎯 {entry.label}"])
        action = item.next_action.strip() if item.next_action and item.next_action.strip() else ""
        content = action or _summary_preview(item, 220)
        if content:
            lines.append(content)
        if item.estimated_action_minutes is not None:
            lines.append(f"≈ {item.estimated_action_minutes} минут")
    return _fit_message(lines)


def format_weekly_review(
    review: WeeklyReview, item_titles_by_id: Mapping[int, str] | None = None
) -> str:
    """Show only concrete saved materials from the existing read-only recommendations."""
    if not review.recommendations:
        return "📌 На этой неделе пока нечего отдельно возвращать в фокус."

    # ❌ Удалены недельные счётчики и категории из Telegram-проекции: аналитика
    # остаётся в WeeklyReview, а человек видит только связанные с ним материалы.
    lines = ["📌 Вернуться на этой неделе"]
    descriptions = {
        "RETURN_OLD_IMPORTANT": "Вернуться к материалу.",
        "QUICK_WIN": "Короткий следующий шаг.",
        "CLEANUP_REVIEW": "Проверить, ещё актуален ли он.",
    }
    for recommendation in review.recommendations[:3]:
        title = _bounded_weekly_label(
            (item_titles_by_id or {}).get(recommendation.item_id)
            or recommendation.title
            or "Сохранённый материал"
        )
        lines.extend(("", f"• {title}"))
        description = descriptions.get(recommendation.kind)
        if (
            recommendation.kind == "QUICK_WIN"
            and recommendation.estimated_action_minutes is not None
        ):
            description = (
                f"{description} ≈ {recommendation.estimated_action_minutes} минут"
                if description
                else None
            )
        if description:
            lines.append(description)
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


def format_attention_item(
    index: int,
    count: int,
    item: Item,
    rank: AttentionRank,
    sources: Sequence[ItemSource] = (),
) -> str:
    """Show ranked content with a bounded persisted summary, leaving scores internal."""
    # ❌ Удалены score, возраст и ranking reason из ручной карточки: это диагностика
    # ранжирования, а preview должен помогать узнать сохранённый материал.
    title = item_display_title(item, sources)
    summary = _summary_preview(item, _ATTENTION_SUMMARY_PREVIEW_LENGTH)
    lines = [f"🎯 {title}"]
    if summary:
        lines.extend(["", summary])
    return _fit_message(lines)


def format_proactive_attention_reminder(
    item: Item,
    hook_text: str | None = None,
    sources: Sequence[ItemSource] = (),
) -> str:
    """Project an Item reminder as title plus hook, summary fallback, or title alone."""
    # ❌ Удалены wrapper, ranking reason, scores, interest and age from reminder copy:
    # они объясняли выбор планировщика вместо содержательной причины открыть материал.
    lines = [f"🎯 {item_display_title(item, sources)}"]
    content = (hook_text or "").strip()
    if not content:
        # Item.summary is presentation fallback only; AttentionHookService never reads it.
        content = _summary_preview(item, _PROACTIVE_SUMMARY_PREVIEW_LENGTH)
    if content:
        lines.extend(("", content))
    return _fit_message(lines)


def format_snooze_reminder(item: Item, sources: Sequence[ItemSource] = ()) -> str:
    """Return a snoozed save with its title and persisted summary beside source actions."""
    lines = ["⏰ Вы хотели вернуться к этому:", "", item_display_title(item, sources)]
    summary = _summary_preview(item, _PROACTIVE_SUMMARY_PREVIEW_LENGTH)
    if summary:
        lines.extend(("", summary))
    return _fit_message(lines)


def format_item_list(
    items: list[Item],
    heading: str,
    sources_by_item: Mapping[int, Sequence[ItemSource]] | None = None,
) -> str:
    """Render the same bounded title projections used as full-width list buttons."""
    if not items:
        return f"{heading}\n\nНичего не найдено."
    lines = [heading]
    entries = item_navigation_entries(items, sources_by_item)
    # ❌ Удалены числовые оценки из списков: пользователь выбирает материал по названию.
    for entry in entries:
        lines.append(f"• {entry.label}")
    return _fit_message(lines)


def format_ask_answer(answer: str, references: Sequence[AskReference]) -> str:
    """Render one bounded synthesis with titles supplied by validated persisted rows."""
    answer = answer.strip()
    lines = [answer or "В найденных материалах недостаточно данных для уверенного ответа."]
    if references:
        lines.extend(("", "Источники:"))
        source_labels = {
            SourceType.TEXT: "заметка",
            SourceType.WEB: "ссылка",
            SourceType.VOICE: "голосовое сообщение",
            SourceType.AUDIO: "аудио",
            SourceType.YOUTUBE: "YouTube",
            SourceType.INSTAGRAM: "Instagram",
            SourceType.VIDEO: "видео",
            SourceType.DOCUMENT: "документ",
        }
        for index, reference in enumerate(references[:5], start=1):
            title = _bounded_weekly_label(reference.title or "Сохранение", 120)
            if reference.source_type:
                try:
                    source_type = SourceType(reference.source_type)
                except ValueError:
                    source_type = None
                source_label = source_labels.get(source_type) if source_type else None
                if source_label:
                    title += f" — {source_label}"
            lines.append(f"[{index}] {title}")
    return _fit_message(lines)


def format_categories(categories: Sequence[tuple[str, int]], *, page: int = 0) -> str:
    """Format one category page while the keyboard carries the bounded choices."""
    if not categories:
        return "Категорий пока нет."
    # ❌ Удалены внутренние числа материалов и номер страницы из списка категорий.
    return _fit_message(["🏷 Категории"] + [name for name, _count in categories])
