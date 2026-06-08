from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

MenuItemType = Literal["folder", "launcher"]

@dataclass
class MenuItem:
    name: str
    path: Path
    item_type: MenuItemType

    @property
    def marker(self) -> str:
        return "<DIR>" if self.item_type == "folder" else "<EMU>"


def scan_menu_folder(folder: Path) -> list[MenuItem]:
    folder.mkdir(parents=True, exist_ok=True)
    items: list[MenuItem] = []
    for child in folder.iterdir():
        if child.name.startswith('.'):
            continue
        if child.is_dir():
            items.append(MenuItem(child.name, child, "folder"))
        elif child.is_file() and child.suffix.lower() == '.json':
            items.append(MenuItem(child.stem, child, "launcher"))
    items.sort(key=lambda item: (0 if item.item_type == "folder" else 1, item.name.lower()))
    return items
