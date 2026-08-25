import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.api import create_app
from app.config import Settings
from app.draft_store import DraftStore
from app.models import NewsDraft, media_access_token
from app.moderation_db import ModerationDB


def request(app, method: str, path: str, **kwargs):
    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            return await client.request(method, path, **kwargs)

    return asyncio.run(run())


def make_settings(tmp_path) -> Settings:
    return Settings(
        telegram_channel_id="-1001234567890",
        telegram_admin_user_ids=(123456789,),
        bot_token="123456:test-bot-token",
        notify_chat_id="-1001234567891",
        tg_webhook_secret="w" * 40,
        telegram_webhook_enforce_ips=False,
        news_bot_api_base="https://dev.nedra.kz/api/internal",
        news_bot_api_token="b" * 40,
        public_api_base="http://localhost:8000",
        media_signing_secret="m" * 40,
        allow_insecure_http=True,
        data_dir=tmp_path,
    )


def test_photo_requires_valid_signature(tmp_path) -> None:
    settings = make_settings(tmp_path)
    store = DraftStore(settings.drafts_dir, settings.photos_dir, 24)
    draft_id = "a" * 32
    (settings.photos_dir / f"{draft_id}.jpg").write_bytes(b"jpeg")
    store.save(
        NewsDraft.create(
            draft_id=draft_id,
            title="Заголовок",
            text="Текст",
            category_id=35,
            source_message_id=10,
            source_url="https://example.com/news",
            photo_filename=f"{draft_id}.jpg",
        )
    )
    app = create_app(store, settings)
    token = media_access_token(draft_id, settings.media_signing_secret)

    assert request(app, "GET", f"/photo/{draft_id}").status_code == 404
    assert request(app, "GET", f"/photo/{draft_id}?token=invalid").status_code == 404
    photo = request(app, "GET", f"/photo/{draft_id}?token={token}")

    assert photo.status_code == 200
    assert photo.headers["cache-control"] == "private, max-age=300"
    assert photo.content == b"jpeg"


def test_legacy_draft_endpoint_is_not_exposed(tmp_path) -> None:
    settings = make_settings(tmp_path)
    store = DraftStore(settings.drafts_dir, settings.photos_dir, 24)

    assert (
        request(
            create_app(store, settings),
            "GET",
            f"/draft/{'a' * 32}",
        ).status_code
        == 404
    )


def test_expired_photo_returns_404_and_is_deleted(tmp_path) -> None:
    settings = make_settings(tmp_path)
    store = DraftStore(settings.drafts_dir, settings.photos_dir, 24)
    draft_id = "b" * 32
    photo_path = settings.photos_dir / f"{draft_id}.jpg"
    photo_path.write_bytes(b"jpeg")
    expired = replace(
        NewsDraft.create(
            draft_id=draft_id,
            title="Старый",
            text="Текст",
            category_id=35,
            source_message_id=1,
            photo_filename=photo_path.name,
        ),
        created_at=(datetime.now(UTC) - timedelta(hours=25)).isoformat(),
    )
    store.save(expired)
    token = media_access_token(draft_id, settings.media_signing_secret)

    response = request(
        create_app(store, settings),
        "GET",
        f"/photo/{draft_id}?token={token}",
    )

    assert response.status_code == 404
    assert not photo_path.exists()


def test_store_finds_existing_draft_for_tracking_url_variant(tmp_path) -> None:
    settings = make_settings(tmp_path)
    store = DraftStore(settings.drafts_dir, settings.photos_dir, 24)
    draft = NewsDraft.create(
        draft_id="e" * 32,
        title="Существующая новость",
        text="Текст",
        category_id=13,
        source_message_id=3,
        source_url="https://example.com/news/42",
    )
    store.save(draft)

    found = store.find_by_source_url(
        "https://EXAMPLE.com/news/42/?utm_source=telegram#preview"
    )

    assert found is not None
    assert found.draft_id == draft.draft_id


def test_store_does_not_return_expired_duplicate(tmp_path) -> None:
    settings = make_settings(tmp_path)
    store = DraftStore(settings.drafts_dir, settings.photos_dir, 24)
    draft = NewsDraft.create(
        draft_id="f" * 32,
        title="Старая новость",
        text="Текст",
        category_id=35,
        source_message_id=4,
        source_url="https://example.com/old-news",
    )
    store.save(
        replace(
            draft,
            created_at=(datetime.now(UTC) - timedelta(hours=25)).isoformat(),
        )
    )

    assert store.find_by_source_url("https://example.com/old-news") is None


def test_health_reports_moderation_queue(tmp_path) -> None:
    settings = make_settings(tmp_path)
    store = DraftStore(settings.drafts_dir, settings.photos_dir, 24)
    database = ModerationDB(settings.moderation_db_file)
    database.enqueue(
        chat_id=settings.telegram_channel_id,
        message_id=700,
        source_url="https://example.com/news",
        raw_text="https://example.com/news",
        draft_id="g" * 32,
    )

    response = request(
        create_app(store, settings, webhook_stats=database.stats),
        "GET",
        "/health",
    )

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "moderation_queue": {"new": 1},
    }


def test_runtime_validation_requires_admin_and_https(tmp_path) -> None:
    settings = replace(
        make_settings(tmp_path),
        telegram_admin_user_ids=(),
        allow_insecure_http=False,
    )
    with pytest.raises(ValueError, match="TG_ADMIN_USER_IDS"):
        settings.validate_runtime()

    insecure = replace(
        make_settings(tmp_path),
        public_api_base="http://public.example/tg",
        allow_insecure_http=False,
    )
    with pytest.raises(ValueError, match="PUBLIC_API_BASE must use HTTPS"):
        insecure.validate_runtime()
