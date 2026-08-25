from __future__ import annotations

import uvicorn

from app.api import create_app
from app.config import load_settings
from app.draft_store import DraftStore
from app.models import NewsDraft


TEST_DRAFT_ID = "test123"


def main() -> None:
    """Run the HTTP API with a fixture draft, without Telegram credentials."""
    settings = load_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    store = DraftStore(
        settings.drafts_dir,
        settings.photos_dir,
        settings.draft_ttl_hours,
    )
    store.save(
        NewsDraft.create(
            draft_id=TEST_DRAFT_ID,
            title="Тестовый заголовок",
            text=(
                "Тестовый заголовок\n\n"
                "Первый абзац тестовой новости.\n\n"
                "Второй абзац нужен для проверки редактора Tiptap."
            ),
            category_id=35,
            source_message_id=0,
        )
    )
    uvicorn.run(
        create_app(store, settings),
        host=settings.api_host,
        port=settings.api_port,
        log_level="info",
    )


if __name__ == "__main__":
    main()

