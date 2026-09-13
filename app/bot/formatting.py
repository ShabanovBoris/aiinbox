from app.domain.models import UserProfile
from app.storage.models import Item


def format_ready_item(item: Item) -> str:
    """Компактный результат анализа для Telegram (PRODUCT_SPEC §14): без перегруза."""
    lines = ["✓ Сохранено", "", f"🎯 {item.title}"]
    lines.append(f"Категория: {item.category}")
    lines.append(f"Тип: {item.item_type.value if item.item_type else '—'}")
    if item.priority_score is not None:
        lines.append(f"Приоритет: {item.priority_score}/100")
    if item.summary:
        lines.append("")
        lines.append(item.summary)
    if item.next_action:
        lines.append("")
        lines.append(f"Следующее действие: {item.next_action}")
    if item.priority_reason:
        lines.append("")
        lines.append(f"Почему: {item.priority_reason}")
    return "\n".join(lines)


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
    if profile.free_text:
        lines.append(f"Заметки: {profile.free_text}")
    if len(lines) == 1:
        lines.append("Профиль пуст — используйте /profile_update <описание>.")
    return "\n".join(lines)
