from __future__ import annotations

import json
import ctypes
import threading
import os
import platform
import socket
import subprocess
import sys
import time
import webbrowser
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote

from PyQt6.QtCore import Qt, QTimer, QRect, QRectF, QEvent, QThread, pyqtSignal
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

from app.app_info import APP_NAME, APP_VERSION, ABOUT_LINES
from app.dialogs.changelog_dialog import ChangelogDialog
from app.dialogs.update_available_dialog import UpdateAvailableDialog
from app.zaparoo_systems import ZAPAROO_SYSTEM_NAMES
from core.arcade_names import ArcadeNameDatabase
from core.launcher import load_launcher, scan_rom_folder, launch_rom, launch_external_process, LauncherConfig, RomBrowserItem
from core.menu_scanner import MenuItem, scan_menu_folder
from core.remote_api import GentlemanApiServer
from core.updater import (
    check_for_update,
    gentleman_updater_available,
    launch_gentleman_updater,
    open_release_page,
)

try:
    import pygame
except Exception:
    pygame = None

try:
    import psutil
except Exception:
    psutil = None


class UpdateCheckWorker(QThread):
    result = pyqtSignal(object)
    error = pyqtSignal(str)

    def run(self):
        try:
            info = check_for_update()
            self.result.emit(info)
        except Exception as exc:
            self.error.emit(str(exc))


def resource_path(relative_path: str) -> Path:
    base_path = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
    return base_path / relative_path


