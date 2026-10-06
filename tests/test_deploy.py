"""compose.yaml: shutdown grace periods, the bot's healthcheck (run as Docker runs it, against a temp file), the
optional local Whisper service, the optional Tailscale Funnel of the Mini App and its serve config; .env.example
documents every setting."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from dumpbot.bot.app import DRAIN_TIMEOUT_S
from dumpbot.config import Settings
from dumpbot.web.server import SHUTDOWN_TIMEOUT_S

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "compose.yaml"


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
    # Stop the Mini App's web server, drain the updates being handled, release the running job (<= 2 s), close.
    assert grace_s("bot") >= SHUTDOWN_TIMEOUT_S + DRAIN_TIMEOUT_S + 2 + 10


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


# --- local Whisper (docs/features/iteration-2.md, B) ---------------------------------------------------------------


def test_whisper_is_an_optional_unpublished_service() -> None:
    block = service("whisper")
    assert re.search(r'^    profiles: \["whisper"\]$', block, re.MULTILINE), "only with --profile whisper"
    assert "ports:" not in block, "never published on the host: the bot reaches it over the compose network"
    assert re.search(r"^      context: \./whisper$", block, re.MULTILINE)
    assert re.search(r"^    restart: unless-stopped$", block, re.MULTILINE)
    assert re.search(r"^    mem_limit: \$\{WHISPER_MEM_LIMIT:-\d+[gm]\}$", block, re.MULTILINE)
    assert "      - whisper-models:/models" in block
    for var in ("WHISPER_MODEL", "WHISPER_THREADS", "WHISPER_BEAM_SIZE", "WHISPER_MAX_AUDIO_S"):
        assert f"      {var}: ${{{var}:-" in block
    assert re.search(r"^  whisper-models:$", COMPOSE.read_text(encoding="utf-8"), re.MULTILINE)
    assert grace_s("whisper") >= 5


def test_whisper_healthcheck_probes_health() -> None:
    match = re.search(r'^        - "(import urllib.+)"$', service("whisper"), re.MULTILINE)
    assert match and "127.0.0.1:9000/health" in match.group(1)

    class Health(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200 if self.path == "/health" else 404)
            self.end_headers()
            self.wfile.write(b'{"status": "ok"}')

        def log_message(self, *args: object) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Health)
    code = match.group(1).replace("9000", str(server.server_address[1]))

    def run() -> int:
        return subprocess.run([sys.executable, "-c", code], check=False, capture_output=True, timeout=30).returncode

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        assert run() == 0
    finally:
        server.shutdown()
        server.server_close()
    assert run() == 1  # nothing listens: unhealthy


def test_whisper_image_files() -> None:
    whisper = ROOT / "whisper"
    dockerfile = (whisper / "Dockerfile").read_text(encoding="utf-8")
    assert 'CMD ["python", "app.py"]' in dockerfile and "USER app" in dockerfile and "/models" in dockerfile
    requirements = (whisper / "requirements.txt").read_text(encoding="utf-8")
    pins = dict(line.split("==") for line in requirements.splitlines() if line and not line.startswith("#"))
    assert {"faster-whisper", "av", "aiohttp"} <= set(pins)
    assert int(pins["av"].split(".")[0]) < 19  # faster-whisper 1.2.1 passes metadata_errors, gone in PyAV 19
    assert (whisper / "app.py").is_file() and (whisper / "README.md").is_file()
    # The bot's image does not need the service.
    assert "whisper/" in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()


# --- Mini App over Tailscale Funnel (docs/features/iteration-2.md, D) ------------------------------------------------


def test_tailscale_is_an_optional_userspace_service() -> None:
    block = service("tailscale")
    assert re.search(r'^    profiles: \["miniapp"\]$', block, re.MULTILINE), "only with --profile miniapp"
    assert re.search(r"^    image: tailscale/tailscale:", block, re.MULTILINE)
    assert "ports:" not in block, "nothing on the host: Funnel reaches the tailnet from inside the container"
    assert "/dev/net/tun" not in block and "cap_add" not in block and "privileged" not in block
    env = dict(re.findall(r"^      (TS_[A-Z_]+): (.+)$", block, re.MULTILINE))
    assert env["TS_AUTHKEY"] == "${TS_AUTHKEY:-}"  # interpolated for every profile: never required
    assert env["TS_HOSTNAME"] == "dump" and env["TS_USERSPACE"] == '"true"' and env["TS_AUTH_ONCE"] == '"true"'
    assert env["TS_STATE_DIR"] == "/var/lib/tailscale" and env["TS_SERVE_CONFIG"] == "/config/serve.json"
    assert "      - tailscale-state:/var/lib/tailscale" in block
    assert "      - ./tailscale:/config:ro" in block  # a directory: Tailscale notices changes of the file
    assert re.search(r"^  tailscale-state:$", COMPOSE.read_text(encoding="utf-8"), re.MULTILINE)
    assert re.search(r"^    restart: unless-stopped$", block, re.MULTILINE)
    assert grace_s("tailscale") >= 5
    assert "tailscale/" in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()


def test_serve_config_funnels_https_to_the_bot_only() -> None:
    config = json.loads((ROOT / "tailscale" / "serve.json").read_text(encoding="utf-8"))
    host = "${TS_CERT_DOMAIN}:443"
    assert config == {
        "TCP": {"443": {"HTTPS": True}},
        "Web": {host: {"Handlers": {"/": {"Proxy": f"http://bot:{Settings.model_fields['webapp_port'].default}"}}}},
        "AllowFunnel": {host: True},
    }


def test_env_example_documents_every_setting() -> None:
    keys = {
        line.split("=", 1)[0]
        for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    }
    assert {name.upper() for name in Settings.model_fields} <= keys
    assert {"WHISPER_MODEL", "WHISPER_THREADS", "WHISPER_BEAM_SIZE", "WHISPER_MAX_AUDIO_S", "WHISPER_MEM_LIMIT"} <= keys
    assert "TS_AUTHKEY" in keys
