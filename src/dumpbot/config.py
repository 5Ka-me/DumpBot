"""Process configuration from environment / .env.

User-editable preferences (timezone, slot times, quiet hours, budget...) live in the DB
(see ``domain.prefs.Prefs``); env values here only seed their defaults.
"""

from __future__ import annotations

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Telegram
    bot_token: SecretStr
    # 0 = not configured yet: the bot answers /start from anyone with their Telegram ID.
    owner_id: int = 0

    # Database (async SQLAlchemy URL, asyncpg driver)
    database_url: str = "postgresql+asyncpg://dump:dump@127.0.0.1:5432/dump"

    # chutes.ai (OpenAI-compatible chat API)
    chutes_api_key: SecretStr
    chutes_base_url: str = "https://llm.chutes.ai/v1"
    llm_model: str = "google/gemma-4-31B-turbo-TEE"  # text + image, structured outputs
    llm_fallback_model: str = "Qwen/Qwen3-32B-TEE"  # text only
    vision_fallback_model: str = "Qwen/Qwen3.6-27B-TEE"  # text + image
    llm_timeout_s: float = 60.0
    llm_max_tokens: int = 1500
    # Used when the live price list (GET /models) is unavailable, USD per 1M tokens.
    llm_fallback_price_in: float = 1.0
    llm_fallback_price_out: float = 3.0

    # Speech-to-text (community Whisper chute on chutes.ai, JSON body with base64 audio)
    stt_url: str = "https://vonkaiser-audiodojo.chutes.ai/stt/whisper"
    stt_timeout_s: float = 60.0
    stt_convert_wav: bool = False  # convert OGG/Opus -> 16 kHz mono WAV with ffmpeg before sending
    stt_usd_per_second: float = 0.0005  # billed per started second of GPU time (estimate)

    # Seeds for the per-user preferences row
    default_tz: str = "Europe/Warsaw"
    default_budget_usd: float = 5.0

    # Runtime
    log_level: str = "INFO"
    heartbeat_file: str = "/tmp/heartbeat"
    scheduler_interval_s: float = 30.0
