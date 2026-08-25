import asyncio
from dataclasses import replace

from app.api import create_app
from app.backend import NewsCategory, PublishedNews
from app.draft_store import DraftStore
from app.models import NewsDraft
from app.moderation_db import ModerationDB
from app.notifier import (
    REJECT_DRAFT_PREFIX,
    SELECT_CATEGORY_PREFIX,
    TelegramNotificationError,
    TelegramNotifier,
)
from app.webhook_service import TelegramWebhookService, extract_webhook_url
from tests.test_store_and_api import make_settings, request


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
    claimed = database.claim("new", "parsing")
    assert len(claimed) == 1
    assert claimed[0].status == "parsing"
    assert claimed[0].attempts == 1
    assert database.claim("new", "parsing") == []
    updated = database.update(first.id, status="awaiting_category", category_id=11)
    assert updated.status == "awaiting_category"
    assert updated.category_id == 11


def test_moderation_db_reclaims_only_stale_active_job(tmp_path) -> None:
    database = ModerationDB(tmp_path / "state.db")
    post = database.enqueue(
        chat_id="-1001",
        message_id=43,
        source_url="https://example.kz/news/43",
        raw_text="https://example.kz/news/43",
        draft_id="r" * 32,
    )
    first = database.claim("new", "parsing")[0]

    assert database.claim("new", "parsing", stale_minutes=15) == []
    with database._connection() as connection:
        connection.execute(
            "UPDATE posts SET updated_at = datetime('now', '-16 minutes') WHERE id = ?",
            (post.id,),
        )
    reclaimed = database.claim("new", "parsing", stale_minutes=15)

    assert len(reclaimed) == 1
    assert reclaimed[0].id == first.id
    assert reclaimed[0].attempts == 2


def test_moderation_db_purges_old_rows_in_any_status(tmp_path) -> None:
    database = ModerationDB(tmp_path / "state.db")
    post = database.enqueue(
        chat_id="-1001",
        message_id=44,
        source_url="https://example.kz/news/44",
        raw_text="https://example.kz/news/44",
        draft_id="o" * 32,
    )
    database.update(post.id, status="awaiting_category")
    with database._connection() as connection:
        connection.execute(
            "UPDATE posts SET updated_at = datetime('now', '-31 days') WHERE id = ?",
            (post.id,),
        )

    assert database.purge_old(30) == 1
    assert database.get(post.id) is None


def test_webhook_requires_secret_and_delivers_update(tmp_path) -> None:
    settings = replace(
        make_settings(tmp_path),
        tg_webhook_secret="webhook-secret",
    )
    store = DraftStore(settings.drafts_dir, settings.photos_dir, 24)
    received = []

    async def handler(payload):
        received.append(payload)

    app = create_app(store, settings, webhook_handler=handler)
    payload = {"update_id": 1, "channel_post": {"message_id": 2}}

    assert request(app, "POST", "/webhook", json=payload).status_code == 403
    accepted = request(
        app,
        "POST",
        "/webhook",
        json=payload,
        headers={"X-Telegram-Bot-Api-Secret-Token": "webhook-secret"},
    )

    assert accepted.status_code == 200
    assert received == [payload]


def test_webhook_rejects_oversized_or_invalid_json(tmp_path) -> None:
    settings = replace(
        make_settings(tmp_path),
        webhook_max_body_bytes=1024,
    )
    store = DraftStore(settings.drafts_dir, settings.photos_dir, 24)

    async def handler(payload):
        raise AssertionError(f"unexpected payload: {payload}")

    app = create_app(store, settings, webhook_handler=handler)
    headers = {"X-Telegram-Bot-Api-Secret-Token": settings.tg_webhook_secret}

    assert (
        request(app, "POST", "/webhook", content=b"{", headers=headers).status_code
        == 400
    )
    assert (
        request(
            app,
            "POST",
            "/webhook",
            content=b"x" * 1025,
            headers={**headers, "content-type": "application/json"},
        ).status_code
        == 413
    )


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


def test_channel_photo_file_id_is_saved_with_queue_job(tmp_path) -> None:
    settings = make_settings(tmp_path)
    store = DraftStore(settings.drafts_dir, settings.photos_dir, 24)
    service = TelegramWebhookService(settings, store)

    asyncio.run(
        service.handle_update(
            {
                "channel_post": {
                    "message_id": 77,
                    "chat": {"id": int(settings.telegram_channel_id)},
                    "caption": "https://example.kz/news/77",
                    "photo": [
                        {"file_id": "small"},
                        {"file_id": "largest"},
                    ],
                }
            }
        )
    )

    queued = service.db.claim("new", "parsing")
    assert len(queued) == 1
    assert queued[0].telegram_photo_file_id == "largest"


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
        categories=categories,
    )["inline_keyboard"]
    buttons = [button for row in keyboard for button in row]

    assert buttons[1]["text"] == "✓ Экология"
    assert buttons[1]["callback_data"] == (
        f"{SELECT_CATEGORY_PREFIX}{draft.draft_id}:11"
    )
    assert buttons[-1]["callback_data"] == (f"{REJECT_DRAFT_PREFIX}{draft.draft_id}")


def test_publication_stays_sent_when_telegram_status_update_fails(
    tmp_path,
    monkeypatch,
) -> None:
    settings = make_settings(tmp_path)
    store = DraftStore(settings.drafts_dir, settings.photos_dir, 24)
    service = TelegramWebhookService(settings, store)
    draft = NewsDraft.create(
        draft_id="p" * 32,
        title="Опубликованная новость",
        text="Текст",
        category_id=35,
        source_message_id=88,
        source_url="https://example.kz/news/88",
    )
    store.save(draft)
    post = service.db.enqueue(
        chat_id=settings.telegram_channel_id,
        message_id=88,
        source_url=draft.source_url,
        raw_text=draft.source_url,
        draft_id=draft.draft_id,
    )
    service.db.update(post.id, status="sending", prompt_message_id=900)
    row = service.db.claim("sending", "publishing")[0]

    async def publish(_draft):
        return PublishedNews(501, "published", "https://dev.nedra.kz/news/501")

    async def fail_status(**kwargs):
        del kwargs
        raise TelegramNotificationError("Telegram API editMessageText failed")

    monkeypatch.setattr(service.backend, "publish", publish)
    monkeypatch.setattr(service.notifier, "edit_status_message", fail_status)

    asyncio.run(service._publish(row))

    saved = service.db.get(post.id)
    assert saved.status == "sent"
    assert saved.backend_news_id == 501


def test_callback_is_denied_for_user_outside_admin_allowlist(
    tmp_path,
    monkeypatch,
) -> None:
    settings = make_settings(tmp_path)
    store = DraftStore(settings.drafts_dir, settings.photos_dir, 24)
    service = TelegramWebhookService(settings, store)
    answers = []

    async def answer(callback_id, text):
        answers.append((callback_id, text))

    monkeypatch.setattr(service.notifier, "answer_callback", answer)
    callback = {
        "id": "callback-1",
        "data": f"{SELECT_CATEGORY_PREFIX}{'a' * 32}:35",
        "from": {"id": 999999999},
        "message": {
            "message_id": 10,
            "chat": {"id": int(settings.notify_chat_id)},
        },
    }

    asyncio.run(service._handle_callback(callback))

    assert answers == [("callback-1", "Недостаточно прав")]
