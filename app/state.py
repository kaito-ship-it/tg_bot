from __future__ import annotations

import json
import os
from pathlib import Path


class ProcessingState:
    def __init__(self, state_file: Path) -> None:
        self.state_file = state_file
        self.state_file.parent.mkdir(parents=True, exist_ok=True)

    def load_last_processed_id(self) -> int:
        if not self.state_file.exists():
            return 0
        try:
            value = json.loads(self.state_file.read_text(encoding="utf-8"))
            return int(value.get("last_message_id", 0))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return 0

    def save_last_processed_id(self, message_id: int) -> None:
        current = self.load_last_processed_id()
        message_id = max(current, int(message_id))
        temp_path = self.state_file.with_suffix(".json.tmp")
        temp_path.write_text(
            json.dumps({"last_message_id": message_id}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temp_path, self.state_file)

