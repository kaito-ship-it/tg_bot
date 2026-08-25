from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.dedup import normalize_source_url
from app.models import NewsDraft

_DRAFT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,64}$")


class DraftStore:
    def __init__(self, drafts_dir: Path, photos_dir: Path, ttl_hours: int) -> None:
        self.drafts_dir = drafts_dir
        self.photos_dir = photos_dir
        self.ttl = timedelta(hours=ttl_hours)
        self.drafts_dir.mkdir(parents=True, exist_ok=True)
        self.photos_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _validate_id(draft_id: str) -> None:
        if not _DRAFT_ID_RE.fullmatch(draft_id):
            raise ValueError("Invalid draft id")

    def _draft_path(self, draft_id: str) -> Path:
        self._validate_id(draft_id)
        return self.drafts_dir / f"{draft_id}.json"

    def save(self, draft: NewsDraft) -> None:
        path = self._draft_path(draft.draft_id)
        temp_path = path.with_suffix(".json.tmp")
        temp_path.write_text(
            json.dumps(draft.to_storage_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temp_path, path)

    def get(self, draft_id: str) -> NewsDraft | None:
        try:
            path = self._draft_path(draft_id)
        except ValueError:
            return None
        if not path.exists():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            draft = NewsDraft.from_storage_dict(value)
            created_at = datetime.fromisoformat(draft.created_at)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return None
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
        if datetime.now(UTC) - created_at > self.ttl:
            self.delete(draft_id)
            return None
        return draft

    def photo_path(self, draft: NewsDraft) -> Path | None:
        if not draft.photo_filename:
            return None
        candidate = (self.photos_dir / draft.photo_filename).resolve()
        if candidate.parent != self.photos_dir.resolve() or not candidate.is_file():
            return None
        return candidate

    def delete(self, draft_id: str) -> None:
        try:
            draft = self.get_without_ttl(draft_id)
            self._draft_path(draft_id).unlink(missing_ok=True)
        except ValueError:
            return
        if draft and draft.photo_filename:
            candidate = (self.photos_dir / draft.photo_filename).resolve()
            if candidate.parent == self.photos_dir.resolve():
                candidate.unlink(missing_ok=True)

    def get_without_ttl(self, draft_id: str) -> NewsDraft | None:
        path = self._draft_path(draft_id)
        if not path.exists():
            return None
        try:
            return NewsDraft.from_storage_dict(
                json.loads(path.read_text(encoding="utf-8"))
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return None

    def find_by_source_url(self, source_url: str) -> NewsDraft | None:
        source_key = normalize_source_url(source_url)
        if source_key is None:
            return None
        paths = sorted(
            self.drafts_dir.glob("*.json"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        for path in paths:
            draft = self.get(path.stem)
            if draft is None:
                continue
            draft_key = draft.source_key or normalize_source_url(draft.source_url)
            if draft_key == source_key:
                return draft
        return None

    def purge_expired(self) -> int:
        removed = 0
        for path in self.drafts_dir.glob("*.json"):
            if self.get(path.stem) is None and not path.exists():
                removed += 1
        return removed
