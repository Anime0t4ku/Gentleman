import ctypes
import json
import math
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import time
import traceback
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import QThread, QTimer, Qt, QRect
from PyQt6.QtGui import QColor, QFont, QImage, QKeyEvent, QPainter, QPixmap
from PyQt6.QtWidgets import QApplication, QWidget
from PyQt6.QtCore import pyqtSignal

try:
    import pygame
except Exception:
    pygame = None


APP_NAME = "Gentleman-Updater"
GITHUB_OWNER = "Anime0t4ku"
GITHUB_REPO = "Gentleman"

SETTINGS_FILE = Path("config") / "settings.json"
UPDATE_NOW_FILE = "updatenow.txt"

WINDOWS_TARGET_EXE = "Gentleman.exe"
LINUX_TARGET_EXE = "Gentleman"

WINDOWS_ZIP_KEYWORDS = ["Windows", ".zip"]
LINUX_TAR_KEYWORDS = ["Linux", ".tar.gz"]

INCLUDE_PRERELEASES = False


class UpdateState:
    IDLE = "idle"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


def app_folder():
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent

    return Path(__file__).resolve().parent


def current_platform():
    system = platform.system().lower()

    if system == "windows":
        return {
            "name": "Windows",
            "target_exe": WINDOWS_TARGET_EXE,
            "asset_keywords": WINDOWS_ZIP_KEYWORDS,
            "archive_type": "zip",
        }

    if system == "linux":
        return {
            "name": "Linux",
            "target_exe": LINUX_TARGET_EXE,
            "asset_keywords": LINUX_TAR_KEYWORDS,
            "archive_type": "tar.gz",
        }

    raise RuntimeError(f"Unsupported operating system: {platform.system()}")


def normalize_version(value):
    text = str(value or "").strip()
    match = re.search(r"v?(\d+)\.(\d+)\.(\d+)", text, re.IGNORECASE)

    if not match:
        return None

    return tuple(int(part) for part in match.groups())


def version_to_text(version):
    if not version:
        return "Unknown"

    return f"v{version[0]}.{version[1]}.{version[2]}"


def read_current_version(base_path):
    settings_path = base_path / SETTINGS_FILE
    version_txt_path = base_path / "version.txt"
    app_info_path = base_path / "app" / "app_info.py"

    version_text = None

    if settings_path.exists():
        with open(settings_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        if isinstance(data, dict):
            version_text = data.get("app_version") or data.get("version")

    if not version_text and version_txt_path.exists():
        version_text = version_txt_path.read_text(encoding="utf-8").strip()

    if not version_text and app_info_path.exists():
        app_info_text = app_info_path.read_text(encoding="utf-8", errors="ignore")
        match = re.search(r"APP_VERSION\s*=\s*[\"\']([^\"\']+)[\"\']", app_info_text)
        if match:
            version_text = match.group(1)

    version = normalize_version(version_text)

    if not version:
        raise ValueError(
            "Could not read the installed Gentleman version. "
            "Expected config/settings.json with app_version, version.txt, or app/app_info.py."
        )

    return version_text, version


def github_api_json(url):
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "Gentleman-Updater",
        },
    )

    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def asset_matches_platform(asset_name, platform_info):
    lowered = str(asset_name or "").lower()

    if "updater" in lowered:
        return False

    if platform_info["archive_type"] == "zip":
        if not lowered.endswith(".zip"):
            return False
        if not lowered.startswith("gentleman"):
            return False
        return "windows" in lowered or "win" in lowered

    if platform_info["archive_type"] == "tar.gz":
        if not lowered.endswith(".tar.gz"):
            return False
        if not lowered.startswith("gentleman"):
            return False
        return "linux" in lowered

    return False


def find_latest_release(platform_info):
    url = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases"
    releases = github_api_json(url)

    if not isinstance(releases, list):
        raise RuntimeError("GitHub did not return a release list.")

    best_release = None
    best_version = None
    best_asset = None

    for release in releases:
        if release.get("draft"):
            continue

        if release.get("prerelease") and not INCLUDE_PRERELEASES:
            continue

        tag_name = release.get("tag_name", "")
        release_name = release.get("name", "")
        version = normalize_version(tag_name) or normalize_version(release_name)

        if not version:
            continue

        assets = release.get("assets", [])
        matching_asset = None

        for asset in assets:
            asset_name = asset.get("name", "")
            if asset_matches_platform(asset_name, platform_info):
                matching_asset = asset
                break

        if not matching_asset:
            continue

        if best_version is None or version > best_version:
            best_release = release
            best_version = version
            best_asset = matching_asset

    if not best_release or not best_version or not best_asset:
        raise RuntimeError(
            f"Could not find a valid Gentleman {platform_info['name']} release asset."
        )

    return best_release, best_version, best_asset


