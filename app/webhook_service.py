from __future__ import annotations

import asyncio
import html
import logging
import re
import time
import uuid
from contextlib import suppress
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

from app.backend import BackendNewsPublisher
from app.category_service import create_category_classifier
from app.config import Settings
from app.draft_builder import build_draft
from app.draft_store import DraftStore
from app.image_service import ImageGenerationError, create_image_service
from app.moderation_db import ModerationDB, ModerationPost
from app.notifier import (
    REGENERATE_PHOTO_PREFIX,
    REJECT_DRAFT_PREFIX,
    SELECT_CATEGORY_PREFIX,
    TelegramNotificationError,
    TelegramNotifier,
)
from app.telegram_media import TelegramBotMediaClient

logger = logging.getLogger(__name__)
URL_RE = re.compile(r"https?://[^\s<>\"]+", re.IGNORECASE)
BLOCKED_URL_PARTS = (
    "t.me/",
    "telegram.me/",
    "instagram.com",
    "facebook.com",
    "wa.me",
)
MAX_PROCESSING_ATTEMPTS = 3
STALE_JOB_MINUTES = 15
CLEANUP_INTERVAL_SECONDS = 3600


def extract_webhook_url(text: str, entities: list[dict[str, Any]]) -> str | None:
    urls: list[str] = []
    for entity in entities:
        if entity.get("type") == "text_link" and isinstance(entity.get("url"), str):
            urls.append(entity["url"])
        elif entity.get("type") == "url":
            offset = entity.get("offset")
            length = entity.get("length")
            if isinstance(offset, int) and isinstance(length, int):
                encoded = text.encode("utf-16-le")
                try:
                    urls.append(
                        encoded[offset * 2 : (offset + length) * 2].decode("utf-16-le")
                    )
                except UnicodeDecodeError:
                    continue
    urls.extend(URL_RE.findall(text))

    seen: set[str] = set()
    for raw_url in urls:
        url = raw_url.rstrip(".,;:!?)»\"'")
        lowered = url.casefold()
        if url in seen or any(part in lowered for part in BLOCKED_URL_PARTS):
            continue
        seen.add(url)
        return url
    return None


