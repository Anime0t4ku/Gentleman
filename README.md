![Gentleman Logo](Gentleman-Frontend/assets/logo.png)

Gentleman is a MiSTer-inspired emulator frontend for PC.

The goal is to bring the simple, fast, menu-driven feel of the MiSTer interface to a PC-based emulator setup while still giving users the option of a richer visual library view. Gentleman can be used as a classic folder-based frontend, or with Modern Mode for boxart, metadata, summaries, and grid/list layouts.

The classic menu is built from a normal `menu/` folder. Folders become menu categories, and JSON files become launcher entries. For example, `menu/Consoles/PS2.json` will make `Consoles` appear in the main menu, with `PS2` listed inside it.

Gentleman is designed to stay simple and transparent. Users can create launcher JSON files manually, or create and edit them from inside the Gentleman menu. Each launcher points to an emulator executable, a ROM folder, supported file extensions, and optional launch arguments. RetroArch launchers can also define a specific core.

Modern Mode is optional and adds ScreenScraper-powered metadata, summaries, and boxart on top of the same launcher setup. Scraped files are stored locally, so the normal transparent folder and JSON structure remains the foundation of the app.

This project is experimental and not affiliated with the official MiSTer FPGA project.

![Gentleman Screenshot](assets/Screenshot.png)

## Platform Status

Gentleman is currently available for Windows and macOS.

Windows remains the main development platform, but macOS builds are now provided as well. Some behavior may still differ between platforms, especially around paths, file pickers, application handling, focus behavior, and packaging.

Linux support may be added later, but it would require more work than a simple port. Linux setups can handle emulators in many different ways, including native packages, AppImages, Flatpaks, custom scripts, and different desktop environments. Because of that, emulator detection, launching behavior, paths, and focus handling would need extra testing and platform-specific work before Linux can be considered officially supported.

## Current Features

- MiSTer-inspired menu layout
- Folder-based menu structure
- Optional subfolder support inside the `menu/` folder
- JSON-based launcher entries
- In-app launcher creation
- In-app launcher editing
- Launcher save folder editing
- Launcher and folder options from the menu
- Confirmed launcher removal
- Confirmed folder removal
- Contextual game search with controller and keyboard shortcuts
- Standalone emulator launcher support
- RetroArch launcher support with core selection
- Application launcher support
- Fast folder browsing without generating a database
- Classic List and Modern game view modes
- Modern Mode with boxart, metadata, and summaries
- Modern View layouts: Detailed List, Simple List, and Grid
- Resolution-aware Modern Mode layouts
- Ultra-wide aware centered Modern Mode content area
- ScreenScraper integration for metadata, summaries, and boxart
- Full library scraping and single-game scraping
- Local metadata and artwork storage
- Recent games list
- Favorites list
- Favorite/unfavorite support
- Clear Recent and Clear Favorites actions
- Optional Favorites, Recent, Systems, and Emulators menu entries
- Automatic Emulators menu generated from launcher JSON files
- System selection using a predefined system list
- Wallpaper support through Settings > Display
- Custom `.json` theme support
- Fullscreen support
- Fullscreen-at-launch setting
- Optional MiSTer-style auto-hide menu setting
- Controller navigation support
- A/B and X/Y controller button swap settings
- API support for third-party apps and remote devices
- Active-session API reporting and close controls
- In-game OSD for running games, emulators, and applications
- Manual update check from the Gentleman Menu
- Optional external Gentleman Updater support
- API active indicator in the top bar
- App icon and logo support
- Logo can be enabled or disabled from Settings
- Gentleman Menu shows the local IP address and app version
- Built-in links for issue reports, feature requests, and project support

## Run

```bash
pip install -r requirements.txt
python main.py
```

## Updating

Gentleman can check GitHub for stable releases. The update check can be started manually from the Gentleman Menu, and the launch-time update check can be enabled or disabled from Settings.

The updater is optional and is released as a separate download for each supported platform. To use it, download the updater package that matches your system, extract it if needed, and place the updater next to the main Gentleman application.

On Windows, place `Gentleman-Updater.exe` next to `Gentleman.exe`.