def make_executable(path):
    if not path.exists():
        raise FileNotFoundError(f"{path.name} was not found after extraction.")

    current_mode = os.stat(path).st_mode
    os.chmod(
        path,
        current_mode
        | stat.S_IXUSR
        | stat.S_IXGRP
        | stat.S_IXOTH,
    )


def launch_gentleman(base_path, platform_info):
    target_path = base_path / platform_info["target_exe"]

    if not target_path.exists():
        raise FileNotFoundError(f"{platform_info['target_exe']} was not found.")

    if platform_info["name"] == "Windows":
        subprocess.Popen([str(target_path)], cwd=str(base_path), close_fds=True)
    else:
        subprocess.Popen([str(target_path)], cwd=str(base_path), start_new_session=True)


class UpdateWorker(QThread):
    status_changed = pyqtSignal(str)
    progress_changed = pyqtSignal(int)
    finished_ok = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.base_path = app_folder()
        self.platform_info = current_platform()

    def log(self, message):
        self.status_changed.emit(message)

    def run(self):
        try:
            self.progress_changed.emit(0)

            self.log(f"Detected platform: {self.platform_info['name']}")
            self.log("Reading installed version...")
            current_version_text, current_version = read_current_version(self.base_path)
            self.log(f"Installed version: {current_version_text}")

            self.log("Checking GitHub releases...")
            release, latest_version, asset = find_latest_release(self.platform_info)
            latest_version_text = version_to_text(latest_version)
            self.log(f"Latest version: {latest_version_text}")

            if latest_version <= current_version:
                self.progress_changed.emit(100)
                self.finished_ok.emit("Gentleman is already up to date.")
                return

            asset_name = asset.get("name")
            download_url = asset.get("browser_download_url")

            if not download_url:
                raise RuntimeError("The release asset does not have a download URL.")

            archive_path = self.base_path / asset_name
            target_name = self.platform_info["target_exe"]
            target_path = self.base_path / target_name
            temp_target_path = self.base_path / f"{target_name}.new"

            self.log(f"Downloading {asset_name}...")
            self.download_file(download_url, archive_path)
            self.progress_changed.emit(45)

            if temp_target_path.exists():
                try:
                    temp_target_path.unlink()
                except Exception:
                    pass

            self.log(f"Extracting {target_name} from update archive...")

            if self.platform_info["archive_type"] == "zip":
                self.extract_zip_target(archive_path, target_name, temp_target_path)
            elif self.platform_info["archive_type"] == "tar.gz":
                self.extract_tar_gz_target(archive_path, target_name, temp_target_path)
            else:
                raise RuntimeError(
                    f"Unsupported archive type: {self.platform_info['archive_type']}"
                )

            self.progress_changed.emit(75)

            if target_path.exists():
                self.log(f"Removing old {target_name}...")
                try:
                    target_path.unlink()
                except PermissionError:
                    try:
                        temp_target_path.unlink()
                    except Exception:
                        pass
                    raise PermissionError(
                        f"Could not remove {target_name}. "
                        "Please make sure Gentleman is closed and try again."
                    )

            self.log(f"Installing new {target_name}...")
            shutil.move(str(temp_target_path), str(target_path))

            self.progress_changed.emit(85)

            if self.platform_info["name"] == "Linux":
                self.log("Making Linux executable runnable...")
                make_executable(target_path)

            self.log("Removing downloaded archive file...")
            try:
                archive_path.unlink()
            except Exception:
                pass

            self.progress_changed.emit(100)
            self.finished_ok.emit(f"Gentleman was updated to {latest_version_text}.")

        except Exception as e:
            error = f"{e}\n\n{traceback.format_exc()}"
            self.failed.emit(error)

    def download_file(self, url, destination):
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Gentleman-Updater",
            },
        )

        with urllib.request.urlopen(request, timeout=60) as response:
            total_size = int(response.headers.get("Content-Length", 0))
            downloaded = 0

            with open(destination, "wb") as f:
                while True:
                    chunk = response.read(1024 * 256)

                    if not chunk:
                        break

                    f.write(chunk)
                    downloaded += len(chunk)

                    if total_size > 0:
                        percent = int((downloaded / total_size) * 40)
                        self.progress_changed.emit(max(1, min(40, percent)))

    def extract_zip_target(self, zip_path, target_name, destination_path):
        target_name_lower = target_name.lower()

        with zipfile.ZipFile(zip_path, "r") as zip_file:
            matching_members = [
                member for member in zip_file.infolist()
                if not member.is_dir() and Path(member.filename).name.lower() == target_name_lower
            ]

            if not matching_members:
                raise RuntimeError(f"{target_name} was not found in the downloaded update ZIP.")

            member = matching_members[0]

            with zip_file.open(member, "r") as source:
                with open(destination_path, "wb") as target:
                    shutil.copyfileobj(source, target)

    def extract_tar_gz_target(self, tar_path, target_name, destination_path):
        target_name_lower = target_name.lower()

        with tarfile.open(tar_path, "r:gz") as tar_file:
            matching_members = [
                member for member in tar_file.getmembers()
                if member.isfile() and Path(member.name).name.lower() == target_name_lower
            ]

            if not matching_members:
                raise RuntimeError(f"{target_name} was not found in the downloaded update archive.")

            member = matching_members[0]
            source = tar_file.extractfile(member)

            if source is None:
                raise RuntimeError(f"Could not read {target_name} from the downloaded update archive.")

            with source:
                with open(destination_path, "wb") as target:
                    shutil.copyfileobj(source, target)


