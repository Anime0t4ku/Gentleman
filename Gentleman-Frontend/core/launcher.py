from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass
class LauncherConfig:
    path: Path
    launcher_type: str
    emulator: str
    rom_directory: str
    extensions: list[str]
    arguments: str
    recursive: bool = True
    core: str = ""
    working_directory: str = ""
    system: str = ""
    emulator_name: str = ""
    shortcut_path: str = ""
    shortcut_directory: str = ""
    link_path: str = ""
    link_directory: str = ""


@dataclass
class RomBrowserItem:
    name: str
    path: Path
    is_dir: bool
    normalized_name: str = ""
    multi_disc_paths: list[Path] | None = None
    multi_disc_names: list[str] | None = None
    multi_disc_scrape_name: str = ""

    @property
    def display_name(self) -> str:
        return self.normalized_name or self.name

    @property
    def marker(self) -> str:
        if self.is_dir:
            return "<DIR>"
        if self.multi_disc_paths:
            return "<DISC>"
        return ""

    @property
    def is_multi_disc_group(self) -> bool:
        return bool(self.multi_disc_paths and len(self.multi_disc_paths) > 1)


def default_shortcut_extensions_for_platform() -> list[str]:
    if os.name == "nt":
        return [".lnk", ".url"]
    if sys.platform == "darwin":
        return [".app", ".command", ".sh", ".webloc"]
    return [".desktop", ".sh", ".appimage"]


def file_matches_extensions(path: Path, allowed_extensions: set[str]) -> bool:
    suffix = path.suffix.lower()
    return suffix in allowed_extensions or ("" in allowed_extensions and suffix == "")


def load_launcher(path: Path) -> LauncherConfig:
    data = json.loads(path.read_text(encoding="utf-8"))
    launcher_type = str(data.get("type", "standalone")).strip().lower()

    if launcher_type == "link":
        launcher_type = "shortcut"
    elif launcher_type == "link_folder":
        launcher_type = "shortcut_folder"

    shortcut_path = str(
        data.get("shortcut_path", data.get("link_path", data.get("emulator", "")))
    ).strip()
    shortcut_directory = str(
        data.get("shortcut_directory", data.get("link_directory", data.get("rom_directory", "")))
    ).strip()

    raw_extensions = data.get("extensions", [])
    if launcher_type == "shortcut_folder" and not raw_extensions:
        raw_extensions = default_shortcut_extensions_for_platform()

    return LauncherConfig(
        path=path,
        launcher_type=launcher_type,
        emulator=data.get("emulator", shortcut_path if launcher_type == "shortcut" else ""),
        rom_directory=data.get("rom_directory", shortcut_directory if launcher_type == "shortcut_folder" else ""),
        extensions=[("" if str(ext).strip() == "" else (str(ext).lower() if str(ext).startswith(".") else f".{str(ext).lower()}")) for ext in raw_extensions],
        arguments=data.get("arguments", "\"{rom}\""),
        recursive=bool(data.get("recursive", True)),
        core=data.get("core", ""),
        working_directory=data.get("working_directory", ""),
        system=str(data.get("system", "")),
        emulator_name=str(data.get("emulator_name", "")).strip(),
        shortcut_path=shortcut_path,
        shortcut_directory=shortcut_directory,
        link_path=shortcut_path,
        link_directory=shortcut_directory,
    )


def scan_rom_folder(
    config: LauncherConfig,
    folder: Path,
    arcade_names: dict[str, str] | None = None,
) -> list[RomBrowserItem]:
    allowed_extensions = {ext.lower() for ext in config.extensions}
    items: list[RomBrowserItem] = []

    try:
        with os.scandir(folder) as entries:
            for entry in entries:
                if entry.name.startswith("."):
                    continue

                try:
                    entry_path = Path(entry.path)
                    mac_app_bundle = sys.platform == "darwin" and entry_path.suffix.lower() == ".app" and entry.is_dir(follow_symlinks=False)

                    if mac_app_bundle and file_matches_extensions(entry_path, allowed_extensions):
                        items.append(RomBrowserItem(entry.name, entry_path, False))
                    elif entry.is_dir(follow_symlinks=False):
                        items.append(RomBrowserItem(entry.name, entry_path, True))
                    elif entry.is_file(follow_symlinks=False):
                        if file_matches_extensions(entry_path, allowed_extensions):
                            normalized_name = ""
                            if arcade_names is not None:
                                normalized_name = arcade_names.get(Path(entry.name).stem.lower(), "")
                            items.append(RomBrowserItem(entry.name, entry_path, False, normalized_name))
                except OSError:
                    continue
    except OSError:
        return []

    folders = [item for item in items if item.is_dir]
    files = [item for item in items if not item.is_dir]

    folders.sort(key=lambda item: item.name.lower())
    files.sort(key=lambda item: item.name.lower())

    return folders + files