On macOS, place the Gentleman Updater application next to the main Gentleman application.

When an update is found, Gentleman can then start the updater for you.

The normal Gentleman release package only contains the main application. User folders such as `config/`, `menu/`, `themes/`, and locally scraped metadata or artwork folders are created on first launch or when needed, and are not part of the release package.

## Navigation

### Keyboard

- Up / Down: move 1 item
- Left / Right: move 10 items
- Enter / Space: select
- Esc / Backspace: go back
- Esc / Backspace on the home screen: open the Gentleman Menu
- Ctrl + F: search the current context
- Y: open options for the selected folder or launcher
- F11: toggle fullscreen
- F5: refresh menu
- F: favorite or unfavorite selected game
- L: open the selected game's summary in Modern Mode
- Ctrl + Alt + G: open the in-game OSD while a game, emulator, or application is running

### Controller

Controller support is available through `pygame`.

Default controller mapping:

- D-pad / left stick Up / Down: move 1 item
- D-pad / left stick Left / Right: move 10 items
- A: select
- B: back
- X: favorite or unfavorite selected game
- L1: open the selected game's summary in Modern Mode
- Y: open options for the selected folder or launcher
- R1: search the current context
- Start: select
- Back: back
- Hold L1 + L2 + L3 + R1 + R2 + R3 for one second: open the in-game OSD

The button layout can be adjusted from Settings:

- Swap A/B
- Swap X/Y

## Search

Gentleman includes contextual search.

Search can be opened with:

- `Ctrl + F` on a keyboard
- `R1` on a controller

Search depends on where it is opened:

- From the root menu, it searches all folders and launchers under `menu/`
- From a menu folder, it searches that folder and its subfolders
- From inside a launcher or game list, it searches that launcher/list
- From search results, opening search again searches the same scope

Search uses the existing onscreen keyboard. Results are shown in the normal Gentleman menu style, and selecting a result launches it like a normal game or launcher entry.

## Gentleman Menu

Press Backspace or Esc on the home screen to open the Gentleman Menu.

The local IP address and Gentleman version are shown at the top of this menu.

From here you can:

- Toggle Fullscreen
- Create Launcher
- Edit Launcher
- Refresh Menu
- Open Settings
- Report Issues & Requests
- Open Support the Project
- Check for Updates
- View About
- Exit

Report Issues & Requests opens the Gentleman GitHub issue templates.

Support the Project opens a submenu with links to Ko-fi and Buy Me a Coffee.

## Folder and Launcher Options

The selected folder or launcher can be managed directly from the menu.

Open options with:

- `Y` on a keyboard
- `Y` on a controller

Folder options include:

- Open Folder
- Remove Folder
- Cancel

Launcher options include:

- Open Launcher
- Edit Launcher
- Remove Launcher
- Cancel

Removing a launcher or folder requires confirmation first. Folder removal also removes launchers and subfolders inside that folder.

## Settings

Settings are organized into categories.

### Display

- Fullscreen at Launch: Enabled / Disabled
- Logo: Enabled / Disabled
- Theme: Default or custom themes from the `themes/` folder
- Wallpaper
- Game View: Classic / Modern
- Modern View: Detailed List / Simple List / Grid, shown only when Modern Mode is active
- Menu Size: 100% / 125% / 150%
- Auto Hide Menu: Disabled / 10 sec / 15 sec / 20 sec / 30 sec / 45 sec / 1 min

### Menu Items

- Systems Menu: Enabled / Disabled
- Emulators Menu: Enabled / Disabled
- Recent Menu: Enabled / Disabled
- Favorites Menu: Enabled / Disabled
- Arcade ROM Names: Enabled / Disabled
- Clear Recent
- Clear Favorites

### Controls

- Swap A/B: Enabled / Disabled
- Swap X/Y: Enabled / Disabled
- In-Game OSD: Enabled / Disabled

### System

- API: Enabled / Disabled
- Update Check at Launch: Enabled / Disabled

## Custom Themes

Gentleman supports custom `.json` theme files.

