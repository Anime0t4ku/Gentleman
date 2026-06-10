import json
import os
import platform
import re
import shutil
import stat
import sys
import tarfile
import traceback
import urllib.request
import zipfile
from pathlib import Path

from PyQt6.QtCore import QThread, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QApplication,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)


APP_NAME = "Gentleman-Updater"
GITHUB_OWNER = "Anime0t4ku"
GITHUB_REPO = "Gentleman"

CONFIG_FILE = "config.json"
UPDATE_NOW_FILE = "updatenow.txt"

WINDOWS_TARGET_EXE = "Gentleman.exe"
LINUX_TARGET_EXE = "Gentleman"

WINDOWS_ZIP_KEYWORDS = ["Windows", ".zip"]
LINUX_TAR_KEYWORDS = ["Linux", ".tar.gz"]

INCLUDE_PRERELEASES = False


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
    config_path = base_path / CONFIG_FILE
    version_txt_path = base_path / "version.txt"
    app_info_path = base_path / "app" / "app_info.py"

    version_text = None

    if config_path.exists():
        with open(config_path, "r", encoding="utf-8") as f:
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
            "Expected config.json with app_version, version.txt, or app/app_info.py."
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

    def is_safe_extract_path(self, destination, target_path):
        destination = destination.resolve()
        target_path = target_path.resolve()

        try:
            target_path.relative_to(destination)
            return True
        except ValueError:
            return False


class UpdaterWindow(QWidget):
    def __init__(self):
        super().__init__()

        self.worker = None
        self.base_path = app_folder()
        self.update_now_path = self.base_path / UPDATE_NOW_FILE
        self.auto_update_mode = self.update_now_path.exists()

        try:
            self.platform_info = current_platform()
            platform_name = self.platform_info["name"]
        except Exception:
            platform_name = platform.system() or "Unknown"

        self.setWindowTitle(APP_NAME)
        self.setMinimumWidth(520)
        self.setMinimumHeight(360)

        self.title_label = QLabel("Gentleman Updater")
        self.title_label.setStyleSheet("font-size: 20px; font-weight: bold;")

        self.info_label = QLabel(
            f"This tool checks your installed Gentleman version and downloads the latest {platform_name} build if needed."
        )
        self.info_label.setWordWrap(True)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)

        self.log_box = QTextEdit()
        self.log_box.setReadOnly(True)

        self.update_button = QPushButton("Check and Update")
        self.update_button.clicked.connect(self.start_update)

        self.close_button = QPushButton("Close")
        self.close_button.clicked.connect(self.close)

        layout = QVBoxLayout(self)
        layout.addWidget(self.title_label)
        layout.addWidget(self.info_label)
        layout.addWidget(self.progress)
        layout.addWidget(self.log_box)
        layout.addWidget(self.update_button)
        layout.addWidget(self.close_button)

        if self.auto_update_mode:
            self.update_button.setVisible(False)
            self.close_button.setVisible(False)
            self.append_log(f"{UPDATE_NOW_FILE} found. Starting automatic update check...")
            QTimer.singleShot(250, self.start_update)

    def append_log(self, message):
        self.log_box.append(message)

    def start_update(self):
        self.update_button.setEnabled(False)
        self.progress.setValue(0)

        if not self.auto_update_mode:
            self.log_box.clear()

        self.worker = UpdateWorker()
        self.worker.status_changed.connect(self.append_log)
        self.worker.progress_changed.connect(self.progress.setValue)
        self.worker.finished_ok.connect(self.update_finished)
        self.worker.failed.connect(self.update_failed)
        self.worker.start()

    def remove_update_now_file(self):
        if self.update_now_path.exists():
            try:
                self.update_now_path.unlink()
                self.append_log(f"Removed {UPDATE_NOW_FILE}.")
            except Exception as e:
                self.append_log(f"Could not remove {UPDATE_NOW_FILE}: {e}")

    def update_finished(self, message):
        self.append_log(message)
        self.update_button.setEnabled(True)

        if self.auto_update_mode:
            self.remove_update_now_file()

            QMessageBox.information(
                self,
                "Update Complete",
                (
                    f"{message}\n\n"
                    "The update has finished successfully.\n\n"
                    "Press OK to close the updater. You can then start Gentleman again."
                ),
            )

            QApplication.quit()
            return

        QMessageBox.information(self, APP_NAME, message)

    def update_failed(self, error):
        self.append_log("Update failed.")
        self.append_log(error)
        self.update_button.setEnabled(True)

        if self.auto_update_mode:
            self.append_log(f"{UPDATE_NOW_FILE} was not removed because the update failed.")
            self.close_button.setVisible(True)
            return

        QMessageBox.critical(self, APP_NAME, "Update failed. Check the log for details.")


def main():
    app = QApplication(sys.argv)
    window = UpdaterWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
