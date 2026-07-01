import sys
from pathlib import Path
from PyQt6.QtWidgets import QApplication
from app.app_info import APP_NAME
from app.main_window import GentlemanWindow


def macos_application_support_dir() -> Path:
    support_dir = Path.home() / "Library" / "Application Support" / APP_NAME
    support_dir.mkdir(parents=True, exist_ok=True)
    return support_dir


def app_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        if sys.platform == "darwin":
            return macos_application_support_dir()
        return Path(sys.executable).resolve().parent

    return Path(__file__).resolve().parent


def main():
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    base_dir = app_base_dir()
    window = GentlemanWindow(base_dir)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
