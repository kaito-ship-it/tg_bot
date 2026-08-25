from __future__ import annotations

import argparse
from typing import Any

import requests

from app.config import load_settings


def _telegram_call(
    bot_token: str, method: str, payload: dict[str, Any]
) -> dict[str, Any]:
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{bot_token}/{method}",
            json=payload,
            timeout=(10, 30),
        )
    except requests.RequestException:
        raise RuntimeError(f"Telegram API {method} network request failed") from None
    try:
        body = response.json()
    except ValueError:
        body = {}
    if response.status_code >= 400 or body.get("ok") is not True:
        description = str(body.get("description", "unknown error"))[:300]
        raise RuntimeError(
            f"Telegram API {method} returned HTTP {response.status_code}: {description}"
        )
    return body


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Register the tg2site Telegram webhook"
    )
    parser.add_argument(
        "--drop-pending",
        action="store_true",
        help="discard queued Telegram updates; use only on the first installation",
    )
    args = parser.parse_args()
    settings = load_settings()
    settings.validate_runtime()
    webhook_url = f"{settings.public_api_base}/webhook"
    _telegram_call(
        settings.bot_token,
        "setWebhook",
        {
            "url": webhook_url,
            "secret_token": settings.tg_webhook_secret,
            "allowed_updates": ["channel_post", "callback_query"],
            "drop_pending_updates": args.drop_pending,
            "max_connections": 20,
        },
    )
    info = _telegram_call(settings.bot_token, "getWebhookInfo", {})
    result = info.get("result", {})
    if result.get("url") != webhook_url:
        raise RuntimeError("Telegram returned an unexpected webhook URL")
    last_error = result.get("last_error_message")
    if last_error:
        print(f"Webhook registered; previous Telegram error: {last_error}")
    else:
        print("Webhook registered successfully")


if __name__ == "__main__":
    main()
