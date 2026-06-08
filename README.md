# Gentleman

Gentleman is a MiSTer-inspired PC frontend prototype for Windows.

The goal is to bring the simple, fast, menu-driven feel of the MiSTer interface to a PC-based emulator setup. Instead of using a database-heavy frontend, Gentleman builds its menu from a normal `menu/` folder. Folders become menu categories, and JSON files become launcher entries.

For example, `menu/Consoles/PS2.json` will make `Consoles` appear in the main menu, with `PS2` listed inside it.

Gentleman is designed to stay simple and transparent. Users can either create launcher JSON files manually, or create them from inside the Gentleman system menu. Each launcher points to an emulator executable, a ROM folder, supported file extensions, and optional launch arguments. RetroArch launchers can also define a specific core.

This project is experimental and not affiliated with the official MiSTer FPGA project.

## Run

```bash
pip install -r requirements.txt
python main.py
```

## Navigation

- Up / Down: move 1 item
- Left / Right: move 10 items
- Enter: select
- Esc / Backspace: go back
- Esc / Backspace on the home screen: open the Gentleman System Menu
- F11: toggle fullscreen
- F5: refresh menu
- Back / Esc on the home screen opens the Gentleman Menu, where you can create launchers and set or clear a wallpaper

## Launcher files

Standalone example:

```json
{
  "type": "standalone",
  "emulator": "D:/Emulators/PCSX2/pcsx2-qt.exe",
  "rom_directory": "D:/Games/PS2",
  "extensions": [".iso", ".chd", ".cso"],
  "arguments": "-fullscreen \"{rom}\"",
  "recursive": true
}
```

RetroArch example:

```json
{
  "type": "retroarch",
  "emulator": "D:/Emulators/RetroArch/retroarch.exe",
  "core": "D:/Emulators/RetroArch/cores/snes9x_libretro.dll",
  "rom_directory": "D:/Games/SNES",
  "extensions": [".sfc", ".smc", ".zip"],
  "arguments": "-L \"{core}\" \"{rom}\"",
  "recursive": true
}
```
