from dataclasses import replace
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.api import create_app
from app.config import Settings
from app.draft_store import DraftStore
from app.models import NewsDraft
from app.processing_queue import ProcessingQueue
from app.publication_queue import PublicationQueue


def make_settings(tmp_path) -> Settings:
    return Settings(
        tg_api_id=None,
        tg_api_hash="",
        tg_session_name="test",
        telegram_channel="",
        bot_token="",
        notify_chat_id="",
        admin_base_url="https://dev.nedra.kz/admin/news",
        api_host="127.0.0.1",
        api_port=8000,
        public_api_base="http://localhost:8000",
        draft_ttl_hours=24,
        album_wait_seconds=2,
        image_fallback_mode="disabled",
        openai_api_key="",
        openai_image_model="gpt-image-1-mini",
        openai_image_quality="medium",
        openai_image_size="1536x1024",
        category_classifier_mode="disabled",
        openai_text_model="gpt-5.4-nano",
        cors_origins=("https://dev.nedra.kz",),
        data_dir=tmp_path,
    )


def test_draft_api_and_photo(tmp_path) -> None:
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
    client = TestClient(create_app(store, settings))

    response = client.get(f"/draft/{draft_id}")
    assert response.status_code == 200
    assert response.json() == {
        "title": "Заголовок",
        "text": "Текст",
        "category_id": 35,
        "source_url": "https://example.com/news",
        "photo_url": f"http://localhost:8000/photo/{draft_id}",
    }

    photo = client.get(f"/photo/{draft_id}")
    assert photo.status_code == 200
    assert photo.headers["access-control-allow-origin"] == "*"
    assert photo.content == b"jpeg"


def test_missing_and_expired_drafts_return_404(tmp_path) -> None:
    settings = make_settings(tmp_path)
    store = DraftStore(settings.drafts_dir, settings.photos_dir, 24)
    draft_id = "b" * 32
    expired = NewsDraft.create(
        draft_id=draft_id,
        title="Старый",
        text="Текст",
        category_id=None,
        source_message_id=1,
    )
    expired = replace(
        expired,
        created_at=(datetime.now(timezone.utc) - timedelta(hours=25)).isoformat(),
    )
    store.save(expired)
    client = TestClient(create_app(store, settings))

    assert client.get(f"/draft/{draft_id}").status_code == 404
    assert client.get(f"/draft/{'c' * 32}").status_code == 404


def test_draft_api_fills_default_category_for_older_draft(tmp_path) -> None:
    settings = make_settings(tmp_path)
    store = DraftStore(settings.drafts_dir, settings.photos_dir, 24)
    draft_id = "d" * 32
    store.save(
        NewsDraft.create(
            draft_id=draft_id,
            title="Новость без распознанной категории",
            text="Обычный текст",
            category_id=None,
            source_message_id=2,
        )
    )

    response = TestClient(create_app(store, settings)).get(f"/draft/{draft_id}")

    assert response.status_code == 200
    assert response.json()["category_id"] == 35


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
            created_at=(datetime.now(timezone.utc) - timedelta(hours=25)).isoformat(),
        )
    )

    assert store.find_by_source_url("https://example.com/old-news") is None


def test_health_reports_persistent_queue_state(tmp_path) -> None:
    settings = make_settings(tmp_path)
    store = DraftStore(settings.drafts_dir, settings.photos_dir, 24)
    queue = ProcessingQueue(settings.queue_dir, settle_seconds=0)
    queue.enqueue([700])

    response = TestClient(create_app(store, settings, queue)).get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "queue": {
            "pending": 1,
            "processing": 0,
            "completed": 0,
            "failed": 0,
        },
    }


def test_userscript_reports_publication_result_with_token(tmp_path) -> None:
    settings = make_settings(tmp_path)
    store = DraftStore(settings.drafts_dir, settings.photos_dir, 24)
    publication_queue = PublicationQueue(settings.publication_queue_dir)
    job = publication_queue.enqueue("g" * 32)
    publication_queue.claim_next()
    client = TestClient(
        create_app(
            store,
            settings,
            publication_queue=publication_queue,
        )
    )

    invalid = client.post(
        f"/publication/{job.draft_id}/result",
        json={"token": "x" * 24, "success": True},
    )
    completed = client.post(
        f"/publication/{job.draft_id}/result",
        json={"token": job.token, "success": True},
    )

    assert invalid.status_code == 403
    assert completed.status_code == 200
    assert completed.json() == {"status": "completed"}
    assert publication_queue.get(job.draft_id).status == "completed"
