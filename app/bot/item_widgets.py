import json
import logging
import zlib
from asyncio import timeout
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from textwrap import wrap
from typing import Literal

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramNotFound
from aiogram.methods.base import TelegramMethod
from aiogram.types import Chat, InaccessibleMessage, InlineKeyboardMarkup, Message
from aiogram.types import User as TelegramUser
from pydantic import BaseModel, Field, ValidationError

from app.bot.keyboards import plain_button_label
from app.bot.presentation import ItemNavigationEntry
from app.domain.enums import ItemState
from app.services.retrieval import ItemPage

log = logging.getLogger(__name__)
INBOX_ARCHIVED_NOTICE = "В архиве"


# These methods use aiogram's normal session while its generated API is still at 10.3.
class SendWidget(TelegramMethod[Message]):
    __api_method__ = "sendWidget"
    __returning__ = Message

    chat_id: int
    html: str
    allow_queries: bool = True
    message_thread_id: int | None = None


class AnswerWidgetQuery(TelegramMethod[bool]):
    __api_method__ = "answerWidgetQuery"
    __returning__ = bool

    widget_query_id: str
    data: str


class EditMessageWidgetData(TelegramMethod[bool]):
    __api_method__ = "editMessageWidgetData"
    __returning__ = bool

    chat_id: int
    message_id: int
    chat_data: str


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _short_label(text: str) -> str:
    if len(_json(text).encode()) <= 88:
        return text
    return f"{text[:12]}…{text[-7:]}"


def _fits_widget_body(*blocks: str) -> bool:
    # Telegram clips at 600 px. Allow room for the status, actions and timestamp at phone width.
    lines = sum(
        max(1, len(wrap(line, width=28))) for block in blocks for line in block.splitlines()
    )
    return lines <= 14


def _widget_rows(markup: InlineKeyboardMarkup | None) -> list:
    rows = []
    for row in markup.inline_keyboard if markup else ():
        buttons = []
        for button in row:
            value = button.callback_data or button.url
            if not value:
                continue
            label = button.text
            if not value.startswith("feedback:category:"):
                label = plain_button_label(label)
            if value.startswith("item:interest:") and label.endswith(" ✓"):
                label = f"{label.removesuffix(' ✓')} (выбран)"
            if value.startswith(("item:back:", "item:more:", "feedback:back:")):
                value = "@card"
            elif value.startswith("item:sources:"):
                value = "@sources"
            elif value == "nav:inbox:page:0":
                value = "nav:inbox"
            buttons.append([_short_label(label), value])
        if buttons:
            rows.append(buttons)
    return rows


def inbox_widget_screen(
    item_page: ItemPage, entries: Sequence[ItemNavigationEntry], *, archived: bool
) -> dict:
    rows = [
        [
            str(entry.item_id),
            entry.label,
            item.state is not ItemState.ARCHIVED,
            item.telegram_message_id is not None,
        ]
        for item, entry in zip(item_page.items, entries, strict=True)
    ]
    page = {
        "number": item_page.page,
        "previous": item_page.has_previous,
        "next": item_page.has_next,
        "archived": archived,
        "rows": rows,
    }
    screen = {"inbox": page}
    size = len(_json({"screen": screen, "notice": INBOX_ARCHIVED_NOTICE}).encode())
    if size > 1024 and rows:
        # Budget against the callback response, which is larger than persisted chat_data.
        labels_size = sum(len(_json(row[1]).encode()) for row in rows)
        quota = (labels_size + 1024 - size) // len(rows)
        for row in rows:
            label = row[1]
            if len(_json(label).encode()) <= quota:
                continue
            while label and len(_json(f"{label}…").encode()) > quota:
                label = label[:-1]
            row[1] = label.rstrip() + "…"
    return screen


async def try_send_inbox_widget(message: Message | InaccessibleMessage, screen: dict) -> bool:
    template = Path(__file__).with_name("inbox_widget.html").read_text(encoding="utf-8")
    page = screen["inbox"]
    data = _json(page).replace("<", "\\u003c")
    html = (
        template.replace("__ROW_COUNT__", str(len(page["rows"])), 1)
        .replace("__PAGER_DISPLAY__", "flex" if page["previous"] or page["next"] else "none", 1)
        .replace("__PREVIOUS_VISIBILITY__", "visible" if page["previous"] else "hidden", 1)
        .replace("__NEXT_VISIBILITY__", "visible" if page["next"] else "hidden", 1)
        .replace("__INBOX_DATA__", data, 1)
    )
    return await _try_send_widget_html(
        message.bot,
        message.chat.id,
        html,
        message_thread_id=(
            message.message_thread_id
            if isinstance(message, Message) and message.is_topic_message
            else None
        ),
    )


