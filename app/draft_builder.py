from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from app.article_service import (
    ArticleExtractionError,
    ExtractedArticle,
    download_article_image,
    extract_first_url,
    fetch_article,
    is_link_only_post,
)
from app.category_service import CategoryClassificationError
from app.config import CATEGORY_KEYWORDS, DEFAULT_CATEGORY_ID, SITE_CATEGORIES
from app.dedup import normalize_source_url
from app.image_service import ImageGenerationError
from app.models import NewsDraft
from app.text_cleaner import clean_news_text

logger = logging.getLogger(__name__)


def _limit_at_word_boundary(value: str, max_len: int) -> str:
    value = " ".join(value.split())
    if len(value) <= max_len:
        return value
    shortened = value[: max_len - 1].rstrip()
    if " " in shortened:
        shortened = shortened.rsplit(" ", 1)[0]
    return shortened.rstrip(".,;:-") + "…"


def extract_title(text: str, max_len: int = 255) -> str:
    for line in text.strip().splitlines():
        line = line.strip()
        if line:
            sentence = re.match(r"^(.+?[.!?…])(?:\s|$)", line)
            candidate = sentence.group(1) if sentence else line
            return _limit_at_word_boundary(candidate, max_len)
    return "Без заголовка"


def match_category_id(text: str) -> int | None:
    normalized = text.casefold()

    def contains_keyword(keyword: str) -> bool:
        normalized_keyword = keyword.casefold()
        if normalized_keyword in {"ии", "it"}:
            return bool(
                re.search(rf"(?<!\w){re.escape(normalized_keyword)}(?!\w)", normalized)
            )
        return normalized_keyword in normalized

    matches = [
        category_name
        for category_name, keywords in CATEGORY_KEYWORDS.items()
        if any(contains_keyword(keyword) for keyword in keywords)
    ]
    if len(matches) == 1:
        return SITE_CATEGORIES[matches[0]]
    return None


def guess_category_id(text: str) -> int:
    matched = match_category_id(text)
    if matched is not None:
        return matched
    # The backend requires a category; the editor can change this fallback
    # before publication.
    return DEFAULT_CATEGORY_ID


def clean_text_for_editor(text: str, title: str | None = None) -> str:
    return clean_news_text(text, title=title)


def extract_album_text(messages: Sequence[Any]) -> str:
    texts: list[str] = []
    for message in messages:
        raw_text = (getattr(message, "raw_text", None) or "").strip()
        if raw_text and raw_text not in texts:
            texts.append(raw_text)
    return "\n\n".join(texts)


async def _download_first_photo(
    messages: Sequence[Any], client: Any, photos_dir: Path, draft_id: str
) -> str | None:
    await asyncio.to_thread(photos_dir.mkdir, parents=True, exist_ok=True)
    for message in messages:
        if not getattr(message, "photo", None):
            continue
        target = photos_dir / f"{draft_id}.jpg"
        downloaded = await client.download_media(message, file=str(target))
        if downloaded:
            return Path(downloaded).name
    return None


def _extract_telegram_web_preview(
    messages: Sequence[Any], article_url: str
) -> tuple[ExtractedArticle | None, Any | None]:
    for message in messages:
        media = getattr(message, "media", None)
        webpage = getattr(media, "webpage", None)
        if webpage is None:
            continue
        title = (getattr(webpage, "title", None) or "").strip()
        description = (getattr(webpage, "description", None) or "").strip()
        photo = getattr(webpage, "photo", None)
        if title and description:
            return (
                ExtractedArticle(
                    url=article_url,
                    title=title,
                    text=description,
                    image_url=None,
                ),
                photo,
            )
    return None, None


async def _download_telegram_preview_photo(
    preview_photo: Any, client: Any, photos_dir: Path, draft_id: str
) -> str | None:
    if preview_photo is None:
        return None
    target = photos_dir / f"{draft_id}.jpg"
    downloaded = await client.download_media(preview_photo, file=str(target))
    return Path(downloaded).name if downloaded else None


async def build_draft(
    messages: Sequence[Any],
    client: Any,
    photos_dir: Path,
    draft_id: str,
    image_service: Any | None = None,
    category_classifier: Any | None = None,
) -> NewsDraft:
    if not messages:
        raise ValueError("Cannot build a draft from an empty message list")

    text = clean_text_for_editor(extract_album_text(messages))
    source_message_id = max(int(message.id) for message in messages)
    article_url = extract_first_url(text)
    article = None
    preview_photo = None
    if article_url and is_link_only_post(text, article_url):
        try:
            article = await fetch_article(article_url)
            text = clean_text_for_editor(article.text, article.title)
        except ArticleExtractionError as exc:
            logger.warning("Could not extract article %s: %s", article_url, exc)
            article, preview_photo = _extract_telegram_web_preview(
                messages, article_url
            )
            if article:
                text = clean_text_for_editor(article.text, article.title)

    photo_filename = await _download_first_photo(messages, client, photos_dir, draft_id)
    if not photo_filename and article and article.image_url:
        try:
            photo_filename = await download_article_image(
                article.image_url, photos_dir / f"{draft_id}.jpg"
            )
        except ArticleExtractionError as exc:
            logger.warning(
                "Could not download article image %s: %s", article.image_url, exc
            )
    if not photo_filename and preview_photo is not None:
        photo_filename = await _download_telegram_preview_photo(
            preview_photo, client, photos_dir, draft_id
        )

    draft_title = extract_title(article.title if article else text)
    category_id = match_category_id(f"{draft_title}\n{text}")
    if category_id is None and category_classifier is not None:
        try:
            category_id = await category_classifier.classify(
                title=draft_title,
                text=text,
            )
        except CategoryClassificationError as exc:
            logger.warning("Could not classify category with AI: %s", exc)
    if category_id is None:
        category_id = DEFAULT_CATEGORY_ID

    photo_is_generated = False
    if not photo_filename and image_service is not None:
        try:
            photo_filename = await image_service.generate_cover(
                title=draft_title,
                news_text=text,
                target=photos_dir / f"{draft_id}.jpg",
            )
            photo_is_generated = bool(photo_filename)
        except ImageGenerationError as exc:
            # A failed optional cover must not prevent delivery of the draft.
            logger.warning("Could not generate an AI cover: %s", exc)

    return NewsDraft.create(
        draft_id=draft_id,
        title=draft_title,
        text=text,
        category_id=category_id,
        source_message_id=source_message_id,
        source_url=article.url if article else article_url,
        photo_filename=photo_filename,
        photo_is_generated=photo_is_generated,
        photo_revision=1 if photo_is_generated else 0,
        source_key=normalize_source_url(article_url),
        source_image_url=article.image_url if article else None,
    )
