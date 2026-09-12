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
