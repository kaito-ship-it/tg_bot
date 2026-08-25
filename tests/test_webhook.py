from dataclasses import replace

from fastapi.testclient import TestClient

from app.api import create_app
from app.backend import NewsCategory
from app.draft_store import DraftStore
from app.models import NewsDraft
from app.moderation_db import ModerationDB
from app.notifier import (
    REJECT_DRAFT_PREFIX,
    SELECT_CATEGORY_PREFIX,
    TelegramNotifier,
)
from app.webhook_service import extract_webhook_url
from tests.test_store_and_api import make_settings


def test_moderation_db_deduplicates_telegram_message(tmp_path) -> None:
    database = ModerationDB(tmp_path / "state.db")

    first = database.enqueue(
        chat_id="-1001",
        message_id=42,
        source_url="https://example.kz/news",
        raw_text="Новость https://example.kz/news",
        draft_id="a" * 32,
    )
    duplicate = database.enqueue(
        chat_id="-1001",
        message_id=42,
        source_url="https://example.kz/news",
        raw_text="Новость https://example.kz/news",
        draft_id="b" * 32,
    )

    assert first is not None
    assert duplicate is None
    assert database.take("new") == [first]
    updated = database.update(first.id, status="awaiting_category", category_id=11)
    assert updated.status == "awaiting_category"
    assert updated.category_id == 11


def test_webhook_requires_secret_and_delivers_update(tmp_path) -> None:
    settings = replace(
        make_settings(tmp_path),
        tg_webhook_secret="webhook-secret",
    )
    store = DraftStore(settings.drafts_dir, settings.photos_dir, 24)
    received = []

    async def handler(payload):
        received.append(payload)

    client = TestClient(create_app(store, settings, webhook_handler=handler))
    payload = {"update_id": 1, "channel_post": {"message_id": 2}}

    assert client.post("/webhook", json=payload).status_code == 403
    accepted = client.post(
        "/webhook",
        json=payload,
        headers={"X-Telegram-Bot-Api-Secret-Token": "webhook-secret"},
    )

    assert accepted.status_code == 200
    assert received == [payload]


def test_webhook_url_extraction_prefers_text_link_entity() -> None:
    text = "Читайте источник"
    entities = [
        {
            "type": "text_link",
            "offset": 0,
            "length": len(text),
            "url": "https://example.kz/news/1",
        }
    ]

    assert extract_webhook_url(text, entities) == "https://example.kz/news/1"


def test_category_keyboard_uses_backend_categories() -> None:
    draft = NewsDraft.create(
        draft_id="c" * 32,
        title="Новость",
        text="Текст",
        category_id=11,
        source_message_id=1,
    )
    categories = (
        NewsCategory(35, "Недропользование"),
        NewsCategory(11, "Экология"),
        NewsCategory(5, "Анонс"),
    )

    keyboard = TelegramNotifier._draft_keyboard(
        draft,
        None,
        categories=categories,
    )["inline_keyboard"]
    buttons = [button for row in keyboard for button in row]

    assert buttons[1]["text"] == "✓ Экология"
    assert buttons[1]["callback_data"] == (
        f"{SELECT_CATEGORY_PREFIX}{draft.draft_id}:11"
    )
    assert buttons[-1]["callback_data"] == (
        f"{REJECT_DRAFT_PREFIX}{draft.draft_id}"
    )
