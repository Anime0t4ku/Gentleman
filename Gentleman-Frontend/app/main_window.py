from __future__ import annotations

import json
import platform
import socket
import subprocess
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import Qt, QTimer, QRect
from PyQt6.QtGui import QFont, QKeyEvent, QPainter, QColor, QPen, QPixmap
from PyQt6.QtWidgets import (
    QApplication,
    QFileDialog,
    QMainWindow,
    QMessageBox,
    QWidget,
)

from app.create_launcher_dialog import CreateLauncherDialog
from core.launcher import load_launcher, scan_roms, launch_rom, LauncherConfig
from core.menu_scanner import MenuItem, scan_menu_folder


class GentlemanWindow(QMainWindow):
    def __init__(self, base_dir: Path):
        super().__init__()

        self.base_dir = base_dir
        self.menu_root = base_dir / "menu"
        self.config_dir = base_dir / "config"
        self.settings_path = self.config_dir / "settings.json"
        self.menu_root.mkdir(exist_ok=True)
        self.config_dir.mkdir(exist_ok=True)

        self.settings = self.load_settings()

        self.setWindowTitle("Gentleman")
        self.resize(1280, 720)

        self.view = GentlemanView(self)
        self.setCentralWidget(self.view)

        self.path_stack: list[Path] = []
        self.current_folder = self.menu_root
        self.mode = "menu"

        self.menu_items: list[MenuItem] = []
        self.rom_items: list[Path] = []
        self.system_items = [
            "Create Launcher",
            "Set Wallpaper",
            "Clear Wallpaper",
            "Refresh Menu",
            "Open Menu Folder",
            "Settings",
            "About Gentleman",
            "Exit Gentleman",
        ]

        self.current_launcher: LauncherConfig | None = None
        self.selected_index = 0
        self.active_input = "keyboard"

        self.refresh_menu()

        self.clock_timer = QTimer(self)
        self.clock_timer.timeout.connect(self.view.update)
        self.clock_timer.start(1000)

    def load_settings(self) -> dict:
        if not self.settings_path.exists():
            return {"wallpaper": ""}

        try:
            data = json.loads(self.settings_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return {"wallpaper": ""}
            data.setdefault("wallpaper", "")
            return data
        except Exception:
            return {"wallpaper": ""}

    def save_settings(self):
        self.settings_path.write_text(json.dumps(self.settings, indent=2), encoding="utf-8")

    def title_path(self) -> str:
        if self.mode == "system":
            return "Gentleman Menu"
        if self.mode == "roms" and self.current_launcher:
            return self.current_launcher.path.stem
        if self.current_folder == self.menu_root:
            return "Menu"
        return str(self.current_folder.relative_to(self.menu_root)).replace("\\", "/")

    def current_items_count(self) -> int:
        if self.mode == "system":
            return len(self.system_items)
        if self.mode == "roms":
            return len(self.rom_items)
        return len(self.menu_items)

    def current_labels(self) -> list[tuple[str, str]]:
        if self.mode == "system":
            return [(name, "") for name in self.system_items]
        if self.mode == "roms":
            return [(path.stem, "") for path in self.rom_items]
        return [(item.name, item.marker) for item in self.menu_items]

    def refresh_menu(self):
        self.mode = "menu"
        self.current_launcher = None
        self.rom_items = []
        self.menu_items = scan_menu_folder(self.current_folder)
        self.selected_index = min(self.selected_index, max(0, len(self.menu_items) - 1))
        self.view.update()

    def open_system_menu(self):
        self.mode = "system"
        self.selected_index = 0
        self.view.update()

    def go_back(self):
        if self.mode == "system":
            self.mode = "menu"
            self.selected_index = 0
            self.view.update()
            return

        if self.mode == "roms":
            self.mode = "menu"
            self.current_launcher = None
            self.rom_items = []
            self.selected_index = 0
            self.view.update()
            return

        if self.current_folder == self.menu_root:
            self.open_system_menu()
            return

        self.current_folder = self.current_folder.parent
        self.refresh_menu()

    def activate_selected(self):
        if self.current_items_count() == 0:
            return

        if self.mode == "system":
            self.activate_system_item(self.system_items[self.selected_index])
            return

        if self.mode == "roms":
            if not self.current_launcher:
                return
            rom = self.rom_items[self.selected_index]
            try:
                launch_rom(self.current_launcher, rom)
            except Exception as exc:
                QMessageBox.critical(self, "Launch failed", str(exc))
            return

        item = self.menu_items[self.selected_index]
        if item.item_type == "folder":
            self.current_folder = item.path
            self.refresh_menu()
            return

        try:
            self.current_launcher = load_launcher(item.path)
            self.rom_items = scan_roms(self.current_launcher)
            self.mode = "roms"
            self.selected_index = 0
            self.view.update()
        except Exception as exc:
            QMessageBox.critical(self, "Launcher error", str(exc))

    def activate_system_item(self, item: str):
        if item == "Create Launcher":
            dialog = CreateLauncherDialog(self.base_dir, self.menu_root, self)
            if dialog.exec():
                self.current_folder = self.menu_root
                self.refresh_menu()
        elif item == "Set Wallpaper":
            self.set_wallpaper()
        elif item == "Clear Wallpaper":
            self.settings["wallpaper"] = ""
            self.save_settings()
            self.view.reload_wallpaper()
            self.view.update()
        elif item == "Refresh Menu":
            self.mode = "menu"
            self.refresh_menu()
        elif item == "Open Menu Folder":
            self.open_folder(self.menu_root)
        elif item == "Settings":
            QMessageBox.information(self, "Settings", "Settings screen will be added later.")
        elif item == "About Gentleman":
            QMessageBox.information(self, "About Gentleman", "Gentleman\nA MiSTer-inspired PC frontend prototype.")
        elif item == "Exit Gentleman":
            QApplication.quit()

    def set_wallpaper(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Select wallpaper",
            str(self.base_dir),
            "Images (*.png *.jpg *.jpeg *.bmp *.webp);;All files (*.*)",
        )
        if not path:
            return

        self.settings["wallpaper"] = path.replace("\\", "/")
        self.save_settings()
        self.view.reload_wallpaper()
        self.view.update()

    def open_folder(self, folder: Path):
        try:
            if platform.system() == "Windows":
                subprocess.Popen(["explorer", str(folder)])
            elif platform.system() == "Darwin":
                subprocess.Popen(["open", str(folder)])
            else:
                subprocess.Popen(["xdg-open", str(folder)])
        except Exception as exc:
            QMessageBox.warning(self, "Open folder failed", str(exc))

    def move_selection(self, delta: int):
        count = self.current_items_count()
        if count <= 0:
            return

        self.selected_index = max(0, min(count - 1, self.selected_index + delta))
        self.view.ensure_visible()
        self.view.update()

    def toggle_fullscreen(self):
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()
        QTimer.singleShot(50, self.view.update)

    def keyPressEvent(self, event: QKeyEvent):
        self.active_input = "keyboard"

        key = event.key()

        if key in (Qt.Key.Key_Up, Qt.Key.Key_W):
            self.move_selection(-1)
        elif key in (Qt.Key.Key_Down, Qt.Key.Key_S):
            self.move_selection(1)
        elif key in (Qt.Key.Key_Left, Qt.Key.Key_A):
            self.move_selection(-10)
        elif key in (Qt.Key.Key_Right, Qt.Key.Key_D):
            self.move_selection(10)
        elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.activate_selected()
        elif key in (Qt.Key.Key_Escape, Qt.Key.Key_Backspace):
            self.go_back()
        elif key == Qt.Key.Key_F5:
            self.refresh_menu()
        elif key == Qt.Key.Key_F11:
            self.toggle_fullscreen()

        self.view.update()


class GentlemanView(QWidget):
    def __init__(self, window: GentlemanWindow):
        super().__init__()
        self.window = window
        self.scroll_offset = 0
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        self.bg = QColor(0, 0, 0)
        self.panel = QColor(55, 0, 15, 220)
        self.light = QColor(220, 185, 190)
        self.text = QColor(245, 235, 235)
        self.dark_text = QColor(40, 0, 10)

        self.font = QFont("Consolas", 20)
        self.font.setStyleHint(QFont.StyleHint.Monospace)

        self.title_font = QFont("Consolas", 22, QFont.Weight.Bold)
        self.title_font.setStyleHint(QFont.StyleHint.Monospace)

        self.wallpaper = QPixmap()
        self.reload_wallpaper()

    def reload_wallpaper(self):
        wallpaper_path = self.window.settings.get("wallpaper", "")
        if wallpaper_path and Path(wallpaper_path).exists():
            self.wallpaper = QPixmap(wallpaper_path)
        else:
            self.wallpaper = QPixmap()

    def ensure_visible(self):
        visible_rows = self.visible_rows()
        idx = self.window.selected_index

        if idx < self.scroll_offset:
            self.scroll_offset = idx
        elif idx >= self.scroll_offset + visible_rows:
            self.scroll_offset = idx - visible_rows + 1

        self.scroll_offset = max(0, self.scroll_offset)

    def visible_rows(self) -> int:
        return max(1, (self.height() - 270) // 30)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)

        if not self.wallpaper.isNull():
            scaled = self.wallpaper.scaled(
                self.size(),
                Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation,
            )
            x = (self.width() - scaled.width()) // 2
            y = (self.height() - scaled.height()) // 2
            painter.drawPixmap(x, y, scaled)
            painter.fillRect(self.rect(), QColor(0, 0, 0, 95))
        else:
            painter.fillRect(self.rect(), self.bg)
            self.draw_starfield(painter)

        self.draw_top_bar(painter)
        self.draw_panel(painter)

    def draw_starfield(self, painter: QPainter):
        painter.setPen(QPen(QColor(255, 255, 255, 180), 1))
        w = self.width()
        h = self.height()
        for i in range(180):
            x = (i * 97) % max(1, w)
            y = (i * 53) % max(1, h)
            painter.drawPoint(x, y)

    def network_icon(self) -> str:
        try:
            socket.create_connection(("8.8.8.8", 53), timeout=0.2).close()
            return "LAN"
        except Exception:
            return "NET"

    def bluetooth_icon(self) -> str:
        return "BT"

    def input_icon(self) -> str:
        return "KBD" if self.window.active_input == "keyboard" else "PAD"

    def draw_top_bar(self, painter: QPainter):
        bar_w = min(620, self.width() - 160)
        bar_h = 44
        x = (self.width() - bar_w) // 2
        y = 72

        painter.fillRect(QRect(x, y, bar_w, bar_h), self.light)

        painter.setFont(self.title_font)
        painter.setPen(self.dark_text)

        left = f"Gentleman   {self.network_icon()}  {self.bluetooth_icon()}  {self.input_icon()}"
        time_text = datetime.now().strftime("%H:%M")

        painter.drawText(x + 18, y + 31, left)
        painter.drawText(x + bar_w - 82, y + 31, time_text)

    def draw_panel(self, painter: QPainter):
        panel_w = min(620, self.width() - 160)
        panel_h = min(430, self.height() - 200)
        x = (self.width() - panel_w) // 2
        y = 150

        side_w = 48
        title_h = 38

        painter.fillRect(QRect(x, y, panel_w, panel_h), self.panel)
        painter.fillRect(QRect(x, y, side_w, panel_h), self.light)
        painter.fillRect(QRect(x + side_w, y, panel_w - side_w, title_h), self.light)

        painter.setFont(self.font)
        painter.setPen(self.dark_text)
        painter.save()
        painter.translate(x + 31, y + panel_h // 2 + 60)
        painter.rotate(-90)
        painter.drawText(0, 0, self.window.title_path())
        painter.restore()

        labels = self.window.current_labels()
        rows = self.visible_rows()
        self.ensure_visible()

        start = self.scroll_offset
        end = min(len(labels), start + rows)

        text_x = x + side_w + 22
        marker_x = x + panel_w - 112
        row_y = y + title_h + 28

        painter.setFont(self.font)

        if not labels:
            painter.setPen(self.text)
            painter.drawText(text_x, row_y, "No entries")
            return

        for row, idx in enumerate(range(start, end)):
            label, marker = labels[idx]
            yy = row_y + row * 30

            if idx == self.window.selected_index:
                painter.fillRect(QRect(text_x - 6, yy - 24, panel_w - side_w - 28, 29), self.light)
                painter.setPen(self.dark_text)
            else:
                painter.setPen(self.text)

            painter.drawText(text_x, yy, label[:28])
            if marker:
                painter.drawText(marker_x, yy, marker)

        if start > 0:
            painter.setPen(self.text)
            painter.drawText(x + panel_w - 38, y + title_h + 20, "^")
        if end < len(labels):
            painter.setPen(self.text)
            painter.drawText(x + panel_w - 38, y + panel_h - 18, "v")
