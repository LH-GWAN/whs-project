from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional

from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
)

from core.acceleration import DEFAULT_THRESHOLD_MPS2


@dataclass
class CaseInfoInput:
    case_number: str
    examiner: str
    memo: str
    settings: Dict
    accel_threshold_mps2: float
    carve_slack: bool


class CaseInfoDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Case Information")
        self.setModal(True)
        self.setMinimumWidth(360)

        self._case_number = QLineEdit()
        self._tracker_cb = QCheckBox("Tracker")
        self._speed_cb = QCheckBox("Speed")
        self._location_cb = QCheckBox("Location")
        for cb in (self._tracker_cb, self._speed_cb, self._location_cb):
            cb.setChecked(True)
        self._examiner = QLineEdit()
        self._memo = QTextEdit()
        self._memo.setFixedHeight(70)
        self._threshold = QDoubleSpinBox()
        self._threshold.setRange(0.5, 20.0)
        self._threshold.setSingleStep(0.5)
        self._threshold.setValue(DEFAULT_THRESHOLD_MPS2)
        self._threshold.setSuffix(" m/s²")

        self._slack_cb = QCheckBox("MP4 슬랙 카빙 (과거 주행 이력 추가 추출, 느림)")
        self._slack_cb.setChecked(False)
        self._slack_cb.setToolTip(
            "컨테이너가 참조하지 않는 영역에서 이전 녹화분의 GPS를 추가로 카빙합니다.\n"
            "결과는 engine_output/slack/ 에 따로 저장되며, 현재 영상의 재생 시각과는\n"
            "매핑되지 않습니다(과거 녹화분이라 sample table이 없음)."
        )

        title = QLabel("Case Information")
        title.setProperty("role", "title")

        form = QFormLayout()
        form.addRow("Case Number", self._case_number)

        analysis_row = QHBoxLayout()
        analysis_row.addWidget(self._tracker_cb)
        analysis_row.addWidget(self._speed_cb)
        analysis_row.addWidget(self._location_cb)
        form.addRow("Analysis Setting", analysis_row)

        form.addRow("Examiner", self._examiner)
        form.addRow("memo", self._memo)
        form.addRow("급가속 임계값", self._threshold)
        form.addRow("", self._slack_cb)

        start_btn = QPushButton("Start")
        start_btn.setProperty("role", "primary")
        start_btn.clicked.connect(self._on_start)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(start_btn)

        layout = QVBoxLayout(self)
        layout.addWidget(title)
        layout.addLayout(form)
        layout.addLayout(btn_row)

        self.result_input: Optional[CaseInfoInput] = None

    def _on_start(self) -> None:
        if not self._case_number.text().strip():
            self._case_number.setStyleSheet("border: 1px solid red;")
            return
        self.result_input = CaseInfoInput(
            case_number=self._case_number.text().strip(),
            examiner=self._examiner.text().strip(),
            memo=self._memo.toPlainText().strip(),
            settings={
                "tracker": self._tracker_cb.isChecked(),
                "speed": self._speed_cb.isChecked(),
                "location": self._location_cb.isChecked(),
                "accel_threshold_mps2": self._threshold.value(),
                "carve_slack": self._slack_cb.isChecked(),
            },
            accel_threshold_mps2=self._threshold.value(),
            carve_slack=self._slack_cb.isChecked(),
        )
        self.accept()
