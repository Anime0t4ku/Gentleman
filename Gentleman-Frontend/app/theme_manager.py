from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from PyQt6.QtGui import QColor


DEFAULT_THEME_ID = "default"
DEFAULT_THEME_NAME = "Default"

USER_COLOR_KEYS = {
    "background_color": "#000000",
    "menu_color": "#37000FDC",
    "highlight_color": "#DCB9BE",
    "text_color": "#F5EBEB",
    "highlight_text_color": "#28000A",
}

LEGACY_COLOR_KEYS = {
    "background": "background_color",
    "panel": "menu_color",
    "light": "highlight_color",
    "text": "text_color",
    "dark_text": "highlight_text_color",
}

_HEX_COLOR_RE = re.compile(r"^#(?:[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")


@dataclass(frozen=True)
class ThemeInfo:
    theme_id: str
    name: str
    path: Path | None = None


@dataclass
class GentlemanTheme:
    theme_id: str
    name: str
    colors: dict[str, QColor]

    def color(self, key: str) -> QColor:
        return QColor(self.colors.get(key, self.colors["text_color"]))


class ThemeManager:
    def __init__(self, base_dir: Path):
        self.themes_dir = base_dir / "themes"
        self.themes_dir.mkdir(exist_ok=True)

    def default_theme(self) -> GentlemanTheme:
        return GentlemanTheme(
            theme_id=DEFAULT_THEME_ID,
            name=DEFAULT_THEME_NAME,
            colors=self.build_palette(USER_COLOR_KEYS),
        )

    def available_themes(self) -> list[ThemeInfo]:
        themes = [ThemeInfo(DEFAULT_THEME_ID, DEFAULT_THEME_NAME, None)]
        seen_names = {DEFAULT_THEME_NAME.lower()}

        for path in sorted(self.themes_dir.glob("*.json"), key=lambda p: p.stem.lower()):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue

            if not isinstance(data, dict):
                continue

            colors = data.get("colors")
            if not isinstance(colors, dict):
                continue

            if not self.has_valid_theme_colors(colors):
                continue

            name = str(data.get("name") or path.stem).strip() or path.stem
            if name.lower() in seen_names:
                name = path.stem
            seen_names.add(name.lower())
            themes.append(ThemeInfo(path.stem, name, path))

        return themes

    def load_theme(self, theme_id: str | None) -> GentlemanTheme:
        if not theme_id or theme_id == DEFAULT_THEME_ID:
            return self.default_theme()

        path = self.themes_dir / f"{theme_id}.json"
        if not path.exists():
            return self.default_theme()

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return self.default_theme()

            raw_colors = data.get("colors")
            if not isinstance(raw_colors, dict):
                return self.default_theme()

            if not self.has_valid_theme_colors(raw_colors):
                return self.default_theme()

            user_colors = USER_COLOR_KEYS.copy()
            normalized = self.normalize_colors(raw_colors)
            user_colors.update(normalized)
            name = str(data.get("name") or path.stem).strip() or path.stem

            return GentlemanTheme(
                theme_id=path.stem,
                name=name,
                colors=self.build_palette(user_colors),
            )
        except Exception:
            return self.default_theme()

    def normalize_colors(self, colors: dict) -> dict[str, str]:
        normalized = {}
        for key, value in colors.items():
            target = LEGACY_COLOR_KEYS.get(key, key)
            if target in USER_COLOR_KEYS and isinstance(value, str):
                normalized[target] = value.strip()
        return normalized

    def has_valid_theme_colors(self, colors: dict) -> bool:
        for key, value in self.normalize_colors(colors).items():
            if key not in USER_COLOR_KEYS:
                continue
            if not isinstance(value, str) or not _HEX_COLOR_RE.match(value.strip()):
                return False
        return True

    def theme_label(self, theme_id: str | None) -> str:
        theme = self.load_theme(theme_id)
        return theme.name

    def build_palette(self, user_colors: dict[str, str]) -> dict[str, QColor]:
        background = self.qcolor_from_hex(user_colors["background_color"])
        menu = self.qcolor_from_hex(user_colors["menu_color"])
        highlight = self.qcolor_from_hex(user_colors["highlight_color"])
        text = self.qcolor_from_hex(user_colors["text_color"])
        highlight_text = self.qcolor_from_hex(user_colors["highlight_text_color"])

        palette = {
            "background_color": background,
            "menu_color": menu,
            "highlight_color": highlight,
            "text_color": text,
            "highlight_text_color": highlight_text,
            "background": background,
            "panel": menu,
            "light": highlight,
            "text": text,
            "dark_text": highlight_text,
            "overlay_color": QColor(0, 0, 0, 150),
            "osd_overlay_color": QColor(0, 0, 0, 205),
            "soft_overlay_color": QColor(0, 0, 0, 95),
            "keyboard_overlay_color": QColor(0, 0, 0, 175),
            "dialog_color": self.with_alpha(menu, 252),
            "dialog_alt_color": self.mix(menu, QColor(0, 0, 0), 0.22, 252),
            "field_color": self.mix(menu, QColor(0, 0, 0), 0.45, 245),
            "preview_color": self.with_alpha(menu, 245),
            "wallpaper_tint_color": self.with_alpha(menu, 45),
        }
        return palette

    @staticmethod
    def qcolor_from_hex(value: str) -> QColor:
        text = value.strip()
        if len(text) == 9:
            red = int(text[1:3], 16)
            green = int(text[3:5], 16)
            blue = int(text[5:7], 16)
            alpha = int(text[7:9], 16)
            return QColor(red, green, blue, alpha)
        return QColor(text)

    @staticmethod
    def with_alpha(color: QColor, alpha: int) -> QColor:
        return QColor(color.red(), color.green(), color.blue(), alpha)

    @staticmethod
    def mix(color: QColor, other: QColor, other_amount: float, alpha: int | None = None) -> QColor:
        own_amount = 1.0 - other_amount
        mixed = QColor(
            round(color.red() * own_amount + other.red() * other_amount),
            round(color.green() * own_amount + other.green() * other_amount),
            round(color.blue() * own_amount + other.blue() * other_amount),
            color.alpha() if alpha is None else alpha,
        )
        return mixed