def external_process_env() -> dict[str, str]:
    env = os.environ.copy()

    for key in (
        "QT_PLUGIN_PATH",
        "QT_QPA_PLATFORM_PLUGIN_PATH",
        "QML2_IMPORT_PATH",
        "PYTHONHOME",
        "PYTHONPATH",
    ):
        env.pop(key, None)

    if getattr(sys, "frozen", False):
        bundle_path = str(getattr(sys, "_MEIPASS", ""))

        if bundle_path:
            path_parts = []
            for part in env.get("PATH", "").split(os.pathsep):
                if part and bundle_path.lower() not in part.lower():
                    path_parts.append(part)

            env["PATH"] = os.pathsep.join(path_parts)

    return env


def launch_external_process(command: str | list[str], cwd: str | None = None) -> subprocess.Popen:
    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP

    return subprocess.Popen(
        command,
        cwd=cwd if cwd and os.path.isdir(cwd) else None,
        shell=isinstance(command, str),
        env=external_process_env(),
        creationflags=creationflags,
    )


def quote_command_part(value: str) -> str:
    if os.name == "nt":
        return f'"{value}"'
    return shlex.quote(value)


def is_macos_app_bundle(path: Path | str) -> bool:
    try:
        return sys.platform == "darwin" and Path(path).suffix.lower() == ".app" and Path(path).is_dir()
    except Exception:
        return False


def macos_open_bundle_command(bundle_path: Path, args: str = "") -> str:
    quoted_bundle = quote_command_part(str(bundle_path))
    args = str(args or "").strip()
    if args:
        return f"open -n {quoted_bundle} --args {args}"
    return f"open -n {quoted_bundle}"


def launch_link_shortcut(shortcut: Path) -> subprocess.Popen:
    shortcut_path = str(shortcut)
    cwd = str(shortcut.parent)

    if os.name == "nt":
        command = f'start "" "{shortcut_path}"'
    elif sys.platform == "darwin":
        command = ["open", shortcut_path]
    else:
        command = ["xdg-open", shortcut_path]

    return launch_external_process(command, cwd)


def build_command(config: LauncherConfig, rom: Path) -> tuple[str, str]:
    args = config.arguments
    args = args.replace("{rom}", str(rom))
    args = args.replace("{core}", config.core)
    args = args.replace("{emulator}", config.emulator)
    args = args.replace("{rom_dir}", config.rom_directory)

    return config.emulator, args


def launch_rom(config: LauncherConfig, rom: Path) -> subprocess.Popen:
    executable, args = build_command(config, rom)
    executable_path = Path(executable)

    if is_macos_app_bundle(executable_path):
        command = macos_open_bundle_command(executable_path, args)
        cwd = config.working_directory or str(executable_path.parent)
        return launch_external_process(command, cwd)

    command = f'{quote_command_part(executable)} {args}'.strip()
    cwd = config.working_directory or str(executable_path.parent)

    return launch_external_process(command, cwd)


def launch_application(config: LauncherConfig) -> subprocess.Popen:
    executable = str(config.emulator or config.shortcut_path).strip()
    args = str(config.arguments or "").strip()

    if not executable:
        raise ValueError("Missing application path")

    executable_path = Path(executable)
    if is_macos_app_bundle(executable_path):
        command = macos_open_bundle_command(executable_path, args)
        cwd = config.working_directory or str(executable_path.parent)
        return launch_external_process(command, cwd)

    command = f'{quote_command_part(executable)} {args}'.strip()
    cwd = config.working_directory or str(executable_path.parent)
    return launch_external_process(command, cwd)
