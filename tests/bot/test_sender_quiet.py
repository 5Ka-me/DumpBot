"""sender.send and the quiet hours: a caller that looked at them itself (the job processor) decides, so the request
middleware does not load the prefs again for the message (docs/features/iteration-4.md §7)."""

from __future__ import annotations

from dumpbot.bot import sender


async def test_a_caller_that_checked_the_quiet_hours_decides(make_ctx, fake_bot, sessionmaker) -> None:
    ctx = make_ctx(sessionmaker)
    await sender.send(ctx, "a", purpose="card", quiet_checked=True)
    await sender.send(ctx, "b", purpose="card", silent=True, quiet_checked=True)
    await sender.send(ctx, "c", purpose="card")  # the middleware decides
    await sender.send(ctx, "d", purpose="reminder")  # a time the owner named rings
    calls = fake_bot.calls_of("send_message")
    assert [c["disable_notification"] for c in calls] == [False, True, None, False]
