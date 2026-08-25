import asyncio
from pathlib import Path
from types import SimpleNamespace

import app.telegram_listener as listener_module
from app.draft_store import DraftStore
from app.image_service import ImageGenerationError
from app.models import NewsDraft
from app.notifier import REGENERATE_PHOTO_PREFIX
from app.processing_queue import ProcessingQueue
from app.state import ProcessingState
from app.telegram_listener import TelegramListener


class Message:
    def __init__(
        self, text: str, message_id: int, grouped_id: int | None = None
    ) -> None:
        self.raw_text = text
        self.id = message_id
        self.grouped_id = grouped_id


class RecordingNotifier:
    def __init__(self) -> None:
        self.existing_calls: list[tuple[NewsDraft, str]] = []

    async def send_existing_draft(
        self, draft: NewsDraft, autofill_link: str
    ) -> None:
        self.existing_calls.append((draft, autofill_link))


def test_duplicate_message_reuses_draft_without_rebuilding(tmp_path, monkeypatch) -> None:
    drafts_dir = tmp_path / "drafts"
    photos_dir = tmp_path / "photos"
    store = DraftStore(drafts_dir, photos_dir, 24)
    existing = NewsDraft.create(
        draft_id="a" * 32,
        title="Готовый черновик",
        text="Текст новости",
        category_id=13,
        source_message_id=10,
        source_url="https://example.com/news/42",
    )
    store.save(existing)

    async def fail_if_called(*args, **kwargs):
        raise AssertionError("build_draft must not run for a duplicate URL")

    monkeypatch.setattr(listener_module, "build_draft", fail_if_called)

    notifier = RecordingNotifier()
    listener = object.__new__(TelegramListener)
    listener.settings = SimpleNamespace(
        admin_base_url="https://dev.nedra.kz/admin/news",
        photos_dir=photos_dir,
    )
    listener.store = store
    listener.state = ProcessingState(tmp_path / "state.json")
    listener.notifier = notifier
    listener.process_lock = asyncio.Lock()

    asyncio.run(
        listener._process_messages(
            [
                Message(
                    "https://EXAMPLE.com/news/42/?utm_source=telegram#preview",
                    20,
                )
            ]
        )
    )

    assert listener.state.load_last_processed_id() == 20
    assert len(notifier.existing_calls) == 1
    returned_draft, returned_link = notifier.existing_calls[0]
    assert returned_draft.draft_id == existing.draft_id
    assert returned_link.endswith(f"?af_draft_id={existing.draft_id}")
    assert len(list(Path(drafts_dir).glob("*.json"))) == 1


def test_retry_resends_saved_job_draft_without_rebuilding(tmp_path, monkeypatch) -> None:
    photos_dir = tmp_path / "photos"
    store = DraftStore(tmp_path / "drafts", photos_dir, 24)
    saved = NewsDraft.create(
        draft_id="d" * 32,
        title="Уже построенный черновик",
        text="Текст",
        category_id=35,
        source_message_id=30,
    )
    store.save(saved)

    async def fail_if_called(*args, **kwargs):
        raise AssertionError("build_draft must not rerun for a saved job draft")

    monkeypatch.setattr(listener_module, "build_draft", fail_if_called)

    class Notifier:
        def __init__(self):
            self.calls = []

        async def send_draft_for_confirmation(self, draft, link):
            self.calls.append((draft, link))

    notifier = Notifier()
    listener = object.__new__(TelegramListener)
    listener.settings = SimpleNamespace(
        admin_base_url="https://dev.nedra.kz/admin/news",
        photos_dir=photos_dir,
    )
    listener.store = store
    listener.state = ProcessingState(tmp_path / "state.json")
    listener.notifier = notifier
    listener.process_lock = asyncio.Lock()

    result = asyncio.run(
        listener._process_messages(
            [Message("Новость", 30)],
            draft_id=saved.draft_id,
        )
    )

    assert result.draft_id == saved.draft_id
    assert len(notifier.calls) == 1
    assert listener.state.load_last_processed_id() == 30


