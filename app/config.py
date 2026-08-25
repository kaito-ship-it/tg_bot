from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


PROJECT_DIR = Path(__file__).resolve().parent.parent

SITE_CATEGORIES: dict[str, int] = {
    "Недропользование": 35,
    "Экология": 11,
    "Анонс": 5,
    "Технологии": 9,
    "Геология": 13,
}
DEFAULT_CATEGORY_ID = SITE_CATEGORIES["Недропользование"]

CATEGORY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "Недропользование": (
        "недр",
        "месторожден",
        "лицензи",
        "разведк",
        "добыч",
    ),
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


def _csv_env(name: str, default: str) -> tuple[str, ...]:
    raw = os.getenv(name, default)
    return tuple(value.strip() for value in raw.split(",") if value.strip())


@dataclass(frozen=True, slots=True)
class Settings:
    tg_api_id: int | None
    tg_api_hash: str
    tg_session_name: str
    telegram_channel: str
    bot_token: str
    notify_chat_id: str
    admin_base_url: str
    api_host: str
    api_port: int
    public_api_base: str
    draft_ttl_hours: int
    album_wait_seconds: float
    image_fallback_mode: str
    openai_api_key: str
    openai_image_model: str
    openai_image_quality: str
    openai_image_size: str
    category_classifier_mode: str
    openai_text_model: str
    cors_origins: tuple[str, ...]
    auto_open_admin: bool = False
    publish_mode: str = "disabled"
    auto_publish_timeout_seconds: int = 90
    browser_command: str = ""
    playwright_headless: bool = True
    playwright_slow_mo_ms: int = 0
    playwright_auth_state: Path | None = None
    data_dir: Path = field(default_factory=lambda: PROJECT_DIR / "data")
    news_bot_api_base: str = ""
    news_bot_api_token: str = ""
    telegram_channel_id: str = ""
    telegram_admin_user_ids: tuple[int, ...] = ()
    telegram_ingest_mode: str = "telethon"
    tg_webhook_secret: str = ""
    telegram_webhook_enforce_ips: bool = False

    @property
    def state_file(self) -> Path:
        return self.data_dir / "state.json"

    @property
    def drafts_dir(self) -> Path:
        return self.data_dir / "drafts"

    @property
    def photos_dir(self) -> Path:
        return self.data_dir / "photos"

    @property
    def queue_dir(self) -> Path:
        return self.data_dir / "queue"

    @property
    def publication_queue_dir(self) -> Path:
        return self.data_dir / "publication_queue"

    @property
    def admin_auth_state_file(self) -> Path:
        return self.playwright_auth_state or self.data_dir / "admin_auth.json"

    @property
    def browser_errors_dir(self) -> Path:
        return self.data_dir / "browser_errors"

    @property
    def moderation_db_file(self) -> Path:
        return self.data_dir / "state.db"

    def validate_runtime(self) -> None:
        missing: list[str] = []
        if self.telegram_ingest_mode == "telethon":
            if self.tg_api_id is None:
                missing.append("TG_API_ID")
            if not self.tg_api_hash:
                missing.append("TG_API_HASH")
        if not self.telegram_channel:
            missing.append("TELEGRAM_CHANNEL")
        if not self.bot_token:
            missing.append("BOT_TOKEN")
        if not self.notify_chat_id:
            missing.append("NOTIFY_CHAT_ID")
        if missing:
            raise ValueError("Fill these values in .env: " + ", ".join(missing))
        if self.telegram_ingest_mode not in {"telethon", "webhook"}:
            raise ValueError(
                "TELEGRAM_INGEST_MODE must be 'telethon' or 'webhook'"
            )
        if self.telegram_ingest_mode == "webhook":
            webhook_missing: list[str] = []
            if not self.telegram_channel_id:
                webhook_missing.append("TG_CHANNEL_ID")
            if not self.tg_webhook_secret:
                webhook_missing.append("TG_WEBHOOK_SECRET")
            if self.publish_mode != "backend_api":
                raise ValueError(
                    "Webhook mode requires PUBLISH_MODE=backend_api"
                )
            if webhook_missing:
                raise ValueError(
                    "Fill these values in .env: " + ", ".join(webhook_missing)
                )
        if self.image_fallback_mode not in {"disabled", "openai"}:
            raise ValueError(
                "IMAGE_FALLBACK_MODE must be 'disabled' or 'openai'"
            )
        if self.image_fallback_mode == "openai" and not self.openai_api_key:
            raise ValueError(
                "Fill OPENAI_API_KEY in .env or set "
                "IMAGE_FALLBACK_MODE=disabled"
            )
        if self.openai_image_quality not in {"low", "medium", "high", "auto"}:
            raise ValueError(
                "OPENAI_IMAGE_QUALITY must be low, medium, high, or auto"
            )
        if self.openai_image_size not in {
            "1024x1024",
            "1024x1536",
            "1536x1024",
            "auto",
        }:
            raise ValueError(
                "OPENAI_IMAGE_SIZE must be 1024x1024, 1024x1536, "
                "1536x1024, or auto"
            )
        if self.category_classifier_mode not in {"disabled", "openai"}:
            raise ValueError(
                "CATEGORY_CLASSIFIER_MODE must be 'disabled' or 'openai'"
            )
        if self.category_classifier_mode == "openai" and not self.openai_api_key:
            raise ValueError(
                "Fill OPENAI_API_KEY in .env or set "
                "CATEGORY_CLASSIFIER_MODE=disabled"
            )
        if not self.openai_text_model:
            raise ValueError("OPENAI_TEXT_MODEL cannot be empty")
        if self.auto_publish_timeout_seconds < 15:
            raise ValueError("AUTO_PUBLISH_TIMEOUT_SECONDS must be at least 15")
        if self.publish_mode not in {
            "disabled",
            "local_browser",
            "playwright",
            "backend_api",
        }:
            raise ValueError(
                "PUBLISH_MODE must be disabled, local_browser, playwright, "
                "or backend_api"
            )
        if self.playwright_slow_mo_ms < 0:
            raise ValueError("PLAYWRIGHT_SLOW_MO_MS cannot be negative")
        if self.publish_mode == "playwright" and not self.admin_auth_state_file.is_file():
            raise ValueError(
                "Playwright admin authentication is missing: "
                f"{self.admin_auth_state_file}. Capture it before VPS startup."
            )
        if self.publish_mode == "backend_api":
            if not self.news_bot_api_base:
                missing.append("NEWS_BOT_API_BASE")
            if not self.news_bot_api_token:
                missing.append("NEWS_BOT_API_TOKEN")
            if missing:
                raise ValueError("Fill these values in .env: " + ", ".join(missing))


