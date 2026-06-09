from __future__ import annotations

import json
import os
import platform
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote

from PyQt6.QtCore import Qt, QTimer, QRect, QRectF, QEvent
from PyQt6.QtGui import QFont, QKeyEvent, QPainter, QColor, QPen, QPixmap, QIcon, QImage
from PyQt6.QtSvg import QSvgRenderer
from PyQt6.QtWidgets import (
    QApplication,
    QFileDialog,
    QMainWindow,
    QMessageBox,
    QInputDialog,
    QWidget,
)

from app.app_info import ABOUT_LINES
from app.zaparoo_systems import ZAPAROO_SYSTEM_NAMES
from core.launcher import load_launcher, scan_rom_folder, launch_rom, launch_external_process, LauncherConfig, RomBrowserItem
from core.menu_scanner import MenuItem, scan_menu_folder
from core.remote_api import GentlemanApiServer

try:
    import pygame
except Exception:
    pygame = None


def resource_path(relative_path: str) -> Path:
    base_path = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
    return base_path / relative_path


class GentlemanWindow(QMainWindow):
    def __init__(self, base_dir: Path):
        super().__init__()

        self.base_dir = base_dir
        self.assets_dir = resource_path("assets")
        self.menu_root = base_dir / "menu"
        self.config_dir = base_dir / "config"
        self.settings_path = self.config_dir / "settings.json"
        self.recent_path = self.config_dir / "recent.json"
        self.favorites_path = self.config_dir / "favorites.json"
        self.menu_root.mkdir(exist_ok=True)
        self.config_dir.mkdir(exist_ok=True)

        self.settings = self.load_settings()
        self.remote_api_server: GentlemanApiServer | None = None

        self.setWindowTitle("Gentleman")

        app_icon = self.assets_dir / "icon.png"
        if app_icon.exists():
            self.setWindowIcon(QIcon(str(app_icon)))

        self.resize(1280, 720)
        self.setMinimumSize(960, 640)

        self.view = GentlemanView(self)
        self.setCentralWidget(self.view)

        self.path_stack: list[Path] = []
        self.current_folder = self.menu_root
        self.current_edit_folder = self.menu_root
        self.edit_launcher_items: list[MenuItem] = []
        self.launcher_form_mode = "create"
        self.launcher_form_path: Path | None = None
        self.launcher_form_data: dict = {}
        self.launcher_form_fields: list[str] = []
        self.launcher_form_folders: list[str] = []
        self.system_picker_items: list[str] = []
        self.type_picker_items: list[str] = []
        self.folder_picker_items: list[str] = []
        self.launcher_form_return_index = 0
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
        self.input_suspended_for_launch = False

        self.controller_available = False
        self.controller = None
        self.controller_axis_state = {
            "up": False,
            "down": False,
            "left": False,
            "right": False,
        }
        self.controller_button_state = {}
        self.controller_repeat_action = None
        self.controller_repeat_next_ms = 0
        self.controller_last_scan_ms = 0

        self.refresh_menu()

        self.clock_timer = QTimer(self)
        self.clock_timer.timeout.connect(self.view.update)
        self.clock_timer.start(1000)

        self.marquee_timer = QTimer(self)
        self.marquee_timer.timeout.connect(self.view.update)
        self.marquee_timer.start(45)

        self.init_controller_support()
        self.controller_timer = QTimer(self)
        self.controller_timer.timeout.connect(self.poll_controller)
        self.controller_timer.start(33)

        self.apply_remote_api_state()

        if self.settings.get("fullscreen_at_launch", False):
            QTimer.singleShot(0, self.showFullScreen)

    def load_settings(self) -> dict:
        if not self.settings_path.exists():
            return {
                "wallpaper": "",
                "fullscreen_at_launch": False,
                "show_emulators_menu": True,
                "show_recent_menu": True,
                "show_favorites_menu": True,
                "show_logo": True,
                "swap_controller_ab": False,
                "swap_controller_xy": False,
                "api_enabled": False,
                "remote_api_port": 8755,
            }

        try:
            data = json.loads(self.settings_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return {
                    "wallpaper": "",
                    "fullscreen_at_launch": False,
                    "show_emulators_menu": True,
                    "show_recent_menu": True,
                    "show_favorites_menu": True,
                    "show_logo": True,
                    "swap_controller_ab": False,
                    "swap_controller_xy": False,
                    "api_enabled": False,
                    "remote_api_port": 8755,
                }
            data.setdefault("wallpaper", "")
            data.setdefault("fullscreen_at_launch", False)
            data.setdefault("show_emulators_menu", True)
            data.setdefault("show_recent_menu", True)
            data.setdefault("show_favorites_menu", True)
            data.setdefault("show_logo", True)
            data.setdefault("swap_controller_ab", False)
            data.setdefault("swap_controller_xy", False)
            data.setdefault("api_enabled", False)
            data.setdefault("remote_api_port", 8755)
            return data
        except Exception:
            return {
                "wallpaper": "",
                "fullscreen_at_launch": False,
                "show_emulators_menu": True,
                "show_recent_menu": True,
                "show_favorites_menu": True,
                "swap_controller_ab": False,
                "swap_controller_xy": False,
                "remote_api_port": 8755,
            }

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

    def remote_api_enabled(self) -> bool:
        return bool(self.settings.get("api_enabled", False))

    def remote_api_host(self) -> str:
        return "0.0.0.0"

    def apply_remote_api_state(self):
        enabled = self.remote_api_enabled()
        host = self.remote_api_host()
        port = int(self.settings.get("remote_api_port", 8755))

        if not enabled:
            if self.remote_api_server:
                self.remote_api_server.stop()
                self.remote_api_server = None
            return

        if self.remote_api_server and self.remote_api_server.is_running():
            if self.remote_api_server.host == host and self.remote_api_server.port == port:
                return

            self.remote_api_server.stop()
            self.remote_api_server = None

        try:
            self.remote_api_server = GentlemanApiServer(self, host=host, port=port)
            self.remote_api_server.start()
        except Exception as exc:
            self.remote_api_server = None
            QMessageBox.warning(self, "Remote API", f"Could not start Remote API:\\n{exc}")

    def api_status(self) -> dict:
        return {
            "app": "Gentleman",
            "api": "Gentleman Remote API",
            "version": "0.1.0",
            "running": True,
            "remote_api_enabled": self.remote_api_enabled(),
            "host": self.remote_api_host(),
            "port": int(self.settings.get("remote_api_port", 8755)),
        }

    def api_safe_menu_folder(self, folder: str) -> Path:
        folder = unquote(str(folder or "")).strip().replace("\\\\", "/").strip("/")
        target = (self.menu_root / folder).resolve()
        root = self.menu_root.resolve()

        if target != root and root not in target.parents:
            raise ValueError("Menu path is outside menu root")

        return target

    def api_safe_launcher_path(self, launcher: str) -> Path:
        launcher = unquote(str(launcher or "")).strip().replace("\\\\", "/").strip("/")
        if not launcher:
            raise ValueError("Missing launcher")

        target = (self.menu_root / launcher).resolve()
        root = self.menu_root.resolve()

        if target != root and root not in target.parents:
            raise ValueError("Launcher path is outside menu root")
        if not target.exists() or target.suffix.lower() != ".json":
            raise ValueError("Launcher not found")

        return target

    def api_safe_rom_folder(self, launcher_config: LauncherConfig, folder: str) -> Path:
        folder = unquote(str(folder or "")).strip().replace("\\\\", "/").strip("/")
        root = Path(launcher_config.rom_directory).resolve()
        target = (root / folder).resolve()

        if target != root and root not in target.parents:
            raise ValueError("ROM folder is outside launcher ROM directory")

        return target

    def api_safe_rom_path(self, launcher_config: LauncherConfig, game: str) -> Path:
        game = unquote(str(game or "")).strip().replace("\\\\", "/").strip("/")
        root = Path(launcher_config.rom_directory).resolve()
        target = (root / game).resolve()

        if target != root and root not in target.parents:
            raise ValueError("Game path is outside launcher ROM directory")
        if not target.exists() or not target.is_file():
            raise ValueError("Game file not found")

        return target

    def api_launcher_metadata(self, launcher_path: Path) -> dict:
        data = {}
        try:
            data = json.loads(launcher_path.read_text(encoding="utf-8"))
        except Exception:
            pass

        try:
            rel = str(launcher_path.relative_to(self.menu_root)).replace(chr(92), "/")
        except ValueError:
            rel = str(launcher_path).replace(chr(92), "/")

        return {
            "name": launcher_path.stem,
            "type": "launcher",
            "path": rel,
            "system": data.get("system", ""),
            "emulator_name": data.get("emulator_name", ""),
            "launcher_type": data.get("type", "standalone"),
        }

    def api_all_launchers(self) -> list[dict]:
        launchers = []

        for launcher_path in self.menu_root.rglob("*.json"):
            try:
                metadata = self.api_launcher_metadata(launcher_path)
            except Exception:
                continue

            launchers.append(metadata)

        launchers.sort(key=lambda item: (
            str(item.get("system", "")).lower(),
            str(item.get("name", "")).lower(),
        ))
        return launchers

    def api_systems(self) -> dict:
        systems: dict[str, dict] = {}

        for launcher in self.api_all_launchers():
            system = str(launcher.get("system", "")).strip()
            if not system:
                continue

            if system not in systems:
                systems[system] = {
                    "system": system,
                    "launchers": [],
                }

            systems[system]["launchers"].append({
                "name": launcher.get("name", ""),
                "path": launcher.get("path", ""),
                "emulator_name": launcher.get("emulator_name", ""),
                "launcher_type": launcher.get("launcher_type", ""),
            })

        return {
            "systems": [systems[key] for key in sorted(systems.keys(), key=str.lower)]
        }

    def api_find_launchers_by_system(self, system: str) -> list[dict]:
        requested = str(system or "").strip().lower()
        if not requested:
            raise ValueError("Missing system")

        matches = []
        for launcher in self.api_all_launchers():
            if str(launcher.get("system", "")).strip().lower() == requested:
                matches.append(launcher)

        if not matches:
            raise ValueError("No launchers found for system")

        return matches

    def api_games_by_system(self, system: str, launcher: str = "", folder: str = "") -> dict:
        launchers = self.api_find_launchers_by_system(system)

        if not launcher and len(launchers) > 1:
            return {
                "system": system,
                "needs_launcher": True,
                "launchers": launchers,
                "items": [],
            }

        selected_launcher = launcher or str(launchers[0].get("path", ""))
        result = self.api_games(selected_launcher, folder)
        result["system"] = system
        result["selected_launcher"] = selected_launcher

        if len(launchers) > 1:
            result["launchers"] = launchers

        return result

    def api_launch_by_system(self, payload: dict) -> dict:
        system = str(payload.get("system", "")).strip()
        launcher = str(payload.get("launcher", "")).strip()
        game = str(payload.get("game", payload.get("rom", ""))).strip()

        launchers = self.api_find_launchers_by_system(system)

        if not launcher:
            if len(launchers) > 1:
                raise ValueError("Multiple launchers found for system, pass launcher from /api/systems or /api/games-by-system")
            launcher = str(launchers[0].get("path", ""))

        return self.api_launch({
            "launcher": launcher,
            "game": game,
        })

    def api_menu(self, folder: str = "") -> dict:
        target = self.api_safe_menu_folder(folder)
        items = []

        if target != self.menu_root.resolve():
            items.append({"name": "...", "type": "back"})

        for item in scan_menu_folder(target):
            if item.item_type == "folder":
                try:
                    rel = str(item.path.relative_to(self.menu_root)).replace(chr(92), "/")
                except ValueError:
                    rel = item.name
                items.append({
                    "name": item.name,
                    "type": "folder",
                    "path": rel,
                })
            else:
                items.append(self.api_launcher_metadata(item.path))

        try:
            path = str(target.relative_to(self.menu_root)).replace(chr(92), "/")
        except ValueError:
            path = ""

        return {"path": "" if path == "." else path, "items": items}

    def api_games(self, launcher: str, folder: str = "") -> dict:
        launcher_path = self.api_safe_launcher_path(launcher)
        config = load_launcher(launcher_path)
        target = self.api_safe_rom_folder(config, folder)

        items = []
        root = Path(config.rom_directory).resolve()

        if target != root:
            items.append({"name": "...", "type": "back"})

        for item in scan_rom_folder(config, target):
            try:
                rel = str(item.path.resolve().relative_to(root)).replace(chr(92), "/")
            except ValueError:
                rel = item.name

            items.append({
                "name": item.display_name,
                "type": "folder" if item.is_dir else "game",
                "path": rel,
            })

        try:
            folder_rel = str(target.relative_to(root)).replace(chr(92), "/")
        except ValueError:
            folder_rel = ""

        return {
            "launcher": launcher,
            "folder": "" if folder_rel == "." else folder_rel,
            "items": items,
        }

    def api_launch(self, payload: dict) -> dict:
        launcher = str(payload.get("launcher", ""))
        game = str(payload.get("game", payload.get("rom", "")))

        launcher_path = self.api_safe_launcher_path(launcher)
        config = load_launcher(launcher_path)

        if config.launcher_type == "application":
            self.suspend_frontend_input_for_launch()
            launch_external_process(f'"{config.emulator}" {config.arguments}'.strip(), str(Path(config.emulator).parent))
            return {"ok": True, "launched": "application", "launcher": launcher}

        rom_path = self.api_safe_rom_path(config, game)
        self.suspend_frontend_input_for_launch()
        launch_rom(config, rom_path)
        self.add_recent_game(launcher_path, rom_path)
        self.update_recent_items()

        return {
            "ok": True,
            "launcher": launcher,
            "game": game,
        }

    def api_show(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def update_system_items(self):
        self.system_items = [
            "Toggle Fullscreen",
            "Create Launcher",
            "Edit Launcher",
            "Refresh Menu",
            "Settings",
            "About",
            "Exit",
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

        logo_label = (
            "Disable Logo"
            if self.settings.get("show_logo", True)
            else "Enable Logo"
        )

        swap_ab_label = (
            "Disable Swap A/B"
            if self.settings.get("swap_controller_ab", False)
            else "Enable Swap A/B"
        )

        swap_xy_label = (
            "Disable Swap X/Y"
            if self.settings.get("swap_controller_xy", False)
            else "Enable Swap X/Y"
        )

        api_label = (
            "Disable API"
            if self.settings.get("api_enabled", False)
            else "Enable API"
        )

        self.settings_items = [
            fullscreen_launch_label,
            emulators_menu_label,
            recent_menu_label,
            favorites_menu_label,
            logo_label,
            "Clear Recent",
            "Clear Favorites",
            api_label,
            swap_ab_label,
            swap_xy_label,
            "Wallpaper",
        ]

    def launcher_type_values(self) -> list[str]:
        return ["Standalone Emulator", "RetroArch", "Application"]

    def launcher_type_to_json(self, type_name: str) -> str:
        if type_name == "RetroArch":
            return "retroarch"
        if type_name == "Application":
            return "application"
        return "standalone"

    def launcher_type_from_json(self, type_name: str) -> str:
        if type_name == "retroarch":
            return "RetroArch"
        if type_name == "application":
            return "Application"
        if type_name == "custom":
            return "Standalone Emulator"
        return "Standalone Emulator"

    def rebuild_launcher_form_folders(self):
        folders = [""]
        for folder in self.menu_root.rglob("*"):
            if folder.is_dir():
                try:
                    folders.append(str(folder.relative_to(self.menu_root)).replace(chr(92), "/"))
                except ValueError:
                    pass
        folders.append("__new__")
        self.launcher_form_folders = folders

    def launcher_form_folder_label(self) -> str:
        folder = self.launcher_form_data.get("folder", "")
        if folder == "__new__":
            return "Create New Folder"
        return "Root Menu" if not folder else folder

    def update_launcher_form_fields(self):
        launcher_type = self.launcher_form_data.get("type", "Standalone Emulator")

        fields = [
            "Type",
            "Launcher Name",
            "Save Folder",
        ]

        if self.launcher_form_data.get("folder") == "__new__":
            fields.append("New Folder Name")

        if launcher_type == "Application":
            fields.extend([
                "Application Name",
                "App Path",
                "Arguments",
                "Save",
                "Cancel",
            ])
        else:
            fields.extend([
                "Emulator Name",
                "System",
                "Emulator Path",
            ])

            if launcher_type == "RetroArch":
                fields.append("RetroArch Core")

            fields.extend([
                "ROM Path",
                "Extensions",
                "Arguments",
                "Save",
                "Cancel",
            ])

        if self.launcher_form_mode == "edit":
            fields.remove("Save Folder")
            if "New Folder Name" in fields:
                fields.remove("New Folder Name")

        self.launcher_form_fields = fields

    def open_launcher_form(self, launcher_path: Path | None = None):
        self.launcher_form_mode = "edit" if launcher_path else "create"
        self.launcher_form_path = launcher_path
        self.rebuild_launcher_form_folders()

        if launcher_path:
            try:
                data = json.loads(launcher_path.read_text(encoding="utf-8"))
            except Exception as exc:
                QMessageBox.warning(self, "Load failed", str(exc))
                return

            launcher_type = self.launcher_type_from_json(str(data.get("type", "standalone")))
            self.launcher_form_data = {
                "launcher_name": launcher_path.stem,
                "folder": "",
                "new_folder": "",
                "emulator_name": str(data.get("emulator_name", "")),
                "system": str(data.get("system", "")),
                "type": launcher_type,
                "emulator": str(data.get("emulator", "")),
                "core": str(data.get("core", "")),
                "rom_directory": str(data.get("rom_directory", "")),
                "extensions": ",".join(data.get("extensions", [])),
                "arguments": str(data.get("arguments", '"{rom}"')),
            }
        else:
            self.launcher_form_data = {
                "launcher_name": "",
                "folder": "",
                "new_folder": "",
                "emulator_name": "",
                "system": "",
                "type": "Standalone Emulator",
                "emulator": "",
                "core": "",
                "rom_directory": "",
                "extensions": "",
                "arguments": '"{rom}"',
            }

        self.update_launcher_form_fields()
        self.mode = "launcher_form"
        self.selected_index = 0
        self.view.scroll_offset = 0
        self.view.update()

    def default_arguments_for_type(self, type_name: str) -> str:
        if type_name == "RetroArch":
            return '-L "{core}" "{rom}"'
        if type_name == "Application":
            return ""
        return '"{rom}"'

    def set_launcher_type(self, type_name: str):
        old_type = self.launcher_form_data.get("type", "Standalone Emulator")
        old_default = self.default_arguments_for_type(old_type)
        current_args = self.launcher_form_data.get("arguments", "")

        self.launcher_form_data["type"] = type_name

        if not current_args or current_args == old_default:
            self.launcher_form_data["arguments"] = self.default_arguments_for_type(type_name)

    def launcher_form_value(self, field: str) -> str:
        if field == "Launcher Name":
            return self.launcher_form_data.get("launcher_name", "")
        if field == "Save Folder":
            return self.launcher_form_folder_label()
        if field == "New Folder Name":
            return self.launcher_form_data.get("new_folder", "")
        if field == "Emulator Name":
            return self.launcher_form_data.get("emulator_name", "")
        if field == "Application Name":
            return self.launcher_form_data.get("emulator_name", "")
        if field == "System":
            return self.launcher_form_data.get("system", "") or "Custom / Unknown"
        if field == "Type":
            return self.launcher_form_data.get("type", "Standalone Emulator")
        if field == "Emulator Path":
            return self.launcher_form_data.get("emulator", "")
        if field == "App Path":
            return self.launcher_form_data.get("emulator", "")
        if field == "RetroArch Core":
            return self.launcher_form_data.get("core", "")
        if field == "ROM Path":
            return self.launcher_form_data.get("rom_directory", "")
        if field == "Extensions":
            return self.launcher_form_data.get("extensions", "")
        if field == "Arguments":
            return self.launcher_form_data.get("arguments", "")
        return ""

    def cycle_launcher_form_value(self, delta: int):
        if not self.launcher_form_fields:
            return

        field = self.launcher_form_fields[self.selected_index]

        if field == "Save Folder":
            self.open_folder_picker()
            return

        if field == "Type":
            self.open_type_picker()
            return

    def open_folder_picker(self):
        self.rebuild_launcher_form_folders()
        self.folder_picker_items = self.launcher_form_folders[:]
        self.launcher_form_return_index = self.selected_index

        current = self.launcher_form_data.get("folder", "")
        try:
            self.selected_index = self.folder_picker_items.index(current)
        except ValueError:
            self.selected_index = 0

        self.mode = "folder_picker"
        self.view.scroll_offset = 0
        self.view.ensure_visible()
        self.view.update()

    def select_folder_picker_item(self):
        if not self.folder_picker_items:
            return

        selected = self.folder_picker_items[self.selected_index]
        self.launcher_form_data["folder"] = selected
        self.update_launcher_form_fields()
        self.mode = "launcher_form"
        self.selected_index = min(self.launcher_form_return_index, max(0, len(self.launcher_form_fields) - 1))
        self.view.scroll_offset = 0
        self.view.ensure_visible()
        self.view.update()

    def open_type_picker(self):
        self.type_picker_items = self.launcher_type_values()
        self.launcher_form_return_index = self.selected_index

        current = self.launcher_form_data.get("type", "Standalone Emulator")
        try:
            self.selected_index = self.type_picker_items.index(current)
        except ValueError:
            self.selected_index = 0

        self.mode = "type_picker"
        self.view.scroll_offset = 0
        self.view.ensure_visible()
        self.view.update()

    def select_type_picker_item(self):
        if not self.type_picker_items:
            return

        selected = self.type_picker_items[self.selected_index]
        self.set_launcher_type(selected)
        self.update_launcher_form_fields()
        self.mode = "launcher_form"
        self.selected_index = min(self.launcher_form_return_index, max(0, len(self.launcher_form_fields) - 1))
        self.view.scroll_offset = 0
        self.view.ensure_visible()
        self.view.update()

    def open_system_picker(self):
        self.system_picker_items = ["Custom / Unknown"] + ZAPAROO_SYSTEM_NAMES
        self.launcher_form_return_index = self.selected_index

        current = self.launcher_form_data.get("system", "")
        current_label = current or "Custom / Unknown"

        try:
            self.selected_index = self.system_picker_items.index(current_label)
        except ValueError:
            self.selected_index = 0

        self.mode = "system_picker"
        self.view.scroll_offset = 0
        self.view.ensure_visible()
        self.view.update()

    def select_system_picker_item(self):
        if not self.system_picker_items:
            return

        selected = self.system_picker_items[self.selected_index]
        self.launcher_form_data["system"] = "" if selected == "Custom / Unknown" else selected
        self.mode = "launcher_form"
        self.selected_index = min(self.launcher_form_return_index, max(0, len(self.launcher_form_fields) - 1))
        self.view.scroll_offset = 0
        self.view.ensure_visible()
        self.view.update()

    def edit_launcher_form_field(self):
        if not self.launcher_form_fields:
            return

        field = self.launcher_form_fields[self.selected_index]

        if field == "Save":
            self.save_launcher_form()
            return

        if field == "Cancel":
            self.cancel_launcher_form()
            return

        if field == "Type":
            self.open_type_picker()
            return

        if field == "System":
            self.open_system_picker()
            return

        if field == "Save Folder":
            self.open_folder_picker()
            return

        key_map = {
            "Launcher Name": "launcher_name",
            "New Folder Name": "new_folder",
            "Emulator Name": "emulator_name",
            "Application Name": "emulator_name",
            "Extensions": "extensions",
            "Arguments": "arguments",
        }

        if field in key_map:
            key = key_map[field]
            prompt = field + ":"

            if field == "Arguments":
                launcher_type = self.launcher_form_data.get("type", "Standalone Emulator")
                if launcher_type == "Application":
                    prompt = "Optional application arguments. Usually empty."
                elif launcher_type == "RetroArch":
                    prompt = 'Arguments. Use {core} and {rom}, for example: -L "{core}" "{rom}"'
                else:
                    prompt = 'Arguments. Use {rom} for the selected game, for example: -fullscreen "{rom}"'

            value, ok = QInputDialog.getText(self, field, prompt, text=self.launcher_form_data.get(key, ""))
            if ok:
                self.launcher_form_data[key] = value.strip()
                self.view.update()
            return

        if field in ("Emulator Path", "App Path"):
            title = "Select application" if field == "App Path" else "Select emulator executable"
            path, _ = QFileDialog.getOpenFileName(
                self,
                title,
                str(self.base_dir),
                "Executables (*.exe);;All files (*.*)",
            )
            if path:
                self.launcher_form_data["emulator"] = path.replace(chr(92), "/")
                self.view.update()
            return

        if field == "RetroArch Core":
            path, _ = QFileDialog.getOpenFileName(
                self,
                "Select RetroArch core",
                str(self.base_dir),
                "RetroArch cores (*.dll);;All files (*.*)",
            )
            if path:
                self.launcher_form_data["core"] = path.replace(chr(92), "/")
                self.view.update()
            return

        if field == "ROM Path":
            path = QFileDialog.getExistingDirectory(self, "Select ROM folder", str(self.base_dir))
            if path:
                self.launcher_form_data["rom_directory"] = path.replace(chr(92), "/")
                self.view.update()
            return

    def launcher_form_safe_filename(self, name: str) -> str:
        blocked = '<>:"/\\|?*'
        cleaned = "".join("_" if ch in blocked else ch for ch in name).strip()
        return cleaned or "Launcher"

    def save_launcher_form(self):
        data = self.launcher_form_data

        launcher_name = data.get("launcher_name", "").strip()
        emulator_name = data.get("emulator_name", "").strip()
        emulator = data.get("emulator", "").strip()
        rom_directory = data.get("rom_directory", "").strip()
        launcher_type = data.get("type", "Standalone Emulator")

        if not launcher_name:
            QMessageBox.warning(self, "Missing launcher name", "Enter a launcher name.")
            return
        if not emulator_name:
            QMessageBox.warning(self, "Missing emulator name", "Enter an emulator name.")
            return
        if not emulator:
            QMessageBox.warning(self, "Missing executable", "Select an emulator or application path.")
            return
        if launcher_type != "Application" and not rom_directory:
            QMessageBox.warning(self, "Missing ROM path", "Select a ROM path.")
            return
        if launcher_type == "RetroArch" and not data.get("core", "").strip():
            QMessageBox.warning(self, "Missing core", "Select a RetroArch core.")
            return

        if self.launcher_form_mode == "edit" and self.launcher_form_path:
            json_path = self.launcher_form_path
        else:
            folder_value = data.get("folder", "")
            if folder_value == "__new__":
                folder_name = data.get("new_folder", "").strip()
                if not folder_name:
                    QMessageBox.warning(self, "Missing folder name", "Enter a new folder name.")
                    return
                target_folder = self.menu_root / self.launcher_form_safe_filename(folder_name)
            elif folder_value:
                target_folder = self.menu_root / folder_value
            else:
                target_folder = self.menu_root

            target_folder.mkdir(parents=True, exist_ok=True)
            json_path = target_folder / f"{self.launcher_form_safe_filename(launcher_name)}.json"

            if json_path.exists():
                QMessageBox.warning(self, "Already exists", f"{json_path.name} already exists.")
                return

        extensions = []
        for ext in data.get("extensions", "").split(","):
            ext = ext.strip().lower()
            if not ext:
                continue
            if not ext.startswith("."):
                ext = "." + ext
            extensions.append(ext)

        output = {
            "type": self.launcher_type_to_json(data.get("type", "Standalone Emulator")),
            "emulator_name": emulator_name,
            "system": data.get("system", ""),
            "emulator": emulator,
            "rom_directory": rom_directory,
            "extensions": extensions,
            "arguments": data.get("arguments", "").strip() or '"{rom}"',
            "recursive": True,
        }

        if data.get("type") == "RetroArch":
            output["core"] = data.get("core", "").strip()

        json_path.write_text(json.dumps(output, indent=2), encoding="utf-8")

        if self.launcher_form_mode == "edit":
            self.open_edit_launcher_browser(self.current_edit_folder)
        else:
            self.current_folder = self.menu_root
            self.refresh_menu()

    def cancel_launcher_form(self):
        if self.launcher_form_mode == "edit":
            self.open_edit_launcher_browser(self.current_edit_folder)
        else:
            self.mode = "system"
            self.selected_index = 0
            self.view.update()

    def title_path(self) -> str:
        if self.mode == "system":
            return "Gentleman Menu"
        if self.mode == "settings":
            return "Settings"
        if self.mode == "wallpaper":
            return "Wallpaper"
        if self.mode == "launcher_form":
            return "Edit Launcher" if self.launcher_form_mode == "edit" else "Create Launcher"
        if self.mode == "system_picker":
            return "System"
        if self.mode == "type_picker":
            return "Type"
        if self.mode == "folder_picker":
            return "Save Folder"
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
        if self.mode == "launcher_form":
            self.update_launcher_form_fields()
            return len(self.launcher_form_fields)
        if self.mode == "system_picker":
            return len(self.system_picker_items)
        if self.mode == "type_picker":
            return len(self.type_picker_items)
        if self.mode == "folder_picker":
            return len(self.folder_picker_items)
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
        if self.mode == "launcher_form":
            self.update_launcher_form_fields()
            labels = []
            for field in self.launcher_form_fields:
                if field in ("Save", "Cancel"):
                    labels.append((field, ""))
                else:
                    labels.append((f"{field}: {self.launcher_form_value(field)}", ""))
            return labels
        if self.mode == "system_picker":
            return [(name, "") for name in self.system_picker_items]
        if self.mode == "type_picker":
            return [(name, "") for name in self.type_picker_items]
        if self.mode == "folder_picker":
            labels = []
            for item in self.folder_picker_items:
                if item == "":
                    labels.append(("Root Menu", ""))
                elif item == "__new__":
                    labels.append(("Create New Folder", ""))
                else:
                    labels.append((item, ""))
            return labels
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

        if self.mode == "launcher_form":
            self.cancel_launcher_form()
            return

        if self.mode == "system_picker":
            self.mode = "launcher_form"
            self.selected_index = min(self.launcher_form_return_index, max(0, len(self.launcher_form_fields) - 1))
            self.view.ensure_visible()
            self.view.update()
            return

        if self.mode == "type_picker":
            self.mode = "launcher_form"
            self.selected_index = min(self.launcher_form_return_index, max(0, len(self.launcher_form_fields) - 1))
            self.view.ensure_visible()
            self.view.update()
            return

        if self.mode == "folder_picker":
            self.mode = "launcher_form"
            self.selected_index = min(self.launcher_form_return_index, max(0, len(self.launcher_form_fields) - 1))
            self.view.ensure_visible()
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

        if self.mode == "system_picker":
            self.select_system_picker_item()
            return

        if self.mode == "type_picker":
            self.select_type_picker_item()
            return

        if self.mode == "folder_picker":
            self.select_folder_picker_item()
            return

        if self.mode == "launcher_form":
            self.edit_launcher_form_field()
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

            self.open_launcher_form(item.path)
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
                self.suspend_frontend_input_for_launch()
                launch_external_process(f'"{emulator_path}"', str(Path(emulator_path).parent))
            except Exception as exc:
                self.resume_frontend_input_after_launch()
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
                self.suspend_frontend_input_for_launch()
                launch_rom(self.current_launcher, selected.path)
                self.add_recent_game(self.current_launcher.path, selected.path)
                self.update_recent_items()
            except Exception as exc:
                self.resume_frontend_input_after_launch()
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
            self.suspend_frontend_input_for_launch()
            launch_rom(launcher, rom)
            self.add_recent_game(launcher_path, rom)
            self.update_recent_items()
            self.view.update()
        except Exception as exc:
            self.resume_frontend_input_after_launch()
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
            self.open_launcher_form()
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
        elif item == "About":
            self.mode = "about"
            self.selected_index = 0
            self.view.update()
        elif item == "Exit":
            QApplication.quit()

    def clear_recent_items(self):
        reply = QMessageBox.question(
            self,
            "Clear Recent",
            "Clear all recently launched games?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )

        if reply != QMessageBox.StandardButton.Yes:
            return

        self.save_recent_items([])
        self.update_recent_items()
        self.view.update()

    def clear_favorite_items(self):
        reply = QMessageBox.question(
            self,
            "Clear Favorites",
            "Clear all favorite games?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )

        if reply != QMessageBox.StandardButton.Yes:
            return

        self.save_favorite_items([])
        self.update_favorite_items()
        self.view.update()

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
        elif item == "Enable Logo":
            self.settings["show_logo"] = True
            self.save_settings()
            self.update_settings_items()
            self.view.update()
        elif item == "Disable Logo":
            self.settings["show_logo"] = False
            self.save_settings()
            self.update_settings_items()
            self.view.update()
        elif item == "Clear Recent":
            self.clear_recent_items()
        elif item == "Clear Favorites":
            self.clear_favorite_items()
        elif item == "Enable API":
            self.settings["api_enabled"] = True
            self.save_settings()
            self.apply_remote_api_state()
            self.update_settings_items()
            self.view.update()
        elif item == "Disable API":
            self.settings["api_enabled"] = False
            self.save_settings()
            self.apply_remote_api_state()
            self.update_settings_items()
            self.view.update()
        elif item == "Enable Swap A/B":
            self.settings["swap_controller_ab"] = True
            self.save_settings()
            self.update_settings_items()
            self.view.update()
        elif item == "Disable Swap A/B":
            self.settings["swap_controller_ab"] = False
            self.save_settings()
            self.update_settings_items()
            self.view.update()
        elif item == "Enable Swap X/Y":
            self.settings["swap_controller_xy"] = True
            self.save_settings()
            self.update_settings_items()
            self.view.update()
        elif item == "Disable Swap X/Y":
            self.settings["swap_controller_xy"] = False
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

    def suspend_frontend_input_for_launch(self):
        self.input_suspended_for_launch = True
        self.controller_button_state.clear()
        self.controller_repeat_action = None
        self.controller_repeat_next_ms = 0

        for action in self.controller_axis_state:
            self.controller_axis_state[action] = False

    def resume_frontend_input_after_launch(self):
        if not self.input_suspended_for_launch:
            return

        self.input_suspended_for_launch = False
        self.controller_button_state.clear()
        self.controller_repeat_action = None
        self.controller_repeat_next_ms = 0

    def changeEvent(self, event):
        if event.type() == QEvent.Type.ActivationChange and self.isActiveWindow():
            QTimer.singleShot(150, self.resume_frontend_input_after_launch)

        super().changeEvent(event)

    def init_controller_support(self):
        if pygame is None:
            return

        try:
            pygame.init()
            pygame.joystick.init()
            self.refresh_controller(force=True)
        except Exception:
            self.controller = None
            self.controller_available = False

    def refresh_controller(self, force: bool = False):
        if pygame is None:
            return

        now = int(time.monotonic() * 1000)
        if not force and now - self.controller_last_scan_ms < 1000:
            return

        self.controller_last_scan_ms = now

        try:
            if not pygame.joystick.get_init():
                pygame.joystick.init()

            count = pygame.joystick.get_count()

            if count > 0:
                if self.controller is None or not self.controller.get_init():
                    self.controller = pygame.joystick.Joystick(0)
                    self.controller.init()
                    self.controller_button_state.clear()
                    self.controller_repeat_action = None
                    self.controller_repeat_next_ms = 0

                self.controller_available = True
            else:
                self.controller = None
                self.controller_available = False
                self.controller_button_state.clear()
                self.controller_repeat_action = None
                self.controller_repeat_next_ms = 0
        except Exception:
            self.controller = None
            self.controller_available = False
            self.controller_button_state.clear()
            self.controller_repeat_action = None
            self.controller_repeat_next_ms = 0

    def controller_activate(self):
        self.active_input = "controller"
        self.activate_selected()
        self.view.update()

    def controller_back(self):
        self.active_input = "controller"
        self.go_back()
        self.view.update()

    def controller_favorite(self):
        self.active_input = "controller"
        if self.mode == "favorites":
            self.remove_selected_favorite()
        else:
            self.toggle_current_favorite()
        self.view.update()

    def controller_move(self, delta: int):
        self.active_input = "controller"
        self.move_selection(delta)
        self.view.update()

    def controller_step(self, action: str):
        if action == "up":
            self.controller_move(-1)
        elif action == "down":
            self.controller_move(1)
        elif action == "left":
            if self.mode == "launcher_form":
                self.cycle_launcher_form_value(-1)
                self.active_input = "controller"
                self.view.update()
            else:
                self.controller_move(-10)
        elif action == "right":
            if self.mode == "launcher_form":
                self.cycle_launcher_form_value(1)
                self.active_input = "controller"
                self.view.update()
            else:
                self.controller_move(10)

    def handle_controller_repeat(self, active_actions: list[str]):
        now = int(time.monotonic() * 1000)

        if not active_actions:
            self.controller_repeat_action = None
            self.controller_repeat_next_ms = 0
            return

        action = active_actions[0]

        if action != self.controller_repeat_action:
            self.controller_repeat_action = action
            self.controller_repeat_next_ms = now + 350
            self.controller_step(action)
            return

        if now >= self.controller_repeat_next_ms:
            self.controller_repeat_next_ms = now + 90
            self.controller_step(action)

    def poll_controller(self):
        if pygame is None:
            return

        try:
            events = pygame.event.get()
        except Exception:
            self.refresh_controller(force=True)
            return

        device_changed = False
        for event in events:
            if event.type in (pygame.JOYDEVICEADDED, pygame.JOYDEVICEREMOVED):
                device_changed = True

        if device_changed:
            self.controller = None
            self.controller_available = False
            self.refresh_controller(force=True)

        self.refresh_controller()

        if not self.controller_available or self.controller is None:
            return

        if self.input_suspended_for_launch:
            self.controller_button_state.clear()
            self.controller_repeat_action = None
            self.controller_repeat_next_ms = 0
            return

        try:
            hat_x = 0
            hat_y = 0
            if self.controller.get_numhats() > 0:
                hat_x, hat_y = self.controller.get_hat(0)

            axis_x = self.controller.get_axis(0) if self.controller.get_numaxes() > 0 else 0
            axis_y = self.controller.get_axis(1) if self.controller.get_numaxes() > 1 else 0

            active_actions = []
            if hat_y > 0 or axis_y < -0.5:
                active_actions.append("up")
            elif hat_y < 0 or axis_y > 0.5:
                active_actions.append("down")
            elif hat_x < 0 or axis_x < -0.5:
                active_actions.append("left")
            elif hat_x > 0 or axis_x > 0.5:
                active_actions.append("right")

            self.handle_controller_repeat(active_actions)

            if self.settings.get("swap_controller_ab", False):
                accept_button = 1
                back_button = 0
            else:
                accept_button = 0
                back_button = 1

            if self.settings.get("swap_controller_xy", False):
                favorite_button = 3
            else:
                favorite_button = 2

            button_actions = {
                accept_button: self.controller_activate,
                back_button: self.controller_back,
                favorite_button: self.controller_favorite,
                6: self.controller_back,
                7: self.controller_activate,
            }

            for button, callback in button_actions.items():
                if button >= self.controller.get_numbuttons():
                    continue

                pressed = bool(self.controller.get_button(button))
                previous = self.controller_button_state.get(button, False)

                if pressed and not previous:
                    callback()

                self.controller_button_state[button] = pressed
        except Exception:
            self.controller = None
            self.controller_available = False
            self.refresh_controller(force=True)

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
        if self.input_suspended_for_launch:
            self.resume_frontend_input_after_launch()

        self.active_input = "keyboard"

        key = event.key()

        if key in (Qt.Key.Key_Up, Qt.Key.Key_W):
            self.move_selection(-1)
        elif key in (Qt.Key.Key_Down, Qt.Key.Key_S):
            self.move_selection(1)
        elif key in (Qt.Key.Key_Left, Qt.Key.Key_A):
            if self.mode == "launcher_form":
                self.cycle_launcher_form_value(-1)
            else:
                self.move_selection(-10)
        elif key in (Qt.Key.Key_Right, Qt.Key.Key_D):
            if self.mode == "launcher_form":
                self.cycle_launcher_form_value(1)
            else:
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

    def closeEvent(self, event):
        if self.remote_api_server:
            self.remote_api_server.stop()
            self.remote_api_server = None
        super().closeEvent(event)



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

        self.logo = QPixmap(str(self.window.assets_dir / "logo.png"))

        self.static_noise = QPixmap()
        self.static_noise_frame = 0

        self.icon_dir = self.window.assets_dir / "icons"
        self.icon_renderers = {
            "lan": QSvgRenderer(str(self.icon_dir / "lan.svg")),
            "wifi": QSvgRenderer(str(self.icon_dir / "wifi.svg")),
            "bluetooth": QSvgRenderer(str(self.icon_dir / "bluetooth.svg")),
            "keyboard": QSvgRenderer(str(self.icon_dir / "keyboard.svg")),
            "controller": QSvgRenderer(str(self.icon_dir / "controller.svg")),
            "favorite": QSvgRenderer(str(self.icon_dir / "favorite.svg")),
            "api": QSvgRenderer(str(self.icon_dir / "api.svg")),
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
            self.draw_static_noise(painter)

        self.draw_logo(painter)
        self.draw_top_bar(painter)
        self.draw_panel(painter)

    def draw_logo(self, painter: QPainter):
        if not self.window.settings.get("show_logo", True):
            return

        if self.logo.isNull():
            return

        margin = 28
        max_w = min(260, max(180, int(self.width() * 0.22)))
        scale = max_w / self.logo.width()
        logo_w = int(self.logo.width() * scale)
        logo_h = int(self.logo.height() * scale)

        if logo_h > int(self.height() * 0.16):
            scale = (self.height() * 0.16) / self.logo.height()
            logo_w = int(self.logo.width() * scale)
            logo_h = int(self.logo.height() * scale)

        scaled = self.logo.scaled(
            logo_w,
            logo_h,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        painter.drawPixmap(margin, margin, scaled)

    def draw_static_noise(self, painter: QPainter):
        noise_w = 220
        noise_h = 124

        data = bytearray(os.urandom(noise_w * noise_h))
        for index, value in enumerate(data):
            data[index] = 70 + (value % 150)

        image = QImage(
            bytes(data),
            noise_w,
            noise_h,
            noise_w,
            QImage.Format.Format_Grayscale8,
        )
        self.static_noise = QPixmap.fromImage(image.copy())
        self.static_noise_frame += 1

        scaled = self.static_noise.scaled(
            self.size(),
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.FastTransformation,
        )

        painter.drawPixmap(0, 0, scaled)
        painter.fillRect(self.rect(), QColor(30, 0, 10, 45))

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

        if self.window.remote_api_server and self.window.remote_api_server.is_running():
            icon_x += icon_gap
            self.draw_svg_icon(painter, "api", QRect(icon_x, icon_y, icon_size, icon_size), self.dark_text)

        time_text = datetime.now().strftime("%H:%M")
        time_width = painter.fontMetrics().horizontalAdvance(time_text)
        painter.drawText(x + bar_w - 18 - time_width, y + 31, time_text)

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
