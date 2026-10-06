"""compose.yaml: shutdown grace periods and the bot's healthcheck (run as Docker runs it, against a temp file)."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

from dumpbot.bot.app import DRAIN_TIMEOUT_S

COMPOSE = Path(__file__).resolve().parents[1] / "compose.yaml"


def service(name: str) -> str:
    """The text block of a top-level service (compose.yaml uses two-space indentation)."""
    lines = COMPOSE.read_text(encoding="utf-8").splitlines()
    start = lines.index(f"  {name}:")
    block: list[str] = []
    for line in lines[start + 1 :]:
        if line.strip() and not line.startswith("    "):
            break
        block.append(line)
    return "\n".join(block)


def grace_s(name: str) -> int:
    match = re.search(r"^    stop_grace_period: (\d+)s$", service(name), re.MULTILINE)
    assert match, f"{name}: no stop_grace_period (Docker Desktop may SIGKILL after 1 s)"
    return int(match.group(1))


def test_bot_has_time_to_drain_before_sigkill() -> None:
    # Drain the updates being handled, release the running job (<= 2 s), close the resources.
    assert grace_s("bot") >= DRAIN_TIMEOUT_S + 2 + 10


def test_db_has_time_for_its_shutdown_checkpoint() -> None:
    assert grace_s("db") >= 30


@pytest.fixture
def healthcheck(tmp_path: Path):
    match = re.search(r'^        - "(import os, sys, time;.+)"$', service("bot"), re.MULTILINE)
    assert match
    code = match.group(1)
    heartbeat = tmp_path / "heartbeat"

    def run(*, age_s: float | None, interval: str | None = None) -> int:
        if age_s is not None:
            heartbeat.write_text("x", encoding="utf-8")
            then = time.time() - age_s
            os.utime(heartbeat, (then, then))
        env = {k: v for k, v in os.environ.items() if k != "SCHEDULER_INTERVAL_S"}
        env["HEARTBEAT_FILE"] = str(heartbeat)
        if interval is not None:
            env["SCHEDULER_INTERVAL_S"] = interval
        return subprocess.run([sys.executable, "-c", code], env=env, check=False, timeout=30).returncode

    return run


def test_healthcheck_follows_the_scheduler_interval(healthcheck) -> None:
    assert healthcheck(age_s=None) == 1  # no heartbeat yet
    assert healthcheck(age_s=5) == 0
    assert healthcheck(age_s=200) == 1  # default 30 s interval: 120 s
    assert healthcheck(age_s=200, interval="300") == 0  # a longer interval is not "stuck"
    assert healthcheck(age_s=1300, interval="300") == 1
