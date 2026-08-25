from __future__ import annotations

import asyncio
import logging
import re
import time

import requests

from app.config import SITE_CATEGORIES


OPENAI_RESPONSES_ENDPOINT = "https://api.openai.com/v1/responses"
ALLOWED_CATEGORY_IDS = frozenset(SITE_CATEGORIES.values())
MAX_AI_ATTEMPTS = 3
logger = logging.getLogger(__name__)


class CategoryClassificationError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


def _is_retryable_status(status_code: int) -> bool:
    return status_code in {408, 409, 425, 429} or status_code >= 500


def _classification_prompt(title: str, text: str) -> str:
    excerpt = " ".join(text.split())[:8000]
    return f"""Choose exactly one category for a Russian-language news article.
Return only its numeric ID and no other text.

Categories:
- 35 Недропользование: subsoil use, mining, deposits, licenses, auctions, extraction.
- 11 Экология: environment, emissions, pollution, waste, remediation.
- 5 Анонс: announcement or invitation to an upcoming event.
- 9 Технологии: technology, digitalization, AI, equipment, innovation.
- 13 Геология: geology, geological research, mapping and exploration science.

Prefer the article's main subject, not a passing mention. If none is a perfect
match, choose the closest category for nedra.kz.

Title: {title}
Article: {excerpt}"""


def _extract_response_text(payload: dict[str, object]) -> str:
    direct = payload.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    output = payload.get("output")
    if not isinstance(output, list):
        return ""
    parts: list[str] = []
    for item in output:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "output_text":
                value = block.get("text")
                if isinstance(value, str):
                    parts.append(value)
    return "\n".join(parts).strip()


class OpenAICategoryClassifier:
    def __init__(self, *, api_key: str, model: str) -> None:
        self.api_key = api_key
        self.model = model

    async def classify(self, *, title: str, text: str) -> int:
        return await asyncio.to_thread(self._classify_sync, title=title, text=text)

    def _classify_sync(self, *, title: str, text: str) -> int:
        for attempt in range(1, MAX_AI_ATTEMPTS + 1):
            try:
                return self._classify_once(title=title, text=text)
            except CategoryClassificationError as exc:
                if not exc.retryable or attempt == MAX_AI_ATTEMPTS:
                    raise
                delay = 2 ** (attempt - 1)
                logger.warning(
                    "OpenAI category attempt %s/%s failed; retrying in %ss: %s",
                    attempt,
                    MAX_AI_ATTEMPTS,
                    delay,
                    exc,
                )
                time.sleep(delay)
        raise CategoryClassificationError("OpenAI category classification failed")

    def _classify_once(self, *, title: str, text: str) -> int:
        try:
            response = requests.post(
                OPENAI_RESPONSES_ENDPOINT,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.model,
                    "input": _classification_prompt(title, text),
                    "reasoning": {"effort": "none"},
                    "max_output_tokens": 20,
                },
                timeout=(10, 60),
            )
        except requests.RequestException as exc:
            raise CategoryClassificationError(
                f"OpenAI category request failed: {exc}", retryable=True
            ) from exc

        if not response.ok:
            status_code = int(response.status_code)
            request_id = response.headers.get("x-request-id", "unknown")
            try:
                message = response.json().get("error", {}).get("message", "")
            except ValueError:
                message = ""
            detail = message[:300] or response.reason or "request failed"
            raise CategoryClassificationError(
                f"OpenAI returned HTTP {status_code}: {detail} "
                f"(request_id={request_id})",
                retryable=_is_retryable_status(status_code),
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise CategoryClassificationError(
                "OpenAI returned invalid category data", retryable=True
            ) from exc
        if not isinstance(payload, dict):
            raise CategoryClassificationError(
                "OpenAI returned invalid category data", retryable=True
            )

        result = _extract_response_text(payload)
        match = re.search(r"(?<!\d)(35|11|5|9|13)(?!\d)", result)
        if not match:
            raise CategoryClassificationError(
                f"OpenAI returned an unsupported category: {result[:100]!r}",
                retryable=True,
            )
        category_id = int(match.group(1))
        if category_id not in ALLOWED_CATEGORY_IDS:
            raise CategoryClassificationError("OpenAI returned an unsupported category")
        return category_id


def create_category_classifier(settings):
    if settings.category_classifier_mode == "disabled":
        return None
    return OpenAICategoryClassifier(
        api_key=settings.openai_api_key,
        model=settings.openai_text_model,
    )
