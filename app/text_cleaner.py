from __future__ import annotations

import re
from urllib.parse import urlparse


ZERO_WIDTH_RE = re.compile(r"[\u200b\u200c\u200d\ufeff]")
COMPARISON_RE = re.compile(r"[^\w]+", re.UNICODE)
URL_ONLY_RE = re.compile(r"^https?://\S+$", re.IGNORECASE)
HASHTAGS_ONLY_RE = re.compile(r"^(?:#[\w-]+[\s,]*)+$", re.UNICODE)
SOCIAL_HOSTS = (
    "t.me",
    "telegram.me",
    "instagram.com",
    "facebook.com",
    "youtube.com",
    "youtu.be",
    "vk.com",
    "x.com",
    "twitter.com",
)
NOISE_LINE_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"^(?:реклама|на правах рекламы)\.?$",
        r"^(?:читайте также|также по теме)\s*:?.*$",
        r"^(?:подписывайтесь|подпишитесь)\b.*$",
        r"^следите за (?:нашими )?новостями\b.*$",
        r"^больше новостей\b.*$",
        r"^наш (?:telegram|телеграм)(?:-канал)?\b.*$",
        r"^все права защищены\.?$",
        r"^источник\s*:\s*https?://\S+$",
    )
)


def _comparison_key(value: str) -> str:
    return COMPARISON_RE.sub("", value.casefold())


def _is_noise_line(line: str) -> bool:
    if HASHTAGS_ONLY_RE.fullmatch(line):
        return True
    if any(pattern.match(line) for pattern in NOISE_LINE_PATTERNS):
        return True
    if not URL_ONLY_RE.fullmatch(line):
        return False
    hostname = (urlparse(line).hostname or "").casefold()
    return any(hostname == host or hostname.endswith("." + host) for host in SOCIAL_HOSTS)


def clean_news_text(text: str, *, title: str | None = None) -> str:
    """Apply conservative cleanup without rewriting or summarizing facts."""
    normalized = ZERO_WIDTH_RE.sub("", text.replace("\xa0", " "))
    normalized = normalized.replace("\r\n", "\n").replace("\r", "\n")
    lines = [" ".join(line.split()) for line in normalized.split("\n")]

    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()

    if title:
        title_key = _comparison_key(title)
        first_content_index = next(
            (index for index, line in enumerate(lines) if line), None
        )
        if first_content_index is not None:
            first_key = _comparison_key(lines[first_content_index])
            if title_key and first_key == title_key:
                lines.pop(first_content_index)
                while lines and not lines[0]:
                    lines.pop(0)

    cleaned: list[str] = []
    previous_content_key = ""
    for line in lines:
        if not line:
            if cleaned and cleaned[-1]:
                cleaned.append("")
            continue
        current_key = _comparison_key(line)
        if current_key and current_key == previous_content_key:
            continue
        cleaned.append(line)
        previous_content_key = current_key

    while cleaned and (not cleaned[-1] or _is_noise_line(cleaned[-1])):
        cleaned.pop()
    while cleaned and not cleaned[0]:
        cleaned.pop(0)

    result = "\n".join(cleaned).strip()
    if result:
        return result
    return "\n".join(line for line in lines if line).strip()
