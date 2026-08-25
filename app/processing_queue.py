from __future__ import annotations

import json
import logging
import os
import re
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


logger = logging.getLogger(__name__)
_JOB_ID_RE = re.compile(r"^[A-Za-z0-9_-]{3,100}$")
QUEUE_STATUSES = ("pending", "processing", "completed", "failed")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso_now() -> str:
    return _utc_now().isoformat()


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


@dataclass(slots=True)
class QueueJob:
    job_id: str
    message_ids: list[int]
    grouped_id: int | None
    draft_id: str
    status: str
    attempts: int
    created_at: str
    updated_at: str
    next_attempt_at: str | None = None
    last_error: str | None = None

    @property
    def first_message_id(self) -> int:
        return min(self.message_ids)

    @property
    def last_message_id(self) -> int:
        return max(self.message_ids)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "QueueJob":
        job = cls(**value)
        if (
            not _JOB_ID_RE.fullmatch(job.job_id)
            or not job.message_ids
            or any(not isinstance(item, int) or item <= 0 for item in job.message_ids)
            or job.status not in QUEUE_STATUSES
        ):
            raise ValueError("Invalid queue job")
        job.message_ids = sorted(set(job.message_ids))
        return job


class ProcessingQueue:
    def __init__(
        self,
        queue_dir: Path,
        *,
        max_attempts: int = 3,
        settle_seconds: float = 0.75,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        if settle_seconds < 0:
            raise ValueError("settle_seconds cannot be negative")
        self.queue_dir = queue_dir
        self.max_attempts = max_attempts
        self.settle_seconds = settle_seconds
        self.queue_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def make_job_id(message_ids: list[int], grouped_id: int | None) -> str:
        normalized = sorted(set(int(item) for item in message_ids))
        if not normalized or any(item <= 0 for item in normalized):
            raise ValueError("message_ids must contain positive integers")
        if grouped_id is not None:
            return f"album-{int(grouped_id)}"
        if len(normalized) == 1:
            return f"message-{normalized[0]}"
        return f"messages-{normalized[0]}-{normalized[-1]}"

    def _path(self, job_id: str) -> Path:
        if not _JOB_ID_RE.fullmatch(job_id):
            raise ValueError("Invalid queue job id")
        return self.queue_dir / f"{job_id}.json"

    def _save(self, job: QueueJob) -> None:
        path = self._path(job.job_id)
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(job.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary, path)

    def get(self, job_id: str) -> QueueJob | None:
        try:
            path = self._path(job_id)
        except ValueError:
            return None
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                return None
            return QueueJob.from_dict(payload)
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            logger.warning("Could not read queue job %s: %s", path.name, exc)
            return None

    def _all_jobs(self) -> list[QueueJob]:
        jobs: list[QueueJob] = []
        for path in self.queue_dir.glob("*.json"):
            job = self.get(path.stem)
            if job is not None:
                jobs.append(job)
        return jobs

    def enqueue(
        self, message_ids: list[int], *, grouped_id: int | None = None
    ) -> QueueJob:
        normalized = sorted(set(int(item) for item in message_ids))
        job_id = self.make_job_id(normalized, grouped_id)
        existing = self.get(job_id)
        if existing is not None:
            new_ids = sorted(set(existing.message_ids).union(normalized))
            if new_ids == existing.message_ids or existing.status == "completed":
                return existing
            existing.message_ids = new_ids
            if existing.status == "failed":
                existing.status = "pending"
                existing.attempts = 0
                existing.next_attempt_at = None
                existing.last_error = None
            existing.updated_at = _iso_now()
            self._save(existing)
            return existing

        now = _iso_now()
        job = QueueJob(
            job_id=job_id,
            message_ids=normalized,
            grouped_id=int(grouped_id) if grouped_id is not None else None,
            draft_id=uuid.uuid4().hex,
            status="pending",
            attempts=0,
            created_at=now,
            updated_at=now,
            next_attempt_at=(
                _utc_now() + timedelta(seconds=self.settle_seconds)
            ).isoformat()
            if self.settle_seconds
            else None,
        )
        self._save(job)
        return job

    def recover_interrupted(self) -> int:
        recovered = 0
        for job in self._all_jobs():
            if job.status != "processing":
                continue
            job.status = "pending"
            # An application shutdown is not a failed processing attempt.
            job.attempts = max(0, job.attempts - 1)
            job.next_attempt_at = None
            job.last_error = "Application stopped during processing"
            job.updated_at = _iso_now()
            self._save(job)
            recovered += 1
        return recovered

    def claim_next_ready(self, now: datetime | None = None) -> QueueJob | None:
        current = now or _utc_now()
        ready: list[QueueJob] = []
        for job in self._all_jobs():
            if job.status != "pending":
                continue
            next_attempt = _parse_datetime(job.next_attempt_at)
            if next_attempt is None or next_attempt <= current:
                ready.append(job)
        if not ready:
            return None
        ready.sort(key=lambda item: (item.first_message_id, item.created_at))
        job = ready[0]
        job.status = "processing"
        job.attempts += 1
        job.updated_at = _iso_now()
        job.next_attempt_at = None
        self._save(job)
        return job

    def mark_completed(self, job_id: str) -> QueueJob | None:
        job = self.get(job_id)
        if job is None:
            return None
        job.status = "completed"
        job.updated_at = _iso_now()
        job.next_attempt_at = None
        job.last_error = None
        self._save(job)
        return job

    def mark_attempt_failed(self, job_id: str, error: str) -> QueueJob | None:
        job = self.get(job_id)
        if job is None:
            return None
        job.last_error = " ".join(str(error).split())[:1000]
        job.updated_at = _iso_now()
        if job.attempts >= self.max_attempts:
            job.status = "failed"
            job.next_attempt_at = None
        else:
            job.status = "pending"
            # Give Telegram, the source site, or the notification bot time to
            # recover instead of spending all retries in a few seconds.
            delay_seconds = 5 * (4 ** max(0, job.attempts - 1))
            job.next_attempt_at = (
                _utc_now() + timedelta(seconds=delay_seconds)
            ).isoformat()
        self._save(job)
        return job

    def seconds_until_next_attempt(self, now: datetime | None = None) -> float | None:
        current = now or _utc_now()
        waits: list[float] = []
        for job in self._all_jobs():
            if job.status != "pending":
                continue
            next_attempt = _parse_datetime(job.next_attempt_at)
            if next_attempt is None:
                return 0
            waits.append(max(0.0, (next_attempt - current).total_seconds()))
        return min(waits) if waits else None

    def highest_message_id(self) -> int:
        jobs = self._all_jobs()
        return max((job.last_message_id for job in jobs), default=0)

    def lowest_message_id(self) -> int:
        jobs = self._all_jobs()
        return min((job.first_message_id for job in jobs), default=0)

    def stats(self) -> dict[str, int]:
        result = {status: 0 for status in QUEUE_STATUSES}
        for job in self._all_jobs():
            result[job.status] += 1
        return result

    def purge_completed(self, *, older_than_hours: int = 72) -> int:
        cutoff = _utc_now() - timedelta(hours=older_than_hours)
        removed = 0
        for job in self._all_jobs():
            if job.status != "completed":
                continue
            updated_at = _parse_datetime(job.updated_at)
            if updated_at is None or updated_at >= cutoff:
                continue
            try:
                self._path(job.job_id).unlink(missing_ok=True)
                removed += 1
            except OSError as exc:
                logger.warning("Could not purge queue job %s: %s", job.job_id, exc)
        return removed
