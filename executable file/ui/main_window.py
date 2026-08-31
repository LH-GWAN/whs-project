from __future__ import annotations

import os
from typing import Optional

from PySide6.QtWidgets import QFileDialog, QMainWindow, QMessageBox, QProgressDialog, QStackedWidget

from core.pipeline import PipelineResult, reopen_case
from report.report_builder import ReportExporter, render_report_html
from storage.history_store import HistoryStore, default_app_data_dir
from ui.analysis_view import AnalysisView
from ui.case_info_dialog import CaseInfoDialog
from ui.home_view import HomeView
from ui.workers import AnalysisWorker


class MainWindow(QMainWindow):
    def __init__(self, app_data_dir: Optional[str] = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("GPS Tracer")
        self.resize(1100, 720)

        self._app_data_dir = app_data_dir or default_app_data_dir()
        self._cases_root_dir = os.path.join(self._app_data_dir, "cases")
        self._history_db_path = os.path.join(self._app_data_dir, "history.db")
        os.makedirs(self._cases_root_dir, exist_ok=True)

        self._home = HomeView()
        self._home.video_selected.connect(self._on_video_selected)
        self._home.history_item_opened.connect(self._on_history_item_opened)

        self._analysis_view = AnalysisView()
        self._analysis_view.home_requested.connect(self._show_home)
        self._analysis_view.report_requested.connect(self._on_report_requested)

        self._stack = QStackedWidget()
        self._stack.addWidget(self._home)
        self._stack.addWidget(self._analysis_view)
        self.setCentralWidget(self._stack)

        self._worker: Optional[AnalysisWorker] = None
        self._progress: Optional[QProgressDialog] = None
        self._report_exporter: Optional[ReportExporter] = None

        self._current_case_id: Optional[int] = None
        self._current_case_number: str = ""
        self._current_examiner: str = ""
        self._current_memo: str = ""
        self._current_settings: dict = {}

        self._pending_settings: dict = {}
        self._pending_case_number: str = ""
        self._pending_examiner: str = ""
        self._pending_memo: str = ""

        self._refresh_history()

    def _refresh_history(self) -> None:
        with HistoryStore(self._history_db_path) as store:
            self._home.set_history(store.list_cases())

    def _show_home(self) -> None:
        self._refresh_history()
        self._stack.setCurrentWidget(self._home)

    def _on_video_selected(self, video_path: str) -> None:
        dialog = CaseInfoDialog(self)
        if dialog.exec() != CaseInfoDialog.Accepted or dialog.result_input is None:
            return
        info = dialog.result_input
        self._pending_settings = info.settings
        self._pending_case_number = info.case_number
        self._pending_examiner = info.examiner
        self._pending_memo = info.memo

        self._progress = QProgressDialog("분석 준비 중...", None, 0, 0, self)
        self._progress.setWindowTitle("GPS Tracer")
        self._progress.setCancelButton(None)
        self._progress.setMinimumDuration(0)
        self._progress.show()

        self._worker = AnalysisWorker(
            video_path=video_path,
            case_number=info.case_number,
            examiner=info.examiner,
            memo=info.memo,
            settings=info.settings,
            cases_root_dir=self._cases_root_dir,
            history_db_path=self._history_db_path,
            accel_threshold_mps2=info.accel_threshold_mps2,
            carve_slack=info.carve_slack,
        )
        self._worker.progress.connect(self._on_worker_progress)
        self._worker.finished_ok.connect(self._on_worker_finished)
        self._worker.failed.connect(self._on_worker_failed)
        self._worker.start()

    def _on_worker_progress(self, message: str) -> None:
        if self._progress is not None:
            self._progress.setLabelText(message)

    def _on_worker_finished(self, result: PipelineResult) -> None:
        if self._progress is not None:
            self._progress.close()
            self._progress = None
        self._current_case_id = result.case_id
        self._current_case_number = self._pending_case_number
        self._current_examiner = self._pending_examiner
        self._current_memo = self._pending_memo
        self._current_settings = self._pending_settings
        self._analysis_view.load_result(result, self._pending_case_number, self._pending_settings)
        self._stack.setCurrentWidget(self._analysis_view)

    def _on_worker_failed(self, message: str) -> None:
        if self._progress is not None:
            self._progress.close()
            self._progress = None
        QMessageBox.critical(self, "분석 실패", f"분석 중 오류가 발생했습니다:\n{message}")

    def _on_history_item_opened(self, case_id: int) -> None:
        with HistoryStore(self._history_db_path) as store:
            case = store.get_case(case_id)
            if case is None:
                return
            store.touch_last_opened(case_id)
        result = reopen_case(case)
        self._current_case_id = case.id
        self._current_case_number = case.case_number
        self._current_examiner = case.examiner
        self._current_memo = case.memo
        self._current_settings = case.analysis_settings
        self._analysis_view.load_result(result, case.case_number, case.analysis_settings)
        self._stack.setCurrentWidget(self._analysis_view)

    def _on_report_requested(self, result: PipelineResult) -> None:
        default_name = f"{self._current_case_number or 'case'}_report.pdf"
        out_path, _ = QFileDialog.getSaveFileName(self, "리포트 저장", default_name, "PDF (*.pdf)")
        if not out_path:
            return

        html_str = render_report_html(
            result, self._current_case_number, self._current_examiner, self._current_memo,
        )

        def on_done(success: bool, error_message: str) -> None:
            self._report_exporter = None
            if not success:
                QMessageBox.critical(self, "Report", f"리포트 생성에 실패했습니다: {error_message}")
                return
            if self._current_case_id is not None:
                with HistoryStore(self._history_db_path) as store:
                    store.set_report_path(self._current_case_id, out_path)
            QMessageBox.information(self, "Report", f"리포트를 저장했습니다:\n{out_path}")

        self._report_exporter = ReportExporter(html_str, out_path, on_done, parent=self)
