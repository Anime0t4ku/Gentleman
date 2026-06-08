import sys
from pathlib import Path
from PyQt6.QtWidgets import QApplication
from app.main_window import GentlemanWindow


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Gentleman")
    base_dir = Path(__file__).resolve().parent
    window = GentlemanWindow(base_dir)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
