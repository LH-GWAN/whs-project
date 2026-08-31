from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS cases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_number TEXT NOT NULL,
    examiner TEXT,
    memo TEXT,
    source_video_path TEXT,
    source_video_filename TEXT,
    source_video_size_bytes INTEGER,
    source_video_sha256 TEXT,
    duration_sec REAL,
    detected_format TEXT,
    avi_repaired INTEGER DEFAULT 0,
    output_folder TEXT,
    analysis_settings_json TEXT,
    created_at TEXT NOT NULL,
    last_opened_at TEXT,
    report_pdf_path TEXT
);

CREATE TABLE IF NOT EXISTS engine_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id INTEGER NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    script_name TEXT NOT NULL,
    argv_json TEXT,
    exit_code INTEGER,
    stdout_log_path TEXT,
    started_at TEXT,
    finished_at TEXT
);
"""


def default_app_data_dir() -> str:
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser(r"~\AppData\Local")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(base, "GPSTracer")


@dataclass
class CaseRecord:
    id: Optional[int]
    case_number: str
    examiner: str = ""
    memo: str = ""
    source_video_path: str = ""
    source_video_filename: str = ""
    source_video_size_bytes: int = 0
    source_video_sha256: str = ""
    duration_sec: Optional[float] = None
    detected_format: str = ""
    avi_repaired: bool = False
    output_folder: str = ""
    analysis_settings: Dict = field(default_factory=dict)
    created_at: str = ""
    last_opened_at: Optional[str] = None
    report_pdf_path: Optional[str] = None


class HistoryStore:
    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or os.path.join(default_app_data_dir(), "history.db")
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "HistoryStore":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def add_case(self, case: CaseRecord) -> int:
        cur = self._conn.execute(
            """INSERT INTO cases (
                case_number, examiner, memo, source_video_path, source_video_filename,
                source_video_size_bytes, source_video_sha256, duration_sec, detected_format,
                avi_repaired, output_folder, analysis_settings_json, created_at,
                last_opened_at, report_pdf_path
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                case.case_number, case.examiner, case.memo, case.source_video_path,
                case.source_video_filename, case.source_video_size_bytes, case.source_video_sha256,
                case.duration_sec, case.detected_format, int(case.avi_repaired), case.output_folder,
                json.dumps(case.analysis_settings, ensure_ascii=False),
                case.created_at or datetime.now().isoformat(timespec="seconds"),
                case.last_opened_at, case.report_pdf_path,
            ),
        )
        self._conn.commit()
        return cur.lastrowid

    def add_engine_run(self, case_id: int, script_name: str, argv: List[str], exit_code: int,
                        stdout_log_path: str, started_at: str, finished_at: str) -> int:
        cur = self._conn.execute(
            """INSERT INTO engine_runs (
                case_id, script_name, argv_json, exit_code, stdout_log_path, started_at, finished_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (case_id, script_name, json.dumps(argv, ensure_ascii=False), exit_code,
             stdout_log_path, started_at, finished_at),
        )
        self._conn.commit()
        return cur.lastrowid

    def get_case(self, case_id: int) -> Optional[CaseRecord]:
        row = self._conn.execute("SELECT * FROM cases WHERE id = ?", (case_id,)).fetchone()
        return _row_to_case(row) if row else None

    def list_cases(self, search: Optional[str] = None) -> List[CaseRecord]:
        if search:
            like = f"%{search}%"
            rows = self._conn.execute(
                """SELECT * FROM cases
                   WHERE case_number LIKE ? OR examiner LIKE ?
                   ORDER BY created_at DESC""",
                (like, like),
            ).fetchall()
        else:
            rows = self._conn.execute("SELECT * FROM cases ORDER BY created_at DESC").fetchall()
        return [_row_to_case(r) for r in rows]

    def touch_last_opened(self, case_id: int) -> None:
        self._conn.execute(
            "UPDATE cases SET last_opened_at = ? WHERE id = ?",
            (datetime.now().isoformat(timespec="seconds"), case_id),
        )
        self._conn.commit()

    def update_case_extraction(self, case_id: int, duration_sec: Optional[float],
                                avi_repaired: bool, output_folder: str) -> None:
        self._conn.execute(
            """UPDATE cases SET duration_sec = ?, avi_repaired = ?, output_folder = ?
               WHERE id = ?""",
            (duration_sec, int(avi_repaired), output_folder, case_id),
        )
        self._conn.commit()

    def set_report_path(self, case_id: int, report_pdf_path: str) -> None:
        self._conn.execute(
            "UPDATE cases SET report_pdf_path = ? WHERE id = ?", (report_pdf_path, case_id),
        )
        self._conn.commit()


def _row_to_case(row: sqlite3.Row) -> CaseRecord:
    return CaseRecord(
        id=row["id"],
        case_number=row["case_number"],
        examiner=row["examiner"] or "",
        memo=row["memo"] or "",
        source_video_path=row["source_video_path"] or "",
        source_video_filename=row["source_video_filename"] or "",
        source_video_size_bytes=row["source_video_size_bytes"] or 0,
        source_video_sha256=row["source_video_sha256"] or "",
        duration_sec=row["duration_sec"],
        detected_format=row["detected_format"] or "",
        avi_repaired=bool(row["avi_repaired"]),
        output_folder=row["output_folder"] or "",
        analysis_settings=json.loads(row["analysis_settings_json"] or "{}"),
        created_at=row["created_at"] or "",
        last_opened_at=row["last_opened_at"],
        report_pdf_path=row["report_pdf_path"],
    )
