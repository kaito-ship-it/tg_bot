from __future__ import annotations

import os

from app.config import load_settings


def main() -> None:
    settings = load_settings()
    settings.validate_runtime()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    if not os.access(settings.data_dir, os.R_OK | os.W_OK):
        raise PermissionError(
            f"DATA_DIR is not readable and writable: {settings.data_dir}"
        )
    print("Configuration check passed")


if __name__ == "__main__":
    main()
