from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from engine.engine_adapter import TrackPoint

DEFAULT_THRESHOLD_MPS2 = 3.0

MAX_GAP_SEC = 5.0


@dataclass
class FlaggedSegment:
    start_index: int
    end_index: int
    start_time_sec: Optional[float]
    end_time_sec: Optional[float]
    max_acceleration_mps2: float


def _time_of(point: TrackPoint) -> Optional[float]:
    return point.start_time_sec


def _fix_key(point: TrackPoint):
    utc = (point.gps_utc_time or "").strip()
    if utc:
        return ("utc", point.gps_date or "", utc)
    return ("val", point.latitude, point.longitude, point.speed_kmh)


def _distinct_fix_indices(points: List[TrackPoint]) -> List[int]:
    out: List[int] = []
    prev_key = None
    for i, p in enumerate(points):
        if p.speed_kmh is None or _time_of(p) is None:
            continue
        key = _fix_key(p)
        if prev_key is None or key != prev_key:
            out.append(i)
            prev_key = key
    return out


def compute_flagged_segments(points: List[TrackPoint],
                              threshold_mps2: float = DEFAULT_THRESHOLD_MPS2,
                              max_gap_sec: float = MAX_GAP_SEC) -> List[FlaggedSegment]:
    n = len(points)
    flags = [False] * n
    accel_at: List[Optional[float]] = [None] * n

    usable = _distinct_fix_indices(points)

    for prev_i, cur_i in zip(usable, usable[1:]):
        prev_p, cur_p = points[prev_i], points[cur_i]
        t0, t1 = _time_of(prev_p), _time_of(cur_p)
        dt = t1 - t0
        if dt <= 0 or dt > max_gap_sec:
            continue
        dv_mps = (cur_p.speed_kmh - prev_p.speed_kmh) / 3.6
        accel = dv_mps / dt
        if abs(accel) < threshold_mps2:
            continue
        for i in range(prev_i, cur_i + 1):
            flags[i] = True
            if accel_at[i] is None or abs(accel) > abs(accel_at[i]):
                accel_at[i] = accel

    segments: List[FlaggedSegment] = []
    i = 0
    while i < n:
        if not flags[i]:
            i += 1
            continue
        start = i
        while i < n and flags[i]:
            i += 1
        end = i - 1
        seg_accels = [abs(a) for a in accel_at[start:end + 1] if a is not None]
        if not seg_accels:
            continue
        segments.append(FlaggedSegment(
            start_index=start,
            end_index=end,
            start_time_sec=_time_of(points[start]),
            end_time_sec=points[end].end_time_sec or _time_of(points[end]),
            max_acceleration_mps2=max(seg_accels),
        ))
    return segments
