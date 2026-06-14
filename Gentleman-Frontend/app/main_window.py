from __future__ import annotations

import json
import math
import ctypes
import threading
import os
import platform
import shutil
import socket
import subprocess
import sys
import time
import webbrowser
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote

from PyQt6.QtCore import Qt, QTimer, QRect, QRectF, QEvent, QThread, pyqtSignal
from PyQt6.QtGui import QFont, QKeyEvent, QPainter, QColor, QPen, QPixmap, QIcon, QImage, QMovie
from PyQt6.QtSvg import QSvgRenderer
from PyQt6.QtWidgets import (
    QApplication,
    QMainWindow,
    QWidget,
)

from app.app_info import APP_NAME, APP_VERSION, ABOUT_LINES
from app.zaparoo_systems import ZAPAROO_SYSTEM_NAMES
from core.arcade_names import ArcadeNameDatabase
from core.launcher import load_launcher, scan_rom_folder, launch_rom, launch_external_process, launch_link_shortcut, LauncherConfig, RomBrowserItem
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


class RomFolderScanWorker(QThread):
    result = pyqtSignal(object)

    def __init__(self, request_id: int, cache_key: tuple, signature: tuple | None, launcher: LauncherConfig, folder: Path, arcade_names: dict[str, str] | None):
        super().__init__()
        self.request_id = request_id
        self.cache_key = cache_key
        self.signature = signature
        self.launcher = launcher
        self.folder = folder
        self.arcade_names = arcade_names

    def run(self):
        try:
            items = scan_rom_folder(self.launcher, self.folder, self.arcade_names)
            self.result.emit({
                "request_id": self.request_id,
                "cache_key": self.cache_key,
                "signature": self.signature,
                "folder": self.folder,
                "items": items,
                "error": "",
            })
        except Exception as exc:
            self.result.emit({
                "request_id": self.request_id,
                "cache_key": self.cache_key,
                "signature": self.signature,
                "folder": self.folder,
                "items": [],
                "error": str(exc),
            })


def resource_path(relative_path: str) -> Path:
    base_path = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
    return base_path / relative_path


