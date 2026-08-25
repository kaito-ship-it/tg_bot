from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class ModerationPost:
    id: int
    tg_chat_id: str
    tg_message_id: int
    source_url: str
    raw_text: str
    telegram_photo_file_id: str | None
    draft_id: str
    category_id: int | None
    status: str
    prompt_message_id: int | None
    backend_news_id: int | None
    attempts: int
    last_error: str | None
    created_at: str
    updated_at: str


class ModerationDB:
    """Durable single-worker queue for Telegram webhook processing."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.init()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=20)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def init(self) -> None:
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS posts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tg_chat_id TEXT NOT NULL,
                    tg_message_id INTEGER NOT NULL,
                    source_url TEXT NOT NULL,
                    raw_text TEXT NOT NULL,
                    telegram_photo_file_id TEXT,
                    draft_id TEXT NOT NULL UNIQUE,
                    category_id INTEGER,
                    status TEXT NOT NULL DEFAULT 'new',
                    prompt_message_id INTEGER,
                    backend_news_id INTEGER,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                    UNIQUE (tg_chat_id, tg_message_id)
                );
                CREATE INDEX IF NOT EXISTS idx_posts_status ON posts(status);
                """
            )
            columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(posts)").fetchall()
            }
            if "telegram_photo_file_id" not in columns:
                connection.execute(
                    "ALTER TABLE posts ADD COLUMN telegram_photo_file_id TEXT"
                )

    @staticmethod
    def _row(value: sqlite3.Row | None) -> ModerationPost | None:
        if value is None:
            return None
        return ModerationPost(
            id=int(value["id"]),
            tg_chat_id=str(value["tg_chat_id"]),
            tg_message_id=int(value["tg_message_id"]),
            source_url=str(value["source_url"]),
            raw_text=str(value["raw_text"]),
            telegram_photo_file_id=(
                str(value["telegram_photo_file_id"])
                if value["telegram_photo_file_id"] is not None
                else None
            ),
            draft_id=str(value["draft_id"]),
            category_id=(
                int(value["category_id"]) if value["category_id"] is not None else None
            ),
            status=str(value["status"]),
            prompt_message_id=(
                int(value["prompt_message_id"])
                if value["prompt_message_id"] is not None
                else None
            ),
            backend_news_id=(
                int(value["backend_news_id"])
                if value["backend_news_id"] is not None
                else None
            ),
            attempts=int(value["attempts"]),
            last_error=(
                str(value["last_error"]) if value["last_error"] is not None else None
            ),
            created_at=str(value["created_at"]),
            updated_at=str(value["updated_at"]),
        )

    def enqueue(
        self,
        *,
        chat_id: str,
        message_id: int,
        source_url: str,
        raw_text: str,
        draft_id: str,
        telegram_photo_file_id: str | None = None,
    ) -> ModerationPost | None:
        with self._connection() as connection:
            try:
                cursor = connection.execute(
                    """
                    INSERT INTO posts (
                        tg_chat_id, tg_message_id, source_url, raw_text,
                        telegram_photo_file_id, draft_id
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(chat_id),
                        int(message_id),
                        source_url,
                        raw_text,
                        telegram_photo_file_id,
                        draft_id,
                    ),
                )
            except sqlite3.IntegrityError:
                return None
            value = connection.execute(
                "SELECT * FROM posts WHERE id = ?", (int(cursor.lastrowid),)
            ).fetchone()
            return self._row(value)

    def get(self, post_id: int) -> ModerationPost | None:
        with self._connection() as connection:
            return self._row(
                connection.execute(
                    "SELECT * FROM posts WHERE id = ?", (int(post_id),)
                ).fetchone()
            )

    def get_by_draft(self, draft_id: str) -> ModerationPost | None:
        with self._connection() as connection:
            return self._row(
                connection.execute(
                    "SELECT * FROM posts WHERE draft_id = ?", (draft_id,)
                ).fetchone()
            )

    def claim(
        self,
        status: str,
        active_status: str,
        *,
        limit: int = 5,
        max_attempts: int = 3,
        stale_minutes: int = 15,
    ) -> list[ModerationPost]:
        """Atomically claims jobs and reclaims abandoned jobs after a lease timeout."""

        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            values = connection.execute(
                """
                SELECT id FROM posts
                WHERE attempts < ?
                  AND (
                    status = ?
                    OR (
                      status = ?
                      AND updated_at <= datetime('now', ?)
                    )
                  )
                ORDER BY id LIMIT ?
                """,
                (
                    int(max_attempts),
                    status,
                    active_status,
                    f"-{int(stale_minutes)} minutes",
                    int(limit),
                ),
            ).fetchall()
            ids = [int(value["id"]) for value in values]
            if not ids:
                return []
            placeholders = ",".join("?" for _ in ids)
            connection.execute(
                f"""
                UPDATE posts
                SET status = ?, attempts = attempts + 1,
                    last_error = NULL, updated_at = datetime('now')
                WHERE id IN ({placeholders})
                """,
                (active_status, *ids),
            )
            claimed = connection.execute(
                f"SELECT * FROM posts WHERE id IN ({placeholders}) ORDER BY id",
                ids,
            ).fetchall()
        return [row for value in claimed if (row := self._row(value)) is not None]

    def update(self, post_id: int, **fields: Any) -> ModerationPost | None:
        allowed = {
            "category_id",
            "status",
            "prompt_message_id",
            "backend_news_id",
            "attempts",
            "last_error",
        }
        values = {key: value for key, value in fields.items() if key in allowed}
        if not values:
            return self.get(post_id)
        assignments = ", ".join(f"{key} = ?" for key in values)
        with self._connection() as connection:
            connection.execute(
                f"UPDATE posts SET {assignments}, updated_at = datetime('now') "
                "WHERE id = ?",
                (*values.values(), int(post_id)),
            )
        return self.get(post_id)

    def stats(self) -> dict[str, int]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) AS count FROM posts GROUP BY status"
            ).fetchall()
        return {str(row["status"]): int(row["count"]) for row in rows}

    def purge_old(self, retention_days: int) -> int:
        with self._connection() as connection:
            cursor = connection.execute(
                """
                DELETE FROM posts
                WHERE updated_at <= datetime('now', ?)
                """,
                (f"-{int(retention_days)} days",),
            )
            return max(0, int(cursor.rowcount))
