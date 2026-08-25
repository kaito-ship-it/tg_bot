from __future__ import annotations

import asyncio
import html
import logging
from typing import Any

import requests

from app.models import NewsDraft

logger = logging.getLogger(__name__)
REGENERATE_PHOTO_PREFIX = "regenerate_photo:"
SELECT_CATEGORY_PREFIX = "select_category:"
REJECT_DRAFT_PREFIX = "reject_draft:"


class TelegramNotificationError(RuntimeError):
    """A sanitized Telegram API error that never contains the bot token URL."""


class TelegramNotifier:
    def __init__(self, bot_token: str, chat_id: str) -> None:
        self.bot_token = bot_token
        self.chat_id = chat_id

    def _method_url(self, method: str) -> str:
        return f"https://api.telegram.org/bot{self.bot_token}/{method}"

    def _post(
        self,
        method: str,
        payload: dict[str, object],
        *,
        timeout: int | tuple[int, int] = 30,
    ) -> dict[str, Any]:
        try:
            response = requests.post(
                self._method_url(method),
                json=payload,
                timeout=timeout,
            )
        except requests.RequestException:
            raise TelegramNotificationError(
                f"Telegram API {method} network request failed"
            ) from None

        status_code = int(getattr(response, "status_code", 0) or 0)
        try:
            body = response.json()
        except (ValueError, TypeError):
            body = {}
        description = (
            str(body.get("description", "")).strip()[:300]
            if isinstance(body, dict)
            else ""
        )
        if status_code >= 400 or (isinstance(body, dict) and body.get("ok") is False):
            suffix = f": {description}" if description else ""
            raise TelegramNotificationError(
                f"Telegram API {method} returned HTTP {status_code}{suffix}"
            )
        return body if isinstance(body, dict) else {}

    async def send_draft_for_confirmation(
        self,
        draft: NewsDraft,
        categories: tuple[object, ...] = (),
    ) -> int | None:
        return await asyncio.to_thread(self._send_with_retry, draft, categories)

    def _send_with_retry(
        self,
        draft: NewsDraft,
        categories: tuple[object, ...],
    ) -> int | None:
        last_error: TelegramNotificationError | None = None
        for attempt in range(1, 4):
            try:
                return self._send(draft, categories)
            except TelegramNotificationError as exc:
                last_error = exc
                logger.warning(
                    "Telegram notification attempt %s/3 failed: %s",
                    attempt,
                    exc,
                )
        if last_error is not None:
            raise last_error
        return None

    def _send(
        self,
        draft: NewsDraft,
        categories: tuple[object, ...],
    ) -> int | None:
        payload = self._post(
            "sendMessage",
            {
                "chat_id": self.chat_id,
                "text": self._draft_message_text(draft),
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
                "reply_markup": self._draft_keyboard(draft, categories=categories),
            },
        )
        result = payload.get("result")
        message_id = result.get("message_id") if isinstance(result, dict) else None
        return message_id if isinstance(message_id, int) else None

    @staticmethod
    def _draft_message_text(
        draft: NewsDraft,
        *,
        status: str | None = None,
    ) -> str:
        category = draft.category_id or "выбрать вручную"
        preview = draft.text[:700]
        if len(draft.text) > len(preview):
            preview += "…"
        heading = status or "Черновик новости готов"
        return (
            f"<b>{heading}</b>\n\n"
            f"<b>{html.escape(draft.title)}</b>\n"
            f"Категория: {html.escape(str(category))}\n"
            f"Фото: {'сгенерировано AI' if draft.photo_is_generated else ('есть' if draft.photo_filename else 'нет')}\n\n"
            f"{html.escape(preview)}"
        )

    @staticmethod
    def _draft_keyboard(
        draft: NewsDraft,
        *,
        categories: tuple[object, ...] = (),
    ) -> dict[str, object]:
        rows: list[list[dict[str, str]]] = []
        category_buttons: list[dict[str, str]] = []
        for category in categories:
            category_id = getattr(category, "id", None)
            name = getattr(category, "name", None)
            if isinstance(category_id, int) and isinstance(name, str):
                prefix = "✓ " if draft.category_id == category_id else ""
                category_buttons.append(
                    {
                        "text": f"{prefix}{name}",
                        "callback_data": (
                            f"{SELECT_CATEGORY_PREFIX}{draft.draft_id}:{category_id}"
                        ),
                    }
                )
        rows.extend(
            category_buttons[index : index + 2]
            for index in range(0, len(category_buttons), 2)
        )
        rows.append(
            [
                {
                    "text": "🚫 Не публиковать",
                    "callback_data": f"{REJECT_DRAFT_PREFIX}{draft.draft_id}",
                }
            ]
        )
        if draft.photo_is_generated:
            rows.append(
                [
                    {
                        "text": "Сгенерировать фото заново",
                        "callback_data": (f"{REGENERATE_PHOTO_PREFIX}{draft.draft_id}"),
                    }
                ]
            )
        return {"inline_keyboard": rows}

    async def answer_callback(self, callback_id: str, text: str) -> None:
        await asyncio.to_thread(self._answer_callback, callback_id, text)

    def _answer_callback(self, callback_id: str, text: str) -> None:
        self._post(
            "answerCallbackQuery",
            {"callback_query_id": callback_id, "text": text[:200]},
        )

    async def update_draft_message(
        self,
        *,
        chat_id: str,
        message_id: int,
        draft: NewsDraft,
        status: str,
        categories: tuple[object, ...] = (),
    ) -> None:
        await asyncio.to_thread(
            self._update_draft_message,
            chat_id,
            message_id,
            draft,
            status,
            categories,
        )

    def _update_draft_message(
        self,
        chat_id: str,
        message_id: int,
        draft: NewsDraft,
        status: str,
        categories: tuple[object, ...],
    ) -> None:
        self._post(
            "editMessageText",
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": self._draft_message_text(draft, status=status),
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
                "reply_markup": self._draft_keyboard(draft, categories=categories),
            },
        )

    async def edit_status_message(
        self,
        *,
        chat_id: str,
        message_id: int,
        text: str,
        url: str | None = None,
    ) -> None:
        await asyncio.to_thread(
            self._edit_status_message,
            chat_id,
            message_id,
            text,
            url,
        )

    def _edit_status_message(
        self,
        chat_id: str,
        message_id: int,
        text: str,
        url: str | None,
    ) -> None:
        self._post(
            "editMessageText",
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
                "reply_markup": {
                    "inline_keyboard": (
                        [[{"text": "Открыть новость", "url": url}]] if url else []
                    )
                },
            },
        )

    async def send_processing_failure(self, message_id: int, error: str) -> None:
        await asyncio.to_thread(self._send_processing_failure, message_id, error)

    def _send_processing_failure(self, message_id: int, error: str) -> None:
        self._post(
            "sendMessage",
            {
                "chat_id": self.chat_id,
                "text": (
                    "<b>Не удалось подготовить новость после трёх попыток</b>\n\n"
                    f"Telegram ID: {message_id}\n"
                    f"Ошибка: {html.escape(error[:700])}\n\n"
                    "Задание сохранено со статусом parse_failed."
                ),
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
        )

    async def send_publication_failure(
        self,
        *,
        draft: NewsDraft,
        error: str,
        fallback_link: str,
    ) -> None:
        await asyncio.to_thread(
            self._send_publication_failure,
            draft,
            error,
            fallback_link,
        )

    def _send_publication_failure(
        self,
        draft: NewsDraft,
        error: str,
        fallback_link: str,
    ) -> None:
        self._post(
            "sendMessage",
            {
                "chat_id": self.chat_id,
                "text": (
                    "<b>Автопубликация не завершена</b>\n\n"
                    f"{html.escape(draft.title)}\n"
                    f"Ошибка: {html.escape(error[:700])}\n\n"
                    "Проверьте сайт перед повторной отправкой, чтобы не создать дубль."
                ),
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
                "reply_markup": {
                    "inline_keyboard": [
                        [{"text": "Открыть вручную", "url": fallback_link}]
                    ]
                },
            },
        )