class InGameOsd(QWidget):
    def __init__(self, window):
        super().__init__(None)
        self.window = window
        self.selected_index = 0
        self.options = []
        self.confirmation_active = False
        self.confirmation_selected = 0
        self.confirmation_title = ""
        self.confirmation_message = ""
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
            self.window.show_osd_confirmation(
                "Force Close",
                "Force closing may interrupt save data or emulator writes.\n\nContinue?",
            )

    def controller_accept(self):
        if self.confirmation_active:
            if self.confirmation_selected == 1:
                self.window.close_active_session(force=True); self.close_confirmation(); self.window.hide_ingame_osd(resume=False)
            else: self.close_confirmation()
        else: self.activate_selected()

    def controller_back(self):
        if self.confirmation_active: self.close_confirmation()
        else: self.window.hide_ingame_osd(resume=True)

    def controller_horizontal(self):
        if self.confirmation_active:
            self.confirmation_selected = 1 - self.confirmation_selected; self.update()

    def show_confirmation(self, title: str, message: str):
        self.confirmation_active = True
        self.confirmation_selected = 0
        self.confirmation_title = title
        self.confirmation_message = message
        self.update()

    def close_confirmation(self):
        self.confirmation_active = False
        self.confirmation_selected = 0
        self.update()

    def keyPressEvent(self, event):
        key = event.key()
        if self.confirmation_active:
            if key in (Qt.Key.Key_Left, Qt.Key.Key_Right, Qt.Key.Key_A, Qt.Key.Key_D):
                self.confirmation_selected = 1 - self.confirmation_selected
                self.update()
            elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
                if self.confirmation_selected == 1:
                    self.window.close_active_session(force=True)
                    self.close_confirmation()
                    self.window.hide_ingame_osd(resume=False)
                else:
                    self.close_confirmation()
            elif key in (Qt.Key.Key_Escape, Qt.Key.Key_Backspace):
                self.close_confirmation()
            event.accept()
            return
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

        if self.confirmation_active:
            box_width = min(700, panel.width() - 40)
            message_width = box_width - 64
            painter.setFont(self.title_font)
            title_height = painter.fontMetrics().height()
            painter.setFont(self.font)
            message_bounds = painter.boundingRect(
                QRect(0, 0, message_width, max(120, panel.height())),
                Qt.TextFlag.TextWordWrap | Qt.AlignmentFlag.AlignHCenter,
                self.confirmation_message,
            )
            message_height = max(painter.fontMetrics().height(), message_bounds.height())
            guide_height = painter.fontMetrics().height()
            box_height = 20 + title_height + 18 + message_height + 18 + 38 + 12 + guide_height + 16
            box_height = min(max(280, box_height), panel.height() - 24)
            box = QRect(
                panel.center().x() - box_width // 2,
                panel.center().y() - box_height // 2,
                box_width,
                box_height,
            )
            painter.fillRect(box, QColor(35, 0, 10, 252))
            painter.setPen(self.light)
            painter.drawRect(box)

            painter.setFont(self.title_font)
            painter.setPen(self.text)
            title_rect = QRect(box.x() + 24, box.y() + 16, box.width() - 48, title_height + 4)
            painter.drawText(title_rect, Qt.AlignmentFlag.AlignCenter, self.confirmation_title)

            painter.setFont(self.font)
            message_top = title_rect.bottom() + 14
            guide_rect = QRect(box.x() + 16, box.bottom() - guide_height - 10, box.width() - 32, guide_height)
            button_y = guide_rect.y() - 50
            message_rect = QRect(box.x() + 32, message_top, box.width() - 64, max(40, button_y - message_top - 14))
            painter.drawText(message_rect, Qt.TextFlag.TextWordWrap | Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter, self.confirmation_message)

            labels = ["No", "Yes"]
            button_w = min(170, (box.width() - 90) // 2)
            gap = 22
            total_width = button_w * 2 + gap
            start_x = box.center().x() - total_width // 2
            for i, label in enumerate(labels):
                r = QRect(start_x + i * (button_w + gap), button_y, button_w, 36)
                if i == self.confirmation_selected:
                    painter.fillRect(r, self.light)
                    painter.setPen(self.dark_text)
                else:
                    painter.setPen(self.text)
                    painter.drawRect(r)
                painter.drawText(r, Qt.AlignmentFlag.AlignCenter, label)

            painter.setPen(self.text)
            painter.drawText(guide_rect, Qt.AlignmentFlag.AlignCenter, "D-pad Select   A Confirm   B Back")


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
        self.folder_picker_items: list[dict] = []
        self.folder_picker_current_folder = self.menu_root
        self.item_options_items: list[str] = []
        self.item_options_target: MenuItem | None = None
        self.item_options_return_mode = "menu"
        self.item_options_return_index = 0
        self.mode = "menu"
        self.overlay = None
        self.overlay_return_mode = None
        self.overlay_return_index = 0
        self.overlay_return_scroll_offset = 0
        self.text_input_value = ""
        self.text_input_cursor = 0
        self.text_input_shift = False
        self.text_input_caps = False
        self.text_input_symbols = False
        self.text_input_keys = []
        self.file_browser_path = None
        self.file_browser_items = []
        self.file_browser_extensions = []
        self.file_browser_select_folder = False
        self.file_browser_callback = None

        self.menu_items: list[MenuItem] = []
        self.rom_items: list[RomBrowserItem] = []
        self.rom_labels: list[tuple[str, str]] = []
        self.rom_folder_cache: dict[tuple, dict] = {}
        self.rom_scan_worker: RomFolderScanWorker | None = None
        self.retired_rom_scan_workers: list[RomFolderScanWorker] = []
        self.rom_scan_request_id = 0
        self.rom_loading = False
        self.emulator_items: list[str] = []
        self.emulator_launchers: dict[str, list[MenuItem]] = {}
        self.emulator_paths: dict[str, str] = {}
        self.current_emulator: str | None = None
        self.game_system_items: list[str] = []
        self.game_system_launchers: dict[str, list[MenuItem]] = {}
        self.game_system_games: dict[str, list[dict]] = {}
        self.current_game_system: str | None = None
        self.recent_items: list[dict] = []
        self.favorite_items: list[dict] = []
        self.favorite_item_keys: set[tuple[str, str]] = set()
        self.system_items = []
        self.update_system_items()
        self.settings_items = []
        self.settings_category = None
        self.update_settings_items()
        self.wallpaper_items = []
        self.update_wallpaper_items()
        self.support_items = [
            "Ko-fi",
            "Buy Me a Coffee",
        ]
        self.search_items: list[dict] = []
        self.search_query = ""
        self.search_scope_label = ""
        self.search_return_state: dict = {}

        self.current_launcher: LauncherConfig | None = None
        self.current_rom_folder: Path | None = None
        self.selected_index = 0
        self.active_input = "keyboard"
        self.idle_menu_hidden = False
        self.last_idle_activity_time = time.monotonic()
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
        self.cached_network_icon = "lan" if self.local_ip_address != "Not connected" else "wifi"

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

        self.idle_menu_timer = QTimer(self)
        self.idle_menu_timer.timeout.connect(self.check_idle_menu_hide)
        self.idle_menu_timer.start(250)

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
        icon = "lan" if address != "Not connected" else "wifi"
        if address != self.local_ip_address or icon != self.cached_network_icon:
            self.local_ip_address = address
            self.cached_network_icon = icon
            self.view.update()

    def default_settings(self) -> dict:
        return {
            "app_name": APP_NAME,
            "app_version": APP_VERSION,
            "wallpaper": "",
            "wallpaper_folder": "",
            "fullscreen_at_launch": False,
            "show_emulators_menu": True,
            "show_systems_menu": False,
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
            "fullscreen_menu_size": 100,
            "menu_idle_hide_timeout": 0,
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
        if launcher.launcher_type == "shortcut_folder":
            return None
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

    def rom_folder_signature(self, folder: Path) -> tuple | None:
        try:
            stat = folder.stat()
            return (getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1_000_000_000)), stat.st_size)
        except OSError:
            return None

    def rom_cache_key(self, launcher: LauncherConfig, folder: Path) -> tuple:
        try:
            launcher_path = str(launcher.path.resolve()).replace(chr(92), "/").lower()
        except Exception:
            launcher_path = str(launcher.path).replace(chr(92), "/").lower()

        try:
            folder_path = str(folder.resolve()).replace(chr(92), "/").lower()
        except Exception:
            folder_path = str(folder).replace(chr(92), "/").lower()

        return (
            launcher_path,
            folder_path,
            tuple(sorted(ext.lower() for ext in launcher.extensions)),
            bool(self.arcade_name_normalization_enabled()),
            launcher.system.strip().lower(),
        )

    def set_rom_items(self, items: list[RomBrowserItem]):
        self.rom_items = items
        self.rom_labels = [("...", "<DIR>")] + [(item.display_name, item.marker) for item in items]
        self.rom_loading = False

    def clear_rom_items(self):
        self.rom_items = []
        self.rom_labels = []
        self.rom_loading = False

    def show_rom_loading(self):
        self.rom_items = []
        self.rom_labels = [("Loading folder...", "")]
        self.rom_loading = True

    def open_rom_folder(self, launcher: LauncherConfig, folder: Path, reset_selection: bool = True):
        self.current_launcher = launcher
        self.current_rom_folder = folder
        self.mode = "roms"

        cache_key = self.rom_cache_key(launcher, folder)
        signature = self.rom_folder_signature(folder)
        cached = self.rom_folder_cache.get(cache_key)

        if cached and cached.get("signature") == signature:
            self.set_rom_items(cached.get("items", []))
            if reset_selection:
                self.reset_selection_to_first_real_entry()
            self.view.update()
            return

        self.rom_scan_request_id += 1
        request_id = self.rom_scan_request_id

        if self.rom_scan_worker is not None and self.rom_scan_worker.isRunning():
            old_worker = self.rom_scan_worker
            try:
                old_worker.result.disconnect()
            except TypeError:
                pass
            try:
                old_worker.finished.disconnect()
            except TypeError:
                pass
            old_worker.finished.connect(lambda worker=old_worker: self.cleanup_retired_rom_scan_worker(worker))
            self.retired_rom_scan_workers.append(old_worker)

        self.show_rom_loading()
        if reset_selection:
            self.selected_index = 0
            self.view.scroll_offset = 0
        self.view.update()

        self.rom_scan_worker = RomFolderScanWorker(
            request_id,
            cache_key,
            signature,
            launcher,
            folder,
            self.arcade_names_for_launcher(launcher),
        )
        self.rom_scan_worker.result.connect(self.on_rom_folder_scan_result)
        self.rom_scan_worker.finished.connect(self.on_rom_folder_scan_finished)
        self.rom_scan_worker.start()

    def on_rom_folder_scan_result(self, result: object):
        if not isinstance(result, dict):
            return

        if result.get("request_id") != self.rom_scan_request_id:
            return

        folder = result.get("folder")
        if self.mode != "roms" or self.current_rom_folder is None or Path(folder) != self.current_rom_folder:
            return

        items = result.get("items", [])
        if not isinstance(items, list):
            items = []

        cache_key = result.get("cache_key")
        if isinstance(cache_key, tuple):
            self.rom_folder_cache[cache_key] = {
                "signature": result.get("signature"),
                "items": items,
                "scanned_at": time.monotonic(),
            }
            while len(self.rom_folder_cache) > 32:
                self.rom_folder_cache.pop(next(iter(self.rom_folder_cache)))

        self.set_rom_items(items)
        self.reset_selection_to_first_real_entry()
        self.view.update()

        error = str(result.get("error", "")).strip()
        if error:
            self.show_message("ROM Browser", error)

    def on_rom_folder_scan_finished(self):
        self.rom_scan_worker = None

    def cleanup_retired_rom_scan_worker(self, worker: RomFolderScanWorker):
        if worker in self.retired_rom_scan_workers:
            self.retired_rom_scan_workers.remove(worker)

    def invalidate_rom_folder_cache(self):
        self.rom_folder_cache.clear()

    def emulators_menu_enabled(self) -> bool:
        return bool(self.settings.get("show_emulators_menu", True))

    def systems_menu_enabled(self) -> bool:
        return bool(self.settings.get("show_systems_menu", False))

    def recent_menu_enabled(self) -> bool:
        return bool(self.settings.get("show_recent_menu", True))

    def favorites_menu_enabled(self) -> bool:
        return bool(self.settings.get("show_favorites_menu", True))

    def item_identity_key(self, item: dict) -> tuple[str, str]:
        launcher = str(item.get("launcher", "")).replace(chr(92), "/").strip().lower()
        rom = str(item.get("rom", "")).replace(chr(92), "/").strip().lower()

        try:
            rom = str(Path(rom).resolve()).replace(chr(92), "/").lower()
        except Exception:
            pass

        return launcher, rom

    def dedupe_game_items(self, items: list[dict]) -> list[dict]:
        deduped: list[dict] = []
        seen: set[tuple[str, str]] = set()

        for item in items:
            if not isinstance(item, dict):
                continue

            key = self.item_identity_key(item)
            if key in seen:
                continue

            seen.add(key)
            deduped.append(item)

        return deduped

    def display_name_for_saved_game_item(self, item: dict) -> str:
        launcher_rel = str(item.get("launcher", ""))
        rom = Path(str(item.get("rom", "")))

        try:
            launcher_path = self.menu_root / launcher_rel
            if launcher_path.exists():
                launcher = load_launcher(launcher_path)
                return self.display_name_for_rom(launcher, rom)
        except Exception:
            pass

        stored_name = str(item.get("name", "")).strip()
        if stored_name:
            return Path(stored_name).stem

        return rom.stem or "Unknown"

    def sorted_favorite_items(self, items: list[dict]) -> list[dict]:
        return sorted(
            self.dedupe_game_items(items),
            key=lambda item: self.display_name_for_saved_game_item(item).lower(),
        )

    def load_favorite_items(self) -> list[dict]:
        if not self.favorites_path.exists():
            return []

        try:
            data = json.loads(self.favorites_path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return self.sorted_favorite_items(data)
        except Exception:
            pass

        return []

    def save_favorite_items(self, items: list[dict]):
        self.favorites_path.write_text(json.dumps(self.sorted_favorite_items(items), indent=2), encoding="utf-8")

    def update_favorite_items(self):
        self.favorite_items = self.load_favorite_items()
        self.favorite_item_keys = {self.item_identity_key(item) for item in self.favorite_items}

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
            "name": self.display_name_for_rom(self.current_launcher, selected.path),
            "launcher": launcher_rel,
            "rom": str(selected.path).replace(chr(92), "/"),
        }

    def current_selection_is_favorite(self) -> bool:
        item = self.favorite_item_from_current_selection()
        if not item:
            return False

        return self.item_identity_key(item) in self.favorite_item_keys

    def toggle_current_favorite(self):
        item = self.favorite_item_from_current_selection()
        if not item:
            return

        favorites = self.load_favorite_items()
        updated = []
        removed = False
        item_key = self.item_identity_key(item)

        for favorite in favorites:
            if self.item_identity_key(favorite) == item_key:
                removed = True
                continue
            updated.append(favorite)

        if not removed:
            updated.append(item)

        self.save_favorite_items(updated)
        self.update_favorite_items()
        self.view.update()

    def load_recent_items(self) -> list[dict]:
        if not self.recent_path.exists():
            return []

        try:
            data = json.loads(self.recent_path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return self.dedupe_game_items(data)
        except Exception:
            pass

        return []

    def save_recent_items(self, items: list[dict]):
        self.recent_path.write_text(json.dumps(self.dedupe_game_items(items)[:50], indent=2), encoding="utf-8")

    def add_recent_game(self, launcher_path: Path, rom_path: Path):
        try:
            launcher_rel = str(launcher_path.relative_to(self.menu_root)).replace(chr(92), "/")
        except ValueError:
            launcher_rel = str(launcher_path).replace(chr(92), "/")

        game_path = str(rom_path).replace(chr(92), "/")
        game_name = rom_path.stem

        try:
            launcher = load_launcher(launcher_path)
            game_name = self.display_name_for_rom(launcher, rom_path)
        except Exception:
            pass

        item = {
            "name": game_name,
            "launcher": launcher_rel,
            "rom": game_path,
        }

        item_key = self.item_identity_key(item)
        items = [
            existing for existing in self.load_recent_items()
            if self.item_identity_key(existing) != item_key
        ]
        items.insert(0, item)
        self.save_recent_items(items)

    def update_recent_items(self):
        self.recent_items = self.load_recent_items()

    def update_game_system_items(self):
        systems: dict[str, list[MenuItem]] = {}

        for launcher_path in self.menu_root.rglob("*.json"):
            try:
                launcher = load_launcher(launcher_path)
            except Exception:
                continue

            system_name = launcher.system.strip()
            if not system_name:
                continue

            try:
                rel_path = launcher_path.relative_to(self.menu_root)
            except ValueError:
                rel_path = launcher_path

            launcher_item = MenuItem(rel_path.stem, launcher_path, "launcher")
            systems.setdefault(system_name, []).append(launcher_item)

        self.game_system_items = []
        self.game_system_launchers = {}

        for system_name in sorted(systems.keys(), key=str.lower):
            launchers = sorted(systems[system_name], key=lambda item: item.name.lower())
            self.game_system_items.append(system_name)
            self.game_system_launchers[system_name] = launchers

    def scan_cached_rom_folder_sync(self, launcher: LauncherConfig, folder: Path) -> list[RomBrowserItem]:
        cache_key = self.rom_cache_key(launcher, folder)
        signature = self.rom_folder_signature(folder)
        cached = self.rom_folder_cache.get(cache_key)

        if cached and cached.get("signature") == signature:
            items = cached.get("items", [])
            if isinstance(items, list):
                return items

        items = self.scan_launcher_folder(launcher, folder)
        self.rom_folder_cache[cache_key] = {
            "signature": signature,
            "items": items,
            "scanned_at": time.monotonic(),
        }
        while len(self.rom_folder_cache) > 64:
            self.rom_folder_cache.pop(next(iter(self.rom_folder_cache)))
        return items

    def collect_system_games_for_launcher(self, launcher_item: MenuItem) -> list[dict]:
        try:
            launcher = load_launcher(launcher_item.path)
        except Exception:
            return []

        if launcher.launcher_type in {"application", "shortcut"}:
            return [{
                "name": launcher.emulator_name or launcher_item.name,
                "launcher_item": launcher_item,
                "launcher": launcher,
                "rom": None,
            }]

        root = Path(launcher.rom_directory)
        if not root.is_dir():
            return []

        games: list[dict] = []
        folders_to_scan = [root]
        visited: set[str] = set()

        while folders_to_scan:
            folder = folders_to_scan.pop(0)
            try:
                folder_key = str(folder.resolve()).replace(chr(92), "/").lower()
            except Exception:
                folder_key = str(folder).replace(chr(92), "/").lower()

            if folder_key in visited:
                continue
            visited.add(folder_key)

            for rom_item in self.scan_cached_rom_folder_sync(launcher, folder):
                if rom_item.is_dir:
                    if launcher.recursive:
                        folders_to_scan.append(rom_item.path)
                    continue

                games.append({
                    "name": rom_item.display_name,
                    "launcher_item": launcher_item,
                    "launcher": launcher,
                    "rom": rom_item.path,
                })

        return games

    def update_current_system_game_items(self):
        system_name = self.current_game_system or ""
        games: list[dict] = []

        for launcher_item in self.game_system_launchers.get(system_name, []):
            games.extend(self.collect_system_games_for_launcher(launcher_item))

        deduped: list[dict] = []
        seen: set[tuple[str, str]] = set()
        for game in games:
            launcher = game.get("launcher")
            rom = game.get("rom")
            launcher_path = getattr(launcher, "path", game.get("launcher_item", MenuItem("", Path(), "launcher")).path)
            rom_path = rom if rom is not None else launcher_path
            key = (
                str(launcher_path).replace(chr(92), "/").lower(),
                str(rom_path).replace(chr(92), "/").lower(),
            )
            if key in seen:
                continue
            seen.add(key)
            deduped.append(game)

        deduped.sort(key=lambda item: str(item.get("name", "")).lower())
        self.game_system_games[system_name] = deduped

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
            self.show_message("Remote API", f"Could not start Remote API:\n{exc}")

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

    def api_launchers(self) -> dict:
        return {"launchers": self.api_all_launchers()}

    def api_systems(self) -> dict:
        systems: dict[str, str] = {}

        for launcher in self.api_all_launchers():
            system = str(launcher.get("system", "")).strip()
            if not system:
                continue
            systems.setdefault(system.lower(), system)

        return {
            "systems": [
                {"system": systems[key]}
                for key in sorted(systems.keys(), key=lambda value: systems[value].lower())
            ]
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

    def api_system_game_entries(self, system: str, launcher_filter: str = "") -> tuple[str, list[dict], list[dict]]:
        launchers = self.api_find_launchers_by_system(system)
        selected_launcher = str(launcher_filter or "").strip().replace(chr(92), "/")

        if selected_launcher:
            launchers = [
                launcher for launcher in launchers
                if str(launcher.get("path", "")).replace(chr(92), "/").lower() == selected_launcher.lower()
            ]
            if not launchers:
                raise ValueError("Launcher not found for system")

        resolved_system = str(launchers[0].get("system", system)).strip() or system
        entries: list[dict] = []
        seen: set[tuple[str, str]] = set()

        for launcher_meta in launchers:
            launcher_rel = str(launcher_meta.get("path", "")).strip().replace(chr(92), "/")
            try:
                launcher_path = self.api_safe_launcher_path(launcher_rel)
                config = load_launcher(launcher_path)
            except Exception:
                continue

            launcher_name = str(launcher_meta.get("name", "") or launcher_path.stem)

            if config.launcher_type in {"application", "shortcut"}:
                entry_name = config.emulator_name or launcher_name
                key = (launcher_rel.lower(), "")
                if key not in seen:
                    seen.add(key)
                    entries.append({
                        "name": entry_name,
                        "type": "application" if config.launcher_type == "application" else "shortcut",
                        "launcher": launcher_rel,
                        "launcher_name": launcher_name,
                        "system": resolved_system,
                        "path": "",
                        "game": "",
                    })
                continue

            root = Path(config.rom_directory)
            if not root.is_dir():
                continue

            folders_to_scan = [root]
            visited: set[str] = set()

            while folders_to_scan:
                folder = folders_to_scan.pop(0)
                try:
                    folder_key = str(folder.resolve()).replace(chr(92), "/").lower()
                except Exception:
                    folder_key = str(folder).replace(chr(92), "/").lower()

                if folder_key in visited:
                    continue
                visited.add(folder_key)

                for rom_item in self.scan_cached_rom_folder_sync(config, folder):
                    if rom_item.is_dir:
                        if config.recursive:
                            folders_to_scan.append(rom_item.path)
                        continue

                    try:
                        rel = str(rom_item.path.resolve().relative_to(root.resolve())).replace(chr(92), "/")
                    except ValueError:
                        rel = rom_item.name

                    key = (launcher_rel.lower(), rel.lower())
                    if key in seen:
                        continue
                    seen.add(key)
                    entries.append({
                        "name": rom_item.display_name,
                        "type": "game",
                        "launcher": launcher_rel,
                        "launcher_name": launcher_name,
                        "system": resolved_system,
                        "path": rel,
                        "game": rel,
                    })

        entries.sort(key=lambda item: (
            str(item.get("name", "")).lower(),
            str(item.get("launcher_name", "")).lower(),
            str(item.get("path", "")).lower(),
        ))
        return resolved_system, launchers, entries

    def api_games_by_system(self, system: str, launcher: str = "", folder: str = "") -> dict:
        resolved_system, launchers, items = self.api_system_game_entries(system, launcher)
        return {
            "system": resolved_system,
            "view": "flat",
            "items": items,
            "launchers": launchers,
        }

    def api_launch_by_system(self, payload: dict) -> dict:
        system = str(payload.get("system", "")).strip()
        launcher = str(payload.get("launcher", "")).strip()
        game = str(payload.get("game", payload.get("rom", ""))).strip().replace(chr(92), "/")

        if not launcher:
            _, _, items = self.api_system_game_entries(system)
            matches = []
            requested = game.lower()

            for item in items:
                item_path = str(item.get("path", item.get("game", ""))).strip().replace(chr(92), "/")
                item_game = str(item.get("game", item_path)).strip().replace(chr(92), "/")
                item_name = str(item.get("name", "")).strip()
                if not requested:
                    if not item_path and item.get("type") in {"application", "shortcut"}:
                        matches.append(item)
                elif requested in {item_path.lower(), item_game.lower(), item_name.lower()}:
                    matches.append(item)

            if len(matches) == 1:
                launcher = str(matches[0].get("launcher", ""))
                game = str(matches[0].get("path", matches[0].get("game", ""))).strip().replace(chr(92), "/")
            elif len(matches) > 1:
                raise ValueError("Multiple games matched for system, pass launcher and game from /api/games-by-system")
            else:
                launchers = self.api_find_launchers_by_system(system)
                if len(launchers) == 1:
                    launcher = str(launchers[0].get("path", ""))
                else:
                    raise ValueError("Game not found for system, pass launcher and game from /api/games-by-system")

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

    def launch_application_config(self, config: LauncherConfig):
        command = f'"{config.emulator}" {config.arguments}'.strip()
        return launch_external_process(command, str(Path(config.emulator).parent))

    def launch_shortcut_path(self, path: Path):
        return launch_link_shortcut(path)

    def launch_shortcut_config(self, config: LauncherConfig):
        shortcut_path = Path(config.shortcut_path or config.emulator)
        return self.launch_shortcut_path(shortcut_path)

    def api_launch(self, payload: dict) -> dict:
        launcher = str(payload.get("launcher", ""))
        game = str(payload.get("game", payload.get("rom", "")))

        launcher_path = self.api_safe_launcher_path(launcher)
        config = load_launcher(launcher_path)

        if config.launcher_type == "application":
            process = self.launch_application_config(config)
            self.begin_active_session(process, "application", config.emulator_name or launcher_path.stem, "")
            return {"ok": True, "launched": "application", "launcher": launcher, "session": self.active_session_snapshot()}

        if config.launcher_type == "shortcut":
            shortcut_path = Path(config.shortcut_path or config.emulator)
            if not shortcut_path.exists():
                raise ValueError("Shortcut file not found")
            process = self.launch_shortcut_path(shortcut_path)
            self.begin_active_session(process, "application", launcher_path.stem, config.system or "Shortcut")
            return {"ok": True, "launched": "shortcut", "launcher": launcher, "session": self.active_session_snapshot()}

        rom_path = self.api_safe_rom_path(config, game)
        display_name = self.display_name_for_rom(config, rom_path)

        if config.launcher_type == "shortcut_folder":
            process = self.launch_shortcut_path(rom_path)
            self.begin_active_session(process, "game", display_name, config.system or launcher_path.stem)
        else:
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
                self.move_selection(-10)
            elif action == "right":
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

    def idle_menu_hide_timeout_choices(self) -> list[int]:
        return [0, 10, 15, 20, 30, 45, 60]

    def idle_menu_hide_timeout_seconds(self) -> int:
        try:
            value = int(self.settings.get("menu_idle_hide_timeout", 0))
        except Exception:
            value = 0
        return value if value in self.idle_menu_hide_timeout_choices() else 0

    def idle_menu_hide_label(self, value: int | None = None) -> str:
        timeout = self.idle_menu_hide_timeout_seconds() if value is None else int(value)
        if timeout <= 0:
            return "Auto Hide Menu: Disabled"
        unit = "sec" if timeout < 60 else "min"
        amount = timeout if timeout < 60 else 1
        return f"Auto Hide Menu: {amount} {unit}"

    def idle_menu_hide_allowed(self) -> bool:
        return (
            self.idle_menu_hide_timeout_seconds() > 0
            and self.mode == "menu"
            and self.overlay is None
            and not self.input_suspended_for_launch
            and not self.ingame_osd.isVisible()
        )

    def note_user_activity(self) -> bool:
        self.last_idle_activity_time = time.monotonic()
        if self.idle_menu_hidden:
            self.idle_menu_hidden = False
            self.view.update()
            return True
        return False

    def check_idle_menu_hide(self):
        if not self.idle_menu_hide_allowed():
            self.last_idle_activity_time = time.monotonic()
            if self.idle_menu_hidden:
                self.idle_menu_hidden = False
                self.view.update()
            return

        if self.idle_menu_hidden:
            return

        if time.monotonic() - self.last_idle_activity_time >= self.idle_menu_hide_timeout_seconds():
            self.idle_menu_hidden = True
            self.view.update()

    def settings_categories(self) -> list[str]:
        return ["Display", "Menu", "Controls", "System"]

    def update_settings_items(self):
        if self.settings_category is None:
            self.settings_items = self.settings_categories()
            return

        fullscreen_launch_label = self.setting_state_label(
            "Fullscreen at Launch",
            self.settings.get("fullscreen_at_launch", False),
        )

        emulators_menu_label = self.setting_state_label(
            "Emulators Menu",
            self.emulators_menu_enabled(),
        )

        systems_menu_label = self.setting_state_label(
            "Systems Menu",
            self.systems_menu_enabled(),
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

        menu_size_label = f"Menu Size: {int(self.settings.get('fullscreen_menu_size', 100))}%"
        idle_menu_hide_label = self.idle_menu_hide_label()

        if self.settings_category == "Display":
            self.settings_items = [
                fullscreen_launch_label,
                logo_label,
            ]
        elif self.settings_category == "Menu":
            self.settings_items = [
                menu_size_label,
                idle_menu_hide_label,
                systems_menu_label,
                emulators_menu_label,
                recent_menu_label,
                favorites_menu_label,
                arcade_names_label,
                "Clear Recent",
                "Clear Favorites",
            ]
        elif self.settings_category == "Controls":
            self.settings_items = [
                swap_ab_label,
                swap_xy_label,
                ingame_osd_label,
            ]
        elif self.settings_category == "System":
            self.settings_items = [
                api_label,
                update_check_label,
            ]
        else:
            self.settings_category = None
            self.settings_items = self.settings_categories()

    def launcher_type_values(self) -> list[str]:
        return ["Standalone Emulator", "RetroArch", "Application", "PC Game", "Shortcut", "Shortcut Folder"]

    def launcher_type_to_json(self, type_name: str) -> str:
        if type_name == "RetroArch":
            return "retroarch"
        if type_name in ("Application", "PC Game"):
            return "application"
        if type_name == "Shortcut":
            return "shortcut"
        if type_name == "Shortcut Folder":
            return "shortcut_folder"
        return "standalone"

    def launcher_type_from_json(self, type_name: str) -> str:
        if type_name == "retroarch":
            return "RetroArch"
        if type_name == "application":
            return "Application"
        if type_name in ("shortcut", "link"):
            return "Shortcut"
        if type_name in ("shortcut_folder", "link_folder"):
            return "Shortcut Folder"
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
        self.launcher_form_folders = folders

    def launcher_form_folder_label(self) -> str:
        folder = self.launcher_form_data.get("folder", "")
        return "Root Menu" if not folder else folder

    def update_launcher_form_fields(self):
        launcher_type = self.launcher_form_data.get("type", "Standalone Emulator")

        fields = [
            "Type",
            "Launcher Name",
            "Save Folder",
        ]


        if launcher_type in ("Application", "PC Game"):
            fields.extend([
                "Application Name" if launcher_type == "Application" else "Game Name",
                "App Path" if launcher_type == "Application" else "Game Path",
                "Arguments",
                "Save",
                "Cancel",
            ])
        elif launcher_type == "Shortcut":
            fields.extend([
                "System",
                "Shortcut Path",
                "Save",
                "Cancel",
            ])
        elif launcher_type == "Shortcut Folder":
            fields.extend([
                "System",
                "Shortcut Folder",
                "Save",
                "Cancel",
            ])
        else:
            if launcher_type == "Standalone Emulator":
                fields.append("Emulator Name")

            fields.extend([
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
            if "Save" in fields and "Remove Launcher" not in fields:
                fields.insert(fields.index("Save") + 1, "Remove Launcher")

        self.launcher_form_fields = fields

    def open_launcher_form(self, launcher_path: Path | None = None):
        self.launcher_form_mode = "edit" if launcher_path else "create"
        self.launcher_form_path = launcher_path
        self.rebuild_launcher_form_folders()

        if launcher_path:
            try:
                data = json.loads(launcher_path.read_text(encoding="utf-8"))
            except Exception as exc:
                self.show_message("Load failed", str(exc))
                return

            launcher_type = self.launcher_type_from_json(str(data.get("type", "standalone")))
            try:
                launcher_folder = launcher_path.parent.relative_to(self.menu_root)
                launcher_folder_value = "" if str(launcher_folder) == "." else str(launcher_folder).replace(chr(92), "/")
            except ValueError:
                launcher_folder_value = ""

            self.launcher_form_data = {
                "launcher_name": launcher_path.stem,
                "folder": launcher_folder_value,
                "emulator_name": str(data.get("emulator_name", "")),
                "system": str(data.get("system", "")),
                "type": launcher_type,
                "emulator": str(data.get("emulator", data.get("shortcut_path", data.get("link_path", "")))),
                "core": str(data.get("core", "")),
                "rom_directory": str(data.get("rom_directory", data.get("shortcut_directory", data.get("link_directory", "")))),
                "extensions": ",".join(data.get("extensions", [])),
                "arguments": str(data.get("arguments", '"{rom}"')),
            }
            if launcher_type == "RetroArch":
                self.launcher_form_data["emulator_name"] = "RetroArch"
            if launcher_type == "Application" and self.launcher_form_data.get("system") == "PC":
                launcher_type = "PC Game"
                self.launcher_form_data["type"] = "PC Game"
            if launcher_type == "Application":
                self.launcher_form_data["system"] = "Application"
            elif launcher_type == "PC Game":
                self.launcher_form_data["system"] = "PC"
        else:
            self.launcher_form_data = {
                "launcher_name": "",
                "folder": "",
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
        if type_name in ("Application", "PC Game", "Shortcut", "Shortcut Folder"):
            return ""
        return '"{rom}"'

    def set_launcher_type(self, type_name: str):
        old_type = self.launcher_form_data.get("type", "Standalone Emulator")
        old_default = self.default_arguments_for_type(old_type)
        current_args = self.launcher_form_data.get("arguments", "")

        self.launcher_form_data["type"] = type_name

        if type_name == "RetroArch":
            self.launcher_form_data["emulator_name"] = "RetroArch"
        elif old_type == "RetroArch" and self.launcher_form_data.get("emulator_name") == "RetroArch":
            self.launcher_form_data["emulator_name"] = ""

        if type_name == "Application":
            self.launcher_form_data["system"] = "Application"
        elif type_name == "PC Game":
            self.launcher_form_data["system"] = "PC"
        elif old_type == "Application" and self.launcher_form_data.get("system") == "Application":
            self.launcher_form_data["system"] = ""
        elif old_type == "PC Game" and self.launcher_form_data.get("system") == "PC":
            self.launcher_form_data["system"] = ""

        if not current_args or current_args == old_default:
            self.launcher_form_data["arguments"] = self.default_arguments_for_type(type_name)

    def launcher_form_value(self, field: str) -> str:
        if field == "Launcher Name":
            return self.launcher_form_data.get("launcher_name", "")
        if field == "Save Folder":
            return self.launcher_form_folder_label()
        if field == "Emulator Name":
            return self.launcher_form_data.get("emulator_name", "")
        if field in ("Application Name", "Game Name"):
            return self.launcher_form_data.get("emulator_name", "")
        if field == "System":
            return self.launcher_form_data.get("system", "") or "Custom / Unknown"
        if field == "Type":
            return self.launcher_form_data.get("type", "Standalone Emulator")
        if field == "Emulator Path":
            return self.launcher_form_data.get("emulator", "")
        if field in ("App Path", "Game Path"):
            return self.launcher_form_data.get("emulator", "")
        if field == "Shortcut Path":
            return self.launcher_form_data.get("emulator", "")
        if field == "Shortcut Folder":
            return self.launcher_form_data.get("rom_directory", "")
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
            return False

        field = self.launcher_form_fields[self.selected_index]

        if field != "Type":
            return False

        values = self.launcher_type_values()
        if not values:
            return False

        current = self.launcher_form_data.get("type", "Standalone Emulator")
        try:
            current_index = values.index(current)
        except ValueError:
            current_index = 0

        self.set_launcher_type(values[(current_index + delta) % len(values)])
        self.update_launcher_form_fields()
        self.selected_index = min(self.selected_index, max(0, len(self.launcher_form_fields) - 1))
        self.view.ensure_visible()
        self.view.update()
        return True

    def folder_picker_relative_label(self) -> str:
        try:
            rel = self.folder_picker_current_folder.relative_to(self.menu_root)
        except ValueError:
            return "Root Menu"
        return "Root Menu" if str(rel) == "." else str(rel).replace(chr(92), "/")

    def is_path_inside_menu_root(self, path: Path) -> bool:
        try:
            root = self.menu_root.resolve()
            resolved = path.resolve()
        except Exception:
            return False
        return resolved == root or root in resolved.parents

    def rebuild_folder_picker_items(self):
        folder = self.folder_picker_current_folder
        if not self.is_path_inside_menu_root(folder):
            folder = self.menu_root
            self.folder_picker_current_folder = folder

        entries = []
        try:
            children = sorted([child for child in folder.iterdir() if child.is_dir()], key=lambda item: item.name.lower())
        except Exception:
            children = []

        for child in children:
            entries.append({"type": "folder", "name": child.name, "path": child})

        entries.append({"type": "separator", "name": ""})
        entries.append({"type": "create", "name": "Create Folder"})
        entries.append({"type": "select", "name": "Select This Folder"})
        if folder != self.menu_root:
            entries.append({"type": "remove", "name": "Remove This Folder"})
        self.folder_picker_items = entries

    def first_selectable_folder_picker_index(self) -> int:
        for index, item in enumerate(self.folder_picker_items):
            if item.get("type") != "separator":
                return index
        return 0

    def select_this_folder_picker_index(self) -> int:
        for index, item in enumerate(self.folder_picker_items):
            if item.get("type") == "select":
                return index
        return self.first_selectable_folder_picker_index()

    def open_folder_picker(self):
        self.launcher_form_return_index = self.selected_index

        folder_value = self.launcher_form_data.get("folder", "")
        target = self.menu_root / folder_value if folder_value else self.menu_root
        self.folder_picker_current_folder = target if target.exists() and target.is_dir() and self.is_path_inside_menu_root(target) else self.menu_root

        self.rebuild_folder_picker_items()
        self.selected_index = self.first_selectable_folder_picker_index()
        self.mode = "folder_picker"
        self.view.scroll_offset = 0
        self.view.ensure_visible()
        self.view.update()

    def set_folder_picker_folder(self, folder: Path, prefer_select: bool = False):
        self.folder_picker_current_folder = folder
        self.rebuild_folder_picker_items()
        self.selected_index = self.select_this_folder_picker_index() if prefer_select else self.first_selectable_folder_picker_index()
        self.view.scroll_offset = 0
        self.view.ensure_visible()
        self.view.update()

    def create_folder_from_picker(self, folder_name: str):
        safe_name = self.launcher_form_safe_filename(folder_name.strip())
        if not folder_name.strip() or not safe_name:
            self.show_message("Missing folder name", "Enter a folder name.")
            return

        target = self.folder_picker_current_folder / safe_name
        if not self.is_path_inside_menu_root(target):
            self.show_message("Invalid folder", "Folder path is outside the menu folder.")
            return

        if target.exists() and not target.is_dir():
            self.show_message("Invalid folder", "A file with this name already exists.")
            return

        try:
            target.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            self.show_message("Create folder failed", str(exc))
            return

        self.mode = "folder_picker"
        self.set_folder_picker_folder(target, prefer_select=True)

    def confirm_remove_folder_from_picker(self):
        folder_path = self.folder_picker_current_folder
        if folder_path == self.menu_root or not folder_path.exists() or not folder_path.is_dir() or not self.is_path_inside_menu_root(folder_path):
            self.show_message("Remove failed", "This folder cannot be removed safely.")
            return

        parent_folder = folder_path.parent
        folder_name = folder_path.name

        def remove_folder():
            try:
                shutil.rmtree(folder_path)
            except Exception as exc:
                self.show_message("Remove failed", str(exc))
                return
            self.mode = "folder_picker"
            self.set_folder_picker_folder(parent_folder if self.is_path_inside_menu_root(parent_folder) else self.menu_root)

        self.show_confirmation(
            "Remove Folder",
            f"Remove folder '{folder_name}'?\n\nThis will remove the folder, all launchers, and all subfolders inside it.",
            remove_folder,
        )

    def select_folder_picker_item(self):
        if not self.folder_picker_items:
            return

        item = self.folder_picker_items[self.selected_index]
        item_type = item.get("type")

        if item_type == "separator":
            return

        if item_type == "folder":
            folder = item.get("path")
            if isinstance(folder, Path):
                self.set_folder_picker_folder(folder)
            return

        if item_type == "create":
            self.open_text_input("Create Folder", "Folder name:", "", self.create_folder_from_picker)
            return

        if item_type == "remove":
            self.confirm_remove_folder_from_picker()
            return

        if item_type == "select":
            try:
                rel = self.folder_picker_current_folder.relative_to(self.menu_root)
                folder_value = "" if str(rel) == "." else str(rel).replace(chr(92), "/")
            except ValueError:
                folder_value = ""
            self.launcher_form_data["folder"] = folder_value
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
        system_names = list(ZAPAROO_SYSTEM_NAMES)
        if "Application" not in system_names:
            system_names.append("Application")
        self.system_picker_items = ["Custom / Unknown"] + sorted(system_names, key=str.lower)
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

        if field == "Remove Launcher":
            self.confirm_remove_launcher()
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
            "Emulator Name": "emulator_name",
            "Application Name": "emulator_name",
            "Game Name": "emulator_name",
            "Extensions": "extensions",
            "Arguments": "arguments",
        }

        if field in key_map:
            key = key_map[field]
            prompt = field + ":"
            if field == "Arguments":
                launcher_type = self.launcher_form_data.get("type", "Standalone Emulator")
                if launcher_type == "Application": prompt = "Optional application arguments. Usually empty."
                elif launcher_type == "PC Game": prompt = "Optional PC game arguments. Usually empty."
                elif launcher_type == "RetroArch": prompt = 'Arguments. Use {core} and {rom}, for example: -L "{core}" "{rom}"'
                else: prompt = 'Arguments. Use {rom} for the selected game, for example: -fullscreen "{rom}"'
            self.open_text_input(field, prompt, self.launcher_form_data.get(key, ""), lambda value, k=key: self.launcher_form_data.__setitem__(k, value.strip()))
            return
        if field in ("Emulator Path", "App Path", "Game Path", "Shortcut Path"):
            if field == "App Path":
                title = "Select application"
                extensions = ['.exe']
            elif field == "Game Path":
                title = "Select PC game"
                extensions = ['.exe']
            elif field == "Shortcut Path":
                title = "Select Windows shortcut"
                extensions = ['.lnk']
            elif self.launcher_form_data.get("type") == "RetroArch":
                title = "Select RetroArch executable"
                extensions = ['.exe']
            else:
                title = "Select emulator executable"
                extensions = ['.exe']
            self.open_file_browser(title, self.base_dir, extensions, False, lambda value: self.launcher_form_data.__setitem__('emulator', value))
            return
        if field == "RetroArch Core":
            self.open_file_browser("Select RetroArch core", self.base_dir, ['.dll'], False, lambda value: self.launcher_form_data.__setitem__('core', value))
            return
        if field == "ROM Path":
            self.open_file_browser("Select ROM folder", self.base_dir, [], True, lambda value: self.launcher_form_data.__setitem__('rom_directory', value))
            return
        if field == "Shortcut Folder":
            self.open_file_browser("Select shortcut folder", self.base_dir, [], True, lambda value: self.launcher_form_data.__setitem__('rom_directory', value))
            return

    def launcher_form_safe_filename(self, name: str) -> str:
        blocked = '<>:"/\\|?*'
        cleaned = "".join("_" if ch in blocked else ch for ch in name).strip()
        return cleaned or "Launcher"

    def save_launcher_form(self):
        data = self.launcher_form_data

        launcher_name = data.get("launcher_name", "").strip()
        launcher_type = data.get("type", "Standalone Emulator")
        emulator_name = "RetroArch" if launcher_type == "RetroArch" else data.get("emulator_name", "").strip()
        emulator = data.get("emulator", "").strip()
        rom_directory = data.get("rom_directory", "").strip()

        if launcher_type == "Application":
            data["system"] = "Application"
        elif launcher_type == "PC Game":
            data["system"] = "PC"

        if not launcher_name:
            self.show_message("Missing launcher name", "Enter a launcher name.")
            return

        if launcher_type in ("Application", "PC Game"):
            if not emulator_name:
                if launcher_type == "PC Game":
                    self.show_message("Missing game name", "Enter a game name.")
                else:
                    self.show_message("Missing application name", "Enter an application name.")
                return
            if not emulator:
                if launcher_type == "PC Game":
                    self.show_message("Missing game", "Select a PC game path.")
                else:
                    self.show_message("Missing application", "Select an application path.")
                return
        elif launcher_type == "Shortcut":
            if not emulator:
                self.show_message("Missing shortcut", "Select a Windows shortcut.")
                return
        elif launcher_type == "Shortcut Folder":
            if not rom_directory:
                self.show_message("Missing shortcut folder", "Select a shortcut folder.")
                return
        else:
            if launcher_type != "RetroArch" and not emulator_name:
                self.show_message("Missing emulator name", "Enter an emulator name.")
                return
            if not emulator:
                self.show_message("Missing executable", "Select an emulator path.")
                return
            if not rom_directory:
                self.show_message("Missing ROM path", "Select a ROM path.")
                return
            if launcher_type == "RetroArch" and not data.get("core", "").strip():
                self.show_message("Missing core", "Select a RetroArch core.")
                return

        folder_value = data.get("folder", "")
        target_folder = self.menu_root / folder_value if folder_value else self.menu_root

        if not self.is_path_inside_menu_root(target_folder):
            self.show_message("Invalid folder", "Folder path is outside the menu folder.")
            return

        try:
            target_folder.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            self.show_message("Save failed", str(exc))
            return

        json_path = target_folder / f"{self.launcher_form_safe_filename(launcher_name)}.json"

        if not self.is_path_inside_menu_root(json_path):
            self.show_message("Invalid launcher", "Launcher path is outside the menu folder.")
            return

        old_json_path = self.launcher_form_path if self.launcher_form_mode == "edit" else None
        if json_path.exists() and (not old_json_path or json_path.resolve() != old_json_path.resolve()):
            self.show_message("Already exists", f"{json_path.name} already exists.")
            return

        extensions = []
        for ext in data.get("extensions", "").split(","):
            ext = ext.strip().lower()
            if not ext:
                continue
            if not ext.startswith("."):
                ext = "." + ext
            extensions.append(ext)

        json_type = self.launcher_type_to_json(launcher_type)

        if launcher_type == "Shortcut":
            output = {
                "type": json_type,
                "system": data.get("system", ""),
                "shortcut_path": emulator,
            }
        elif launcher_type == "Shortcut Folder":
            output = {
                "type": json_type,
                "system": data.get("system", ""),
                "shortcut_directory": rom_directory,
                "extensions": [".lnk"],
                "recursive": True,
            }
        else:
            output = {
                "type": json_type,
                "emulator_name": emulator_name,
                "system": "Application" if launcher_type == "Application" else ("PC" if launcher_type == "PC Game" else data.get("system", "")),
                "emulator": emulator,
                "rom_directory": "" if launcher_type in ("Application", "PC Game") else rom_directory,
                "extensions": [] if launcher_type in ("Application", "PC Game") else extensions,
                "arguments": data.get("arguments", "").strip() if launcher_type in ("Application", "PC Game") else data.get("arguments", "").strip() or '"{rom}"',
                "recursive": True,
            }

            if launcher_type == "RetroArch":
                output["core"] = data.get("core", "").strip()

        json_path.write_text(json.dumps(output, indent=2), encoding="utf-8")

        if old_json_path and json_path.resolve() != old_json_path.resolve():
            try:
                old_json_path.unlink()
            except Exception:
                pass
            self.launcher_form_path = json_path

        if self.launcher_form_mode == "edit":
            self.open_edit_launcher_browser(json_path.parent)
        else:
            self.current_folder = self.menu_root
            self.refresh_menu()

    def confirm_remove_launcher(self):
        if self.launcher_form_mode != "edit" or not self.launcher_form_path:
            return

        launcher_path = self.launcher_form_path
        if launcher_path.suffix.lower() != ".json" or not self.is_path_inside_menu_root(launcher_path):
            self.show_message("Remove failed", "This launcher cannot be removed safely.")
            return

        launcher_name = launcher_path.stem

        def remove_launcher():
            try:
                launcher_path.unlink()
            except Exception as exc:
                self.show_message("Remove failed", str(exc))
                return
            self.open_edit_launcher_browser(launcher_path.parent)

        self.show_confirmation(
            "Remove Launcher",
            f"Remove '{launcher_name}'?\n\nThis will delete the launcher JSON file.",
            remove_launcher,
        )

    def cancel_launcher_form(self):
        if self.launcher_form_mode == "edit":
            self.open_edit_launcher_browser(self.current_edit_folder)
        else:
            self.mode = "system"
            self.selected_index = 0
            self.view.update()

    def show_message(self, title: str, message: str, on_close=None, scrollable: bool = False):
        self.overlay = {"type": "message", "title": title, "message": message, "selected": 0, "on_close": on_close, "scrollable": scrollable, "scroll_offset": 0}
        self.view.update()

    def show_confirmation(self, title: str, message: str, on_yes):
        self.overlay = {"type": "choice", "title": title, "message": message, "selected": 0, "buttons": [("No", None), ("Yes", on_yes)]}
        self.view.update()

    def show_choice(self, title: str, message: str, buttons):
        self.overlay = {"type": "choice", "title": title, "message": message, "selected": 0, "buttons": buttons}
        self.view.update()

    def show_osd_confirmation(self, title: str, message: str):
        self.ingame_osd.show_confirmation(title, message)

    def close_overlay(self):
        callback = self.overlay.get("on_close") if self.overlay else None
        self.overlay = None
        self.view.update()
        if callback:
            callback()

    def activate_overlay(self):
        if not self.overlay: return
        if self.overlay["type"] == "message": self.close_overlay(); return
        buttons = self.overlay.get("buttons", [])
        selected = self.overlay.get("selected", 0)
        callback = buttons[selected][1] if 0 <= selected < len(buttons) else None
        self.overlay = None; self.view.update()
        if callback: callback()

    def open_text_input(self, title: str, prompt: str, value: str, callback):
        self.overlay_return_mode = self.mode
        self.overlay_return_index = self.selected_index
        self.overlay_return_scroll_offset = self.view.scroll_offset
        self.text_input_value = value
        self.text_input_cursor = len(value)
        self.text_input_shift = False
        self.text_input_caps = False
        self.text_input_symbols = False
        self.text_input_callback = callback
        self.text_input_title = title
        self.text_input_prompt = prompt
        self.text_keyboard_index = 0
        self.mode = "text_input"
        self.rebuild_text_input_keys()
        self.view.update()

    def rebuild_text_input_keys(self):
        if self.text_input_symbols:
            rows = [list('!@#$%^&*()'), list('[]{}<>\\/|'), list(':;\"\'`~+=_-'), ['QWERTY','Space','Backspace','Clear'], ['Cancel','Done']]
        else:
            rows = [list('1234567890-=') , list('qwertyuiop[]'), list("asdfghjkl;'"), ['Shift'] + list('zxcvbnm,./'), ['Caps','Symbols','Space','Backspace','Clear'], ['Cancel','Done']]
        self.text_input_keys = rows
        self.text_keyboard_row = min(getattr(self, 'text_keyboard_row', 0), len(rows)-1)
        self.text_keyboard_col = min(getattr(self, 'text_keyboard_col', 0), len(rows[self.text_keyboard_row])-1)

    def text_input_move(self, dx: int, dy: int):
        rows = self.text_input_keys
        self.text_keyboard_row = (getattr(self,'text_keyboard_row',0) + dy) % len(rows)
        self.text_keyboard_col = min(getattr(self,'text_keyboard_col',0), len(rows[self.text_keyboard_row])-1)
        if dx:
            self.text_keyboard_col = (self.text_keyboard_col + dx) % len(rows[self.text_keyboard_row])
        self.view.update()

    def insert_text(self, text: str):
        self.text_input_value = self.text_input_value[:self.text_input_cursor] + text + self.text_input_value[self.text_input_cursor:]
        self.text_input_cursor += len(text)
        if self.text_input_shift:
            self.text_input_shift = False
        self.view.update()

    def text_backspace(self):
        if self.text_input_cursor > 0:
            self.text_input_value = self.text_input_value[:self.text_input_cursor-1] + self.text_input_value[self.text_input_cursor:]
            self.text_input_cursor -= 1
        self.view.update()

    def activate_text_key(self):
        key = self.text_input_keys[self.text_keyboard_row][self.text_keyboard_col]
        if len(key) == 1:
            ch = key
            if ch.isalpha():
                upper = self.text_input_caps ^ self.text_input_shift
                ch = ch.upper() if upper else ch.lower()
            elif self.text_input_shift:
                shifted = {'1':'!','2':'@','3':'#','4':'$','5':'%','6':'^','7':'&','8':'*','9':'(','0':')','-':'_','=':'+','[':'{',']':'}',';':':',"'":'\"',',':'<','.':'>','/':'?','`':'~'}
                ch = shifted.get(ch, ch)
            self.insert_text(ch)
        elif key == 'Shift':
            self.text_input_shift = not self.text_input_shift; self.view.update()
        elif key == 'Caps':
            self.text_input_caps = not self.text_input_caps; self.view.update()
        elif key in ('Symbols','QWERTY'):
            self.text_input_symbols = not self.text_input_symbols; self.text_keyboard_row = self.text_keyboard_col = 0; self.rebuild_text_input_keys(); self.view.update()
        elif key == 'Space': self.insert_text(' ')
        elif key == 'Backspace': self.text_backspace()
        elif key == 'Clear': self.text_input_value=''; self.text_input_cursor=0; self.view.update()
        elif key == 'Cancel': self.cancel_text_input()
        elif key == 'Done': self.finish_text_input()

    def finish_text_input(self):
        callback = self.text_input_callback
        value = self.text_input_value
        self.mode = self.overlay_return_mode or 'launcher_form'
        self.selected_index = self.overlay_return_index
        self.view.scroll_offset = self.overlay_return_scroll_offset
        if callback: callback(value)
        self.view.ensure_visible()
        self.view.update()

    def cancel_text_input(self):
        self.mode = self.overlay_return_mode or 'launcher_form'
        self.selected_index = self.overlay_return_index
        self.view.scroll_offset = self.overlay_return_scroll_offset
        self.view.ensure_visible()
        self.view.update()

    def list_windows_roots(self):
        roots = []
        if platform.system() == 'Windows':
            for letter in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ':
                root = Path(f'{letter}:\\')
                if root.exists():
                    roots.append(root)
        else:
            roots = [Path('/')]
        return roots

    def drive_display_name(self, root: Path) -> str:
        if platform.system() != 'Windows':
            return str(root)
        drive = root.drive or str(root).rstrip('\\/')
        label = ''
        try:
            volume_name = ctypes.create_unicode_buffer(261)
            file_system_name = ctypes.create_unicode_buffer(261)
            serial_number = ctypes.c_ulong()
            maximum_component_length = ctypes.c_ulong()
            file_system_flags = ctypes.c_ulong()
            success = ctypes.windll.kernel32.GetVolumeInformationW(
                ctypes.c_wchar_p(str(root)),
                volume_name,
                len(volume_name),
                ctypes.byref(serial_number),
                ctypes.byref(maximum_component_length),
                ctypes.byref(file_system_flags),
                file_system_name,
                len(file_system_name),
            )
            if success:
                label = volume_name.value.strip()
        except Exception:
            label = ''
        return f'{drive}  {label}' if label else drive

    def open_file_browser(self, title: str, start: Path, extensions, select_folder: bool, callback, use_start: bool = False):
        self.overlay_return_mode = self.mode
        self.overlay_return_index = self.selected_index
        self.overlay_return_scroll_offset = self.view.scroll_offset
        self.file_browser_title = title
        self.file_browser_extensions = [x.lower() for x in extensions]
        self.file_browser_select_folder = select_folder
        self.file_browser_callback = callback
        start_path = Path(start) if start else None
        self.file_browser_path = start_path if use_start and start_path and start_path.is_dir() else None
        self.mode = 'file_browser'
        self.selected_index = 0
        self.refresh_file_browser()

    def refresh_file_browser(self):
        try:
            if self.file_browser_path is None:
                self.file_browser_items = [
                    (self.drive_display_name(root), root, True)
                    for root in self.list_windows_roots()
                ]
            else:
                items = []
                for child in sorted(
                    self.file_browser_path.iterdir(),
                    key=lambda p: (not p.is_dir(), p.name.lower()),
                ):
                    try:
                        if child.is_dir():
                            items.append((child.name, child, True))
                        elif not self.file_browser_select_folder and (
                            not self.file_browser_extensions
                            or child.suffix.lower() in self.file_browser_extensions
                        ):
                            items.append((child.name, child, False))
                    except OSError:
                        pass
                self.file_browser_items = items
        except Exception as exc:
            self.file_browser_items = []
            self.show_message('Open folder failed', str(exc))
        self.selected_index = 0
        self.view.scroll_offset = 0
        self.view.update()

    def activate_file_browser(self):
        can_select_folder = self.file_browser_select_folder and self.file_browser_path is not None
        if can_select_folder and self.selected_index == len(self.file_browser_items):
            callback = self.file_browser_callback
            value = str(self.file_browser_path).replace('\\', '/')
            self.mode = self.overlay_return_mode
            self.selected_index = self.overlay_return_index
            self.view.scroll_offset = self.overlay_return_scroll_offset
            callback(value)
            self.view.ensure_visible()
            self.view.update()
            return
        if not self.file_browser_items:
            return
        _, path, is_dir = self.file_browser_items[self.selected_index]
        if is_dir:
            self.file_browser_path = path
            self.refresh_file_browser()
        else:
            callback = self.file_browser_callback
            value = str(path).replace('\\', '/')
            self.mode = self.overlay_return_mode
            self.selected_index = self.overlay_return_index
            self.view.scroll_offset = self.overlay_return_scroll_offset
            callback(value)
            self.view.ensure_visible()
            self.view.update()

    def file_browser_back(self):
        if self.file_browser_path is None:
            self.cancel_file_browser()
            return
        parent = self.file_browser_path.parent
        if parent == self.file_browser_path:
            self.file_browser_path = None
        else:
            self.file_browser_path = parent
        self.refresh_file_browser()

    def cancel_file_browser(self):
        self.mode = self.overlay_return_mode or 'launcher_form'
        self.selected_index = self.overlay_return_index
        self.view.scroll_offset = self.overlay_return_scroll_offset
        self.view.ensure_visible()
        self.view.update()

    def title_path(self) -> str:
        if self.mode == "text_input": return self.text_input_title
        if self.mode == "file_browser": return self.file_browser_title
        if self.mode == "system":
            return "Gentleman Menu"
        if self.mode == "settings":
            if self.settings_category:
                return f"Settings/{self.settings_category}"
            return "Settings"
        if self.mode == "menu_size":
            return "Menu Size"
        if self.mode == "idle_hide_timeout":
            return "Auto Hide Menu"
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
            return f"Save Folder/{self.folder_picker_relative_label()}"
        if self.mode == "item_options":
            if self.item_options_target and self.item_options_target.item_type == "folder":
                return "Folder Options"
            return "Launcher Options"
        if self.mode == "edit_launchers":
            if self.current_edit_folder == self.menu_root:
                return "Edit Launcher"
            try:
                return f"Edit/{str(self.current_edit_folder.relative_to(self.menu_root)).replace(chr(92), '/')}"
            except Exception:
                return "Edit Launcher"
        if self.mode == "search_results":
            return f"Search Results/{self.search_query}" if self.search_query else "Search Results"
        if self.mode == "favorites":
            return "Favorites"
        if self.mode == "recent":
            return "Recent"
        if self.mode == "systems":
            return "Systems"
        if self.mode == "system_launchers":
            return self.current_game_system or "Systems"
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
        if self.mode == "text_input": return 1
        if self.mode == "file_browser": return len(self.file_browser_items) + (1 if self.file_browser_select_folder and self.file_browser_path is not None else 0)
        if self.mode == "system":
            self.update_system_items()
            return len(self.system_items)
        if self.mode == "settings":
            self.update_settings_items()
            return len(self.settings_items)
        if self.mode == "menu_size":
            return 3
        if self.mode == "idle_hide_timeout":
            return len(self.idle_menu_hide_timeout_choices())
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
        if self.mode == "item_options":
            return len(self.item_options_items)
        if self.mode == "edit_launchers":
            return len(self.edit_launcher_items) + 1
        if self.mode == "about":
            return len(ABOUT_LINES)
        if self.mode == "search_results":
            return len(self.search_items) + 1
        if self.mode == "favorites":
            return len(self.favorite_items) + 1
        if self.mode == "recent":
            return len(self.recent_items) + 1
        if self.mode == "systems":
            return len(self.game_system_items) + 1
        if self.mode == "system_launchers":
            if not self.current_game_system:
                return 1
            return len(self.game_system_games.get(self.current_game_system, [])) + 1
        if self.mode == "emulators":
            return len(self.emulator_items) + 1
        if self.mode == "emulator_launchers":
            if not self.current_emulator:
                return 1
            return len(self.emulator_launchers.get(self.current_emulator, [])) + 1
        if self.mode == "roms":
            return len(self.rom_labels)

        back_count = 1 if self.current_folder != self.menu_root else 0
        favorites_count = 1 if self.current_folder == self.menu_root and self.favorites_menu_enabled() else 0
        recent_count = 1 if self.current_folder == self.menu_root and self.recent_menu_enabled() else 0
        systems_count = 1 if self.current_folder == self.menu_root and self.systems_menu_enabled() else 0
        emulator_count = 1 if self.current_folder == self.menu_root and self.emulators_menu_enabled() else 0
        return len(self.menu_items) + back_count + favorites_count + recent_count + systems_count + emulator_count

    def first_real_list_index(self) -> int:
        labels = self.current_labels()
        if len(labels) > 1 and labels[0][0] == "...":
            return 1
        return 0

    def reset_selection_to_first_real_entry(self):
        self.selected_index = self.first_real_list_index()
        self.view.scroll_offset = 0

    def current_labels(self) -> list[tuple[str, str]]:
        if self.mode == "text_input": return []
        if self.mode == "file_browser":
            labels = [(name, "<DRIVE>" if self.file_browser_path is None else ("<DIR>" if is_dir else "")) for name, _, is_dir in self.file_browser_items]
            if self.file_browser_select_folder and self.file_browser_path is not None:
                labels.append(("Select This Folder", ""))
            return labels
        if self.mode == "system":
            self.update_system_items()
            return [(name, "") for name in self.system_items]
        if self.mode == "settings":
            self.update_settings_items()
            return [(name, "") for name in self.settings_items]
        if self.mode == "menu_size":
            selected = int(self.settings.get("fullscreen_menu_size", 100))
            return [
                ("100% (Default)" + ("  ✓" if selected == 100 else ""), ""),
                ("125%" + ("  ✓" if selected == 125 else ""), ""),
                ("150%" + ("  ✓" if selected == 150 else ""), ""),
            ]
        if self.mode == "idle_hide_timeout":
            selected = self.idle_menu_hide_timeout_seconds()
            labels = []
            for value in self.idle_menu_hide_timeout_choices():
                label = "Disabled" if value == 0 else ("1 min" if value == 60 else f"{value} sec")
                labels.append((label + ("  ✓" if selected == value else ""), ""))
            return labels
        if self.mode == "wallpaper":
            return [(name, "") for name in self.wallpaper_items]
        if self.mode == "support":
            return [(name, "") for name in self.support_items]
        if self.mode == "launcher_form":
            self.update_launcher_form_fields()
            labels = []
            launcher_type = self.launcher_form_data.get("type", "Standalone Emulator")
            for field in self.launcher_form_fields:
                display_field = "RetroArch Path" if field == "Emulator Path" and launcher_type == "RetroArch" else field
                if field in ("Save", "Cancel", "Remove Launcher"):
                    labels.append((field, ""))
                else:
                    labels.append((f"{display_field}: {self.launcher_form_value(field)}", ""))
            return labels
        if self.mode == "system_picker":
            return [(name, "") for name in self.system_picker_items]
        if self.mode == "type_picker":
            return [(name, "") for name in self.type_picker_items]
        if self.mode == "folder_picker":
            labels = []
            for item in self.folder_picker_items:
                item_type = item.get("type")
                if item_type == "folder":
                    labels.append((item.get("name", ""), "<DIR>"))
                elif item_type == "separator":
                    labels.append(("", ""))
                else:
                    labels.append((item.get("name", ""), ""))
            return labels
        if self.mode == "item_options":
            return [(name, "") for name in self.item_options_items]
        if self.mode == "edit_launchers":
            return [("...", "<DIR>")] + [(item.name, item.marker) for item in self.edit_launcher_items]
        if self.mode == "about":
            return [(line, "") for line in ABOUT_LINES]
        if self.mode == "search_results":
            return [("...", "<DIR>")] + [(str(item.get("name", "")), str(item.get("marker", ""))) for item in self.search_items]
        if self.mode == "favorites":
            return [("...", "<DIR>")] + [(self.display_name_for_saved_game_item(item), "") for item in self.favorite_items]
        if self.mode == "recent":
            return [("...", "<DIR>")] + [(self.display_name_for_saved_game_item(item), "") for item in self.recent_items]
        if self.mode == "systems":
            return [("...", "<DIR>")] + [(name, "") for name in self.game_system_items]
        if self.mode == "system_launchers":
            games = self.game_system_games.get(self.current_game_system or "", [])
            return [("...", "<DIR>")] + [(str(item.get("name", "")), "") for item in games]
        if self.mode == "emulators":
            return [("...", "<DIR>")] + [(name, "") for name in self.emulator_items]
        if self.mode == "emulator_launchers":
            launchers = self.emulator_launchers.get(self.current_emulator or "", [])
            return [("...", "<DIR>")] + [(item.name, "<DIR>") for item in launchers]
        if self.mode == "roms":
            return self.rom_labels

        labels = []
        if self.current_folder == self.menu_root and self.favorites_menu_enabled():
            labels.append(("Favorites", "<DIR>"))
        if self.current_folder == self.menu_root and self.recent_menu_enabled():
            labels.append(("Recent", "<DIR>"))
        if self.current_folder == self.menu_root and self.systems_menu_enabled():
            labels.append(("Systems", "<DIR>"))
        if self.current_folder == self.menu_root and self.emulators_menu_enabled():
            labels.append(("Emulators", "<DIR>"))

        for item in self.menu_items:
            labels.append((item.name, item.marker))

        if self.current_folder != self.menu_root:
            labels.insert(0, ("...", "<DIR>"))
        return labels

    def selected_menu_item(self) -> MenuItem | None:
        if self.mode != "menu":
            return None

        menu_index = self.selected_index

        if self.current_folder == self.menu_root:
            if self.favorites_menu_enabled():
                if menu_index == 0:
                    return None
                menu_index -= 1

            if self.recent_menu_enabled():
                if menu_index == 0:
                    return None
                menu_index -= 1

            if self.systems_menu_enabled():
                if menu_index == 0:
                    return None
                menu_index -= 1

            if self.emulators_menu_enabled():
                if menu_index == 0:
                    return None
                menu_index -= 1

        if self.current_folder != self.menu_root:
            if self.selected_index == 0:
                return None
            menu_index -= 1

        if menu_index < 0 or menu_index >= len(self.menu_items):
            return None

        return self.menu_items[menu_index]

    def open_current_item_options(self):
        item = self.selected_menu_item()
        if not item:
            return

        self.item_options_target = item
        self.item_options_return_mode = self.mode
        self.item_options_return_index = self.selected_index

        if item.item_type == "folder":
            self.item_options_items = ["Open Folder", "Remove Folder", "Cancel"]
        else:
            self.item_options_items = ["Open Launcher", "Edit Launcher", "Remove Launcher", "Cancel"]

        self.mode = "item_options"
        self.selected_index = 0
        self.view.scroll_offset = 0
        self.view.update()

    def close_item_options(self):
        self.mode = self.item_options_return_mode or "menu"
        self.selected_index = self.item_options_return_index
        self.item_options_target = None
        self.item_options_items = []
        self.view.ensure_visible()
        self.view.update()

    def activate_item_option(self):
        if not self.item_options_target or self.selected_index >= len(self.item_options_items):
            self.close_item_options()
            return

        action = self.item_options_items[self.selected_index]
        item = self.item_options_target

        if action == "Cancel":
            self.close_item_options()
            return

        if item.item_type == "folder":
            if action == "Open Folder":
                folder = item.path
                self.close_item_options()
                self.current_folder = folder
                self.refresh_menu()
                return
            if action == "Remove Folder":
                self.confirm_remove_menu_folder(item.path)
                return

        if item.item_type == "launcher":
            if action == "Open Launcher":
                launcher_item = item
                self.close_item_options()
                self.open_launcher_item(launcher_item)
                return
            if action == "Edit Launcher":
                launcher_path = item.path
                self.close_item_options()
                self.open_launcher_form(launcher_path)
                return
            if action == "Remove Launcher":
                self.confirm_remove_menu_launcher(item.path)
                return

    def confirm_remove_menu_launcher(self, launcher_path: Path):
        if launcher_path.suffix.lower() != ".json" or not self.is_path_inside_menu_root(launcher_path):
            self.show_message("Remove failed", "This launcher cannot be removed safely.")
            return

        launcher_name = launcher_path.stem
        parent_folder = launcher_path.parent
        return_index = self.item_options_return_index

        def remove_launcher():
            try:
                launcher_path.unlink()
            except Exception as exc:
                self.show_message("Remove failed", str(exc))
                return

            self.item_options_target = None
            self.item_options_items = []
            self.current_folder = parent_folder if self.is_path_inside_menu_root(parent_folder) else self.menu_root
            self.refresh_menu()
            count = self.current_items_count()
            self.selected_index = min(return_index, max(0, count - 1)) if count else 0
            self.view.ensure_visible()
            self.view.update()

        self.show_confirmation(
            "Remove Launcher",
            f"Remove '{launcher_name}'?\n\nThis will delete the launcher JSON file.",
            remove_launcher,
        )

    def confirm_remove_menu_folder(self, folder_path: Path):
        if not folder_path.exists() or not folder_path.is_dir() or not self.is_path_inside_menu_root(folder_path):
            self.show_message("Remove failed", "This folder cannot be removed safely.")
            return

        try:
            root = self.menu_root.resolve()
            target = folder_path.resolve()
        except Exception:
            self.show_message("Remove failed", "This folder cannot be removed safely.")
            return

        if target == root or root not in target.parents:
            self.show_message("Remove failed", "The root menu folder cannot be removed.")
            return

        folder_name = folder_path.name
        parent_folder = folder_path.parent
        return_index = self.item_options_return_index

        def remove_folder():
            try:
                shutil.rmtree(folder_path)
            except Exception as exc:
                self.show_message("Remove failed", str(exc))
                return

            self.item_options_target = None
            self.item_options_items = []
            self.current_folder = parent_folder if self.is_path_inside_menu_root(parent_folder) else self.menu_root
            self.refresh_menu()
            count = self.current_items_count()
            self.selected_index = min(return_index, max(0, count - 1)) if count else 0
            self.view.ensure_visible()
            self.view.update()

        self.show_confirmation(
            "Remove Folder",
            f"Remove folder '{folder_name}'?\n\nThis will remove the folder, all launchers, and all subfolders inside it.",
            remove_folder,
        )

    def refresh_menu(self):
        self.mode = "menu"
        self.current_launcher = None
        self.current_rom_folder = None
        self.current_emulator = None
        self.current_game_system = None
        self.clear_rom_items()
        self.menu_items = scan_menu_folder(self.current_folder)
        self.update_favorite_items()
        self.update_recent_items()
        self.update_game_system_items()
        self.update_emulator_items()
        self.reset_selection_to_first_real_entry()
        self.view.update()

    def open_edit_launcher_browser(self, folder: Path | None = None):
        self.current_edit_folder = folder or self.menu_root
        self.edit_launcher_items = scan_menu_folder(self.current_edit_folder)
        self.mode = "edit_launchers"
        self.reset_selection_to_first_real_entry()
        self.view.update()

    def open_system_menu(self):
        self.update_system_items()
        self.mode = "system"
        self.selected_index = 0
        self.view.update()

    def go_back(self):
        if self.overlay:
            self.close_overlay(); return
        if self.mode == "text_input": self.cancel_text_input(); return
        if self.mode == "file_browser": self.file_browser_back(); return
        if self.mode == "system":
            self.mode = "menu"
            self.selected_index = 0
            self.view.update()
            return

        if self.mode == "settings":
            if self.settings_category:
                self.settings_category = None
                self.update_settings_items()
                self.selected_index = 0
                self.view.scroll_offset = 0
                self.view.update()
                return
            self.mode = "system"
            self.selected_index = 0
            self.view.update()
            return

        if self.mode == "menu_size":
            self.mode = "settings"
            self.settings_category = "Menu"
            self.update_settings_items()
            self.selected_index = 0
            self.view.scroll_offset = 0
            self.view.update()
            return

        if self.mode == "idle_hide_timeout":
            self.mode = "settings"
            self.settings_category = "Menu"
            self.update_settings_items()
            self.selected_index = 1
            self.view.scroll_offset = 0
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
            if self.folder_picker_current_folder != self.menu_root:
                self.set_folder_picker_folder(self.folder_picker_current_folder.parent)
                return
            self.mode = "launcher_form"
            self.selected_index = min(self.launcher_form_return_index, max(0, len(self.launcher_form_fields) - 1))
            self.view.ensure_visible()
            self.view.update()
            return

        if self.mode == "item_options":
            self.close_item_options()
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

        if self.mode == "search_results":
            self.restore_search_return_state()
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

        if self.mode == "systems":
            self.mode = "menu"
            self.selected_index = 0
            self.view.update()
            return

        if self.mode == "system_launchers":
            self.mode = "systems"
            self.current_game_system = None
            self.reset_selection_to_first_real_entry()
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
            self.reset_selection_to_first_real_entry()
            self.view.update()
            return

        if self.mode == "roms":
            if self.current_launcher and self.current_rom_folder:
                rom_root = Path(self.current_launcher.rom_directory).resolve()
                current = self.current_rom_folder.resolve()

                if current != rom_root and rom_root in current.parents:
                    self.open_rom_folder(self.current_launcher, self.current_rom_folder.parent)
                    return

            self.mode = "menu"
            self.current_launcher = None
            self.current_rom_folder = None
            self.clear_rom_items()
            self.rom_scan_request_id += 1
            self.selected_index = 0
            self.view.update()
            return

        if self.current_folder == self.menu_root:
            self.open_system_menu()
            return

        self.current_folder = self.current_folder.parent
        self.refresh_menu()

    def activate_selected(self):
        if self.overlay:
            self.activate_overlay(); return
        if self.mode == "text_input": self.activate_text_key(); return
        if self.mode == "file_browser": self.activate_file_browser(); return
        if self.current_items_count() == 0:
            return

        if self.mode == "system":
            self.activate_system_item(self.system_items[self.selected_index])
            return

        if self.mode == "settings":
            self.activate_settings_item(self.settings_items[self.selected_index])
            return

        if self.mode == "menu_size":
            self.activate_menu_size_item(self.selected_index)
            return

        if self.mode == "idle_hide_timeout":
            self.activate_idle_hide_timeout_item(self.selected_index)
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

        if self.mode == "item_options":
            self.activate_item_option()
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

        if self.mode == "search_results":
            self.activate_search_result()
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

        if self.mode == "systems":
            if self.selected_index == 0:
                self.go_back()
                return

            self.current_game_system = self.game_system_items[self.selected_index - 1]
            self.update_current_system_game_items()
            self.mode = "system_launchers"
            self.reset_selection_to_first_real_entry()
            self.view.update()
            return

        if self.mode == "system_launchers":
            if self.selected_index == 0:
                self.go_back()
                return

            games = self.game_system_games.get(self.current_game_system or "", [])
            game_index = self.selected_index - 1
            if game_index < 0 or game_index >= len(games):
                return

            self.launch_system_game_item(games[game_index])
            return

        if self.mode == "emulators":
            if self.selected_index == 0:
                self.go_back()
                return

            emulator_name = self.emulator_items[self.selected_index - 1]
            emulator_path = self.emulator_paths.get(emulator_name, "")

            if not emulator_path:
                self.show_message("Launch failed", "No emulator path found.")
                return

            try:
                process = launch_external_process(f'"{emulator_path}"', str(Path(emulator_path).parent))
                self.begin_active_session(process, "emulator", emulator_name, "")
            except Exception as exc:
                self.resume_frontend_input_after_launch()
                self.show_message("Launch failed", str(exc))
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
            if self.rom_loading:
                return

            if self.selected_index == 0:
                self.go_back()
                return

            if not self.current_launcher:
                return

            selected = self.rom_items[self.selected_index - 1]

            if selected.is_dir:
                self.open_rom_folder(self.current_launcher, selected.path)
                return

            try:
                if self.current_launcher.launcher_type == "shortcut_folder":
                    process = self.launch_shortcut_path(selected.path)
                    emulator_name = self.current_launcher.system or self.current_launcher.path.stem
                else:
                    process = launch_rom(self.current_launcher, selected.path)
                    emulator_name = self.current_launcher.emulator_name or self.current_launcher.path.stem

                self.begin_active_session(
                    process,
                    "game",
                    selected.display_name,
                    emulator_name,
                )
                self.add_recent_game(self.current_launcher.path, selected.path)
                self.update_recent_items()
            except Exception as exc:
                self.resume_frontend_input_after_launch()
                self.show_message("Launch failed", str(exc))
            return

        menu_index = self.selected_index

        if self.current_folder == self.menu_root:
            if self.favorites_menu_enabled():
                if menu_index == 0:
                    self.update_favorite_items()
                    self.mode = "favorites"
                    self.reset_selection_to_first_real_entry()
                    self.view.update()
                    return
                menu_index -= 1

            if self.recent_menu_enabled():
                if menu_index == 0:
                    self.update_recent_items()
                    self.mode = "recent"
                    self.reset_selection_to_first_real_entry()
                    self.view.update()
                    return
                menu_index -= 1

            if self.systems_menu_enabled():
                if menu_index == 0:
                    self.update_game_system_items()
                    self.mode = "systems"
                    self.reset_selection_to_first_real_entry()
                    self.view.update()
                    return
                menu_index -= 1

            if self.emulators_menu_enabled():
                if menu_index == 0:
                    self.update_emulator_items()
                    self.mode = "emulators"
                    self.reset_selection_to_first_real_entry()
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


    def search_normalized_text(self, value: str) -> str:
        text = str(value or "").lower()
        return "".join(ch for ch in text if ch.isalnum())

    def search_result_marker(self, launcher: LauncherConfig | None, fallback: str = "") -> str:
        if launcher is None:
            return fallback
        if launcher.launcher_type == "application":
            return "<APP>"
        if launcher.launcher_type == "shortcut":
            return "<LINK>"
        if launcher.launcher_type == "shortcut_folder":
            return "<LINK>"
        label = (launcher.system or launcher.emulator_name or launcher.path.stem).strip()
        if not label:
            return fallback
        return f"<{label[:12]}>"

    def search_result_matches(self, name: str, query: str) -> bool:
        q = self.search_normalized_text(query)
        if not q:
            return False
        return q in self.search_normalized_text(name)

    def current_search_scope_label(self) -> str:
        if self.mode == "menu":
            if self.current_folder == self.menu_root:
                return "All"
            try:
                return str(self.current_folder.relative_to(self.menu_root)).replace(chr(92), "/")
            except Exception:
                return self.current_folder.name
        if self.mode == "roms" and self.current_launcher:
            label = self.current_launcher.system or self.current_launcher.path.stem
            if self.current_rom_folder:
                try:
                    root = Path(self.current_launcher.rom_directory).resolve()
                    folder = self.current_rom_folder.resolve()
                    if folder != root and root in folder.parents:
                        rel = str(folder.relative_to(root)).replace(chr(92), "/")
                        return f"{label}/{rel}"
                except Exception:
                    pass
            return label
        if self.mode == "system_launchers" and self.current_game_system:
            return self.current_game_system
        if self.mode == "favorites":
            return "Favorites"
        if self.mode == "recent":
            return "Recent"
        return "Current"

    def capture_search_return_state(self) -> dict:
        return {
            "mode": self.mode,
            "selected_index": self.selected_index,
            "scroll_offset": self.view.scroll_offset,
            "current_folder": self.current_folder,
            "current_launcher": self.current_launcher,
            "current_rom_folder": self.current_rom_folder,
            "current_game_system": self.current_game_system,
        }

    def restore_search_return_state(self):
        state = self.search_return_state or {}
        self.mode = state.get("mode", "menu")
        self.selected_index = int(state.get("selected_index", 0) or 0)
        self.view.scroll_offset = int(state.get("scroll_offset", 0) or 0)
        self.current_folder = state.get("current_folder", self.current_folder)
        self.current_launcher = state.get("current_launcher", self.current_launcher)
        self.current_rom_folder = state.get("current_rom_folder", self.current_rom_folder)
        self.current_game_system = state.get("current_game_system", self.current_game_system)
        self.search_items = []
        self.search_query = ""
        self.search_scope_label = ""
        self.view.ensure_visible()
        self.view.update()

    def open_context_search(self):
        if self.overlay or self.input_suspended_for_launch or self.ingame_osd.isVisible():
            return
        if self.mode not in {"menu", "roms", "system_launchers", "favorites", "recent", "search_results"}:
            return
        if self.mode == "roms" and self.rom_loading:
            return

        if self.mode != "search_results":
            self.search_return_state = self.capture_search_return_state()
            self.search_scope_label = self.current_search_scope_label()
        title = f"Search: {self.search_scope_label or 'Current'}"
        self.open_text_input(title, "Enter search text", self.search_query, self.run_context_search)

    def launcher_items_under_folder(self, folder: Path) -> list[MenuItem]:
        items: list[MenuItem] = []
        try:
            launcher_paths = sorted(folder.rglob("*.json"), key=lambda p: str(p).lower())
        except Exception:
            launcher_paths = []
        for launcher_path in launcher_paths:
            if not self.is_path_inside_menu_root(launcher_path):
                continue
            items.append(MenuItem(launcher_path.stem, launcher_path, "launcher"))
        return items

    def collect_search_games_for_launcher(self, launcher_item: MenuItem, start_folder: Path | None = None, recursive: bool = True) -> list[dict]:
        try:
            launcher = load_launcher(launcher_item.path)
        except Exception:
            return []

        if launcher.launcher_type in {"application", "shortcut"}:
            return [{
                "type": "launcher",
                "name": launcher.emulator_name or launcher_item.name,
                "marker": self.search_result_marker(launcher),
                "launcher_item": launcher_item,
                "launcher": launcher,
                "rom": None,
            }]

        root = Path(start_folder or launcher.rom_directory)
        if not root.is_dir():
            return []

        games: list[dict] = []
        folders_to_scan = [root]
        visited: set[str] = set()

        while folders_to_scan:
            folder = folders_to_scan.pop(0)
            try:
                folder_key = str(folder.resolve()).replace(chr(92), "/").lower()
            except Exception:
                folder_key = str(folder).replace(chr(92), "/").lower()
            if folder_key in visited:
                continue
            visited.add(folder_key)

            for rom_item in self.scan_cached_rom_folder_sync(launcher, folder):
                if rom_item.is_dir:
                    if recursive:
                        folders_to_scan.append(rom_item.path)
                    continue
                games.append({
                    "type": "game",
                    "name": rom_item.display_name,
                    "marker": self.search_result_marker(launcher),
                    "launcher_item": launcher_item,
                    "launcher": launcher,
                    "rom": rom_item.path,
                })
        return games

    def build_context_search_items(self, query: str) -> list[dict]:
        state = self.search_return_state or {}
        mode = state.get("mode", self.mode)
        results: list[dict] = []

        if mode == "menu":
            folder = state.get("current_folder", self.menu_root)
            if not isinstance(folder, Path):
                folder = self.menu_root
            for launcher_item in self.launcher_items_under_folder(folder):
                results.extend(self.collect_search_games_for_launcher(launcher_item, recursive=True))
        elif mode == "roms":
            launcher = state.get("current_launcher")
            folder = state.get("current_rom_folder")
            if isinstance(launcher, LauncherConfig) and isinstance(folder, Path):
                launcher_item = MenuItem(launcher.path.stem, launcher.path, "launcher")
                results.extend(self.collect_search_games_for_launcher(launcher_item, folder, recursive=True))
        elif mode == "system_launchers":
            system_name = str(state.get("current_game_system") or "")
            results.extend(self.game_system_games.get(system_name, []))
        elif mode == "favorites":
            for item in self.favorite_items:
                results.append({
                    "type": "saved",
                    "name": self.display_name_for_saved_game_item(item),
                    "marker": "<FAV>",
                    "saved_item": item,
                })
        elif mode == "recent":
            for item in self.recent_items:
                results.append({
                    "type": "saved",
                    "name": self.display_name_for_saved_game_item(item),
                    "marker": "<RECENT>",
                    "saved_item": item,
                })

        filtered: list[dict] = []
        seen: set[tuple[str, str]] = set()
        for item in results:
            name = str(item.get("name", ""))
            if not self.search_result_matches(name, query):
                continue
            launcher = item.get("launcher")
            rom = item.get("rom")
            launcher_path = getattr(launcher, "path", item.get("launcher_item", MenuItem("", Path(), "launcher")).path)
            rom_path = rom if rom is not None else launcher_path
            if item.get("type") == "saved":
                saved = item.get("saved_item", {})
                key = (str(saved.get("launcher", "")).lower(), str(saved.get("rom", "")).lower())
            else:
                key = (str(launcher_path).lower(), str(rom_path).lower())
            if key in seen:
                continue
            seen.add(key)
            if "marker" not in item or not item.get("marker"):
                item["marker"] = self.search_result_marker(item.get("launcher"))
            filtered.append(item)

        filtered.sort(key=lambda item: str(item.get("name", "")).lower())
        return filtered

    def run_context_search(self, value: str):
        query = str(value or "").strip()
        if not query:
            self.restore_search_return_state()
            return

        self.search_query = query
        self.search_items = self.build_context_search_items(query)
        self.mode = "search_results"
        self.selected_index = 0 if not self.search_items else 1
        self.view.scroll_offset = 0
        self.view.update()

        if not self.search_items:
            self.show_message("Search", f"No results found for '{query}'.")

    def activate_search_result(self):
        if self.selected_index == 0:
            self.restore_search_return_state()
            return
        result_index = self.selected_index - 1
        if result_index < 0 or result_index >= len(self.search_items):
            return

        item = self.search_items[result_index]
        item_type = item.get("type")
        if item_type == "saved":
            self.launch_recent_item(item.get("saved_item", {}))
            return
        if item_type == "launcher":
            launcher_item = item.get("launcher_item")
            if isinstance(launcher_item, MenuItem):
                self.open_launcher_item(launcher_item)
            return
        self.launch_system_game_item(item)

    def launch_system_game_item(self, item: dict):
        launcher = item.get("launcher")
        launcher_item = item.get("launcher_item")
        rom = item.get("rom")

        if not isinstance(launcher, LauncherConfig):
            if isinstance(launcher_item, MenuItem):
                self.open_launcher_item(launcher_item)
            return

        try:
            if launcher.launcher_type == "application":
                process = self.launch_application_config(launcher)
                self.begin_active_session(process, "application", str(item.get("name", launcher.path.stem)), "")
                return

            if launcher.launcher_type == "shortcut":
                shortcut_path = Path(launcher.shortcut_path or launcher.emulator)
                if not shortcut_path.exists():
                    self.show_message("Launch failed", "Shortcut file not found.")
                    return
                process = self.launch_shortcut_path(shortcut_path)
                self.begin_active_session(process, "application", str(item.get("name", launcher.path.stem)), launcher.system or "Shortcut")
                return

            if rom is None:
                return

            rom_path = Path(rom)
            if launcher.launcher_type == "shortcut_folder":
                process = self.launch_shortcut_path(rom_path)
                emulator_name = launcher.system or launcher.path.stem
            else:
                process = launch_rom(launcher, rom_path)
                emulator_name = launcher.emulator_name or launcher.path.stem

            self.begin_active_session(
                process,
                "game",
                str(item.get("name", rom_path.stem)),
                emulator_name,
            )
            self.add_recent_game(launcher.path, rom_path)
            self.update_recent_items()
            self.view.update()
        except Exception as exc:
            self.resume_frontend_input_after_launch()
            self.show_message("Launch failed", str(exc))

    def launch_recent_item(self, item: dict):
        launcher_rel = str(item.get("launcher", ""))
        rom = Path(str(item.get("rom", "")))

        launcher_path = self.menu_root / launcher_rel
        if not launcher_path.exists() or not rom.exists():
            self.show_message("Recent item unavailable", "The launcher or game file no longer exists.")
            return

        try:
            launcher = load_launcher(launcher_path)
            if launcher.launcher_type == "shortcut_folder":
                process = self.launch_shortcut_path(rom)
                emulator_name = launcher.system or launcher.path.stem
            else:
                process = launch_rom(launcher, rom)
                emulator_name = launcher.emulator_name or launcher.path.stem

            self.begin_active_session(
                process,
                "game",
                self.display_name_for_rom(launcher, rom),
                emulator_name,
            )
            self.add_recent_game(launcher_path, rom)
            self.update_recent_items()
            self.view.update()
        except Exception as exc:
            self.resume_frontend_input_after_launch()
            self.show_message("Launch failed", str(exc))

    def open_launcher_item(self, item: MenuItem):
        try:
            launcher = load_launcher(item.path)

            if launcher.launcher_type == "application":
                process = self.launch_application_config(launcher)
                self.begin_active_session(process, "application", launcher.emulator_name or item.name, "")
                return

            if launcher.launcher_type == "shortcut":
                shortcut_path = Path(launcher.shortcut_path or launcher.emulator)
                if not shortcut_path.exists():
                    self.show_message("Launch failed", "Shortcut file not found.")
                    return
                process = self.launch_shortcut_path(shortcut_path)
                self.begin_active_session(process, "application", item.name, launcher.system or "Shortcut")
                return

            self.open_rom_folder(launcher, Path(launcher.rom_directory))
        except Exception as exc:
            self.show_message("Launcher error", str(exc))

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
            self.update_wallpaper_items()
            self.mode = "wallpaper"
            self.selected_index = 0
            self.view.scroll_offset = 0
            self.view.update()
        elif item == "Settings":
            self.settings_category = None
            self.update_settings_items()
            self.mode = "settings"
            self.selected_index = 0
            self.view.scroll_offset = 0
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
            self.show_update_available_choice(info)
        elif show_no_update:
            self.show_message("No Update Available", f"You are already running the latest version.\n\nCurrent version: {info.current_version}")

    def show_update_available_choice(self, info):
        if gentleman_updater_available():
            update_action = self._run_gentleman_updater
            update_label = "Run Updater"
        else:
            update_action = lambda: open_release_page(info.release_url)
            update_label = "Download"

        self.show_choice(
            "Update Available",
            f"{info.release_name}\n\nA new version of Gentleman is available.",
            [("Later", None), ("Release Notes", lambda: self.show_update_changelog(info)), (update_label, update_action)],
        )

    def _run_gentleman_updater(self):
        if launch_gentleman_updater(): QApplication.quit()
        else: self.show_message("Updater Failed", "Gentleman-Updater could not be started.")

    def show_update_changelog(self, info):
        release_body = getattr(info, "release_body", "") or ""
        if release_body.strip():
            self.show_message(info.release_name, release_body, on_close=lambda: self.show_update_available_choice(info), scrollable=True)
        else:
            open_release_page(info.release_url)
            self.show_update_available_choice(info)

    def on_update_check_error(self, message: str):
        if getattr(self.update_check_worker, "show_errors", True):
            self.show_message("Update Check Failed", f"Unable to check for updates.\n\n{message}")

    def on_update_check_finished(self):
        self.update_check_worker = None

    def clear_recent_items(self):
        self.show_confirmation("Clear Recent", "Clear all recently launched games?", self._confirm_clear_recent)

    def _confirm_clear_recent(self):
        self.save_recent_items([]); self.update_recent_items(); self.view.update()

    def clear_favorite_items(self):
        self.show_confirmation("Clear Favorites", "Clear all favorite games?", self._confirm_clear_favorites)

    def _confirm_clear_favorites(self):
        self.save_favorite_items([]); self.update_favorite_items(); self.view.update()

    def refresh_settings_menu(self):
        self.save_settings()
        self.update_settings_items()
        self.view.update()

    def activate_settings_item(self, item: str):
        if self.settings_category is None:
            if item in self.settings_categories():
                self.settings_category = item
                self.update_settings_items()
                self.selected_index = 0
                self.view.scroll_offset = 0
                self.view.update()
            return

        if item.startswith("Fullscreen at Launch:"):
            self.settings["fullscreen_at_launch"] = not self.settings.get("fullscreen_at_launch", False)
            self.refresh_settings_menu()
        elif item.startswith("Menu Size:"):
            self.mode = "menu_size"
            sizes = [100, 125, 150]
            current = int(self.settings.get("fullscreen_menu_size", 100))
            self.selected_index = sizes.index(current) if current in sizes else 0
            self.view.scroll_offset = 0
            self.view.update()
        elif item.startswith("Auto Hide Menu:"):
            self.mode = "idle_hide_timeout"
            choices = self.idle_menu_hide_timeout_choices()
            current = self.idle_menu_hide_timeout_seconds()
            self.selected_index = choices.index(current) if current in choices else 0
            self.view.scroll_offset = 0
            self.view.update()
        elif item.startswith("Systems Menu:"):
            self.settings["show_systems_menu"] = not self.systems_menu_enabled()
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
            self.invalidate_rom_folder_cache()
            if self.current_launcher and self.current_rom_folder:
                self.open_rom_folder(self.current_launcher, self.current_rom_folder)
            self.update_recent_items()
            self.update_favorite_items()
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

    def activate_menu_size_item(self, index: int):
        sizes = [100, 125, 150]
        if not (0 <= index < len(sizes)):
            return
        self.settings["fullscreen_menu_size"] = sizes[index]
        self.save_settings()
        self.view.scroll_offset = 0
        self.view.update()

    def activate_idle_hide_timeout_item(self, index: int):
        choices = self.idle_menu_hide_timeout_choices()
        if not (0 <= index < len(choices)):
            return
        self.settings["menu_idle_hide_timeout"] = choices[index]
        self.save_settings()
        self.idle_menu_hidden = False
        self.last_idle_activity_time = time.monotonic()
        self.view.scroll_offset = 0
        self.view.update()

    def wallpaper_folder_path(self) -> Path | None:
        value = str(self.settings.get("wallpaper_folder", "")).strip()
        if not value:
            return None
        path = Path(value)
        if path.is_dir():
            return path
        self.settings["wallpaper_folder"] = ""
        self.save_settings()
        return None

    def update_wallpaper_items(self):
        if self.wallpaper_folder_path() is not None:
            self.wallpaper_items = [
                "Choose Wallpaper",
                "Clear Wallpaper Folder",
                "Browse This PC",
            ]
        else:
            self.wallpaper_items = [
                "Set Wallpaper Folder",
                "Browse This PC",
            ]

    def activate_wallpaper_item(self, item: str):
        if item == "Choose Wallpaper":
            folder = self.wallpaper_folder_path()
            if folder is None:
                self.update_wallpaper_items()
                self.selected_index = 0
                self.view.update()
                return
            self.open_file_browser(
                "Select wallpaper",
                folder,
                [".png", ".jpg", ".jpeg", ".bmp", ".webp", ".gif"],
                False,
                self._set_wallpaper_path,
                use_start=True,
            )
        elif item == "Set Wallpaper Folder":
            self.open_file_browser(
                "Select wallpaper folder",
                self.base_dir,
                [],
                True,
                self._set_wallpaper_folder,
            )
        elif item == "Clear Wallpaper Folder":
            self.settings["wallpaper_folder"] = ""
            self.save_settings()
            self.update_wallpaper_items()
            self.selected_index = 0
            self.view.scroll_offset = 0
            self.view.update()
        elif item == "Browse This PC":
            self.open_file_browser(
                "Select wallpaper",
                self.base_dir,
                [".png", ".jpg", ".jpeg", ".bmp", ".webp", ".gif"],
                False,
                self._set_wallpaper_path,
            )

    def _set_wallpaper_folder(self, path):
        folder = Path(path)
        if not folder.is_dir():
            self.show_message("Wallpaper Folder", "The selected wallpaper folder is not available.")
            return
        self.settings["wallpaper_folder"] = str(folder).replace("\\", "/")
        self.save_settings()
        self.update_wallpaper_items()
        self.selected_index = 0
        self.view.scroll_offset = 0
        self.view.update()

    def activate_support_item(self, item: str):
        if item == "Ko-fi":
            webbrowser.open("https://ko-fi.com/anime0t4ku")
        elif item == "Buy Me a Coffee":
            webbrowser.open("https://buymeacoffee.com/anime0t4ku")

    def _set_wallpaper_path(self, path):
        self.settings["wallpaper"] = path
        self.save_settings(); self.view.reload_wallpaper(); self.view.update()

    def open_folder(self, folder: Path):
        try:
            if platform.system() == "Windows":
                subprocess.Popen(["explorer", str(folder)])
            elif platform.system() == "Darwin":
                subprocess.Popen(["open", str(folder)])
            else:
                subprocess.Popen(["xdg-open", str(folder)])
        except Exception as exc:
            self.show_message("Open folder failed", str(exc))

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
        self.note_user_activity()
        self.activate_selected(); self.view.update()

    def controller_back(self):
        self.active_input = "controller"
        self.note_user_activity()
        if self.mode == "text_input": self.text_backspace()
        else: self.go_back()
        self.view.update()

    def controller_favorite(self):
        self.active_input = "controller"
        self.note_user_activity()
        if self.mode == "text_input": self.insert_text(' ')
        elif self.mode == "favorites": self.remove_selected_favorite()
        else: self.toggle_current_favorite()
        self.view.update()

    def controller_shift(self):
        self.active_input='controller'
        self.note_user_activity()
        if self.mode=='text_input':
            self.text_input_shift=not self.text_input_shift
        else:
            self.open_current_item_options()
        self.view.update()

    def controller_caps(self):
        self.active_input='controller'
        self.note_user_activity()
        if self.mode=='text_input': self.text_input_caps=not self.text_input_caps; self.view.update()

    def controller_search(self):
        self.active_input = 'controller'
        self.note_user_activity()
        if self.mode == 'text_input':
            self.text_input_symbols = not self.text_input_symbols
            self.text_keyboard_row = self.text_keyboard_col = 0
            self.rebuild_text_input_keys()
        else:
            self.open_context_search()
        self.view.update()

    def controller_symbols(self):
        self.controller_search()

    def controller_done(self):
        self.active_input='controller'
        self.note_user_activity()
        if self.mode=='text_input': self.finish_text_input()
        else: self.activate_selected()
        self.view.update()

    def controller_step(self, action: str):
        self.active_input = "controller"
        self.note_user_activity()
        if self.overlay:
            if self.overlay.get("type") == "choice" and action in ("left", "right"):
                count=len(self.overlay.get("buttons", []))
                if count: self.overlay["selected"]=(self.overlay.get("selected",0)+(-1 if action=="left" else 1))%count; self.view.update()
            elif self.overlay.get("scrollable") and action in ("up", "down"):
                self.overlay["scroll_offset"] = max(0, self.overlay.get("scroll_offset", 0) + (-48 if action == "up" else 48))
                self.view.update()
            return
        if self.mode == "text_input":
            dx = -1 if action == "left" else 1 if action == "right" else 0
            dy = -1 if action == "up" else 1 if action == "down" else 0
            self.text_input_move(dx, dy); return
        if action == "up":
            self.move_selection(-1)
        elif action == "down":
            self.move_selection(1)
        elif action in ("left", "right"):
            if self.mode == "launcher_form" and self.cycle_launcher_form_value(-1 if action == "left" else 1):
                return
            self.jump_selection(-10 if action == "left" else 10)

    def set_controller_active_input(self):
        if self.active_input != "controller":
            self.active_input = "controller"
            self.view.update()

    def set_keyboard_active_input(self):
        if self.active_input != "keyboard":
            self.active_input = "keyboard"
            self.view.update()

    def read_controller_buttons(self) -> dict[int, bool]:
        buttons = {}
        if self.controller is None:
            return buttons

        try:
            for index in range(self.controller.get_numbuttons()):
                buttons[index] = bool(self.controller.get_button(index))
        except Exception:
            return {}

        return buttons

    def read_controller_axes(self) -> dict[int, float]:
        axes = {}
        if self.controller is None:
            return axes

        try:
            for index in range(self.controller.get_numaxes()):
                axes[index] = float(self.controller.get_axis(index))
        except Exception:
            return {}

        return axes

    def read_controller_hats(self) -> list[tuple[int, int]]:
        hats = []
        if self.controller is None:
            return hats

        try:
            for index in range(self.controller.get_numhats()):
                hats.append(self.controller.get_hat(index))
        except Exception:
            return []

        return hats

    def controller_any_input_active(self, buttons: dict[int, bool], axes: dict[int, float], hats: list[tuple[int, int]]) -> bool:
        if any(buttons.values()):
            return True
        if any(x != 0 or y != 0 for x, y in hats):
            return True

        # Only the main left-stick axes are treated as active input here.
        # Many Bluetooth controllers expose trigger/rest axes as -1.0 or 1.0,
        # which made the app immediately switch back to the gamepad icon after
        # keyboard input even when the controller was untouched.
        return abs(axes.get(0, 0.0)) > 0.5 or abs(axes.get(1, 0.0)) > 0.5

    def controller_direction_state(self, buttons: dict[int, bool], axes: dict[int, float], hats: list[tuple[int, int]]) -> dict[str, bool]:
        state = {"up": False, "down": False, "left": False, "right": False}

        for hat_x, hat_y in hats:
            if hat_y > 0:
                state["up"] = True
            elif hat_y < 0:
                state["down"] = True

            if hat_x < 0:
                state["left"] = True
            elif hat_x > 0:
                state["right"] = True

        if any(state.values()):
            return state

        dpad_button_sets = (
            {"up": 11, "down": 12, "left": 13, "right": 14},
            {"up": 13, "down": 14, "left": 11, "right": 12},
            {"up": 12, "down": 13, "left": 14, "right": 15},
        )
        for mapping in dpad_button_sets:
            mapped_state = {"up": False, "down": False, "left": False, "right": False}
            for direction, button in mapping.items():
                if buttons.get(button, False):
                    mapped_state[direction] = True

            if any(mapped_state.values()):
                return mapped_state

        value = axes.get(0, 0.0)
        if value < -0.5:
            state["left"] = True
        elif value > 0.5:
            state["right"] = True

        value = axes.get(1, 0.0)
        if value < -0.5:
            state["up"] = True
        elif value > 0.5:
            state["down"] = True

        return state

    def controller_active_actions(self, directions: dict[str, bool]) -> list[str]:
        active_actions = []
        if directions.get("up"):
            active_actions.append("up")
        elif directions.get("down"):
            active_actions.append("down")
        elif directions.get("left"):
            active_actions.append("left")
        elif directions.get("right"):
            active_actions.append("right")
        return active_actions

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
        controller_event_seen = False
        for event in events:
            if event.type in (pygame.JOYDEVICEADDED, pygame.JOYDEVICEREMOVED):
                device_changed = True
            elif event.type in (pygame.JOYBUTTONDOWN, pygame.JOYHATMOTION):
                controller_event_seen = True
            elif event.type == pygame.JOYAXISMOTION and abs(getattr(event, "value", 0.0)) > 0.5:
                controller_event_seen = True

        if controller_event_seen:
            self.set_controller_active_input()

        if device_changed:
            self.controller = None
            self.controller_available = False
            self.refresh_controller(force=True)

        self.refresh_controller()

        if not self.controller_available or self.controller is None:
            return

        try:
            buttons = self.read_controller_buttons()
            axes = self.read_controller_axes()
            hats = self.read_controller_hats()
            directions = self.controller_direction_state(buttons, axes, hats)

            if self.controller_any_input_active(buttons, axes, hats):
                self.set_controller_active_input()
        except Exception:
            self.controller = None
            self.controller_available = False
            self.refresh_controller(force=True)
            return

        if self.input_suspended_for_launch:
            self.poll_ingame_osd_shortcuts()
            if not self.ingame_osd.isVisible():
                self.controller_button_state.clear()
            self.controller_repeat_action = None
            self.controller_repeat_next_ms = 0
            if self.ingame_osd.isVisible():
                try:
                    up = directions.get("up", False)
                    down = directions.get("down", False)
                    horizontal = directions.get("left", False) or directions.get("right", False)
                    accept = buttons.get(0, False)
                    back = buttons.get(1, False)
                    for key, pressed, callback in (
                        ("osd_up", up, lambda: self.ingame_osd.move_selection(-1)),
                        ("osd_down", down, lambda: self.ingame_osd.move_selection(1)),
                        ("osd_horizontal", horizontal, self.ingame_osd.controller_horizontal),
                        ("osd_accept", accept, self.ingame_osd.controller_accept),
                        ("osd_back", back, self.ingame_osd.controller_back),
                    ):
                        previous = self.controller_button_state.get(key, False)
                        if pressed and not previous:
                            callback()
                        self.controller_button_state[key] = bool(pressed)
                except Exception:
                    pass
            return

        try:
            active_actions = self.controller_active_actions(directions)

            if self.idle_menu_hidden and self.controller_any_input_active(buttons, axes, hats):
                self.note_user_activity()
                self.controller_button_state = {button: bool(pressed) for button, pressed in buttons.items()}
                if active_actions:
                    self.controller_repeat_action = active_actions[0]
                    self.controller_repeat_next_ms = int(time.monotonic() * 1000) + 350
                else:
                    self.controller_repeat_action = None
                    self.controller_repeat_next_ms = 0
                return

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
                3 if favorite_button == 2 else 2: self.controller_shift,
                4: self.controller_caps,
                5: self.controller_symbols,
                6: self.controller_back,
                7: self.controller_done,
            }

            for button, callback in button_actions.items():
                pressed = buttons.get(button, False)
                previous = self.controller_button_state.get(button, False)

                if pressed and not previous:
                    callback()

                self.controller_button_state[button] = pressed

            for button, pressed in buttons.items():
                if button not in button_actions:
                    self.controller_button_state[button] = pressed
        except Exception:
            self.controller = None
            self.controller_available = False
            self.refresh_controller(force=True)

    def is_current_selection_selectable(self) -> bool:
        if self.mode == "folder_picker" and 0 <= self.selected_index < len(self.folder_picker_items):
            return self.folder_picker_items[self.selected_index].get("type") != "separator"
        return True

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

        for _ in range(count):
            self.selected_index = (self.selected_index + delta) % count
            if self.is_current_selection_selectable():
                break
        self.view.ensure_visible()
        self.view.update()

    def jump_selection(self, delta: int):
        count = self.current_items_count()
        if count <= 0:
            return

        if self.mode == "about":
            self.move_selection(delta)
            return

        self.selected_index = max(0, min(count - 1, self.selected_index + delta))
        if not self.is_current_selection_selectable():
            direction = 1 if delta >= 0 else -1
            for _ in range(count):
                self.selected_index = max(0, min(count - 1, self.selected_index + direction))
                if self.is_current_selection_selectable() or self.selected_index in (0, count - 1):
                    break
        self.view.ensure_visible()
        self.view.update()

    def toggle_fullscreen(self):
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()
        QTimer.singleShot(50, self.view.update)

    def keyPressEvent(self, event: QKeyEvent):
        if self.input_suspended_for_launch: self.resume_frontend_input_after_launch()
        self.set_keyboard_active_input()
        key = event.key()
        if self.note_user_activity():
            return
        if self.overlay:
            if self.overlay.get("type") == "choice" and key in (Qt.Key.Key_Left, Qt.Key.Key_Right, Qt.Key.Key_A, Qt.Key.Key_D):
                count=len(self.overlay.get("buttons", [])); delta=-1 if key in (Qt.Key.Key_Left,Qt.Key.Key_A) else 1
                if count: self.overlay["selected"]=(self.overlay.get("selected",0)+delta)%count
            elif self.overlay.get("scrollable") and key in (Qt.Key.Key_Up, Qt.Key.Key_W, Qt.Key.Key_Down, Qt.Key.Key_S):
                delta = -48 if key in (Qt.Key.Key_Up, Qt.Key.Key_W) else 48
                self.overlay["scroll_offset"] = max(0, self.overlay.get("scroll_offset", 0) + delta)
            elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space): self.activate_overlay()
            elif key in (Qt.Key.Key_Escape, Qt.Key.Key_Backspace): self.close_overlay()
            self.view.update(); return
        mods=event.modifiers()
        if mods & Qt.KeyboardModifier.ControlModifier and key == Qt.Key.Key_F:
            self.open_context_search()
            self.view.update()
            return
        if self.mode == "text_input":
            if mods & Qt.KeyboardModifier.ControlModifier and key == Qt.Key.Key_A: self.text_input_value=''; self.text_input_cursor=0
            elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter): self.finish_text_input()
            elif key == Qt.Key.Key_Escape: self.cancel_text_input()
            elif key == Qt.Key.Key_Backspace: self.text_backspace()
            elif key == Qt.Key.Key_Delete and self.text_input_cursor < len(self.text_input_value): self.text_input_value=self.text_input_value[:self.text_input_cursor]+self.text_input_value[self.text_input_cursor+1:]
            elif key == Qt.Key.Key_Left: self.text_input_cursor=max(0,self.text_input_cursor-1)
            elif key == Qt.Key.Key_Right: self.text_input_cursor=min(len(self.text_input_value),self.text_input_cursor+1)
            elif key == Qt.Key.Key_Home: self.text_input_cursor=0
            elif key == Qt.Key.Key_End: self.text_input_cursor=len(self.text_input_value)
            elif event.text() and event.text().isprintable(): self.insert_text(event.text())
            self.view.update(); return
        if key in (Qt.Key.Key_Up, Qt.Key.Key_W): self.move_selection(-1)
        elif key in (Qt.Key.Key_Down, Qt.Key.Key_S): self.move_selection(1)
        elif key in (Qt.Key.Key_Left, Qt.Key.Key_A):
            if not (self.mode == "launcher_form" and self.cycle_launcher_form_value(-1)):
                self.jump_selection(-10)
        elif key in (Qt.Key.Key_Right, Qt.Key.Key_D):
            if not (self.mode == "launcher_form" and self.cycle_launcher_form_value(1)):
                self.jump_selection(10)
        elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space): self.activate_selected()
        elif key in (Qt.Key.Key_Escape, Qt.Key.Key_Backspace): self.go_back()
        elif key == Qt.Key.Key_F5: self.refresh_menu()
        elif key == Qt.Key.Key_F:
            if self.mode == "favorites": self.remove_selected_favorite()
            else: self.toggle_current_favorite()
        elif key == Qt.Key.Key_Y:
            self.open_current_item_options()
        elif key == Qt.Key.Key_F11: self.toggle_fullscreen()
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
        self.wallpaper_movie = None
        self.scaled_wallpaper = QPixmap()
        self.scaled_wallpaper_cache_key = None
        self.reload_wallpaper()

        self.wallpaper_preview_timer = QTimer(self)
        self.wallpaper_preview_timer.setSingleShot(True)
        self.wallpaper_preview_timer.setInterval(200)
        self.wallpaper_preview_timer.timeout.connect(self.load_pending_wallpaper_preview)
        self.wallpaper_preview_pending_path = None
        self.wallpaper_preview_current_path = None
        self.wallpaper_preview_pixmap = QPixmap()
        self.wallpaper_preview_cache = {}

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
        if self.wallpaper_movie is not None:
            self.wallpaper_movie.stop()
            self.wallpaper_movie.deleteLater()
            self.wallpaper_movie = None

        wallpaper_path = self.window.settings.get("wallpaper", "")
        path = Path(wallpaper_path) if wallpaper_path else None
        if path and path.exists() and path.suffix.lower() == ".gif":
            movie = QMovie(str(path))
            movie.setCacheMode(QMovie.CacheMode.CacheAll)
            movie.frameChanged.connect(lambda _frame: self.update())
            if movie.isValid():
                self.wallpaper_movie = movie
                self.wallpaper = QPixmap()
                self.scaled_wallpaper = QPixmap()
                self.scaled_wallpaper_cache_key = None
                movie.start()
                return
            movie.deleteLater()

        self.wallpaper_movie = None
        self.wallpaper = QPixmap(str(path)) if path and path.exists() else QPixmap()
        self.scaled_wallpaper = QPixmap()
        self.scaled_wallpaper_cache_key = None

    def ensure_visible(self):
        visible_rows = self.visible_rows()
        idx = self.window.selected_index

        if idx < self.scroll_offset:
            self.scroll_offset = idx
        elif idx >= self.scroll_offset + visible_rows:
            self.scroll_offset = idx - visible_rows + 1

        self.scroll_offset = max(0, self.scroll_offset)

    def effective_menu_scale(self) -> float:
        if not self.window.isFullScreen():
            return 1.0
        size = int(self.window.settings.get("fullscreen_menu_size", 100))
        if size not in (100, 125, 150):
            size = 100
        return size / 100.0

    def menu_panel_size(self) -> tuple[int, int]:
        scale = self.effective_menu_scale()
        panel_w = min(round(620 * scale), max(620, self.width() - 80))
        panel_h = min(round(430 * scale), max(430, self.height() - 120))
        panel_w = min(panel_w, self.width() - 40)
        panel_h = min(panel_h, self.height() - 100)
        return max(480, panel_w), max(320, panel_h)

    def is_wallpaper_browser(self) -> bool:
        return self.window.mode == "file_browser" and self.window.file_browser_title == "Select wallpaper"

    def menu_panel_rect(self) -> QRect:
        panel_w, panel_h = self.menu_panel_size()
        x = (self.width() - panel_w) // 2
        y = (self.height() - panel_h) // 2
        return QRect(x, y, panel_w, panel_h)

    def selected_wallpaper_preview_path(self):
        if not self.is_wallpaper_browser():
            return None
        index = self.window.selected_index
        if not (0 <= index < len(self.window.file_browser_items)):
            return None
        _name, path, is_dir = self.window.file_browser_items[index]
        if is_dir or path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".gif"}:
            return None
        return path

    def request_wallpaper_preview(self, path):
        path = Path(path) if path is not None else None
        if path == self.wallpaper_preview_pending_path:
            return
        self.wallpaper_preview_pending_path = path
        self.wallpaper_preview_current_path = None
        self.wallpaper_preview_pixmap = QPixmap()
        self.wallpaper_preview_timer.stop()
        if path is None:
            self.update()
            return
        cached = self.wallpaper_preview_cache.get(str(path))
        if cached is not None:
            self.wallpaper_preview_current_path = path
            self.wallpaper_preview_pixmap = cached
            self.update()
            return
        self.wallpaper_preview_timer.start()

    def load_pending_wallpaper_preview(self):
        path = self.wallpaper_preview_pending_path
        if path is None or not path.exists():
            return
        preview = QPixmap(str(path))
        if preview.isNull():
            return
        self.wallpaper_preview_cache[str(path)] = preview
        while len(self.wallpaper_preview_cache) > 8:
            self.wallpaper_preview_cache.pop(next(iter(self.wallpaper_preview_cache)))
        if path == self.wallpaper_preview_pending_path:
            self.wallpaper_preview_current_path = path
            self.wallpaper_preview_pixmap = preview
            self.update()

    def wallpaper_preview_rect(self) -> QRect:
        panel = self.menu_panel_rect()
        width = 280
        height = 220
        gap = 24
        margin = 24
        right_x = panel.right() + gap
        left_x = panel.x() - gap - width
        if right_x + width <= self.width() - margin:
            x = right_x
        elif left_x >= margin:
            x = left_x
        else:
            x = max(margin, self.width() - margin - width)
        y = panel.y() + (panel.height() - height) // 2
        y = max(margin, min(y, self.height() - margin - height))
        return QRect(x, y, width, height)

    def draw_wallpaper_preview(self, painter: QPainter):
        path = self.selected_wallpaper_preview_path()
        if path != self.wallpaper_preview_pending_path:
            self.request_wallpaper_preview(path)
        if path is None or path != self.wallpaper_preview_current_path or self.wallpaper_preview_pixmap.isNull():
            return
        rect = self.wallpaper_preview_rect()
        preview = self.wallpaper_preview_pixmap
        painter.fillRect(rect, QColor(45, 0, 12, 245))
        painter.setPen(self.light)
        painter.drawRect(rect)
        inner = rect.adjusted(8, 8, -8, -8)
        scaled = preview.scaled(
            inner.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        x = inner.x() + (inner.width() - scaled.width()) // 2
        y = inner.y() + (inner.height() - scaled.height()) // 2
        painter.drawPixmap(x, y, scaled)

    def top_bar_rect(self) -> QRect:
        panel_rect = self.menu_panel_rect()
        bar_h = 44
        gap = 34
        y = max(16, panel_rect.y() - bar_h - gap)
        return QRect(panel_rect.x(), y, panel_rect.width(), bar_h)

    def visible_rows(self) -> int:
        _, panel_h = self.menu_panel_size()
        if self.window.mode == "system":
            reserved_height = 114
        elif self.window.mode in {"menu_size", "idle_hide_timeout"}:
            reserved_height = 114
        else:
            reserved_height = 30
        return max(1, (panel_h - reserved_height) // 28)

    def wrapped_text_lines(self, painter: QPainter, text: str, max_width: int) -> list[str]:
        words = text.split()
        if not words:
            return [""]
        lines = []
        current = words[0]
        metrics = painter.fontMetrics()
        for word in words[1:]:
            candidate = f"{current} {word}"
            if metrics.horizontalAdvance(candidate) <= max_width:
                current = candidate
            else:
                lines.append(current)
                current = word
        lines.append(current)
        return lines

    def draw_instruction_text(self, painter: QPainter, x: int, y: int, max_width: int, lines: list[str]) -> int:
        current_y = y
        for text in lines:
            for wrapped in self.wrapped_text_lines(painter, text, max_width):
                painter.drawText(x, current_y, wrapped)
                current_y += 28
        return current_y + 14

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        background_overscan = 8
        painter.fillRect(
            QRect(-background_overscan, -background_overscan, self.width() + background_overscan * 2, self.height() + background_overscan * 2),
            self.bg,
        )

        wallpaper_frame = self.wallpaper_movie.currentPixmap() if self.wallpaper_movie is not None else self.wallpaper
        if not wallpaper_frame.isNull():
            viewport_w = max(1, self.width())
            viewport_h = max(1, self.height())
            source_w = max(1, wallpaper_frame.width())
            source_h = max(1, wallpaper_frame.height())
            overscan = 8
            scale = max(
                (viewport_w + overscan * 2) / source_w,
                (viewport_h + overscan * 2) / source_h,
            )
            scaled_w = max(viewport_w + overscan * 2, math.ceil(source_w * scale))
            scaled_h = max(viewport_h + overscan * 2, math.ceil(source_h * scale))
            if self.wallpaper_movie is not None:
                scaled = wallpaper_frame.scaled(
                    scaled_w,
                    scaled_h,
                    Qt.AspectRatioMode.IgnoreAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            else:
                cache_key = (viewport_w, viewport_h, scaled_w, scaled_h, self.wallpaper.cacheKey())
                if self.scaled_wallpaper_cache_key != cache_key or self.scaled_wallpaper.isNull():
                    self.scaled_wallpaper = wallpaper_frame.scaled(
                        scaled_w,
                        scaled_h,
                        Qt.AspectRatioMode.IgnoreAspectRatio,
                        Qt.TransformationMode.SmoothTransformation,
                    )
                    self.scaled_wallpaper_cache_key = cache_key
                scaled = self.scaled_wallpaper
            x = (viewport_w - scaled.width()) // 2
            y = (viewport_h - scaled.height()) // 2
            painter.save()
            painter.setClipRect(self.rect())
            painter.drawPixmap(x, y, scaled)
            painter.restore()
            painter.fillRect(self.rect(), QColor(0, 0, 0, 95))
        else:
            self.draw_static_noise(painter)

        self.draw_logo(painter)
        if not self.window.idle_menu_hidden:
            self.draw_top_bar(painter)
            self.draw_panel(painter)
            self.draw_wallpaper_preview(painter)
        if self.window.mode == "text_input": self.draw_text_input(painter)
        if self.window.overlay: self.draw_overlay(painter)

    def draw_overlay(self, painter):
        ov = self.window.overlay
        labels = ["OK"] if ov.get("type") == "message" else [item[0] for item in ov.get("buttons", [])]
        box_width = min(820, self.width() - 80)
        content_width = box_width - 72

        painter.setFont(self.title_font)
        title_height = painter.fontMetrics().height()
        painter.setFont(self.font)
        guide_height = painter.fontMetrics().height()
        button_rows = max(1, (len(labels) + 2) // 3)
        button_area_height = button_rows * 44

        if ov.get("scrollable"):
            box_height = min(max(420, self.height() - 100), self.height() - 60)
        else:
            message_bounds = painter.boundingRect(
                QRect(0, 0, content_width, max(160, self.height() - 220)),
                Qt.TextFlag.TextWordWrap | Qt.AlignmentFlag.AlignHCenter,
                ov.get("message", ""),
            )
            message_height = max(painter.fontMetrics().height(), message_bounds.height())
            box_height = 22 + title_height + 18 + message_height + 20 + button_area_height + 12 + guide_height + 18
            box_height = min(max(300, box_height), self.height() - 80)

        box = QRect(
            self.width() // 2 - box_width // 2,
            self.height() // 2 - box_height // 2,
            box_width,
            box_height,
        )

        painter.fillRect(self.rect(), QColor(0, 0, 0, 150))
        painter.fillRect(box, QColor(45, 0, 12, 252))
        painter.setPen(self.light)
        painter.drawRect(box)

        painter.setFont(self.title_font)
        painter.setPen(self.text)
        title_rect = QRect(box.x() + 24, box.y() + 18, box.width() - 48, title_height + 4)
        painter.drawText(title_rect, Qt.AlignmentFlag.AlignCenter, ov.get("title", ""))

        painter.setFont(self.font)
        guide_rect = QRect(box.x() + 16, box.bottom() - guide_height - 10, box.width() - 32, guide_height)
        button_area_bottom = guide_rect.y() - 12
        button_area_top = button_area_bottom - button_area_height
        message_rect = QRect(
            box.x() + 36,
            title_rect.bottom() + 14,
            box.width() - 72,
            max(40, button_area_top - title_rect.bottom() - 28),
        )

        message = ov.get("message", "")
        if ov.get("scrollable"):
            full_bounds = painter.boundingRect(
                QRect(0, 0, message_rect.width(), 100000),
                Qt.TextFlag.TextWordWrap | Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
                message,
            )
            max_scroll = max(0, full_bounds.height() - message_rect.height() + 12)
            scroll_offset = min(max(0, ov.get("scroll_offset", 0)), max_scroll)
            ov["scroll_offset"] = scroll_offset
            painter.save()
            painter.setClipRect(message_rect)
            draw_rect = QRect(message_rect.x(), message_rect.y() - scroll_offset, message_rect.width(), full_bounds.height() + 12)
            painter.drawText(draw_rect, Qt.TextFlag.TextWordWrap | Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop, message)
            painter.restore()
            if max_scroll > 0:
                scroll_track = QRect(message_rect.right() + 8, message_rect.y(), 4, message_rect.height())
                thumb_height = max(28, int(message_rect.height() * (message_rect.height() / (full_bounds.height() + 1))))
                thumb_y = scroll_track.y() + int((scroll_track.height() - thumb_height) * (scroll_offset / max_scroll)) if max_scroll else scroll_track.y()
                painter.setPen(self.light)
                painter.drawRect(scroll_track)
                painter.fillRect(QRect(scroll_track.x(), thumb_y, scroll_track.width(), thumb_height), self.light)
        else:
            painter.drawText(
                message_rect,
                Qt.TextFlag.TextWordWrap | Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
                message,
            )

        columns = min(3, max(1, len(labels)))
        gap = 12
        available_width = box.width() - 72 - gap * (columns - 1)
        label_width = max((painter.fontMetrics().horizontalAdvance(label) for label in labels), default=0)
        button_w = min(240, max(140, label_width + 52, available_width // columns))
        row_width = columns * button_w + gap * (columns - 1)
        start_x = box.center().x() - row_width // 2
        for i, label in enumerate(labels):
            row = i // columns
            col = i % columns
            r = QRect(start_x + col * (button_w + gap), button_area_top + row * 44, button_w, 36)
            if i == ov.get("selected", 0):
                painter.fillRect(r, self.light)
                painter.setPen(self.dark_text)
            else:
                painter.setPen(self.text)
                painter.drawRect(r)
            painter.drawText(r, Qt.AlignmentFlag.AlignCenter, label)

        painter.setPen(self.text)
        guide = "D-pad Select   A Confirm   B Back"
        if ov.get("scrollable"):
            guide = "Up/Down Scroll   A Close   B Back"
        painter.drawText(guide_rect, Qt.AlignmentFlag.AlignCenter, guide)

    def display_text_key(self, key: str) -> str:
        if len(key) != 1 or self.window.text_input_symbols:
            return key
        if key.isalpha():
            return key.upper() if (self.window.text_input_caps ^ self.window.text_input_shift) else key.lower()
        if self.window.text_input_shift:
            shifted = {'1':'!','2':'@','3':'#','4':'$','5':'%','6':'^','7':'&','8':'*','9':'(','0':')','-':'_','=':'+','[':'{',']':'}',';':':',"'":'"',',':'<','.':'>','/':'?','`':'~'}
            return shifted.get(key, key)
        return key

    def draw_text_input(self, painter):
        painter.fillRect(self.rect(), QColor(0,0,0,175)); box=QRect(70,70,self.width()-140,self.height()-140); painter.fillRect(box,QColor(45,0,12,252)); painter.setPen(self.light); painter.drawRect(box)
        painter.setFont(self.title_font); painter.setPen(self.text); painter.drawText(box.x()+28,box.y()+40,self.window.text_input_title)
        painter.setFont(self.font); painter.drawText(box.x()+28,box.y()+76,self.window.text_input_prompt)
        field=QRect(box.x()+28,box.y()+95,box.width()-56,48); painter.fillRect(field,QColor(20,0,6,245)); painter.setPen(self.light); painter.drawRect(field)
        value=self.window.text_input_value; cursor=self.window.text_input_cursor; display=value[:cursor]+'|' + value[cursor:]; painter.setPen(self.text); painter.drawText(field.adjusted(12,0,-12,0),Qt.AlignmentFlag.AlignVCenter,display)
        rows=self.window.text_input_keys; y=box.y()+175
        for ri,row in enumerate(rows):
            labels=[self.display_text_key(k) for k in row]
            widths=[max(54, painter.fontMetrics().horizontalAdvance(label)+28) for label in labels]
            gap=8; total=sum(widths)+gap*(len(row)-1); x=box.center().x()-total//2
            for ci,(k,label,w) in enumerate(zip(row,labels,widths)):
                r=QRect(x,y,w,38)
                active=(ri==self.window.text_keyboard_row and ci==self.window.text_keyboard_col)
                toggled=(k=='Shift' and self.window.text_input_shift) or (k=='Caps' and self.window.text_input_caps)
                if active: painter.fillRect(r,self.light); painter.setPen(self.dark_text)
                else:
                    painter.setPen(self.light if toggled else self.text)
                    painter.drawRect(r)
                painter.drawText(r,Qt.AlignmentFlag.AlignCenter,label); x+=w+gap
            y+=48
        guide="D-pad Navigate   A Select   B Backspace   X Space   Y Shift   LB Caps Lock   RB Symbols   Start Done"
        painter.setPen(self.text); painter.drawText(box.adjusted(18,0,-18,-14),Qt.AlignmentFlag.AlignBottom|Qt.AlignmentFlag.AlignHCenter,guide)

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

        overscan = 8
        target_w = max(1, self.width() + overscan * 2)
        target_h = max(1, self.height() + overscan * 2)
        scaled = self.static_noise.scaled(
            target_w,
            target_h,
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.FastTransformation,
        )

        painter.save()
        painter.setClipRect(self.rect())
        painter.drawPixmap(-overscan, -overscan, scaled)
        painter.fillRect(
            QRect(-overscan, -overscan, self.width() + overscan * 2, self.height() + overscan * 2),
            QColor(30, 0, 10, 45),
        )
        painter.restore()

    def network_icon(self) -> str:
        return self.window.cached_network_icon

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
        elif self.window.mode == "menu_size":
            painter.setPen(self.text)
            instruction_w = max(120, panel_w - side_w - 60)
            row_y = self.draw_instruction_text(
                painter,
                text_x,
                row_y,
                instruction_w,
                [
                    "Menu size applies in fullscreen mode.",
                    "Windowed mode always uses 100%.",
                ],
            )
        elif self.window.mode == "idle_hide_timeout":
            painter.setPen(self.text)
            instruction_w = max(120, panel_w - side_w - 60)
            row_y = self.draw_instruction_text(
                painter,
                text_x,
                row_y,
                instruction_w,
                [
                    "Hides menu and top bar after idle time.",
                    "Disabled during dialogs and input screens.",
                ],
            )
        elif self.window.mode == "wallpaper" and self.window.wallpaper_folder_path() is not None:
            painter.setPen(self.text)
            painter.drawText(text_x, row_y, "Wallpaper folder set")
            row_y += 56

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
