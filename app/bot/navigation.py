"""Static Telegram command discovery, kept outside domain and persistence code."""

import logging

from aiogram.types import BotCommand

log = logging.getLogger(__name__)

# This tuple is the bot's code-owned discovery surface; callbacks remain the
# interactive navigation layer so no persistent reply keyboard is needed.
BOT_COMMANDS = (
    BotCommand(command="start", description="Открыть AIInbox"),
    BotCommand(command="menu", description="Главное меню"),
    BotCommand(command="today", description="Что сделать сегодня"),
    BotCommand(command="attention", description="Что вернуть в фокус"),
    BotCommand(command="inbox", description="Последние сохранения"),
    BotCommand(command="search", description="Поиск по сохранённому"),
    BotCommand(command="ask", description="Спросить свой Inbox"),
    BotCommand(command="weekly", description="Обзор недели"),
    BotCommand(command="category", description="Категории"),
    BotCommand(command="profile", description="Профиль"),
    BotCommand(command="settings", description="Настройки"),
    BotCommand(command="export", description="Экспорт данных"),
    BotCommand(command="help", description="Краткая справка"),
)


async def configure_bot_commands(bot) -> None:
    """Publish discoverability at startup without making Telegram a boot dependency."""
    try:
        await bot.set_my_commands(list(BOT_COMMANDS))
    except Exception as exc:
        # Command discovery is presentation metadata; workers and polling own
        # their normal supervision even if this best-effort Telegram call fails.
        log.warning(
            "telegram command setup failed operation=set_my_commands exception_type=%s",
            type(exc).__name__,
        )