def test_regenerate_callback_replaces_ai_photo_and_increments_revision(tmp_path) -> None:
    photos_dir = tmp_path / "photos"
    store = DraftStore(tmp_path / "drafts", photos_dir, 24)
    draft = NewsDraft.create(
        draft_id="b" * 32,
        title="Технологическая новость",
        text="Подробный текст новости",
        category_id=9,
        source_message_id=11,
        photo_filename=f"{'b' * 32}.jpg",
        photo_is_generated=True,
        photo_revision=1,
    )
    store.save(draft)
    photo_path = photos_dir / draft.photo_filename
    photo_path.write_bytes(b"old-photo")

    class ImageService:
        async def generate_cover(
            self, *, title, news_text, target, regenerate=False
        ):
            assert title == draft.title
            assert news_text == draft.text
            assert regenerate is True
            target.write_bytes(b"new-photo")
            return target.name

    class Notifier:
        def __init__(self):
            self.answers = []
            self.updates = []

        async def answer_callback(self, callback_id, text):
            self.answers.append((callback_id, text))

        async def update_draft_message(self, **kwargs):
            self.updates.append(kwargs)

    notifier = Notifier()
    listener = object.__new__(TelegramListener)
    listener.settings = SimpleNamespace(
        notify_chat_id="123",
        admin_base_url="https://dev.nedra.kz/admin/news",
        photos_dir=photos_dir,
    )
    listener.store = store
    listener.notifier = notifier
    listener.image_service = ImageService()
    listener.regenerating_draft_ids = set()
    listener.regeneration_tasks = set()

    async def run_callback():
        await listener._start_callback_action(
            {
                "id": "callback-1",
                "data": f"{REGENERATE_PHOTO_PREFIX}{draft.draft_id}",
                "message": {"message_id": 77, "chat": {"id": 123}},
            }
        )
        await asyncio.gather(*list(listener.regeneration_tasks))

    asyncio.run(run_callback())

    updated = store.get(draft.draft_id)
    assert updated is not None
    assert updated.photo_revision == 2
    assert photo_path.read_bytes() == b"new-photo"
    assert notifier.answers == [("callback-1", "Начинаю генерацию нового фото")]
    assert notifier.updates[-1]["status"] == "Новое AI-фото готово"


def test_failed_regeneration_keeps_previous_photo(tmp_path) -> None:
    photos_dir = tmp_path / "photos"
    store = DraftStore(tmp_path / "drafts", photos_dir, 24)
    draft = NewsDraft.create(
        draft_id="c" * 32,
        title="Новость",
        text="Текст",
        category_id=35,
        source_message_id=12,
        photo_filename=f"{'c' * 32}.jpg",
        photo_is_generated=True,
        photo_revision=3,
    )
    store.save(draft)
    photo_path = photos_dir / draft.photo_filename
    photo_path.write_bytes(b"previous-photo")

    class FailingImageService:
        async def generate_cover(self, **kwargs):
            del kwargs
            raise ImageGenerationError("temporary failure")

    class Notifier:
        def __init__(self):
            self.updates = []

        async def answer_callback(self, callback_id, text):
            del callback_id, text

        async def update_draft_message(self, **kwargs):
            self.updates.append(kwargs)

    notifier = Notifier()
    listener = object.__new__(TelegramListener)
    listener.settings = SimpleNamespace(
        notify_chat_id="123",
        admin_base_url="https://dev.nedra.kz/admin/news",
        photos_dir=photos_dir,
    )
    listener.store = store
    listener.notifier = notifier
    listener.image_service = FailingImageService()
    listener.regenerating_draft_ids = set()
    listener.regeneration_tasks = set()

    async def run_callback():
        await listener._start_callback_action(
            {
                "id": "callback-2",
                "data": f"{REGENERATE_PHOTO_PREFIX}{draft.draft_id}",
                "message": {"message_id": 78, "chat": {"id": 123}},
            }
        )
        await asyncio.gather(*list(listener.regeneration_tasks))

    asyncio.run(run_callback())

    unchanged = store.get(draft.draft_id)
    assert unchanged is not None
    assert unchanged.photo_revision == 3
    assert photo_path.read_bytes() == b"previous-photo"
    assert notifier.updates[-1]["status"] == (
        "Не удалось обновить фото — прежнее сохранено"
    )


