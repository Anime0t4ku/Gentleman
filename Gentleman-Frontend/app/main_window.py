from __future__ import annotations

import json
import platform
import socket
import subprocess
import time
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import Qt, QTimer, QRect, QRectF
from PyQt6.QtGui import QFont, QKeyEvent, QPainter, QColor, QPen, QPixmap
from PyQt6.QtSvg import QSvgRenderer
from PyQt6.QtWidgets import (
    QApplication,
    QFileDialog,
    QMainWindow,
    QMessageBox,
    QWidget,
)

from app.app_info import ABOUT_LINES
from app.create_launcher_dialog import CreateLauncherDialog
from core.launcher import load_launcher, scan_rom_folder, launch_rom, LauncherConfig, RomBrowserItem
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
        self.rom_items: list[RomBrowserItem] = []
        self.system_items = []
        self.update_system_items()
        self.settings_items = []
        self.update_settings_items()
        self.wallpaper_items = [
            "Set Wallpaper",
            "Clear Wallpaper",
        ]

        self.current_launcher: LauncherConfig | None = None
        self.current_rom_folder: Path | None = None
        self.selected_index = 0
        self.active_input = "keyboard"

        self.refresh_menu()

        self.clock_timer = QTimer(self)
        self.clock_timer.timeout.connect(self.view.update)
        self.clock_timer.start(1000)

        self.marquee_timer = QTimer(self)
        self.marquee_timer.timeout.connect(self.view.update)
        self.marquee_timer.start(120)

        if self.settings.get("fullscreen_at_launch", False):
            QTimer.singleShot(0, self.showFullScreen)

    def load_settings(self) -> dict:
        if not self.settings_path.exists():
            return {"wallpaper": "", "fullscreen_at_launch": False}

        try:
            data = json.loads(self.settings_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return {"wallpaper": ""}
            data.setdefault("wallpaper", "")
            data.setdefault("fullscreen_at_launch", False)
            return data
        except Exception:
            return {"wallpaper": "", "fullscreen_at_launch": False}

    def save_settings(self):
        self.settings_path.write_text(json.dumps(self.settings, indent=2), encoding="utf-8")

    def update_system_items(self):
        self.system_items = [
            "Toggle Fullscreen",
            "Create Launcher",
            "Refresh Menu",
            "Settings",
            "About Gentleman",
            "Exit Gentleman",
        ]

    def update_settings_items(self):
        fullscreen_launch_label = (
            "Disable Fullscreen at Launch"
            if self.settings.get("fullscreen_at_launch", False)
            else "Enable Fullscreen at Launch"
        )

        self.settings_items = [
            fullscreen_launch_label,
            "Wallpaper",
        ]

    def title_path(self) -> str:
        if self.mode == "system":
            return "Gentleman Menu"
        if self.mode == "settings":
            return "Settings"
        if self.mode == "wallpaper":
            return "Wallpaper"
        if self.mode == "about":
            return "About"
        if self.mode == "roms" and self.current_launcher:
            if self.current_rom_folder:
                try:
                    rom_root = Path(self.current_launcher.rom_directory)
                    rel = self.current_rom_folder.relative_to(rom_root)
                    if str(rel) != ".":
                        return f"{self.current_launcher.path.stem}/{str(rel).replace(chr(92), '/')}"
                except Exception:
                    pass
            return self.current_launcher.path.stem
        if self.current_folder == self.menu_root:
            return "Menu"
        return str(self.current_folder.relative_to(self.menu_root)).replace("\\", "/")

    def current_items_count(self) -> int:
        if self.mode == "system":
            self.update_system_items()
            return len(self.system_items)
        if self.mode == "settings":
            self.update_settings_items()
            return len(self.settings_items)
        if self.mode == "wallpaper":
            return len(self.wallpaper_items)
        if self.mode == "about":
            return len(ABOUT_LINES)
        if self.mode == "roms":
            return len(self.rom_items) + 1

        back_count = 1 if self.current_folder != self.menu_root else 0
        return len(self.menu_items) + back_count

    def current_labels(self) -> list[tuple[str, str]]:
        if self.mode == "system":
            self.update_system_items()
            return [(name, "") for name in self.system_items]
        if self.mode == "settings":
            self.update_settings_items()
            return [(name, "") for name in self.settings_items]
        if self.mode == "wallpaper":
            return [(name, "") for name in self.wallpaper_items]
        if self.mode == "about":
            return [(line, "") for line in ABOUT_LINES]
        if self.mode == "roms":
            return [("...", "<DIR>")] + [(item.display_name, item.marker) for item in self.rom_items]

        labels = []
        for item in self.menu_items:
            labels.append((item.name, "<DIR>"))

        if self.current_folder != self.menu_root:
            labels.insert(0, ("...", "<DIR>"))
        return labels

    def refresh_menu(self):
        self.mode = "menu"
        self.current_launcher = None
        self.current_rom_folder = None
        self.rom_items = []
        self.menu_items = scan_menu_folder(self.current_folder)
        self.selected_index = min(self.selected_index, max(0, len(self.menu_items) - 1))
        self.view.update()

    def open_system_menu(self):
        self.update_system_items()
        self.mode = "system"
        self.selected_index = 0
        self.view.update()

    def go_back(self):
        if self.mode == "system":
            self.mode = "menu"
            self.selected_index = 0
            self.view.update()
            return

        if self.mode == "settings":
            self.mode = "system"
            self.selected_index = 0
            self.view.update()
            return

        if self.mode == "wallpaper":
            self.mode = "settings"
            self.selected_index = 0
            self.view.update()
            return

        if self.mode == "about":
            self.mode = "system"
            self.selected_index = 0
            self.view.update()
            return

        if self.mode == "roms":
            if self.current_launcher and self.current_rom_folder:
                rom_root = Path(self.current_launcher.rom_directory).resolve()
                current = self.current_rom_folder.resolve()

                if current != rom_root and rom_root in current.parents:
                    self.current_rom_folder = self.current_rom_folder.parent
                    self.rom_items = scan_rom_folder(self.current_launcher, self.current_rom_folder)
                    self.selected_index = 0
                    self.view.scroll_offset = 0
                    self.view.update()
                    return

            self.mode = "menu"
            self.current_launcher = None
            self.current_rom_folder = None
            self.rom_items = []
            self.selected_index = 0
            self.view.update()
            return

        if self.current_folder == self.menu_root:
            self.open_system_menu()
            return

        self.current_folder = self.current_folder.parent
        self.selected_index = 0
        self.view.scroll_offset = 0
        self.refresh_menu()

    def activate_selected(self):
        if self.current_items_count() == 0:
            return

        if self.mode == "system":
            self.activate_system_item(self.system_items[self.selected_index])
            return

        if self.mode == "settings":
            self.activate_settings_item(self.settings_items[self.selected_index])
            return

        if self.mode == "wallpaper":
            self.activate_wallpaper_item(self.wallpaper_items[self.selected_index])
            return

        if self.mode == "roms":
            if self.selected_index == 0:
                self.go_back()
                return

            if not self.current_launcher:
                return

            selected = self.rom_items[self.selected_index - 1]

            if selected.is_dir:
                self.current_rom_folder = selected.path
                self.rom_items = scan_rom_folder(self.current_launcher, self.current_rom_folder)
                self.selected_index = 0
                self.view.scroll_offset = 0
                self.view.update()
                return

            try:
                launch_rom(self.current_launcher, selected.path)
            except Exception as exc:
                QMessageBox.critical(self, "Launch failed", str(exc))
            return

        menu_index = self.selected_index
        if self.current_folder != self.menu_root:
            if self.selected_index == 0:
                self.go_back()
                return
            menu_index -= 1

        item = self.menu_items[menu_index]
        if item.item_type == "folder":
            self.current_folder = item.path
            self.refresh_menu()
            return

        try:
            self.current_launcher = load_launcher(item.path)
            self.current_rom_folder = Path(self.current_launcher.rom_directory)
            self.rom_items = scan_rom_folder(self.current_launcher, self.current_rom_folder)
            self.mode = "roms"
            self.selected_index = 0
            self.view.scroll_offset = 0
            self.view.update()
        except Exception as exc:
            QMessageBox.critical(self, "Launcher error", str(exc))

    def activate_system_item(self, item: str):
        if item == "Toggle Fullscreen":
            self.toggle_fullscreen()
            self.update_system_items()
            self.view.update()
        elif item == "Create Launcher":
            dialog = CreateLauncherDialog(self.base_dir, self.menu_root, self)
            if dialog.exec():
                self.current_folder = self.menu_root
                self.refresh_menu()
        elif item == "Refresh Menu":
            self.mode = "menu"
            self.refresh_menu()
        elif item == "Settings":
            self.update_settings_items()
            self.mode = "settings"
            self.selected_index = 0
            self.view.update()
        elif item == "About Gentleman":
            self.mode = "about"
            self.selected_index = 0
            self.view.update()
        elif item == "Exit Gentleman":
            QApplication.quit()

    def activate_settings_item(self, item: str):
        if item == "Enable Fullscreen at Launch":
            self.settings["fullscreen_at_launch"] = True
            self.save_settings()
            self.update_settings_items()
            self.view.update()
        elif item == "Disable Fullscreen at Launch":
            self.settings["fullscreen_at_launch"] = False
            self.save_settings()
            self.update_settings_items()
            self.view.update()
        elif item == "Wallpaper":
            self.mode = "wallpaper"
            self.selected_index = 0
            self.view.update()

    def activate_wallpaper_item(self, item: str):
        if item == "Set Wallpaper":
            self.set_wallpaper()
        elif item == "Clear Wallpaper":
            self.settings["wallpaper"] = ""
            self.save_settings()
            self.view.reload_wallpaper()
            self.view.update()

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

        if self.mode == "about":
            max_offset = max(0, count - self.view.visible_rows())
            self.view.scroll_offset = max(0, min(max_offset, self.view.scroll_offset + delta))
            self.selected_index = self.view.scroll_offset
            self.view.update()
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

        self.icon_dir = self.window.base_dir / "assets" / "icons"
        self.icon_renderers = {
            "lan": QSvgRenderer(str(self.icon_dir / "lan.svg")),
            "wifi": QSvgRenderer(str(self.icon_dir / "wifi.svg")),
            "bluetooth": QSvgRenderer(str(self.icon_dir / "bluetooth.svg")),
            "keyboard": QSvgRenderer(str(self.icon_dir / "keyboard.svg")),
            "controller": QSvgRenderer(str(self.icon_dir / "controller.svg")),
        }

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

    def menu_panel_size(self) -> tuple[int, int]:
        panel_w = min(620, self.width() - 160)
        panel_h = min(430, self.height() - 200)
        return panel_w, panel_h

    def menu_panel_rect(self) -> QRect:
        panel_w, panel_h = self.menu_panel_size()
        x = (self.width() - panel_w) // 2
        y = (self.height() - panel_h) // 2
        return QRect(x, y, panel_w, panel_h)

    def top_bar_rect(self) -> QRect:
        panel_rect = self.menu_panel_rect()
        bar_h = 44
        gap = 34
        y = max(16, panel_rect.y() - bar_h - gap)
        return QRect(panel_rect.x(), y, panel_rect.width(), bar_h)

    def visible_rows(self) -> int:
        _, panel_h = self.menu_panel_size()
        return max(1, (panel_h - 80) // 30)

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
            return "lan"
        except Exception:
            return "wifi"

    def bluetooth_icon(self) -> str:
        return "bluetooth"

    def input_icon(self) -> str:
        return "keyboard" if self.window.active_input == "keyboard" else "controller"

    def draw_svg_icon(self, painter: QPainter, icon_name: str, rect: QRect, color: QColor):
        renderer = self.icon_renderers.get(icon_name)
        if not renderer or not renderer.isValid():
            return

        painter.save()
        painter.setPen(color)
        painter.setBrush(color)
        renderer.render(painter, QRectF(rect))
        painter.restore()

    def draw_top_bar(self, painter: QPainter):
        bar_rect = self.top_bar_rect()
        bar_w = bar_rect.width()
        bar_h = bar_rect.height()
        x = bar_rect.x()
        y = bar_rect.y()

        painter.fillRect(bar_rect, self.light)

        painter.setFont(self.title_font)
        painter.setPen(self.dark_text)

        painter.drawText(x + 18, y + 31, "Gentleman")

        icon_size = 24
        icon_y = y + (bar_h - icon_size) // 2
        icon_x = x + 172
        icon_gap = 34

        self.draw_svg_icon(painter, self.network_icon(), QRect(icon_x, icon_y, icon_size, icon_size), self.dark_text)
        icon_x += icon_gap
        self.draw_svg_icon(painter, self.bluetooth_icon(), QRect(icon_x, icon_y, icon_size, icon_size), self.dark_text)
        icon_x += icon_gap
        self.draw_svg_icon(painter, self.input_icon(), QRect(icon_x, icon_y, icon_size, icon_size), self.dark_text)

        time_text = datetime.now().strftime("%H:%M")
        painter.drawText(x + bar_w - 82, y + 31, time_text)

    def draw_panel(self, painter: QPainter):
        panel_rect = self.menu_panel_rect()
        panel_w = panel_rect.width()
        panel_h = panel_rect.height()
        x = panel_rect.x()
        y = panel_rect.y()

        side_w = 48
        title_h = 38

        painter.fillRect(QRect(x, y, panel_w, panel_h), self.panel)
        painter.fillRect(QRect(x, y, side_w, panel_h), self.light)
        painter.fillRect(QRect(x + side_w, y, panel_w - side_w, title_h), self.light)

        painter.setFont(self.font)
        painter.setPen(self.dark_text)
        painter.save()
        painter.setClipRect(QRect(x, y, side_w, panel_h))

        title = self.window.title_path()
        max_title_width = panel_h - 56
        metrics = painter.fontMetrics()

        if metrics.horizontalAdvance(title) > max_title_width:
            ellipsis = "..."
            while title and metrics.horizontalAdvance(ellipsis + title) > max_title_width:
                title = title[1:]
            title = ellipsis + title

        title_width = metrics.horizontalAdvance(title)
        title_start = (panel_h - title_width) // 2

        painter.translate(x + 31, y + panel_h - title_start)
        painter.rotate(-90)
        painter.drawText(0, 0, title)
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

            if self.window.mode != "about" and idx == self.window.selected_index:
                painter.fillRect(QRect(text_x - 6, yy - 24, panel_w - side_w - 28, 29), self.light)
                painter.setPen(self.dark_text)
            else:
                painter.setPen(self.text)

            if self.window.mode == "about":
                painter.drawText(text_x, yy, label)
            else:
                text_area_w = marker_x - text_x - 18
                label_width = painter.fontMetrics().horizontalAdvance(label)

                if idx == self.window.selected_index and label_width > text_area_w:
                    clip_rect = QRect(text_x, yy - 24, text_area_w, 29)
                    painter.save()
                    painter.setClipRect(clip_rect)

                    gap = 48
                    cycle = label_width + gap
                    offset = int((time.monotonic() * 55) % cycle)

                    painter.drawText(text_x - offset, yy, label)
                    painter.drawText(text_x - offset + cycle, yy, label)
                    painter.restore()
                else:
                    clip_rect = QRect(text_x, yy - 24, text_area_w, 29)
                    painter.save()
                    painter.setClipRect(clip_rect)
                    painter.drawText(text_x, yy, label)
                    painter.restore()

                if marker:
                    painter.drawText(marker_x, yy, marker)

        if start > 0:
            painter.setPen(self.text)
            painter.drawText(x + panel_w - 38, y + title_h + 20, "^")
        if end < len(labels):
            painter.setPen(self.text)
            painter.drawText(x + panel_w - 38, y + panel_h - 18, "v")
