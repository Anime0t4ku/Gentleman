from __future__ import annotations

import json
from pathlib import Path


class ArcadeNameDatabase:
    def __init__(self, path: Path):
        self.path = path
        self._names: dict[str, str] | None = None

    def names(self) -> dict[str, str]:
        if self._names is not None:
            return self._names

        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self._names = {
                    str(key).lower(): str(value)
                    for key, value in data.items()
                    if key and value
                }
            else:
                self._names = {}
        except Exception:
            self._names = {}

        return self._names

    def display_name(self, rom_name: str) -> str | None:
        shortname = Path(rom_name).stem.lower()
        return self.names().get(shortname)
