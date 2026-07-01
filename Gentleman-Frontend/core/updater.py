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


def is_macos() -> bool:
    return current_platform_name() == "darwin"


def updater_supported() -> bool:
    return is_windows() or is_linux() or is_macos()


def get_app_folder() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent

    return Path(__file__).resolve().parent.parent


def get_macos_app_bundle() -> Path | None:
    if not is_macos():
        return None

    executable_path = Path(sys.executable).resolve() if getattr(sys, "frozen", False) else Path(__file__).resolve()
    for parent in [executable_path] + list(executable_path.parents):
        if parent.suffix.lower() == ".app":
            return parent
    return None


def get_install_folder() -> Path:
    bundle = get_macos_app_bundle()
    if bundle is not None:
        return bundle.parent
    return get_app_folder()


def get_gentleman_updater_filename() -> str:
    if is_windows():
        return "Gentleman-Updater.exe"

    return "Gentleman-Updater"


def get_gentleman_updater_path() -> Path:
    if is_macos():
        install_folder = get_install_folder()
        candidates = [
            install_folder / "Gentleman-Updater.app",
            install_folder / "Gentleman-Updater",
            install_folder / "Gentleman-Updater.app" / "Contents" / "MacOS" / "Gentleman-Updater",
            get_app_folder() / get_gentleman_updater_filename(),
        ]
    else:
        candidates = [get_app_folder() / get_gentleman_updater_filename()]

    for candidate in candidates:
        if candidate.exists():
            return candidate

    return candidates[0]


def get_macos_application_support_folder() -> Path:
    support_dir = Path.home() / "Library" / "Application Support" / "Gentleman"
    support_dir.mkdir(parents=True, exist_ok=True)
    return support_dir


def get_update_now_path() -> Path:
    if is_macos() and getattr(sys, "frozen", False):
        return get_macos_application_support_folder() / "updatenow.txt"
    return get_install_folder() / "updatenow.txt"


def make_executable(path: Path):
    if not path.exists():
        return

    if not (is_linux() or is_macos()):
        return

    current_mode = os.stat(path).st_mode
    os.chmod(path, current_mode | 0o100 | 0o010 | 0o001)


def gentleman_updater_available() -> bool:
    if not updater_supported():
        return False

    updater_path = get_gentleman_updater_path()

    if not updater_path.exists():
        return False

    if is_linux() or is_macos():
        make_executable(updater_path)

    return True


def launch_gentleman_updater() -> bool:
    if not updater_supported():
        return False

    updater_path = get_gentleman_updater_path()

    if not updater_path.exists():
        return False

    if is_linux() or is_macos():
        make_executable(updater_path)

    update_now_path = get_update_now_path()
    update_now_path.parent.mkdir(parents=True, exist_ok=True)
    update_now_path.write_text("", encoding="utf-8")

    if is_macos() and updater_path.suffix.lower() == ".app":
        subprocess.Popen(["open", str(updater_path)], cwd=str(updater_path.parent), shell=False)
        return True

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
