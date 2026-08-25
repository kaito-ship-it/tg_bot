from __future__ import annotations

import asyncio
import logging

import uvicorn

from app.api import create_app
from app.config import load_settings
from app.draft_store import DraftStore
from app.webhook_service import TelegramWebhookService

logger = logging.getLogger(__name__)


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
    webhook_service = TelegramWebhookService(settings, store)
    webhook_service.purge_expired()
    app = create_app(
        store,
        settings,
        webhook_handler=webhook_service.handle_update,
        webhook_stats=webhook_service.db.stats,
    )
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=settings.api_host,
            port=settings.api_port,
            log_level="info",
            proxy_headers=False,
        )
    )

    server_task = asyncio.create_task(server.serve(), name="http-api")
    worker_task = asyncio.create_task(
        webhook_service.run(),
        name="telegram-webhook-worker",
    )
    tasks = {server_task, worker_task}
    logger.info("tg2site webhook service started")
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            error = task.exception()
            if error is not None:
                raise error
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
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
    asyncio.run(run())


if __name__ == "__main__":
    main()
