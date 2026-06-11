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


@dataclass
class RomBrowserItem:
    name: str
    path: Path
    is_dir: bool
    normalized_name: str = ""

    @property
    def display_name(self) -> str:
        return self.normalized_name or self.name

    @property
    def marker(self) -> str:
        return "<DIR>" if self.is_dir else ""


def load_launcher(path: Path) -> LauncherConfig:
    data = json.loads(path.read_text(encoding="utf-8"))

    return LauncherConfig(
        path=path,
        launcher_type=data.get("type", "standalone"),
        emulator=data.get("emulator", ""),
        rom_directory=data.get("rom_directory", ""),
        extensions=[ext.lower() if ext.startswith(".") else f".{ext.lower()}" for ext in data.get("extensions", [])],
        arguments=data.get("arguments", "\"{rom}\""),
        recursive=bool(data.get("recursive", True)),
        core=data.get("core", ""),
        working_directory=data.get("working_directory", ""),
        system=str(data.get("system", "")),
        emulator_name=str(data.get("emulator_name", "")).strip(),
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