class InGameOsd(QWidget):
    def __init__(self, window):
        super().__init__(None)
        self.window = window
        self.selected_index = 0
        self.options = []
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.resize(960, 640)

        self.panel = QColor(55, 0, 15, 245)
        self.light = QColor(220, 185, 190)
        self.text = QColor(245, 235, 235)
        self.dark_text = QColor(40, 0, 10)

        self.font = QFont("Consolas", 20)
        self.font.setStyleHint(QFont.StyleHint.Monospace)
        self.title_font = QFont("Consolas", 22, QFont.Weight.Bold)
        self.title_font.setStyleHint(QFont.StyleHint.Monospace)

    def refresh_options(self):
        session = self.window.active_session_snapshot()
        kind = session.get("type")
        noun = "Game" if kind == "game" else "Emulator" if kind == "emulator" else "Application"
        self.options = ["Resume", f"Close {noun}", f"Force Close {noun}"]
        self.selected_index = min(self.selected_index, len(self.options) - 1)
        self.update()

    def show_osd(self):
        self.refresh_options()
        screen = QApplication.primaryScreen()
        if screen:
            geometry = screen.geometry()
            self.resize(geometry.size())
            self.move(geometry.topLeft())
        self.show()
        self.raise_()
        self.activateWindow()
        self.setFocus()

    def move_selection(self, delta):
        if not self.options:
            return
        self.selected_index = (self.selected_index + delta) % len(self.options)
        self.update()

    def activate_selected(self):
        if self.selected_index == 0:
            self.window.hide_ingame_osd(resume=True)
        elif self.selected_index == 1:
            self.window.close_active_session(force=False)
            self.window.hide_ingame_osd(resume=False)
        elif self.selected_index == 2:
            answer = QMessageBox.warning(
                self,
                "Force Close",
                "Force closing may interrupt save data or emulator writes.\n\nContinue?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer == QMessageBox.StandardButton.Yes:
                self.window.close_active_session(force=True)
                self.window.hide_ingame_osd(resume=False)

    def keyPressEvent(self, event):
        key = event.key()
        if key in (Qt.Key.Key_Up, Qt.Key.Key_W):
            self.move_selection(-1)
        elif key in (Qt.Key.Key_Down, Qt.Key.Key_S):
            self.move_selection(1)
        elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.activate_selected()
        elif key in (Qt.Key.Key_Escape, Qt.Key.Key_Backspace):
            self.window.hide_ingame_osd(resume=True)
        event.accept()

    def menu_panel_rect(self) -> QRect:
        panel_w = min(620, self.width() - 160)
        panel_h = min(430, self.height() - 200)
        return QRect((self.width() - panel_w) // 2, (self.height() - panel_h) // 2, panel_w, panel_h)

    def top_bar_rect(self) -> QRect:
        panel = self.menu_panel_rect()
        return QRect(panel.x(), max(16, panel.y() - 78), panel.width(), 44)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        painter.fillRect(self.rect(), QColor(0, 0, 0, 205))

        bar = self.top_bar_rect()
        painter.fillRect(bar, self.light)
        painter.setFont(self.title_font)
        painter.setPen(self.dark_text)
        painter.drawText(bar.x() + 18, bar.y() + 31, "Gentleman")

        time_text = datetime.now().strftime("%H:%M")
        time_width = painter.fontMetrics().horizontalAdvance(time_text)
        painter.drawText(bar.right() - 18 - time_width, bar.y() + 31, time_text)

        panel = self.menu_panel_rect()
        side_w = 48
        painter.fillRect(panel, self.panel)
        painter.fillRect(QRect(panel.x(), panel.y(), side_w, panel.height()), self.light)

        painter.setFont(self.font)
        painter.setPen(self.dark_text)
        side_title = "IN-GAME OSD"
        title_width = painter.fontMetrics().horizontalAdvance(side_title)
        painter.save()
        painter.translate(panel.x() + 31, panel.y() + (panel.height() + title_width) // 2)
        painter.rotate(-90)
        painter.drawText(0, 0, side_title)
        painter.restore()

        session = self.window.active_session_snapshot()
        name = str(session.get("name") or "Running Session")
        emulator = str(session.get("emulator") or "")
        text_x = panel.x() + side_w + 22
        right_x = panel.right() - 72

        painter.setPen(self.text)
        painter.setFont(self.title_font)
        name_metrics = painter.fontMetrics()
        available = max(40, right_x - text_x)
        painter.drawText(text_x, panel.y() + 48, name_metrics.elidedText(name, Qt.TextElideMode.ElideRight, available))

        if emulator:
            painter.setFont(self.font)
            painter.drawText(text_x, panel.y() + 82, painter.fontMetrics().elidedText(emulator, Qt.TextElideMode.ElideRight, available))

        divider_y = panel.y() + 108
        painter.fillRect(QRect(text_x - 6, divider_y, right_x - text_x + 6, 2), self.light)

        painter.setFont(self.font)
        start_y = divider_y + 45
        for index, option in enumerate(self.options):
            yy = start_y + index * 42
            row = QRect(text_x - 6, yy - 27, right_x - text_x + 6, 32)
            if index == self.selected_index:
                painter.fillRect(row, self.light)
                painter.setPen(self.dark_text)
            else:
                painter.setPen(self.text)
            painter.drawText(text_x, yy, option)


class GentlemanWindow(QMainWindow):
    api_show_requested = pyqtSignal()
    api_input_requested = pyqtSignal(str, object)
    api_input_context_requested = pyqtSignal(object)

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
        self.arcade_name_database = ArcadeNameDatabase(self.assets_dir / "databases" / "arcade_names.json")
        self.ensure_settings_app_info()
        self.remote_api_server: GentlemanApiServer | None = None
        self.api_show_requested.connect(self._show_from_api)
        self.api_input_requested.connect(self._handle_api_input)
        self.api_input_context_requested.connect(self._handle_api_input_context)
        self.update_check_worker: UpdateCheckWorker | None = None
        self.startup_update_check_done = False

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
        self.support_items = [
            "Ko-fi",
            "Buy Me a Coffee",
        ]

        self.current_launcher: LauncherConfig | None = None
        self.current_rom_folder: Path | None = None
        self.selected_index = 0
        self.active_input = "keyboard"
        self.input_suspended_for_launch = False
        self.active_process = None
        self.active_session = {"running": False, "type": None, "name": None, "emulator": None}
        self.active_session_lock = threading.RLock()
        self.osd_shortcut_started_ms = 0
        self.osd_shortcut_latched = False
        self.keyboard_osd_latched = False
        self.ingame_osd = InGameOsd(self)
        self.suspended_session_processes = []
        self.dolphin_osd_fullscreen_toggled = False
        self.local_ip_address = self.resolve_local_ip_address()

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

        self.process_timer = QTimer(self)
        self.process_timer.timeout.connect(self.check_active_process)
        self.process_timer.start(500)

        self.ip_refresh_timer = QTimer(self)
        self.ip_refresh_timer.timeout.connect(self.refresh_local_ip_address)
        self.ip_refresh_timer.start(30000)

        self.apply_remote_api_state()

        if self.settings.get("fullscreen_at_launch", False):
            QTimer.singleShot(0, self.showFullScreen)

        if self.settings.get("check_updates_at_launch", True):
            QTimer.singleShot(1500, self.check_for_updates_on_startup)

    def resolve_local_ip_address(self) -> str:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect(("8.8.8.8", 80))
            address = sock.getsockname()[0]
            return address or "Not connected"
        except OSError:
            try:
                return socket.gethostbyname(socket.gethostname()) or "Not connected"
            except OSError:
                return "Not connected"
        finally:
            sock.close()

    def refresh_local_ip_address(self):
        address = self.resolve_local_ip_address()
        if address != self.local_ip_address:
            self.local_ip_address = address
            if self.mode == "system":
                self.view.update()

    def default_settings(self) -> dict:
        return {
            "app_name": APP_NAME,
            "app_version": APP_VERSION,
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
            "check_updates_at_launch": True,
            "normalize_arcade_names": True,
            "ingame_osd_enabled": True,
        }

    def load_settings(self) -> dict:
        defaults = self.default_settings()

        if not self.settings_path.exists():
            return defaults

        try:
            data = json.loads(self.settings_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return defaults

            for key, value in defaults.items():
                data.setdefault(key, value)

            return data
        except Exception:
            return defaults

    def ensure_settings_app_info(self):
        changed = False

        if self.settings.get("app_name") != APP_NAME:
            self.settings["app_name"] = APP_NAME
            changed = True

        if self.settings.get("app_version") != APP_VERSION:
            self.settings["app_version"] = APP_VERSION
            changed = True

        if changed or not self.settings_path.exists():
            self.save_settings()

    def save_settings(self):
        self.settings_path.write_text(json.dumps(self.settings, indent=2), encoding="utf-8")


    def arcade_name_normalization_enabled(self) -> bool:
        return bool(self.settings.get("normalize_arcade_names", True))

    def arcade_names_for_launcher(self, launcher: LauncherConfig) -> dict[str, str] | None:
        if not self.arcade_name_normalization_enabled():
            return None
        if launcher.system.strip().lower() != "arcade":
            return None
        return self.arcade_name_database.names()

    def scan_launcher_folder(self, launcher: LauncherConfig, folder: Path) -> list[RomBrowserItem]:
        return scan_rom_folder(
            launcher,
            folder,
            self.arcade_names_for_launcher(launcher),
        )

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

    def ingame_osd_enabled(self) -> bool:
        return bool(self.settings.get("ingame_osd_enabled", True))

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
            "session": self.active_session_snapshot(),
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

        for item in self.scan_launcher_folder(config, target):
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
            process = launch_external_process(f'"{config.emulator}" {config.arguments}'.strip(), str(Path(config.emulator).parent))
            self.begin_active_session(process, "application", config.emulator_name or launcher_path.stem, "")
            return {"ok": True, "launched": "application", "launcher": launcher, "session": self.active_session_snapshot()}

        rom_path = self.api_safe_rom_path(config, game)
        display_name = self.display_name_for_rom(config, rom_path)
        process = launch_rom(config, rom_path)
        self.begin_active_session(process, "game", display_name, config.emulator_name or launcher_path.stem)
        self.add_recent_game(launcher_path, rom_path)
        self.update_recent_items()

        return {
            "ok": True,
            "launcher": launcher,
            "game": game,
            "session": self.active_session_snapshot(),
        }

    def api_show(self):
        self.api_show_requested.emit()

    def api_input(self, action: str) -> dict:
        request = {
            "event": threading.Event(),
            "result": None,
        }
        self.api_input_requested.emit(str(action or "").strip().lower(), request)

        if not request["event"].wait(2.0):
            return {"ok": False, "error": "Input request timed out"}

        return request.get("result") or {"ok": False, "error": "Input request failed"}

    def api_input_context(self) -> dict:
        request = {
            "event": threading.Event(),
            "result": None,
        }
        self.api_input_context_requested.emit(request)

        if not request["event"].wait(2.0):
            return {"ok": False, "error": "Input context request timed out"}

        return request.get("result") or {"ok": False, "error": "Input context request failed"}

    def _input_context_snapshot(self) -> dict:
        favorite_item = self.favorite_item_from_current_selection()
        favorite_available = favorite_item is not None

        return {
            "ok": True,
            "mode": self.mode,
            "favorite_available": favorite_available,
            "is_favorite": self.current_selection_is_favorite() if favorite_available else False,
            "selected_game": favorite_item.get("name", "") if favorite_item else "",
        }

    def _handle_api_input_context(self, request: object):
        try:
            request["result"] = self._input_context_snapshot()
        except Exception as exc:
            request["result"] = {"ok": False, "error": str(exc)}
        finally:
            request["event"].set()

    def _handle_api_input(self, action: str, request: object):
        try:
            if self.input_suspended_for_launch:
                request["result"] = {
                    "ok": False,
                    "error": "Gentleman navigation is unavailable while a game or application is running",
                }
                return

            self.active_input = "controller"

            if action == "up":
                self.move_selection(-1)
            elif action == "down":
                self.move_selection(1)
            elif action == "left":
                if self.mode == "launcher_form":
                    self.cycle_launcher_form_value(-1)
                else:
                    self.move_selection(-10)
            elif action == "right":
                if self.mode == "launcher_form":
                    self.cycle_launcher_form_value(1)
                else:
                    self.move_selection(10)
            elif action == "select":
                self.activate_selected()
            elif action == "back":
                self.go_back()
            elif action == "favorite":
                if self.favorite_item_from_current_selection() is None:
                    request["result"] = {
                        "ok": False,
                        "error": "Favorite is only available for a selected game",
                        "context": self._input_context_snapshot(),
                    }
                    return
                self.toggle_current_favorite()
            else:
                request["result"] = {"ok": False, "error": "Unsupported input action"}
                return

            self.view.update()
            request["result"] = {
                "ok": True,
                "action": action,
                "context": self._input_context_snapshot(),
            }
        except Exception as exc:
            request["result"] = {"ok": False, "error": str(exc)}
        finally:
            request["event"].set()

    def _show_from_api(self):
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
            "Wallpapers",
            "Report Issues & Requests",
            "Support the Project",
            "Check for Updates",
            "About",
            "Exit",
        ]

    def setting_state_label(self, name: str, enabled: bool) -> str:
        state = "Enabled" if enabled else "Disabled"
        return f"{name}: {state}"

    def update_settings_items(self):
        fullscreen_launch_label = self.setting_state_label(
            "Fullscreen at Launch",
            self.settings.get("fullscreen_at_launch", False),
        )

        emulators_menu_label = self.setting_state_label(
            "Emulators Menu",
            self.emulators_menu_enabled(),
        )

        recent_menu_label = self.setting_state_label(
            "Recent Menu",
            self.recent_menu_enabled(),
        )

        favorites_menu_label = self.setting_state_label(
            "Favorites Menu",
            self.favorites_menu_enabled(),
        )

        logo_label = self.setting_state_label(
            "Logo",
            self.settings.get("show_logo", True),
        )

        swap_ab_label = self.setting_state_label(
            "Swap A/B",
            self.settings.get("swap_controller_ab", False),
        )

        swap_xy_label = self.setting_state_label(
            "Swap X/Y",
            self.settings.get("swap_controller_xy", False),
        )

        api_label = self.setting_state_label(
            "API",
            self.settings.get("api_enabled", False),
        )

        arcade_names_label = self.setting_state_label(
            "Arcade ROM Names",
            self.arcade_name_normalization_enabled(),
        )

        update_check_label = self.setting_state_label(
            "Update Check at Launch",
            self.settings.get("check_updates_at_launch", True),
        )

        ingame_osd_label = self.setting_state_label(
            "In-Game OSD",
            self.ingame_osd_enabled(),
        )

        self.settings_items = [
            fullscreen_launch_label,
            emulators_menu_label,
            recent_menu_label,
            favorites_menu_label,
            logo_label,
            arcade_names_label,
            "Clear Recent",
            "Clear Favorites",
            update_check_label,
            ingame_osd_label,
            api_label,
            swap_ab_label,
            swap_xy_label,
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
            return "Wallpapers"
        if self.mode == "support":
            return "Support the Project"
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
        if self.mode == "support":
            return len(self.support_items)
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
        if self.mode == "support":
            return [(name, "") for name in self.support_items]
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
            self.mode = "system"
            self.selected_index = 0
            self.view.update()
            return

        if self.mode == "support":
            self.mode = "system"
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
                    self.rom_items = self.scan_launcher_folder(self.current_launcher, self.current_rom_folder)
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

        if self.mode == "support":
            self.activate_support_item(self.support_items[self.selected_index])
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
                process = launch_external_process(f'"{emulator_path}"', str(Path(emulator_path).parent))
                self.begin_active_session(process, "emulator", emulator_name, "")
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
                self.rom_items = self.scan_launcher_folder(self.current_launcher, self.current_rom_folder)
                self.selected_index = 0
                self.view.scroll_offset = 0
                self.view.update()
                return

            try:
                process = launch_rom(self.current_launcher, selected.path)
                self.begin_active_session(
                    process,
                    "game",
                    selected.display_name,
                    self.current_launcher.emulator_name or self.current_launcher.path.stem,
                )
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
            process = launch_rom(launcher, rom)
            self.begin_active_session(
                process,
                "game",
                self.display_name_for_rom(launcher, rom),
                launcher.emulator_name or launcher.path.stem,
            )
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
            self.rom_items = self.scan_launcher_folder(self.current_launcher, self.current_rom_folder)
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
        elif item == "Wallpapers":
            self.mode = "wallpaper"
            self.selected_index = 0
            self.view.update()
        elif item == "Settings":
            self.update_settings_items()
            self.mode = "settings"
            self.selected_index = 0
            self.view.update()
        elif item == "Report Issues & Requests":
            webbrowser.open("https://github.com/Anime0t4ku/Gentleman/issues/new/choose")
        elif item == "Support the Project":
            self.mode = "support"
            self.selected_index = 0
            self.view.update()
        elif item == "Check for Updates":
            self.check_for_updates_manual()
        elif item == "About":
            self.mode = "about"
            self.selected_index = 0
            self.view.update()
        elif item == "Exit":
            QApplication.quit()


    def check_for_updates_on_startup(self):
        if self.startup_update_check_done:
            return

        self.startup_update_check_done = True
        self.start_update_check(show_no_update=False, show_errors=False)

    def check_for_updates_manual(self):
        self.start_update_check(show_no_update=True, show_errors=True)

    def start_update_check(self, show_no_update: bool, show_errors: bool):
        if self.update_check_worker is not None and self.update_check_worker.isRunning():
            return

        self.update_check_worker = UpdateCheckWorker()
        self.update_check_worker.show_no_update = show_no_update
        self.update_check_worker.show_errors = show_errors
        self.update_check_worker.result.connect(self.on_update_check_result)
        self.update_check_worker.error.connect(self.on_update_check_error)
        self.update_check_worker.finished.connect(self.on_update_check_finished)
        self.update_check_worker.start()

    def on_update_check_result(self, info):
        show_no_update = getattr(self.update_check_worker, "show_no_update", True)

        if info.update_available:
            if gentleman_updater_available():
                while True:
                    dialog = UpdateAvailableDialog(
                        info,
                        "Run Gentleman-Updater",
                        "Do you want to run Gentleman-Updater now?",
                        self,
                    )
                    dialog.exec()

                    if dialog.selected_action == UpdateAvailableDialog.ACTION_SHOW_CHANGELOG:
                        self.show_update_changelog(info)
                        continue

                    if dialog.selected_action == UpdateAvailableDialog.ACTION_UPDATE:
                        if launch_gentleman_updater():
                            QApplication.quit()
                        else:
                            QMessageBox.warning(
                                self,
                                "Updater Failed",
                                "Gentleman-Updater could not be started.",
                            )

                    break

                return

            while True:
                dialog = UpdateAvailableDialog(
                    info,
                    "Open Download Page",
                    "Do you want to open the download page?",
                    self,
                )
                dialog.exec()

                if dialog.selected_action == UpdateAvailableDialog.ACTION_SHOW_CHANGELOG:
                    self.show_update_changelog(info)
                    continue

                if dialog.selected_action == UpdateAvailableDialog.ACTION_UPDATE:
                    open_release_page(info.release_url)

                break
        elif show_no_update:
            QMessageBox.information(
                self,
                "No Update Available",
                (
                    "You are already running the latest version.\n\n"
                    f"Current version: {info.current_version}"
                ),
            )

    def show_update_changelog(self, info):
        release_body = getattr(info, "release_body", "") or ""

        if not release_body.strip():
            open_release_page(info.release_url)
            return

        dialog = ChangelogDialog(info.release_name, release_body, self)
        dialog.exec()

    def on_update_check_error(self, message: str):
        show_errors = getattr(self.update_check_worker, "show_errors", True)

        if show_errors:
            QMessageBox.warning(
                self,
                "Update Check Failed",
                f"Unable to check for updates.\n\n{message}",
            )

    def on_update_check_finished(self):
        self.update_check_worker = None

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

    def refresh_settings_menu(self):
        self.save_settings()
        self.update_settings_items()
        self.view.update()

    def activate_settings_item(self, item: str):
        if item.startswith("Fullscreen at Launch:"):
            self.settings["fullscreen_at_launch"] = not self.settings.get("fullscreen_at_launch", False)
            self.refresh_settings_menu()
        elif item.startswith("Emulators Menu:"):
            self.settings["show_emulators_menu"] = not self.emulators_menu_enabled()
            self.refresh_settings_menu()
        elif item.startswith("Recent Menu:"):
            self.settings["show_recent_menu"] = not self.recent_menu_enabled()
            self.refresh_settings_menu()
        elif item.startswith("Favorites Menu:"):
            self.settings["show_favorites_menu"] = not self.favorites_menu_enabled()
            self.refresh_settings_menu()
        elif item.startswith("Logo:"):
            self.settings["show_logo"] = not self.settings.get("show_logo", True)
            self.refresh_settings_menu()
        elif item.startswith("Arcade ROM Names:"):
            self.settings["normalize_arcade_names"] = not self.arcade_name_normalization_enabled()
            if self.current_launcher and self.current_rom_folder:
                self.rom_items = self.scan_launcher_folder(self.current_launcher, self.current_rom_folder)
            self.refresh_settings_menu()
        elif item == "Clear Recent":
            self.clear_recent_items()
        elif item == "Clear Favorites":
            self.clear_favorite_items()
        elif item.startswith("Update Check at Launch:"):
            self.settings["check_updates_at_launch"] = not self.settings.get("check_updates_at_launch", True)
            self.refresh_settings_menu()
        elif item.startswith("In-Game OSD:"):
            self.settings["ingame_osd_enabled"] = not self.ingame_osd_enabled()
            self.osd_shortcut_started_ms = 0
            self.osd_shortcut_latched = False
            self.keyboard_osd_latched = False
            self.refresh_settings_menu()
        elif item.startswith("API:"):
            self.settings["api_enabled"] = not self.settings.get("api_enabled", False)
            self.save_settings()
            self.apply_remote_api_state()
            self.update_settings_items()
            self.view.update()
        elif item.startswith("Swap A/B:"):
            self.settings["swap_controller_ab"] = not self.settings.get("swap_controller_ab", False)
            self.refresh_settings_menu()
        elif item.startswith("Swap X/Y:"):
            self.settings["swap_controller_xy"] = not self.settings.get("swap_controller_xy", False)
            self.refresh_settings_menu()

    def activate_wallpaper_item(self, item: str):
        if item == "Set Wallpaper":
            self.set_wallpaper()
        elif item == "Clear Wallpaper":
            self.settings["wallpaper"] = ""
            self.save_settings()
            self.view.reload_wallpaper()
            self.view.update()

    def activate_support_item(self, item: str):
        if item == "Ko-fi":
            webbrowser.open("https://ko-fi.com/anime0t4ku")
        elif item == "Buy Me a Coffee":
            webbrowser.open("https://buymeacoffee.com/anime0t4ku")

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

    def display_name_for_rom(self, config: LauncherConfig, rom: Path) -> str:
        if self.settings.get("normalize_arcade_names", True) and config.system.strip().lower() == "arcade":
            normalized = self.arcade_name_database.display_name(rom.name)
            if normalized:
                return normalized
        return rom.stem

    def begin_active_session(self, process, session_type: str, name: str, emulator: str = ""):
        with self.active_session_lock:
            self.active_process = process
            self.active_session = {
                "running": True,
                "type": session_type,
                "name": name,
                "emulator": emulator or None,
                "pid": getattr(process, "pid", None),
            }
        self.suspend_frontend_input_for_launch()

    def active_session_snapshot(self) -> dict:
        with self.active_session_lock:
            return dict(self.active_session)

    def clear_active_session(self):
        self.resume_active_session_processes()
        with self.active_session_lock:
            self.active_process = None
            self.active_session = {"running": False, "type": None, "name": None, "emulator": None}
        self.resume_frontend_input_after_launch()

    def check_active_process(self):
        self.poll_keyboard_osd_shortcut()
        process = self.active_process
        if process is None:
            return
        try:
            ended = process.poll() is not None
        except Exception:
            ended = False
        if ended:
            if self.ingame_osd.isVisible():
                self.ingame_osd.hide()
            self.clear_active_session()

    def suspend_active_session_processes(self):
        self.suspended_session_processes = []
        if psutil is None:
            return
        session = self.active_session_snapshot()
        pid = session.get("pid")
        if not pid:
            return
        try:
            root = psutil.Process(int(pid))
            processes = root.children(recursive=True) + [root]
            for process in reversed(processes):
                try:
                    process.suspend()
                    self.suspended_session_processes.append(process)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
        except (psutil.NoSuchProcess, psutil.AccessDenied, ValueError):
            self.suspended_session_processes = []

    def resume_active_session_processes(self):
        processes = self.suspended_session_processes
        self.suspended_session_processes = []
        for process in reversed(processes):
            try:
                process.resume()
            except Exception:
                continue

    def close_active_session(self, force: bool = False) -> dict:
        session = self.active_session_snapshot()
        if not session.get("running"):
            return {"ok": False, "error": "no_active_session", "session": session}

        self.resume_active_session_processes()
        process = self.active_process
        pid = session.get("pid")
        try:
            if os.name == "nt" and pid:
                command = ["taskkill", "/PID", str(pid), "/T"]
                if force:
                    command.append("/F")
                completed = subprocess.run(command, capture_output=True, text=True, timeout=10)
                if completed.returncode != 0 and process is not None:
                    if force:
                        process.kill()
                    else:
                        process.terminate()
            elif process is not None:
                if force:
                    process.kill()
                else:
                    process.terminate()
            else:
                return {"ok": False, "error": "process_unavailable", "session": session}
        except Exception as exc:
            return {"ok": False, "error": str(exc), "session": session}

        if force:
            self.clear_active_session()
        return {"ok": True, "action": "force_close" if force else "close", "session": self.active_session_snapshot()}

    def is_dolphin_session(self) -> bool:
        session = self.active_session_snapshot()
        emulator = str(session.get("emulator") or "").strip().lower()
        return "dolphin" in emulator

    def foreground_session_window_is_fullscreen(self) -> bool:
        if os.name != "nt":
            return False
        try:
            user32 = ctypes.windll.user32
            hwnd = user32.GetForegroundWindow()
            if not hwnd:
                return False

            window_pid = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(window_pid))
            session_pid = self.active_session_snapshot().get("pid")
            if not session_pid:
                return False

            valid_pids = {int(session_pid)}
            if psutil is not None:
                try:
                    root = psutil.Process(int(session_pid))
                    valid_pids.update(child.pid for child in root.children(recursive=True))
                except Exception:
                    pass
            if int(window_pid.value) not in valid_pids:
                return False

            class Rect(ctypes.Structure):
                _fields_ = [
                    ("left", ctypes.c_long),
                    ("top", ctypes.c_long),
                    ("right", ctypes.c_long),
                    ("bottom", ctypes.c_long),
                ]

            class MonitorInfo(ctypes.Structure):
                _fields_ = [
                    ("cbSize", ctypes.c_ulong),
                    ("rcMonitor", Rect),
                    ("rcWork", Rect),
                    ("dwFlags", ctypes.c_ulong),
                ]

            window_rect = Rect()
            if not user32.GetWindowRect(hwnd, ctypes.byref(window_rect)):
                return False
            monitor = user32.MonitorFromWindow(hwnd, 2)
            if not monitor:
                return False
            monitor_info = MonitorInfo()
            monitor_info.cbSize = ctypes.sizeof(MonitorInfo)
            if not user32.GetMonitorInfoW(monitor, ctypes.byref(monitor_info)):
                return False

            tolerance = 2
            screen = monitor_info.rcMonitor
            return (
                abs(window_rect.left - screen.left) <= tolerance
                and abs(window_rect.top - screen.top) <= tolerance
                and abs(window_rect.right - screen.right) <= tolerance
                and abs(window_rect.bottom - screen.bottom) <= tolerance
            )
        except Exception:
            return False

    def send_alt_enter(self):
        if os.name != "nt":
            return
        try:
            user32 = ctypes.windll.user32
            user32.keybd_event(0x12, 0, 0, 0)
            user32.keybd_event(0x0D, 0, 0, 0)
            user32.keybd_event(0x0D, 0, 2, 0)
            user32.keybd_event(0x12, 0, 2, 0)
        except Exception:
            pass

    def finish_show_ingame_osd(self):
        if not self.active_session_snapshot().get("running") or self.ingame_osd.isVisible():
            return
        self.ingame_osd.show_osd()

    def show_ingame_osd(self):
        if not self.ingame_osd_enabled():
            return
        if not self.active_session_snapshot().get("running") or self.ingame_osd.isVisible():
            return

        if self.is_dolphin_session():
            self.dolphin_osd_fullscreen_toggled = self.foreground_session_window_is_fullscreen()
            if self.dolphin_osd_fullscreen_toggled:
                self.send_alt_enter()
                QTimer.singleShot(450, self.finish_show_ingame_osd)
            else:
                self.finish_show_ingame_osd()
            return

        self.suspend_active_session_processes()
        self.ingame_osd.show_osd()

    def hide_ingame_osd(self, resume: bool):
        self.ingame_osd.hide()
        dolphin_session = self.is_dolphin_session()
        if resume:
            self.resume_active_session_processes()
        else:
            self.suspended_session_processes = []

        if dolphin_session:
            should_restore_fullscreen = (
                resume
                and self.dolphin_osd_fullscreen_toggled
                and self.active_session_snapshot().get("running")
            )
            self.dolphin_osd_fullscreen_toggled = False
            if should_restore_fullscreen:
                QTimer.singleShot(200, self.send_alt_enter)
            return

        if resume and self.active_session_snapshot().get("running"):
            try:
                if os.name == "nt":
                    ctypes.windll.user32.keybd_event(0x12, 0, 0, 0)
                    ctypes.windll.user32.keybd_event(0x09, 0, 0, 0)
                    ctypes.windll.user32.keybd_event(0x09, 0, 2, 0)
                    ctypes.windll.user32.keybd_event(0x12, 0, 2, 0)
            except Exception:
                pass

    def poll_keyboard_osd_shortcut(self):
        if not self.ingame_osd_enabled():
            self.keyboard_osd_latched = False
            return
        if os.name != "nt" or not self.active_session_snapshot().get("running"):
            self.keyboard_osd_latched = False
            return
        user32 = ctypes.windll.user32
        pressed = bool(user32.GetAsyncKeyState(0x11) & 0x8000) and bool(user32.GetAsyncKeyState(0x12) & 0x8000) and bool(user32.GetAsyncKeyState(ord("G")) & 0x8000)
        if pressed and not self.keyboard_osd_latched:
            self.keyboard_osd_latched = True
            self.show_ingame_osd()
        elif not pressed:
            self.keyboard_osd_latched = False

    def controller_osd_shortcut_pressed(self) -> bool:
        if self.controller is None:
            return False
        try:
            buttons = self.controller.get_numbuttons()
            axes = self.controller.get_numaxes()
            l1 = buttons > 4 and bool(self.controller.get_button(4))
            r1 = buttons > 5 and bool(self.controller.get_button(5))
            l3 = buttons > 8 and bool(self.controller.get_button(8))
            r3 = buttons > 9 and bool(self.controller.get_button(9))
            l2 = (axes > 2 and self.controller.get_axis(2) > 0.5) or (axes > 4 and self.controller.get_axis(4) > 0.5)
            r2 = axes > 5 and self.controller.get_axis(5) > 0.5
            return l1 and l2 and l3 and r1 and r2 and r3
        except Exception:
            return False

    def poll_ingame_osd_shortcuts(self):
        if not self.ingame_osd_enabled():
            self.osd_shortcut_started_ms = 0
            self.osd_shortcut_latched = False
            self.keyboard_osd_latched = False
            return
        self.poll_keyboard_osd_shortcut()
        if not self.active_session_snapshot().get("running"):
            self.osd_shortcut_started_ms = 0
            self.osd_shortcut_latched = False
            return
        pressed = self.controller_osd_shortcut_pressed()
        now = int(time.monotonic() * 1000)
        if pressed:
            if not self.osd_shortcut_started_ms:
                self.osd_shortcut_started_ms = now
            elif now - self.osd_shortcut_started_ms >= 1000 and not self.osd_shortcut_latched:
                self.osd_shortcut_latched = True
                self.show_ingame_osd()
        else:
            self.osd_shortcut_started_ms = 0
            self.osd_shortcut_latched = False

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
        if event.type() == QEvent.Type.ActivationChange and self.isActiveWindow() and not self.active_session_snapshot().get("running"):
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
            self.poll_ingame_osd_shortcuts()
            if not self.ingame_osd.isVisible():
                self.controller_button_state.clear()
            self.controller_repeat_action = None
            self.controller_repeat_next_ms = 0
            if self.ingame_osd.isVisible():
                try:
                    hat_y = self.controller.get_hat(0)[1] if self.controller.get_numhats() > 0 else 0
                    axis_y = self.controller.get_axis(1) if self.controller.get_numaxes() > 1 else 0
                    up = hat_y > 0 or axis_y < -0.5
                    down = hat_y < 0 or axis_y > 0.5
                    accept = self.controller.get_button(0) if self.controller.get_numbuttons() > 0 else False
                    back = self.controller.get_button(1) if self.controller.get_numbuttons() > 1 else False
                    for key, pressed, callback in (
                        ("osd_up", up, lambda: self.ingame_osd.move_selection(-1)),
                        ("osd_down", down, lambda: self.ingame_osd.move_selection(1)),
                        ("osd_accept", accept, self.ingame_osd.activate_selected),
                        ("osd_back", back, lambda: self.hide_ingame_osd(resume=True)),
                    ):
                        previous = self.controller_button_state.get(key, False)
                        if pressed and not previous:
                            callback()
                        self.controller_button_state[key] = bool(pressed)
                except Exception:
                    pass
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
        if self.update_check_worker is not None and self.update_check_worker.isRunning():
            self.update_check_worker.quit()
            self.update_check_worker.wait(1000)
            self.update_check_worker = None

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
        reserved_height = 114 if self.window.mode == "system" else 30
        return max(1, (panel_h - reserved_height) // 28)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        painter.fillRect(self.rect(), self.bg)

        if not self.wallpaper.isNull():
            target_size = self.size()
            target_size.setWidth(target_size.width() + 2)
            target_size.setHeight(target_size.height() + 2)
            scaled = self.wallpaper.scaled(
                target_size,
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

        if self.window.mode == "system":
            painter.setPen(self.text)
            painter.drawText(text_x, row_y, f"IP: {self.window.local_ip_address}")
            painter.drawText(text_x, row_y + 28, f"Version: {APP_VERSION}")
            row_y += 84

        if not labels:
            painter.setPen(self.text)
            painter.drawText(text_x, row_y, "No entries")
            return

        for row, idx in enumerate(range(start, end)):
            label, marker = labels[idx]
            support_gap = 14 if self.window.mode == "system" and idx >= 6 else 0
            yy = row_y + row * 28 + support_gap

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
