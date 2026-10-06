"""Bot routers in dispatch order: commands -> settings (FSM input) -> edit (FSM input) -> callbacks -> capture.

Each module has ``make_router()`` returning a fresh ``Router`` (a router can be attached to one parent only, and
tests build several dispatchers).
"""

from __future__ import annotations

from aiogram import Router

from dumpbot.bot.routers import callbacks, capture, commands, edit, settings


def make_routers() -> list[Router]:
    return [
        commands.make_router(),
        settings.make_router(),
        edit.make_router(),
        callbacks.make_router(),
        capture.make_router(),
    ]
