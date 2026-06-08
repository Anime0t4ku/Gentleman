from __future__ import annotations

import json
import os
import subprocess
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


@dataclass
class RomBrowserItem:
    name: str
    path: Path
    is_dir: bool

    @property
    def display_name(self) -> str:
        return self.name

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
    )


def scan_rom_folder(config: LauncherConfig, folder: Path) -> list[RomBrowserItem]:
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
                            items.append(RomBrowserItem(entry.name, Path(entry.path), False))
                except OSError:
                    continue
    except OSError:
        return []

    return items


def build_command(config: LauncherConfig, rom: Path) -> tuple[str, str]:
    args = config.arguments
    args = args.replace("{rom}", str(rom))
    args = args.replace("{core}", config.core)
    args = args.replace("{emulator}", config.emulator)
    args = args.replace("{rom_dir}", config.rom_directory)

    return config.emulator, args


def launch_rom(config: LauncherConfig, rom: Path) -> None:
    executable, args = build_command(config, rom)

    command = f'"{executable}" {args}'.strip()
    cwd = config.working_directory or str(Path(executable).parent)

    subprocess.Popen(command, cwd=cwd if os.path.isdir(cwd) else None, shell=True)
