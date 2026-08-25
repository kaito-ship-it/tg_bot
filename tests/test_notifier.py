import pytest
import requests

from app.models import NewsDraft
from app.notifier import (
    REGENERATE_PHOTO_PREFIX,
    TelegramNotificationError,
    TelegramNotifier,
)


def _draft(*, generated: bool) -> NewsDraft:
    return NewsDraft.create(
        draft_id="a" * 32,
        title="Новость",
        text="Текст",
        category_id=9,
        source_message_id=1,
        photo_filename="cover.jpg",
        photo_is_generated=generated,
        photo_revision=1 if generated else 0,
    )


def test_regenerate_button_is_shown_only_for_ai_photo() -> None:
    generated_keyboard = TelegramNotifier._draft_keyboard(_draft(generated=True))
    source_keyboard = TelegramNotifier._draft_keyboard(_draft(generated=False))

    generated_rows = generated_keyboard["inline_keyboard"]
    source_rows = source_keyboard["inline_keyboard"]
    assert len(generated_rows) == 2
    assert generated_rows[1][0]["callback_data"] == (
        f"{REGENERATE_PHOTO_PREFIX}{'a' * 32}"
    )
    assert len(source_rows) == 1


def test_request_error_does_not_expose_bot_token(monkeypatch) -> None:
    token = "123456:very-secret-bot-token"
    notifier = TelegramNotifier(token, "-1001")

    def fail(*args, **kwargs):
        del args, kwargs
        raise requests.RequestException(
            f"request failed at https://api.telegram.org/bot{token}/sendMessage"
        )

    monkeypatch.setattr("app.notifier.requests.post", fail)

    with pytest.raises(TelegramNotificationError) as caught:
        notifier._post("sendMessage", {"chat_id": "-1001"})

    assert token not in str(caught.value)
