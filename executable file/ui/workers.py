from __future__ import annotations

from typing import Dict, Optional

from PySide6.QtCore import QThread, Signal

from core.pipeline import PipelineResult, run_analysis_pipeline
from storage.history_store import HistoryStore


class AnalysisWorker(QThread):
    progress = Signal(str)
    finished_ok = Signal(object)
    failed = Signal(str)

    def __init__(self, video_path: str, case_number: str, examiner: str, memo: str,
                 settings: Dict, cases_root_dir: str, history_db_path: Optional[str],
                 accel_threshold_mps2: float, carve_slack: bool = False, parent=None):
        super().__init__(parent)
        self._video_path = video_path
        self._case_number = case_number
        self._examiner = examiner
        self._memo = memo
        self._settings = settings
        self._cases_root_dir = cases_root_dir
        self._history_db_path = history_db_path
        self._accel_threshold_mps2 = accel_threshold_mps2
        self._carve_slack = carve_slack

    def run(self) -> None:
        try:
            with HistoryStore(self._history_db_path) as store:
                result: PipelineResult = run_analysis_pipeline(
                    video_path=self._video_path,
                    case_number=self._case_number,
                    examiner=self._examiner,
                    memo=self._memo,
                    settings=self._settings,
                    cases_root_dir=self._cases_root_dir,
                    history_store=store,
                    accel_threshold_mps2=self._accel_threshold_mps2,
                    carve_slack=self._carve_slack,
                    progress_cb=self.progress.emit,
                )
            self.finished_ok.emit(result)
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(f"{type(exc).__name__}: {exc}")
