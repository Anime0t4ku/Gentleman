from __future__ import annotations

import json
import math
import ctypes
import threading
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import time
import webbrowser
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote

from PyQt6.QtCore import Qt, QTimer, QRect, QRectF, QEvent, QThread, pyqtSignal, QByteArray, QSize
from PyQt6.QtGui import QFont, QKeyEvent, QPainter, QColor, QPen, QPixmap, QIcon, QImage, QMovie
from PyQt6.QtSvg import QSvgRenderer
from PyQt6.QtWidgets import (
    QApplication,
    QMainWindow,
    QWidget,
)

from app.app_info import APP_NAME, APP_VERSION, ABOUT_LINES
from app.zaparoo_systems import ZAPAROO_SYSTEM_NAMES
from app.theme_manager import ThemeManager, DEFAULT_THEME_ID
from core.arcade_names import ArcadeNameDatabase
from core.launcher import load_launcher, scan_rom_folder, launch_rom, launch_application, launch_external_process, launch_link_shortcut, LauncherConfig, RomBrowserItem
from core.menu_scanner import MenuItem, scan_menu_folder
from core.metadata import (
    MetadataCache,
    ScreenScraperClient,
    GameMetadataIdentity,
    SCRAPE_MODES,
    SCRAPE_REGIONS,
    SCREENSCRAPER_SYSTEM_IDS,
    ScreenScraperQuotaError,
    ScreenScraperDailyQuotaError,
    cleaned_scrape_name,
    region_from_option,
)
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




class ScrapeJobIndexWorker(QThread):
    result = pyqtSignal(object)

    def __init__(self, window, target: str, mode: str):
        super().__init__()
        self.window = window
        self.target = target
        self.mode = mode

    def run(self):
        try:
            jobs = self.window.build_scrape_jobs(self.target, self.mode)
            self.result.emit({"ok": True, "jobs": jobs})
        except Exception as exc:
            self.result.emit({"ok": False, "error": str(exc)})

class ScrapeWorker(QThread):
    progress = pyqtSignal(object)
    finished_result = pyqtSignal(object)

    def __init__(self, cache: MetadataCache, jobs: list[dict], mode: str, username: str, password: str):
        super().__init__()
        self.cache = cache
        self.jobs = jobs
        self.mode = mode
        self.username = username
        self.password = password
        self.cancel_requested = False

    def cancel(self):
        self.cancel_requested = True

    def run(self):
        def quota_callback(quota):
            self.progress.emit({"type": "quota", "quota": quota})

        client = ScreenScraperClient(self.username, self.password, quota_callback=quota_callback)
        total = len(self.jobs)
        summary = {"scraped": 0, "skipped": 0, "missing": 0, "failed": 0, "stopped": "", "quota_reached": False, "quota_message": "", "login_failed": False, "login_message": ""}
        try:
            client.quota_info()
        except Exception as exc:
            summary["stopped"] = "Login failed"
            summary["login_failed"] = True
            summary["login_message"] = str(exc) or "ScreenScraper login rejected."
            self.finished_result.emit(summary)
            return
        for index, job in enumerate(self.jobs, 1):
            if self.cancel_requested:
                summary["stopped"] = "Cancelled"
                break
            identity = job.get("identity")
            if not isinstance(identity, GameMetadataIdentity):
                summary["failed"] += 1
                continue
            if not self.cache.should_scrape(identity, self.mode):
                summary["skipped"] += 1
                self.progress.emit({"index": index, "total": total, "name": identity.rom_stem, "status": "Skipped"})
                continue
            try:
                search_name = str(job.get("search_name") or cleaned_scrape_name(identity.rom_name))
                self.progress.emit({"index": index, "total": total, "name": identity.rom_stem, "status": "Scraping"})
                preferred_region = str(job.get("preferred_region", ""))
                metadata, box2d = client.scrape_game(int(job.get("system_id")), identity.rom_name, search_name, preferred_region, str(job.get("game_id", "")))
                if not metadata.get("scrape_name"):
                    summary["missing"] += 1
                    self.progress.emit({"index": index, "total": total, "name": identity.rom_stem, "status": "Not found"})
                    continue
                self.cache.save(identity, metadata, box2d, str(job.get("manual_scrape_name", "")))
                summary["scraped"] += 1
                self.progress.emit({"index": index, "total": total, "name": str(metadata.get("scrape_name") or identity.rom_stem), "status": "Saved"})
            except ScreenScraperDailyQuotaError as exc:
                message = str(exc).strip() or "ScreenScraper daily quota has been reached."
                summary["stopped"] = "Quota reached"
                summary["quota_reached"] = True
                summary["quota_message"] = message
                self.progress.emit({"index": index, "total": total, "name": identity.rom_stem, "status": "Quota reached"})
                break
            except ScreenScraperQuotaError as exc:
                message = str(exc).strip() or "ScreenScraper quota or rate limit reached."
                summary["stopped"] = "Quota or rate limit reached"
                summary["quota_reached"] = True
                summary["quota_message"] = message
                self.progress.emit({"index": index, "total": total, "name": identity.rom_stem, "status": "Quota or rate limit reached"})
                break
            except Exception as exc:
                message = str(exc)
                lower_message = message.lower()
                if "login" in lower_message or "credential" in lower_message or "identifiant" in lower_message:
                    summary["stopped"] = "Login failed"
                    summary["login_failed"] = True
                    summary["login_message"] = message
                    self.progress.emit({"index": index, "total": total, "name": identity.rom_stem, "status": "Login failed"})
                    break
                if "limit reached" in lower_message or "quota" in lower_message:
                    summary["stopped"] = message
                    summary["quota_reached"] = True
                    summary["quota_message"] = message
                    self.progress.emit({"index": index, "total": total, "name": identity.rom_stem, "status": message})
                    break
                if "not found" in lower_message:
                    summary["missing"] += 1
                else:
                    summary["failed"] += 1
                self.progress.emit({"index": index, "total": total, "name": identity.rom_stem, "status": message})
        self.finished_result.emit(summary)


class ScreenScraperQuotaWorker(QThread):
    result = pyqtSignal(object)

    def __init__(self, username: str, password: str):
        super().__init__()
        self.username = username
        self.password = password

    def run(self):
        try:
            client = ScreenScraperClient(self.username, self.password)
            self.result.emit({"ok": True, "quota": client.quota_info()})
        except Exception as exc:
            self.result.emit({"ok": False, "error": str(exc)})


class ScreenScraperSuggestionWorker(QThread):
    result = pyqtSignal(object)

    def __init__(self, username: str, password: str, system_id: int, search_name: str, preferred_region: str):
        super().__init__()
        self.username = username
        self.password = password
        self.system_id = system_id
        self.search_name = search_name
        self.preferred_region = preferred_region

    def run(self):
        try:
            client = ScreenScraperClient(self.username, self.password)
            items = client.search_game_suggestions(self.system_id, self.search_name, self.preferred_region)
            self.result.emit({"ok": True, "items": items})
        except Exception as exc:
            self.result.emit({"ok": False, "error": str(exc)})


