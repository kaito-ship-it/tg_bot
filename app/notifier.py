from __future__ import annotations

import asyncio
import html
import logging

import requests

from app.models import NewsDraft


logger = logging.getLogger(__name__)
REGENERATE_PHOTO_PREFIX = "regenerate_photo:"
SELECT_CATEGORY_PREFIX = "select_category:"
REJECT_DRAFT_PREFIX = "reject_draft:"


class TelegramNotifier:
    def __init__(self, bot_token: str, chat_id: str) -> None:
        self.bot_token = bot_token
        self.chat_id = chat_id

    async def send_draft_for_confirmation(
        self,
        draft: NewsDraft,
        autofill_link: str | None,
        categories: tuple[object, ...] = (),
    ) -> int | None:
        return await asyncio.to_thread(
            self._send_with_retry,
            draft,
            autofill_link,
            False,
            categories,
        )

    async def send_existing_draft(
        self,
        draft: NewsDraft,
        autofill_link: str | None,
        categories: tuple[object, ...] = (),
    ) -> int | None:
        return await asyncio.to_thread(
            self._send_with_retry,
            draft,
            autofill_link,
            True,
            categories,
        )

    def _send_with_retry(
        self,
        draft: NewsDraft,
        autofill_link: str | None,
        duplicate: bool,
        categories: tuple[object, ...] = (),
    ) -> int | None:
        last_error: Exception | None = None
        for attempt in range(1, 4):
            try:
                return self._send(draft, autofill_link, duplicate, categories)
            except requests.RequestException as exc:
                last_error = exc
                logger.warning("Notification attempt %s failed: %s", attempt, exc)
        if last_error:
            raise last_error
        return None

    def _send(
        self,
        draft: NewsDraft,
        autofill_link: str | None,
        duplicate: bool = False,
        categories: tuple[object, ...] = (),
    ) -> int | None:
        text = self._draft_message_text(draft, duplicate=duplicate)
        keyboard = self._draft_keyboard(
            draft,
            autofill_link,
            categories=categories,
        )
        payload: dict[str, object] = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if keyboard["inline_keyboard"]:
            payload["reply_markup"] = keyboard
        response = requests.post(
            self._method_url("sendMessage"),
            json=payload,
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        result = payload.get("result") if isinstance(payload, dict) else None
        message_id = result.get("message_id") if isinstance(result, dict) else None
        return message_id if isinstance(message_id, int) else None

    def _method_url(self, method: str) -> str:
        return f"https://api.telegram.org/bot{self.bot_token}/{method}"

    @staticmethod
    def _draft_message_text(
        draft: NewsDraft,
        *,
        duplicate: bool = False,
        status: str | None = None,
    ) -> str:
        category = draft.category_id or "выбрать вручную"
        preview = draft.text[:700]
        if len(draft.text) > len(preview):
            preview += "…"
        if status:
            heading = status
        elif duplicate:
            heading = "Такая ссылка уже обработана — возвращаю готовый черновик"
        else:
            heading = "Черновик новости готов"
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
        autofill_link: str | None,
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
        if category_buttons:
            rows.append(
                [
                    {
                        "text": "🚫 Не публиковать",
                        "callback_data": f"{REJECT_DRAFT_PREFIX}{draft.draft_id}",
                    }
                ]
            )
        elif autofill_link:
            rows.append([{"text": "Открыть и проверить", "url": autofill_link}])
        if draft.photo_is_generated:
            rows.append(
                [
                    {
                        "text": "Сгенерировать фото заново",
                        "callback_data": f"{REGENERATE_PHOTO_PREFIX}{draft.draft_id}",
                    }
                ]
            )
        return {"inline_keyboard": rows}

    async def get_callback_updates(
        self, offset: int | None
    ) -> list[dict[str, object]]:
        return await asyncio.to_thread(self._get_callback_updates, offset)

    def _get_callback_updates(self, offset: int | None) -> list[dict[str, object]]:
        payload: dict[str, object] = {
            "timeout": 25,
            "allowed_updates": ["callback_query"],
        }
        if offset is not None:
            payload["offset"] = offset
        response = requests.post(
            self._method_url("getUpdates"),
            json=payload,
            timeout=(10, 35),
        )
        response.raise_for_status()
        body = response.json()
        result = body.get("result", []) if isinstance(body, dict) else []
        if not isinstance(result, list):
            return []
        return [update for update in result if isinstance(update, dict)]

    async def answer_callback(self, callback_id: str, text: str) -> None:
        await asyncio.to_thread(self._answer_callback, callback_id, text)

    def _answer_callback(self, callback_id: str, text: str) -> None:
        response = requests.post(
            self._method_url("answerCallbackQuery"),
            json={"callback_query_id": callback_id, "text": text[:200]},
            timeout=30,
        )
        response.raise_for_status()

    async def update_draft_message(
        self,
        *,
        chat_id: str,
        message_id: int,
        draft: NewsDraft,
        autofill_link: str | None,
        status: str,
        categories: tuple[object, ...] = (),
    ) -> None:
        await asyncio.to_thread(
            self._update_draft_message,
            chat_id,
            message_id,
            draft,
            autofill_link,
            status,
            categories,
        )

    def _update_draft_message(
        self,
        chat_id: str,
        message_id: int,
        draft: NewsDraft,
        autofill_link: str | None,
        status: str,
        categories: tuple[object, ...] = (),
    ) -> None:
        response = requests.post(
            self._method_url("editMessageText"),
            json={
                "chat_id": chat_id,
                "message_id": message_id,
                "text": self._draft_message_text(draft, status=status),
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
                "reply_markup": self._draft_keyboard(
                    draft,
                    autofill_link,
                    categories=categories,
                ),
            },
            timeout=30,
        )
        response.raise_for_status()

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
        payload: dict[str, object] = {
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
        }
        response = requests.post(
            self._method_url("editMessageText"),
            json=payload,
            timeout=30,
        )
        response.raise_for_status()

    async def send_processing_failure(
        self, message_ids: list[int], error: str
    ) -> None:
        await asyncio.to_thread(self._send_processing_failure, message_ids, error)

    def _send_processing_failure(self, message_ids: list[int], error: str) -> None:
        identifiers = ", ".join(str(item) for item in message_ids)
        text = (
            "<b>Не удалось подготовить новость после трёх попыток</b>\n\n"
            f"Telegram ID: {html.escape(identifiers)}\n"
            f"Ошибка: {html.escape(error[:700])}\n\n"
            "Задание сохранено в очереди со статусом failed."
        )
        response = requests.post(
            self._method_url("sendMessage"),
            json={
                "chat_id": self.chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=30,
        )
        response.raise_for_status()

    async def send_publication_result(
        self,
        *,
        draft: NewsDraft,
        success: bool,
        error: str | None,
        fallback_link: str,
        publication_url: str | None = None,
    ) -> None:
        await asyncio.to_thread(
            self._send_publication_result,
            draft,
            success,
            error,
            fallback_link,
            publication_url,
        )

    def _send_publication_result(
        self,
        draft: NewsDraft,
        success: bool,
        error: str | None,
        fallback_link: str,
        publication_url: str | None,
    ) -> None:
        if success:
            text = (
                "<b>Новость автоматически опубликована</b>\n\n"
                f"{html.escape(draft.title)}"
            )
            reply_markup = (
                {
                    "inline_keyboard": [
                        [{"text": "Открыть новость", "url": publication_url}]
                    ]
                }
                if publication_url
                else None
            )
        else:
            text = (
                "<b>Автопубликация не завершена</b>\n\n"
                f"{html.escape(draft.title)}\n"
                f"Ошибка: {html.escape((error or 'неизвестная ошибка')[:700])}\n\n"
                "Проверьте сайт перед повторной отправкой, чтобы не создать дубль."
            )
            reply_markup = {
                "inline_keyboard": [
                    [{"text": "Открыть вручную", "url": fallback_link}]
                ]
            }
        payload: dict[str, object] = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        response = requests.post(
            self._method_url("sendMessage"),
            json=payload,
            timeout=30,
        )
        response.raise_for_status()
