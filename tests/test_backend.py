import asyncio
from types import SimpleNamespace

import pytest

from app.backend import BackendAPIError, BackendNewsPublisher, NewsCategory, _lead
from app.draft_store import DraftStore
from app.models import NewsDraft


class Response:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


def _publisher(tmp_path, *, public_api_base="http://localhost:8000"):
    settings = SimpleNamespace(
        news_bot_api_base="https://dev.nedra.kz/api/internal",
        news_bot_api_token="secret-token",
        telegram_channel_id="-100123",
        public_api_base=public_api_base,
        media_signing_secret="m" * 40,
    )
    store = DraftStore(tmp_path / "drafts", tmp_path / "photos", 24)
    return BackendNewsPublisher(settings, store), store


def test_backend_categories_are_loaded_and_cached(tmp_path, monkeypatch) -> None:
    publisher, _ = _publisher(tmp_path)
    calls = []

    def get(url, **kwargs):
        calls.append((url, kwargs))
        return Response(
            200,
            {"data": [{"id": 35, "name": "Недропользование", "slug": None}]},
        )

    monkeypatch.setattr("app.backend.requests.get", get)

    first = asyncio.run(publisher.categories())
    second = asyncio.run(publisher.categories())

    assert first == second == (NewsCategory(35, "Недропользование", None),)
    assert len(calls) == 1
    assert calls[0][1]["headers"]["Authorization"] == "Bearer secret-token"


def test_backend_payload_uses_idempotency_and_safe_html(tmp_path) -> None:
    publisher, _ = _publisher(tmp_path)
    draft = NewsDraft.create(
        draft_id="a" * 32,
        title="Заголовок",
        text="Первый <абзац>\n\nВторой\nряд",
        category_id=11,
        source_message_id=42,
        source_url="https://www.example.kz/news/42",
        source_image_url="https://cdn.example.kz/photo.jpg",
    )

    payload = publisher._payload(draft)

    assert payload["external_id"] == "tg:-100123:42"
    assert payload["category_id"] == 11
    assert payload["content_html"] == (
        "<p>Первый &lt;абзац&gt;</p><p>Второй<br>ряд</p>"
    )
    assert payload["source_name"] == "example.kz"
    # External image URLs are not delegated to the backend. The parser downloads
    # a validated copy and only that local copy is exposed through a signed URL.
    assert payload["image_url"] is None


def test_backend_lead_contains_only_first_sentence() -> None:
    text = (
        "Первое предложение кратко описывает новость. "
        "Второе предложение уже содержит подробности."
    )

    assert _lead(text) == "Первое предложение кратко описывает новость."


def test_backend_lead_normalizes_whitespace_and_closing_quote() -> None:
    text = "  Власти сообщили: \u00abРешение принято!\u00bb\n\nДалее идут подробности.  "

    assert _lead(text) == "Власти сообщили: \u00abРешение принято!\u00bb"


def test_backend_lead_limits_unfinished_long_text() -> None:
    lead = _lead("слово " * 100)

    assert lead is not None
    assert len(lead) <= 300
    assert lead.endswith("…")


def test_backend_refuses_to_silently_drop_local_photo(tmp_path) -> None:
    publisher, store = _publisher(tmp_path)
    draft = NewsDraft.create(
        draft_id="b" * 32,
        title="Заголовок",
        text="Текст",
        category_id=35,
        source_message_id=43,
        source_url="https://example.kz/news/43",
        photo_filename="photo.jpg",
    )
    store.save(draft)
    (store.photos_dir / "photo.jpg").write_bytes(b"jpg")

    with pytest.raises(BackendAPIError, match="only available locally"):
        publisher._payload(draft)


def test_backend_uses_signed_public_photo_url(tmp_path) -> None:
    publisher, store = _publisher(
        tmp_path,
        public_api_base="https://dev.nedra.kz/tg",
    )
    draft = NewsDraft.create(
        draft_id="d" * 32,
        title="Заголовок",
        text="Текст",
        category_id=35,
        source_message_id=45,
        source_url="https://example.kz/news/45",
        photo_filename="photo.jpg",
    )
    store.save(draft)
    (store.photos_dir / "photo.jpg").write_bytes(b"jpg")

    image_url = publisher._payload(draft)["image_url"]

    assert image_url.startswith(
        f"https://dev.nedra.kz/tg/photo/{draft.draft_id}?token="
    )
    assert "m" * 40 not in image_url


def test_backend_validation_error_is_not_retryable(tmp_path, monkeypatch) -> None:
    publisher, _ = _publisher(tmp_path)
    draft = NewsDraft.create(
        draft_id="c" * 32,
        title="Заголовок",
        text="Текст",
        category_id=35,
        source_message_id=44,
        source_url="https://example.kz/news/44",
    )
    calls = 0

    def post(*args, **kwargs):
        nonlocal calls
        calls += 1
        return Response(422, {"errors": {"title": ["invalid"]}})

    monkeypatch.setattr("app.backend.requests.post", post)

    with pytest.raises(BackendAPIError, match="title: invalid"):
        asyncio.run(publisher.publish(draft))
    assert calls == 1
