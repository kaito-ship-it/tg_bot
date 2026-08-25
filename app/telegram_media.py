from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests

MAX_TELEGRAM_PHOTO_BYTES = 12 * 1024 * 1024


class TelegramMediaError(RuntimeError):
    """A sanitized Bot API media error that never exposes the bot token."""


class TelegramBotMediaClient:
    def __init__(self, bot_token: str) -> None:
        self.bot_token = bot_token

    async def download_media(self, item: Any, *, file: str) -> str | None:
        file_id = item if isinstance(item, str) else getattr(item, "photo", None)
        if not isinstance(file_id, str) or not file_id:
            return None
        return await asyncio.to_thread(self._download, file_id, Path(file))

    def _download(self, file_id: str, target: Path) -> str:
        try:
            metadata = requests.post(
                f"https://api.telegram.org/bot{self.bot_token}/getFile",
                json={"file_id": file_id},
                timeout=(10, 30),
            )
        except requests.RequestException:
            raise TelegramMediaError(
                "Telegram getFile network request failed"
            ) from None
        try:
            body = metadata.json()
        except ValueError:
            body = {}
        metadata_status = metadata.status_code
        close_metadata = getattr(metadata, "close", None)
        if callable(close_metadata):
            close_metadata()
        result = body.get("result") if isinstance(body, dict) else None
        file_path = result.get("file_path") if isinstance(result, dict) else None
        if metadata_status >= 400 or not isinstance(file_path, str):
            raise TelegramMediaError(
                f"Telegram getFile returned HTTP {metadata_status}"
            )

        safe_path = quote(file_path.lstrip("/"), safe="/")
        try:
            response = requests.get(
                f"https://api.telegram.org/file/bot{self.bot_token}/{safe_path}",
                stream=True,
                timeout=(10, 60),
            )
        except requests.RequestException:
            raise TelegramMediaError("Telegram file download failed") from None
        if response.status_code >= 400:
            raise TelegramMediaError(
                f"Telegram file download returned HTTP {response.status_code}"
            )
        content_length = response.headers.get("content-length", "")
        if content_length.isdigit() and int(content_length) > MAX_TELEGRAM_PHOTO_BYTES:
            raise TelegramMediaError("Telegram photo exceeds the 12 MB limit")

        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_suffix(target.suffix + ".tmp")
        written = 0
        try:
            with temp.open("wb") as output:
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    if not chunk:
                        continue
                    written += len(chunk)
                    if written > MAX_TELEGRAM_PHOTO_BYTES:
                        raise TelegramMediaError(
                            "Telegram photo exceeds the 12 MB limit"
                        )
                    output.write(chunk)
            if written == 0:
                raise TelegramMediaError("Telegram returned an empty photo")
            os.replace(temp, target)
        finally:
            temp.unlink(missing_ok=True)
            response.close()
        return str(target)
