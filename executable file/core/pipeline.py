from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Dict, List, Optional

from core import acceleration, duration as duration_mod, format_sniffer, hashing
from core.acceleration import FlaggedSegment
from core.format_sniffer import RoutingResult
from engine.engine_adapter import (
    ExtractionResult,
    TrackPoint,
    load_existing_results,
    run_full_extraction,
)
from storage.history_store import CaseRecord, HistoryStore

ProgressCallback = Optional[Callable[[str], None]]


@dataclass
class PipelineResult:
    case_id: int
    case_folder: str
    source_copy_path: str
    extraction: ExtractionResult
    duration_sec: Optional[float]
    flagged_segments: List[FlaggedSegment]
    sha256: str
    accel_threshold_mps2: float

    @property
    def points(self) -> List[TrackPoint]:
        return self.extraction.points


def _safe_case_folder_name(case_number: str, case_id: int) -> str:
    safe = "".join(c for c in case_number if c.isalnum() or c in "-_") or "case"
    return f"{safe}_{case_id}"


def run_analysis_pipeline(
    video_path: str,
    case_number: str,
    examiner: str,
    memo: str,
    settings: Dict,
    cases_root_dir: str,
    history_store: HistoryStore,
    accel_threshold_mps2: float = acceleration.DEFAULT_THRESHOLD_MPS2,
    carve_slack: bool = False,
    progress_cb: ProgressCallback = None,
) -> PipelineResult:
    def report(msg: str) -> None:
        if progress_cb:
            progress_cb(msg)

    report("파일 해시 계산 중 (SHA-256)...")
    sha256 = hashing.sha256_file(video_path)

    report("파일 형식 확인 중...")
    routing = format_sniffer.sniff(video_path)

    provisional = CaseRecord(
        id=None,
        case_number=case_number,
        examiner=examiner,
        memo=memo,
        source_video_path=video_path,
        source_video_filename=os.path.basename(video_path),
        source_video_size_bytes=os.path.getsize(video_path),
        source_video_sha256=sha256,
        detected_format=routing.container,
        analysis_settings=settings,
    )
    case_id = history_store.add_case(provisional)

    case_folder = os.path.join(cases_root_dir, _safe_case_folder_name(case_number, case_id))
    source_dir = os.path.join(case_folder, "source")
    output_dir = os.path.join(case_folder, "engine_output")
    os.makedirs(source_dir, exist_ok=True)
    os.makedirs(output_dir, exist_ok=True)

    report("원본 영상을 사건 폴더로 복사 중 (무결성 보존)...")
    source_copy_path = os.path.join(source_dir, os.path.basename(video_path))
    shutil.copy2(video_path, source_copy_path)

    report("GPS/센서 메타데이터 추출 중..." + (" (슬랙 카빙 포함)" if carve_slack else ""))
    extraction = run_full_extraction(video_path, output_dir, slack=carve_slack)

    report("영상 길이 계산 중...")
    dur = duration_mod.get_duration_sec(video_path, routing.container, engine_output_dir=output_dir)

    report("급가속 구간 분석 중...")
    flagged = acceleration.compute_flagged_segments(extraction.points, accel_threshold_mps2)

    _write_case_json(case_folder, case_id, case_number, examiner, memo, video_path, sha256,
                      routing, extraction, dur, settings, flagged, carve_slack)

    for run in extraction.engine_runs:
        log_path = os.path.join(
            output_dir, f"_run_{run.started_at.strftime('%H%M%S%f')}.log",
        )
        with open(log_path, "w", encoding="utf-8") as f:
            f.write(
                "ARGV: " + " ".join(run.argv) + "\n"
                + (f"NOTE: {run.note}\n" if run.note else "")
                + f"EXIT: {run.exit_code}\n\n"
                "--- stdout ---\n" + run.stdout + "\n"
                "--- stderr ---\n" + run.stderr
            )
        history_store.add_engine_run(
            case_id, "integration_blackbox", run.argv, run.exit_code, log_path,
            run.started_at.isoformat(timespec="seconds"),
            run.finished_at.isoformat(timespec="seconds"),
        )

    history_store.update_case_extraction(
        case_id, duration_sec=dur, avi_repaired=False, output_folder=output_dir,
    )

    report("완료")
    return PipelineResult(
        case_id=case_id,
        case_folder=case_folder,
        source_copy_path=source_copy_path,
        extraction=extraction,
        duration_sec=dur,
        flagged_segments=flagged,
        sha256=sha256,
        accel_threshold_mps2=accel_threshold_mps2,
    )


def reopen_case(case: CaseRecord) -> PipelineResult:
    points, primary, time_source = (
        load_existing_results(case.output_folder) if case.output_folder else ([], None, "")
    )

    threshold = float(case.analysis_settings.get("accel_threshold_mps2",
                                                  acceleration.DEFAULT_THRESHOLD_MPS2))
    flagged = acceleration.compute_flagged_segments(points, threshold)

    routing = RoutingResult(
        container=case.detected_format, supported=True,
        reason="저장된 사건을 다시 열었습니다 (재추출 없이 기존 결과 표시).",
    )
    extraction = ExtractionResult(
        routing=routing, points=points, engine_runs=[],
        used_input_path=case.source_video_path, primary_source_file=primary,
        time_source=time_source,
    )
    case_folder = os.path.dirname(case.output_folder) if case.output_folder else ""
    return PipelineResult(
        case_id=case.id or -1,
        case_folder=case_folder,
        source_copy_path=(os.path.join(case_folder, "source", case.source_video_filename)
                          if case_folder else ""),
        extraction=extraction,
        duration_sec=case.duration_sec,
        flagged_segments=flagged,
        sha256=case.source_video_sha256,
        accel_threshold_mps2=threshold,
    )


def _write_case_json(case_folder, case_id, case_number, examiner, memo, video_path, sha256,
                      routing, extraction: ExtractionResult, dur, settings, flagged,
                      carve_slack) -> None:
    case_json_path = os.path.join(case_folder, "case.json")
    with open(case_json_path, "w", encoding="utf-8") as f:
        json.dump({
            "case_id": case_id,
            "case_number": case_number,
            "examiner": examiner,
            "memo": memo,
            "source_video_filename": os.path.basename(video_path),
            "source_video_sha256": sha256,
            "detected_format": routing.container,
            "extension_mismatch": routing.extension_mismatch,
            "engine": "integration_blackbox",
            "slack_carving": carve_slack,
            "duration_sec": dur,
            "time_source": extraction.time_source,
            "analysis_settings": settings,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "point_count": len(extraction.points),
            "gps_fix_count": extraction.fix_count,
            "flagged_segment_count": len(flagged),
        }, f, ensure_ascii=False, indent=2)
