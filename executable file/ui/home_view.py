from __future__ import annotations

from typing import List

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from storage.history_store import CaseRecord

VIDEO_FILTER = "블랙박스 영상 (*.mp4 *.avi);;모든 파일 (*)"


class HomeView(QWidget):
    video_selected = Signal(str)
    history_item_opened = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)

        title = QLabel("GPS Tracer")
        title.setProperty("role", "title")

        heading = QLabel("Title")
        heading.setProperty("role", "heading")

        self._upload_box = QFrame()
        self._upload_box.setProperty("role", "upload-box")
        self._upload_box.setMinimumSize(360, 220)
        upload_layout = QVBoxLayout(self._upload_box)
        upload_btn = QPushButton("Upload")
        upload_btn.clicked.connect(self._on_upload_clicked)
        upload_layout.addStretch(1)
        upload_layout.addWidget(upload_btn)
        upload_layout.addStretch(1)

        left = QVBoxLayout()
        left.addWidget(heading)
        left.addWidget(self._upload_box)
        left.addStretch(1)

        history_heading = QLabel("History")
        self._history_list = QListWidget()
        self._history_list.itemDoubleClicked.connect(self._on_history_double_clicked)

        right = QVBoxLayout()
        right.addWidget(history_heading)
        right.addWidget(self._history_list, 1)

        body = QHBoxLayout()
        body.addLayout(left, 2)
        body.addLayout(right, 1)

        layout = QVBoxLayout(self)
        layout.addWidget(title)
        layout.addLayout(body, 1)

    def _on_upload_clicked(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "블랙박스 영상 선택", "", VIDEO_FILTER)
        if path:
            self.video_selected.emit(path)

    def _on_history_double_clicked(self, item: QListWidgetItem) -> None:
        case_id = item.data(1)
        if case_id is not None:
            self.history_item_opened.emit(case_id)

    def set_history(self, cases: List[CaseRecord]) -> None:
        self._history_list.clear()
        for case in cases:
            label = f"{case.case_number} - {case.source_video_filename} ({case.created_at})"
            item = QListWidgetItem(label)
            item.setData(1, case.id)
            self._history_list.addItem(item)
