from __future__ import annotations

import csv
import glob
import os
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional

from core.format_sniffer import RoutingResult, sniff
from engine.registry import ENGINE_NAME, NEEDS_EXTRA_TRACK_PASSES

from core.paths import engine_entry_script, is_frozen

RUN_ENGINE_FLAG = "--run-engine"


@dataclass
class EngineRunResult:
    argv: List[str]
    exit_code: int
    stdout: str
    stderr: str
    started_at: datetime
    finished_at: datetime
    timed_out: bool = False
    note: str = ""


@dataclass
class TrackPoint:

    start_time_sec: Optional[float] = None
    end_time_sec: Optional[float] = None
    time_source: str = ""

    latitude: Optional[float] = None
    longitude: Optional[float] = None
    speed_kmh: Optional[float] = None
    track_deg: Optional[float] = None
    gps_date: str = ""
    gps_utc_time: str = ""
    gps_checksum_ok: Optional[bool] = None

    latitude_last: Optional[float] = None
    longitude_last: Optional[float] = None
    speed_kmh_last: Optional[float] = None

    x_g: Optional[float] = None
    y_g: Optional[float] = None
    z_g: Optional[float] = None
    x_g_cal: Optional[float] = None
    y_g_cal: Optional[float] = None
    z_g_cal: Optional[float] = None

    source_file: str = ""

    @property
    def has_fix(self) -> bool:
        return self.latitude is not None and self.longitude is not None

    @property
    def has_gps_record(self) -> bool:
        return bool((self.gps_utc_time or "").strip() or (self.gps_date or "").strip())

    @property
    def is_dropout(self) -> bool:
        return self.has_gps_record and not self.has_fix

    @property
    def display_latitude(self) -> Optional[float]:
        return self.latitude if self.latitude is not None else self.latitude_last

    @property
    def display_longitude(self) -> Optional[float]:
        return self.longitude if self.longitude is not None else self.longitude_last


@dataclass
class ExtractionResult:
    routing: RoutingResult
    points: List[TrackPoint]
    engine_runs: List[EngineRunResult]
    used_input_path: str
    primary_source_file: Optional[str] = None
    time_source: str = ""
    warnings: List[str] = field(default_factory=list)

    @property
    def fix_count(self) -> int:
        return sum(1 for p in self.points if p.has_fix)

    @property
    def dropout_count(self) -> int:
        return sum(1 for p in self.points if p.is_dropout)


def build_subprocess_argv(engine_args: List[str]) -> List[str]:
    if is_frozen():
        return [sys.executable, RUN_ENGINE_FLAG, ENGINE_NAME, *engine_args]
    return [sys.executable, engine_entry_script(), ENGINE_NAME, *engine_args]


def run_engine(input_path: str, output_dir: str, slack: bool = False,
                extra_args: Optional[List[str]] = None, note: str = "",
                timeout_sec: Optional[float] = 1800) -> EngineRunResult:
    os.makedirs(output_dir, exist_ok=True)
    engine_args = ["-o", output_dir]
    if slack:
        engine_args.append("--slack")
    engine_args.extend(extra_args or [])
    engine_args.append(input_path)

    argv = build_subprocess_argv(engine_args)
    started_at = datetime.now()
    timed_out = False
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout_sec,
        )
        exit_code, stdout, stderr = proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        exit_code = -1
        stdout = exc.stdout or ""
        stderr = (exc.stderr or "") + f"\n[engine_adapter] {timeout_sec}초 초과로 강제 종료됨"
    finished_at = datetime.now()
    return EngineRunResult(
        argv=argv, exit_code=exit_code, stdout=stdout, stderr=stderr,
        started_at=started_at, finished_at=finished_at, timed_out=timed_out, note=note,
    )


def _f(value: Optional[str]) -> Optional[float]:
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _b(value: Optional[str]) -> Optional[bool]:
    if value is None:
        return None
    value = value.strip().lower()
    if value in ("true", "1", "yes"):
        return True
    if value in ("false", "0", "no"):
        return False
    return None


def find_csvs(output_dir: str, filename: str) -> List[str]:
    return sorted(glob.glob(os.path.join(output_dir, "**", filename), recursive=True))


def _count_fixes(csv_path: str) -> int:
    try:
        with open(csv_path, newline="", encoding="utf-8") as f:
            return sum(
                1 for row in csv.DictReader(f)
                if (row.get("latitude") or "").strip()
            )
    except OSError:
        return 0


def pick_primary_csv(csv_paths: List[str]) -> Optional[str]:
    if not csv_paths:
        return None
    if len(csv_paths) == 1:
        return csv_paths[0]
    return max(csv_paths, key=_count_fixes)


