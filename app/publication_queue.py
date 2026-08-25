from __future__ import annotations

import json
import logging
import os
import re
import secrets
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


logger = logging.getLogger(__name__)
_DRAFT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,64}$")
PUBLICATION_STATUSES = ("pending", "processing", "completed", "failed")


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(slots=True)
class PublicationJob:
    draft_id: str
    token: str
    status: str
    created_at: str
    updated_at: str
    last_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "PublicationJob":
        job = cls(**value)
        if (
            not _DRAFT_ID_RE.fullmatch(job.draft_id)
            or not isinstance(job.token, str)
            or len(job.token) < 20
            or job.status not in PUBLICATION_STATUSES
        ):
            raise ValueError("Invalid publication job")
        return job


class PublicationQueue:
    def __init__(self, queue_dir: Path) -> None:
        self.queue_dir = queue_dir
        self.queue_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def _path(self, draft_id: str) -> Path:
        if not _DRAFT_ID_RE.fullmatch(draft_id):
            raise ValueError("Invalid draft id")
        return self.queue_dir / f"{draft_id}.json"

    def _save(self, job: PublicationJob) -> None:
        path = self._path(job.draft_id)
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(job.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary, path)

    def get(self, draft_id: str) -> PublicationJob | None:
        with self._lock:
            try:
                path = self._path(draft_id)
            except ValueError:
                return None
            if not path.exists():
                return None
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(payload, dict):
                    return None
                return PublicationJob.from_dict(payload)
            except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
                logger.warning("Could not read publication job %s: %s", path.name, exc)
                return None

    def _all_jobs(self) -> list[PublicationJob]:
        jobs: list[PublicationJob] = []
        for path in self.queue_dir.glob("*.json"):
            job = self.get(path.stem)
            if job is not None:
                jobs.append(job)
        return jobs

    def enqueue(self, draft_id: str) -> PublicationJob:
        with self._lock:
            existing = self.get(draft_id)
            if existing is not None:
                return existing
            now = _iso_now()
            job = PublicationJob(
                draft_id=draft_id,
                token=secrets.token_urlsafe(24),
                status="pending",
                created_at=now,
                updated_at=now,
            )
            self._save(job)
            return job

    def claim_next(self) -> PublicationJob | None:
        with self._lock:
            pending = [job for job in self._all_jobs() if job.status == "pending"]
            if not pending:
                return None
            pending.sort(key=lambda job: job.created_at)
            job = pending[0]
            job.status = "processing"
            job.updated_at = _iso_now()
            job.last_error = None
            self._save(job)
            return job

    def report_result(
        self,
        draft_id: str,
        token: str,
        *,
        success: bool,
        error: str | None = None,
    ) -> PublicationJob | None:
        with self._lock:
            job = self.get(draft_id)
            if job is None or not secrets.compare_digest(job.token, token):
                return None
            # Never turn a confirmed publication into a failure if a delayed
            # browser callback arrives afterwards.
            if job.status == "completed":
                return job
            job.status = "completed" if success else "failed"
            job.last_error = None if success else " ".join((error or "Unknown error").split())[:1000]
            job.updated_at = _iso_now()
            self._save(job)
            return job

    def mark_failed(self, draft_id: str, error: str) -> PublicationJob | None:
        with self._lock:
            job = self.get(draft_id)
            if job is None or job.status == "completed":
                return job
            job.status = "failed"
            job.last_error = " ".join(str(error).split())[:1000]
            job.updated_at = _iso_now()
            self._save(job)
            return job

    def retry_failed(self, draft_id: str) -> PublicationJob | None:
        with self._lock:
            job = self.get(draft_id)
            if job is None or job.status != "failed":
                return None
            job.status = "pending"
            job.last_error = None
            job.updated_at = _iso_now()
            self._save(job)
            return job

    def recover_interrupted(self, *, retry_safe: bool = False) -> int:
        recovered = 0
        with self._lock:
            for job in self._all_jobs():
                if job.status != "processing":
                    continue
                job.status = "pending" if retry_safe else "failed"
                job.last_error = None if retry_safe else (
                    "Application stopped while the admin form was being submitted; "
                    "check the site before retrying"
                )
                job.updated_at = _iso_now()
                self._save(job)
                recovered += 1
        return recovered

    def stats(self) -> dict[str, int]:
        with self._lock:
            result = {status: 0 for status in PUBLICATION_STATUSES}
            for job in self._all_jobs():
                result[job.status] += 1
            return result
