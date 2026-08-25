from __future__ import annotations

import hmac
import ipaddress
import json
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import FastAPI, Header, Request, Response
from fastapi.responses import FileResponse

from app.config import Settings
from app.draft_store import DraftStore
from app.models import media_access_token

TELEGRAM_NETWORKS = (
    ipaddress.ip_network("149.154.160.0/20"),
    ipaddress.ip_network("91.108.4.0/22"),
)


def create_app(
    store: DraftStore,
    settings: Settings,
    *,
    webhook_handler: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    webhook_stats: Callable[[], dict[str, int]] | None = None,
) -> FastAPI:
    app = FastAPI(title="tg2site", docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/health")
    def health() -> dict[str, object]:
        result: dict[str, object] = {"status": "ok"}
        if webhook_stats is not None:
            result["moderation_queue"] = webhook_stats()
        return result

    if webhook_handler is not None:

        @app.post("/webhook")
        async def telegram_webhook(
            request: Request,
            x_telegram_bot_api_secret_token: str = Header(default=""),
        ) -> Response:
            if not hmac.compare_digest(
                x_telegram_bot_api_secret_token,
                settings.tg_webhook_secret,
            ):
                return Response(status_code=403)
            if settings.telegram_webhook_enforce_ips:
                client_host = request.headers.get("x-real-ip")
                if not client_host and request.client is not None:
                    client_host = request.client.host
                try:
                    client_ip = ipaddress.ip_address(client_host or "")
                except ValueError:
                    return Response(status_code=403)
                if not any(client_ip in network for network in TELEGRAM_NETWORKS):
                    return Response(status_code=403)

            content_length = request.headers.get("content-length")
            if (
                content_length
                and content_length.isdigit()
                and int(content_length) > settings.webhook_max_body_bytes
            ):
                return Response(status_code=413)
            body = await request.body()
            if len(body) > settings.webhook_max_body_bytes:
                return Response(status_code=413)
            try:
                payload = json.loads(body)
            except (UnicodeDecodeError, json.JSONDecodeError):
                return Response(status_code=400)
            if not isinstance(payload, dict):
                return Response(status_code=400)
            await webhook_handler(payload)
            return Response(status_code=200)

    @app.get("/photo/{draft_id}")
    def get_photo(draft_id: str, token: str = "") -> Response:
        expected = media_access_token(draft_id, settings.media_signing_secret)
        if not hmac.compare_digest(token, expected):
            return Response(status_code=404)
        draft = store.get(draft_id)
        if draft is None:
            return Response(status_code=404)
        photo_path = store.photo_path(draft)
        if photo_path is None:
            return Response(status_code=404)
        return FileResponse(
            photo_path,
            filename=photo_path.name,
            headers={"Cache-Control": "private, max-age=300"},
        )

    return app