def load_settings(env_file: Path | None = None) -> Settings:
    load_dotenv(env_file or PROJECT_DIR / ".env")
    raw_api_id = os.getenv("TG_API_ID", "").strip()
    data_dir = Path(os.getenv("DATA_DIR", str(PROJECT_DIR / "data"))).expanduser()
    legacy_auto_open = os.getenv("AUTO_OPEN_ADMIN", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    publish_mode = os.getenv("PUBLISH_MODE", "").strip().lower()
    if not publish_mode:
        publish_mode = "local_browser" if legacy_auto_open else "disabled"
    raw_auth_state = os.getenv("PLAYWRIGHT_AUTH_STATE", "").strip()
    playwright_auth_state = Path(raw_auth_state).expanduser() if raw_auth_state else None

    return Settings(
        tg_api_id=int(raw_api_id) if raw_api_id else None,
        tg_api_hash=os.getenv("TG_API_HASH", "").strip(),
        tg_session_name=os.getenv("TG_SESSION_NAME", "tg2site").strip(),
        telegram_channel=os.getenv("TELEGRAM_CHANNEL", "").strip(),
        bot_token=os.getenv("BOT_TOKEN", "").strip(),
        notify_chat_id=os.getenv("NOTIFY_CHAT_ID", "").strip(),
        admin_base_url=os.getenv(
            "ADMIN_BASE_URL", "https://dev.nedra.kz/admin/news"
        ).rstrip("/"),
        api_host=os.getenv("API_HOST", "127.0.0.1").strip(),
        api_port=int(os.getenv("API_PORT", "8000")),
        public_api_base=os.getenv(
            "PUBLIC_API_BASE", "http://localhost:8000"
        ).rstrip("/"),
        draft_ttl_hours=int(os.getenv("DRAFT_TTL_HOURS", "24")),
        album_wait_seconds=float(os.getenv("ALBUM_WAIT_SECONDS", "2")),
        image_fallback_mode=os.getenv(
            "IMAGE_FALLBACK_MODE", "disabled"
        ).strip().lower(),
        openai_api_key=os.getenv("OPENAI_API_KEY", "").strip(),
        openai_image_model=os.getenv(
            "OPENAI_IMAGE_MODEL", "gpt-image-1-mini"
        ).strip(),
        openai_image_quality=os.getenv(
            "OPENAI_IMAGE_QUALITY", "medium"
        ).strip().lower(),
        openai_image_size=os.getenv(
            "OPENAI_IMAGE_SIZE", "1536x1024"
        ).strip().lower(),
        category_classifier_mode=os.getenv(
            "CATEGORY_CLASSIFIER_MODE", "disabled"
        ).strip().lower(),
        openai_text_model=os.getenv(
            "OPENAI_TEXT_MODEL", "gpt-5.4-nano"
        ).strip(),
        cors_origins=_csv_env("CORS_ORIGINS", "https://dev.nedra.kz"),
        auto_open_admin=legacy_auto_open,
        publish_mode=publish_mode,
        auto_publish_timeout_seconds=int(
            os.getenv("AUTO_PUBLISH_TIMEOUT_SECONDS", "90")
        ),
        browser_command=os.getenv("BROWSER_COMMAND", "").strip(),
        playwright_headless=os.getenv(
            "PLAYWRIGHT_HEADLESS", "true"
        ).strip().lower()
        in {"1", "true", "yes", "on"},
        playwright_slow_mo_ms=int(os.getenv("PLAYWRIGHT_SLOW_MO_MS", "0")),
        playwright_auth_state=playwright_auth_state,
        data_dir=data_dir,
        news_bot_api_base=os.getenv("NEWS_BOT_API_BASE", "").strip().rstrip("/"),
        news_bot_api_token=os.getenv("NEWS_BOT_API_TOKEN", "").strip(),
        telegram_channel_id=os.getenv("TG_CHANNEL_ID", "").strip(),
        telegram_admin_user_ids=tuple(
            int(value)
            for value in _csv_env("TG_ADMIN_USER_IDS", "")
            if value.lstrip("-").isdigit()
        ),
        telegram_ingest_mode=os.getenv(
            "TELEGRAM_INGEST_MODE", "telethon"
        ).strip().lower(),
        tg_webhook_secret=os.getenv("TG_WEBHOOK_SECRET", "").strip(),
        telegram_webhook_enforce_ips=os.getenv(
            "TELEGRAM_WEBHOOK_ENFORCE_IPS", "false"
        ).strip().lower()
        in {"1", "true", "yes", "on"},
    )
