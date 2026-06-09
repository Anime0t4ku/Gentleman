from __future__ import annotations
import json
from pathlib import Path
from app.zaparoo_systems import ZAPAROO_SYSTEM_NAMES
from PyQt6.QtWidgets import QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QMessageBox, QPushButton, QVBoxLayout, QWidget

class CreateLauncherDialog(QDialog):
    def __init__(self, base_dir: Path, menu_root: Path, parent=None, launcher_path: Path | None = None):
        super().__init__(parent)
        self.base_dir = base_dir
        self.menu_root = menu_root
        self.launcher_path = launcher_path
        self.created_path: Path | None = None
        self.setWindowTitle('Edit Launcher' if self.launcher_path else 'Create Launcher')
        self.setMinimumWidth(620)
        self.name_edit = QLineEdit()
        self.emulator_name_edit = QLineEdit()
        self.system_combo = QComboBox()
        self.folder_combo = QComboBox()
        self.new_folder_label = QLabel('New folder name:')
        self.new_folder_edit = QLineEdit()
        self.type_combo = QComboBox()
        self.emulator_edit = QLineEdit()
        self.core_edit = QLineEdit()
        self.rom_dir_edit = QLineEdit()
        self.extensions_edit = QLineEdit()
        self.arguments_edit = QLineEdit()
        self.recursive_check = QCheckBox('Scan subfolders')
        self.recursive_check.setChecked(True)
        self.type_combo.addItems(['Standalone Emulator', 'RetroArch', 'Custom Command'])
        self.system_combo.addItem('Custom / Unknown', '')
        for system_name in ZAPAROO_SYSTEM_NAMES:
            self.system_combo.addItem(system_name, system_name)
        self.type_combo.currentTextChanged.connect(self._on_type_changed)
        self.folder_combo.currentIndexChanged.connect(self._on_folder_changed)
        self._load_folders()
        form = QFormLayout()
        form.addRow('Launcher name:', self.name_edit)
        form.addRow('Emulator name:', self.emulator_name_edit)
        form.addRow('System:', self.system_combo)
        form.addRow('Save in menu folder:', self.folder_combo)
        form.addRow(self.new_folder_label, self.new_folder_edit)
        form.addRow('Type:', self.type_combo)
        emulator_row = QHBoxLayout(); emulator_row.addWidget(self.emulator_edit)
        emulator_browse = QPushButton('Browse'); emulator_browse.clicked.connect(self._browse_emulator); emulator_row.addWidget(emulator_browse)
        form.addRow('Emulator:', emulator_row)
        self.core_label = QLabel('RetroArch core:')
        self.core_widget = QWidget()
        core_row = QHBoxLayout(self.core_widget)
        core_row.setContentsMargins(0, 0, 0, 0)
        core_row.addWidget(self.core_edit)
        self.core_browse = QPushButton('Browse')
        self.core_browse.clicked.connect(self._browse_core)
        core_row.addWidget(self.core_browse)
        form.addRow(self.core_label, self.core_widget)
        rom_row = QHBoxLayout(); rom_row.addWidget(self.rom_dir_edit)
        rom_browse = QPushButton('Browse'); rom_browse.clicked.connect(self._browse_rom_dir); rom_row.addWidget(rom_browse)
        form.addRow('ROM path:', rom_row)
        form.addRow('Extensions:', self.extensions_edit)
        form.addRow('Arguments:', self.arguments_edit)
        form.addRow('', self.recursive_check)
        buttons = QHBoxLayout()
        save_button = QPushButton('Save' if self.launcher_path else 'Create'); cancel_button = QPushButton('Cancel')
        save_button.clicked.connect(self._save); cancel_button.clicked.connect(self.reject)
        buttons.addStretch(); buttons.addWidget(save_button); buttons.addWidget(cancel_button)
        layout = QVBoxLayout(self); layout.addLayout(form); layout.addLayout(buttons)
        self._on_type_changed(self.type_combo.currentText())
        if self.launcher_path:
            self._load_existing_launcher()

    def _load_existing_launcher(self):
        try:
            data = json.loads(self.launcher_path.read_text(encoding="utf-8"))
        except Exception as exc:
            QMessageBox.warning(self, "Load failed", str(exc))
            return

        self.name_edit.setText(self.launcher_path.stem)
        self.name_edit.setEnabled(False)
        self.folder_combo.setEnabled(False)
        self.new_folder_label.setVisible(False)
        self.new_folder_edit.setVisible(False)

        self.emulator_name_edit.setText(str(data.get("emulator_name", "")))

        system_name = str(data.get("system", ""))
        index = self.system_combo.findData(system_name)
        if index >= 0:
            self.system_combo.setCurrentIndex(index)

        launcher_type = str(data.get("type", "standalone"))
        if launcher_type == "retroarch":
            self.type_combo.setCurrentText("RetroArch")
        elif launcher_type == "custom":
            self.type_combo.setCurrentText("Custom Command")
        else:
            self.type_combo.setCurrentText("Standalone Emulator")

        self.emulator_edit.setText(str(data.get("emulator", "")))
        self.core_edit.setText(str(data.get("core", "")))
        self.rom_dir_edit.setText(str(data.get("rom_directory", "")))
        self.extensions_edit.setText(",".join(data.get("extensions", [])))
        self.arguments_edit.setText(str(data.get("arguments", '"{rom}"')))
        self.recursive_check.setChecked(bool(data.get("recursive", True)))
        self._on_type_changed(self.type_combo.currentText())

    def _load_folders(self):
        self.folder_combo.clear(); self.folder_combo.addItem('Root Menu', '')
        folders = [p for p in self.menu_root.rglob('*') if p.is_dir()]
        folders.sort(key=lambda p: str(p.relative_to(self.menu_root)).lower())
        for folder in folders:
            rel = folder.relative_to(self.menu_root)
            self.folder_combo.addItem(str(rel).replace('\\', '/'), str(rel))
        self.folder_combo.addItem('Create New Folder...', '__create_new__')
        self._on_folder_changed()

    def _on_folder_changed(self):
        is_new_folder = self.folder_combo.currentData() == '__create_new__'
        self.new_folder_label.setVisible(is_new_folder)
        self.new_folder_edit.setVisible(is_new_folder)

    def _on_type_changed(self, text: str):
        is_retroarch = text == 'RetroArch'
        self.core_label.setVisible(is_retroarch)
        self.core_widget.setVisible(is_retroarch)
        self.core_edit.setEnabled(is_retroarch)
        self.core_browse.setEnabled(is_retroarch)
        if is_retroarch:
            self.arguments_edit.setText('-L "{core}" "{rom}"')
            self.extensions_edit.setPlaceholderText('.sfc,.smc,.zip')
        elif text == 'Standalone Emulator':
            self.arguments_edit.setText('"{rom}"')
            self.extensions_edit.setPlaceholderText('.iso,.chd,.zip')
        else:
            self.arguments_edit.setText('"{rom}"')
            self.extensions_edit.setPlaceholderText('.zip,.exe,.bat')

    def _browse_emulator(self):
        path, _ = QFileDialog.getOpenFileName(self, 'Select emulator executable', str(self.base_dir), 'Executables (*.exe);;All files (*.*)')
        if path: self.emulator_edit.setText(path.replace('\\', '/'))

    def _browse_core(self):
        path, _ = QFileDialog.getOpenFileName(self, 'Select RetroArch core', str(self.base_dir), 'RetroArch cores (*.dll);;All files (*.*)')
        if path: self.core_edit.setText(path.replace('\\', '/'))

    def _browse_rom_dir(self):
        path = QFileDialog.getExistingDirectory(self, 'Select ROM folder', str(self.base_dir))
        if path: self.rom_dir_edit.setText(path.replace('\\', '/'))

    def _safe_filename(self, name: str) -> str:
        blocked = '<>:"/\\|?*'
        cleaned = ''.join('_' if ch in blocked else ch for ch in name).strip()
        return cleaned or 'Launcher'

    def _save(self):
        name = self.name_edit.text().strip()
        emulator_name = self.emulator_name_edit.text().strip()
        system_name = str(self.system_combo.currentData() or '').strip()
        emulator = self.emulator_edit.text().strip()
        rom_dir = self.rom_dir_edit.text().strip()
        launcher_type_text = self.type_combo.currentText()
        if not name:
            QMessageBox.warning(self, 'Missing name', 'Enter a launcher name.'); return
        if not emulator_name:
            QMessageBox.warning(self, 'Missing emulator name', 'Enter an emulator name.'); return
        if not emulator:
            QMessageBox.warning(self, 'Missing emulator', 'Select an emulator executable.'); return
        if not rom_dir:
            QMessageBox.warning(self, 'Missing ROM path', 'Select a ROM path.'); return
        launcher_type = 'standalone'
        if launcher_type_text == 'RetroArch':
            launcher_type = 'retroarch'
            if not self.core_edit.text().strip():
                QMessageBox.warning(self, 'Missing core', 'Select a RetroArch core.'); return
        elif launcher_type_text == 'Custom Command':
            launcher_type = 'custom'
        if self.launcher_path:
            json_path = self.launcher_path
        else:
            folder_data = self.folder_combo.currentData()
            if folder_data == '__create_new__':
                folder_name = self.new_folder_edit.text().strip()
                if not folder_name:
                    QMessageBox.warning(self, 'Missing folder name', 'Enter a new menu folder name.')
                    return
                target_folder = self.menu_root / self._safe_filename(folder_name)
            elif folder_data:
                target_folder = self.menu_root / folder_data
            else:
                target_folder = self.menu_root
            target_folder.mkdir(parents=True, exist_ok=True)
            json_path = target_folder / f'{self._safe_filename(name)}.json'
            if json_path.exists():
                QMessageBox.warning(self, 'Already exists', f'{json_path.name} already exists.'); return
        extensions = []
        for ext in self.extensions_edit.text().split(','):
            ext = ext.strip().lower()
            if not ext: continue
            if not ext.startswith('.'): ext = '.' + ext
            extensions.append(ext)
        data = {
            'type': launcher_type,
            'emulator_name': emulator_name,
            'system': system_name,
            'emulator': emulator,
            'rom_directory': rom_dir,
            'extensions': extensions,
            'arguments': self.arguments_edit.text().strip() or '"{rom}"',
            'recursive': self.recursive_check.isChecked(),
        }
        if launcher_type == 'retroarch': data['core'] = self.core_edit.text().strip()
        json_path.write_text(json.dumps(data, indent=2), encoding='utf-8')
        self.created_path = json_path
        self.accept()