class TelegramWebhookService:
    """Telegram Bot API webhook intake plus the SQLite moderation worker."""

    def __init__(self, settings: Settings, store: DraftStore) -> None:
        self.settings = settings
        self.store = store
        self.db = ModerationDB(settings.moderation_db_file)
        self.notifier = TelegramNotifier(settings.bot_token, settings.notify_chat_id)
        self.media_client = TelegramBotMediaClient(settings.bot_token)
        self.backend = BackendNewsPublisher(settings, store)
        self.image_service = create_image_service(settings)
        self.category_classifier = create_category_classifier(settings)
        self.wakeup = asyncio.Event()
        self.moderation_lock = asyncio.Lock()
        self.callback_tasks: set[asyncio.Task[None]] = set()
        self._last_cleanup = 0.0

    def purge_expired(self) -> None:
        removed_drafts = self.store.purge_expired()
        removed_rows = self.db.purge_old(self.settings.retention_days)
        self._last_cleanup = time.monotonic()
        if removed_drafts or removed_rows:
            logger.info(
                "Expired data removed: drafts=%s queue_rows=%s",
                removed_drafts,
                removed_rows,
            )

    async def handle_update(self, update: dict[str, Any]) -> None:
        callback = update.get("callback_query")
        if isinstance(callback, dict):
            task = asyncio.create_task(
                self._handle_callback(callback),
                name="telegram-webhook-callback",
            )
            self.callback_tasks.add(task)
            task.add_done_callback(self._callback_finished)
            return

        post = update.get("channel_post")
        if not isinstance(post, dict):
            return
        chat = post.get("chat")
        chat_id = str(chat.get("id", "")) if isinstance(chat, dict) else ""
        if chat_id != self.settings.telegram_channel_id:
            logger.warning("Ignoring webhook post from unexpected channel %s", chat_id)
            return
        if any(
            key in post for key in ("document", "audio", "voice", "video_note", "poll")
        ):
            logger.info("Ignoring unsupported Telegram attachment")
            return
        message_id = post.get("message_id")
        if not isinstance(message_id, int):
            return
        text = post.get("text") or post.get("caption") or ""
        if not isinstance(text, str):
            return
        entities = post.get("entities") or post.get("caption_entities") or []
        if not isinstance(entities, list):
            entities = []
        source_url = extract_webhook_url(
            text,
            [item for item in entities if isinstance(item, dict)],
        )
        if not source_url:
            logger.info("Ignoring Telegram post %s without an article URL", message_id)
            return
        telegram_photo_file_id = None
        photo_sizes = post.get("photo")
        if isinstance(photo_sizes, list):
            for item in reversed(photo_sizes):
                if isinstance(item, dict) and isinstance(item.get("file_id"), str):
                    telegram_photo_file_id = item["file_id"]
                    break
        queued = self.db.enqueue(
            chat_id=chat_id,
            message_id=message_id,
            source_url=source_url,
            raw_text=text,
            draft_id=uuid.uuid4().hex,
            telegram_photo_file_id=telegram_photo_file_id,
        )
        if queued is not None:
            logger.info("Queued webhook post %s as row %s", message_id, queued.id)
            self.wakeup.set()

    def _callback_finished(self, task: asyncio.Task[None]) -> None:
        self.callback_tasks.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.error(
                "Telegram webhook callback failed",
                exc_info=(type(error), error, error.__traceback__),
            )

    async def _answer_callback(self, callback_id: str, text: str) -> None:
        try:
            await self.notifier.answer_callback(callback_id, text)
        except TelegramNotificationError as exc:
            logger.warning("Could not answer Telegram callback: %s", exc)

    async def _edit_status(
        self,
        *,
        chat_id: str,
        message_id: int,
        text: str,
        url: str | None = None,
    ) -> None:
        try:
            await self.notifier.edit_status_message(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                url=url,
            )
        except TelegramNotificationError as exc:
            logger.warning("Could not edit Telegram status message: %s", exc)

    async def run(self) -> None:
        try:
            while True:
                worked = False
                for row in self.db.claim(
                    "new",
                    "parsing",
                    max_attempts=MAX_PROCESSING_ATTEMPTS,
                    stale_minutes=STALE_JOB_MINUTES,
                ):
                    worked = True
                    await self._parse(row)
                for row in self.db.claim(
                    "sending",
                    "publishing",
                    max_attempts=MAX_PROCESSING_ATTEMPTS,
                    stale_minutes=STALE_JOB_MINUTES,
                ):
                    worked = True
                    await self._publish(row)
                if time.monotonic() - self._last_cleanup >= CLEANUP_INTERVAL_SECONDS:
                    self.purge_expired()
                if worked:
                    continue
                self.wakeup.clear()
                with suppress(TimeoutError):
                    await asyncio.wait_for(self.wakeup.wait(), timeout=5)
        finally:
            for task in self.callback_tasks:
                task.cancel()
            await asyncio.gather(*self.callback_tasks, return_exceptions=True)

    async def _parse(self, row: ModerationPost) -> None:
        message = SimpleNamespace(
            id=row.tg_message_id,
            raw_text=row.source_url,
            photo=row.telegram_photo_file_id,
            media=None,
        )
        try:
            draft = await build_draft(
                [message],
                self.media_client,
                self.settings.photos_dir,
                row.draft_id,
                self.image_service,
                self.category_classifier,
            )
            self.store.save(draft)
            categories = await self.backend.categories()
            prompt_message_id = await self.notifier.send_draft_for_confirmation(
                draft,
                categories,
            )
            if prompt_message_id is None:
                raise RuntimeError("Telegram did not return a moderation message ID")
            self.db.update(
                row.id,
                status="awaiting_category",
                prompt_message_id=prompt_message_id,
                attempts=0,
            )
        except Exception as exc:
            logger.exception("Could not parse webhook post %s", row.id)
            error = str(exc)[:1000]
            if row.attempts < MAX_PROCESSING_ATTEMPTS:
                self.db.update(row.id, status="new", last_error=error)
                await asyncio.sleep(2 ** (row.attempts - 1))
                self.wakeup.set()
                return
            self.db.update(row.id, status="parse_failed", last_error=error)
            try:
                await self.notifier.send_processing_failure(
                    row.tg_message_id,
                    error,
                )
            except TelegramNotificationError as notify_error:
                logger.warning(
                    "Could not notify about parse failure: %s",
                    notify_error,
                )

    async def _publish(self, row: ModerationPost) -> None:
        draft = self.store.get(row.draft_id)
        if draft is None:
            self.db.update(row.id, status="failed", last_error="Draft is missing")
            return
        try:
            published = await self.backend.publish(draft)
        except Exception as exc:
            logger.exception("Could not publish webhook post %s", row.id)
            self.db.update(row.id, status="failed", last_error=str(exc)[:1000])
            try:
                await self.notifier.send_publication_failure(
                    draft=draft,
                    error=str(exc),
                    fallback_link=self.settings.admin_base_url,
                )
            except TelegramNotificationError as notify_error:
                logger.warning(
                    "Could not notify about publication failure: %s",
                    notify_error,
                )
            return
        updated = replace(
            draft,
            moderation_status="published",
            published_url=published.url,
        )
        self.store.save(updated)
        self.db.update(
            row.id,
            status="sent",
            backend_news_id=published.id,
            last_error=None,
        )
        if row.prompt_message_id is not None:
            try:
                await self.notifier.edit_status_message(
                    chat_id=self.settings.notify_chat_id,
                    message_id=row.prompt_message_id,
                    text=(
                        f"✅ <b>{html.escape(draft.title)}</b>\nОпубликовано на сайте"
                    ),
                    url=published.url,
                )
            except TelegramNotificationError as notify_error:
                logger.warning(
                    "Publication succeeded but Telegram status update failed: %s",
                    notify_error,
                )

    async def _handle_callback(self, callback: dict[str, Any]) -> None:
        callback_id = callback.get("id")
        data = callback.get("data")
        message = callback.get("message")
        sender = callback.get("from")
        if (
            not isinstance(callback_id, str)
            or not isinstance(data, str)
            or not isinstance(message, dict)
        ):
            return
        chat = message.get("chat")
        message_id = message.get("message_id")
        chat_id = str(chat.get("id", "")) if isinstance(chat, dict) else ""
        user_id = sender.get("id") if isinstance(sender, dict) else None
        if chat_id != self.settings.notify_chat_id:
            await self._answer_callback(callback_id, "Недоступно для этого чата")
            return
        if user_id not in self.settings.telegram_admin_user_ids:
            await self._answer_callback(callback_id, "Недостаточно прав")
            return
        if not isinstance(message_id, int):
            return

        async with self.moderation_lock:
            if data.startswith(SELECT_CATEGORY_PREFIX):
                await self._select_category(callback_id, data, chat_id, message_id)
            elif data.startswith(REJECT_DRAFT_PREFIX):
                await self._reject(callback_id, data, chat_id, message_id)
            elif data.startswith(REGENERATE_PHOTO_PREFIX):
                await self._regenerate(callback_id, data, chat_id, message_id)

    async def _select_category(
        self, callback_id: str, data: str, chat_id: str, message_id: int
    ) -> None:
        try:
            draft_id, raw_category = data.removeprefix(SELECT_CATEGORY_PREFIX).rsplit(
                ":", 1
            )
            category_id = int(raw_category)
        except (TypeError, ValueError):
            await self._answer_callback(callback_id, "Некорректная категория")
            return
        row = self.db.get_by_draft(draft_id)
        draft = self.store.get(draft_id)
        if row is None or draft is None or row.status != "awaiting_category":
            await self._answer_callback(callback_id, "Запись уже обработана")
            return
        categories = await self.backend.categories()
        category = next((item for item in categories if item.id == category_id), None)
        if category is None:
            await self._answer_callback(callback_id, "Категория недоступна")
            return
        updated = replace(
            draft,
            category_id=category_id,
            moderation_status="publishing",
        )
        self.store.save(updated)
        self.db.update(row.id, category_id=category_id, status="sending", attempts=0)
        self.wakeup.set()
        await self._answer_callback(callback_id, f"Публикую: {category.name}")
        await self._edit_status(
            chat_id=chat_id,
            message_id=message_id,
            text=(
                f"⏳ <b>{html.escape(updated.title)}</b>\n"
                f"Отправляю · {html.escape(category.name)}"
            ),
        )

    async def _reject(
        self, callback_id: str, data: str, chat_id: str, message_id: int
    ) -> None:
        draft_id = data.removeprefix(REJECT_DRAFT_PREFIX)
        row = self.db.get_by_draft(draft_id)
        draft = self.store.get(draft_id)
        if row is None or draft is None or row.status != "awaiting_category":
            await self._answer_callback(callback_id, "Запись уже обработана")
            return
        self.store.save(replace(draft, moderation_status="rejected"))
        self.db.update(row.id, status="rejected")
        await self._answer_callback(callback_id, "Новость отклонена")
        await self._edit_status(
            chat_id=chat_id,
            message_id=message_id,
            text=f"🚫 <s>{html.escape(draft.title)}</s>\nОтклонено",
        )

    async def _regenerate(
        self, callback_id: str, data: str, chat_id: str, message_id: int
    ) -> None:
        draft_id = data.removeprefix(REGENERATE_PHOTO_PREFIX)
        row = self.db.get_by_draft(draft_id)
        draft = self.store.get(draft_id)
        if row is None or draft is None or row.status != "awaiting_category":
            await self._answer_callback(callback_id, "Запись уже обработана")
            return
        if not draft.photo_is_generated or self.image_service is None:
            await self._answer_callback(callback_id, "Фото нельзя перегенерировать")
            return
        await self._answer_callback(callback_id, "Генерирую новое фото")
        try:
            filename = await self.image_service.generate_cover(
                title=draft.title,
                news_text=draft.text,
                target=self.settings.photos_dir / f"{draft.draft_id}.jpg",
                regenerate=True,
            )
        except ImageGenerationError:
            await self._answer_callback(callback_id, "Не удалось обновить фото")
            return
        updated = replace(
            draft,
            photo_filename=filename,
            photo_revision=draft.photo_revision + 1,
        )
        self.store.save(updated)
        categories = await self.backend.categories()
        try:
            await self.notifier.update_draft_message(
                chat_id=chat_id,
                message_id=message_id,
                draft=updated,
                status="Новое AI-фото готово",
                categories=categories,
            )
        except TelegramNotificationError as exc:
            logger.warning("Could not update regenerated photo message: %s", exc)
