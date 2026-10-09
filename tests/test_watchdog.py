"""watchdog: last-progress stamps of the background loops, checked from a thread."""

from __future__ import annotations

import threading

from dumpbot.watchdog import Watchdog


class FakeMonotonic:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_stalled_reports_only_watched_loops_silent_beyond_their_limit() -> None:
    clock = FakeMonotonic()
    dog = Watchdog(monotonic=clock)
    dog.watch("scheduler", 60)
    dog.watch("jobs", 100)
    dog.beat("other")  # never watched: never stalled

    clock.now += 60
    assert dog.stalled() == {}
    clock.now += 1
    assert dog.stalled() == {"scheduler": 61}

    dog.beat("scheduler")
    clock.now += 59
    assert dog.stalled() == {"jobs": 120}


def test_thread_reports_a_stall_once_and_stop_forgets_the_loops() -> None:
    clock = FakeMonotonic()
    # The fake clock jumps 61 s between two checks 10 ms apart: with the default allowance that would read as a
    # frozen process (a suspend, see below), so this thread may be late by up to 1000 s.
    dog = Watchdog(check_every_s=0.01, monotonic=clock, suspend_after_s=1000)
    dog.watch("scheduler", 60)
    calls: list[dict[str, float]] = []
    called = threading.Event()

    def on_stall(stalled: dict[str, float]) -> None:
        calls.append(stalled)
        called.set()

    dog.start(on_stall)
    clock.now += 61
    assert called.wait(2)
    dog.stop()
    assert calls == [{"scheduler": 61}]

    dog.start(on_stall)  # restartable; nothing is watched any more
    clock.now += 1000
    threading.Event().wait(0.05)
    dog.stop()
    assert len(calls) == 1


def test_beats_keep_a_loop_alive() -> None:
    clock = FakeMonotonic()
    dog = Watchdog(check_every_s=0.01, monotonic=clock)
    dog.watch("jobs", 10)
    calls: list[dict[str, float]] = []
    dog.start(calls.append)
    for _ in range(20):
        clock.now += 5
        dog.beat("jobs")
        threading.Event().wait(0.005)
    dog.stop()
    assert calls == []


def test_a_frozen_process_is_no_stall() -> None:
    """A PC that slept: the checking thread itself was frozen, so its next check comes hours late — every loop starts
    its limit afresh instead of being reported (docs/features/iteration-3.md §5)."""
    clock = FakeMonotonic()
    dog = Watchdog(check_every_s=15, monotonic=clock)
    dog.watch("scheduler", 900)
    dog.watch("jobs", 1800)
    assert dog.check() == {}  # the first check: nothing to compare with

    clock.now += 4 * 3600  # 19:00 -> 23:19 asleep
    assert dog.stalled() == {"scheduler": 4 * 3600, "jobs": 4 * 3600}
    assert dog.check() == {}  # a suspend, not a stall
    assert dog.stalled() == {}

    for _ in range(60):  # the limits run again from the wake-up, checked on time
        clock.now += 15
        dog.beat("jobs")
        assert dog.check() == {}
    clock.now += 15
    assert dog.check() == {"scheduler": 915}  # a loop that stays silent is a stall again


def test_a_hung_loop_is_still_reported_while_the_thread_checks_on_time() -> None:
    clock = FakeMonotonic()
    dog = Watchdog(check_every_s=15, monotonic=clock)
    dog.watch("scheduler", 60)
    dog.check()
    for _ in range(4):
        clock.now += 15
        assert dog.check() == {}
    clock.now += 15
    assert dog.check() == {"scheduler": 75}


def test_thread_survives_a_suspend() -> None:
    clock = FakeMonotonic()
    dog = Watchdog(check_every_s=0.01, monotonic=clock)
    dog.watch("scheduler", 60)
    calls: list[dict[str, float]] = []
    dog.start(calls.append)
    clock.now += 4 * 3600  # one jump: the whole process slept
    threading.Event().wait(0.1)
    dog.stop()
    assert calls == []
