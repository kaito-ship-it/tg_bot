from __future__ import annotations

import hmac
import ipaddress
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.config import Settings
from app.draft_store import DraftStore
from app.processing_queue import ProcessingQueue
from app.publication_queue import PublicationQueue


TELEGRAM_NETWORKS = (
    ipaddress.ip_network("149.154.160.0/20"),
    ipaddress.ip_network("91.108.4.0/22"),
)


class PublicationResult(BaseModel):
    token: str = Field(min_length=20, max_length=200)
    success: bool
    error: str | None = Field(default=None, max_length=2000)


def create_app(
    store: DraftStore,
    settings: Settings,
    queue: ProcessingQueue | None = None,
    publication_queue: PublicationQueue | None = None,
    webhook_handler: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    webhook_stats: Callable[[], dict[str, int]] | None = None,
) -> FastAPI:
    app = FastAPI(title="tg2site draft API", docs_url=None, redoc_url=None)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )

    @app.get("/health")
    def health() -> dict[str, object]:
        result: dict[str, object] = {"status": "ok"}
        if queue is not None:
            result["queue"] = queue.stats()
        if publication_queue is not None:
            result["publication_queue"] = publication_queue.stats()
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
            payload = await request.json()
            if not isinstance(payload, dict):
                return Response(status_code=400)
            await webhook_handler(payload)
            return Response(status_code=200)

    @app.get("/draft/{draft_id}")
    def get_draft(draft_id: str) -> dict[str, object]:
        draft = store.get(draft_id)
        if draft is None:
            raise HTTPException(status_code=404, detail="Draft not found or expired")
        return draft.to_api_dict(settings.public_api_base)

    @app.get("/photo/{draft_id}")
    def get_photo(draft_id: str) -> FileResponse:
        draft = store.get(draft_id)
        if draft is None:
            raise HTTPException(status_code=404, detail="Draft not found or expired")
        photo_path = store.photo_path(draft)
        if photo_path is None:
            raise HTTPException(status_code=404, detail="Photo not found")
        return FileResponse(
            photo_path,
            filename=photo_path.name,
            headers={"Access-Control-Allow-Origin": "*"},
        )

    @app.post("/publication/{draft_id}/result")
    def report_publication_result(
        draft_id: str, result: PublicationResult
    ) -> dict[str, str]:
        if publication_queue is None:
            raise HTTPException(status_code=404, detail="Auto-publication is disabled")
        job = publication_queue.report_result(
            draft_id,
            result.token,
            success=result.success,
            error=result.error,
        )
        if job is None:
            raise HTTPException(status_code=403, detail="Invalid publication token")
        return {"status": job.status}

    return app
