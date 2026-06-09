from __future__ import annotations

import json
import platform
import socket
import subprocess
import time
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import Qt, QTimer, QRect, QRectF
from PyQt6.QtGui import QFont, QKeyEvent, QPainter, QColor, QPen, QPixmap
from PyQt6.QtSvg import QSvgRenderer
from PyQt6.QtWidgets import (
    QApplication,
    QFileDialog,
    QMainWindow,
    QMessageBox,
    QWidget,
)

from app.app_info import ABOUT_LINES
from app.create_launcher_dialog import CreateLauncherDialog
from core.launcher import load_launcher, scan_rom_folder, launch_rom, LauncherConfig, RomBrowserItem
from core.menu_scanner import MenuItem, scan_menu_folder


class GentlemanWindow(QMainWindow):
    def __init__(self, base_dir: Path):
        super().__init__()

        self.base_dir = base_dir
        self.menu_root = base_dir / "menu"
        self.config_dir = base_dir / "config"
        self.settings_path = self.config_dir / "settings.json"
        self.recent_path = self.config_dir / "recent.json"
        self.favorites_path = self.config_dir / "favorites.json"
        self.menu_root.mkdir(exist_ok=True)
        self.config_dir.mkdir(exist_ok=True)

        self.settings = self.load_settings()

        self.setWindowTitle("Gentleman")
        self.resize(1280, 720)

        self.view = GentlemanView(self)
        self.setCentralWidget(self.view)

        self.path_stack: list[Path] = []
        self.current_folder = self.menu_root
        self.current_edit_folder = self.menu_root
        self.edit_launcher_items: list[MenuItem] = []
        self.mode = "menu"

        self.menu_items: list[MenuItem] = []
        self.rom_items: list[RomBrowserItem] = []
        self.emulator_items: list[str] = []
        self.emulator_launchers: dict[str, list[MenuItem]] = {}
        self.emulator_paths: dict[str, str] = {}
        self.current_emulator: str | None = None
        self.recent_items: list[dict] = []
        self.favorite_items: list[dict] = []
        self.system_items = []
        self.update_system_items()
        self.settings_items = []
        self.update_settings_items()
        self.wallpaper_items = [
            "Set Wallpaper",
            "Clear Wallpaper",
        ]

        self.current_launcher: LauncherConfig | None = None
        self.current_rom_folder: Path | None = None
        self.selected_index = 0
        self.active_input = "keyboard"

        self.refresh_menu()

        self.clock_timer = QTimer(self)
        self.clock_timer.timeout.connect(self.view.update)
        self.clock_timer.start(1000)

        self.marquee_timer = QTimer(self)
        self.marquee_timer.timeout.connect(self.view.update)
        self.marquee_timer.start(120)

        if self.settings.get("fullscreen_at_launch", False):
            QTimer.singleShot(0, self.showFullScreen)

    def load_settings(self) -> dict:
        if not self.settings_path.exists():
            return {"wallpaper": "", "fullscreen_at_launch": False, "show_emulators_menu": True, "show_recent_menu": True, "show_favorites_menu": True}

        try:
            data = json.loads(self.settings_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return {"wallpaper": "", "fullscreen_at_launch": False, "show_emulators_menu": True, "show_recent_menu": True, "show_favorites_menu": True}
            data.setdefault("wallpaper", "")
            data.setdefault("fullscreen_at_launch", False)
            data.setdefault("show_emulators_menu", True)
            data.setdefault("show_recent_menu", True)
            data.setdefault("show_favorites_menu", True)
            return data
        except Exception:
            return {"wallpaper": "", "fullscreen_at_launch": False, "show_emulators_menu": True, "show_recent_menu": True, "show_favorites_menu": True}

    def save_settings(self):
        self.settings_path.write_text(json.dumps(self.settings, indent=2), encoding="utf-8")

    def emulators_menu_enabled(self) -> bool:
        return bool(self.settings.get("show_emulators_menu", True))

    def recent_menu_enabled(self) -> bool:
        return bool(self.settings.get("show_recent_menu", True))

    def favorites_menu_enabled(self) -> bool:
        return bool(self.settings.get("show_favorites_menu", True))

    def load_favorite_items(self) -> list[dict]:
        if not self.favorites_path.exists():
            return []

        try:
            data = json.loads(self.favorites_path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
        except Exception:
            pass

        return []

    def save_favorite_items(self, items: list[dict]):
        self.favorites_path.write_text(json.dumps(items, indent=2), encoding="utf-8")

    def update_favorite_items(self):
        self.favorite_items = self.load_favorite_items()

    def remove_selected_favorite(self):
        if self.mode != "favorites" or self.selected_index == 0:
            return

        favorite_index = self.selected_index - 1
        favorites = self.load_favorite_items()

        if favorite_index < 0 or favorite_index >= len(favorites):
            return

        favorites.pop(favorite_index)
        self.save_favorite_items(favorites)
        self.update_favorite_items()

        max_index = max(0, len(self.favorite_items))
        self.selected_index = min(self.selected_index, max_index)
        self.view.ensure_visible()
        self.view.update()

    def favorite_item_from_current_selection(self) -> dict | None:
        if self.mode != "roms" or not self.current_launcher or self.selected_index == 0:
            return None

        item_index = self.selected_index - 1
        if item_index < 0 or item_index >= len(self.rom_items):
            return None

        selected = self.rom_items[item_index]
        if selected.is_dir:
            return None

        try:
            launcher_rel = str(self.current_launcher.path.relative_to(self.menu_root)).replace(chr(92), "/")
        except ValueError:
            launcher_rel = str(self.current_launcher.path).replace(chr(92), "/")

        return {
            "name": selected.path.name,
            "launcher": launcher_rel,
            "rom": str(selected.path).replace(chr(92), "/"),
        }

    def current_selection_is_favorite(self) -> bool:
        item = self.favorite_item_from_current_selection()
        if not item:
            return False

        favorites = self.load_favorite_items()
        return any(
            favorite.get("launcher") == item.get("launcher")
            and favorite.get("rom") == item.get("rom")
            for favorite in favorites
        )

    def toggle_current_favorite(self):
        item = self.favorite_item_from_current_selection()
        if not item:
            return

        favorites = self.load_favorite_items()
        updated = []
        removed = False

        for favorite in favorites:
            if favorite.get("launcher") == item.get("launcher") and favorite.get("rom") == item.get("rom"):
                removed = True
                continue
            updated.append(favorite)

        if not removed:
            updated.insert(0, item)

        self.save_favorite_items(updated)
        self.update_favorite_items()
        self.view.update()

    def load_recent_items(self) -> list[dict]:
        if not self.recent_path.exists():
            return []

        try:
            data = json.loads(self.recent_path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
        except Exception:
            pass

        return []

    def save_recent_items(self, items: list[dict]):
        self.recent_path.write_text(json.dumps(items[:50], indent=2), encoding="utf-8")

    def add_recent_game(self, launcher_path: Path, rom_path: Path):
        try:
            launcher_rel = str(launcher_path.relative_to(self.menu_root)).replace(chr(92), "/")
        except ValueError:
            launcher_rel = str(launcher_path).replace(chr(92), "/")

        game_name = rom_path.name
        game_path = str(rom_path).replace(chr(92), "/")

        item = {
            "name": game_name,
            "launcher": launcher_rel,
            "rom": game_path,
        }

        items = self.load_recent_items()
        items = [
            existing for existing in items
            if not (
                existing.get("launcher") == item["launcher"]
                and existing.get("rom") == item["rom"]
            )
        ]
        items.insert(0, item)
        self.save_recent_items(items)

    def update_recent_items(self):
        self.recent_items = self.load_recent_items()

    def update_emulator_items(self):
        emulators: dict[str, list[MenuItem]] = {}

        for launcher_path in self.menu_root.rglob("*.json"):
            try:
                data = json.loads(launcher_path.read_text(encoding="utf-8"))
            except Exception:
                continue

            emulator_path = str(data.get("emulator", "")).strip()
            if not emulator_path:
                continue

            emulator_name = str(data.get("emulator_name", "")).strip()
            if not emulator_name:
                emulator_name = Path(emulator_path).stem or emulator_path
            try:
                rel_path = launcher_path.relative_to(self.menu_root)
            except ValueError:
                rel_path = launcher_path

            launcher_item = MenuItem(rel_path.stem, launcher_path, "launcher")
            emulators.setdefault(emulator_name, []).append(launcher_item)

        self.emulator_items = []
        self.emulator_launchers = {}
        self.emulator_paths = {}

        for emulator_name in sorted(emulators.keys(), key=str.lower):
            launchers = sorted(emulators[emulator_name], key=lambda item: item.name.lower())
            self.emulator_items.append(emulator_name)
            self.emulator_launchers[emulator_name] = launchers

            first_launcher = launchers[0] if launchers else None
            if first_launcher:
                try:
                    data = json.loads(first_launcher.path.read_text(encoding="utf-8"))
                    self.emulator_paths[emulator_name] = str(data.get("emulator", "")).strip()
                except Exception:
                    self.emulator_paths[emulator_name] = ""

    def update_system_items(self):
        self.system_items = [
            "Toggle Fullscreen",
            "Create Launcher",
            "Edit Launcher",
            "Refresh Menu",
            "Settings",
            "About Gentleman",
            "Exit Gentleman",
        ]

    def update_settings_items(self):
        fullscreen_launch_label = (
            "Disable Fullscreen at Launch"
            if self.settings.get("fullscreen_at_launch", False)
            else "Enable Fullscreen at Launch"
        )

        emulators_menu_label = (
            "Disable Emulators Menu"
            if self.emulators_menu_enabled()
            else "Enable Emulators Menu"
        )

        recent_menu_label = (
            "Disable Recent Menu"
            if self.recent_menu_enabled()
            else "Enable Recent Menu"
        )

        favorites_menu_label = (
            "Disable Favorites Menu"
            if self.favorites_menu_enabled()
            else "Enable Favorites Menu"
        )

        self.settings_items = [
            fullscreen_launch_label,
            emulators_menu_label,
            recent_menu_label,
            favorites_menu_label,
            "Wallpaper",
        ]

    def title_path(self) -> str:
        if self.mode == "system":
            return "Gentleman Menu"
        if self.mode == "settings":
            return "Settings"
        if self.mode == "wallpaper":
            return "Wallpaper"
        if self.mode == "edit_launchers":
            if self.current_edit_folder == self.menu_root:
                return "Edit Launcher"
            try:
                return f"Edit/{str(self.current_edit_folder.relative_to(self.menu_root)).replace(chr(92), '/')}"
            except Exception:
                return "Edit Launcher"
        if self.mode == "favorites":
            return "Favorites"
        if self.mode == "recent":
            return "Recent"
        if self.mode == "emulators":
            return "Emulators"
        if self.mode == "emulator_launchers":
            return self.current_emulator or "Emulators"
        if self.mode == "about":
            return "About"
        if self.mode == "roms" and self.current_launcher:
            if self.current_rom_folder:
                try:
                    rom_root = Path(self.current_launcher.rom_directory)
                    rel = self.current_rom_folder.relative_to(rom_root)
                    if str(rel) != ".":
                        return f"{self.current_launcher.path.stem}/{str(rel).replace(chr(92), '/')}"
                except Exception:
                    pass
            return self.current_launcher.path.stem
        if self.current_folder == self.menu_root:
            return "Menu"
        return str(self.current_folder.relative_to(self.menu_root)).replace("\\", "/")

    def current_items_count(self) -> int:
        if self.mode == "system":
            self.update_system_items()
            return len(self.system_items)
        if self.mode == "settings":
            self.update_settings_items()
            return len(self.settings_items)
        if self.mode == "wallpaper":
            return len(self.wallpaper_items)
        if self.mode == "edit_launchers":
            return len(self.edit_launcher_items) + 1
        if self.mode == "about":
            return len(ABOUT_LINES)
        if self.mode == "favorites":
            return len(self.favorite_items) + 1
        if self.mode == "recent":
            return len(self.recent_items) + 1
        if self.mode == "emulators":
            return len(self.emulator_items) + 1
        if self.mode == "emulator_launchers":
            if not self.current_emulator:
                return 1
            return len(self.emulator_launchers.get(self.current_emulator, [])) + 1
        if self.mode == "roms":
            return len(self.rom_items) + 1

        back_count = 1 if self.current_folder != self.menu_root else 0
        favorites_count = 1 if self.current_folder == self.menu_root and self.favorites_menu_enabled() else 0
        recent_count = 1 if self.current_folder == self.menu_root and self.recent_menu_enabled() else 0
        emulator_count = 1 if self.current_folder == self.menu_root and self.emulators_menu_enabled() else 0
        return len(self.menu_items) + back_count + favorites_count + recent_count + emulator_count

    def current_labels(self) -> list[tuple[str, str]]:
        if self.mode == "system":
            self.update_system_items()
            return [(name, "") for name in self.system_items]
        if self.mode == "settings":
            self.update_settings_items()
            return [(name, "") for name in self.settings_items]
        if self.mode == "wallpaper":
            return [(name, "") for name in self.wallpaper_items]
        if self.mode == "edit_launchers":
            return [("...", "<DIR>")] + [(item.name, "<DIR>") for item in self.edit_launcher_items]
        if self.mode == "about":
            return [(line, "") for line in ABOUT_LINES]
        if self.mode == "favorites":
            return [("...", "<DIR>")] + [(item.get("name", "Unknown"), "") for item in self.favorite_items]
        if self.mode == "recent":
            return [("...", "<DIR>")] + [(item.get("name", "Unknown"), "") for item in self.recent_items]
        if self.mode == "emulators":
            return [("...", "<DIR>")] + [(name, "") for name in self.emulator_items]
        if self.mode == "emulator_launchers":
            launchers = self.emulator_launchers.get(self.current_emulator or "", [])
            return [("...", "<DIR>")] + [(item.name, "<DIR>") for item in launchers]
        if self.mode == "roms":
            return [("...", "<DIR>")] + [(item.display_name, item.marker) for item in self.rom_items]

        labels = []
        if self.current_folder == self.menu_root and self.favorites_menu_enabled():
            labels.append(("Favorites", "<DIR>"))
        if self.current_folder == self.menu_root and self.recent_menu_enabled():
            labels.append(("Recent", "<DIR>"))
        if self.current_folder == self.menu_root and self.emulators_menu_enabled():
            labels.append(("Emulators", "<DIR>"))

        for item in self.menu_items:
            labels.append((item.name, "<DIR>"))

        if self.current_folder != self.menu_root:
            labels.insert(0, ("...", "<DIR>"))
        return labels

    def refresh_menu(self):
        self.mode = "menu"
        self.current_launcher = None
        self.current_rom_folder = None
        self.current_emulator = None
        self.rom_items = []
        self.menu_items = scan_menu_folder(self.current_folder)
        self.update_favorite_items()
        self.update_recent_items()
        self.update_emulator_items()
        self.selected_index = min(self.selected_index, max(0, self.current_items_count() - 1))
        self.view.update()

    def open_edit_launcher_browser(self, folder: Path | None = None):
        self.current_edit_folder = folder or self.menu_root
        self.edit_launcher_items = scan_menu_folder(self.current_edit_folder)
        self.mode = "edit_launchers"
        self.selected_index = 0
        self.view.scroll_offset = 0
        self.view.update()

    def open_system_menu(self):
        self.update_system_items()
        self.mode = "system"
        self.selected_index = 0
        self.view.update()

    def go_back(self):
        if self.mode == "system":
            self.mode = "menu"
            self.selected_index = 0
            self.view.update()
            return

        if self.mode == "settings":
            self.mode = "system"
            self.selected_index = 0
            self.view.update()
            return

        if self.mode == "wallpaper":
            self.mode = "settings"
            self.selected_index = 0
            self.view.update()
            return

        if self.mode == "edit_launchers":
            if self.current_edit_folder != self.menu_root:
                self.open_edit_launcher_browser(self.current_edit_folder.parent)
                return

            self.mode = "system"
            self.selected_index = 0
            self.view.update()
            return

        if self.mode == "about":
            self.mode = "system"
            self.selected_index = 0
            self.view.update()
            return

        if self.mode == "favorites":
            self.mode = "menu"
            self.selected_index = 0
            self.view.update()
            return

        if self.mode == "recent":
            self.mode = "menu"
            self.selected_index = 0
            self.view.update()
            return

        if self.mode == "emulators":
            self.mode = "menu"
            self.selected_index = 0
            self.view.update()
            return

        if self.mode == "emulator_launchers":
            self.mode = "emulators"
            self.current_emulator = None
            self.selected_index = 0
            self.view.update()
            return

        if self.mode == "roms":
            if self.current_launcher and self.current_rom_folder:
                rom_root = Path(self.current_launcher.rom_directory).resolve()
                current = self.current_rom_folder.resolve()

                if current != rom_root and rom_root in current.parents:
                    self.current_rom_folder = self.current_rom_folder.parent
                    self.rom_items = scan_rom_folder(self.current_launcher, self.current_rom_folder)
                    self.selected_index = 0
                    self.view.scroll_offset = 0
                    self.view.update()
                    return

            self.mode = "menu"
            self.current_launcher = None
            self.current_rom_folder = None
            self.rom_items = []
            self.selected_index = 0
            self.view.update()
            return

        if self.current_folder == self.menu_root:
            self.open_system_menu()
            return

        self.current_folder = self.current_folder.parent
        self.selected_index = 0
        self.view.scroll_offset = 0
        self.refresh_menu()

    def activate_selected(self):
        if self.current_items_count() == 0:
            return

        if self.mode == "system":
            self.activate_system_item(self.system_items[self.selected_index])
            return

        if self.mode == "settings":
            self.activate_settings_item(self.settings_items[self.selected_index])
            return

        if self.mode == "wallpaper":
            self.activate_wallpaper_item(self.wallpaper_items[self.selected_index])
            return

        if self.mode == "edit_launchers":
            if self.selected_index == 0:
                self.go_back()
                return

            item_index = self.selected_index - 1
            if item_index < 0 or item_index >= len(self.edit_launcher_items):
                return

            item = self.edit_launcher_items[item_index]
            if item.item_type == "folder":
                self.open_edit_launcher_browser(item.path)
                return

            dialog = CreateLauncherDialog(self.base_dir, self.menu_root, self, launcher_path=item.path)
            if dialog.exec():
                self.open_edit_launcher_browser(self.current_edit_folder)
            return

        if self.mode == "favorites":
            if self.selected_index == 0:
                self.go_back()
                return

            favorite_index = self.selected_index - 1
            if favorite_index >= len(self.favorite_items):
                return

            self.launch_recent_item(self.favorite_items[favorite_index])
            return

        if self.mode == "recent":
            if self.selected_index == 0:
                self.go_back()
                return

            recent_index = self.selected_index - 1
            if recent_index >= len(self.recent_items):
                return

            self.launch_recent_item(self.recent_items[recent_index])
            return

        if self.mode == "emulators":
            if self.selected_index == 0:
                self.go_back()
                return

            emulator_name = self.emulator_items[self.selected_index - 1]
            emulator_path = self.emulator_paths.get(emulator_name, "")

            if not emulator_path:
                QMessageBox.warning(self, "Launch failed", "No emulator path found.")
                return

            try:
                subprocess.Popen(f'"{emulator_path}"', cwd=str(Path(emulator_path).parent), shell=True)
            except Exception as exc:
                QMessageBox.critical(self, "Launch failed", str(exc))
            return

        if self.mode == "emulator_launchers":
            if self.selected_index == 0:
                self.go_back()
                return

            launchers = self.emulator_launchers.get(self.current_emulator or "", [])
            if self.selected_index - 1 >= len(launchers):
                return

            item = launchers[self.selected_index - 1]
            self.open_launcher_item(item)
            return

        if self.mode == "roms":
            if self.selected_index == 0:
                self.go_back()
                return

            if not self.current_launcher:
                return

            selected = self.rom_items[self.selected_index - 1]

            if selected.is_dir:
                self.current_rom_folder = selected.path
                self.rom_items = scan_rom_folder(self.current_launcher, self.current_rom_folder)
                self.selected_index = 0
                self.view.scroll_offset = 0
                self.view.update()
                return

            try:
                launch_rom(self.current_launcher, selected.path)
                self.add_recent_game(self.current_launcher.path, selected.path)
                self.update_recent_items()
            except Exception as exc:
                QMessageBox.critical(self, "Launch failed", str(exc))
            return

        menu_index = self.selected_index

        if self.current_folder == self.menu_root:
            if self.favorites_menu_enabled():
                if menu_index == 0:
                    self.update_favorite_items()
                    self.mode = "favorites"
                    self.selected_index = 0
                    self.view.scroll_offset = 0
                    self.view.update()
                    return
                menu_index -= 1

            if self.recent_menu_enabled():
                if menu_index == 0:
                    self.update_recent_items()
                    self.mode = "recent"
                    self.selected_index = 0
                    self.view.scroll_offset = 0
                    self.view.update()
                    return
                menu_index -= 1

            if self.emulators_menu_enabled():
                if menu_index == 0:
                    self.update_emulator_items()
                    self.mode = "emulators"
                    self.selected_index = 0
                    self.view.scroll_offset = 0
                    self.view.update()
                    return
                menu_index -= 1

        if self.current_folder != self.menu_root:
            if self.selected_index == 0:
                self.go_back()
                return
            menu_index -= 1

        item = self.menu_items[menu_index]
        if item.item_type == "folder":
            self.current_folder = item.path
            self.refresh_menu()
            return

        self.open_launcher_item(item)

    def launch_recent_item(self, item: dict):
        launcher_rel = str(item.get("launcher", ""))
        rom = Path(str(item.get("rom", "")))

        launcher_path = self.menu_root / launcher_rel
        if not launcher_path.exists() or not rom.exists():
            QMessageBox.warning(self, "Recent item unavailable", "The launcher or game file no longer exists.")
            return

        try:
            launcher = load_launcher(launcher_path)
            launch_rom(launcher, rom)
            self.add_recent_game(launcher_path, rom)
            self.update_recent_items()
            self.view.update()
        except Exception as exc:
            QMessageBox.critical(self, "Launch failed", str(exc))

    def open_launcher_item(self, item: MenuItem):
        try:
            self.current_launcher = load_launcher(item.path)
            self.current_rom_folder = Path(self.current_launcher.rom_directory)
            self.rom_items = scan_rom_folder(self.current_launcher, self.current_rom_folder)
            self.mode = "roms"
            self.selected_index = 0
            self.view.scroll_offset = 0
            self.view.update()
        except Exception as exc:
            QMessageBox.critical(self, "Launcher error", str(exc))

    def edit_launcher(self):
        self.open_edit_launcher_browser(self.menu_root)

    def activate_system_item(self, item: str):
        if item == "Toggle Fullscreen":
            self.toggle_fullscreen()
            self.update_system_items()
            self.view.update()
        elif item == "Create Launcher":
            dialog = CreateLauncherDialog(self.base_dir, self.menu_root, self)
            if dialog.exec():
                self.current_folder = self.menu_root
                self.refresh_menu()
        elif item == "Edit Launcher":
            self.edit_launcher()
        elif item == "Refresh Menu":
            self.mode = "menu"
            self.refresh_menu()
        elif item == "Settings":
            self.update_settings_items()
            self.mode = "settings"
            self.selected_index = 0
            self.view.update()
        elif item == "About Gentleman":
            self.mode = "about"
            self.selected_index = 0
            self.view.update()
        elif item == "Exit Gentleman":
            QApplication.quit()

    def activate_settings_item(self, item: str):
        if item == "Enable Fullscreen at Launch":
            self.settings["fullscreen_at_launch"] = True
            self.save_settings()
            self.update_settings_items()
            self.view.update()
        elif item == "Disable Fullscreen at Launch":
            self.settings["fullscreen_at_launch"] = False
            self.save_settings()
            self.update_settings_items()
            self.view.update()
        elif item == "Enable Emulators Menu":
            self.settings["show_emulators_menu"] = True
            self.save_settings()
            self.update_settings_items()
            self.view.update()
        elif item == "Disable Emulators Menu":
            self.settings["show_emulators_menu"] = False
            self.save_settings()
            self.update_settings_items()
            self.view.update()
        elif item == "Enable Recent Menu":
            self.settings["show_recent_menu"] = True
            self.save_settings()
            self.update_settings_items()
            self.view.update()
        elif item == "Disable Recent Menu":
            self.settings["show_recent_menu"] = False
            self.save_settings()
            self.update_settings_items()
            self.view.update()
        elif item == "Enable Favorites Menu":
            self.settings["show_favorites_menu"] = True
            self.save_settings()
            self.update_settings_items()
            self.view.update()
        elif item == "Disable Favorites Menu":
            self.settings["show_favorites_menu"] = False
            self.save_settings()
            self.update_settings_items()
            self.view.update()
        elif item == "Wallpaper":
            self.mode = "wallpaper"
            self.selected_index = 0
            self.view.update()

    def activate_wallpaper_item(self, item: str):
        if item == "Set Wallpaper":
            self.set_wallpaper()
        elif item == "Clear Wallpaper":
            self.settings["wallpaper"] = ""
            self.save_settings()
            self.view.reload_wallpaper()
            self.view.update()

    def set_wallpaper(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Select wallpaper",
            str(self.base_dir),
            "Images (*.png *.jpg *.jpeg *.bmp *.webp);;All files (*.*)",
        )
        if not path:
            return

        self.settings["wallpaper"] = path.replace("\\", "/")
        self.save_settings()
        self.view.reload_wallpaper()
        self.view.update()

    def open_folder(self, folder: Path):
        try:
            if platform.system() == "Windows":
                subprocess.Popen(["explorer", str(folder)])
            elif platform.system() == "Darwin":
                subprocess.Popen(["open", str(folder)])
            else:
                subprocess.Popen(["xdg-open", str(folder)])
        except Exception as exc:
            QMessageBox.warning(self, "Open folder failed", str(exc))

    def move_selection(self, delta: int):
        count = self.current_items_count()
        if count <= 0:
            return

        if self.mode == "about":
            max_offset = max(0, count - self.view.visible_rows())
            self.view.scroll_offset = max(0, min(max_offset, self.view.scroll_offset + delta))
            self.selected_index = self.view.scroll_offset
            self.view.update()
            return

        self.selected_index = (self.selected_index + delta) % count
        self.view.ensure_visible()
        self.view.update()

    def toggle_fullscreen(self):
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()
        QTimer.singleShot(50, self.view.update)

    def keyPressEvent(self, event: QKeyEvent):
        self.active_input = "keyboard"

        key = event.key()

        if key in (Qt.Key.Key_Up, Qt.Key.Key_W):
            self.move_selection(-1)
        elif key in (Qt.Key.Key_Down, Qt.Key.Key_S):
            self.move_selection(1)
        elif key in (Qt.Key.Key_Left, Qt.Key.Key_A):
            self.move_selection(-10)
        elif key in (Qt.Key.Key_Right, Qt.Key.Key_D):
            self.move_selection(10)
        elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.activate_selected()
        elif key in (Qt.Key.Key_Escape, Qt.Key.Key_Backspace):
            self.go_back()
        elif key == Qt.Key.Key_F5:
            self.refresh_menu()
        elif key == Qt.Key.Key_F:
            if self.mode == "favorites":
                self.remove_selected_favorite()
            else:
                self.toggle_current_favorite()
        elif key == Qt.Key.Key_F11:
            self.toggle_fullscreen()

        self.view.update()


class GentlemanView(QWidget):
    def __init__(self, window: GentlemanWindow):
        super().__init__()
        self.window = window
        self.scroll_offset = 0
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        self.bg = QColor(0, 0, 0)
        self.panel = QColor(55, 0, 15, 220)
        self.light = QColor(220, 185, 190)
        self.text = QColor(245, 235, 235)
        self.dark_text = QColor(40, 0, 10)

        self.font = QFont("Consolas", 20)
        self.font.setStyleHint(QFont.StyleHint.Monospace)

        self.title_font = QFont("Consolas", 22, QFont.Weight.Bold)
        self.title_font.setStyleHint(QFont.StyleHint.Monospace)

        self.wallpaper = QPixmap()
        self.reload_wallpaper()

        self.icon_dir = self.window.base_dir / "assets" / "icons"
        self.icon_renderers = {
            "lan": QSvgRenderer(str(self.icon_dir / "lan.svg")),
            "wifi": QSvgRenderer(str(self.icon_dir / "wifi.svg")),
            "bluetooth": QSvgRenderer(str(self.icon_dir / "bluetooth.svg")),
            "keyboard": QSvgRenderer(str(self.icon_dir / "keyboard.svg")),
            "controller": QSvgRenderer(str(self.icon_dir / "controller.svg")),
            "favorite": QSvgRenderer(str(self.icon_dir / "favorite.svg")),
        }

    def reload_wallpaper(self):
        wallpaper_path = self.window.settings.get("wallpaper", "")
        if wallpaper_path and Path(wallpaper_path).exists():
            self.wallpaper = QPixmap(wallpaper_path)
        else:
            self.wallpaper = QPixmap()

    def ensure_visible(self):
        visible_rows = self.visible_rows()
        idx = self.window.selected_index

        if idx < self.scroll_offset:
            self.scroll_offset = idx
        elif idx >= self.scroll_offset + visible_rows:
            self.scroll_offset = idx - visible_rows + 1

        self.scroll_offset = max(0, self.scroll_offset)

    def menu_panel_size(self) -> tuple[int, int]:
        panel_w = min(620, self.width() - 160)
        panel_h = min(430, self.height() - 200)
        return panel_w, panel_h

    def menu_panel_rect(self) -> QRect:
        panel_w, panel_h = self.menu_panel_size()
        x = (self.width() - panel_w) // 2
        y = (self.height() - panel_h) // 2
        return QRect(x, y, panel_w, panel_h)

    def top_bar_rect(self) -> QRect:
        panel_rect = self.menu_panel_rect()
        bar_h = 44
        gap = 34
        y = max(16, panel_rect.y() - bar_h - gap)
        return QRect(panel_rect.x(), y, panel_rect.width(), bar_h)

    def visible_rows(self) -> int:
        _, panel_h = self.menu_panel_size()
        return max(1, (panel_h - 30) // 28)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)

        if not self.wallpaper.isNull():
            scaled = self.wallpaper.scaled(
                self.size(),
                Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation,
            )
            x = (self.width() - scaled.width()) // 2
            y = (self.height() - scaled.height()) // 2
            painter.drawPixmap(x, y, scaled)
            painter.fillRect(self.rect(), QColor(0, 0, 0, 95))
        else:
            painter.fillRect(self.rect(), self.bg)
            self.draw_starfield(painter)

        self.draw_top_bar(painter)
        self.draw_panel(painter)

    def draw_starfield(self, painter: QPainter):
        painter.setPen(QPen(QColor(255, 255, 255, 180), 1))
        w = self.width()
        h = self.height()
        for i in range(180):
            x = (i * 97) % max(1, w)
            y = (i * 53) % max(1, h)
            painter.drawPoint(x, y)

    def network_icon(self) -> str:
        try:
            socket.create_connection(("8.8.8.8", 53), timeout=0.2).close()
            return "lan"
        except Exception:
            return "wifi"

    def bluetooth_icon(self) -> str:
        return "bluetooth"

    def input_icon(self) -> str:
        return "keyboard" if self.window.active_input == "keyboard" else "controller"

    def draw_svg_icon(self, painter: QPainter, icon_name: str, rect: QRect, color: QColor):
        renderer = self.icon_renderers.get(icon_name)
        if not renderer or not renderer.isValid():
            return

        painter.save()
        painter.setPen(color)
        painter.setBrush(color)
        renderer.render(painter, QRectF(rect))
        painter.restore()

    def draw_top_bar(self, painter: QPainter):
        bar_rect = self.top_bar_rect()
        bar_w = bar_rect.width()
        bar_h = bar_rect.height()
        x = bar_rect.x()
        y = bar_rect.y()

        painter.fillRect(bar_rect, self.light)

        painter.setFont(self.title_font)
        painter.setPen(self.dark_text)

        painter.drawText(x + 18, y + 31, "Gentleman")

        icon_size = 24
        icon_y = y + (bar_h - icon_size) // 2
        icon_x = x + 172
        icon_gap = 34

        self.draw_svg_icon(painter, self.network_icon(), QRect(icon_x, icon_y, icon_size, icon_size), self.dark_text)
        icon_x += icon_gap
        self.draw_svg_icon(painter, self.bluetooth_icon(), QRect(icon_x, icon_y, icon_size, icon_size), self.dark_text)
        icon_x += icon_gap
        self.draw_svg_icon(painter, self.input_icon(), QRect(icon_x, icon_y, icon_size, icon_size), self.dark_text)

        time_text = datetime.now().strftime("%H:%M")
        painter.drawText(x + bar_w - 82, y + 31, time_text)

    def draw_panel(self, painter: QPainter):
        panel_rect = self.menu_panel_rect()
        panel_w = panel_rect.width()
        panel_h = panel_rect.height()
        x = panel_rect.x()
        y = panel_rect.y()

        side_w = 48
        title_h = 38

        painter.fillRect(QRect(x, y, panel_w, panel_h), self.panel)
        painter.fillRect(QRect(x, y, side_w, panel_h), self.light)

        painter.setFont(self.font)
        painter.setPen(self.dark_text)
        painter.save()
        painter.setClipRect(QRect(x, y, side_w, panel_h))

        title = self.window.title_path()
        max_title_width = panel_h - 56
        metrics = painter.fontMetrics()

        if metrics.horizontalAdvance(title) > max_title_width:
            ellipsis = "..."
            while title and metrics.horizontalAdvance(ellipsis + title) > max_title_width:
                title = title[1:]
            title = ellipsis + title

        title_width = metrics.horizontalAdvance(title)
        title_start = (panel_h - title_width) // 2

        painter.translate(x + 31, y + panel_h - title_start)
        painter.rotate(-90)
        painter.drawText(0, 0, title)
        painter.restore()

        labels = self.window.current_labels()
        rows = self.visible_rows()
        self.ensure_visible()

        start = self.scroll_offset
        end = min(len(labels), start + rows)

        text_x = x + side_w + 22
        marker_x = x + panel_w - 118
        row_y = y + 28

        painter.setFont(self.font)

        if not labels:
            painter.setPen(self.text)
            painter.drawText(text_x, row_y, "No entries")
            return

        for row, idx in enumerate(range(start, end)):
            label, marker = labels[idx]
            yy = row_y + row * 28

            row_left_x = text_x - 6

            marker_w = painter.fontMetrics().horizontalAdvance("<DIR>")
            marker_end_x = x + panel_w - 72
            marker_x = marker_end_x - marker_w
            row_right_x = marker_end_x + 8

            highlight_w = max(0, row_right_x - row_left_x)

            if marker:
                text_area_w = max(0, marker_x - text_x - 16)
            else:
                text_area_w = max(0, row_right_x - text_x - 8)

            if self.window.mode != "about" and idx == self.window.selected_index:
                painter.fillRect(QRect(row_left_x, yy - 23, highlight_w, 27), self.light)
                painter.setPen(self.dark_text)
            else:
                painter.setPen(self.text)

            if self.window.mode == "about":
                painter.drawText(text_x, yy, label)
            else:
                draw_text_x = text_x
                draw_text_area_w = text_area_w

                show_favorite_icon = (
                    self.window.mode == "roms"
                    and idx == self.window.selected_index
                    and self.window.current_selection_is_favorite()
                )

                if show_favorite_icon:
                    icon_size = 18
                    icon_y = yy - 20
                    self.draw_svg_icon(
                        painter,
                        "favorite",
                        QRect(text_x, icon_y, icon_size, icon_size),
                        painter.pen().color(),
                    )
                    draw_text_x = text_x + 24
                    draw_text_area_w = max(0, text_area_w - 24)

                label_width = painter.fontMetrics().horizontalAdvance(label)

                clip_rect = QRect(draw_text_x, yy - 23, draw_text_area_w, 27)
                painter.save()
                painter.setClipRect(clip_rect)

                if idx == self.window.selected_index and label_width > draw_text_area_w:
                    gap = 48
                    cycle = label_width + gap
                    offset = int((time.monotonic() * 55) % cycle)

                    painter.drawText(draw_text_x - offset, yy, label)
                    painter.drawText(draw_text_x - offset + cycle, yy, label)
                else:
                    painter.drawText(draw_text_x, yy, label)

                painter.restore()

                if marker:
                    painter.drawText(marker_x, yy, marker)

        if start > 0:
            painter.setPen(self.text)
            painter.drawText(x + panel_w - 38, y + title_h + 20, "^")
        if end < len(labels):
            painter.setPen(self.text)
            painter.drawText(x + panel_w - 38, y + panel_h - 8, "v")
