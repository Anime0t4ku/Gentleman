from __future__ import annotations

import json
import os
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
        raw_extensions = [".lnk"]

    return LauncherConfig(
        path=path,
        launcher_type=launcher_type,
        emulator=data.get("emulator", shortcut_path if launcher_type == "shortcut" else ""),
        rom_directory=data.get("rom_directory", shortcut_directory if launcher_type == "shortcut_folder" else ""),
        extensions=[ext.lower() if ext.startswith(".") else f".{ext.lower()}" for ext in raw_extensions],
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
                    if entry.is_dir(follow_symlinks=False):
                        items.append(RomBrowserItem(entry.name, Path(entry.path), True))
                    elif entry.is_file(follow_symlinks=False):
                        _, ext = os.path.splitext(entry.name)
                        if ext.lower() in allowed_extensions:
                            normalized_name = ""
                            if arcade_names is not None:
                                normalized_name = arcade_names.get(Path(entry.name).stem.lower(), "")
                            items.append(RomBrowserItem(entry.name, Path(entry.path), False, normalized_name))
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


def launch_external_process(command: str, cwd: str | None = None) -> subprocess.Popen:
    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP

    return subprocess.Popen(
        command,
        cwd=cwd if cwd and os.path.isdir(cwd) else None,
        shell=True,
        env=external_process_env(),
        creationflags=creationflags,
    )


def launch_link_shortcut(shortcut: Path) -> subprocess.Popen:
    shortcut_path = str(shortcut)
    cwd = str(shortcut.parent)

    if os.name == "nt":
        command = f'start "" "{shortcut_path}"'
    elif sys.platform == "darwin":
        command = f'open "{shortcut_path}"'
    else:
        command = f'xdg-open "{shortcut_path}"'

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

    command = f'"{executable}" {args}'.strip()
    cwd = config.working_directory or str(Path(executable).parent)

    return launch_external_process(command, cwd)
