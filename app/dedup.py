from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

TRACKING_QUERY_NAMES = frozenset(
    {
        "fbclid",
        "gclid",
        "yclid",
        "mc_cid",
        "mc_eid",
        "_ga",
        "_gl",
    }
)


def _is_tracking_parameter(name: str) -> bool:
    normalized = name.casefold()
    return normalized.startswith("utm_") or normalized in TRACKING_QUERY_NAMES


def normalize_source_url(url: str | None) -> str | None:
    if not url:
        return None
    try:
        parsed = urlparse(url.strip())
        if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
            return None
        scheme = parsed.scheme.casefold()
        hostname = parsed.hostname.casefold()
        port = parsed.port
    except ValueError:
        return None

    if port and not (
        (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    ):
        netloc = f"{hostname}:{port}"
    else:
        netloc = hostname

    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    if path != "/":
        path = path.rstrip("/")
    query_items = [
        (name, value)
        for name, value in parse_qsl(parsed.query, keep_blank_values=True)
        if not _is_tracking_parameter(name)
    ]
    query_items.sort(key=lambda item: (item[0].casefold(), item[1]))
    query = urlencode(query_items, doseq=True)
    return urlunparse((scheme, netloc, path, "", query, ""))
