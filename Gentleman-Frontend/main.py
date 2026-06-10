import json
import sys
from pathlib import Path
from PyQt6.QtWidgets import QApplication
from app.app_info import APP_NAME, APP_VERSION
from app.main_window import GentlemanWindow


def app_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent

    return Path(__file__).resolve().parent


def write_updater_config(base_dir: Path):
    config_path = base_dir / "config.json"
    data = {}

    if config_path.exists():
        try:
            loaded = json.loads(config_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except Exception:
            data = {}

    data["app_name"] = APP_NAME
    data["app_version"] = APP_VERSION

    try:
        config_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        pass


def main():
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    base_dir = app_base_dir()
    write_updater_config(base_dir)
    window = GentlemanWindow(base_dir)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