The built-in Default theme is always available and is not stored as a visible `default.json` file. To add your own themes, place `.json` files in the `themes/` folder and select them from Settings > Display > Theme.

Theme files use a small set of user-facing color fields. Gentleman automatically applies these colors across the menu, text, highlights, dialogs, overlays, and related interface elements.

Example using the Default theme values:

```json
{
  "name": "Default Style Example",
  "colors": {
    "background_color": "#000000",
    "menu_color": "#37000FDC",
    "highlight_color": "#DCB9BE",
    "text_color": "#F5EBEB",
    "highlight_text_color": "#28000A"
  }
}
```

The color format is 8-digit hex:

```text
#RRGGBBAA
```

The last two characters control opacity. For example, `DC` is about 86% opacity.

## Auto Hide Menu

Gentleman includes an optional MiSTer-style auto-hide menu feature.

When enabled, the main menu and top bar hide after the selected idle time. The logo stays visible when enabled. Any keyboard or controller input shows the menu again.

Auto-hide is ignored while dialogs, confirmations, file browsers, launcher forms, and the onscreen keyboard are active.

The setting is disabled by default.

## Modern Mode

Modern Mode is an optional visual game browser. It keeps the normal launcher and ROM folder setup, but adds locally stored metadata, summaries, and boxart for games that have been scraped.

Modern Mode can be selected from Settings > Display > Game View. When Modern Mode is active, Settings > Display also shows Modern View.

Modern View options are:

- Detailed List, shows the game list with boxart, metadata, and summary text.
- Simple List, shows the game list with larger boxart and hides inline metadata and summary text.
- Grid, shows boxart tiles with the game title underneath, or the filename when a game has not been scraped yet.

The Summary button remains available in Modern Mode, including Simple List and Grid. Opening the summary shows metadata above the summary text and remains scrollable.

Modern Mode layouts are resolution-aware. Detailed List, Simple List, and Grid adjust to the available screen space. Grid calculates how many rows and columns fit and only shows complete artwork tiles so boxart is not cut off.

On ultra-wide displays, Modern Mode keeps its content inside a centered widescreen area. The extra left and right space remains background or wallpaper, instead of stretching the game list and metadata too far apart.

## ScreenScraper

Gentleman includes ScreenScraper integration for Modern Mode.

ScreenScraper can be used to fetch:

- Game metadata
- Game summaries
- Boxart

Scraping can be started for a full library or for a single game from the game list. Large scrape jobs show a progress dialog immediately while Gentleman indexes games for scraping. Single-game scraping can also search for similar game matches before applying metadata and artwork.

ScreenScraper login details are configured from the scrape settings shown in Modern Mode. Dev credentials are intended to be provided by the packaged build.

Scraped metadata and artwork are stored locally. Gentleman uses clean ROM-based filenames for metadata and boxart. If duplicate ROM filenames exist in different folders, a stable duplicate suffix is added so metadata and artwork stay matched to the correct ROM.

## Menu Structure

Gentleman scans the `menu/` folder automatically.

Example:

```text
menu/
├─ Consoles/
│  ├─ Nintendo/
│  │  └─ SNES.json
│  ├─ PS2.json
│  └─ GameCube.json
├─ Handhelds/
│  └─ GBA.json
└─ Apps/
   └─ Steam.json
```

This becomes:

```text
Consoles
Handhelds
Apps
```

Inside `Consoles`:

```text
Nintendo
PS2
GameCube
```

Inside `Nintendo`:

```text
SNES
```

No manual registration is needed. If a JSON launcher exists inside the `menu/` folder, Gentleman can find it.

## Launcher Creation and Editing

Launchers can be created and edited from inside Gentleman.

The Save Folder field opens a folder browser. From there users can:

- Browse into folders
- Create folders with the onscreen keyboard
- Select the current folder as the launcher save location
- Remove folders with confirmation

Edit Launcher can also change the save folder of an existing launcher. When the save folder is changed, the launcher JSON is moved to the selected folder. When the launcher name is changed, the launcher JSON is renamed.

## Launcher Types

Gentleman currently supports three launcher types:

### Standalone Emulator

