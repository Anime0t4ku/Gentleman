from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse


class GentlemanApiServer:
    def __init__(self, app, host: str = "127.0.0.1", port: int = 8755):
        self.app = app
        self.host = host
        self.port = port
        self.httpd: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None

    def start(self):
        if self.httpd is not None:
            return

        app = self.app

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                return

            def _send_json(self, payload, status: int = 200):
                data = json.dumps(payload, indent=2).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
                self.end_headers()
                self.wfile.write(data)

            def _read_json(self):
                length = int(self.headers.get("Content-Length", "0") or "0")
                if length <= 0:
                    return {}
                data = self.rfile.read(length).decode("utf-8")
                return json.loads(data) if data.strip() else {}

            def do_OPTIONS(self):
                self._send_json({"ok": True})

            def do_GET(self):
                parsed = urlparse(self.path)
                query = parse_qs(parsed.query)

                try:
                    if parsed.path == "/api/status":
                        self._send_json(app.api_status())
                        return

                    if parsed.path in ("/api/session", "/api/v1/session"):
                        self._send_json(app.active_session_snapshot())
                        return

                    if parsed.path == "/api/menu":
                        folder = query.get("path", [""])[0]
                        self._send_json(app.api_menu(folder))
                        return

                    if parsed.path == "/api/games":
                        launcher = query.get("launcher", [""])[0]
                        folder = query.get("folder", [""])[0]
                        self._send_json(app.api_games(launcher, folder))
                        return

                    if parsed.path == "/api/systems":
                        self._send_json(app.api_systems())
                        return

                    if parsed.path == "/api/games-by-system":
                        system = query.get("system", [""])[0]
                        launcher = query.get("launcher", [""])[0]
                        folder = query.get("folder", [""])[0]
                        self._send_json(app.api_games_by_system(system, launcher, folder))
                        return

                    if parsed.path == "/api/recent":
                        self._send_json({"items": app.load_recent_items()})
                        return

                    if parsed.path == "/api/favorites":
                        self._send_json({"items": app.load_favorite_items()})
                        return

                    if parsed.path in ("/api/input/context", "/api/v1/input/context"):
                        result = app.api_input_context()
                        self._send_json(result, 200 if result.get("ok") else 500)
                        return

                    self._send_json({"error": "Not found"}, 404)
                except Exception as exc:
                    self._send_json({"error": str(exc)}, 500)

            def do_POST(self):
                parsed = urlparse(self.path)

                try:
                    payload = self._read_json()

                    if parsed.path == "/api/launch":
                        self._send_json(app.api_launch(payload))
                        return

                    if parsed.path == "/api/launch-by-system":
                        self._send_json(app.api_launch_by_system(payload))
                        return

                    if parsed.path == "/api/show":
                        app.api_show()
                        self._send_json({"ok": True})
                        return

                    if parsed.path in ("/api/input", "/api/v1/input"):
                        action = str(payload.get("action", "")).strip().lower()
                        result = app.api_input(action)
                        status = 200 if result.get("ok") else 409
                        self._send_json(result, status)
                        return

                    if parsed.path in ("/api/session/close", "/api/v1/session/close"):
                        result = app.close_active_session(force=False)
                        self._send_json(result, 200 if result.get("ok") else 409)
                        return

                    if parsed.path in ("/api/session/force-close", "/api/v1/session/force-close"):
                        result = app.close_active_session(force=True)
                        self._send_json(result, 200 if result.get("ok") else 409)
                        return

                    self._send_json({"error": "Not found"}, 404)
                except Exception as exc:
                    self._send_json({"error": str(exc)}, 500)

        self.httpd = ThreadingHTTPServer((self.host, self.port), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        if self.httpd is None:
            return

        self.httpd.shutdown()
        self.httpd.server_close()
        self.httpd = None
        self.thread = None

    def is_running(self) -> bool:
        return self.httpd is not None
