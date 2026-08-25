from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

from app.config import DEFAULT_CATEGORY_ID


@dataclass(slots=True)
class NewsDraft:
    draft_id: str
    title: str
    text: str
    category_id: int | None
    source_message_id: int
    created_at: str
    source_url: str | None = None
    photo_filename: str | None = None
    photo_is_generated: bool = False
    photo_revision: int = 0
    source_key: str | None = None
    moderation_status: str = "awaiting_category"
    published_url: str | None = None
    source_image_url: str | None = None

    @classmethod
    def create(
        cls,
        *,
        draft_id: str,
        title: str,
        text: str,
        category_id: int | None,
        source_message_id: int,
        source_url: str | None = None,
        photo_filename: str | None = None,
        photo_is_generated: bool = False,
        photo_revision: int = 0,
        source_key: str | None = None,
        source_image_url: str | None = None,
    ) -> "NewsDraft":
        return cls(
            draft_id=draft_id,
            title=title,
            text=text,
            category_id=category_id,
            source_message_id=source_message_id,
            created_at=datetime.now(timezone.utc).isoformat(),
            source_url=source_url,
            photo_filename=photo_filename,
            photo_is_generated=photo_is_generated,
            photo_revision=photo_revision,
            source_key=source_key,
            source_image_url=source_image_url,
        )

    def to_storage_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_storage_dict(cls, value: dict[str, Any]) -> "NewsDraft":
        return cls(**value)

    def to_api_dict(self, public_api_base: str) -> dict[str, Any]:
        photo_url = None
        if self.photo_filename:
            photo_url = f"{public_api_base.rstrip('/')}/photo/{self.draft_id}"
            if self.photo_revision > 0:
                photo_url += f"?v={self.photo_revision}"
        return {
            "title": self.title,
            "text": self.text,
            # Also fixes links for drafts created before category fallback was
            # introduced, where the stored value can still be null.
            "category_id": self.category_id or DEFAULT_CATEGORY_ID,
            "source_url": self.source_url,
            "photo_url": photo_url,
        }
