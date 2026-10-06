"""Liveness watchdog for the background loops (architecture.md §4.20).

A loop stuck on an await that never returns, or an event loop blocked by synchronous code, never ends its task:
``Lifecycle`` cannot see that, and Docker only marks the container ``unhealthy`` (it never restarts one). So the
loops report progress with ``beat`` and a daemon thread, which keeps running while the event loop is blocked,
checks the stamps: ``on_stall`` gets every watched loop that has been silent for longer than its limit (the app
then exits and ``restart: unless-stopped`` brings it back). Limits must exceed a loop's longest legitimate step.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Final

__all__ = ["CHECK_EVERY_S", "Watchdog"]

CHECK_EVERY_S: Final = 15.0

StallHook = Callable[[dict[str, float]], object]


class Watchdog:
    """Monotonic last-progress stamps per loop name; thread-safe."""

    def __init__(
        self, *, check_every_s: float = CHECK_EVERY_S, monotonic: Callable[[], float] = time.monotonic
    ) -> None:
        self._check_every_s = check_every_s
        self._now = monotonic
        self._lock = threading.Lock()
        self._limits: dict[str, float] = {}
        self._beats: dict[str, float] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def watch(self, name: str, limit_s: float) -> None:
        """From now on ``name`` must ``beat`` at least every ``limit_s`` seconds."""
        with self._lock:
            self._limits[name] = limit_s
            self._beats[name] = self._now()

    def beat(self, name: str) -> None:
        """``name`` made progress."""
        with self._lock:
            self._beats[name] = self._now()

    def stalled(self) -> dict[str, float]:
        """Watched loops silent for longer than their limit -> seconds since their last beat."""
        now = self._now()
        with self._lock:
            silent = {name: now - self._beats[name] for name in self._limits}
            return {name: s for name, s in silent.items() if s > self._limits[name]}

    def start(self, on_stall: StallHook) -> None:
        """Check in a daemon thread until ``stop``; the first stall calls ``on_stall`` (in that thread) and ends it."""
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, args=(on_stall,), name="dumpbot-watchdog", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop checking and forget the watched loops (shutdown: cancelled loops stop beating)."""
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)
        with self._lock:
            self._limits.clear()

    def _run(self, on_stall: StallHook) -> None:
        while not self._stop.wait(self._check_every_s):
            stalled = self.stalled()
            if stalled:
                on_stall(stalled)
                return
