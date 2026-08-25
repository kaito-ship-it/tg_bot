from __future__ import annotations

import asyncio
import logging
import uuid
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import replace
from typing import Any

import requests
from telethon import TelegramClient, events
from telethon.errors import BotMethodInvalidError

from app.auto_publisher import AutoPublisher
from app.config import Settings
from app.category_service import create_category_classifier
from app.article_service import extract_first_url
from app.draft_builder import build_draft, extract_album_text
from app.draft_store import DraftStore
from app.image_service import ImageGenerationError, create_image_service
from app.link_builder import build_autofill_link
from app.models import NewsDraft
from app.notifier import (
    REGENERATE_PHOTO_PREFIX,
    REJECT_DRAFT_PREFIX,
    SELECT_CATEGORY_PREFIX,
    TelegramNotifier,
)
from app.processing_queue import ProcessingQueue, QueueJob
from app.publication_queue import PublicationQueue
from app.state import ProcessingState


logger = logging.getLogger(__name__)


class TelegramListener:
    def __init__(
        self,
        settings: Settings,
        store: DraftStore,
        queue: ProcessingQueue | None = None,
        publication_queue: PublicationQueue | None = None,
    ) -> None:
        if settings.tg_api_id is None:
            raise ValueError("TG_API_ID is required")
        self.settings = settings
        self.store = store
        self.queue = queue or ProcessingQueue(settings.queue_dir)
        self.state = ProcessingState(settings.state_file)
        self.notifier = TelegramNotifier(
            settings.bot_token, settings.notify_chat_id
        )
        self.auto_publisher = (
            AutoPublisher(
                settings,
                store,
                publication_queue or PublicationQueue(settings.publication_queue_dir),
                self.notifier,
            )
            if settings.publish_mode != "disabled"
            else None
        )
        self.image_service = create_image_service(settings)
        self.category_classifier = create_category_classifier(settings)
        self.client = TelegramClient(
            settings.tg_session_name,
            settings.tg_api_id,
            settings.tg_api_hash,
        )
        self.album_buffers: dict[int, list[Any]] = defaultdict(list)
        self.album_tasks: dict[int, asyncio.Task[None]] = {}
        self.regeneration_tasks: set[asyncio.Task[None]] = set()
        self.regenerating_draft_ids: set[str] = set()
        self.queue_wakeup = asyncio.Event()
        self.process_lock = asyncio.Lock()

        self.client.add_event_handler(
            self._on_new_message,
            events.NewMessage(chats=settings.telegram_channel),
        )

    def _autofill_link(self, draft_id: str) -> str:
        auto_publisher = getattr(self, "auto_publisher", None)
        if (
            auto_publisher is not None
            and getattr(self.settings, "publish_mode", "disabled") != "backend_api"
        ):
            return auto_publisher.schedule(draft_id)
        return build_autofill_link(self.settings.admin_base_url, draft_id)

    async def _send_draft(self, draft: NewsDraft, *, duplicate: bool = False) -> None:
        if getattr(self.settings, "publish_mode", "disabled") == "backend_api":
            if self.auto_publisher is None:
                raise RuntimeError("Backend API publisher is not initialized")
            publication_job = self.auto_publisher.queue.get(draft.draft_id)
            already_final = draft.moderation_status in {
                "published",
                "publishing",
                "rejected",
            } or (
                publication_job is not None
                and publication_job.status in {"processing", "completed"}
            )
            categories = (
                ()
                if duplicate and already_final
                else await self.auto_publisher.backend_categories()
            )
            sender = (
                self.notifier.send_existing_draft
                if duplicate
                else self.notifier.send_draft_for_confirmation
            )
            await sender(draft, None, categories)
            return
        link = self._autofill_link(draft.draft_id)
        sender = (
            self.notifier.send_existing_draft
            if duplicate
            else self.notifier.send_draft_for_confirmation
        )
        await sender(draft, link)

    async def _on_new_message(self, event: Any) -> None:
        message = event.message
        grouped_id = getattr(message, "grouped_id", None)
        if grouped_id is None:
            # A following standalone post can arrive before the two-second album
            # window closes. Flush older albums first so the high-watermark state
            # cannot make those album messages look already processed.
            pending_albums = list(self.album_tasks.values())
            if pending_albums:
                await asyncio.gather(*pending_albums)
            self._enqueue_messages([message])
            return

        group_key = int(grouped_id)
        self.album_buffers[group_key].append(message)
        if group_key not in self.album_tasks:
            self.album_tasks[group_key] = asyncio.create_task(
                self._flush_album_after_delay(group_key)
            )

    async def _flush_album_after_delay(self, group_key: int) -> None:
        try:
            await asyncio.sleep(self.settings.album_wait_seconds)
            messages = self.album_buffers.pop(group_key, [])
            messages.sort(key=lambda message: int(message.id))
            if messages:
                self._enqueue_messages(messages, grouped_id=group_key)
        finally:
            self.album_tasks.pop(group_key, None)

    def _enqueue_messages(
        self,
        messages: Sequence[Any],
        *,
        grouped_id: int | None = None,
    ) -> QueueJob:
        message_ids = sorted({int(message.id) for message in messages})
        job = self.queue.enqueue(message_ids, grouped_id=grouped_id)
        self.queue_wakeup.set()
        logger.info(
            "Queued Telegram messages %s as %s",
            message_ids,
            job.job_id,
        )
        return job

    async def _process_messages(
        self,
        messages: Sequence[Any],
        *,
        draft_id: str | None = None,
    ) -> NewsDraft:
        async with self.process_lock:
            max_message_id = max(int(message.id) for message in messages)

            draft_id = draft_id or uuid.uuid4().hex
            saved_draft = self.store.get(draft_id)
            if saved_draft is not None:
                await self._send_draft(saved_draft)
                self.state.save_last_processed_id(max_message_id)
                logger.info(
                    "Resent saved draft %s for message %s",
                    saved_draft.draft_id,
                    max_message_id,
                )
                return saved_draft

            submitted_url = extract_first_url(extract_album_text(messages))
            if submitted_url:
                existing_draft = self.store.find_by_source_url(submitted_url)
                if existing_draft is not None:
                    await self._send_draft(existing_draft, duplicate=True)
                    self.state.save_last_processed_id(max_message_id)
                    logger.info(
                        "Reused draft %s for duplicate source in message %s",
                        existing_draft.draft_id,
                        max_message_id,
                    )
                    return existing_draft

            draft = await build_draft(
                messages,
                self.client,
                self.settings.photos_dir,
                draft_id,
                self.image_service,
                self.category_classifier,
            )
            self.store.save(draft)
            await self._send_draft(draft)
            self.state.save_last_processed_id(max_message_id)
            logger.info("Prepared draft %s for message %s", draft_id, max_message_id)
            return draft

    async def _catch_up_channel_history(self) -> int:
        try:
            return await self._catch_up_supported_channel_history()
        except BotMethodInvalidError:
            logger.warning(
                "Telegram history catch-up is unavailable for bot sessions; "
                "continuing with real-time channel updates and the persisted queue"
            )
            return 0

    async def _catch_up_supported_channel_history(self) -> int:
        checkpoint = self.state.load_last_processed_id()
        if checkpoint <= 0:
            lowest_queued = self.queue.lowest_message_id()
            if lowest_queued > 0:
                checkpoint = max(0, lowest_queued - 1)
            else:
                latest = await self.client.get_messages(
                    self.settings.telegram_channel, limit=1
                )
                latest_messages = list(latest) if latest else []
                if latest_messages:
                    baseline = max(int(message.id) for message in latest_messages)
                    self.state.save_last_processed_id(baseline)
                    logger.info(
                        "Initialized Telegram history baseline at message %s",
                        baseline,
                    )
                return 0

        standalone: list[Any] = []
        albums: dict[int, list[Any]] = defaultdict(list)
        async for message in self.client.iter_messages(
            self.settings.telegram_channel,
            min_id=checkpoint,
            reverse=True,
        ):
            grouped_id = getattr(message, "grouped_id", None)
            if grouped_id is None:
                standalone.append(message)
            else:
                albums[int(grouped_id)].append(message)

        for message in standalone:
            self._enqueue_messages([message])
        for grouped_id, messages in albums.items():
            messages.sort(key=lambda item: int(item.id))
            self._enqueue_messages(messages, grouped_id=grouped_id)

        recovered = len(standalone) + len(albums)
        if recovered:
            logger.info(
                "Recovered %s Telegram post(s) from channel history after message %s",
                recovered,
                checkpoint,
            )
        return recovered

    async def _load_job_messages(self, job: QueueJob) -> list[Any]:
        result = await self.client.get_messages(
            self.settings.telegram_channel,
            ids=job.message_ids,
        )
        if result is None:
            messages: list[Any] = []
        elif isinstance(result, (list, tuple)):
            messages = [message for message in result if message is not None]
        else:
            messages = [result]
        messages.sort(key=lambda message: int(message.id))
        if not messages:
            raise RuntimeError(
                f"Telegram messages are unavailable: {job.message_ids}"
            )
        if len(messages) != len(job.message_ids):
            logger.warning(
                "Queue job %s has %s/%s Telegram messages available",
                job.job_id,
                len(messages),
                len(job.message_ids),
            )
        return messages

    async def _process_queue_job(self, job: QueueJob) -> None:
        try:
            messages = await self._load_job_messages(job)
            await self._process_messages(messages, draft_id=job.draft_id)
            self.queue.mark_completed(job.job_id)
            logger.info("Completed queue job %s", job.job_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            updated = self.queue.mark_attempt_failed(job.job_id, str(exc))
            if updated is None:
                logger.exception("Queue job %s disappeared after failure", job.job_id)
                return
            if updated.status == "failed":
                logger.exception(
                    "Queue job %s failed permanently after %s attempts",
                    job.job_id,
                    updated.attempts,
                )
                try:
                    await self.notifier.send_processing_failure(
                        updated.message_ids,
                        updated.last_error or "unknown error",
                    )
                except requests.RequestException as notify_error:
                    logger.warning(
                        "Could not notify about failed queue job %s: %s",
                        job.job_id,
                        notify_error,
                    )
            else:
                logger.warning(
                    "Queue job %s attempt %s failed; retry scheduled: %s",
                    job.job_id,
                    updated.attempts,
                    exc,
                )
            self.queue_wakeup.set()

    async def _queue_worker(self) -> None:
        while True:
            job = self.queue.claim_next_ready()
            if job is not None:
                await self._process_queue_job(job)
                continue

            self.queue_wakeup.clear()
            wait_seconds = self.queue.seconds_until_next_attempt()
            timeout = 30.0 if wait_seconds is None else max(0.1, wait_seconds)
            try:
                await asyncio.wait_for(self.queue_wakeup.wait(), timeout=timeout)
            except TimeoutError:
                pass

    async def _poll_bot_callbacks(self) -> None:
        offset: int | None = None
        while True:
            try:
                updates = await self.notifier.get_callback_updates(offset)
                for update in updates:
                    update_id = update.get("update_id")
                    if isinstance(update_id, int):
                        offset = max(offset or 0, update_id + 1)
                    callback = update.get("callback_query")
                    if isinstance(callback, dict):
                        await self._start_callback_action(callback)
            except asyncio.CancelledError:
                raise
            except (requests.RequestException, TypeError, ValueError) as exc:
                logger.warning("Telegram bot callback polling failed: %s", exc)
                await asyncio.sleep(2)

    async def _start_callback_action(self, callback: dict[str, Any]) -> None:
        data = callback.get("data")
        callback_id = callback.get("id")
        message = callback.get("message")
        if (
            not isinstance(data, str)
            or not isinstance(callback_id, str)
            or not isinstance(message, dict)
        ):
            return

        chat = message.get("chat")
        message_id = message.get("message_id")
        if not isinstance(chat, dict) or not isinstance(message_id, int):
            return
        chat_id = str(chat.get("id", ""))
        if chat_id != str(self.settings.notify_chat_id):
            await self.notifier.answer_callback(callback_id, "Недоступно для этого чата")
            return

        callback_user = callback.get("from")
        user_id = callback_user.get("id") if isinstance(callback_user, dict) else None
        allowed_users = getattr(self.settings, "telegram_admin_user_ids", ())
        if allowed_users and user_id not in allowed_users:
            await self.notifier.answer_callback(callback_id, "Недостаточно прав")
            return

        if data.startswith(SELECT_CATEGORY_PREFIX):
            await self._select_category(callback_id, data, chat_id, message_id)
            return
        if data.startswith(REJECT_DRAFT_PREFIX):
            await self._reject_draft(callback_id, data, chat_id, message_id)
            return
        if not data.startswith(REGENERATE_PHOTO_PREFIX):
            return

        draft_id = data.removeprefix(REGENERATE_PHOTO_PREFIX)
        draft = self.store.get(draft_id)
        if draft is None:
            await self.notifier.answer_callback(callback_id, "Черновик уже истёк")
            return
        if not draft.photo_is_generated:
            await self.notifier.answer_callback(
                callback_id, "Это фото получено из источника и не заменяется"
            )
            return
        if self.image_service is None:
            await self.notifier.answer_callback(
                callback_id, "Генерация изображений сейчас отключена"
            )
            return
        if draft_id in self.regenerating_draft_ids:
            await self.notifier.answer_callback(
                callback_id, "Новое фото уже генерируется"
            )
            return

        self.regenerating_draft_ids.add(draft_id)
        try:
            await self.notifier.answer_callback(
                callback_id, "Начинаю генерацию нового фото"
            )
        except requests.RequestException:
            self.regenerating_draft_ids.discard(draft_id)
            raise

        task = asyncio.create_task(
            self._regenerate_draft_photo(
                draft=draft,
                chat_id=chat_id,
                message_id=message_id,
            ),
            name=f"regenerate-photo-{draft_id}",
        )
        self.regeneration_tasks.add(task)
        task.add_done_callback(self.regeneration_tasks.discard)

    async def _select_category(
        self,
        callback_id: str,
        data: str,
        chat_id: str,
        message_id: int,
    ) -> None:
        if (
            self.auto_publisher is None
            or getattr(self.settings, "publish_mode", "disabled") != "backend_api"
        ):
            await self.notifier.answer_callback(callback_id, "API-публикация отключена")
            return
        value = data.removeprefix(SELECT_CATEGORY_PREFIX)
        try:
            draft_id, raw_category_id = value.rsplit(":", 1)
            category_id = int(raw_category_id)
        except (TypeError, ValueError):
            await self.notifier.answer_callback(callback_id, "Некорректная категория")
            return
        draft = self.store.get(draft_id)
        if draft is None:
            await self.notifier.answer_callback(callback_id, "Черновик уже истёк")
            return
        categories = await self.auto_publisher.backend_categories()
        selected = next((item for item in categories if item.id == category_id), None)
        if selected is None:
            await self.notifier.answer_callback(callback_id, "Категория недоступна")
            return
        updated = replace(
            draft,
            category_id=category_id,
            moderation_status="publishing",
        )
        self.store.save(updated)
        await self.notifier.answer_callback(
            callback_id, f"Публикую: {selected.name}"
        )
        await self._safe_update_draft_message(
            chat_id=chat_id,
            message_id=message_id,
            draft=updated,
            autofill_link=None,
            status=f"Отправляю на сайт · {selected.name}",
        )
        self.auto_publisher.schedule(draft_id)

    async def _reject_draft(
        self,
        callback_id: str,
        data: str,
        chat_id: str,
        message_id: int,
    ) -> None:
        draft_id = data.removeprefix(REJECT_DRAFT_PREFIX)
        draft = self.store.get(draft_id)
        if draft is None:
            await self.notifier.answer_callback(callback_id, "Черновик уже истёк")
            return
        updated = replace(draft, moderation_status="rejected")
        self.store.save(updated)
        await self.notifier.answer_callback(callback_id, "Новость отклонена")
        await self._safe_update_draft_message(
            chat_id=chat_id,
            message_id=message_id,
            draft=updated,
            autofill_link=None,
            status="🚫 Новость не будет опубликована",
        )

    async def _regenerate_draft_photo(
        self,
        *,
        draft: NewsDraft,
        chat_id: str,
        message_id: int,
    ) -> None:
        autofill_link = (
            None
            if getattr(self.settings, "publish_mode", "disabled") == "backend_api"
            else self._autofill_link(draft.draft_id)
        )
        categories = (
            await self.auto_publisher.backend_categories()
            if getattr(self.settings, "publish_mode", "disabled") == "backend_api"
            and self.auto_publisher is not None
            else ()
        )
        await self._safe_update_draft_message(
            chat_id=chat_id,
            message_id=message_id,
            draft=draft,
            autofill_link=autofill_link,
            status="Генерирую новое AI-фото…",
            categories=categories,
        )
        try:
            photo_filename = await self.image_service.generate_cover(
                title=draft.title,
                news_text=draft.text,
                target=self.settings.photos_dir / f"{draft.draft_id}.jpg",
                regenerate=True,
            )
            updated = replace(
                draft,
                photo_filename=photo_filename,
                photo_is_generated=True,
                photo_revision=draft.photo_revision + 1,
            )
            self.store.save(updated)
            await self._safe_update_draft_message(
                chat_id=chat_id,
                message_id=message_id,
                draft=updated,
                autofill_link=autofill_link,
                status="Новое AI-фото готово",
                categories=categories,
            )
            logger.info("Regenerated AI photo for draft %s", draft.draft_id)
        except ImageGenerationError as exc:
            logger.warning(
                "Could not regenerate AI photo for draft %s: %s",
                draft.draft_id,
                exc,
            )
            await self._safe_update_draft_message(
                chat_id=chat_id,
                message_id=message_id,
                draft=draft,
                autofill_link=autofill_link,
                status="Не удалось обновить фото — прежнее сохранено",
                categories=categories,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception(
                "Unexpected photo regeneration error for draft %s: %s",
                draft.draft_id,
                exc,
            )
            await self._safe_update_draft_message(
                chat_id=chat_id,
                message_id=message_id,
                draft=draft,
                autofill_link=autofill_link,
                status="Не удалось обновить фото — прежнее сохранено",
                categories=categories,
            )
        finally:
            self.regenerating_draft_ids.discard(draft.draft_id)

    async def _safe_update_draft_message(self, **kwargs: Any) -> None:
        try:
            await self.notifier.update_draft_message(**kwargs)
        except requests.RequestException as exc:
            logger.warning("Could not update Telegram bot message: %s", exc)

    async def run(self) -> None:
        await self.client.start()
        recovered = self.queue.recover_interrupted()
        purged = self.queue.purge_completed()
        if recovered or purged:
            logger.info(
                "Queue startup maintenance: recovered=%s, purged=%s",
                recovered,
                purged,
            )
        await self._catch_up_channel_history()
        logger.info("Listening to %s", self.settings.telegram_channel)
        worker_task = asyncio.create_task(
            self._queue_worker(), name="telegram-queue-worker"
        )
        callback_task = asyncio.create_task(
            self._poll_bot_callbacks(), name="telegram-bot-callbacks"
        )
        publisher_task = (
            asyncio.create_task(
                self.auto_publisher.run(), name="admin-auto-publisher"
            )
            if self.auto_publisher is not None
            else None
        )
        try:
            await self.client.run_until_disconnected()
        finally:
            worker_task.cancel()
            callback_task.cancel()
            if publisher_task is not None:
                publisher_task.cancel()
            for task in self.album_tasks.values():
                task.cancel()
            for task in self.regeneration_tasks:
                task.cancel()
            await asyncio.gather(
                worker_task,
                callback_task,
                *([publisher_task] if publisher_task is not None else []),
                *self.regeneration_tasks,
                return_exceptions=True,
            )
            await self.client.disconnect()
