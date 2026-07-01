import sys
import traceback
from datetime import datetime
from pathlib import Path

from PyQt6.QtWidgets import QApplication
from app.app_info import APP_NAME


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


def crash_log_path() -> Path:
    if sys.platform == "darwin" and getattr(sys, "frozen", False):
        log_dir = macos_application_support_dir() / "logs"
    else:
        log_dir = app_base_dir() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / "Gentleman-crash.log"


def write_crash_log(exc_type, exc_value, exc_traceback):
    try:
        with crash_log_path().open("a", encoding="utf-8") as handle:
            handle.write(f"\n[{datetime.now().isoformat(timespec='seconds')}] Gentleman failed to start\n")
            traceback.print_exception(exc_type, exc_value, exc_traceback, file=handle)
    except Exception:
        pass


def install_crash_logging():
    original_hook = sys.excepthook

    def handle_exception(exc_type, exc_value, exc_traceback):
        write_crash_log(exc_type, exc_value, exc_traceback)
        original_hook(exc_type, exc_value, exc_traceback)

    sys.excepthook = handle_exception


def main():
    install_crash_logging()
    try:
        from app.main_window import GentlemanWindow

        app = QApplication(sys.argv)
        app.setApplicationName(APP_NAME)
        base_dir = app_base_dir()
        window = GentlemanWindow(base_dir)
        window.show()
        sys.exit(app.exec())
    except Exception:
        exc_type, exc_value, exc_traceback = sys.exc_info()
        write_crash_log(exc_type, exc_value, exc_traceback)
        raise


if __name__ == "__main__":
    main()
