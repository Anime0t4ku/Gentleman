from __future__ import annotations

import json
import os
import platform
import re
import subprocess
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from app.app_info import APP_VERSION, GITHUB_OWNER, GITHUB_REPO


@dataclass
class UpdateInfo:
    update_available: bool
    current_version: str
    latest_version: str
    release_url: str
    release_name: str
    release_body: str


def normalize_version(version: str) -> tuple[int, int, int]:
    if not version:
        return (0, 0, 0)

    match = re.search(r"(\d+)\.(\d+)\.(\d+)", version)
    if not match:
        return (0, 0, 0)

    return tuple(int(part) for part in match.groups())


def check_for_update(timeout: int = 10) -> UpdateInfo:
    url = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest"
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "Gentleman-Updater",
        },
    )

    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = json.loads(response.read().decode("utf-8"))

    latest_version = data.get("tag_name", "") or data.get("name", "")
    release_url = data.get("html_url", "")
    release_name = data.get("name", latest_version)
    release_body = data.get("body", "") or ""

    current_tuple = normalize_version(APP_VERSION)
    latest_tuple = normalize_version(latest_version)

    return UpdateInfo(
        update_available=latest_tuple > current_tuple,
        current_version=APP_VERSION,
        latest_version=latest_version,
        release_url=release_url,
        release_name=release_name,
        release_body=release_body,
    )


def current_platform_name() -> str:
    return platform.system().lower()


def is_windows() -> bool:
    return current_platform_name() == "windows"


def is_linux() -> bool:
    return current_platform_name() == "linux"


def updater_supported() -> bool:
    return is_windows() or is_linux()


def get_app_folder() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent

    return Path(__file__).resolve().parent.parent


def get_gentleman_updater_filename() -> str:
    if is_windows():
        return "Gentleman-Updater.exe"

    if is_linux():
        return "Gentleman-Updater"

    return "Gentleman-Updater"


def get_gentleman_updater_path() -> Path:
    return get_app_folder() / get_gentleman_updater_filename()


def get_update_now_path() -> Path:
    return get_app_folder() / "updatenow.txt"


def make_executable(path: Path):
    if not path.exists():
        return

    if not is_linux():
        return

    current_mode = os.stat(path).st_mode
    os.chmod(path, current_mode | 0o100 | 0o010 | 0o001)


def gentleman_updater_available() -> bool:
    if not updater_supported():
        return False

    updater_path = get_gentleman_updater_path()

    if not updater_path.exists():
        return False

    if is_linux():
        make_executable(updater_path)

    return True


def launch_gentleman_updater() -> bool:
    if not updater_supported():
        return False

    updater_path = get_gentleman_updater_path()

    if not updater_path.exists():
        return False

    if is_linux():
        make_executable(updater_path)

    get_update_now_path().write_text("", encoding="utf-8")

    subprocess.Popen(
        [str(updater_path)],
        cwd=str(updater_path.parent),
        shell=False,
    )

    return True


def open_release_page(url: str):
    url = str(url or "").strip()

    if not url:
        return

    if sys.platform.startswith("win"):
        subprocess.Popen(["explorer", url])
        return

    if sys.platform == "darwin":
        subprocess.Popen(["open", url])
        return

    subprocess.Popen(["xdg-open", url])
