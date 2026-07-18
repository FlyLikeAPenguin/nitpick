from __future__ import annotations

import json
import logging
import subprocess
import sys
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import yaml

from .cache import ErrorCache
from .config import PROJECT_ROOT, Config

log = logging.getLogger(__name__)


class DashboardHandler(SimpleHTTPRequestHandler):
    cache: ErrorCache
    config: Config
    static_dir: Path

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        params = parse_qs(parsed.query)

        if path.startswith("/api/"):
            self._handle_api(path, params)
        elif path == "" or path == "/":
            self._serve_file("index.html")
        else:
            self._serve_file(path.lstrip("/"))

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        if path.startswith("/api/errors/") and path.endswith("/ignore"):
            fp = path.split("/")[3]
            self.cache.update_error_status(fp, "ignored")
            self._json_response({"status": "ok"})
        elif path == "/api/settings":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length) if length else b"{}"
            try:
                updates = json.loads(body)
                for key, value in updates.items():
                    self.cache.set_setting(key, str(value))
                self._json_response(self.cache.get_all_settings())
            except Exception as e:
                self._json_response({"status": "error", "message": str(e)})

        elif path.startswith("/api/service-settings/"):
            service = "/".join(path.split("/")[3:])
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length) if length else b"{}"
            try:
                updates = json.loads(body)
                for key, value in updates.items():
                    if value is None or value == "":
                        self.cache.delete_service_setting(service, key)
                    else:
                        self.cache.set_service_setting(service, key, str(value))
                self._json_response(self.cache.get_service_settings(service))
            except Exception as e:
                self._json_response({"status": "error", "message": str(e)})

        elif path == "/api/subscriptions":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length) if length else b"{}"
            try:
                data = json.loads(body)
                self.cache.add_subscription(
                    service=data["service"],
                    slack_user_id=data["slack_user_id"],
                    display_name=data.get("display_name", ""),
                )
                self._json_response({"status": "ok"})
            except Exception as e:
                self._json_response({"status": "error", "message": str(e)})

        elif path == "/api/services":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length) if length else b"{}"
            try:
                data = json.loads(body)
                config_yml = self._read_config_yml()
                config_yml.setdefault("services", {})[data["name"]] = {
                    "datadog_query": data["datadog_query"],
                    "regions": data.get("regions", ["eu", "us"]),
                    "code_paths": data.get("code_paths", []),
                }
                self._write_config_yml(config_yml)
                self._json_response({"status": "ok"})
            except Exception as e:
                self._json_response({"status": "error", "message": str(e)})

        elif path == "/api/trigger-run":
            try:
                subprocess.Popen(
                    [sys.executable, "-m", "src.main", "run"],
                    cwd=str(PROJECT_ROOT),
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                self._json_response({"status": "ok", "message": "Run triggered"})
            except Exception as e:
                self._json_response({"status": "error", "message": str(e)})
        else:
            self.send_error(404)

    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        if path.startswith("/api/subscriptions/"):
            try:
                sub_id = int(path.split("/")[-1])
                self.cache.remove_subscription(sub_id)
                self._json_response({"status": "ok"})
            except Exception as e:
                self._json_response({"status": "error", "message": str(e)})
        elif path.startswith("/api/services/"):
            try:
                svc_name = "/".join(path.split("/")[3:])  # handle names with slashes
                config_yml = self._read_config_yml()
                services = config_yml.get("services", {})
                if svc_name in services:
                    del services[svc_name]
                    self._write_config_yml(config_yml)
                    self._json_response({"status": "ok"})
                else:
                    self.send_error(404)
            except Exception as e:
                self._json_response({"status": "error", "message": str(e)})
        else:
            self.send_error(404)

    def _handle_api(self, path: str, params: dict[str, list[str]]) -> None:
        if path == "/api/runs":
            limit = int(params.get("limit", ["50"])[0])
            data = self.cache.get_runs(limit=limit)
            self._json_response(data)

        elif path == "/api/errors":
            service = params.get("service", [None])[0]
            status = params.get("status", [None])[0]
            limit = int(params.get("limit", ["200"])[0])
            data = self.cache.get_errors(service=service, status=status, limit=limit)
            self._json_response(data)

        elif path.startswith("/api/errors/") and not path.endswith("/ignore"):
            fp = path.split("/")[3]
            data = self.cache.get_error_detail(fp)
            if data:
                self._json_response(data)
            else:
                self.send_error(404)

        elif path == "/api/investigations":
            limit = int(params.get("limit", ["100"])[0])
            data = self.cache.get_investigations(limit=limit)
            self._json_response(data)

        elif path == "/api/related-errors":
            data = self.cache.get_related_errors()
            self._json_response(data)

        elif path == "/api/stats":
            data = self.cache.get_stats()
            self._json_response(data)

        elif path == "/api/settings":
            data = self.cache.get_all_settings()
            self._json_response(data)

        elif path == "/api/service-settings":
            data = self.cache.get_all_service_settings()
            self._json_response(data)

        elif path == "/api/subscriptions":
            data = self.cache.get_subscriptions()
            self._json_response(data)

        elif path == "/api/services":
            # Return full service configs from config.yml
            config_yml = self._read_config_yml()
            services = config_yml.get("services", {})
            repos = config_yml.get("repos", {})
            self._json_response({"services": services, "repos": repos})

        elif path == "/api/logs":
            lines = int(params.get("lines", ["200"])[0])
            log_file = self.static_dir.parent / "logs" / "nitpick.log"
            if not log_file.exists():
                self._json_response({"lines": []})
                return
            with open(log_file, "rb") as f:
                # Read from end of file efficiently
                try:
                    f.seek(0, 2)
                    size = f.tell()
                    # Read last ~64KB max
                    read_size = min(size, 65536)
                    f.seek(size - read_size)
                    tail = f.read().decode("utf-8", errors="replace")
                except Exception:
                    tail = ""
                all_lines = tail.strip().split("\n")
                self._json_response({"lines": all_lines[-lines:]})

        else:
            self.send_error(404)

    @staticmethod
    def _read_config_yml() -> dict[str, Any]:
        config_path = PROJECT_ROOT / "config.yml"
        if config_path.exists():
            with open(config_path) as f:
                return yaml.safe_load(f) or {}
        return {}

    @staticmethod
    def _write_config_yml(data: dict[str, Any]) -> None:
        config_path = PROJECT_ROOT / "config.yml"
        with open(config_path, "w") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)

    def _json_response(self, data: Any) -> None:
        body = json.dumps(data, default=str).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _serve_file(self, filename: str) -> None:
        filepath = self.static_dir / filename
        if not filepath.exists():
            self.send_error(404)
            return

        content = filepath.read_bytes()
        content_type = "text/html"
        if filename.endswith(".css"):
            content_type = "text/css"
        elif filename.endswith(".js"):
            content_type = "application/javascript"
        elif filename.endswith(".json"):
            content_type = "application/json"
        elif filename.endswith(".png"):
            content_type = "image/png"
        elif filename.endswith(".ico"):
            content_type = "image/x-icon"

        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, format: str, *args: Any) -> None:
        log.debug(format, *args)


def run_dashboard(config: Config) -> None:
    cache = ErrorCache(config.db_path)
    DashboardHandler.cache = cache
    DashboardHandler.config = config
    DashboardHandler.static_dir = config.static_dir

    HTTPServer.allow_reuse_address = True
    server = HTTPServer(("127.0.0.1", config.dashboard_port), DashboardHandler)
    log.info("Dashboard running at http://127.0.0.1:%d", config.dashboard_port)
    print(f"Dashboard running at http://127.0.0.1:{config.dashboard_port}")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Dashboard shutting down")
        server.shutdown()