def load_timeline(csv_path: str) -> List[TrackPoint]:
    points: List[TrackPoint] = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            points.append(TrackPoint(
                start_time_sec=_f(row.get("start_time_sec")),
                end_time_sec=_f(row.get("end_time_sec")),
                time_source=(row.get("time_source") or "").strip(),
                latitude=_f(row.get("latitude")),
                longitude=_f(row.get("longitude")),
                speed_kmh=_f(row.get("speed_kmh")),
                track_deg=_f(row.get("track_deg")),
                gps_date=(row.get("gps_date") or "").strip(),
                gps_utc_time=(row.get("gps_utc_time") or "").strip(),
                gps_checksum_ok=_b(row.get("gps_checksum_ok")),
                latitude_last=_f(row.get("latitude_last")),
                longitude_last=_f(row.get("longitude_last")),
                speed_kmh_last=_f(row.get("speed_kmh_last")),
                x_g=_f(row.get("x_g")), y_g=_f(row.get("y_g")), z_g=_f(row.get("z_g")),
                x_g_cal=_f(row.get("x_g_cal")), y_g_cal=_f(row.get("y_g_cal")),
                z_g_cal=_f(row.get("z_g_cal")),
                source_file=csv_path,
            ))
    return points


def load_coordinates_as_points(csv_path: str) -> List[TrackPoint]:
    points: List[TrackPoint] = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            points.append(TrackPoint(
                start_time_sec=_f(row.get("start_time_sec")),
                end_time_sec=_f(row.get("end_time_sec")),
                time_source=(row.get("time_source") or "").strip(),
                latitude=_f(row.get("latitude")),
                longitude=_f(row.get("longitude")),
                speed_kmh=_f(row.get("speed_kmh")),
                track_deg=_f(row.get("track_deg")),
                gps_date=(row.get("date") or "").strip(),
                gps_utc_time=(row.get("utc_time") or "").strip(),
                gps_checksum_ok=_b(row.get("checksum_ok")),
                source_file=csv_path,
            ))
    _fill_last_known(points)
    return points


def _fill_last_known(points: List[TrackPoint]) -> None:
    last_lat = last_lon = last_speed = None
    for p in points:
        if p.has_fix:
            last_lat, last_lon, last_speed = p.latitude, p.longitude, p.speed_kmh
        p.latitude_last = last_lat
        p.longitude_last = last_lon
        p.speed_kmh_last = last_speed


def _collect_warnings(output_dir: str) -> List[str]:
    messages: List[str] = []
    for log_path in find_csvs(output_dir, "warnings.log"):
        try:
            with open(log_path, encoding="utf-8", errors="replace") as f:
                messages.extend(line.rstrip() for line in f if line.strip())
        except OSError:
            continue
    return messages


def _pending_track_ids(output_dir: str) -> List[int]:
    if not NEEDS_EXTRA_TRACK_PASSES:
        return []
    pending: List[int] = []
    for table_path in find_csvs(output_dir, "track_table.csv"):
        try:
            with open(table_path, newline="", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    if (row.get("handler_type") or "").strip() not in ("text", "sbtl", "subt"):
                        continue
                    if _b(row.get("is_text_track")):
                        continue
                    track_id = _f(row.get("track"))
                    if track_id is not None:
                        pending.append(int(track_id))
        except OSError:
            continue
    return sorted(set(pending))


def run_full_extraction(input_path: str, output_dir: str, slack: bool = False,
                          timeout_sec: Optional[float] = 1800) -> ExtractionResult:
    routing = sniff(input_path)
    if not routing.supported:
        return ExtractionResult(
            routing=routing, points=[], engine_runs=[], used_input_path=input_path,
        )

    engine_runs = [run_engine(input_path, output_dir, slack=slack, timeout_sec=timeout_sec)]

    for track_id in _pending_track_ids(output_dir):
        engine_runs.append(run_engine(
            input_path, output_dir, slack=slack,
            extra_args=[f"--mp4-opt=--track-id {track_id}"],
            note=f"추가 text Track {track_id}", timeout_sec=timeout_sec,
        ))

    timeline_paths = find_csvs(output_dir, "timeline.csv")
    primary = pick_primary_csv(timeline_paths)
    if primary is not None:
        points = load_timeline(primary)
    else:
        coord_paths = find_csvs(output_dir, "coordinates.csv")
        primary = pick_primary_csv(coord_paths)
        points = load_coordinates_as_points(primary) if primary else []

    points.sort(key=lambda p: (p.start_time_sec is None, p.start_time_sec or 0.0))
    time_source = next((p.time_source for p in points if p.time_source), "")

    return ExtractionResult(
        routing=routing, points=points, engine_runs=engine_runs,
        used_input_path=input_path, primary_source_file=primary,
        time_source=time_source, warnings=_collect_warnings(output_dir),
    )


def load_existing_results(output_dir: str) -> tuple[List[TrackPoint], Optional[str], str]:
    timeline_paths = find_csvs(output_dir, "timeline.csv")
    primary = pick_primary_csv(timeline_paths)
    if primary is not None:
        points = load_timeline(primary)
    else:
        coord_paths = find_csvs(output_dir, "coordinates.csv")
        primary = pick_primary_csv(coord_paths)
        points = load_coordinates_as_points(primary) if primary else []
    points.sort(key=lambda p: (p.start_time_sec is None, p.start_time_sec or 0.0))
    time_source = next((p.time_source for p in points if p.time_source), "")
    return points, primary, time_source
