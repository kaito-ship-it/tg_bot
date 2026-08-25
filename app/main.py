from __future__ import annotations

import asyncio
import logging

import uvicorn

from app.api import create_app
from app.config import load_settings
from app.draft_store import DraftStore
from app.processing_queue import ProcessingQueue
from app.publication_queue import PublicationQueue
from app.telegram_listener import TelegramListener
from app.webhook_service import TelegramWebhookService


async def run() -> None:
    settings = load_settings()
    settings.validate_runtime()
    settings.data_dir.mkdir(parents=True, exist_ok=True)

    store = DraftStore(
        settings.drafts_dir,
        settings.photos_dir,
        settings.draft_ttl_hours,
    )
    store.purge_expired()
    queue = ProcessingQueue(settings.queue_dir)
    publication_queue = PublicationQueue(settings.publication_queue_dir)
    webhook_service = (
        TelegramWebhookService(settings, store)
        if settings.telegram_ingest_mode == "webhook"
        else None
    )
    app = create_app(
        store,
        settings,
        queue,
        publication_queue,
        webhook_handler=(
            webhook_service.handle_update if webhook_service is not None else None
        ),
        webhook_stats=(
            webhook_service.db.stats if webhook_service is not None else None
        ),
    )
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=settings.api_host,
            port=settings.api_port,
            log_level="info",
        )
    )
    listener = (
        TelegramListener(settings, store, queue, publication_queue)
        if webhook_service is None
        else None
    )

    server_task = asyncio.create_task(server.serve(), name="http-api")
    listener_task = asyncio.create_task(
        (
            webhook_service.run()
            if webhook_service is not None
            else listener.run()
        ),
        name=(
            "telegram-webhook-worker"
            if webhook_service is not None
            else "telegram-listener"
        ),
    )
    tasks = {server_task, listener_task}
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            error = task.exception()
            if error:
                raise error
        for task in pending:
            task.cancel()
    finally:
        server.should_exit = True
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        asyncio.run(run())
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass


if __name__ == "__main__":
    main()