def test_five_fast_posts_are_persisted_and_claimed_in_id_order(tmp_path) -> None:
    listener = object.__new__(TelegramListener)
    listener.queue = ProcessingQueue(tmp_path / "queue", settle_seconds=0)
    listener.queue_wakeup = asyncio.Event()
    listener.album_tasks = {}

    async def enqueue_burst():
        for message_id in [805, 801, 804, 802, 803]:
            await listener._on_new_message(
                SimpleNamespace(message=Message("Новость", message_id))
            )

    asyncio.run(enqueue_burst())

    claimed_ids = []
    for _ in range(5):
        job = listener.queue.claim_next_ready()
        assert job is not None
        claimed_ids.append(job.message_ids[0])
        listener.queue.mark_completed(job.job_id)

    assert claimed_ids == [801, 802, 803, 804, 805]


def test_startup_catchup_groups_album_and_standalone_news(tmp_path) -> None:
    state = ProcessingState(tmp_path / "state.json")
    state.save_last_processed_id(900)

    class HistoryClient:
        def iter_messages(self, channel, *, min_id, reverse):
            assert channel == "@channel"
            assert min_id == 900
            assert reverse is True

            async def generate():
                yield Message("Одиночная новость", 901)
                yield Message("Подпись альбома", 902, grouped_id=12345)
                yield Message("", 903, grouped_id=12345)

            return generate()

    listener = object.__new__(TelegramListener)
    listener.settings = SimpleNamespace(telegram_channel="@channel")
    listener.state = state
    listener.queue = ProcessingQueue(tmp_path / "queue", settle_seconds=0)
    listener.queue_wakeup = asyncio.Event()
    listener.client = HistoryClient()

    recovered = asyncio.run(listener._catch_up_channel_history())

    assert recovered == 2
    first = listener.queue.claim_next_ready()
    assert first is not None
    assert first.message_ids == [901]
    listener.queue.mark_completed(first.job_id)
    second = listener.queue.claim_next_ready()
    assert second is not None
    assert second.message_ids == [902, 903]


def test_first_start_sets_baseline_without_importing_old_channel(tmp_path) -> None:
    class HistoryClient:
        async def get_messages(self, channel, *, limit):
            assert channel == "@channel"
            assert limit == 1
            return [Message("Последняя старая новость", 999)]

    listener = object.__new__(TelegramListener)
    listener.settings = SimpleNamespace(telegram_channel="@channel")
    listener.state = ProcessingState(tmp_path / "state.json")
    listener.queue = ProcessingQueue(tmp_path / "queue", settle_seconds=0)
    listener.queue_wakeup = asyncio.Event()
    listener.client = HistoryClient()

    recovered = asyncio.run(listener._catch_up_channel_history())

    assert recovered == 0
    assert listener.state.load_last_processed_id() == 999
    assert listener.queue.stats()["pending"] == 0


def test_bot_session_skips_unsupported_history_without_crashing(
    tmp_path, caplog
) -> None:
    from telethon.errors import BotMethodInvalidError

    state = ProcessingState(tmp_path / "state.json")
    state.save_last_processed_id(900)

    class BotHistoryClient:
        def iter_messages(self, channel, *, min_id, reverse):
            async def generate():
                raise BotMethodInvalidError(request=None)
                yield

            return generate()

    listener = object.__new__(TelegramListener)
    listener.settings = SimpleNamespace(telegram_channel="@channel")
    listener.state = state
    listener.queue = ProcessingQueue(tmp_path / "queue", settle_seconds=0)
    listener.queue_wakeup = asyncio.Event()
    listener.client = BotHistoryClient()

    recovered = asyncio.run(listener._catch_up_channel_history())

    assert recovered == 0
    assert listener.queue.stats()["pending"] == 0
    assert "history catch-up is unavailable for bot sessions" in caplog.text
