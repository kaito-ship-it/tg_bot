from __future__ import annotations

import hmac
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

PROJECT_DIR = Path(__file__).resolve().parent.parent
_SECRET_RE = re.compile(r"^[A-Za-z0-9_-]{32,256}$")

SITE_CATEGORIES: dict[str, int] = {
    "Недропользование": 35,
    "Экология": 11,
    "Анонс": 5,
    "Технологии": 9,
    "Геология": 13,
}
DEFAULT_CATEGORY_ID = SITE_CATEGORIES["Недропользование"]

CATEGORY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "Недропользование": ("недр", "месторожден", "лицензи", "разведк", "добыч"),
    "Экология": ("эколог", "выброс", "загрязнен", "утилизац"),
    "Анонс": ("анонс", "приглашаем", "состоится"),
    "Технологии": (
        "технолог",
        "цифров",
        "ии",
        "искусственн",
        "нейросет",
        "алгоритм",
        "машинн",
        "робот",
        "it",
        "инновац",
    ),
    "Геология": ("геолог", "геологоразведк"),
}


def _csv_env(name: str, default: str = "") -> tuple[str, ...]:
    return tuple(
        value.strip() for value in os.getenv(name, default).split(",") if value.strip()
    )


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name, "true" if default else "false").strip().lower()
    return raw in {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class Settings:
    telegram_channel_id: str = ""
    telegram_admin_user_ids: tuple[int, ...] = ()
    bot_token: str = ""
    notify_chat_id: str = ""
    tg_webhook_secret: str = ""
    telegram_webhook_enforce_ips: bool = True
    news_bot_api_base: str = ""
    news_bot_api_token: str = ""
    admin_base_url: str = "https://dev.nedra.kz/admin/news"
    api_host: str = "127.0.0.1"
    api_port: int = 8081
    public_api_base: str = ""
    media_signing_secret: str = ""
    webhook_max_body_bytes: int = 1_000_000
    draft_ttl_hours: int = 48
    retention_days: int = 30
    image_fallback_mode: str = "disabled"
    openai_api_key: str = ""
    openai_image_model: str = "gpt-image-1-mini"
    openai_image_quality: str = "medium"
    openai_image_size: str = "1536x1024"
    category_classifier_mode: str = "disabled"
    openai_text_model: str = "gpt-5.4-nano"
    allow_insecure_http: bool = False
    data_dir: Path = field(default_factory=lambda: PROJECT_DIR / "data")

    @property
    def drafts_dir(self) -> Path:
        return self.data_dir / "drafts"

    @property
    def photos_dir(self) -> Path:
        return self.data_dir / "photos"

    @property
    def moderation_db_file(self) -> Path:
        return self.data_dir / "state.db"

    def validate_runtime(self) -> None:
        required = {
            "TG_CHANNEL_ID": self.telegram_channel_id,
            "BOT_TOKEN": self.bot_token,
            "NOTIFY_CHAT_ID": self.notify_chat_id,
            "TG_WEBHOOK_SECRET": self.tg_webhook_secret,
            "NEWS_BOT_API_BASE": self.news_bot_api_base,
            "NEWS_BOT_API_TOKEN": self.news_bot_api_token,
            "PUBLIC_API_BASE": self.public_api_base,
            "MEDIA_SIGNING_SECRET": self.media_signing_secret,
        }
        missing = [name for name, value in required.items() if not value]
        if not self.telegram_admin_user_ids:
            missing.append("TG_ADMIN_USER_IDS")
        if missing:
            raise ValueError("Fill these values in .env: " + ", ".join(missing))

        for name, value in {
            "TG_CHANNEL_ID": self.telegram_channel_id,
            "NOTIFY_CHAT_ID": self.notify_chat_id,
        }.items():
            if not value.lstrip("-").isdigit():
                raise ValueError(f"{name} must be a numeric Telegram chat ID")
        if any(user_id <= 0 for user_id in self.telegram_admin_user_ids):
            raise ValueError("TG_ADMIN_USER_IDS must contain positive numeric IDs")
        if not _SECRET_RE.fullmatch(self.tg_webhook_secret):
            raise ValueError(
                "TG_WEBHOOK_SECRET must contain 32-256 letters, digits, '_' or '-'"
            )
        if not _SECRET_RE.fullmatch(self.media_signing_secret):
            raise ValueError(
                "MEDIA_SIGNING_SECRET must contain 32-256 letters, digits, '_' or '-'"
            )
        if hmac.compare_digest(self.tg_webhook_secret, self.media_signing_secret):
            raise ValueError("TG_WEBHOOK_SECRET and MEDIA_SIGNING_SECRET must differ")
        if len(self.news_bot_api_token) < 20:
            raise ValueError("NEWS_BOT_API_TOKEN must contain at least 20 characters")

        for name, value in {
            "NEWS_BOT_API_BASE": self.news_bot_api_base,
            "PUBLIC_API_BASE": self.public_api_base,
            "ADMIN_BASE_URL": self.admin_base_url,
        }.items():
            parsed = urlparse(value)
            if (
                not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError(f"{name} must be an absolute URL without credentials")
            if not self.allow_insecure_http and parsed.scheme != "https":
                raise ValueError(f"{name} must use HTTPS")
            if self.allow_insecure_http and parsed.scheme not in {"http", "https"}:
                raise ValueError(f"{name} must use HTTP or HTTPS")

        if not self.allow_insecure_http and self.api_host not in {
            "127.0.0.1",
            "::1",
            "localhost",
        }:
            raise ValueError("API_HOST must be loopback when ALLOW_INSECURE_HTTP=false")

        if not 1 <= self.api_port <= 65535:
            raise ValueError("API_PORT must be between 1 and 65535")
        if self.webhook_max_body_bytes < 1024:
            raise ValueError("WEBHOOK_MAX_BODY_BYTES must be at least 1024")
        if self.draft_ttl_hours < 1:
            raise ValueError("DRAFT_TTL_HOURS must be positive")
        if self.retention_days < 1:
            raise ValueError("RETENTION_DAYS must be positive")
        if self.retention_days * 24 < self.draft_ttl_hours:
            raise ValueError("RETENTION_DAYS must cover DRAFT_TTL_HOURS")
        if not self.data_dir.is_absolute():
            raise ValueError("DATA_DIR must be an absolute path")

        if self.image_fallback_mode not in {"disabled", "openai"}:
            raise ValueError("IMAGE_FALLBACK_MODE must be 'disabled' or 'openai'")
        if self.image_fallback_mode == "openai" and not self.openai_api_key:
            raise ValueError(
                "Fill OPENAI_API_KEY in .env or set IMAGE_FALLBACK_MODE=disabled"
            )
        if self.openai_image_quality not in {"low", "medium", "high", "auto"}:
            raise ValueError("OPENAI_IMAGE_QUALITY must be low, medium, high, or auto")
        if self.openai_image_size not in {
            "1024x1024",
            "1024x1536",
            "1536x1024",
            "auto",
        }:
            raise ValueError(
                "OPENAI_IMAGE_SIZE must be 1024x1024, 1024x1536, 1536x1024, or auto"
            )
        if self.category_classifier_mode not in {"disabled", "openai"}:
            raise ValueError("CATEGORY_CLASSIFIER_MODE must be 'disabled' or 'openai'")
        if self.category_classifier_mode == "openai" and not self.openai_api_key:
            raise ValueError(
                "Fill OPENAI_API_KEY in .env or set CATEGORY_CLASSIFIER_MODE=disabled"
            )
        if not self.openai_text_model:
            raise ValueError("OPENAI_TEXT_MODEL cannot be empty")


def load_settings(env_file: Path | None = None) -> Settings:
    load_dotenv(env_file or PROJECT_DIR / ".env")
    data_dir = Path(os.getenv("DATA_DIR", str(PROJECT_DIR / "data"))).expanduser()
    return Settings(
        telegram_channel_id=os.getenv("TG_CHANNEL_ID", "").strip(),
        telegram_admin_user_ids=tuple(
            int(value) for value in _csv_env("TG_ADMIN_USER_IDS") if value.isdigit()
        ),
        bot_token=os.getenv("BOT_TOKEN", "").strip(),
        notify_chat_id=os.getenv("NOTIFY_CHAT_ID", "").strip(),
        tg_webhook_secret=os.getenv("TG_WEBHOOK_SECRET", "").strip(),
        telegram_webhook_enforce_ips=_bool_env("TELEGRAM_WEBHOOK_ENFORCE_IPS", True),
        news_bot_api_base=os.getenv("NEWS_BOT_API_BASE", "").strip().rstrip("/"),
        news_bot_api_token=os.getenv("NEWS_BOT_API_TOKEN", "").strip(),
        admin_base_url=os.getenv("ADMIN_BASE_URL", "https://dev.nedra.kz/admin/news")
        .strip()
        .rstrip("/"),
        api_host=os.getenv("API_HOST", "127.0.0.1").strip(),
        api_port=int(os.getenv("API_PORT", "8081")),
        public_api_base=os.getenv("PUBLIC_API_BASE", "").strip().rstrip("/"),
        media_signing_secret=os.getenv("MEDIA_SIGNING_SECRET", "").strip(),
        webhook_max_body_bytes=int(os.getenv("WEBHOOK_MAX_BODY_BYTES", "1000000")),
        draft_ttl_hours=int(os.getenv("DRAFT_TTL_HOURS", "48")),
        retention_days=int(os.getenv("RETENTION_DAYS", "30")),
        image_fallback_mode=os.getenv("IMAGE_FALLBACK_MODE", "disabled")
        .strip()
        .lower(),
        openai_api_key=os.getenv("OPENAI_API_KEY", "").strip(),
        openai_image_model=os.getenv("OPENAI_IMAGE_MODEL", "gpt-image-1-mini").strip(),
        openai_image_quality=os.getenv("OPENAI_IMAGE_QUALITY", "medium")
        .strip()
        .lower(),
        openai_image_size=os.getenv("OPENAI_IMAGE_SIZE", "1536x1024").strip().lower(),
        category_classifier_mode=os.getenv("CATEGORY_CLASSIFIER_MODE", "disabled")
        .strip()
        .lower(),
        openai_text_model=os.getenv("OPENAI_TEXT_MODEL", "gpt-5.4-nano").strip(),
        allow_insecure_http=_bool_env("ALLOW_INSECURE_HTTP", False),
        data_dir=data_dir,
    )
