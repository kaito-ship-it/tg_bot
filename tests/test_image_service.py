import asyncio
import base64
from io import BytesIO

import pytest
from PIL import Image

from app import image_service
from app.image_service import ImageGenerationError, OpenAIImageService


def _jpeg_base64() -> str:
    output = BytesIO()
    Image.new("RGB", (32, 20), "navy").save(output, format="JPEG")
    return base64.b64encode(output.getvalue()).decode("ascii")


def test_openai_image_service_saves_jpeg(monkeypatch, tmp_path) -> None:
    captured = {}

    class Response:
        ok = True
        headers = {}

        def json(self):
            return {"data": [{"b64_json": _jpeg_base64()}]}

    def fake_post(url, *, headers, json, timeout):
        captured.update(
            url=url,
            headers=headers,
            json=json,
            timeout=timeout,
        )
        return Response()

    monkeypatch.setattr(image_service.requests, "post", fake_post)
    service = OpenAIImageService(
        api_key="test-key",
        model="gpt-image-1-mini",
        quality="medium",
        size="1536x1024",
    )
    target = tmp_path / "cover.jpg"

    filename = asyncio.run(
        service.generate_cover(
            title="Новая технология",
            news_text="Подробный текст новости.",
            target=target,
        )
    )

    assert filename == "cover.jpg"
    with Image.open(target) as saved:
        assert saved.format == "JPEG"
        assert saved.mode == "RGB"
    assert captured["url"] == image_service.OPENAI_IMAGE_ENDPOINT
    assert captured["headers"]["Authorization"] == "Bearer test-key"
    assert captured["json"]["model"] == "gpt-image-1-mini"
    assert captured["json"]["quality"] == "medium"
    assert captured["json"]["size"] == "1536x1024"
    assert captured["json"]["output_format"] == "jpeg"
    assert captured["timeout"] == (10, 180)


def test_openai_image_service_retries_temporary_failure(monkeypatch, tmp_path) -> None:
    attempts = 0
    delays = []

    class Response:
        headers = {}
        reason = "temporary failure"

        def __init__(self, ok: bool, status_code: int) -> None:
            self.ok = ok
            self.status_code = status_code

        def json(self):
            if self.ok:
                return {"data": [{"b64_json": _jpeg_base64()}]}
            return {"error": {"message": "try again"}}

    def fake_post(*args, **kwargs):
        nonlocal attempts
        del args, kwargs
        attempts += 1
        return Response(attempts > 1, 200 if attempts > 1 else 500)

    monkeypatch.setattr(image_service.requests, "post", fake_post)
    monkeypatch.setattr(image_service.time, "sleep", delays.append)
    service = OpenAIImageService(
        api_key="test-key",
        model="gpt-image-1-mini",
        quality="medium",
        size="1536x1024",
    )

    asyncio.run(
        service.generate_cover(
            title="Новость",
            news_text="Текст",
            target=tmp_path / "retry.jpg",
        )
    )

    assert attempts == 2
    assert delays == [1]


def test_openai_image_service_does_not_retry_auth_error(monkeypatch, tmp_path) -> None:
    attempts = 0

    class Response:
        ok = False
        status_code = 401
        headers = {}
        reason = "unauthorized"

        def json(self):
            return {"error": {"message": "invalid key"}}

    def fake_post(*args, **kwargs):
        nonlocal attempts
        del args, kwargs
        attempts += 1
        return Response()

    monkeypatch.setattr(image_service.requests, "post", fake_post)
    monkeypatch.setattr(
        image_service.time,
        "sleep",
        lambda delay: pytest.fail(f"unexpected retry delay: {delay}"),
    )
    service = OpenAIImageService(
        api_key="bad-key",
        model="gpt-image-1-mini",
        quality="medium",
        size="1536x1024",
    )

    with pytest.raises(ImageGenerationError, match="HTTP 401"):
        asyncio.run(
            service.generate_cover(
                title="Новость",
                news_text="Текст",
                target=tmp_path / "auth.jpg",
            )
        )

    assert attempts == 1
