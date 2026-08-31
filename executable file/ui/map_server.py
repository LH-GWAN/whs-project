from __future__ import annotations

import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

from core.paths import resource_root

_MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".pmtiles": "application/octet-stream",
}

_RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")


def web_dir() -> str:
    return os.path.join(resource_root(), "ui", "web")


def vendor_dir() -> str:
    return os.path.join(resource_root(), "ui", "vendor")


def basemap_path() -> Optional[str]:
    candidate = os.path.join(resource_root(), "assets", "korea.pmtiles")
    return candidate if os.path.isfile(candidate) else None


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # noqa: A003
        pass

    def _resolve(self, url_path: str) -> Optional[str]:
        name = url_path.lstrip("/").split("?", 1)[0]
        if not name:
            name = "map.html"
        if "/" in name or "\\" in name or name.startswith("."):
            return None
        if name == "basemap.pmtiles":
            return basemap_path()
        for base in (web_dir(), vendor_dir()):
            candidate = os.path.join(base, name)
            if os.path.isfile(candidate):
                return candidate
        return None

    def do_HEAD(self):  # noqa: N802
        self._serve(head_only=True)

    def do_GET(self):  # noqa: N802
        if self.path.split("?", 1)[0] == "/basemap-info":
            body = b'{"available": true}' if basemap_path() else b'{"available": false}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self._serve()

    def _serve(self, head_only: bool = False):
        path = self._resolve(self.path)
        if path is None:
            self.send_error(404)
            return

        size = os.path.getsize(path)
        ctype = _MIME.get(os.path.splitext(path)[1].lower(), "application/octet-stream")
        rng = self.headers.get("Range")

        start, end = 0, size - 1
        partial = False
        if rng:
            m = _RANGE_RE.search(rng)
            if m:
                s, e = m.group(1), m.group(2)
                if s:
                    start = int(s)
                    end = int(e) if e else size - 1
                elif e:
                    start = max(0, size - int(e))
                if start >= size:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.end_headers()
                    return
                end = min(end, size - 1)
                partial = True

        length = end - start + 1
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        if head_only:
            return

        with open(path, "rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(65536, remaining))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    return
                remaining -= len(chunk)


class MapServer:

    _instance: Optional["MapServer"] = None

    def __init__(self):
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._httpd.daemon_threads = True
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                         name="MapServer", daemon=True)
        self._thread.start()

    @classmethod
    def instance(cls) -> "MapServer":
        if cls._instance is None:
            cls._instance = MapServer()
        return cls._instance

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def map_url(self) -> str:
        return f"{self.base_url}/map.html"

    def shutdown(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