Used for emulators that launch games directly.

Example:

```json
{
  "type": "standalone",
  "emulator_name": "PCSX2",
  "system": "PS2",
  "emulator": "D:/Emulators/PCSX2/pcsx2-qt.exe",
  "rom_directory": "D:/Games/PS2",
  "extensions": [".iso", ".chd", ".cso"],
  "arguments": "-fullscreen \"{rom}\"",
  "recursive": true
}
```

### RetroArch

Used for RetroArch launchers with a specific core.

Example:

```json
{
  "type": "retroarch",
  "emulator_name": "RetroArch",
  "system": "SNES",
  "emulator": "D:/Emulators/RetroArch/retroarch.exe",
  "core": "D:/Emulators/RetroArch/cores/snes9x_libretro.dll",
  "rom_directory": "D:/Games/SNES",
  "extensions": [".sfc", ".smc", ".zip"],
  "arguments": "-L \"{core}\" \"{rom}\"",
  "recursive": true
}
```

### Application

Used for launching a single executable without a ROM folder.

Example:

```json
{
  "type": "application",
  "emulator_name": "Steam",
  "emulator": "C:/Program Files (x86)/Steam/steam.exe",
  "arguments": "",
  "recursive": true
}
```

## Arguments

Arguments control how Gentleman starts an emulator or application.

Gentleman supports placeholders that are replaced before launching:

- `{rom}` is used by Gentleman to inject the selected ROM/game path into the launch command. For game launchers, this placeholder should always be included so the emulator knows which game to start.
- `{core}` is used by Gentleman to inject the selected RetroArch core path.

Other arguments are passed directly to the emulator. These are emulator-specific, so an argument that works for one emulator may not work for another. Check the emulator's official documentation or wiki for the correct argument list.

Examples:

- `"{rom}"`

  Launches the selected ROM directly.

- `-fullscreen "{rom}"`

  Launches the selected ROM with a fullscreen argument.

- `-L "{core}" "{rom}"`

  Launches RetroArch with the selected core and ROM.

Applications usually do not need `{rom}` because they launch directly without a selected game.

## Favorites and Recent

Gentleman can keep track of recently launched games and favorite games.

Recent games are added automatically when launched.

Favorites can be toggled with:

```text
F
```

or the mapped controller button.

Favorites and Recent can both be disabled from Settings. They can also be cleared from Settings with confirmation.

## Emulators Menu

Gentleman can automatically generate an Emulators entry on the main menu.

This is built from the emulator names used in launcher JSON files.

For example, launchers using:

```json
"emulator_name": "PCSX2"
```

will appear under the Emulators menu as:

```text
PCSX2
```

Selecting an emulator opens the emulator directly without a ROM.

## In-Game OSD

Gentleman includes an optional in-game OSD for running games, emulators, and applications.

The OSD displays the active game and emulator. For application launchers, it displays the application name.

Available actions include:

- Resume
- Close Game, Emulator, or Application
- Force Close Game, Emulator, or Application

Force Close includes a warning because it may interrupt save data or emulator writes.

The OSD can be opened with:

- `Ctrl + Alt + G` on a keyboard
- Hold `L1 + L2 + L3 + R1 + R2 + R3` for one second on a controller

While the OSD is open, the launched process is paused so OSD navigation does not also affect the running game.

The In-Game OSD setting is enabled by default and can be disabled from Settings.

## API

Gentleman includes an API for third-party apps.

The API allows external tools to:

- Retrieve available systems
- Retrieve games by system name
- Browse the Gentleman menu
- Browse games from a launcher
- Launch games
- Launch applications
- Retrieve recent games
- Retrieve favorites
- View the active game, emulator, or application
- Close the active session
- Force close the active session
- Bring Gentleman to the front

Third-party apps do not need to know which emulator is used, which JSON launcher exists, or which ROM folder is configured. They can ask Gentleman for systems and games directly.

See:

[API Documentation](API-Doc.docx)

## Notes

Gentleman is still experimental.

The current focus is to keep the app fast, simple, and transparent while building a MiSTer-inspired frontend experience for PC-based emulation.
