from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


@dataclass(frozen=True)
class Settings:
    app_env: str = os.getenv("APP_ENV", "development")
    personal_mode: bool = os.getenv("PERSONAL_MODE", "0").lower() in {"1", "true", "yes"}
    trust_mode: str = os.getenv(
        "JARVIS_TRUST_MODE",
        "personal" if personal_mode else "confirm",
    ).lower()
    database_path: str = os.getenv(
        "DATABASE_PATH",
        str(ROOT / "data" / "jarvis.db"),
    )
    device_token: str = os.getenv("DEVICE_TOKEN", "")
    # Base URL used when handing links back to the user (artifacts, uploads).
    # It is configuration, not a constant: hardcoding one deployment's domain in
    # the source made the code unusable anywhere else.
    public_base_url: str = os.getenv("PUBLIC_BASE_URL", "http://127.0.0.1:8787").rstrip("/")
    deepseek_api_key: str = os.getenv("DEEPSEEK_API_KEY", "")
    deepseek_base_url: str = os.getenv(
        "DEEPSEEK_BASE_URL",
        "https://aicyy.xyz",
    )
    deepseek_model: str = os.getenv(
        "DEEPSEEK_MODEL",
        "deepseek-v4.1-flash-expires-on-0910",
    )
    llm_api_format: str = os.getenv(
        "LLM_API_FORMAT",
        "chat_completions",
    ).strip().lower()
    vision_base_url: str = os.getenv("VISION_BASE_URL", "")
    vision_api_key: str = os.getenv("VISION_API_KEY", "")
    vision_model: str = os.getenv("VISION_MODEL", "")
    # Model routing (DEVICE_OPS.md §4). Leaving FALLBACK_MODEL empty disables
    # fallback entirely, which is the honest default: no degradation is better
    # than an untested one.
    llm_timeout_seconds: float = float(os.getenv("LLM_TIMEOUT_SECONDS", "90"))
    fallback_model: str = os.getenv("FALLBACK_MODEL", "").strip()
    fallback_base_url: str = os.getenv("FALLBACK_BASE_URL", "").strip()
    fallback_timeout_seconds: float = float(os.getenv("FALLBACK_TIMEOUT_SECONDS", "45"))
    asr_provider: str = os.getenv("ASR_PROVIDER", "none")
    asr_endpoint: str = os.getenv(
        "ASR_ENDPOINT",
        "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_nostream",
    )
    asr_api_key: str = os.getenv("ASR_API_KEY", "")
    asr_app_id: str = os.getenv("ASR_APP_ID", "")
    asr_access_key: str = os.getenv("ASR_ACCESS_KEY", "")
    asr_resource_id: str = os.getenv(
        "ASR_RESOURCE_ID",
        "volc.seedasr.sauc.duration",
    )
    tts_provider: str = os.getenv("TTS_PROVIDER", "edge").lower()
    tts_voice: str = os.getenv("TTS_VOICE", "zh-CN-XiaoxiaoNeural")
    tts_rate: str = os.getenv("TTS_RATE", "+0%")
    daily_token_limit: int = int(os.getenv("DAILY_TOKEN_LIMIT", "50000000"))
    daily_asr_seconds_limit: int = int(os.getenv("DAILY_ASR_SECONDS_LIMIT", "18000"))
    user_timezone: str = os.getenv("USER_TIMEZONE", "Asia/Shanghai")


settings = Settings()

