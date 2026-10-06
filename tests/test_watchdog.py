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
    dog = Watchdog(check_every_s=0.01, monotonic=clock)
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
