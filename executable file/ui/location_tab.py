from __future__ import annotations

from typing import List

from PySide6.QtGui import QColor
from PySide6.QtWidgets import QHBoxLayout, QSplitter, QTableWidget, QTableWidgetItem, QWidget
from PySide6.QtCore import Qt

from core.acceleration import FlaggedSegment
from engine.engine_adapter import TrackPoint
from ui.map_view import MapView

_FLAG_COLOR = QColor(255, 200, 200)
_DROPOUT_COLOR = QColor(190, 110, 40)
_NOGPS_COLOR = QColor(170, 170, 170)


class LocationTab(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._map = MapView()
        self._table = QTableWidget(0, 4)
        self._table.setHorizontalHeaderLabels(["시각(초)", "위도", "경도", "속도(km/h)"])
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._table.setSelectionBehavior(QTableWidget.SelectRows)
        self._table.itemSelectionChanged.connect(self._on_row_selected)
        self._points: List[TrackPoint] = []

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self._map)
        splitter.addWidget(self._table)
        splitter.setSizes([500, 500])

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(splitter)

    def ensure_map_loaded(self) -> None:
        self._map.ensure_loaded()

    def _on_row_selected(self) -> None:
        rows = self._table.selectionModel().selectedRows() if self._table.selectionModel() else []
        if not rows:
            return
        index = rows[0].row()
        if 0 <= index < len(self._points):
            point = self._points[index]
            if point.start_time_sec is not None:
                self._map.set_playback_time(point.start_time_sec)

    def load(self, records: List[TrackPoint], segments: List[FlaggedSegment]) -> None:
        self._points = records
        self._map.set_track(records, segments)

        flagged_indices = set()
        for seg in segments:
            flagged_indices.update(range(seg.start_index, seg.end_index + 1))

        self._table.setRowCount(len(records))
        for row, rec in enumerate(records):
            time_item = QTableWidgetItem(
                f"{rec.start_time_sec:.2f}" if rec.start_time_sec is not None else "-")
            if rec.has_fix:
                lat_item = QTableWidgetItem(f"{rec.latitude:.6f}")
                lon_item = QTableWidgetItem(f"{rec.longitude:.6f}")
            elif rec.is_dropout:
                lat_item = QTableWidgetItem("(GPS 끊김)")
                lon_item = QTableWidgetItem("-")
                lat_item.setForeground(_DROPOUT_COLOR)
                lon_item.setForeground(_DROPOUT_COLOR)
            else:
                lat_item = QTableWidgetItem("(GPS 없음)")
                lon_item = QTableWidgetItem("-")
                lat_item.setForeground(_NOGPS_COLOR)
                lon_item.setForeground(_NOGPS_COLOR)
            speed_item = QTableWidgetItem(
                f"{rec.speed_kmh:.1f}" if rec.speed_kmh is not None else "-")
            items = (time_item, lat_item, lon_item, speed_item)
            if row in flagged_indices:
                for item in items:
                    item.setBackground(_FLAG_COLOR)
            for col, item in enumerate(items):
                self._table.setItem(row, col, item)
