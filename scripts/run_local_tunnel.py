from __future__ import annotations

import hashlib
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parent.parent
CLOUDFLARED_VERSION = "2026.5.2"
CLOUDFLARED_SHA256 = (
    "20b9638f685333d623798e733effbad2487093f15ba592f6c7752360ff3b7ab7"
)
CLOUDFLARED_URL = (
    "https://github.com/cloudflare/cloudflared/releases/download/"
    f"{CLOUDFLARED_VERSION}/cloudflared-windows-amd64.exe"
)
QUICK_TUNNEL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _cloudflared() -> Path:
    target = PROJECT_DIR / "data" / "tools" / "cloudflared.exe"
    if target.is_file() and _sha256(target) == CLOUDFLARED_SHA256:
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".exe.download")
    temporary.unlink(missing_ok=True)
    print("Загружаю официальный cloudflared для временного HTTPS-туннеля…")
    urllib.request.urlretrieve(CLOUDFLARED_URL, temporary)
    actual = _sha256(temporary)
    if actual != CLOUDFLARED_SHA256:
        temporary.unlink(missing_ok=True)
        raise RuntimeError(
            "Контрольная сумма cloudflared не совпала; запуск отменён"
        )
    temporary.replace(target)
    return target


def _port_is_busy(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:
        connection.settimeout(0.3)
        return connection.connect_ex(("127.0.0.1", port)) == 0


def _stop(process: subprocess.Popen[str] | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def main() -> None:
    port = 8000
    if _port_is_busy(port):
        raise SystemExit(
            "Порт 8000 уже занят. Остановите текущий python -m app.main "
            "через Ctrl+C и запустите эту команду снова."
        )

    binary = _cloudflared()
    tunnel: subprocess.Popen[str] | None = None
    application: subprocess.Popen[str] | None = None
    try:
        tunnel = subprocess.Popen(
            [
                str(binary),
                "tunnel",
                "--no-autoupdate",
                "--url",
                f"http://127.0.0.1:{port}",
            ],
            cwd=PROJECT_DIR,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        deadline = time.monotonic() + 45
        public_base: str | None = None
        while time.monotonic() < deadline:
            if tunnel.poll() is not None:
                raise RuntimeError("cloudflared завершился до создания туннеля")
            line = tunnel.stdout.readline() if tunnel.stdout is not None else ""
            if line:
                match = QUICK_TUNNEL_RE.search(line)
                if match:
                    public_base = match.group(0)
                    break
        if public_base is None:
            raise RuntimeError("Не удалось получить адрес Quick Tunnel за 45 секунд")

        print(f"Временный адрес фотографий: {public_base}")
        print("Запускаю tg2site. Для остановки приложения и туннеля нажмите Ctrl+C.")
        environment = os.environ.copy()
        environment.update(
            {
                "PUBLIC_API_BASE": public_base,
                "API_HOST": "127.0.0.1",
                "API_PORT": str(port),
                "TELEGRAM_INGEST_MODE": "telethon",
                "PUBLISH_MODE": "backend_api",
            }
        )
        application = subprocess.Popen(
            [sys.executable, "-m", "app.main"],
            cwd=PROJECT_DIR,
            env=environment,
        )
        exit_code = application.wait()
        if exit_code:
            raise SystemExit(exit_code)
    except KeyboardInterrupt:
        pass
    finally:
        _stop(application)
        _stop(tunnel)


if __name__ == "__main__":
    main()
