from __future__ import annotations

import os

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QTabWidget, QVBoxLayout, QWidget

from core.pipeline import PipelineResult
from ui.location_tab import LocationTab
from ui.speed_tab import SpeedTab
from ui.tracker_tab import TrackerTab


def _format_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} TB"


def _format_duration(seconds) -> str:
    if seconds is None:
        return "알 수 없음"
    total = int(round(seconds))
    return f"{total // 60:02d}:{total % 60:02d}"


class AnalysisView(QWidget):
    home_requested = Signal()
    report_requested = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._result: PipelineResult | None = None

        title = QLabel("GPS Tracer")
        title.setProperty("role", "title")
        self._case_label = QLabel("")

        report_btn = QPushButton("Report")
        report_btn.setProperty("role", "primary")
        report_btn.clicked.connect(self._on_report_clicked)
        home_btn = QPushButton("Home")
        home_btn.setProperty("role", "primary")
        home_btn.clicked.connect(self.home_requested.emit)

        header = QWidget()
        header.setObjectName("HeaderBar")
        header_layout = QHBoxLayout(header)
        header_layout.addWidget(title)
        header_layout.addStretch(1)
        header_layout.addWidget(self._case_label)
        header_layout.addWidget(report_btn)
        header_layout.addWidget(home_btn)

        self._file_badge = QLabel("MP4")
        self._file_name_label = QLabel("")
        self._file_size_label = QLabel("")
        self._duration_label = QLabel("")
        self._hash_label = QLabel("")

        file_info = QWidget()
        file_info.setObjectName("FileInfoBar")
        file_info_layout = QHBoxLayout(file_info)
        for w in (self._file_badge, self._file_name_label, self._file_size_label,
                  self._duration_label, self._hash_label):
            file_info_layout.addWidget(w)
        file_info_layout.addStretch(1)

        self._tabs = QTabWidget()
        self._tabs.currentChanged.connect(self._on_tab_changed)
        self._tracker_tab = TrackerTab()
        self._speed_tab = SpeedTab()
        self._location_tab = LocationTab()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(header)
        layout.addWidget(file_info)
        layout.addWidget(self._tabs, 1)

    def load_result(self, result: PipelineResult, case_number: str, settings: dict) -> None:
        self._result = result
        self._case_label.setText(f"Case Number : {case_number}")

        video_path = result.source_copy_path or result.extraction.used_input_path
        filename = os.path.basename(video_path) if video_path else "-"
        size_bytes = os.path.getsize(video_path) if video_path and os.path.isfile(video_path) else 0

        self._file_name_label.setText(filename)
        self._file_size_label.setText(f"Size : {_format_size(size_bytes)}")
        self._duration_label.setText(f"Duration : {_format_duration(result.duration_sec)}")
        self._hash_label.setText(f"Hash : {result.sha256[:16]}…")

        self._tabs.clear()
        if settings.get("tracker", True):
            self._tabs.addTab(self._tracker_tab, "Tracker")
            self._tracker_tab.stop()
            if video_path and os.path.isfile(video_path):
                self._tracker_tab.load_video(video_path)
            self._tracker_tab.load_track(result.extraction.points, result.flagged_segments)
        if settings.get("speed", True):
            self._tabs.addTab(self._speed_tab, "Speed Analysis")
            self._speed_tab.load(result.extraction.points, result.flagged_segments)
        if settings.get("location", True):
            self._tabs.addTab(self._location_tab, "Location Analysis")
            self._location_tab.load(result.extraction.points, result.flagged_segments)

        self._on_tab_changed(self._tabs.currentIndex())

    def _on_tab_changed(self, _index: int) -> None:
        widget = self._tabs.currentWidget()
        loader = getattr(widget, "ensure_map_loaded", None)
        if callable(loader):
            loader()

    def _on_report_clicked(self) -> None:
        if self._result is not None:
            self.report_requested.emit(self._result)