XINPUT_GAMEPAD_DPAD_UP = 0x0001
XINPUT_GAMEPAD_DPAD_DOWN = 0x0002
XINPUT_GAMEPAD_DPAD_LEFT = 0x0004
XINPUT_GAMEPAD_DPAD_RIGHT = 0x0008
XINPUT_GAMEPAD_START = 0x0010
XINPUT_GAMEPAD_BACK = 0x0020
XINPUT_GAMEPAD_A = 0x1000
XINPUT_GAMEPAD_B = 0x2000


class XINPUT_GAMEPAD(ctypes.Structure):
    _fields_ = [
        ("wButtons", ctypes.c_ushort),
        ("bLeftTrigger", ctypes.c_ubyte),
        ("bRightTrigger", ctypes.c_ubyte),
        ("sThumbLX", ctypes.c_short),
        ("sThumbLY", ctypes.c_short),
        ("sThumbRX", ctypes.c_short),
        ("sThumbRY", ctypes.c_short),
    ]


class XINPUT_STATE(ctypes.Structure):
    _fields_ = [
        ("dwPacketNumber", ctypes.c_ulong),
        ("Gamepad", XINPUT_GAMEPAD),
    ]


def load_xinput():
    if platform.system().lower() != "windows":
        return None

    for dll_name in ("xinput1_4.dll", "xinput1_3.dll", "xinput9_1_0.dll"):
        try:
            return ctypes.windll.LoadLibrary(dll_name)
        except Exception:
            continue

    return None


