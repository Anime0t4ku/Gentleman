from __future__ import annotations
import json, os, subprocess
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


def load_launcher(path: Path) -> LauncherConfig:
    data = json.loads(path.read_text(encoding='utf-8'))
    return LauncherConfig(
        path=path,
        launcher_type=data.get('type', 'standalone'),
        emulator=data.get('emulator', ''),
        rom_directory=data.get('rom_directory', ''),
        extensions=[ext.lower() if ext.startswith('.') else f'.{ext.lower()}' for ext in data.get('extensions', [])],
        arguments=data.get('arguments', '"{rom}"'),
        recursive=bool(data.get('recursive', True)),
        core=data.get('core', ''),
        working_directory=data.get('working_directory', ''),
    )


def scan_roms(config: LauncherConfig) -> list[Path]:
    rom_dir = Path(config.rom_directory)
    if not rom_dir.exists():
        return []
    files: list[Path] = []
    for ext in config.extensions:
        pattern = f'*{ext}'
        files.extend(rom_dir.rglob(pattern) if config.recursive else rom_dir.glob(pattern))
    files = [p for p in files if p.is_file()]
    files.sort(key=lambda p: p.name.lower())
    return files


def build_command(config: LauncherConfig, rom: Path) -> tuple[str, str]:
    args = config.arguments
    args = args.replace('{rom}', str(rom))
    args = args.replace('{core}', config.core)
    args = args.replace('{emulator}', config.emulator)
    args = args.replace('{rom_dir}', config.rom_directory)
    return config.emulator, args


def launch_rom(config: LauncherConfig, rom: Path) -> None:
    executable, args = build_command(config, rom)
    command = f'"{executable}" {args}'.strip()
    cwd = config.working_directory or str(Path(executable).parent)
    subprocess.Popen(command, cwd=cwd if os.path.isdir(cwd) else None, shell=True)
