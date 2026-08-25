from __future__ import annotations

import asyncio
import logging
import time
import webbrowser
from collections.abc import Callable
from dataclasses import replace

import requests

from app.backend import BackendNewsPublisher
from app.config import Settings
from app.draft_store import DraftStore
from app.link_builder import build_autofill_link
from app.notifier import TelegramNotifier
from app.playwright_publisher import PlaywrightAdminPublisher
from app.publication_queue import PublicationJob, PublicationQueue


logger = logging.getLogger(__name__)


class AutoPublisher:
    def __init__(
        self,
        settings: Settings,
        store: DraftStore,
        queue: PublicationQueue,
        notifier: TelegramNotifier,
        *,
        opener: Callable[[str], bool] | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.queue = queue
        self.notifier = notifier
        self.wakeup = asyncio.Event()
        self._opener = opener or self._open_browser
        self.publish_mode = getattr(settings, "publish_mode", "local_browser")
        self._playwright_publisher = (
            PlaywrightAdminPublisher(settings, store)
            if self.publish_mode == "playwright"
            else None
        )
        self._backend_publisher = (
            BackendNewsPublisher(settings, store)
            if self.publish_mode == "backend_api"
            else None
        )

    def schedule(self, draft_id: str) -> str:
        job = self.queue.enqueue(draft_id)
        if job.status == "failed" and self._backend_publisher is not None:
            # The backend contract is idempotent by external_id, so a new
            # explicit category click can safely retry a failed API request.
            job = self.queue.retry_failed(draft_id) or job
        if job.status == "pending":
            self.wakeup.set()
        return build_autofill_link(
            self.settings.admin_base_url,
            draft_id,
            job.token,
        )

    async def backend_categories(self):
        if self._backend_publisher is None:
            return ()
        return await self._backend_publisher.categories()

    def _open_browser(self, url: str) -> bool:
        if self.settings.browser_command:
            browser = webbrowser.get(self.settings.browser_command)
            return bool(browser.open_new_tab(url))
        return bool(webbrowser.open_new_tab(url))

    async def _notify_result(
        self,
        job: PublicationJob,
        *,
        success: bool,
        error: str | None = None,
        publication_url: str | None = None,
    ) -> None:
        draft = self.store.get(job.draft_id)
        if draft is None:
            return
        fallback_link = build_autofill_link(
            self.settings.admin_base_url,
            job.draft_id,
            job.token,
        )
        try:
            await self.notifier.send_publication_result(
                draft=draft,
                success=success,
                error=error,
                fallback_link=fallback_link,
                publication_url=publication_url,
            )
        except requests.RequestException as exc:
            logger.warning(
                "Could not send auto-publication result for %s: %s",
                job.draft_id,
                exc,
            )

    async def _process(self, job: PublicationJob) -> None:
        draft = self.store.get(job.draft_id)
        if draft is None:
            error = "Draft not found or expired"
            self.queue.mark_failed(job.draft_id, error)
            await self._notify_result(job, success=False, error=error)
            return

        if self._playwright_publisher is not None:
            try:
                await self._playwright_publisher.publish(draft)
            except Exception as exc:
                error = f"Playwright publication failed: {exc}"
                failed = self.queue.mark_failed(job.draft_id, error)
                await self._notify_result(
                    failed or job,
                    success=False,
                    error=error,
                )
                return
            completed = self.queue.report_result(
                job.draft_id,
                job.token,
                success=True,
            )
            await self._notify_result(completed or job, success=True)
            return

        if self._backend_publisher is not None:
            try:
                published = await self._backend_publisher.publish(draft)
            except Exception as exc:
                error = f"Backend API publication failed: {exc}"
                self.store.save(replace(draft, moderation_status="failed"))
                failed = self.queue.mark_failed(job.draft_id, error)
                await self._notify_result(
                    failed or job,
                    success=False,
                    error=error,
                )
                return
            completed = self.queue.report_result(
                job.draft_id,
                job.token,
                success=True,
            )
            logger.info(
                "Backend API published draft %s as news %s",
                draft.draft_id,
                published.id,
            )
            self.store.save(
                replace(
                    draft,
                    moderation_status="published",
                    published_url=published.url,
                )
            )
            await self._notify_result(
                completed or job,
                success=True,
                publication_url=published.url,
            )
            return

        url = build_autofill_link(
            self.settings.admin_base_url,
            job.draft_id,
            job.token,
        )
        try:
            opened = await asyncio.to_thread(self._opener, url)
        except Exception as exc:
            error = f"Could not open the admin browser: {exc}"
            self.queue.mark_failed(job.draft_id, error)
            await self._notify_result(job, success=False, error=error)
            return

        if not opened:
            logger.warning(
                "The browser did not confirm opening draft %s; waiting for callback",
                job.draft_id,
            )
        logger.info("Opened the admin page for draft %s", job.draft_id)

        deadline = time.monotonic() + self.settings.auto_publish_timeout_seconds
        while time.monotonic() < deadline:
            current = self.queue.get(job.draft_id)
            if current is None:
                return
            if current.status == "completed":
                logger.info("Auto-published draft %s", job.draft_id)
                await self._notify_result(current, success=True)
                return
            if current.status == "failed":
                logger.warning(
                    "Auto-publication failed for draft %s: %s",
                    job.draft_id,
                    current.last_error,
                )
                await self._notify_result(
                    current,
                    success=False,
                    error=current.last_error,
                )
                return
            await asyncio.sleep(0.5)

        error = "Timed out waiting for the admin page; check the site before retrying"
        failed = self.queue.mark_failed(job.draft_id, error)
        await self._notify_result(failed or job, success=False, error=error)

    async def run(self) -> None:
        interrupted = self.queue.recover_interrupted(
            retry_safe=self._backend_publisher is not None
        )
        if interrupted:
            logger.warning(
                "Marked %s interrupted auto-publication job(s) for manual review",
                interrupted,
            )
        try:
            while True:
                self.wakeup.clear()
                job = self.queue.claim_next()
                if job is not None:
                    await self._process(job)
                    continue
                try:
                    await asyncio.wait_for(self.wakeup.wait(), timeout=30)
                except TimeoutError:
                    pass
        finally:
            if self._playwright_publisher is not None:
                await self._playwright_publisher.close()
