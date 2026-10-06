"""Edit dialogs started from the card's edit menu: 🕒 Время, ✏️ Название, #️⃣ Теги.

The button turns the card into a prompt (with [✖️ Отмена]); the next text message is the answer:
- time  -> job ``edit_time`` (the AI reads the free-form time; the processor sends the updated card);
- title -> ``items.set_title``; tags -> ``items.set_tags`` (split by spaces/commas, "-" clears them).
Afterwards the prompt turns back into the card. ``/cancel`` (commands router) or [✖️ Отмена] leave the dialog;
a prompt older than ``states.PROMPT_TTL`` lets the text through to capture instead.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, Final, NamedTuple

from aiogram import F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.enums import ChatType
from aiogram.fsm.state import State
from aiogram.types import CallbackQuery, Message

from dumpbot import clock
from dumpbot.bot import sender, views
from dumpbot.bot.cbdata import ItemCb
from dumpbot.bot.keyboards import button, markup
from dumpbot.bot.routers import common
from dumpbot.bot.states import EditStates, expired, prompt_data
from dumpbot.services import items
from dumpbot.workers import jobs

if TYPE_CHECKING:
    from aiogram.fsm.context import FSMContext
    from sqlalchemy.ext.asyncio import AsyncSession

    from dumpbot.context import AppContext
    from dumpbot.db.models import Item

log = logging.getLogger(__name__)

Change = Callable[["AsyncSession", int], Awaitable["Item"]]

FOOTER: Final = "\n<i>/cancel — отмена</i>"
CLEAR_TAGS: Final = frozenset({"-", "—", "–"})
_TAG_SPLIT: Final = re.compile(r"[\s,;]+")


class Prompt(NamedTuple):
    state: State
    text: str  # {title} = escaped item title, {tags} = current tags


PROMPTS: Final[dict[str, Prompt]] = {
    "time": Prompt(
        EditStates.time,
        "🕒 Когда? Напиши новое время для <b>{title}</b>, например «завтра в 15:00» или «в пятницу утром».",
    ),
    "title": Prompt(EditStates.title, "✏️ Напиши новое название для <b>{title}</b>."),
    "tags": Prompt(
        EditStates.tags,
        "#️⃣ Напиши теги для <b>{title}</b> через пробел или запятую. Сейчас: {tags}. «-» — убрать все.",
    ),
}


def make_router() -> Router:
    router = Router(name="edit")
    router.callback_query.register(on_prompt, ItemCb.filter(F.a.in_(PROMPTS)))
    router.message.filter(F.chat.type == ChatType.PRIVATE)
    router.message.register(on_time, EditStates.time, F.text)
    router.message.register(on_title, EditStates.title, F.text)
    router.message.register(on_tags, EditStates.tags, F.text)
    return router


async def on_prompt(callback: CallbackQuery, callback_data: ItemCb, ctx: AppContext, state: FSMContext) -> None:
    prompt = PROMPTS[callback_data.a]
    message_id = common.message_id_of(callback)
    async with ctx.sessionmaker() as session:
        prefs = await ctx.prefs(session)
        item = await items.get_item(session, callback_data.id)
        await session.commit()
    if item is None:
        await common.answer(callback, common.NOT_FOUND)
        return
    if item.status == "deleted":
        await common.answer(callback, common.DELETED)
        await common.show(ctx, message_id, views.deleted_card(item))
        return
    # Another dialog's prompt (other card, settings) turns back into what it was.
    await common.restore_prompt(ctx, state, prefs, clock.utcnow(), keep=message_id)
    await state.set_state(prompt.state)
    await state.set_data(prompt_data(message_id, item_id=item.id))
    await common.answer(callback)
    await common.show(ctx, message_id, prompt_view(prompt, item))


def prompt_view(prompt: Prompt, item: Item) -> views.View:
    tags = " ".join(f"#{views.esc(t)}" for t in item.tags or []) or "нет"
    text = prompt.text.format(title=views.q(item.title), tags=tags) + FOOTER
    return text, markup([button("✖️ Отмена", ItemCb(a="back", id=item.id))])


# --- answers -------------------------------------------------------------------------------------------------------


async def on_time(message: Message, ctx: AppContext, state: FSMContext) -> None:
    """Queue the free-form time for the AI (job ``edit_time``); the processor replies with the new card."""
    data = await _active(message, ctx, state)
    text = (message.text or "").strip()
    now = clock.utcnow()
    async with ctx.sessionmaker() as session:
        prefs = await ctx.prefs(session)
        item = await items.get_item(session, data["item_id"])
        if item is None or item.status == "deleted":
            await session.commit()
            await state.clear()
            await message.answer(common.NOT_FOUND)
            return
        payload = {"item_id": item.id, "text": text, "message_id": message.message_id, "now": message.date.isoformat()}
        key = f"edit_time:{message.chat.id}:{message.message_id}"
        await jobs.enqueue(session, "edit_time", payload, dedupe_key=key)
        view = await common.card_view(session, item, prefs, now)
        await session.commit()
    await state.clear()
    await common.show(ctx, data.get("prompt_id"), view)
    await sender.react(ctx, message.message_id, sender.REACT_SEEN)
    ctx.jobs_wakeup.set()


async def on_title(message: Message, ctx: AppContext, state: FSMContext) -> None:
    data = await _active(message, ctx, state)
    title = " ".join((message.text or "").split())
    if not title:
        await message.answer("Название не может быть пустым — напиши текст или /cancel.")
        return
    await _save_field(message, ctx, state, data, lambda session, item_id: items.set_title(session, item_id, title))


async def on_tags(message: Message, ctx: AppContext, state: FSMContext) -> None:
    data = await _active(message, ctx, state)
    tags = parse_tags(message.text or "")
    await _save_field(message, ctx, state, data, lambda session, item_id: items.set_tags(session, item_id, tags))


def parse_tags(text: str) -> list[str]:
    """Tags from "#дом, работа  idea" (normalized by ``items.set_tags``); "-" means "no tags"."""
    text = text.strip()
    if text in CLEAR_TAGS:
        return []
    return [t for t in _TAG_SPLIT.split(text) if t.strip("#")]


async def _save_field(
    message: Message, ctx: AppContext, state: FSMContext, data: dict[str, Any], change: Change
) -> None:
    """Apply ``change(session, item_id)``, commit, turn the prompt back into the card and react 👌."""
    now = clock.utcnow()
    try:
        async with ctx.sessionmaker() as session:
            prefs = await ctx.prefs(session)
            item = await change(session, data["item_id"])
            view = await common.card_view(session, item, prefs, now)
            await session.commit()
    except items.NotFoundError:
        await state.clear()
        await message.answer(common.NOT_FOUND)
        return
    await state.clear()
    await common.show(ctx, data.get("prompt_id"), view)
    await sender.react(ctx, message.message_id, sender.REACT_SAVED)


async def _active(message: Message, ctx: AppContext, state: FSMContext) -> dict[str, Any]:
    """The dialog's data; an expired or broken dialog is closed and the message goes on to capture."""
    data = await state.get_data()
    if isinstance(data.get("item_id"), int) and not expired(data):
        return data
    async with ctx.sessionmaker() as session:
        prefs = await ctx.prefs(session)
        await session.commit()
    await common.restore_prompt(ctx, state, prefs, clock.utcnow())
    log.info("edit prompt expired; message %s goes to capture", message.message_id)
    raise SkipHandler
