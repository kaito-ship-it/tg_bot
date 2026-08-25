import asyncio
from types import SimpleNamespace
from typing import ClassVar

import pytest
import requests

from app.telegram_media import TelegramBotMediaClient, TelegramMediaError


def test_bot_api_photo_is_downloaded(monkeypatch, tmp_path) -> None:
    class MetadataResponse:
        status_code = 200

        def json(self):
            return {"ok": True, "result": {"file_path": "photos/image.jpg"}}

    class FileResponse:
        status_code = 200
        headers: ClassVar[dict[str, str]] = {"content-length": "4"}

        def iter_content(self, chunk_size):
            del chunk_size
            yield b"jpeg"

        def close(self):
            pass

    monkeypatch.setattr(
        "app.telegram_media.requests.post", lambda *a, **k: MetadataResponse()
    )
    monkeypatch.setattr(
        "app.telegram_media.requests.get", lambda *a, **k: FileResponse()
    )
    target = tmp_path / "photo.jpg"

    downloaded = asyncio.run(
        TelegramBotMediaClient("test-token").download_media(
            SimpleNamespace(photo="file-id"),
            file=str(target),
        )
    )

    assert downloaded == str(target)
    assert target.read_bytes() == b"jpeg"


def test_bot_api_media_error_does_not_expose_token(monkeypatch, tmp_path) -> None:
    token = "123456:very-secret-token"

    def fail(*args, **kwargs):
        del args, kwargs
        raise requests.RequestException(
            f"failed https://api.telegram.org/bot{token}/getFile"
        )

    monkeypatch.setattr("app.telegram_media.requests.post", fail)

    with pytest.raises(TelegramMediaError) as caught:
        asyncio.run(
            TelegramBotMediaClient(token).download_media(
                SimpleNamespace(photo="file-id"),
                file=str(tmp_path / "photo.jpg"),
            )
        )

    assert token not in str(caught.value)
