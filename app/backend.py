from __future__ import annotations

import asyncio
import html
import logging
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import requests

from app.config import Settings
from app.draft_store import DraftStore
from app.models import NewsDraft

logger = logging.getLogger(__name__)
_RETRYABLE_STATUSES = {408, 425, 429}


class BackendAPIError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class NewsCategory:
    id: int
    name: str
    slug: str | None = None


@dataclass(frozen=True, slots=True)
class PublishedNews:
    id: int
    status: str
    url: str


def _content_html(text: str) -> str:
    paragraphs = [part.strip() for part in text.split("\n\n") if part.strip()]
    if not paragraphs:
        paragraphs = [text.strip()]
    return "".join(
        f"<p>{html.escape(part).replace(chr(10), '<br>')}</p>"
        for part in paragraphs
        if part
    )


def _lead(text: str) -> str | None:
    for paragraph in text.split("\n\n"):
        value = " ".join(paragraph.split())
        if value:
            return value[:1000]
    return None


class BackendNewsPublisher:
    """Publishes prepared drafts through Nedra's idempotent internal API."""

    def __init__(self, settings: Settings, store: DraftStore) -> None:
        self.settings = settings
        self.store = store
        self._categories: tuple[NewsCategory, ...] = ()
        self._categories_at = 0.0

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.settings.news_bot_api_token}",
            "Accept": "application/json",
        }

    async def categories(self, *, force: bool = False) -> tuple[NewsCategory, ...]:
        if (
            not force
            and self._categories
            and time.monotonic() - self._categories_at < 3600
        ):
            return self._categories
        categories: tuple[NewsCategory, ...] | None = None
        for attempt in range(1, 6):
            try:
                categories = await asyncio.to_thread(self._fetch_categories)
                break
            except BackendAPIError as exc:
                if not exc.retryable or attempt == 5:
                    raise
                delay = min(30, 2 ** (attempt - 1))
                logger.warning(
                    "Backend categories attempt %s/5 failed; retrying in %ss: %s",
                    attempt,
                    delay,
                    exc,
                )
                await asyncio.sleep(delay)
        if categories is None:
            raise BackendAPIError("Backend categories request failed")
        self._categories = categories
        self._categories_at = time.monotonic()
        return categories

    def _fetch_categories(self) -> tuple[NewsCategory, ...]:
        try:
            response = requests.get(
                f"{self.settings.news_bot_api_base}/news-categories",
                headers=self._headers,
                timeout=(10, 20),
            )
        except requests.RequestException as exc:
            raise BackendAPIError(
                f"Backend categories request failed: {exc}", retryable=True
            ) from exc
        payload = self._response_payload(response, operation="categories")
        raw_categories = payload.get("data")
        if not isinstance(raw_categories, list):
            raise BackendAPIError("Backend returned invalid categories data")
        categories: list[NewsCategory] = []
        for item in raw_categories:
            if not isinstance(item, dict):
                continue
            try:
                category_id = int(item["id"])
                name = str(item["name"]).strip()
            except (KeyError, TypeError, ValueError):
                continue
            if category_id > 0 and name:
                slug = item.get("slug")
                categories.append(
                    NewsCategory(
                        id=category_id,
                        name=name,
                        slug=str(slug) if slug is not None else None,
                    )
                )
        if not categories:
            raise BackendAPIError("Backend returned an empty categories list")
        return tuple(categories)

    async def publish(self, draft: NewsDraft) -> PublishedNews:
        last_error: BackendAPIError | None = None
        for attempt in range(1, 6):
            try:
                return await asyncio.to_thread(self._publish_once, draft)
            except BackendAPIError as exc:
                last_error = exc
                if not exc.retryable or attempt == 5:
                    raise
                delay = min(30, 2 ** (attempt - 1))
                logger.warning(
                    "Backend publication attempt %s/5 failed; retrying in %ss: %s",
                    attempt,
                    delay,
                    exc,
                )
                await asyncio.sleep(delay)
        raise last_error or BackendAPIError("Backend publication failed")

    def _publish_once(self, draft: NewsDraft) -> PublishedNews:
        payload = self._payload(draft)
        try:
            response = requests.post(
                f"{self.settings.news_bot_api_base}/news",
                headers={**self._headers, "Content-Type": "application/json"},
                json=payload,
                timeout=(10, 60),
            )
        except requests.RequestException as exc:
            raise BackendAPIError(
                f"Backend publication request failed: {exc}", retryable=True
            ) from exc
        body = self._response_payload(response, operation="publication")
        data = body.get("data")
        if not isinstance(data, dict):
            raise BackendAPIError("Backend returned invalid publication data")
        try:
            return PublishedNews(
                id=int(data["id"]),
                status=str(data["status"]),
                url=str(data["url"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise BackendAPIError(
                "Backend returned incomplete publication data"
            ) from exc

    def _payload(self, draft: NewsDraft) -> dict[str, Any]:
        if not draft.source_url:
            raise BackendAPIError(
                "Backend API requires a source URL; the Telegram post had no article link"
            )
        category_id = draft.category_id
        if not category_id:
            raise BackendAPIError("A category must be selected before publication")

        channel_key = self.settings.telegram_channel_id
        if not channel_key:
            raise BackendAPIError("Telegram channel ID is not configured")

        image_url = None
        if self.store.photo_path(draft) is not None:
            public = draft.to_api_dict(
                self.settings.public_api_base,
                self.settings.media_signing_secret,
            ).get("photo_url")
            public_host = urlparse(public).hostname if isinstance(public, str) else None
            if isinstance(public, str) and public_host not in {
                None,
                "localhost",
                "127.0.0.1",
                "::1",
            }:
                image_url = public
            else:
                raise BackendAPIError(
                    "The prepared image is only available locally. Configure "
                    "PUBLIC_API_BASE with the public HTTPS /tg address before "
                    "publishing this draft"
                )

        host = urlparse(draft.source_url).hostname or ""
        return {
            "external_id": f"tg:{channel_key}:{draft.source_message_id}"[:190],
            "category_id": int(category_id),
            "title": draft.title[:500],
            "lead": _lead(draft.text),
            "content_html": _content_html(draft.text),
            "source_url": draft.source_url[:2000],
            "source_name": host.removeprefix("www.")[:190] or None,
            "image_url": image_url,
            "published_at": None,
            "status": "published",
        }

    @staticmethod
    def _response_payload(
        response: requests.Response, *, operation: str
    ) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise BackendAPIError(
                f"Backend {operation} returned invalid JSON",
                retryable=response.status_code >= 500,
            ) from exc
        if response.status_code not in {200, 201}:
            detail: Any = payload.get("errors") or payload.get("message")
            if isinstance(detail, dict):
                detail = "; ".join(
                    f"{key}: {', '.join(map(str, value if isinstance(value, list) else [value]))}"
                    for key, value in detail.items()
                )
            message = str(detail or f"HTTP {response.status_code}")[:700]
            retryable = (
                response.status_code in _RETRYABLE_STATUSES
                or response.status_code >= 500
            )
            raise BackendAPIError(
                f"Backend {operation} rejected the request: {message}",
                retryable=retryable,
            )
        if not isinstance(payload, dict):
            raise BackendAPIError(f"Backend {operation} returned invalid data")
        return payload
