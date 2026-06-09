import sys
from pathlib import Path
from PyQt6.QtWidgets import QApplication
from app.main_window import GentlemanWindow


def app_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent

    return Path(__file__).resolve().parent


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Gentleman")
    base_dir = app_base_dir()
    window = GentlemanWindow(base_dir)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