class UpdaterWindow(QWidget):
    def __init__(self):
        super().__init__()

        self.worker = None
        self.base_path = app_folder()
        self.update_now_path = self.base_path / UPDATE_NOW_FILE
        self.auto_update_mode = self.update_now_path.exists()
        self.update_state = UpdateState.IDLE
        self.update_message = ""
        self.progress_value = 0
        self.logs = []
        self.selected_index = 0
        self.overlay = None
        self.active_input = "controller"

        self.xinput = load_xinput()
        self.xinput_index = None
        self.xinput_button_state = {}
        self.xinput_repeat_action = None
        self.xinput_repeat_next_ms = 0

        self.controller_available = False
        self.controller = None
        self.controller_axis_state = {"up": False, "down": False, "left": False, "right": False}
        self.controller_button_state = {}
        self.controller_repeat_action = None
        self.controller_repeat_next_ms = 0
        self.controller_last_scan_ms = 0

        try:
            self.platform_info = current_platform()
            self.platform_name = self.platform_info["name"]
        except Exception:
            self.platform_info = {
                "name": platform.system() or "Unknown",
                "target_exe": WINDOWS_TARGET_EXE if platform.system().lower() == "windows" else LINUX_TARGET_EXE,
            }
            self.platform_name = self.platform_info["name"]

        self.setWindowTitle(APP_NAME)
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        self.setMinimumSize(780, 520)
        self.resize(960, 640)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        self.bg = QColor(0, 0, 0)
        self.panel = QColor(55, 0, 15, 230)
        self.light = QColor(220, 185, 190)
        self.text = QColor(245, 235, 235)
        self.dark_text = QColor(40, 0, 10)
        self.dim_text = QColor(190, 155, 160)
        self.error_text = QColor(255, 150, 150)

        self.font = QFont("Consolas", 20)
        self.font.setStyleHint(QFont.StyleHint.Monospace)
        self.small_font = QFont("Consolas", 14)
        self.small_font.setStyleHint(QFont.StyleHint.Monospace)
        self.title_font = QFont("Consolas", 22, QFont.Weight.Bold)
        self.title_font.setStyleHint(QFont.StyleHint.Monospace)

        self.clock_timer = QTimer(self)
        self.clock_timer.timeout.connect(self.update)
        self.clock_timer.start(1000)

        self.repaint_timer = QTimer(self)
        self.repaint_timer.timeout.connect(self.update)
        self.repaint_timer.start(100)

        self.init_controller_support()
        self.controller_timer = QTimer(self)
        self.controller_timer.timeout.connect(self.poll_controller)
        self.controller_timer.start(33)

        if self.auto_update_mode:
            self.append_log(f"{UPDATE_NOW_FILE} found. Starting automatic update check...")
            QTimer.singleShot(250, self.start_update)
        else:
            self.append_log("Ready.")

    def menu_items(self):
        if self.update_state == UpdateState.RUNNING:
            return []
        if self.update_state in (UpdateState.DONE, UpdateState.FAILED):
            return [("Exit & Open Gentleman", "open"), ("Exit", "exit")]
        return [("Check and Update", "update"), ("Exit", "exit")]

    def title_path(self):
        if self.update_state == UpdateState.RUNNING:
            return "UPDATING"
        if self.update_state == UpdateState.DONE:
            return "COMPLETE"
        if self.update_state == UpdateState.FAILED:
            return "ERROR"
        return "UPDATER"

    def append_log(self, message):
        text = str(message)
        for line in text.splitlines() or [""]:
            self.logs.append(line)
        self.logs = self.logs[-80:]
        self.update()

    def start_update(self):
        if self.update_state == UpdateState.RUNNING:
            return

        self.update_state = UpdateState.RUNNING
        self.update_message = ""
        self.progress_value = 0
        self.selected_index = 0

        if not self.auto_update_mode:
            self.logs.clear()

        self.worker = UpdateWorker()
        self.worker.status_changed.connect(self.append_log)
        self.worker.progress_changed.connect(self.set_progress)
        self.worker.finished_ok.connect(self.update_finished)
        self.worker.failed.connect(self.update_failed)
        self.worker.start()
        self.update()

    def set_progress(self, value):
        self.progress_value = max(0, min(100, int(value)))
        self.update()

    def remove_update_now_file(self):
        if self.update_now_path.exists():
            try:
                self.update_now_path.unlink()
                self.append_log(f"Removed {UPDATE_NOW_FILE}.")
            except Exception as e:
                self.append_log(f"Could not remove {UPDATE_NOW_FILE}: {e}")

    def update_finished(self, message):
        self.update_state = UpdateState.DONE
        self.update_message = message
        self.append_log(message)
        self.selected_index = 0

        if self.auto_update_mode:
            self.remove_update_now_file()

        self.show_overlay(
            "Update Complete",
            f"{message}\n\nWhat would you like to do next?",
            [("Exit & Open Gentleman", "open"), ("Exit", "exit")],
        )
        self.update()

    def update_failed(self, error):
        self.update_state = UpdateState.FAILED
        self.update_message = "Update failed. Check the log for details."
        self.append_log("Update failed.")
        self.append_log(error)
        self.selected_index = 1

        if self.auto_update_mode:
            self.append_log(f"{UPDATE_NOW_FILE} was not removed because the update failed.")

        self.show_overlay(
            "Update Failed",
            "The update failed. Check the log for details.",
            [("Exit", "exit")],
        )
        self.update()

    def show_overlay(self, title, message, buttons):
        self.overlay = {"title": title, "message": message, "buttons": buttons, "selected": 0}
        self.update()

    def close_overlay(self):
        self.overlay = None
        self.update()

    def activate_overlay(self):
        if not self.overlay:
            return
        buttons = self.overlay.get("buttons", [])
        if not buttons:
            self.close_overlay()
            return
        index = max(0, min(len(buttons) - 1, self.overlay.get("selected", 0)))
        action = buttons[index][1]
        self.perform_action(action)

    def perform_action(self, action):
        if action == "update":
            self.start_update()
            return

        if action == "open":
            try:
                launch_gentleman(self.base_path, self.platform_info)
            except Exception as exc:
                self.append_log(f"Could not open Gentleman: {exc}")
                self.show_overlay("Could Not Open Gentleman", str(exc), [("Exit", "exit")])
                return
            QApplication.quit()
            return

        if action == "exit":
            QApplication.quit()
            return

    def activate_selected(self):
        if self.overlay:
            self.activate_overlay()
            return
        items = self.menu_items()
        if not items:
            return
        index = max(0, min(len(items) - 1, self.selected_index))
        self.perform_action(items[index][1])

    def move_selection(self, delta):
        if self.overlay:
            buttons = self.overlay.get("buttons", [])
            if buttons:
                self.overlay["selected"] = (self.overlay.get("selected", 0) + delta) % len(buttons)
                self.update()
            return

        items = self.menu_items()
        if not items:
            return
        self.selected_index = (self.selected_index + delta) % len(items)
        self.update()

    def keyPressEvent(self, event: QKeyEvent):
        self.active_input = "keyboard"
        key = event.key()

        if key in (Qt.Key.Key_Up, Qt.Key.Key_W, Qt.Key.Key_Left, Qt.Key.Key_A):
            self.move_selection(-1)
        elif key in (Qt.Key.Key_Down, Qt.Key.Key_S, Qt.Key.Key_Right, Qt.Key.Key_D):
            self.move_selection(1)
        elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.activate_selected()
        elif key in (Qt.Key.Key_Escape, Qt.Key.Key_Backspace):
            if self.overlay and self.update_state == UpdateState.IDLE:
                self.close_overlay()
            elif self.update_state != UpdateState.RUNNING:
                QApplication.quit()
        event.accept()
        self.update()

    def menu_panel_size(self):
        panel_w = min(700, max(620, self.width() - 120))
        panel_h = min(460, max(390, self.height() - 150))
        panel_w = min(panel_w, self.width() - 40)
        panel_h = min(panel_h, self.height() - 100)
        return max(520, panel_w), max(340, panel_h)

    def menu_panel_rect(self):
        panel_w, panel_h = self.menu_panel_size()
        return QRect((self.width() - panel_w) // 2, (self.height() - panel_h) // 2, panel_w, panel_h)

    def top_bar_rect(self):
        panel = self.menu_panel_rect()
        return QRect(panel.x(), max(16, panel.y() - 78), panel.width(), 44)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        painter.fillRect(self.rect(), self.bg)
        self.draw_top_bar(painter)
        self.draw_panel(painter)
        if self.overlay:
            self.draw_overlay(painter)

    def draw_static_noise(self, painter):
        return

    def draw_top_bar(self, painter):
        bar = self.top_bar_rect()
        painter.fillRect(bar, self.light)
        painter.setFont(self.title_font)
        painter.setPen(self.dark_text)
        painter.drawText(bar.x() + 18, bar.y() + 31, "Gentleman Updater")

        time_text = datetime.now().strftime("%H:%M")
        time_width = painter.fontMetrics().horizontalAdvance(time_text)
        painter.drawText(bar.right() - 18 - time_width, bar.y() + 31, time_text)

    def draw_panel(self, painter):
        panel = self.menu_panel_rect()
        side_w = 48
        x, y, w, h = panel.x(), panel.y(), panel.width(), panel.height()

        painter.fillRect(panel, self.panel)
        painter.fillRect(QRect(x, y, side_w, h), self.light)

        painter.setFont(self.font)
        painter.setPen(self.dark_text)
        painter.save()
        painter.setClipRect(QRect(x, y, side_w, h))
        title = self.title_path()
        title_width = painter.fontMetrics().horizontalAdvance(title)
        title_start = (h - title_width) // 2
        painter.translate(x + 31, y + h - title_start)
        painter.rotate(-90)
        painter.drawText(0, 0, title)
        painter.restore()

        text_x = x + side_w + 22
        right_x = x + w - 34
        row_y = y + 34

        painter.setFont(self.title_font)
        painter.setPen(self.text)
        painter.drawText(text_x, row_y, "Gentleman Updater")

        painter.setFont(self.small_font)
        row_y += 34
        painter.setPen(self.dim_text)
        painter.drawText(text_x, row_y, f"Platform: {self.platform_name}")
        row_y += 24
        painter.drawText(text_x, row_y, "Controller-first update utility")

        row_y += 28
        self.draw_progress_bar(painter, QRect(text_x, row_y, right_x - text_x, 24))

        row_y += 44
        painter.setFont(self.small_font)
        painter.setPen(self.text if self.update_state != UpdateState.FAILED else self.error_text)
        status = self.update_message or self.status_text()
        painter.drawText(QRect(text_x, row_y, right_x - text_x, 24), Qt.AlignmentFlag.AlignLeft, status)

        row_y += 34
        log_rect = QRect(text_x, row_y, right_x - text_x, max(100, h - (row_y - y) - 104))
        self.draw_log(painter, log_rect)

        items = self.menu_items()
        if items:
            menu_y = y + h - 72
            button_gap = 18
            button_w = 300
            total_w = len(items) * button_w + (len(items) - 1) * button_gap
            start_x = x + side_w + ((w - side_w) - total_w) // 2
            painter.setFont(self.font)
            for index, (label, action) in enumerate(items):
                rect = QRect(start_x + index * (button_w + button_gap), menu_y, button_w, 34)
                if index == self.selected_index:
                    painter.fillRect(rect, self.light)
                    painter.setPen(self.dark_text)
                else:
                    painter.setPen(self.text)
                    painter.drawRect(rect)
                painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, label)

        guide = "D-pad Select   A Confirm   B Back"
        if self.update_state == UpdateState.RUNNING:
            guide = "Update running, please keep Gentleman closed"
        painter.setFont(self.small_font)
        painter.setPen(self.text)
        painter.drawText(QRect(text_x, y + h - 28, right_x - text_x, 20), Qt.AlignmentFlag.AlignCenter, guide)

    def status_text(self):
        if self.update_state == UpdateState.RUNNING:
            return "Updating..."
        if self.update_state == UpdateState.DONE:
            return "Update complete."
        if self.update_state == UpdateState.FAILED:
            return "Update failed."
        return "Ready to check for updates."

    def draw_progress_bar(self, painter, rect):
        painter.setPen(self.light)
        painter.drawRect(rect)
        inner = rect.adjusted(3, 3, -3, -3)
        fill_w = int(inner.width() * (self.progress_value / 100.0))
        if fill_w > 0:
            painter.fillRect(QRect(inner.x(), inner.y(), fill_w, inner.height()), self.light)
        painter.setFont(self.small_font)
        painter.setPen(self.text)
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, f"{self.progress_value}%")

    def draw_log(self, painter, rect):
        painter.fillRect(rect, QColor(20, 0, 6, 180))
        painter.setPen(self.light)
        painter.drawRect(rect)
        painter.setFont(self.small_font)
        metrics = painter.fontMetrics()
        line_height = metrics.height() + 2
        max_lines = max(1, (rect.height() - 16) // line_height)
        lines = self.logs[-max_lines:]
        y = rect.y() + 8 + metrics.ascent()
        painter.setPen(self.text)
        for line in lines:
            clean = line.replace("\t", "    ")
            while metrics.horizontalAdvance(clean) > rect.width() - 20 and clean:
                clean = clean[:-1]
            painter.drawText(rect.x() + 10, y, clean)
            y += line_height

    def draw_overlay(self, painter):
        ov = self.overlay
        labels = [item[0] for item in ov.get("buttons", [])]
        box_width = min(760, self.width() - 100)
        box_height = 300
        box = QRect(
            self.width() // 2 - box_width // 2,
            self.height() // 2 - box_height // 2,
            box_width,
            box_height,
        )

        painter.fillRect(self.rect(), QColor(0, 0, 0, 155))
        painter.fillRect(box, QColor(45, 0, 12, 252))
        painter.setPen(self.light)
        painter.drawRect(box)

        painter.setFont(self.title_font)
        painter.setPen(self.text)
        title_rect = QRect(box.x() + 24, box.y() + 20, box.width() - 48, 40)
        painter.drawText(title_rect, Qt.AlignmentFlag.AlignCenter, ov.get("title", ""))

        painter.setFont(self.font)
        message_rect = QRect(box.x() + 36, box.y() + 76, box.width() - 72, 110)
        painter.drawText(
            message_rect,
            Qt.TextFlag.TextWordWrap | Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
            ov.get("message", ""),
        )

        if labels:
            gap = 14
            columns = len(labels)
            button_w = min(300, (box.width() - 72 - gap * (columns - 1)) // columns)
            total_w = columns * button_w + gap * (columns - 1)
            start_x = box.center().x() - total_w // 2
            button_y = box.y() + box.height() - 82
            painter.setFont(self.font)
            for index, label in enumerate(labels):
                rect = QRect(start_x + index * (button_w + gap), button_y, button_w, 36)
                if index == ov.get("selected", 0):
                    painter.fillRect(rect, self.light)
                    painter.setPen(self.dark_text)
                else:
                    painter.setPen(self.text)
                    painter.drawRect(rect)
                painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, label)

        painter.setFont(self.small_font)
        painter.setPen(self.text)
        painter.drawText(
            QRect(box.x() + 16, box.bottom() - 30, box.width() - 32, 20),
            Qt.AlignmentFlag.AlignCenter,
            "D-pad Select   A Confirm   B Back",
        )

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

    def refresh_controller(self, force=False):
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

    def read_controller_buttons(self):
        buttons = {}
        if self.controller is None:
            return buttons
        try:
            for index in range(self.controller.get_numbuttons()):
                buttons[index] = bool(self.controller.get_button(index))
        except Exception:
            pass
        return buttons

    def read_controller_axes(self):
        axes = {}
        if self.controller is None:
            return axes
        try:
            for index in range(self.controller.get_numaxes()):
                axes[index] = float(self.controller.get_axis(index))
        except Exception:
            pass
        return axes

    def read_controller_hats(self):
        hats = []
        if self.controller is None:
            return hats
        try:
            for index in range(self.controller.get_numhats()):
                hats.append(self.controller.get_hat(index))
        except Exception:
            pass
        return hats

    def controller_any_input_active(self, buttons, axes, hats):
        if any(buttons.values()):
            return True
        if any(x != 0 or y != 0 for x, y in hats):
            return True
        return abs(axes.get(0, 0.0)) > 0.55 or abs(axes.get(1, 0.0)) > 0.55

    def controller_direction_state(self, buttons, axes, hats):
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

        axis_x = axes.get(0, 0.0)
        axis_y = axes.get(1, 0.0)
        if axis_x < -0.55:
            state["left"] = True
        elif axis_x > 0.55:
            state["right"] = True
        if axis_y < -0.55:
            state["up"] = True
        elif axis_y > 0.55:
            state["down"] = True

        return state

    def controller_active_actions(self, directions):
        if self.overlay:
            if directions.get("left") or directions.get("up"):
                return ["up"]
            if directions.get("right") or directions.get("down"):
                return ["down"]
        else:
            if directions.get("up") or directions.get("left"):
                return ["up"]
            if directions.get("down") or directions.get("right"):
                return ["down"]
        return []

    def handle_controller_repeat(self, active_actions):
        now = int(time.monotonic() * 1000)
        if not active_actions:
            self.controller_repeat_action = None
            self.controller_repeat_next_ms = 0
            return

        action = active_actions[0]
        if action != self.controller_repeat_action:
            self.controller_repeat_action = action
            self.controller_repeat_next_ms = now + 350
            self.move_selection(-1 if action == "up" else 1)
            return

        if now >= self.controller_repeat_next_ms:
            self.controller_repeat_next_ms = now + 90
            self.move_selection(-1 if action == "up" else 1)

    def read_xinput_state(self):
        if self.xinput is None:
            return None

        for index in range(4):
            state = XINPUT_STATE()
            try:
                result = self.xinput.XInputGetState(index, ctypes.byref(state))
            except Exception:
                return None

            if result == 0:
                self.xinput_index = index
                return state

        self.xinput_index = None
        self.xinput_button_state.clear()
        self.xinput_repeat_action = None
        self.xinput_repeat_next_ms = 0
        return None

    def xinput_direction_state(self, state):
        gamepad = state.Gamepad
        buttons = gamepad.wButtons
        directions = {
            "up": bool(buttons & XINPUT_GAMEPAD_DPAD_UP),
            "down": bool(buttons & XINPUT_GAMEPAD_DPAD_DOWN),
            "left": bool(buttons & XINPUT_GAMEPAD_DPAD_LEFT),
            "right": bool(buttons & XINPUT_GAMEPAD_DPAD_RIGHT),
        }

        deadzone = 9000
        if gamepad.sThumbLX < -deadzone:
            directions["left"] = True
        elif gamepad.sThumbLX > deadzone:
            directions["right"] = True

        if gamepad.sThumbLY > deadzone:
            directions["up"] = True
        elif gamepad.sThumbLY < -deadzone:
            directions["down"] = True

        return directions

    def xinput_buttons(self, state):
        buttons = state.Gamepad.wButtons
        return {
            "accept": bool(buttons & (XINPUT_GAMEPAD_A | XINPUT_GAMEPAD_START)),
            "back": bool(buttons & (XINPUT_GAMEPAD_B | XINPUT_GAMEPAD_BACK)),
        }

    def poll_xinput_controller(self):
        state = self.read_xinput_state()
        if state is None:
            return False

        self.controller_available = True
        self.active_input = "controller"

        directions = self.xinput_direction_state(state)
        active_actions = self.controller_active_actions(directions)

        now = int(time.monotonic() * 1000)
        if not active_actions:
            self.xinput_repeat_action = None
            self.xinput_repeat_next_ms = 0
        else:
            action = active_actions[0]
            if action != self.xinput_repeat_action:
                self.xinput_repeat_action = action
                self.xinput_repeat_next_ms = now + 350
                self.move_selection(-1 if action == "up" else 1)
            elif now >= self.xinput_repeat_next_ms:
                self.xinput_repeat_next_ms = now + 90
                self.move_selection(-1 if action == "up" else 1)

        buttons = self.xinput_buttons(state)
        for name, pressed in buttons.items():
            previous = self.xinput_button_state.get(name, False)
            if pressed and not previous:
                if name == "accept":
                    self.activate_selected()
                elif name == "back" and self.update_state != UpdateState.RUNNING:
                    if self.overlay and self.update_state == UpdateState.IDLE:
                        self.close_overlay()
                    else:
                        QApplication.quit()
            self.xinput_button_state[name] = pressed

        return True

    def poll_controller(self):
        xinput_handled = self.poll_xinput_controller()
        if xinput_handled:
            return

        if pygame is None:
            return

        try:
            events = pygame.event.get()
            controller_event_seen = False
            for event in events:
                if event.type in (pygame.JOYDEVICEADDED, pygame.JOYDEVICEREMOVED):
                    self.refresh_controller(force=True)
                elif event.type in (pygame.JOYBUTTONDOWN, pygame.JOYHATMOTION):
                    controller_event_seen = True
                elif event.type == pygame.JOYAXISMOTION and abs(getattr(event, "value", 0.0)) > 0.55:
                    controller_event_seen = True

            self.refresh_controller()
            if not self.controller_available or self.controller is None:
                return

            buttons = self.read_controller_buttons()
            axes = self.read_controller_axes()
            hats = self.read_controller_hats()

            if controller_event_seen or self.controller_any_input_active(buttons, axes, hats):
                self.active_input = "controller"

            directions = self.controller_direction_state(buttons, axes, hats)
            self.handle_controller_repeat(self.controller_active_actions(directions))

            accept_buttons = [0, 7]
            back_buttons = [1, 6]
            for button in accept_buttons:
                pressed = buttons.get(button, False)
                previous = self.controller_button_state.get(button, False)
                if pressed and not previous:
                    self.activate_selected()
                self.controller_button_state[button] = pressed

            for button in back_buttons:
                pressed = buttons.get(button, False)
                previous = self.controller_button_state.get(button, False)
                if pressed and not previous and self.update_state != UpdateState.RUNNING:
                    if self.overlay and self.update_state == UpdateState.IDLE:
                        self.close_overlay()
                    else:
                        QApplication.quit()
                self.controller_button_state[button] = pressed

            for button, pressed in buttons.items():
                if button not in accept_buttons and button not in back_buttons:
                    self.controller_button_state[button] = pressed
        except Exception:
            self.controller = None
            self.controller_available = False
            self.refresh_controller(force=True)


def main():
    app = QApplication(sys.argv)
    window = UpdaterWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