async def _try_send_widget_html(
    bot: Bot,
    chat_id: int,
    html: str,
    *,
    message_thread_id: int | None = None,
) -> bool:
    encoded = html.encode()
    if len(encoded) > 32_768 or len(zlib.compress(encoded, 9)) > 4096:
        return False
    try:
        await bot(
            SendWidget(
                chat_id=chat_id,
                html=html,
                message_thread_id=message_thread_id,
            )
        )
    except TelegramNotFound as exc:
        if exc.message.casefold() != "not found":
            raise
        log.info("Bot API server does not support sendWidget")
        return False
    return True


class _DataQuery(BaseModel):
    type: Literal["data"]


class _WidgetQuery(BaseModel):
    id: str = Field(min_length=1, max_length=128)
    from_user: TelegramUser = Field(alias="from")
    chat: Chat
    message_id: int = Field(gt=0)
    type: _DataQuery | None = None
    data: str


@dataclass
class WidgetMessage:
    target: InaccessibleMessage
    screen: dict | None = None

    @property
    def chat(self) -> Chat:
        return self.target.chat

    async def edit_reply_markup(self, *, reply_markup: InlineKeyboardMarkup | None) -> None:
        self.screen = {"rows": _widget_rows(reply_markup)}

    async def edit_text(self, text: str, *, reply_markup: InlineKeyboardMarkup | None) -> None:
        self.screen = {"text": plain_button_label(text), "rows": _widget_rows(reply_markup)}

    async def answer(self, text: str, *, reply_markup: InlineKeyboardMarkup | None) -> None:
        # The Item handler offers the saved sources when its original Telegram message is gone.
        if reply_markup:
            self.screen = {"text": text, "sources": True}
        else:
            self.screen = {"text": text, "rows": [[["Назад", "@card"]]]}


@dataclass
class WidgetCallback:
    id: str
    from_user: TelegramUser
    data: str
    message: WidgetMessage
    bot: Bot
    notice: str | None = None

    async def answer(self, text: str | None = None) -> None:
        self.notice = plain_button_label(text) if text is not None else None


async def _answer(bot: Bot, query_id: str, payload: dict) -> None:
    await bot(AnswerWidgetQuery(widget_query_id=query_id, data=_json(payload)), request_timeout=4)


async def _persist_widget_screen(message: InaccessibleMessage, screen: dict) -> None:
    try:
        await message.bot(
            EditMessageWidgetData(
                chat_id=message.chat.id, message_id=message.message_id, chat_data=_json(screen)
            ),
            request_timeout=4,
        )
    except TelegramAPIError:
        log.warning("Could not persist widget screen message_id=%s", message.message_id)


def widget_update_middleware(handle_action):
    async def handle_update(handler, update, data):
        raw = (update.model_extra or {}).get("widget_query")
        if raw is None:
            return await handler(update, data)
        bot = data["bot"]
        try:
            query = _WidgetQuery.model_validate(raw)
        except ValidationError:
            if isinstance(raw, dict) and isinstance(raw.get("id"), str):
                await _answer(bot, raw["id"], {"notice": "Действие недоступно"})
            return None
        if (
            query.chat.type != "private"
            or query.from_user.id != query.chat.id
            or not 1 <= len(query.data.encode()) <= 64
        ):
            await _answer(bot, query.id, {"notice": "Действие недоступно"})
            return None

        message = InaccessibleMessage(chat=query.chat, message_id=query.message_id).as_(bot)
        callback = WidgetCallback(
            f"widget:{query.id}", query.from_user, query.data, WidgetMessage(message), bot
        )
        fsm = data["dispatcher"].fsm
        state = fsm.get_context(bot=bot, chat_id=query.chat.id, user_id=query.from_user.id)
        try:
            async with timeout(10):
                async with fsm.events_isolation.lock(key=state.key):
                    if await state.get_state() is not None:
                        await state.clear()
                    await handle_action(callback)
                    screen = callback.message.screen
                    if screen is not None and "inbox" in screen:
                        # Serialize list page changes before accepting another tap on the same chat.
                        await _persist_widget_screen(message, screen)
        except TimeoutError:
            await _answer(
                bot, query.id, {"notice": "Действие заняло много времени. Проверь карточку."}
            )
            return None
        except Exception:
            await _answer(bot, query.id, {"notice": "Не удалось выполнить действие"})
            raise

        screen = callback.message.screen
        result = {"screen": screen, "notice": callback.notice}
        overflow = None
        if screen is not None and (
            len(_json(result).encode()) > 1024 or not _fits_widget_body(screen.get("text", ""))
        ):
            overflow = screen.pop("text", None)
            if overflow:
                screen["text"] = "Подробности в следующем сообщении."
        if len(_json(result).encode()) > 1024:
            log.error("Widget action response exceeded Telegram's size limit")
            await _answer(bot, query.id, {"notice": "Эта страница недоступна в виджете"})
            return None
        await _answer(bot, query.id, result)
        if overflow:
            await message.answer(overflow)
        namespace, _, suffix = query.data.partition(":")
        action = suffix.split(":", 1)[0]
        if (
            screen is not None
            and namespace == "item"
            and action in {"done", "archive", "snooze", "cancel", "retry"}
        ):
            await _persist_widget_screen(message, screen)
        return None

    return handle_update
