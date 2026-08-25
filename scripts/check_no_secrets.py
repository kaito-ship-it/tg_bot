from __future__ import annotations

import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "data",
    "htmlcov",
    "venv",
}
BLOCKED_FILES = {
    ROOT / ".env",
    ROOT / "data" / "admin_auth.json",
}
SECRET_PATTERNS = {
    "Telegram bot token": re.compile(r"\b\d{8,12}:[A-Za-z0-9_-]{30,}\b"),
    "OpenAI API key": re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b"),
    "Telegram API hash": re.compile(r"(?m)^TG_API_HASH=[a-fA-F0-9]{32}\s*$"),
    "backend API token": re.compile(
        r"(?m)^NEWS_BOT_API_TOKEN=[a-fA-F0-9]{32,}\s*$"
    ),
    "personal Windows path": re.compile(r"[A-Za-z]:\\Users\\[^\\\s]+\\"),
}


def iter_text_files() -> list[Path]:
    files: list[Path] = []
    for path in ROOT.rglob("*"):
        if not path.is_file() or any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.stat().st_size <= 2_000_000:
            files.append(path)
    return files


def main() -> int:
    findings: list[str] = []
    for path in BLOCKED_FILES:
        if path.exists():
            findings.append(f"blocked local file: {path.relative_to(ROOT)}")

    for path in ROOT.rglob("*.session*"):
        if path.is_file() and not any(part in SKIP_DIRS for part in path.parts):
            findings.append(f"Telegram session: {path.relative_to(ROOT)}")

    for path in iter_text_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for label, pattern in SECRET_PATTERNS.items():
            if pattern.search(text):
                findings.append(f"{label}: {path.relative_to(ROOT)}")

    if findings:
        print("Secret check failed:", file=sys.stderr)
        for finding in sorted(set(findings)):
            print(f"- {finding}", file=sys.stderr)
        return 1

    print("Secret check passed: no local credentials or known secret formats found.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
