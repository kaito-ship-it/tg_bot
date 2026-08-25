import asyncio
from types import SimpleNamespace

from app.auto_publisher import AutoPublisher
from app.draft_store import DraftStore
from app.models import NewsDraft
from app.playwright_publisher import PlaywrightAdminPublisher
from app.publication_queue import PublicationQueue


def _saved_draft(store: DraftStore, draft_id: str) -> NewsDraft:
    draft = NewsDraft.create(
        draft_id=draft_id,
        title="Автоматическая публикация",
        text="Текст новости",
        category_id=35,
        source_message_id=100,
    )
    store.save(draft)
    return draft


def test_publication_queue_validates_token_and_prevents_duplicate(tmp_path) -> None:
    queue = PublicationQueue(tmp_path / "publication_queue")
    draft_id = "a" * 32

    first = queue.enqueue(draft_id)
    second = queue.enqueue(draft_id)

    assert second.token == first.token
    assert queue.claim_next().draft_id == draft_id
    assert queue.report_result(draft_id, "wrong-token-value-12345", success=True) is None
    completed = queue.report_result(draft_id, first.token, success=True)
    assert completed is not None
    assert completed.status == "completed"
    assert queue.claim_next() is None


def test_interrupted_submission_requires_manual_review(tmp_path) -> None:
    queue = PublicationQueue(tmp_path / "publication_queue")
    job = queue.enqueue("b" * 32)
    queue.claim_next()

    assert queue.recover_interrupted() == 1
    recovered = queue.get(job.draft_id)
    assert recovered is not None
    assert recovered.status == "failed"
    assert "check the site" in recovered.last_error

    retried = queue.retry_failed(job.draft_id)
    assert retried is not None
    assert retried.status == "pending"
    assert retried.last_error is None


def test_auto_publisher_waits_for_browser_confirmation(tmp_path) -> None:
    store = DraftStore(tmp_path / "drafts", tmp_path / "photos", 24)
    draft = _saved_draft(store, "c" * 32)
    queue = PublicationQueue(tmp_path / "publication_queue")
    opened_urls = []

    class Notifier:
        def __init__(self):
            self.results = []

        async def send_publication_result(self, **kwargs):
            self.results.append(kwargs)

    notifier = Notifier()
    publisher = AutoPublisher(
        SimpleNamespace(
            admin_base_url="https://dev.nedra.kz/admin/news",
            browser_command="",
            auto_publish_timeout_seconds=15,
        ),
        store,
        queue,
        notifier,
        opener=lambda url: opened_urls.append(url) or True,
    )
    link = publisher.schedule(draft.draft_id)
    job = queue.claim_next()
    assert job is not None

    async def confirm_from_userscript():
        task = asyncio.create_task(publisher._process(job))
        while not opened_urls:
            await asyncio.sleep(0.01)
        queue.report_result(draft.draft_id, job.token, success=True)
        await task

    asyncio.run(confirm_from_userscript())

    assert f"af_publish_token={job.token}" in link
    assert opened_urls == [link]
    assert notifier.results[-1]["success"] is True


def test_playwright_mode_marks_job_completed_without_browser_callback(tmp_path) -> None:
    store = DraftStore(tmp_path / "drafts", tmp_path / "photos", 24)
    draft = _saved_draft(store, "d" * 32)
    queue = PublicationQueue(tmp_path / "publication_queue")

    class Notifier:
        def __init__(self):
            self.results = []

        async def send_publication_result(self, **kwargs):
            self.results.append(kwargs)

    class PlaywrightPublisher:
        def __init__(self):
            self.published = []

        async def publish(self, item):
            self.published.append(item.draft_id)

        async def close(self):
            pass

    notifier = Notifier()
    publisher = AutoPublisher(
        SimpleNamespace(
            publish_mode="playwright",
            admin_base_url="https://dev.nedra.kz/admin/news",
            browser_command="",
            auto_publish_timeout_seconds=15,
        ),
        store,
        queue,
        notifier,
    )
    fake_playwright = PlaywrightPublisher()
    publisher._playwright_publisher = fake_playwright
    publisher.schedule(draft.draft_id)
    job = queue.claim_next()

    asyncio.run(publisher._process(job))

    assert fake_playwright.published == [draft.draft_id]
    assert queue.get(draft.draft_id).status == "completed"
    assert notifier.results[-1]["success"] is True


def test_backend_category_click_retries_failed_idempotent_job(tmp_path) -> None:
    store = DraftStore(tmp_path / "drafts", tmp_path / "photos", 24)
    draft = _saved_draft(store, "e" * 32)
    queue = PublicationQueue(tmp_path / "publication_queue")

    class Notifier:
        async def send_publication_result(self, **kwargs):
            del kwargs

    settings = SimpleNamespace(
        publish_mode="backend_api",
        admin_base_url="https://dev.nedra.kz/admin/news",
        browser_command="",
        auto_publish_timeout_seconds=15,
        news_bot_api_base="https://dev.nedra.kz/api/internal",
        news_bot_api_token="token",
    )
    publisher = AutoPublisher(settings, store, queue, Notifier())
    queue.enqueue(draft.draft_id)
    queue.claim_next()
    queue.mark_failed(draft.draft_id, "temporary error")

    publisher.schedule(draft.draft_id)

    assert queue.get(draft.draft_id).status == "pending"
    assert publisher.wakeup.is_set()


def test_document_removal_uses_live_locator_after_dom_replacement() -> None:
    class LiveButtons:
        def __init__(self, page):
            self.page = page

        async def count(self):
            return self.page.remaining

        @property
        def first(self):
            return self

        async def click(self):
            self.page.delete_pending = True
            self.page.wait_ticks = 0

    class Page:
        def __init__(self):
            self.remaining = 2
            self.selectors = []
            self.delete_pending = False
            self.wait_ticks = 0

        def locator(self, selector):
            self.selectors.append(selector)
            return LiveButtons(self)

        async def wait_for_timeout(self, milliseconds):
            assert milliseconds == 100
            if self.delete_pending:
                self.wait_ticks += 1
                if self.wait_ticks == 2:
                    self.remaining -= 1
                    self.delete_pending = False

    publisher = object.__new__(PlaywrightAdminPublisher)
    page = Page()

    removed = asyncio.run(publisher._remove_documents(page))

    assert removed == 2
    assert page.remaining == 0
    assert set(page.selectors) == {publisher.DOCUMENT_DELETE_SELECTOR}


def test_document_removal_repeats_when_livewire_restores_row() -> None:
    class LiveButtons:
        def __init__(self, page):
            self.page = page

        async def count(self):
            return self.page.remaining

        @property
        def first(self):
            return self

        async def click(self):
            self.page.remaining -= 1

    class Page:
        def __init__(self):
            self.remaining = 1
            self.restored = False

        def locator(self, selector):
            assert selector == PlaywrightAdminPublisher.DOCUMENT_DELETE_SELECTOR
            return LiveButtons(self)

        async def wait_for_timeout(self, milliseconds):
            if milliseconds == 350 and not self.restored and self.remaining == 0:
                self.remaining = 1
                self.restored = True

    publisher = object.__new__(PlaywrightAdminPublisher)
    page = Page()

    removed = asyncio.run(publisher._remove_documents_until_stable(page))

    assert removed == 2
    assert page.remaining == 0
