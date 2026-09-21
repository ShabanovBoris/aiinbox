from app.domain.enums import SourceType
from app.domain.models import UserProfile
from app.storage.models import Item

_TELEGRAM_MAX_MESSAGE_LENGTH = 4096


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


def format_ready_item(item: Item) -> str:
    """Компактный результат анализа для Telegram (PRODUCT_SPEC §14): без перегруза."""
    lines = ["✓ Сохранено", "", f"🎯 {item.title}"]
    lines.append(f"Категория: {item.category}")
    lines.append(f"Тип: {item.item_type.value if item.item_type else '—'}")
    if item.priority_score is not None:
        lines.append(f"Приоритет: {item.priority_score}/100")
    if item.source_type is SourceType.YOUTUBE and item.analysis_completeness == "TRANSCRIPT_ONLY":
        lines.append("Анализ: по транскрипту, без визуальной части")
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
