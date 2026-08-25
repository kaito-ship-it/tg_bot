from __future__ import annotations

import logging
import re
from typing import Any

from app.config import Settings
from app.draft_store import DraftStore
from app.models import NewsDraft


logger = logging.getLogger(__name__)


class PlaywrightPublicationError(RuntimeError):
    pass


class PlaywrightAdminPublisher:
    """Publishes one draft at a time through the Filament/Livewire admin UI."""

    TITLE_SELECTOR = "#mountedActionSchema0\\.title"
    CATEGORY_SELECTOR = "#mountedActionSchema0\\.news_category_id"
    EDITOR_SELECTOR = ".tiptap.ProseMirror"
    OPEN_CREATE_SELECTOR = "[wire\\:click=\"mountAction('create')\"]:visible"
    DOCUMENTS_SCHEMA = "mountedActionSchema0.documents"
    DOCUMENT_DELETE_SELECTOR = (
        "button[wire\\:click*=\"mountAction('delete'\"]"
        "[wire\\:click*=\"mountedActionSchema0.documents\"]:visible"
    )

    def __init__(self, settings: Settings, store: DraftStore) -> None:
        self.settings = settings
        self.store = store
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._page: Any = None

    async def _start(self) -> None:
        if self._page is not None:
            page_open = not self._page.is_closed()
            browser_connected = self._browser is not None and self._browser.is_connected()
            if page_open and browser_connected:
                return
            await self.close()
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise PlaywrightPublicationError(
                "Playwright is not installed; run pip install -r requirements.txt "
                "and python -m playwright install --with-deps chromium"
            ) from exc

        self.settings.browser_errors_dir.mkdir(parents=True, exist_ok=True)
        self._playwright = await async_playwright().start()
        try:
            self._browser = await self._playwright.chromium.launch(
                headless=self.settings.playwright_headless,
                slow_mo=self.settings.playwright_slow_mo_ms,
            )
            self._context = await self._browser.new_context(
                storage_state=str(self.settings.admin_auth_state_file),
                viewport={"width": 1440, "height": 1000},
                locale="ru-RU",
            )
            self._context.set_default_timeout(45_000)
            self._page = await self._context.new_page()
        except Exception:
            await self.close()
            raise

    async def close(self) -> None:
        context, browser, playwright = (
            self._context,
            self._browser,
            self._playwright,
        )
        self._page = None
        self._context = None
        self._browser = None
        self._playwright = None
        if context is not None:
            try:
                await context.storage_state(
                    path=str(self.settings.admin_auth_state_file),
                    indexed_db=True,
                )
            except Exception as exc:
                logger.warning("Could not refresh Playwright auth state: %s", exc)
            try:
                await context.close()
            except Exception as exc:
                logger.warning("Could not close Playwright context: %s", exc)
        if browser is not None:
            try:
                await browser.close()
            except Exception as exc:
                logger.warning("Could not close Playwright browser: %s", exc)
        if playwright is not None:
            try:
                await playwright.stop()
            except Exception as exc:
                logger.warning("Could not stop Playwright: %s", exc)

    async def _ensure_admin_page(self) -> Any:
        await self._start()
        page = self._page
        await page.goto(
            self.settings.admin_base_url,
            wait_until="domcontentloaded",
            timeout=60_000,
        )
        try:
            await page.locator(self.OPEN_CREATE_SELECTOR).wait_for(
                state="visible", timeout=30_000
            )
        except Exception as exc:
            password_visible = await page.locator('input[type="password"]:visible').count()
            if password_visible or "login" in page.url.lower():
                raise PlaywrightPublicationError(
                    "Admin session expired; capture PLAYWRIGHT_AUTH_STATE again"
                ) from exc
            raise PlaywrightPublicationError(
                f"Admin Create button was not found at {page.url}"
            ) from exc
        return page

    def _document_delete_buttons(self, page: Any) -> Any:
        # Keep this as a live Locator. Livewire replaces the modal DOM after a
        # deletion, so enumerating button indexes can leave stale nth() entries.
        return page.locator(self.DOCUMENT_DELETE_SELECTOR)

    async def _remove_documents(self, page: Any) -> int:
        removed = 0
        while removed < 20:
            buttons = self._document_delete_buttons(page)
            count_before = await buttons.count()
            if count_before == 0:
                return removed
            await buttons.first.click()
            for _ in range(100):
                if await self._document_delete_buttons(page).count() < count_before:
                    break
                await page.wait_for_timeout(100)
            else:
                raise PlaywrightPublicationError("Document row deletion timed out")
            removed += 1
        if await self._document_delete_buttons(page).count():
            raise PlaywrightPublicationError("More than 20 document rows were found")
        return removed

    async def _remove_documents_until_stable(self, page: Any) -> int:
        """Remove rows again if a later Livewire render restores them."""
        removed = 0
        quiet_checks = 0
        for _ in range(8):
            removed += await self._remove_documents(page)
            await page.wait_for_timeout(350)
            if await self._document_delete_buttons(page).count() == 0:
                quiet_checks += 1
                if quiet_checks == 2:
                    return removed
            else:
                quiet_checks = 0
        raise PlaywrightPublicationError(
            "The site keeps restoring the optional document row"
        )

    async def _fill_source(self, page: Any, source_url: str | None) -> None:
        if not source_url:
            return
        for selector in (
            "#mountedActionSchema0\\.source",
            "#mountedActionSchema0\\.source_url",
            '[wire\\:model="mountedActions.0.data.source"]',
        ):
            field = page.locator(f"{selector}:visible")
            if await field.count():
                await field.last.fill(source_url)
                return

    async def _visible_field(self, page: Any, selector: str) -> Any:
        field = page.locator(f"{selector}:visible").last
        await field.wait_for(state="visible", timeout=45_000)
        return field

    async def _wait_for_stable_form(self, page: Any) -> None:
        # A Livewire delete response can remove the old schema before the new
        # one is ready. Require all main fields to be visible in the same render
        # and give the final transition a short quiet window.
        await self._visible_field(page, self.TITLE_SELECTOR)
        await self._visible_field(page, self.CATEGORY_SELECTOR)
        await self._visible_field(page, self.EDITOR_SELECTOR)
        await page.wait_for_timeout(400)
        await self._visible_field(page, self.TITLE_SELECTOR)

    async def _set_photo(self, page: Any, draft: NewsDraft) -> None:
        photo_path = self.store.photo_path(draft)
        if photo_path is None:
            return
        input_field = page.locator(
            'input.filepond--browser[accept*="image/jpeg"]'
        ).last
        await input_field.wait_for(state="attached")
        await input_field.set_input_files(str(photo_path))
        await page.locator(
            '.filepond--item[data-filepond-item-state="processing-complete"]'
        ).wait_for(state="attached", timeout=60_000)

    async def _find_submit_button(self, page: Any, dialog: Any) -> Any:
        scope = dialog if await dialog.count() else page
        buttons = scope.locator("button:visible")
        for index in range(await buttons.count()):
            button = buttons.nth(index)
            label = (await button.inner_text()).strip()
            action = await button.get_attribute("wire:click") or ""
            button_type = (await button.get_attribute("type") or "").lower()
            if "mountAction('create')" in action:
                continue
            if (
                button_type == "submit"
                or "callMountedAction" in action
                or re.fullmatch(r"(Создать|Сохранить)( публикацию)?", label, re.I)
            ):
                return button
        raise PlaywrightPublicationError("Publication Create button was not found")

    async def _submit(self, page: Any) -> None:
        title = await self._visible_field(page, self.TITLE_SELECTOR)
        dialog = title.locator(
            "xpath=ancestor::*[@role='dialog' or contains(@class, 'fi-modal-window')][1]"
        )
        submit = await self._find_submit_button(page, dialog)
        await submit.click(timeout=60_000)
        try:
            if await dialog.count():
                await dialog.wait_for(state="hidden", timeout=45_000)
            else:
                await title.wait_for(state="detached", timeout=45_000)
        except Exception as exc:
            messages: list[str] = []
            # FilePond uses role=alert for successful uploads too. Only collect
            # actual Filament validation messages here.
            errors = page.locator(".fi-fo-field-wrp-error-message:visible")
            for index in range(min(await errors.count(), 8)):
                text = (await errors.nth(index).inner_text()).strip()
                if text:
                    messages.append(text)
            detail = "; ".join(dict.fromkeys(messages)) or "modal did not close"
            raise PlaywrightPublicationError(
                f"Site rejected the publication: {detail}"
            ) from exc

    async def publish(self, draft: NewsDraft) -> None:
        page: Any = None
        try:
            page = await self._ensure_admin_page()
            await page.locator(self.OPEN_CREATE_SELECTOR).click()
            await self._wait_for_stable_form(page)

            removed = await self._remove_documents_until_stable(page)
            await self._wait_for_stable_form(page)
            title = await self._visible_field(page, self.TITLE_SELECTOR)
            await title.fill(draft.title)
            category = await self._visible_field(page, self.CATEGORY_SELECTOR)
            await category.select_option(
                str(draft.category_id or 35)
            )
            await self._fill_source(page, draft.source_url)
            editor = await self._visible_field(page, self.EDITOR_SELECTOR)
            await editor.fill(draft.text)
            await self._set_photo(page, draft)

            # Uploading an image and other Livewire updates can restore the
            # default blank document repeater. Remove it immediately before
            # submit and wait through two quiet checks so required document
            # fields cannot block publication.
            removed += await self._remove_documents_until_stable(page)
            await self._wait_for_stable_form(page)
            if await self._document_delete_buttons(page).count():
                raise PlaywrightPublicationError(
                    "Optional document row is still present before publication"
                )
            await self._submit(page)
            await self._context.storage_state(
                path=str(self.settings.admin_auth_state_file),
                indexed_db=True,
            )
            logger.info(
                "Playwright published draft %s; removed document rows=%s",
                draft.draft_id,
                removed,
            )
        except PlaywrightPublicationError:
            await self._capture_failure(page, draft.draft_id)
            raise
        except Exception as exc:
            await self._capture_failure(page, draft.draft_id)
            raise PlaywrightPublicationError(str(exc)) from exc

    async def _capture_failure(self, page: Any, draft_id: str) -> None:
        if page is None:
            return
        target = self.settings.browser_errors_dir / f"{draft_id}.png"
        try:
            await page.screenshot(path=str(target), full_page=True)
            logger.warning("Saved browser failure screenshot to %s", target)
        except Exception as exc:
            logger.warning("Could not save browser failure screenshot: %s", exc)