class ModernBoxartLoadWorker(QThread):
    result = pyqtSignal(object)

    def __init__(self, items: list[tuple[str, str]]):
        super().__init__()
        self.items = items

    def run(self):
        loaded = {}
        for key, path_text in self.items:
            try:
                image = QImage(path_text)
                if not image.isNull():
                    loaded[key] = image
            except Exception:
                pass
        self.result.emit(loaded)


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
        self.apply_theme()

        self.font = QFont("Consolas", 20)
        self.font.setStyleHint(QFont.StyleHint.Monospace)
        self.title_font = QFont("Consolas", 22, QFont.Weight.Bold)
        self.title_font.setStyleHint(QFont.StyleHint.Monospace)

    def apply_theme(self):
        theme = self.window.current_theme
        self.panel = theme.color("menu_color")
        self.light = theme.color("highlight_color")
        self.text = theme.color("text_color")
        self.dark_text = theme.color("highlight_text_color")
        self.overlay = theme.color("osd_overlay_color")
        self.dialog = theme.color("dialog_alt_color")

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
        self.window.set_controller_active_input()
        if self.confirmation_active:
            if self.confirmation_selected == 1:
                self.window.close_active_session(force=True); self.close_confirmation(); self.window.hide_ingame_osd(resume=False)
            else: self.close_confirmation()
        else: self.activate_selected()

    def controller_back(self):
        self.window.set_controller_active_input()
        if self.confirmation_active: self.close_confirmation()
        else: self.window.hide_ingame_osd(resume=True)

    def controller_horizontal(self):
        self.window.set_controller_active_input()
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
        self.window.set_keyboard_active_input()
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

    def confirmation_guide_text(self) -> str:
        if self.window.active_input == "keyboard":
            return "Left/Right Select   Enter Confirm   Esc Back"
        return "D-pad Select   A Confirm   B Back"

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        painter.fillRect(self.rect(), self.overlay)

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
            painter.setFont(self.font)
            guide_text = self.confirmation_guide_text()
            guide_width = painter.fontMetrics().horizontalAdvance(guide_text) + 64
            box_width = min(max(700, guide_width), panel.width() - 40)
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
            painter.fillRect(box, self.dialog)
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
            painter.drawText(guide_rect, Qt.AlignmentFlag.AlignCenter, guide_text)


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
        self.themes_dir = base_dir / "themes"
        self.settings_path = self.config_dir / "settings.json"
        self.recent_path = self.config_dir / "recent.json"
        self.favorites_path = self.config_dir / "favorites.json"
        self.menu_root.mkdir(exist_ok=True)
        self.config_dir.mkdir(exist_ok=True)

        self.theme_manager = ThemeManager(base_dir)
        self.current_theme = self.theme_manager.load_theme(DEFAULT_THEME_ID)
        self.theme_picker_items: list[str] = []
        self.theme_picker_infos = []

        self.settings = self.load_settings()
        self.apply_selected_theme(save_if_invalid=False)
        self.arcade_name_database = ArcadeNameDatabase(self.assets_dir / "databases" / "arcade_names.json")
        self.metadata_cache = MetadataCache(self.base_dir)
        self.scrape_worker: ScrapeWorker | None = None
        self.scrape_index_worker: ScrapeJobIndexWorker | None = None
        self.scrape_quota_worker: ScreenScraperQuotaWorker | None = None
        self.scrape_target_items: list[str] = []
        self.scrape_target = "All Systems"
        self.scrape_mode = "Unscraped Only"
        self.scrape_region = str(self.settings.get("scrape_region", "Same as Game"))
        self.scrape_progress_lines: list[str] = []
        self.scrape_progress_text = ""
        self.scrape_status_text = ""
        self.scrape_status_complete_until = 0.0
        self.scrape_return_mode = "settings"
        self.scrape_progress_window_open = False
        self.scrape_stop_requested = False
        self.screenscraper_quota_text = "User Quota: Loading..."
        self.screenscraper_login_validated = False
        self.screenscraper_login_error = ""
        self.single_scrape_identity: GameMetadataIdentity | None = None
        self.single_scrape_system_id = 0
        self.single_scrape_action = "Scrape"
        self.single_scrape_use_custom = False
        self.single_scrape_custom_name = ""
        self.single_scrape_region = "Same as Game"
        self.single_scrape_restore_key: tuple[str, str] | None = None
        self.pending_rom_selection_key: tuple[str, str] | None = None
        self.scrape_match_items: list[dict] = []
        self.scrape_match_worker: ScreenScraperSuggestionWorker | None = None
        self.scrape_match_loading = False
        self.scrape_match_status = ""
        self.scrape_match_return_mode = "single_scrape"
        self.region_picker_return_mode = "scrape_settings"
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
        self.item_options_game_data: dict | None = None
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
        self.rom_has_game_entries = False
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
        self.local_ip_address, self.cached_network_icon = self.resolve_local_network_state()
        self.cached_bluetooth_enabled = self.resolve_bluetooth_enabled()

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

    def resolve_local_network_state(self) -> tuple[str, str]:
        address = self.resolve_local_ip_address()
        if address == "Not connected":
            return address, "wifi"

        interface_name = self.interface_name_for_address(address)
        icon = self.network_icon_for_interface(interface_name)
        return address, icon

    def interface_name_for_address(self, address: str) -> str:
        if not address or address == "Not connected" or psutil is None:
            return ""

        try:
            for interface_name, addresses in psutil.net_if_addrs().items():
                for interface_address in addresses:
                    if getattr(interface_address, "family", None) == socket.AF_INET and getattr(interface_address, "address", "") == address:
                        return interface_name
        except Exception:
            return ""

        return ""

    def macos_hardware_port_devices(self) -> dict[str, str]:
        if platform.system() != "Darwin":
            return {}

        try:
            result = subprocess.run(
                ["networksetup", "-listallhardwareports"],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
        except Exception:
            return {}

        devices: dict[str, str] = {}
        port_name = ""
        for raw_line in result.stdout.splitlines():
            line = raw_line.strip()
            if line.startswith("Hardware Port:"):
                port_name = line.split(":", 1)[1].strip().lower()
            elif line.startswith("Device:") and port_name:
                device_name = line.split(":", 1)[1].strip()
                if device_name:
                    devices[device_name] = port_name
                port_name = ""

        return devices

    def network_icon_for_interface(self, interface_name: str) -> str:
        interface_key = (interface_name or "").lower()
        macos_ports = self.macos_hardware_port_devices()
        hardware_port = macos_ports.get(interface_name, "").lower() if interface_name else ""
        label = f"{interface_key} {hardware_port}".strip()

        wifi_markers = ("wifi", "wi-fi", "wireless", "airport", "wlan", "wl")
        ethernet_markers = ("ethernet", "lan", "gbe", "thunderbolt ethernet", "usb 10", "usb ethernet", "enx", "eth")

        if any(marker in label for marker in wifi_markers):
            return "wifi"
        if any(marker in label for marker in ethernet_markers):
            return "lan"

        if platform.system() == "Darwin" and interface_name:
            active_ethernet = any(
                self.interface_is_active(device_name) and any(marker in port_name.lower() for marker in ethernet_markers)
                for device_name, port_name in macos_ports.items()
            )
            if not active_ethernet:
                return "wifi"

        return "lan"

    def interface_is_active(self, interface_name: str) -> bool:
        if not interface_name or psutil is None:
            return False

        try:
            stats = psutil.net_if_stats().get(interface_name)
            if stats is not None and not stats.isup:
                return False
            for interface_address in psutil.net_if_addrs().get(interface_name, []):
                if getattr(interface_address, "family", None) == socket.AF_INET:
                    address = getattr(interface_address, "address", "")
                    if address and not address.startswith("127."):
                        return True
        except Exception:
            return False

        return False

    def resolve_bluetooth_enabled(self) -> bool:
        system_name = platform.system()
        if system_name == "Darwin":
            return self.macos_bluetooth_enabled()
        if system_name == "Linux":
            return self.linux_bluetooth_enabled()
        if system_name == "Windows":
            return self.windows_bluetooth_enabled()
        return True

    def macos_bluetooth_enabled(self) -> bool:
        try:
            result = subprocess.run(
                ["ioreg", "-r", "-c", "IOBluetoothHCIController", "-d", "1"],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
            output = result.stdout or ""
            power_match = re.search(r'"(?:BluetoothPowerState|ControllerPowerState)"\s*=\s*(\d+)', output)
            if power_match:
                return power_match.group(1) != "0"
        except Exception:
            pass

        try:
            result = subprocess.run(
                ["system_profiler", "SPBluetoothDataType"],
                capture_output=True,
                text=True,
                timeout=4,
                check=False,
            )
            output = result.stdout or ""
            power_match = re.search(r"Bluetooth Power:\s*(On|Off)", output, re.IGNORECASE)
            if power_match:
                return power_match.group(1).lower() == "on"
            state_match = re.search(r"State:\s*(On|Off)", output, re.IGNORECASE)
            if state_match:
                return state_match.group(1).lower() == "on"
        except Exception:
            pass

        return True

    def linux_bluetooth_enabled(self) -> bool:
        try:
            result = subprocess.run(
                ["rfkill", "-J"],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
            data = json.loads(result.stdout or "{}")
            bluetooth_devices = [
                device
                for device in data.get("rfkilldevices", [])
                if str(device.get("type", "")).lower() == "bluetooth"
            ]
            if bluetooth_devices:
                return any(
                    not bool(device.get("soft")) and not bool(device.get("hard"))
                    for device in bluetooth_devices
                )
        except Exception:
            pass

        try:
            result = subprocess.run(
                ["hciconfig"],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
            output = result.stdout or ""
            if "UP RUNNING" in output:
                return True
            if "DOWN" in output:
                return False
        except Exception:
            pass

        return True

    def windows_bluetooth_enabled(self) -> bool:
        try:
            result = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    "Get-PnpDevice -Class Bluetooth | Select-Object -ExpandProperty Status",
                ],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
            statuses = [line.strip().lower() for line in result.stdout.splitlines() if line.strip()]
            if statuses:
                return any(status == "ok" for status in statuses)
        except Exception:
            pass

        return True

    def refresh_local_ip_address(self):
        address, icon = self.resolve_local_network_state()
        bluetooth_enabled = self.resolve_bluetooth_enabled()
        if (
            address != self.local_ip_address
            or icon != self.cached_network_icon
            or bluetooth_enabled != self.cached_bluetooth_enabled
        ):
            self.local_ip_address = address
            self.cached_network_icon = icon
            self.cached_bluetooth_enabled = bluetooth_enabled
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
            "theme": DEFAULT_THEME_ID,
            "game_view": "classic",
            "modern_view": "detailed",
            "group_multi_disc_games": False,
            "screenscraper_username": "",
            "screenscraper_password": "",
            "scrape_region": "Same as Game",
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
        if self.current_launcher:
            items = self.grouped_multi_disc_items(self.current_launcher, items, self.current_rom_folder.name if self.current_rom_folder else "")
            items = self.sort_rom_items_for_current_view(items)
        self.rom_items = items
        self.rom_has_game_entries = any(not item.is_dir for item in items)
        self.refresh_rom_labels()
        self.rom_loading = False

    def rom_item_selection_key(self, launcher: LauncherConfig, item: RomBrowserItem) -> tuple[str, str]:
        try:
            launcher_key = str(launcher.path.relative_to(self.menu_root)).replace(chr(92), "/").lower()
        except Exception:
            launcher_key = str(launcher.path).replace(chr(92), "/").lower()

        try:
            rom_key = str(item.path.resolve()).replace(chr(92), "/").lower()
        except Exception:
            rom_key = str(item.path).replace(chr(92), "/").lower()

        return launcher_key, rom_key

    def sort_name_for_rom_item(self, item: RomBrowserItem) -> str:
        if not self.current_launcher:
            return item.display_name.lower()
        return self.metadata_display_name_for_rom_item(self.current_launcher, item).lower()

    def sort_rom_items_for_current_view(self, items: list[RomBrowserItem]) -> list[RomBrowserItem]:
        if not self.modern_mode_enabled():
            return items

        normal_folders = [item for item in items if item.is_dir and not item.is_multi_disc_group]
        games = [item for item in items if not (item.is_dir and not item.is_multi_disc_group)]
        normal_folders.sort(key=lambda item: item.display_name.lower())
        games.sort(key=lambda item: self.sort_name_for_rom_item(item))
        return normal_folders + games

    def restore_pending_rom_selection(self) -> bool:
        if not self.current_launcher or not self.pending_rom_selection_key:
            return False
        for index, item in enumerate(self.rom_items):
            if self.rom_item_selection_key(self.current_launcher, item) == self.pending_rom_selection_key:
                self.selected_index = index + 1
                self.view.ensure_visible()
                self.pending_rom_selection_key = None
                return True
        self.pending_rom_selection_key = None
        return False

    def refresh_rom_labels(self):
        labels = [("...", "<DIR>")]
        index = {}
        use_index = False
        if self.current_launcher and self.modern_mode_enabled():
            system = self.current_launcher.system.strip()
            if self.metadata_supported_system(system):
                index = self.metadata_cache.load_index(system)
                use_index = bool(index)

        for item in self.rom_items:
            name = item.display_name
            if self.current_launcher and not item.is_dir and use_index:
                identity = self.game_metadata_identity(self.current_launcher, item.path)
                if identity:
                    entry = index.get(self.metadata_cache.cache_key(identity))
                    if isinstance(entry, dict):
                        scraped_name = str(entry.get("scrape_name", "")).strip()
                        if scraped_name:
                            name = scraped_name
            labels.append((name, item.marker))
        self.rom_labels = labels

    def clear_rom_items(self):
        self.rom_items = []
        self.rom_labels = []
        self.rom_has_game_entries = False
        self.rom_loading = False

    def show_rom_loading(self):
        self.rom_items = []
        self.rom_has_game_entries = False
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
            if not self.restore_pending_rom_selection() and reset_selection:
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
        if not self.restore_pending_rom_selection():
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

    def modern_mode_enabled(self) -> bool:
        return str(self.settings.get("game_view", "classic")).lower() == "modern"

    def game_view_label(self) -> str:
        return "Game View: Modern" if self.modern_mode_enabled() else "Game View: Classic"

    def modern_view_choices(self) -> list[tuple[str, str]]:
        return [("Detailed List", "detailed"), ("Simple List", "simple"), ("Grid", "grid")]

    def modern_view_mode(self) -> str:
        value = str(self.settings.get("modern_view", "detailed")).lower().strip()
        return value if value in {"detailed", "simple", "grid"} else "detailed"

    def modern_view_name(self) -> str:
        current = self.modern_view_mode()
        for name, value in self.modern_view_choices():
            if value == current:
                return name
        return "Detailed List"

    def modern_view_label(self) -> str:
        return f"Modern View: {self.modern_view_name()}"

    def modern_simple_list_enabled(self) -> bool:
        return self.modern_view_mode() == "simple"

    def modern_grid_enabled(self) -> bool:
        return self.modern_view_mode() == "grid"

    def modern_grid_active(self) -> bool:
        return self.modern_game_list_active() and self.modern_grid_enabled()

    def multi_disc_grouping_enabled(self) -> bool:
        return bool(self.settings.get("group_multi_disc_games", False))

    def multi_disc_grouping_label(self) -> str:
        return self.setting_state_label("Group Multi-Disc Games", self.multi_disc_grouping_enabled())

    def multi_disc_marker_info(self, name: str) -> tuple[str, int] | None:
        stem = Path(str(name)).stem
        patterns = (
            r"(?i)(?P<base>.*?)(?:[\s._-]*[\(\[]?\s*(?:disc|disk|cd)\s*0?(?P<num>\d+)\s*[\)\]]?)(?P<tail>.*)$",
            r"(?i)(?P<base>.*?)(?:[\s._-]+(?:d)0?(?P<num>\d+)(?:[\s._-]|$))(?P<tail>.*)$",
        )
        for pattern in patterns:
            match = re.match(pattern, stem)
            if not match:
                continue
            try:
                disc_number = int(match.group("num"))
            except Exception:
                continue
            base = str(match.group("base") or "").strip()
            tail = str(match.group("tail") or "").strip()
            base = re.sub(r"[\s._-]+$", "", base).strip()
            tail = re.sub(r"^[\s._-]+", "", tail).strip()
            cleaned = f"{base} {tail}".strip() if tail else base
            cleaned = re.sub(r"\s+", " ", cleaned.replace("_", " ")).strip()
            cleaned = re.sub(r"\s*\(\s*\)\s*", " ", cleaned).strip()
            if cleaned:
                return cleaned, disc_number
        return None

    def multi_disc_group_key(self, display_name: str, fallback_folder: str = "") -> str:
        name = str(display_name or "").strip() or str(fallback_folder or "").strip()
        scrape_name = cleaned_scrape_name(name)
        key = re.sub(r"[^a-z0-9]+", "", scrape_name.lower())
        if not key and fallback_folder:
            key = re.sub(r"[^a-z0-9]+", "", cleaned_scrape_name(fallback_folder).lower())
        return key

    def disc_display_label(self, path: Path, fallback_index: int) -> str:
        info = self.multi_disc_marker_info(path.name)
        if info:
            return f"Disc {info[1]}"
        return f"Disc {fallback_index}"

    def make_multi_disc_item(self, display_name: str, scrape_name: str, members: list[tuple[int, RomBrowserItem]]) -> RomBrowserItem | None:
        if len(members) < 2:
            return None
        members = sorted(members, key=lambda pair: (pair[0], pair[1].name.lower()))
        primary = members[0][1]
        paths = [item.path for _, item in members]
        labels = [self.disc_display_label(item.path, index + 1) for index, (_, item) in enumerate(members)]
        return RomBrowserItem(
            display_name,
            primary.path,
            False,
            display_name,
            paths,
            labels,
            scrape_name or cleaned_scrape_name(display_name),
        )

    def grouped_multi_disc_items(self, launcher: LauncherConfig, items: list[RomBrowserItem], folder_name: str = "", force: bool = False) -> list[RomBrowserItem]:
        if not self.multi_disc_grouping_enabled() or (not force and not self.modern_mode_enabled()):
            return items

        result: list[RomBrowserItem] = []
        loose_groups: dict[str, dict] = {}

        for item in items:
            if item.is_dir:
                grouped_folder = self.multi_disc_item_for_folder(launcher, item)
                result.append(grouped_folder or item)
                continue

            result.append(item)
            info = self.multi_disc_marker_info(item.name)
            if not info:
                continue
            base_name, disc_number = info
            key = self.multi_disc_group_key(base_name, folder_name)
            if not key:
                continue
            group = loose_groups.setdefault(key, {"display": base_name, "members": []})
            group["members"].append((disc_number, item))

        emitted_groups: set[str] = set()
        final: list[RomBrowserItem] = []
        for item in result:
            if item.is_dir or item.is_multi_disc_group:
                final.append(item)
                continue
            info = self.multi_disc_marker_info(item.name)
            key = self.multi_disc_group_key(info[0], folder_name) if info else ""
            group = loose_groups.get(key) if key else None
            if group and len(group.get("members", [])) >= 2:
                if key not in emitted_groups:
                    display = str(group.get("display") or item.display_name).strip()
                    grouped = self.make_multi_disc_item(display, cleaned_scrape_name(display), group.get("members", []))
                    if grouped:
                        final.append(grouped)
                    else:
                        final.append(item)
                    emitted_groups.add(key)
                continue
            final.append(item)

        return final

    def multi_disc_item_for_folder(self, launcher: LauncherConfig, folder_item: RomBrowserItem) -> RomBrowserItem | None:
        try:
            child_items = self.scan_launcher_folder(launcher, folder_item.path)
        except Exception:
            return None

        groups: dict[str, list[tuple[int, RomBrowserItem]]] = {}
        non_disc_files = 0
        for child in child_items:
            if child.is_dir:
                continue
            info = self.multi_disc_marker_info(child.name)
            if not info:
                non_disc_files += 1
                continue
            base_name, disc_number = info
            key = self.multi_disc_group_key(base_name, folder_item.name)
            if not key:
                continue
            groups.setdefault(key, []).append((disc_number, child))

        valid_groups = [(key, members) for key, members in groups.items() if len(members) >= 2]
        if len(valid_groups) != 1 or non_disc_files > 0:
            return None

        _, members = valid_groups[0]
        display_name = folder_item.display_name or folder_item.name
        return self.make_multi_disc_item(display_name, cleaned_scrape_name(display_name), members)

    def grouped_multi_disc_game_dicts(self, games: list[dict]) -> list[dict]:
        if not self.modern_mode_enabled() or not self.multi_disc_grouping_enabled():
            return games

        groups: dict[tuple[str, str, str], dict] = {}
        for item in games:
            rom = item.get("rom")
            launcher = item.get("launcher")
            if rom is None or not isinstance(launcher, LauncherConfig):
                continue
            path = Path(rom)
            info = self.multi_disc_marker_info(path.name)
            if not info:
                continue
            base_name, disc_number = info
            key = (str(launcher.path).replace(chr(92), "/").lower(), str(path.parent).replace(chr(92), "/").lower(), self.multi_disc_group_key(base_name, path.parent.name))
            if not key[2]:
                continue
            group = groups.setdefault(key, {"display": base_name, "members": []})
            group["members"].append((disc_number, item))

        emitted: set[tuple[str, str, str]] = set()
        output: list[dict] = []
        for item in games:
            rom = item.get("rom")
            launcher = item.get("launcher")
            if rom is None or not isinstance(launcher, LauncherConfig):
                output.append(item)
                continue
            path = Path(rom)
            info = self.multi_disc_marker_info(path.name)
            key = (str(launcher.path).replace(chr(92), "/").lower(), str(path.parent).replace(chr(92), "/").lower(), self.multi_disc_group_key(info[0], path.parent.name)) if info else ("", "", "")
            group = groups.get(key)
            if group and len(group.get("members", [])) >= 2:
                if key not in emitted:
                    members = sorted(group.get("members", []), key=lambda pair: (pair[0], str(pair[1].get("name", "")).lower()))
                    primary = dict(members[0][1])
                    paths = [Path(member.get("rom")) for _, member in members if member.get("rom") is not None]
                    display = str(group.get("display") or primary.get("name") or Path(primary.get("rom", "")).stem).strip()
                    primary["name"] = display
                    primary["rom"] = paths[0] if paths else primary.get("rom")
                    primary["multi_disc_paths"] = paths
                    primary["multi_disc_names"] = [self.disc_display_label(path, index + 1) for index, path in enumerate(paths)]
                    primary["scrape_name"] = cleaned_scrape_name(display)
                    output.append(primary)
                    emitted.add(key)
                continue
            output.append(item)
        return output

    def screenscraper_username(self) -> str:
        return str(self.settings.get("screenscraper_username", "")).strip()

    def screenscraper_password(self) -> str:
        return str(self.settings.get("screenscraper_password", "")).strip()

    def screenscraper_account_ready(self) -> bool:
        return bool(self.screenscraper_username() and self.screenscraper_password())

    def screenscraper_account_label(self) -> str:
        return "ScreenScraper Account: Set" if self.screenscraper_account_ready() else "ScreenScraper Account: Not Set"

    def require_screenscraper_account(self) -> bool:
        if self.screenscraper_account_ready():
            return True
        self.show_message("ScreenScraper Account", "Enter your ScreenScraper username and password before scraping.")
        return False

    def screenscraper_login_error_message(self, error: str) -> str:
        detail = str(error or "").strip()
        if not detail:
            detail = "ScreenScraper rejected the account login."
        return (
            "ScreenScraper Login Failed\n\n"
            f"{detail}\n\n"
            "Please check your ScreenScraper username and password. "
            "ScreenScraper passwords should be alphanumeric."
        )

    def validate_screenscraper_login_now(self, show_error: bool = True) -> bool:
        if not self.require_screenscraper_account():
            return False
        try:
            client = ScreenScraperClient(self.screenscraper_username(), self.screenscraper_password())
            quota = client.quota_info()
            self.screenscraper_login_validated = True
            self.screenscraper_login_error = ""
            if isinstance(quota, dict) and quota:
                self.screenscraper_quota_text = self.format_quota_text(quota)
            return True
        except Exception as exc:
            self.screenscraper_login_validated = False
            self.screenscraper_login_error = str(exc)
            self.screenscraper_quota_text = "User Quota: Login failed"
            if show_error:
                self.show_message("ScreenScraper Login Failed", self.screenscraper_login_error_message(str(exc)), scrollable=True)
            else:
                self.view.update()
            return False

    def validate_screenscraper_login_after_account_edit(self):
        if not self.screenscraper_username() or not self.screenscraper_password():
            self.screenscraper_login_validated = False
            self.screenscraper_login_error = ""
            self.screenscraper_quota_text = "User Quota: Not logged in"
            self.view.update()
            return
        self.validate_screenscraper_login_now(show_error=True)
        self.view.update()

    def metadata_supported_system(self, system: str) -> bool:
        return system.strip() in ZAPAROO_SYSTEM_NAMES and system.strip() in SCREENSCRAPER_SYSTEM_IDS

    def game_metadata_identity(self, launcher: LauncherConfig | None, rom: Path | None) -> GameMetadataIdentity | None:
        if launcher is None or rom is None:
            return None
        system = launcher.system.strip()
        if not self.metadata_supported_system(system):
            return None
        try:
            launcher_rel = str(launcher.path.relative_to(self.menu_root)).replace(chr(92), "/")
        except Exception:
            launcher_rel = str(launcher.path).replace(chr(92), "/")
        try:
            rom_path = str(rom if rom.is_absolute() else rom.absolute()).replace(chr(92), "/")
        except Exception:
            rom_path = str(rom).replace(chr(92), "/")
        return GameMetadataIdentity(system, launcher_rel, rom_path, rom.name, rom.stem)

    def metadata_for_identity(self, identity: GameMetadataIdentity | None) -> dict | None:
        return self.metadata_cache.load(identity) if identity is not None else None

    def display_name_for_identity(self, fallback: str, identity: GameMetadataIdentity | None) -> str:
        if self.modern_mode_enabled():
            entry = self.metadata_cache.index_entry(identity)
            if entry:
                name = str(entry.get("scrape_name", "")).strip()
                if name:
                    return name
        return fallback

    def current_view_has_game_entries(self) -> bool:
        if self.mode == "roms":
            return self.rom_has_game_entries
        if self.mode == "favorites":
            return bool(self.favorite_items)
        if self.mode == "recent":
            return bool(self.recent_items)
        if self.mode == "system_launchers":
            return any(item.get("rom") is not None for item in self.game_system_games.get(self.current_game_system or "", []))
        if self.mode == "search_results":
            return any(item.get("type") == "game" and item.get("rom") is not None for item in self.search_items)
        return False

    def modern_game_list_active(self) -> bool:
        return (
            self.modern_mode_enabled()
            and self.mode in {"roms", "favorites", "recent", "system_launchers", "search_results"}
            and self.current_view_has_game_entries()
        )

    def display_name_for_game_dict(self, item: dict) -> str:
        fallback = str(item.get("name", "")).strip()
        launcher = item.get("launcher")
        rom = item.get("rom")
        if isinstance(launcher, LauncherConfig) and rom is not None:
            return self.display_name_for_identity(fallback, self.game_metadata_identity(launcher, Path(rom)))
        return fallback

    def metadata_display_name_for_rom_item(self, launcher: LauncherConfig, item: RomBrowserItem) -> str:
        return self.display_name_for_identity(item.display_name, self.game_metadata_identity(launcher, item.path))

    def selected_game_identity(self) -> GameMetadataIdentity | None:
        data = self.selected_game_data()
        if not data:
            return None
        return self.game_metadata_identity(data.get("launcher"), data.get("rom"))

    def metadata_summary_message(self, identity: GameMetadataIdentity, data: dict | None) -> str:
        metadata_lines = []
        system = str(identity.system or "").strip()
        if system:
            metadata_lines.append(f"System: {system}")
        if data:
            fields = [
                ("Year", data.get("year", "")),
                ("Genre", data.get("genre", "")),
                ("Developer", data.get("developer", "")),
                ("Publisher", data.get("publisher", "")),
                ("Players", data.get("players", "")),
            ]
            for label, value in fields:
                text = str(value or "").strip()
                if text:
                    metadata_lines.append(f"{label}: {text}")
        description = str((data or {}).get("description", "")).strip()
        if not description:
            description = "No summary available."
        if metadata_lines:
            return "Metadata\n" + "\n".join(metadata_lines) + "\n\nSummary\n" + description
        return "Summary\n" + description

    def open_selected_summary_overlay(self):
        if not self.modern_mode_enabled():
            return
        identity = self.selected_game_identity()
        if identity is None:
            return
        data = self.metadata_cache.load(identity)
        title = "Summary"
        if data:
            title = str(data.get("scrape_name", "")).strip() or title
        self.overlay = {
            "type": "message",
            "title": title,
            "message": self.metadata_summary_message(identity, data),
            "buttons": [("Close", lambda: None)],
            "selected": 0,
            "scrollable": True,
            "scroll_offset": 0,
        }
        self.view.update()

    def favorite_item_from_game_data(self, data: dict | None) -> dict | None:
        if not data:
            return None
        launcher = data.get("launcher")
        rom = data.get("rom")
        if not isinstance(launcher, LauncherConfig) or rom is None:
            return None
        try:
            launcher_rel = str(launcher.path.relative_to(self.menu_root)).replace(chr(92), "/")
        except Exception:
            launcher_rel = str(launcher.path).replace(chr(92), "/")
        name = str(data.get("scrape_name") or "").strip() or self.display_name_for_rom(launcher, Path(rom))
        return {
            "name": name,
            "launcher": launcher_rel,
            "rom": str(Path(rom)).replace(chr(92), "/"),
        }

    def game_data_is_favorite(self, data: dict | None) -> bool:
        item = self.favorite_item_from_game_data(data)
        return bool(item and self.item_identity_key(item) in self.favorite_item_keys)

    def toggle_favorite_for_game_data(self, data: dict | None):
        item = self.favorite_item_from_game_data(data)
        if not item:
            return
        favorites = self.load_favorite_items()
        item_key = self.item_identity_key(item)
        updated = []
        removed = False
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

    def game_data_for_index(self, index: int) -> dict | None:
        if index <= 0:
            return None
        if self.mode == "roms" and self.current_launcher:
            idx = index - 1
            if 0 <= idx < len(self.rom_items):
                item = self.rom_items[idx]
                if not item.is_dir:
                    data = {"launcher": self.current_launcher, "rom": item.path, "name": item.display_name}
                    if item.is_multi_disc_group:
                        data["multi_disc_paths"] = item.multi_disc_paths or []
                        data["multi_disc_names"] = item.multi_disc_names or []
                        data["scrape_name"] = item.multi_disc_scrape_name or cleaned_scrape_name(item.display_name)
                    return data
        if self.mode == "favorites":
            idx = index - 1
            if 0 <= idx < len(self.favorite_items):
                return self.game_data_from_saved_item(self.favorite_items[idx])
        if self.mode == "recent":
            idx = index - 1
            if 0 <= idx < len(self.recent_items):
                return self.game_data_from_saved_item(self.recent_items[idx])
        if self.mode == "system_launchers":
            games = self.game_system_games.get(self.current_game_system or "", [])
            idx = index - 1
            if 0 <= idx < len(games):
                item = games[idx]
                if item.get("rom") is not None:
                    return {"launcher": item.get("launcher"), "rom": item.get("rom"), "name": str(item.get("name", ""))}
        if self.mode == "search_results":
            idx = index - 1
            if 0 <= idx < len(self.search_items):
                item = self.search_items[idx]
                if item.get("type") == "game" and item.get("rom") is not None:
                    return {"launcher": item.get("launcher"), "rom": item.get("rom"), "name": str(item.get("name", ""))}
        return None

    def selected_game_data(self) -> dict | None:
        return self.game_data_for_index(self.selected_index)

    def game_data_from_saved_item(self, item: dict) -> dict | None:
        launcher_rel = str(item.get("launcher", ""))
        rom = Path(str(item.get("rom", "")))
        try:
            launcher_path = self.menu_root / launcher_rel
            if launcher_path.exists():
                launcher = load_launcher(launcher_path)
                return {"launcher": launcher, "rom": rom, "name": self.display_name_for_rom(launcher, rom)}
        except Exception:
            pass
        return None

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
                fallback = self.display_name_for_rom(launcher, rom)
                return self.display_name_for_identity(fallback, self.game_metadata_identity(launcher, rom))
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

        name = selected.multi_disc_scrape_name if selected.is_multi_disc_group else self.display_name_for_rom(self.current_launcher, selected.path)
        return {
            "name": name,
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

        return self.grouped_multi_disc_game_dicts(games)

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

        deduped.sort(key=lambda item: self.display_name_for_game_dict(item).lower())
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
        return launch_application(config)

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
        ]
        if self.modern_mode_enabled():
            self.system_items.append("Scrape Artwork & Metadata")
        self.system_items.extend([
            "Settings",
            "Report Issues & Requests",
            "Support the Project",
            "Check for Updates",
            "About",
            "Exit",
        ])

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
        return ["Display", "Menu Items", "Controls", "System"]

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

        theme_label = f"Theme: {self.current_theme.name}"
        game_view_label = self.game_view_label()
        modern_view_label = self.modern_view_label()
        multi_disc_grouping_label = self.multi_disc_grouping_label()

        menu_size_label = f"Menu Size: {int(self.settings.get('fullscreen_menu_size', 100))}%"
        idle_menu_hide_label = self.idle_menu_hide_label()

        if self.settings_category == "Display":
            self.settings_items = [
                fullscreen_launch_label,
                logo_label,
                theme_label,
                "Wallpapers",
                menu_size_label,
                idle_menu_hide_label,
                game_view_label,
            ]
            if self.modern_mode_enabled():
                self.settings_items.append(modern_view_label)
                self.settings_items.append(multi_disc_grouping_label)
        elif self.settings_category == "Menu Items":
            self.settings_items = [
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

    def is_macos(self) -> bool:
        return platform.system().lower() == "darwin"

    def executable_extensions_for_platform(self) -> list[str]:
        system = platform.system().lower()
        if system == "windows":
            return [".exe", ".bat", ".cmd"]
        if system == "darwin":
            return [".app", ".command", ".sh"]
        return [".appimage", ".sh", ".desktop", ""]

    def shortcut_extensions_for_platform(self) -> list[str]:
        system = platform.system().lower()
        if system == "windows":
            return [".lnk", ".url"]
        if system == "darwin":
            return [".app", ".command", ".sh", ".webloc"]
        return [".desktop", ".sh", ".appimage", ""]

    def retroarch_core_extensions_for_platform(self) -> list[str]:
        system = platform.system().lower()
        if system == "windows":
            return [".dll"]
        if system == "darwin":
            return [".dylib"]
        return [".so"]

    def shortcut_folder_default_extensions(self) -> list[str]:
        system = platform.system().lower()
        if system == "windows":
            return [".lnk"]
        if system == "darwin":
            return [".app", ".command", ".sh", ".webloc"]
        return [".desktop", ".sh", ".appimage"]

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
            executable_extensions = self.executable_extensions_for_platform()
            if field == "App Path":
                title = "Select application"
                extensions = executable_extensions
            elif field == "Game Path":
                title = "Select game application"
                extensions = executable_extensions
            elif field == "Shortcut Path":
                title = "Select shortcut"
                extensions = self.shortcut_extensions_for_platform()
            elif self.launcher_form_data.get("type") == "RetroArch":
                title = "Select RetroArch executable"
                extensions = executable_extensions
            else:
                title = "Select emulator executable"
                extensions = executable_extensions
            self.open_file_browser(title, self.base_dir, extensions, False, lambda value: self.launcher_form_data.__setitem__('emulator', value))
            return
        if field == "RetroArch Core":
            self.open_file_browser("Select RetroArch core", self.base_dir, self.retroarch_core_extensions_for_platform(), False, lambda value: self.launcher_form_data.__setitem__('core', value))
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
                self.show_message("Missing shortcut", "Select a shortcut.")
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
                "extensions": self.shortcut_folder_default_extensions(),
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
        if self.overlay and self.overlay.get("type") == "scrape_progress" and self.bulk_scrape_active():
            self.scrape_progress_window_open = False
            self.scrape_status_text = self.active_scrape_label()
            self.overlay = None
            self.view.update()
            return
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
                        suffix = child.suffix.lower()
                        mac_app_bundle = self.is_macos() and suffix == ".app" and child.is_dir()
                        extension_allowed = (
                            not self.file_browser_extensions
                            or suffix in self.file_browser_extensions
                            or ("" in self.file_browser_extensions and child.is_file())
                        )

                        if mac_app_bundle and not self.file_browser_select_folder and extension_allowed:
                            items.append((child.name, child, False))
                        elif child.is_dir():
                            items.append((child.name, child, True))
                        elif not self.file_browser_select_folder and extension_allowed:
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
        if self.mode == "theme_picker":
            return "Theme"
        if self.mode == "modern_view_picker":
            return "Modern View"
        if self.mode == "scrape_settings":
            return "Scrape Artwork & Metadata"
        if self.mode == "scrape_target_picker":
            return "Scrape Target"
        if self.mode == "scrape_mode_picker":
            return "Scrape Mode"
        if self.mode == "scrape_region_picker":
            return "Region"
        if self.mode == "single_scrape":
            return "Scrape Game"
        if self.mode == "scrape_match_picker":
            return "Select Scrape Match"
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
        if self.mode == "theme_picker":
            self.update_theme_picker_items()
            return len(self.theme_picker_items)
        if self.mode == "modern_view_picker":
            return len(self.modern_view_choices())
        if self.mode == "scrape_settings":
            return 10
        if self.mode == "scrape_target_picker":
            return len(self.scrape_target_items)
        if self.mode == "scrape_mode_picker":
            return len(SCRAPE_MODES)
        if self.mode == "scrape_region_picker":
            return len(SCRAPE_REGIONS)
        if self.mode == "single_scrape":
            return len(self.single_scrape_labels())
        if self.mode == "scrape_match_picker":
            return len(self.scrape_match_labels())
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

    def single_scrape_labels(self) -> list[tuple[str, str]]:
        custom = "Enabled" if self.single_scrape_use_custom else "Disabled"
        labels = [(f"Use Custom Name: {custom}", "")]
        if self.single_scrape_use_custom:
            labels.append((f"Scrape Name: {self.single_scrape_custom_name}", ""))
        labels.extend([
            (f"Region: {self.single_scrape_region}", ""),
            (self.single_scrape_action, ""),
            ("Cancel", ""),
        ])
        return labels

    def scrape_match_labels(self) -> list[tuple[str, str]]:
        if self.scrape_match_loading:
            return [(self.scrape_match_status or "Fetching similar game matches...", ""), ("Cancel", "")]
        labels = [(str(item.get("label", item.get("title", "Unknown"))), "") for item in self.scrape_match_items]
        if not labels and self.scrape_match_status:
            labels.append((self.scrape_match_status, ""))
        labels.append(("Cancel", ""))
        return labels

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
        if self.mode == "theme_picker":
            self.update_theme_picker_items()
            selected = str(self.settings.get("theme", DEFAULT_THEME_ID))
            labels = []
            for info in self.theme_picker_infos:
                labels.append((info.name + ("  ✓" if info.theme_id == selected else ""), ""))
            return labels
        if self.mode == "modern_view_picker":
            selected = self.modern_view_mode()
            return [(name + ("  ✓" if value == selected else ""), "") for name, value in self.modern_view_choices()]
        if self.mode == "scrape_settings":
            start_label = self.active_scrape_label() if self.bulk_scrape_active() else "Start Scraping"
            username = self.screenscraper_username() or "Not Set"
            password = "Set" if self.screenscraper_password() else "Not Set"
            return [
                (self.screenscraper_quota_text, ""),
                ("", ""),
                (f"Username: {username}", ""),
                (f"Password: {password}", ""),
                ("", ""),
                (f"Target: {self.scrape_target}", ""),
                (f"Mode: {self.scrape_mode}", ""),
                (f"Region: {self.scrape_region}", ""),
                (start_label, ""),
                ("Cancel", ""),
            ]
        if self.mode == "scrape_target_picker":
            return [(target + ("  ✓" if target == self.scrape_target else ""), "") for target in self.scrape_target_items]
        if self.mode == "scrape_mode_picker":
            return [(mode + ("  ✓" if mode == self.scrape_mode else ""), "") for mode in SCRAPE_MODES]
        if self.mode == "scrape_region_picker":
            current = self.single_scrape_region if self.region_picker_return_mode == "single_scrape" else self.scrape_region
            return [(region + ("  ✓" if region == current else ""), "") for region in SCRAPE_REGIONS]
        if self.mode == "single_scrape":
            return self.single_scrape_labels()
        if self.mode == "scrape_match_picker":
            return self.scrape_match_labels()
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
            return [("...", "<DIR>")] + [(self.display_name_for_game_dict(item) if item.get("type") == "game" else str(item.get("name", "")), str(item.get("marker", ""))) for item in self.search_items]
        if self.mode == "favorites":
            return [("...", "<DIR>")] + [(self.display_name_for_saved_game_item(item), "") for item in self.favorite_items]
        if self.mode == "recent":
            return [("...", "<DIR>")] + [(self.display_name_for_saved_game_item(item), "") for item in self.recent_items]
        if self.mode == "systems":
            return [("...", "<DIR>")] + [(name, "") for name in self.game_system_items]
        if self.mode == "system_launchers":
            games = self.game_system_games.get(self.current_game_system or "", [])
            return [("...", "<DIR>")] + [(self.display_name_for_game_dict(item), "") for item in games]
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
        self.item_options_game_data = None
        if self.modern_mode_enabled():
            game_data = self.selected_game_data()
            if game_data:
                identity = self.game_metadata_identity(game_data.get("launcher"), game_data.get("rom"))
                scrape_label = "Rescrape" if self.metadata_cache.exists(identity) else "Scrape"
                fav_label = "Remove Favorite" if self.mode == "favorites" else ("Unfavorite" if self.game_data_is_favorite(game_data) else "Favorite")
                self.item_options_game_data = game_data
                self.item_options_return_mode = self.mode
                self.item_options_return_index = self.selected_index
                self.item_options_items = ["Launch", fav_label, scrape_label, "Cancel"]
                self.mode = "item_options"
                self.selected_index = 0
                self.view.scroll_offset = 0
                self.view.update()
                return

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
        self.item_options_game_data = None
        self.item_options_items = []
        self.view.ensure_visible()
        self.view.update()

    def activate_item_option(self):
        if self.selected_index >= len(self.item_options_items):
            self.close_item_options()
            return

        action = self.item_options_items[self.selected_index]

        if action == "Cancel":
            self.close_item_options()
            return

        if self.item_options_game_data is not None:
            return_mode = self.item_options_return_mode or "roms"
            return_index = self.item_options_return_index
            if action == "Launch":
                self.close_item_options()
                self.mode = return_mode
                self.selected_index = return_index
                self.activate_selected()
                return
            if action in {"Favorite", "Unfavorite"}:
                data = self.item_options_game_data
                self.close_item_options()
                self.mode = return_mode
                self.selected_index = return_index
                self.toggle_favorite_for_game_data(data)
                return
            if action == "Remove Favorite":
                self.close_item_options()
                self.mode = return_mode
                self.selected_index = return_index
                self.remove_selected_favorite()
                return
            if action in {"Scrape", "Rescrape"}:
                self.mode = return_mode
                self.selected_index = return_index
                self.item_options_game_data = None
                self.item_options_items = []
                self.open_single_scrape_window()
                return

        if not self.item_options_target:
            self.close_item_options()
            return

        item = self.item_options_target

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
            self.settings_category = "Display"
            self.update_settings_items()
            self.selected_index = 4
            self.view.scroll_offset = 0
            self.view.update()
            return

        if self.mode == "idle_hide_timeout":
            self.mode = "settings"
            self.settings_category = "Display"
            self.update_settings_items()
            self.selected_index = 5
            self.view.scroll_offset = 0
            self.view.update()
            return

        if self.mode == "theme_picker":
            self.mode = "settings"
            self.settings_category = "Display"
            self.update_settings_items()
            self.selected_index = 2
            self.view.scroll_offset = 0
            self.view.update()
            return

        if self.mode in {"scrape_settings", "scrape_target_picker", "scrape_mode_picker", "scrape_region_picker"}:
            previous_mode = self.mode
            if previous_mode == "scrape_region_picker" and self.region_picker_return_mode == "single_scrape":
                self.mode = "single_scrape"
                self.selected_index = self.single_scrape_region_index()
                self.region_picker_return_mode = "scrape_settings"
            elif previous_mode == "scrape_settings":
                self.mode = "system"
                self.update_system_items()
                target = "Scrape Artwork & Metadata"
                self.selected_index = self.system_items.index(target) if target in self.system_items else 0
            else:
                self.mode = "scrape_settings"
                self.selected_index = 5
            self.view.scroll_offset = 0
            self.view.update()
            return

        if self.mode == "scrape_match_picker":
            self.cancel_scrape_match_worker()
            self.mode = "single_scrape"
            self.selected_index = self.single_scrape_action_index()
            self.view.scroll_offset = 0
            self.view.update()
            return

        if self.mode == "single_scrape":
            self.mode = self.scrape_return_mode or ("roms" if self.current_launcher else "menu")
            self.view.update()
            return

        if self.mode == "wallpaper":
            self.mode = "settings"
            self.settings_category = "Display"
            self.update_settings_items()
            self.selected_index = 3
            self.view.scroll_offset = 0
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

        if self.mode == "theme_picker":
            self.activate_theme_picker_item(self.selected_index)
            return

        if self.mode == "modern_view_picker":
            self.activate_modern_view_item(self.selected_index)
            return

        if self.mode == "scrape_settings":
            self.activate_scrape_settings_item()
            return

        if self.mode == "scrape_target_picker":
            self.activate_scrape_target_picker_item()
            return

        if self.mode == "scrape_mode_picker":
            self.activate_scrape_mode_picker_item()
            return

        if self.mode == "scrape_region_picker":
            self.activate_scrape_region_picker_item()
            return

        if self.mode == "single_scrape":
            self.activate_single_scrape_item()
            return

        if self.mode == "scrape_match_picker":
            self.activate_scrape_match_picker_item()
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
                if selected.is_multi_disc_group:
                    self.show_multi_disc_picker(
                        self.current_launcher,
                        selected.display_name,
                        selected.multi_disc_paths or [selected.path],
                        selected.multi_disc_names or [],
                    )
                else:
                    self.launch_game_path(self.current_launcher, selected.path, selected.display_name)
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
        return self.grouped_multi_disc_game_dicts(games)

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

        filtered.sort(key=lambda item: self.display_name_for_game_dict(item).lower() if item.get("type") != "saved" else str(item.get("name", "")).lower())
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

    def launch_game_path(self, launcher: LauncherConfig, rom_path: Path, display_name: str):
        if launcher.launcher_type == "shortcut_folder":
            process = self.launch_shortcut_path(rom_path)
            emulator_name = launcher.system or launcher.path.stem
        else:
            process = launch_rom(launcher, rom_path)
            emulator_name = launcher.emulator_name or launcher.path.stem

        self.begin_active_session(process, "game", display_name, emulator_name)
        self.add_recent_game(launcher.path, rom_path)
        self.update_recent_items()
        self.view.update()

    def show_multi_disc_picker(self, launcher: LauncherConfig, title: str, paths: list[Path], labels: list[str] | None = None):
        clean_paths = [Path(path) for path in paths if Path(path).exists()]
        if not clean_paths:
            self.show_message("Launch failed", "No disc files were found for this game.")
            return
        if len(clean_paths) == 1:
            self.launch_game_path(launcher, clean_paths[0], title)
            return

        labels = labels or []
        buttons = []
        for index, path in enumerate(clean_paths):
            label = labels[index] if index < len(labels) and labels[index] else self.disc_display_label(path, index + 1)
            buttons.append((label, lambda p=path: self.launch_game_path(launcher, p, title)))
        buttons.append(("Back", None))
        self.show_choice(title, "Select which disc to launch.", buttons)

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
            if item.get("multi_disc_paths"):
                self.show_multi_disc_picker(
                    launcher,
                    str(item.get("name", rom_path.stem)),
                    [Path(path) for path in item.get("multi_disc_paths", [])],
                    [str(label) for label in item.get("multi_disc_names", [])],
                )
            else:
                self.launch_game_path(launcher, rom_path, str(item.get("name", rom_path.stem)))
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
        elif item.startswith("Game View:"):
            self.settings["game_view"] = "classic" if self.modern_mode_enabled() else "modern"
            self.invalidate_rom_folder_cache()
            if self.current_launcher and self.current_rom_folder:
                self.open_rom_folder(self.current_launcher, self.current_rom_folder, reset_selection=False)
            self.refresh_settings_menu()
        elif item.startswith("Group Multi-Disc Games:"):
            self.settings["group_multi_disc_games"] = not self.multi_disc_grouping_enabled()
            self.save_settings()
            self.invalidate_rom_folder_cache()
            if self.current_launcher and self.current_rom_folder:
                self.open_rom_folder(self.current_launcher, self.current_rom_folder, reset_selection=False)
            self.refresh_settings_menu()
        elif item.startswith("Modern View:"):
            self.mode = "modern_view_picker"
            choices = [value for _, value in self.modern_view_choices()]
            current = self.modern_view_mode()
            self.selected_index = choices.index(current) if current in choices else 0
            self.view.scroll_offset = 0
            self.view.update()
        elif item == "Scrape Artwork & Metadata":
            self.open_scrape_settings()
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

    def available_scrape_targets(self) -> list[str]:
        systems = []
        for launcher_path in self.menu_root.rglob("*.json"):
            try:
                launcher = load_launcher(launcher_path)
            except Exception:
                continue
            system = launcher.system.strip()
            if system and self.metadata_supported_system(system) and Path(launcher.rom_directory).is_dir():
                systems.append(system)
        unique = sorted(set(systems), key=str.lower)
        return ["All Systems"] + unique


    def format_quota_text(self, quota: dict | None = None, unavailable: bool = False) -> str:
        if not self.screenscraper_username() or not self.screenscraper_password():
            return "User Quota: Not logged in"
        if unavailable or not isinstance(quota, dict):
            return "User Quota: Unavailable"
        used = quota.get("used", quota.get("daily_used"))
        limit = quota.get("limit", quota.get("daily_limit"))
        remaining = quota.get("remaining", quota.get("daily_remaining"))
        if used is not None and limit is not None:
            return f"User Quota: {int(used):,} / {int(limit):,}"
        if used is not None:
            return f"User Quota: {int(used):,} used"
        if remaining is not None:
            return f"User Quota: {int(remaining):,} remaining"
        return "User Quota: Unavailable"

    def refresh_screenscraper_quota(self):
        if not self.screenscraper_username() or not self.screenscraper_password():
            self.screenscraper_quota_text = "User Quota: Not logged in"
            self.view.update()
            return
        if self.scrape_quota_worker is not None and self.scrape_quota_worker.isRunning():
            return
        self.screenscraper_quota_text = "User Quota: Loading..."
        self.screenscraper_login_validated = False
        self.screenscraper_login_error = ""
        self.scrape_quota_worker = ScreenScraperQuotaWorker(self.screenscraper_username(), self.screenscraper_password())
        self.scrape_quota_worker.result.connect(self.on_screenscraper_quota_result)
        self.scrape_quota_worker.finished.connect(self.on_screenscraper_quota_finished)
        self.scrape_quota_worker.start()
        self.view.update()

    def on_screenscraper_quota_result(self, result: object):
        if isinstance(result, dict) and result.get("ok"):
            self.screenscraper_login_validated = True
            self.screenscraper_login_error = ""
            self.screenscraper_quota_text = self.format_quota_text(result.get("quota"))
        else:
            error = str(result.get("error", "") if isinstance(result, dict) else "")
            if "login" in error.lower() or "credential" in error.lower() or "identifiant" in error.lower():
                self.screenscraper_login_validated = False
                self.screenscraper_login_error = error
                self.screenscraper_quota_text = "User Quota: Login failed"
            else:
                self.screenscraper_quota_text = self.format_quota_text(unavailable=True)
        self.view.update()

    def on_screenscraper_quota_finished(self):
        worker = self.scrape_quota_worker
        self.scrape_quota_worker = None
        if worker is not None:
            worker.deleteLater()

    def open_scrape_settings(self):
        self.scrape_target_items = self.available_scrape_targets()
        if self.scrape_target not in self.scrape_target_items:
            self.scrape_target = self.scrape_target_items[0] if self.scrape_target_items else "All Systems"
        if self.scrape_mode not in SCRAPE_MODES:
            self.scrape_mode = "Unscraped Only"
        self.scrape_region = str(self.settings.get("scrape_region", self.scrape_region))
        if self.scrape_region not in SCRAPE_REGIONS:
            self.scrape_region = "Same as Game"
        self.mode = "scrape_settings"
        self.selected_index = 8 if self.bulk_scrape_active() else 5
        self.view.scroll_offset = 0
        self.refresh_screenscraper_quota()
        self.view.update()

    def build_scrape_jobs(self, target: str, mode: str) -> list[dict]:
        jobs = []
        seen = set()
        for launcher_path in self.menu_root.rglob("*.json"):
            try:
                launcher = load_launcher(launcher_path)
            except Exception:
                continue
            system = launcher.system.strip()
            if not self.metadata_supported_system(system):
                continue
            if target != "All Systems" and system != target:
                continue
            root = Path(launcher.rom_directory)
            if not root.is_dir():
                continue
            folders = [root]
            visited = set()
            while folders:
                folder = folders.pop(0)
                try:
                    folder_key = str(folder.resolve()).replace(chr(92), "/").lower()
                except Exception:
                    folder_key = str(folder).replace(chr(92), "/").lower()
                if folder_key in visited:
                    continue
                visited.add(folder_key)
                for rom_item in self.grouped_multi_disc_items(launcher, self.scan_cached_rom_folder_sync(launcher, folder), folder.name, force=True):
                    if rom_item.is_dir:
                        if launcher.recursive:
                            folders.append(rom_item.path)
                        continue
                    identity = self.game_metadata_identity(launcher, rom_item.path)
                    if identity is None:
                        continue
                    key = self.metadata_cache.cache_key(identity)
                    if key in seen:
                        continue
                    seen.add(key)
                    if not self.metadata_cache.should_scrape(identity, mode):
                        continue
                    jobs.append({
                        "identity": identity,
                        "system_id": SCREENSCRAPER_SYSTEM_IDS[system],
                        "search_name": rom_item.multi_disc_scrape_name or cleaned_scrape_name(rom_item.name),
                        "preferred_region": region_from_option(self.scrape_region, identity),
                    })
        return jobs

    def set_screenscraper_username(self, value: str):
        self.settings["screenscraper_username"] = value.strip()
        self.screenscraper_login_validated = False
        self.screenscraper_login_error = ""
        self.save_settings()
        self.validate_screenscraper_login_after_account_edit()
        self.view.update()

    def set_screenscraper_password(self, value: str):
        self.settings["screenscraper_password"] = value.strip()
        self.screenscraper_login_validated = False
        self.screenscraper_login_error = ""
        self.save_settings()
        self.validate_screenscraper_login_after_account_edit()
        self.view.update()

    def activate_scrape_settings_item(self):
        if self.selected_index in (0, 1, 4):
            self.move_selection(1)
            return
        if self.selected_index == 2:
            self.open_text_input("ScreenScraper Username", "Enter your ScreenScraper username", self.screenscraper_username(), self.set_screenscraper_username)
            return
        if self.selected_index == 3:
            self.open_text_input("ScreenScraper Password", "Enter your ScreenScraper password", self.screenscraper_password(), self.set_screenscraper_password)
            return
        if self.selected_index == 5:
            self.scrape_target_items = self.available_scrape_targets()
            self.mode = "scrape_target_picker"
            self.selected_index = self.scrape_target_items.index(self.scrape_target) if self.scrape_target in self.scrape_target_items else 0
            self.view.scroll_offset = 0
            self.view.update()
            return
        if self.selected_index == 6:
            self.mode = "scrape_mode_picker"
            self.selected_index = SCRAPE_MODES.index(self.scrape_mode) if self.scrape_mode in SCRAPE_MODES else 0
            self.view.scroll_offset = 0
            self.view.update()
            return
        if self.selected_index == 7:
            self.region_picker_return_mode = "scrape_settings"
            self.mode = "scrape_region_picker"
            self.selected_index = SCRAPE_REGIONS.index(self.scrape_region) if self.scrape_region in SCRAPE_REGIONS else 0
            self.view.scroll_offset = 0
            self.view.update()
            return
        if self.selected_index == 8:
            if self.bulk_scrape_active():
                self.reopen_scrape_progress()
            else:
                self.start_bulk_scrape()
            return
        self.mode = "system"
        self.update_system_items()
        target = "Scrape Artwork & Metadata"
        self.selected_index = self.system_items.index(target) if target in self.system_items else 0
        self.view.scroll_offset = 0
        self.view.update()

    def activate_scrape_target_picker_item(self):
        if 0 <= self.selected_index < len(self.scrape_target_items):
            self.scrape_target = self.scrape_target_items[self.selected_index]
        self.mode = "scrape_settings"
        self.selected_index = 5
        self.view.scroll_offset = 0
        self.view.update()

    def activate_scrape_mode_picker_item(self):
        if 0 <= self.selected_index < len(SCRAPE_MODES):
            self.scrape_mode = SCRAPE_MODES[self.selected_index]
        self.mode = "scrape_settings"
        self.selected_index = 6
        self.view.scroll_offset = 0
        self.view.update()

    def activate_scrape_region_picker_item(self):
        if 0 <= self.selected_index < len(SCRAPE_REGIONS):
            selected = SCRAPE_REGIONS[self.selected_index]
            if self.region_picker_return_mode == "single_scrape":
                self.single_scrape_region = selected
                self.mode = "single_scrape"
                self.selected_index = self.single_scrape_region_index()
            else:
                self.scrape_region = selected
                self.settings["scrape_region"] = selected
                self.save_settings()
                self.mode = "scrape_settings"
                self.selected_index = 7
        else:
            self.mode = "single_scrape" if self.region_picker_return_mode == "single_scrape" else "scrape_settings"
            self.selected_index = self.single_scrape_region_index() if self.region_picker_return_mode == "single_scrape" else 7
        self.region_picker_return_mode = "scrape_settings"
        self.view.scroll_offset = 0
        self.view.update()

    def start_bulk_scrape(self):
        if not self.require_screenscraper_account():
            return
        if self.scrape_worker is not None and self.scrape_worker.isRunning():
            self.show_message("Scrape", "A scrape is already running.")
            return
        if self.scrape_index_worker is not None and self.scrape_index_worker.isRunning():
            self.reopen_scrape_progress()
            return
        self.scrape_return_mode = "settings"
        self.scrape_progress_lines = ["Indexing games for scraping..."]
        self.scrape_progress_text = "Indexing games for scraping..."
        self.scrape_status_text = self.scrape_progress_text
        self.scrape_status_complete_until = 0.0
        self.scrape_stop_requested = False
        self.reopen_scrape_progress()
        self.view.repaint()
        QApplication.processEvents()
        self.scrape_index_worker = ScrapeJobIndexWorker(self, self.scrape_target, self.scrape_mode)
        self.scrape_index_worker.result.connect(self.on_scrape_jobs_indexed)
        self.scrape_index_worker.start()

    def on_scrape_jobs_indexed(self, result: object):
        self.scrape_index_worker = None
        if not isinstance(result, dict) or not result.get("ok"):
            error = str(result.get("error", "") if isinstance(result, dict) else "")
            self.show_message("Scrape", f"Could not index games for scraping.\n\n{error}".strip())
            return
        jobs = result.get("jobs", [])
        if not jobs:
            self.show_message("Scrape", "No games need scraping for this target and mode.")
            return
        if self.scrape_mode == "All Games":
            self.show_choice("Scrape All Games", "This will rescrape every matching game and may use many ScreenScraper requests. Continue?", [("Cancel", None), ("Continue", lambda: self.start_scrape_worker(jobs, self.scrape_mode, "settings"))])
            return
        self.start_scrape_worker(jobs, self.scrape_mode, "settings")

    def start_scrape_worker(self, jobs: list[dict], mode: str, return_mode: str):
        self.scrape_return_mode = return_mode
        self.scrape_progress_lines = [f"Preparing {len(jobs)} game(s)..."]
        self.scrape_progress_text = f"Scraping 0 / {len(jobs)}"
        self.scrape_status_text = self.scrape_progress_text
        self.scrape_status_complete_until = 0.0
        self.scrape_stop_requested = False
        self.scrape_worker = ScrapeWorker(self.metadata_cache, jobs, mode, self.screenscraper_username(), self.screenscraper_password())
        self.scrape_worker.progress.connect(self.on_scrape_progress)
        self.scrape_worker.finished_result.connect(self.on_scrape_finished)
        self.scrape_worker.start()
        self.reopen_scrape_progress()

    def bulk_scrape_active(self) -> bool:
        return bool(
            (self.scrape_worker is not None and self.scrape_worker.isRunning())
            or (self.scrape_index_worker is not None and self.scrape_index_worker.isRunning())
        )

    def active_scrape_label(self) -> str:
        return self.scrape_progress_text or "Scraping"

    def progress_overlay_open(self) -> bool:
        return bool(self.overlay and self.overlay.get("type") == "scrape_progress")

    def show_scrape_progress_overlay(self):
        self.scrape_progress_window_open = True
        self.scrape_status_text = ""
        message = "\n".join(self.scrape_progress_lines) if self.scrape_progress_lines else self.active_scrape_label()
        self.overlay = {
            "type": "scrape_progress",
            "title": "Scrape",
            "message": message,
            "selected": 0,
            "buttons": [("Stop Scraper", self.confirm_stop_scraper)],
            "scrollable": True,
            "scroll_offset": 1000000,
        }
        self.view.update()

    def reopen_scrape_progress(self):
        self.show_scrape_progress_overlay()

    def confirm_stop_scraper(self):
        self.show_choice(
            "Stop Scraper",
            "Stop scraping?\n\nThe current scrape will stop after the current request finishes.",
            [("No", self.reopen_scrape_progress), ("Yes", self.request_stop_scraper)],
        )

    def request_stop_scraper(self):
        if self.bulk_scrape_active():
            self.scrape_stop_requested = True
            self.scrape_progress_lines.append("Stopping after current request...")
            self.scrape_progress_text = "Stopping scraper..."
            self.scrape_status_text = self.scrape_progress_text
            if self.scrape_worker is not None:
                self.scrape_worker.cancel()
            self.reopen_scrape_progress()

    def on_scrape_progress(self, info: object):
        if not isinstance(info, dict):
            return
        if info.get("type") == "quota":
            quota = info.get("quota")
            if isinstance(quota, dict) and quota:
                self.screenscraper_quota_text = self.format_quota_text(quota)
                self.view.update()
            return
        index = info.get('index', '?')
        total = info.get('total', '?')
        line = f"{index}/{total} {info.get('name', '')}: {info.get('status', '')}"
        self.scrape_progress_lines.append(line)
        self.scrape_progress_text = f"Scraping {index} / {total}"
        self.scrape_status_text = self.scrape_progress_text
        if self.progress_overlay_open():
            self.overlay["message"] = "\n".join(self.scrape_progress_lines)
            self.overlay["scrollable"] = True
            self.overlay["scroll_offset"] = 1000000
        self.view.update()

    def on_scrape_finished(self, summary: object):
        self.scrape_worker = None
        self.refresh_screenscraper_quota()
        if hasattr(self.view, "metadata_pixmap_cache"):
            self.view.metadata_pixmap_cache.clear()
        lines = []
        stopped = ""
        if isinstance(summary, dict):
            lines = [
                f"Scraped: {summary.get('scraped', 0)}",
                f"Skipped: {summary.get('skipped', 0)}",
                f"Missing: {summary.get('missing', 0)}",
                f"Failed: {summary.get('failed', 0)}",
            ]
            stopped = str(summary.get("stopped", "")).strip()
            quota_reached = bool(summary.get("quota_reached"))
            quota_message = str(summary.get("quota_message", "")).strip()
            login_failed = bool(summary.get("login_failed"))
            login_message = str(summary.get("login_message", "")).strip()
            if stopped:
                lines.append(f"Stopped: {stopped}")
            if quota_reached and quota_message:
                lines.append("")
                lines.append(quota_message)
            if login_failed and login_message:
                lines.append("")
                lines.append(self.screenscraper_login_error_message(login_message))
        else:
            quota_reached = False
            quota_message = ""
            login_failed = False
            login_message = ""
            lines = ["Scrape finished."]
        final_title = "Login failed" if login_failed else ("Quota reached" if quota_reached else ("Scraping stopped" if stopped else "Scraping complete"))
        self.scrape_progress_text = final_title
        self.scrape_status_text = final_title
        self.scrape_status_complete_until = time.monotonic() + 5.0
        if login_failed:
            self.screenscraper_login_validated = False
            self.screenscraper_login_error = login_message
            self.show_message("ScreenScraper Login Failed", "\n".join(lines), scrollable=True)
        elif quota_reached:
            self.show_message("ScreenScraper Quota Reached", "\n".join(lines), scrollable=True)
        elif self.progress_overlay_open():
            self.overlay["title"] = "Scrape Complete" if not stopped else "Scrape Stopped"
            self.overlay["type"] = "message"
            self.overlay["message"] = "\n".join(lines)
            self.overlay["scrollable"] = True
            self.overlay["scroll_offset"] = 1000000
        self.invalidate_rom_folder_cache()
        if self.current_launcher and self.current_rom_folder:
            self.open_rom_folder(self.current_launcher, self.current_rom_folder, reset_selection=False)
        self.update_recent_items()
        self.update_favorite_items()
        self.update_current_system_game_items()
        if self.mode == "roms":
            self.refresh_rom_labels()
        self.view.update()

    def open_single_scrape_window(self):
        data = self.selected_game_data()
        if not data:
            return
        launcher = data.get("launcher")
        rom = data.get("rom")
        identity = self.game_metadata_identity(launcher, rom)
        if identity is None:
            self.show_message("Scrape", "This game is not tied to a supported internal system.")
            return
        self.scrape_return_mode = self.mode
        self.single_scrape_restore_key = None
        if self.mode == "roms" and self.current_launcher and self.selected_index > 0:
            index = self.selected_index - 1
            if 0 <= index < len(self.rom_items):
                self.single_scrape_restore_key = self.rom_item_selection_key(self.current_launcher, self.rom_items[index])
        self.single_scrape_identity = identity
        self.single_scrape_system_id = SCREENSCRAPER_SYSTEM_IDS.get(identity.system, 0)
        existing = self.metadata_cache.load(identity) or {}
        self.single_scrape_action = "Rescrape" if existing else "Scrape"
        self.single_scrape_use_custom = False
        self.single_scrape_custom_name = str(existing.get("manual_scrape_name") or existing.get("scrape_name") or data.get("scrape_name") or cleaned_scrape_name(identity.rom_name))
        self.single_scrape_region = str(self.settings.get("scrape_region", "Same as Game"))
        if self.single_scrape_region not in SCRAPE_REGIONS:
            self.single_scrape_region = "Same as Game"
        self.mode = "single_scrape"
        self.selected_index = 0
        self.view.scroll_offset = 0
        self.view.update()

    def single_scrape_region_index(self) -> int:
        return 2 if self.single_scrape_use_custom else 1

    def single_scrape_action_index(self) -> int:
        return 3 if self.single_scrape_use_custom else 2

    def single_scrape_cancel_index(self) -> int:
        return 4 if self.single_scrape_use_custom else 3

    def activate_single_scrape_item(self):
        if self.selected_index == 0:
            self.single_scrape_use_custom = not self.single_scrape_use_custom
            self.selected_index = 1 if self.single_scrape_use_custom else min(self.selected_index, self.single_scrape_cancel_index())
            self.view.update()
            return
        if self.single_scrape_use_custom and self.selected_index == 1:
            self.open_text_input("Scrape Name", "Used when searching ScreenScraper", self.single_scrape_custom_name, self.set_single_scrape_custom_name)
            return
        if self.selected_index == self.single_scrape_region_index():
            self.region_picker_return_mode = "single_scrape"
            self.mode = "scrape_region_picker"
            self.selected_index = SCRAPE_REGIONS.index(self.single_scrape_region) if self.single_scrape_region in SCRAPE_REGIONS else 0
            self.view.scroll_offset = 0
            self.view.update()
            return
        if self.selected_index == self.single_scrape_action_index():
            self.start_single_scrape()
            return
        self.go_back()

    def set_single_scrape_custom_name(self, value: str):
        self.single_scrape_custom_name = value.strip() or self.single_scrape_custom_name

    def start_single_scrape(self):
        if self.single_scrape_identity is None or self.single_scrape_system_id <= 0:
            self.show_message("Scrape", "This game cannot be scraped.")
            return
        if self.single_scrape_use_custom:
            self.open_scrape_match_picker()
            return
        if not self.validate_screenscraper_login_now(show_error=True):
            return
        search_name = self.single_scrape_custom_name.strip() or cleaned_scrape_name(self.single_scrape_identity.rom_name)
        self.start_single_scrape_job(search_name, "", "")

    def open_scrape_match_picker(self):
        if not self.require_screenscraper_account():
            return
        if self.single_scrape_identity is None or self.single_scrape_system_id <= 0:
            self.show_message("Scrape", "This game cannot be scraped.")
            return
        self.cancel_scrape_match_worker()
        self.scrape_match_items = []
        self.scrape_match_loading = True
        self.scrape_match_status = "Fetching similar game matches..."
        self.mode = "scrape_match_picker"
        self.selected_index = 1
        self.view.scroll_offset = 0
        self.view.update()
        QTimer.singleShot(0, self.start_scrape_match_worker)

    def start_scrape_match_worker(self):
        if not self.scrape_match_loading or self.single_scrape_identity is None or self.single_scrape_system_id <= 0:
            return
        search_name = self.single_scrape_custom_name.strip() or cleaned_scrape_name(self.single_scrape_identity.rom_name)
        preferred_region = region_from_option(self.single_scrape_region, self.single_scrape_identity)
        worker = ScreenScraperSuggestionWorker(
            self.screenscraper_username(),
            self.screenscraper_password(),
            self.single_scrape_system_id,
            search_name,
            preferred_region,
        )
        self.scrape_match_worker = worker
        worker.result.connect(self.handle_scrape_match_result)
        worker.finished.connect(lambda: self.clear_scrape_match_worker(worker))
        worker.start()

    def clear_scrape_match_worker(self, worker: ScreenScraperSuggestionWorker):
        if self.scrape_match_worker is worker:
            self.scrape_match_worker = None

    def cancel_scrape_match_worker(self):
        self.scrape_match_loading = False
        if self.scrape_match_worker and self.scrape_match_worker.isRunning():
            self.scrape_match_worker.requestInterruption()

    def handle_scrape_match_result(self, result: object):
        if not self.scrape_match_loading:
            return
        self.scrape_match_loading = False
        if not isinstance(result, dict) or not result.get("ok"):
            self.scrape_match_items = []
            self.scrape_match_status = str((result or {}).get("error", "Unable to fetch similar game matches.")) if isinstance(result, dict) else "Unable to fetch similar game matches."
            self.selected_index = 1
        else:
            self.scrape_match_items = list(result.get("items") or [])
            self.scrape_match_status = "" if self.scrape_match_items else "No similar game matches found."
            self.selected_index = 0 if self.scrape_match_items else 1
        if self.mode == "scrape_match_picker":
            self.view.scroll_offset = 0
            self.view.update()

    def activate_scrape_match_picker_item(self):
        if self.scrape_match_loading:
            if self.selected_index >= 1:
                self.cancel_scrape_match_worker()
                self.mode = "single_scrape"
                self.selected_index = self.single_scrape_action_index()
                self.view.scroll_offset = 0
                self.view.update()
            return
        if self.selected_index >= len(self.scrape_match_items):
            self.mode = "single_scrape"
            self.selected_index = self.single_scrape_action_index()
            self.view.scroll_offset = 0
            self.view.update()
            return
        item = self.scrape_match_items[self.selected_index]
        title = str(item.get("title", self.single_scrape_custom_name)).strip() or self.single_scrape_custom_name
        game_id = str(item.get("game_id", "")).strip()
        search_name = self.single_scrape_custom_name.strip() or title
        self.start_single_scrape_job(search_name, game_id, search_name)

    def start_single_scrape_job(self, search_name: str, game_id: str = "", manual_name: str = ""):
        if self.single_scrape_identity is None or self.single_scrape_system_id <= 0:
            self.show_message("Scrape", "This game cannot be scraped.")
            return
        job = {
            "identity": self.single_scrape_identity,
            "system_id": self.single_scrape_system_id,
            "search_name": search_name,
            "manual_scrape_name": manual_name,
            "preferred_region": region_from_option(self.single_scrape_region, self.single_scrape_identity),
            "game_id": game_id,
        }
        return_mode = self.scrape_return_mode or "roms"
        if return_mode == "roms" and self.single_scrape_restore_key:
            self.pending_rom_selection_key = self.single_scrape_restore_key
        self.start_scrape_worker([job], "All Games", return_mode)
        self.mode = return_mode
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
        elif item.startswith("Theme:"):
            self.mode = "theme_picker"
            self.update_theme_picker_items()
            current = str(self.settings.get("theme", DEFAULT_THEME_ID))
            ids = [info.theme_id for info in self.theme_picker_infos]
            self.selected_index = ids.index(current) if current in ids else 0
            self.view.scroll_offset = 0
            self.view.update()
        elif item == "Wallpapers":
            self.update_wallpaper_items()
            self.mode = "wallpaper"
            self.selected_index = 0
            self.view.scroll_offset = 0
            self.view.update()
        elif item.startswith("Game View:"):
            self.settings["game_view"] = "classic" if self.modern_mode_enabled() else "modern"
            self.invalidate_rom_folder_cache()
            if self.current_launcher and self.current_rom_folder:
                self.open_rom_folder(self.current_launcher, self.current_rom_folder, reset_selection=False)
            self.refresh_settings_menu()
        elif item.startswith("Group Multi-Disc Games:"):
            self.settings["group_multi_disc_games"] = not self.multi_disc_grouping_enabled()
            self.save_settings()
            self.invalidate_rom_folder_cache()
            if self.current_launcher and self.current_rom_folder:
                self.open_rom_folder(self.current_launcher, self.current_rom_folder, reset_selection=False)
            self.refresh_settings_menu()
        elif item.startswith("Modern View:"):
            self.mode = "modern_view_picker"
            choices = [value for _, value in self.modern_view_choices()]
            current = self.modern_view_mode()
            self.selected_index = choices.index(current) if current in choices else 0
            self.view.scroll_offset = 0
            self.view.update()
        elif item == "Scrape Artwork & Metadata":
            self.open_scrape_settings()
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

    def update_theme_picker_items(self):
        self.theme_picker_infos = self.theme_manager.available_themes()
        self.theme_picker_items = [info.name for info in self.theme_picker_infos]

    def apply_selected_theme(self, save_if_invalid: bool = True):
        requested = str(self.settings.get("theme", DEFAULT_THEME_ID))
        self.current_theme = self.theme_manager.load_theme(requested)
        if self.current_theme.theme_id != requested:
            self.settings["theme"] = DEFAULT_THEME_ID
            if save_if_invalid:
                self.save_settings()
        if hasattr(self, "view"):
            self.view.apply_theme()
        if hasattr(self, "ingame_osd"):
            self.ingame_osd.apply_theme()

    def activate_theme_picker_item(self, index: int):
        self.update_theme_picker_items()
        if not (0 <= index < len(self.theme_picker_infos)):
            return
        self.settings["theme"] = self.theme_picker_infos[index].theme_id
        self.apply_selected_theme()
        self.save_settings()
        self.update_settings_items()
        self.view.scroll_offset = 0
        self.view.update()

    def activate_modern_view_item(self, index: int):
        choices = self.modern_view_choices()
        if not (0 <= index < len(choices)):
            return
        self.settings["modern_view"] = choices[index][1]
        self.save_settings()
        self.update_settings_items()
        self.view.scroll_offset = 0
        self.view.update()

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
        fallback = rom.stem
        return self.display_name_for_identity(fallback, self.game_metadata_identity(config, rom))

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
        self.request_frontend_focus_after_session()

    def request_frontend_focus_after_session(self):
        if sys.platform != "darwin":
            return
        QTimer.singleShot(150, self.activate_frontend_window)
        QTimer.singleShot(500, self.activate_frontend_window)

    def activate_frontend_window(self):
        if sys.platform != "darwin" or self.active_session_snapshot().get("running"):
            return

        try:
            self.show()
            if self.isMinimized():
                self.showNormal()
            self.raise_()
            self.activateWindow()
        except Exception:
            pass

        try:
            subprocess.Popen(
                [
                    "osascript",
                    "-e",
                    f'tell application "System Events" to set frontmost of the first process whose unix id is {os.getpid()} to true',
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass

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
            self.set_keyboard_active_input()
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
                self.set_controller_active_input()
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
        if self.overlay:
            self.activate_overlay()
        else:
            self.activate_selected()
        self.view.update()

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
        if self.mode=='text_input':
            self.text_input_caps=not self.text_input_caps
            self.view.update()
        else:
            self.open_selected_summary_overlay()

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
            if self.overlay.get("type") in ("choice", "scrape_progress") and action in ("left", "right"):
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
        if self.modern_grid_active() and action in ("left", "right", "up", "down"):
            self.move_grid_selection(self.grid_navigation_delta(action))
            return
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
                        ("osd_up", up, lambda: (self.set_controller_active_input(), self.ingame_osd.move_selection(-1))),
                        ("osd_down", down, lambda: (self.set_controller_active_input(), self.ingame_osd.move_selection(1))),
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
        if self.mode == "scrape_settings" and self.selected_index in (0, 1, 4):
            return False
        return True

    def grid_selectable_bounds(self) -> tuple[int, int]:
        count = self.current_items_count()
        if count <= 1:
            return (0, -1)
        return (1, count - 1)

    def move_grid_selection(self, delta: int):
        first, last = self.grid_selectable_bounds()
        if last < first:
            return
        if self.selected_index < first:
            self.selected_index = first
        else:
            self.selected_index = max(first, min(last, self.selected_index + delta))
        self.view.ensure_visible()
        self.view.update()

    def grid_navigation_delta(self, action: str) -> int:
        columns = max(1, self.view.modern_grid_columns())
        if action == "left":
            return -1
        if action == "right":
            return 1
        if action == "up":
            return -columns
        if action == "down":
            return columns
        return 0

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
                if self.is_current_selection_selectable():
                    break
                if self.selected_index in (0, count - 1):
                    self.move_selection(direction)
                    return
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
            if self.overlay.get("type") in ("choice", "scrape_progress") and key in (Qt.Key.Key_Left, Qt.Key.Key_Right, Qt.Key.Key_A, Qt.Key.Key_D):
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
        if self.modern_grid_active() and key in (Qt.Key.Key_Up, Qt.Key.Key_W, Qt.Key.Key_Down, Qt.Key.Key_S, Qt.Key.Key_Left, Qt.Key.Key_A, Qt.Key.Key_Right, Qt.Key.Key_D, Qt.Key.Key_PageUp, Qt.Key.Key_PageDown):
            if key in (Qt.Key.Key_Up, Qt.Key.Key_W):
                self.move_grid_selection(self.grid_navigation_delta("up"))
            elif key in (Qt.Key.Key_Down, Qt.Key.Key_S):
                self.move_grid_selection(self.grid_navigation_delta("down"))
            elif key in (Qt.Key.Key_Left, Qt.Key.Key_A):
                self.move_grid_selection(self.grid_navigation_delta("left"))
            elif key in (Qt.Key.Key_Right, Qt.Key.Key_D):
                self.move_grid_selection(self.grid_navigation_delta("right"))
            elif key == Qt.Key.Key_PageUp:
                self.move_grid_selection(-max(1, self.view.modern_grid_visible_slots()))
            elif key == Qt.Key.Key_PageDown:
                self.move_grid_selection(max(1, self.view.modern_grid_visible_slots()))
        elif key in (Qt.Key.Key_Up, Qt.Key.Key_W): self.move_selection(-1)
        elif key in (Qt.Key.Key_Down, Qt.Key.Key_S): self.move_selection(1)
        elif key in (Qt.Key.Key_Left, Qt.Key.Key_A):
            if not (self.mode == "launcher_form" and self.cycle_launcher_form_value(-1)):
                self.jump_selection(-10)
        elif key in (Qt.Key.Key_Right, Qt.Key.Key_D):
            if not (self.mode == "launcher_form" and self.cycle_launcher_form_value(1)):
                self.jump_selection(10)
        elif key == Qt.Key.Key_PageUp:
            self.jump_selection(-10)
        elif key == Qt.Key.Key_PageDown:
            self.jump_selection(10)
        elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space): self.activate_selected()
        elif key in (Qt.Key.Key_Escape, Qt.Key.Key_Backspace): self.go_back()
        elif key == Qt.Key.Key_F5: self.refresh_menu()
        elif key == Qt.Key.Key_F:
            if self.mode == "favorites": self.remove_selected_favorite()
            else: self.toggle_current_favorite()
        elif key == Qt.Key.Key_Y:
            self.open_current_item_options()
        elif key == Qt.Key.Key_I:
            self.open_selected_summary_overlay()
        elif key == Qt.Key.Key_F11: self.toggle_fullscreen()
        self.view.update()

    def closeEvent(self, event):
        if self.update_check_worker is not None and self.update_check_worker.isRunning():
            self.update_check_worker.quit()
            self.update_check_worker.wait(1000)
            self.update_check_worker = None
        if self.scrape_quota_worker is not None and self.scrape_quota_worker.isRunning():
            self.scrape_quota_worker.quit()
            self.scrape_quota_worker.wait(1000)
            self.scrape_quota_worker = None
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

        self.apply_theme()

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
        self.metadata_pixmap_cache = {}
        self.grid_boxart_pending_keys = set()
        self.grid_boxart_worker = None

        self.logo = QPixmap(str(self.window.assets_dir / "logo.png"))

        self.static_noise = QPixmap()
        self.static_noise_frame = 0

        self.icon_dir = self.window.assets_dir / "icons"
        self.icon_svgs = {}
        self.icon_renderer_cache = {}
        for icon_name in ("lan", "wifi", "bluetooth", "keyboard", "controller", "favorite", "api", "folder"):
            icon_path = self.icon_dir / f"{icon_name}.svg"
            if icon_path.exists():
                try:
                    self.icon_svgs[icon_name] = icon_path.read_text(encoding="utf-8")
                except Exception:
                    self.icon_svgs[icon_name] = ""

    def apply_theme(self):
        theme = self.window.current_theme
        self.bg = theme.color("background_color")
        self.panel = theme.color("menu_color")
        self.light = theme.color("highlight_color")
        self.text = theme.color("text_color")
        self.dark_text = theme.color("highlight_text_color")
        self.overlay = theme.color("overlay_color")
        self.soft_overlay = theme.color("soft_overlay_color")
        self.keyboard_overlay = theme.color("keyboard_overlay_color")
        self.dialog = theme.color("dialog_color")
        self.dialog_alt = theme.color("dialog_alt_color")
        self.field = theme.color("field_color")
        self.preview = theme.color("preview_color")
        self.wallpaper_tint = theme.color("wallpaper_tint_color")
        self.update()

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
        if self.window.modern_grid_active():
            columns = max(1, self.modern_grid_columns())
            rows = max(1, self.modern_grid_visible_rows())
            first_index = 1
            count = self.window.current_items_count()
            last_index = max(first_index, count - 1)
            idx = max(first_index, min(last_index, self.window.selected_index))
            first_row = max(0, (max(first_index, self.scroll_offset) - first_index) // columns)
            selected_row = max(0, (idx - first_index) // columns)
            if selected_row < first_row:
                first_row = selected_row
            elif selected_row >= first_row + rows:
                first_row = selected_row - rows + 1
            self.scroll_offset = first_index + first_row * columns
            return

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
        if self.window.mode in {"item_options", "single_scrape", "scrape_settings"}:
            panel_w = min(round(760 * scale), max(700, self.width() - 80))
            panel_h = min(round(430 * scale), max(430, self.height() - 120))
            panel_w = min(panel_w, self.width() - 40)
            panel_h = min(panel_h, self.height() - 100)
            return max(620, panel_w), max(320, panel_h)
        if self.window.modern_game_list_active():
            available_w = max(720, self.width() - 60)
            available_h = max(480, self.height() - 170)
            panel_h = min(available_h, max(560, round(self.height() * 0.66)))

            unconstrained_w = min(available_w, max(1040, round(self.width() * 0.78)))
            widescreen_w = int(panel_h * 16 / 9)
            panel_w = min(unconstrained_w, widescreen_w, available_w)

            panel_w = max(900, panel_w)
            panel_h = max(500, panel_h)

            if panel_w > available_w:
                panel_w = available_w
                panel_h = min(panel_h, int(panel_w * 9 / 16))

            return panel_w, panel_h
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
        painter.fillRect(rect, self.preview)
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

    def modern_grid_area_rect(self) -> QRect:
        panel = self.menu_panel_rect()
        side_w = 48
        return panel.adjusted(side_w + 20, 20, -20, -72)

    def modern_grid_tile_metrics(self) -> tuple[int, int, int, int, int]:
        area = self.modern_grid_area_rect()
        gap = 18
        min_box_w = 130
        max_box_w = 190
        horizontal_padding = 22
        vertical_padding = 18
        selection_padding = 8
        title_h = 42
        title_gap = 8
        bottom_gap = 12

        available_w = max(1, area.width() - horizontal_padding * 2)
        columns = max(1, (available_w + gap) // (min_box_w + gap))
        while columns > 1:
            candidate_w = (available_w - gap * (columns - 1)) // columns
            if candidate_w >= min_box_w:
                break
            columns -= 1
        box_w = min(max_box_w, max(min_box_w, (available_w - gap * (columns - 1)) // columns))

        box_h = int(box_w * 1.36)
        tile_h = selection_padding * 2 + box_h + title_gap + title_h + bottom_gap
        available_h = max(1, area.height() - vertical_padding * 2)
        rows = max(1, available_h // max(1, tile_h))
        return columns, rows, box_w, box_h, gap

    def modern_grid_columns(self) -> int:
        return self.modern_grid_tile_metrics()[0]

    def modern_grid_visible_rows(self) -> int:
        return self.modern_grid_tile_metrics()[1]

    def modern_grid_visible_slots(self) -> int:
        columns, rows, *_ = self.modern_grid_tile_metrics()
        return max(1, columns * rows)

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
            painter.fillRect(self.rect(), self.soft_overlay)
        else:
            self.draw_static_noise(painter)

        self.draw_logo(painter)
        if not self.window.idle_menu_hidden:
            self.draw_top_bar(painter)
            self.draw_panel(painter)
            self.draw_background_scrape_status(painter)
            self.draw_wallpaper_preview(painter)
        if self.window.mode == "text_input": self.draw_text_input(painter)
        if self.window.overlay: self.draw_overlay(painter)


    def overlay_guide_text(self, ov: dict) -> str:
        if self.window.active_input == "keyboard":
            if ov.get("type") == "scrape_progress":
                return "Up/Down Scroll   Enter Stop Scraper   Esc Background"
            if ov.get("scrollable"):
                return "Up/Down Scroll   Enter Close   Esc Back"
            return "Left/Right Select   Enter Confirm   Esc Back"
        if ov.get("type") == "scrape_progress":
            return "Up/Down Scroll   A Stop Scraper   B Background"
        if ov.get("scrollable"):
            return "Up/Down Scroll   A Close   B Back"
        return "D-pad Select   A Confirm   B Back"

    def text_input_guide_text(self) -> str:
        if self.window.active_input == "keyboard":
            return "Arrow Keys Navigate   Enter Done   Esc Cancel   Backspace Delete   Space Space   Shift Toggle Shift   Caps Lock Toggle Caps   Ctrl+S Symbols"
        return "D-pad Navigate   A Select   B Backspace   X Space   Y Shift   LB Caps Lock   RB Symbols   Start Done"

    def draw_background_scrape_status(self, painter):
        text = ""
        if self.window.bulk_scrape_active() and not self.window.progress_overlay_open():
            text = self.window.active_scrape_label()
        elif self.window.scrape_status_text and time.monotonic() < self.window.scrape_status_complete_until:
            text = self.window.scrape_status_text
        if not text:
            return
        painter.setFont(self.font)
        fm = painter.fontMetrics()
        margin = 28
        w = min(self.width() - (margin * 2), max(340, fm.horizontalAdvance(text) + 64))
        h = max(54, fm.height() + 24)
        x = self.width() - margin - w
        y = margin
        rect = QRect(x, y, w, h)
        painter.fillRect(rect, self.dialog)
        painter.setPen(self.light)
        painter.drawRect(rect)
        painter.setPen(self.text)
        painter.drawText(rect.adjusted(16, 0, -16, 0), Qt.AlignmentFlag.AlignCenter, text)

    def draw_overlay(self, painter):
        ov = self.window.overlay
        labels = ["OK"] if ov.get("type") == "message" else [item[0] for item in ov.get("buttons", [])]
        painter.setFont(self.font)
        guide = self.overlay_guide_text(ov)
        guide_width = painter.fontMetrics().horizontalAdvance(guide) + 96
        label_width = max((painter.fontMetrics().horizontalAdvance(label) for label in labels), default=0)
        columns = min(3, max(1, len(labels)))
        gap = 12
        needed_button_width = columns * min(240, max(140, label_width + 52)) + gap * (columns - 1) + 72
        box_width = min(max(820, guide_width, needed_button_width), self.width() - 80)
        content_width = box_width - 72

        painter.setFont(self.title_font)
        title_height = painter.fontMetrics().height()
        painter.setFont(self.font)
        guide_line_height = painter.fontMetrics().height()
        guide_wrap = painter.fontMetrics().horizontalAdvance(guide) > box_width - 64
        guide_height = guide_line_height * (2 if guide_wrap else 1) + (6 if guide_wrap else 0)
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

        painter.fillRect(self.rect(), self.overlay)
        painter.fillRect(box, self.dialog)
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

        available_width = box.width() - 72 - gap * (columns - 1)
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
        guide_flags = Qt.AlignmentFlag.AlignCenter | (Qt.TextFlag.TextWordWrap if guide_wrap else Qt.TextFlag.TextSingleLine)
        painter.drawText(guide_rect, guide_flags, guide)

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
        painter.fillRect(self.rect(), self.keyboard_overlay)
        painter.setFont(self.font)
        guide = self.text_input_guide_text()
        guide_width = painter.fontMetrics().horizontalAdvance(guide) + 96
        box_width = min(max(self.width() - 140, guide_width), self.width() - 40)
        box=QRect((self.width()-box_width)//2,70,box_width,self.height()-140); painter.fillRect(box,self.dialog); painter.setPen(self.light); painter.drawRect(box)
        painter.setFont(self.title_font); painter.setPen(self.text); painter.drawText(box.x()+28,box.y()+40,self.window.text_input_title)
        painter.setFont(self.font); painter.drawText(box.x()+28,box.y()+76,self.window.text_input_prompt)
        field=QRect(box.x()+28,box.y()+95,box.width()-56,48); painter.fillRect(field,self.field); painter.setPen(self.light); painter.drawRect(field)
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
        guide_rect = box.adjusted(18, 0, -18, -14)
        guide_available = guide_rect.width()
        guide_wrap = painter.fontMetrics().horizontalAdvance(guide) > guide_available
        guide_height = painter.fontMetrics().height() * (2 if guide_wrap else 1) + (6 if guide_wrap else 0)
        guide_rect = QRect(guide_rect.x(), box.bottom() - guide_height - 14, guide_rect.width(), guide_height)
        guide_flags = Qt.AlignmentFlag.AlignCenter | (Qt.TextFlag.TextWordWrap if guide_wrap else Qt.TextFlag.TextSingleLine)
        painter.setPen(self.text); painter.drawText(guide_rect, guide_flags, guide)

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
            self.wallpaper_tint,
        )
        painter.restore()

    def network_icon(self) -> str:
        return self.window.cached_network_icon

    def bluetooth_icon(self) -> str:
        return "bluetooth"

    def bluetooth_icon_visible(self) -> bool:
        return bool(self.window.cached_bluetooth_enabled)

    def input_icon(self) -> str:
        return "keyboard" if self.window.active_input == "keyboard" else "controller"

    def svg_renderer_for_color(self, icon_name: str, color: QColor):
        svg_text = self.icon_svgs.get(icon_name)
        if not svg_text:
            return None

        color_key = color.rgba()
        cache_key = (icon_name, color_key)
        renderer = self.icon_renderer_cache.get(cache_key)
        if renderer is not None:
            return renderer

        if len(self.icon_renderer_cache) > 64:
            self.icon_renderer_cache.clear()

        svg_color = color.name(QColor.NameFormat.HexRgb)
        themed_svg = svg_text.replace("currentColor", svg_color)
        renderer = QSvgRenderer(QByteArray(themed_svg.encode("utf-8")))
        self.icon_renderer_cache[cache_key] = renderer
        return renderer

    def draw_svg_icon(self, painter: QPainter, icon_name: str, rect: QRect, color: QColor):
        renderer = self.svg_renderer_for_color(icon_name, color)
        if not renderer or not renderer.isValid():
            return

        painter.save()
        renderer.render(painter, QRectF(rect))
        painter.restore()

    def modern_rom_folder_item_at_index(self, index: int) -> RomBrowserItem | None:
        if not (self.window.mode == "roms" and self.window.modern_mode_enabled()):
            return None
        if not self.window.rom_has_game_entries:
            return None
        rom_index = index - 1
        if rom_index < 0 or rom_index >= len(self.window.rom_items):
            return None
        item = self.window.rom_items[rom_index]
        if item.is_dir and not item.is_multi_disc_group:
            return item
        return None

    def selected_modern_rom_folder_item(self) -> RomBrowserItem | None:
        return self.modern_rom_folder_item_at_index(self.window.selected_index)

    def draw_modern_folder_boxart_icon(self, painter: QPainter, rect: QRect):
        icon_size = min(rect.width() - 48, rect.height() - 48, 180)
        if icon_size <= 0:
            return
        icon_rect = QRect(
            rect.x() + (rect.width() - icon_size) // 2,
            rect.y() + (rect.height() - icon_size) // 2,
            icon_size,
            icon_size,
        )
        self.draw_svg_icon(painter, "folder", icon_rect, self.light)

    def selected_metadata(self) -> tuple[GameMetadataIdentity | None, dict | None]:
        identity = self.window.selected_game_identity()
        return identity, self.window.metadata_cache.load(identity) if identity else None

    def grid_item_metadata(self, index: int) -> tuple[GameMetadataIdentity | None, dict | None]:
        data = self.window.game_data_for_index(index)
        if not data:
            return None, None
        identity = self.window.game_metadata_identity(data.get("launcher"), data.get("rom"))
        if identity is None:
            return None, None
        return identity, self.window.metadata_cache.load(identity)

    def grid_item_title(self, labels: list[tuple[str, str]], index: int, data: dict | None) -> str:
        if data:
            title = str(data.get("scrape_name", "")).strip()
            if title:
                return title
        if 0 <= index < len(labels):
            return str(labels[index][0])
        return ""

    def request_grid_boxart(self, requests: list[tuple[str, str]]):
        if not requests:
            return
        missing = []
        for key, path_text in requests:
            if not key or key in self.metadata_pixmap_cache or key in self.grid_boxart_pending_keys:
                continue
            missing.append((key, path_text))
            self.grid_boxart_pending_keys.add(key)
        if not missing:
            return
        if self.grid_boxart_worker is not None and self.grid_boxart_worker.isRunning():
            return
        worker = ModernBoxartLoadWorker(missing)
        self.grid_boxart_worker = worker
        worker.result.connect(self.on_grid_boxart_loaded)
        worker.finished.connect(lambda: self.cleanup_grid_boxart_worker(worker))
        worker.start()

    def on_grid_boxart_loaded(self, loaded: object):
        if isinstance(loaded, dict):
            for key, image in loaded.items():
                self.grid_boxart_pending_keys.discard(key)
                if isinstance(image, QImage) and not image.isNull():
                    self.metadata_pixmap_cache[key] = QPixmap.fromImage(image)
        while len(self.metadata_pixmap_cache) > 160:
            self.metadata_pixmap_cache.pop(next(iter(self.metadata_pixmap_cache)))
        self.update()

    def cleanup_grid_boxart_worker(self, worker):
        if self.grid_boxart_worker is worker:
            self.grid_boxart_worker = None
        for key, _path_text in getattr(worker, "items", []):
            self.grid_boxart_pending_keys.discard(key)
        self.update()

    def draw_scrolling_grid_title(self, painter: QPainter, rect: QRect, text: str, selected: bool):
        metrics = painter.fontMetrics()
        width = metrics.horizontalAdvance(text)
        painter.save()
        painter.setClipRect(rect)
        y = rect.y() + metrics.ascent() + max(0, (rect.height() - metrics.height()) // 2)
        if selected and width > rect.width():
            gap = 42
            cycle = width + gap
            offset = int((time.monotonic() * 48) % cycle)
            painter.drawText(rect.x() - offset, y, text)
            painter.drawText(rect.x() - offset + cycle, y, text)
        else:
            painter.drawText(rect, Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter, metrics.elidedText(text, Qt.TextElideMode.ElideRight, rect.width()))
        painter.restore()

    def draw_modern_grid_panel(self, painter: QPainter, panel_rect: QRect, side_w: int):
        labels = self.window.current_labels()
        if len(labels) <= 1:
            painter.setPen(self.text)
            painter.drawText(panel_rect.adjusted(side_w + 22, 28, -28, -72), Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft, "No games")
            return

        area = self.modern_grid_area_rect()
        painter.fillRect(area, self.dialog_alt)
        painter.setPen(self.light)
        painter.drawRect(area)

        columns, rows, box_w, box_h, gap = self.modern_grid_tile_metrics()
        visible_slots = max(1, columns * rows)
        first_index = max(1, self.scroll_offset)
        max_first = max(1, len(labels) - visible_slots)
        first_index = min(first_index, max_first)
        first_row = (first_index - 1) // columns
        first_index = first_row * columns + 1
        self.scroll_offset = first_index
        end_index = min(len(labels), first_index + visible_slots)

        grid_w = columns * box_w + (columns - 1) * gap
        start_x = area.x() + max(0, (area.width() - grid_w) // 2)
        y = area.y() + 18
        title_h = 42
        title_gap = 8
        selection_padding = 8
        bottom_gap = 12
        tile_h = selection_padding * 2 + box_h + title_gap + title_h + bottom_gap
        font = QFont(self.font)
        font.setPointSize(max(10, min(15, int(box_w / 10))))
        painter.setFont(font)

        boxart_requests = []
        for visible_pos, index in enumerate(range(first_index, end_index)):
            col = visible_pos % columns
            row = visible_pos // columns
            x = start_x + col * (box_w + gap)
            tile_y = y + row * tile_h + selection_padding
            box_rect = QRect(x, tile_y, box_w, box_h)
            title_rect = QRect(x, box_rect.bottom() + title_gap, box_w, title_h)
            selected = index == self.window.selected_index

            if selected:
                painter.fillRect(QRect(x - selection_padding, tile_y - selection_padding, box_w + selection_padding * 2, box_h + title_gap + title_h + selection_padding * 2), self.light)
                painter.setPen(self.dark_text)
            else:
                painter.setPen(self.light)

            painter.drawRect(box_rect)
            identity, data = self.grid_item_metadata(index)
            title = self.grid_item_title(labels, index, data)
            pixmap = QPixmap()
            key = ""
            if identity and data:
                box = str(data.get("box2d", "")).strip()
                if box:
                    art_path = self.window.metadata_cache.resolve_box2d_path(identity, box)
                    if art_path and art_path.exists():
                        key = str(art_path)
                        pixmap = self.metadata_pixmap_cache.get(key, QPixmap())
                        if pixmap.isNull():
                            boxart_requests.append((key, key))

            if not pixmap.isNull():
                scaled = pixmap.scaled(box_rect.adjusted(8, 8, -8, -8).size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
                painter.drawPixmap(box_rect.x() + (box_rect.width() - scaled.width()) // 2, box_rect.y() + (box_rect.height() - scaled.height()) // 2, scaled)
            else:
                folder_item = self.modern_rom_folder_item_at_index(index)
                if folder_item is not None:
                    icon_size = min(box_rect.width() - 36, box_rect.height() - 36, 96)
                    icon_rect = QRect(
                        box_rect.x() + (box_rect.width() - icon_size) // 2,
                        box_rect.y() + (box_rect.height() - icon_size) // 2,
                        icon_size,
                        icon_size,
                    )
                    self.draw_svg_icon(painter, "folder", icon_rect, self.dark_text if selected else self.light)
                else:
                    painter.setFont(font)
                    painter.setPen(self.dark_text if selected else self.text)
                    placeholder = "Loading Boxart" if key in self.grid_boxart_pending_keys else "No Boxart"
                    painter.drawText(box_rect.adjusted(8, 8, -8, -8), Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap, placeholder)

            painter.setPen(self.dark_text if selected else self.text)
            painter.setFont(font)
            self.draw_scrolling_grid_title(painter, title_rect, title, selected)

        self.request_grid_boxart(boxart_requests)

        arrow_x = area.right() - 18
        painter.setPen(self.text)
        if first_index > 1:
            painter.drawText(arrow_x, area.y() + 24, "^")
        if end_index < len(labels):
            painter.drawText(arrow_x, area.bottom() - 8, "v")

    def draw_modern_metadata_panel(self, painter: QPainter, panel_rect: QRect, side_w: int):
        meta_x = panel_rect.x() + side_w + int((panel_rect.width() - side_w) * 0.54)
        meta_w = panel_rect.right() - meta_x - 18
        if meta_w < 260:
            return

        rect = QRect(meta_x, panel_rect.y() + 18, meta_w, panel_rect.height() - 72)
        painter.fillRect(rect, self.dialog_alt)
        painter.setPen(self.light)
        painter.drawRect(rect)
        inner = rect.adjusted(14, 12, -14, -12)

        identity, data = self.selected_metadata()
        title = "No metadata available"
        if data:
            title = str(data.get("scrape_name", "")).strip() or title

        title_font = QFont(self.title_font)
        title_font.setPointSize(max(12, min(17, int(panel_rect.height() / 38))))
        meta_font = QFont(self.font)
        meta_font.setPointSize(max(10, min(13, int(panel_rect.height() / 52))))
        desc_font = QFont(self.font)
        desc_font.setPointSize(max(9, min(12, int(panel_rect.height() / 58))))

        art_h = min(max(320, int(inner.height() * 0.56)), int(inner.height() * 0.66))
        art_rect = QRect(inner.x(), inner.y(), inner.width(), art_h)

        pixmap = QPixmap()
        if identity and data:
            box = str(data.get("box2d", "")).strip()
            if box:
                art_path = self.window.metadata_cache.resolve_box2d_path(identity, box)
                key = str(art_path) if art_path else ""
                pixmap = self.metadata_pixmap_cache.get(key, QPixmap()) if key else QPixmap()
                if key and pixmap.isNull() and art_path.exists():
                    pixmap = QPixmap(key)
                    self.metadata_pixmap_cache[key] = pixmap
                    while len(self.metadata_pixmap_cache) > 24:
                        self.metadata_pixmap_cache.pop(next(iter(self.metadata_pixmap_cache)))

        if not pixmap.isNull():
            scaled = pixmap.scaled(
                art_rect.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            painter.drawPixmap(
                art_rect.x() + (art_rect.width() - scaled.width()) // 2,
                art_rect.y() + (art_rect.height() - scaled.height()) // 2,
                scaled,
            )
        else:
            if self.selected_modern_rom_folder_item() is not None:
                self.draw_modern_folder_boxart_icon(painter, art_rect)
            else:
                painter.setFont(meta_font)
                painter.setPen(self.text)
                painter.drawText(art_rect, Qt.AlignmentFlag.AlignCenter, "No Boxart")

        metadata_lines = []
        if data:
            for label, key in (("System", "system"), ("Year", "year"), ("Developer", "developer"), ("Publisher", "publisher"), ("Genre", "genre"), ("Players", "players")):
                value = str(data.get(key, "")).strip()
                if key in {"developer", "publisher"} and re.fullmatch(r"\d+", value):
                    value = ""
                if key == "players":
                    match = re.search(r"['\"]text['\"]\s*:\s*['\"]([^'\"]+)['\"]", value)
                    if match:
                        value = match.group(1).strip()
                if value:
                    metadata_lines.append(f"{label}: {value}")
            description = str(data.get("description", "")).strip()
        else:
            metadata_lines.append("Use Scrape from the game menu")
            metadata_lines.append("or bulk scrape from Settings.")
            description = ""

        lower_top = art_rect.bottom() + 16
        lower_h = inner.bottom() - lower_top
        if lower_h <= 22:
            return

        gap = 18
        summary_w = max(180, int((inner.width() - gap) * 0.56))
        summary_rect = QRect(inner.x(), lower_top, summary_w, lower_h)
        info_rect = QRect(summary_rect.right() + gap, lower_top, inner.right() - summary_rect.right() - gap, lower_h)

        if info_rect.width() < 170:
            summary_w = max(160, int((inner.width() - gap) * 0.50))
            summary_rect = QRect(inner.x(), lower_top, summary_w, lower_h)
            info_rect = QRect(summary_rect.right() + gap, lower_top, inner.right() - summary_rect.right() - gap, lower_h)

        painter.save()
        painter.setClipRect(summary_rect)
        painter.setFont(desc_font)
        painter.setPen(self.text)

        if not description and data:
            description = "No summary available."

        if description:
            desc_lines = self.wrapped_text_lines(painter, description, summary_rect.width())
            metrics = painter.fontMetrics()
            desc_step = max(14, metrics.height() + 3)
            content_h = len(desc_lines) * desc_step
            x = summary_rect.x()
            if content_h <= summary_rect.height():
                y = summary_rect.y() + metrics.ascent()
                for line in desc_lines:
                    painter.drawText(x, y, line)
                    y += desc_step
            else:
                gap_h = desc_step * 2
                cycle = content_h + gap_h
                offset = int((time.monotonic() * 22) % cycle)
                start_y = summary_rect.y() + metrics.ascent() - offset
                for repeat in range(2):
                    y = start_y + repeat * cycle
                    for line in desc_lines:
                        if y >= summary_rect.y() - desc_step and y <= summary_rect.bottom() + desc_step:
                            painter.drawText(x, y, line)
                        y += desc_step
        painter.restore()

        if info_rect.width() <= 20:
            return

        painter.setFont(title_font)
        title_metrics = painter.fontMetrics()
        title_h = max(26, title_metrics.height() + 4)

        painter.setFont(meta_font)
        meta_metrics = painter.fontMetrics()
        line_step = max(15, meta_metrics.height() + 2)
        max_lines = max(0, int((info_rect.height() - title_h - line_step) / line_step))
        visible_lines = metadata_lines[:max_lines] if max_lines else []
        block_h = title_h + (line_step if visible_lines else 0) + len(visible_lines) * line_step
        block_y = info_rect.y() + max(0, (info_rect.height() - block_h) // 2)

        painter.setFont(title_font)
        painter.setPen(self.text)
        title_rect = QRect(info_rect.x(), block_y, info_rect.width(), title_h)
        painter.drawText(
            title_rect,
            Qt.TextFlag.TextSingleLine,
            title_metrics.elidedText(title, Qt.TextElideMode.ElideRight, title_rect.width()),
        )

        painter.setFont(meta_font)
        painter.setPen(self.text)
        text_y = title_rect.bottom() + line_step
        for line in visible_lines:
            painter.drawText(
                info_rect.x(),
                text_y,
                meta_metrics.elidedText(line, Qt.TextElideMode.ElideRight, info_rect.width()),
            )
            text_y += line_step

    def draw_modern_simple_boxart_panel(self, painter: QPainter, panel_rect: QRect, side_w: int):
        art_x = panel_rect.x() + side_w + int((panel_rect.width() - side_w) * 0.54)
        art_w = panel_rect.right() - art_x - 18
        if art_w < 260:
            return

        rect = QRect(art_x, panel_rect.y() + 18, art_w, panel_rect.height() - 72)
        painter.fillRect(rect, self.dialog_alt)
        painter.setPen(self.light)
        painter.drawRect(rect)
        inner = rect.adjusted(18, 18, -18, -18)

        identity, data = self.selected_metadata()
        pixmap = QPixmap()
        if identity and data:
            box = str(data.get("box2d", "")).strip()
            if box:
                art_path = self.window.metadata_cache.resolve_box2d_path(identity, box)
                key = str(art_path) if art_path else ""
                pixmap = self.metadata_pixmap_cache.get(key, QPixmap()) if key else QPixmap()
                if key and pixmap.isNull() and art_path.exists():
                    pixmap = QPixmap(key)
                    self.metadata_pixmap_cache[key] = pixmap
                    while len(self.metadata_pixmap_cache) > 24:
                        self.metadata_pixmap_cache.pop(next(iter(self.metadata_pixmap_cache)))

        if not pixmap.isNull():
            scaled = pixmap.scaled(
                inner.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            painter.drawPixmap(
                inner.x() + (inner.width() - scaled.width()) // 2,
                inner.y() + (inner.height() - scaled.height()) // 2,
                scaled,
            )
        else:
            if self.selected_modern_rom_folder_item() is not None:
                self.draw_modern_folder_boxart_icon(painter, inner)
            else:
                painter.setFont(self.font)
                painter.setPen(self.text)
                painter.drawText(inner, Qt.AlignmentFlag.AlignCenter, "No Boxart")

    def draw_modern_help_bar(self, painter: QPainter, panel_rect: QRect):
        rect = QRect(panel_rect.x(), panel_rect.bottom() + 12, panel_rect.width(), 36)
        if rect.bottom() > self.height() - 12:
            rect.moveTop(panel_rect.bottom() - 42)
        painter.fillRect(rect, self.light)
        painter.setFont(self.font)
        painter.setPen(self.dark_text)
        if self.window.active_input == "controller":
            if self.window.modern_grid_active():
                text = "D-pad Move   A Launch   B Back   X Favorite   Y Menu   L Summary   R Search"
            else:
                text = "A Launch   B Back   X Favorite   Y Menu   L Summary   R Search   Left/Right Page"
        else:
            if self.window.modern_grid_active():
                text = "Arrows Move   Enter Launch   Esc Back   F Favorite   Y Menu   I Summary   Ctrl+F Search"
            else:
                text = "Enter Launch   Esc Back   F Favorite   Y Menu   I Summary   Ctrl+F Search   PgUp/PgDn Page"
        painter.drawText(rect.adjusted(12, 0, -12, 0), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignHCenter, text)

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
        if self.bluetooth_icon_visible():
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

        modern = self.window.modern_game_list_active()
        labels = self.window.current_labels()
        rows = self.visible_rows()
        self.ensure_visible()

        start = self.scroll_offset
        end = min(len(labels), start + rows)

        text_x = x + side_w + 22
        list_right = x + panel_w - 32
        if modern:
            list_right = x + side_w + int((panel_w - side_w) * 0.55) - 12
        marker_x = list_right - 92
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

        if modern and self.window.modern_grid_enabled():
            if self.window.selected_index <= 0 and len(labels) > 1:
                self.window.selected_index = 1
            self.ensure_visible()
            self.draw_modern_grid_panel(painter, panel_rect, side_w)
            self.draw_modern_help_bar(painter, panel_rect)
            return

        for row, idx in enumerate(range(start, end)):
            label, marker = labels[idx]
            support_gap = 14 if self.window.mode == "system" and idx >= 6 else 0
            yy = row_y + row * 28 + support_gap

            row_left_x = text_x - 6

            marker_w = painter.fontMetrics().horizontalAdvance("<DIR>")
            marker_end_x = list_right - 8
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

                show_folder_icon = self.modern_rom_folder_item_at_index(idx) is not None
                if show_folder_icon:
                    icon_size = 20
                    icon_y = yy - 21
                    self.draw_svg_icon(
                        painter,
                        "folder",
                        QRect(text_x, icon_y, icon_size, icon_size),
                        self.dark_text if idx == self.window.selected_index else self.light,
                    )
                    draw_text_x = text_x + 28
                    draw_text_area_w = max(0, text_area_w - 28)

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
                        QRect(draw_text_x, icon_y, icon_size, icon_size),
                        painter.pen().color(),
                    )
                    draw_text_x += 24
                    draw_text_area_w = max(0, draw_text_area_w - 24)

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

        arrow_x = list_right - 18 if modern else x + panel_w - 38
        if start > 0:
            painter.setPen(self.text)
            painter.drawText(arrow_x, y + title_h + 20, "^")
        if end < len(labels):
            painter.setPen(self.text)
            painter.drawText(arrow_x, y + panel_h - 8, "v")
        if modern:
            if self.window.modern_simple_list_enabled():
                self.draw_modern_simple_boxart_panel(painter, panel_rect, side_w)
            else:
                self.draw_modern_metadata_panel(painter, panel_rect, side_w)
            self.draw_modern_help_bar(painter, panel_rect)
