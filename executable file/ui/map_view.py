from __future__ import annotations

import json
from typing import List, Optional

from PySide6.QtCore import QTimer, QUrl
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QVBoxLayout, QWidget

from core.acceleration import FlaggedSegment
from engine.engine_adapter import TrackPoint
from ui.map_server import MapServer


class MapView(QWidget):
    MAX_RELOAD_ATTEMPTS = 3

    def __init__(self, parent=None):
        super().__init__(parent)
        self._loaded = False
        self._pending_js: List[str] = []
        self._reload_attempts = 0
        self._last_track_js: Optional[str] = None
        self._last_time_js: Optional[str] = None

        self._view = QWebEngineView(self)
        self._view.loadFinished.connect(self._on_load_finished)
        self._view.page().renderProcessTerminated.connect(self._on_render_process_gone)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._view)

        self._load_started = False

    def ensure_loaded(self) -> None:
        if not self._load_started:
            self._load_page()

    def showEvent(self, event):  # noqa: N802
        super().showEvent(event)
        self.ensure_loaded()

    def _load_page(self) -> None:
        self._loaded = False
        self._load_started = True
        self._view.load(QUrl(MapServer.instance().map_url()))

    def _on_load_finished(self, ok: bool) -> None:
        self._loaded = bool(ok)
        if not ok:
            return
        self._reload_attempts = 0
        for script in (self._last_track_js, self._last_time_js):
            if script:
                self._view.page().runJavaScript(script)
        for script in self._pending_js:
            self._view.page().runJavaScript(script)
        self._pending_js.clear()

    def _on_render_process_gone(self, status, exit_code: int) -> None:
        self._loaded = False
        if self._reload_attempts >= self.MAX_RELOAD_ATTEMPTS:
            return
        self._reload_attempts += 1
        QTimer.singleShot(600, self._load_page)

    def _run_js(self, script: str) -> None:
        if self._loaded:
            self._view.page().runJavaScript(script)
        else:
            self._pending_js.append(script)

    def set_track(self, points: List[TrackPoint],
                   segments: Optional[List[FlaggedSegment]] = None) -> None:
        payload = {
            "points": [
                {
                    "t": p.start_time_sec,
                    "lat": p.latitude,
                    "lon": p.longitude,
                    "v": p.speed_kmh,
                    "d": 1 if p.is_dropout else 0,
                }
                for p in points
            ],
            "flagged": [[s.start_index, s.end_index] for s in (segments or [])],
        }
        self._last_track_js = f"renderTrack({json.dumps(payload, ensure_ascii=False)});"
        self._last_time_js = None
        self._run_js(self._last_track_js)

    def set_playback_time(self, seconds: float) -> None:
        self._last_time_js = f"setPlaybackTime({float(seconds)});"
        self._run_js(self._last_time_js)
