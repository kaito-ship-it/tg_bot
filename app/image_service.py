from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import time
from io import BytesIO
from pathlib import Path

import requests
from PIL import Image, ImageOps


OPENAI_IMAGE_ENDPOINT = "https://api.openai.com/v1/images/generations"
MAX_AI_ATTEMPTS = 3
logger = logging.getLogger(__name__)


class ImageGenerationError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


def _is_retryable_status(status_code: int) -> bool:
    return status_code in {408, 409, 425, 429} or status_code >= 500


def _make_prompt(title: str, news_text: str, *, regenerate: bool = False) -> str:
    article_excerpt = " ".join(news_text.split())[:3500]
    retry_instruction = (
        "The previous image was rejected as irrelevant. Create a clearly "
        "different composition and anchor every visible subject in the article's "
        "central factual topic.\n"
        if regenerate
        else ""
    )
    return (
        "Create a realistic editorial cover image for a Russian-language news "
        "website. Use a landscape composition suitable for a news card. Show the "
        "main subject of the article clearly, with natural lighting and a "
        "professional documentary-photo style. Do not use unrelated symbolic "
        "objects or invent unsupported events, locations, equipment, or people. "
        "Prefer a neutral but directly relevant industry or institutional scene "
        "when the article is abstract. Do not add any text, captions, "
        "letters, logos, watermarks, borders, or interface elements. Do not "
        "invent a recognizable real person's face.\n"
        f"{retry_instruction}\n"
        f"Article title: {title}\n"
        f"Article summary: {article_excerpt}"
    )


class OpenAIImageService:
    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        quality: str,
        size: str,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.quality = quality
        self.size = size

    async def generate_cover(
        self,
        *,
        title: str,
        news_text: str,
        target: Path,
        regenerate: bool = False,
    ) -> str:
        return await asyncio.to_thread(
            self._generate_cover_sync,
            title=title,
            news_text=news_text,
            target=target,
            regenerate=regenerate,
        )

    def _generate_cover_sync(
        self,
        *,
        title: str,
        news_text: str,
        target: Path,
        regenerate: bool = False,
    ) -> str:
        image_bytes: bytes | None = None
        for attempt in range(1, MAX_AI_ATTEMPTS + 1):
            try:
                image_bytes = self._request_image(
                    title=title,
                    news_text=news_text,
                    regenerate=regenerate,
                )
                break
            except ImageGenerationError as exc:
                if not exc.retryable or attempt == MAX_AI_ATTEMPTS:
                    raise
                delay = 2 ** (attempt - 1)
                logger.warning(
                    "OpenAI image attempt %s/%s failed; retrying in %ss: %s",
                    attempt,
                    MAX_AI_ATTEMPTS,
                    delay,
                    exc,
                )
                time.sleep(delay)
        if image_bytes is None:
            raise ImageGenerationError("OpenAI image generation failed")

        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(target.suffix + ".tmp")
        try:
            with Image.open(BytesIO(image_bytes)) as source:
                image = ImageOps.exif_transpose(source).convert("RGB")
                image.save(temporary, format="JPEG", quality=90, optimize=True)
            temporary.replace(target)
        except (OSError, ValueError) as exc:
            temporary.unlink(missing_ok=True)
            raise ImageGenerationError(
                "OpenAI returned an unreadable image"
            ) from exc

        return target.name

    def _request_image(
        self, *, title: str, news_text: str, regenerate: bool
    ) -> bytes:
        try:
            response = requests.post(
                OPENAI_IMAGE_ENDPOINT,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.model,
                    "prompt": _make_prompt(
                        title, news_text, regenerate=regenerate
                    ),
                    "n": 1,
                    "size": self.size,
                    "quality": self.quality,
                    "output_format": "jpeg",
                    "output_compression": 90,
                },
                timeout=(10, 180),
            )
        except requests.RequestException as exc:
            raise ImageGenerationError(
                f"OpenAI image request failed: {exc}", retryable=True
            ) from exc

        if not response.ok:
            status_code = int(response.status_code)
            request_id = response.headers.get("x-request-id", "unknown")
            try:
                message = response.json().get("error", {}).get("message", "")
            except ValueError:
                message = ""
            detail = message[:300] or response.reason or "request failed"
            raise ImageGenerationError(
                f"OpenAI returned HTTP {status_code}: {detail} "
                f"(request_id={request_id})",
                retryable=_is_retryable_status(status_code),
            )

        try:
            encoded = response.json()["data"][0]["b64_json"]
            image_bytes = base64.b64decode(encoded, validate=True)
        except (KeyError, IndexError, TypeError, ValueError, binascii.Error) as exc:
            raise ImageGenerationError(
                "OpenAI response did not contain a valid image", retryable=True
            ) from exc
        return image_bytes


def create_image_service(settings):
    """Create the configured fallback provider, or disable generation.

    The untyped settings argument avoids coupling this small provider module to
    application startup and keeps it easy to unit-test.
    """
    if settings.image_fallback_mode == "disabled":
        return None
    return OpenAIImageService(
        api_key=settings.openai_api_key,
        model=settings.openai_image_model,
        quality=settings.openai_image_quality,
        size=settings.openai_image_size,
    )
