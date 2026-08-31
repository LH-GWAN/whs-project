from __future__ import annotations

import os
import struct
from typing import Optional

from core.paths import ensure_vendor_importable

ensure_vendor_importable()

import integration_avi as _avi  # noqa: E402
import integration_mp4 as _mp4  # noqa: E402

from core.format_sniffer import CONTAINER_AVI, CONTAINER_MP4  # noqa: E402


def get_duration_sec(path: str, container: str,
                      engine_output_dir: Optional[str] = None) -> Optional[float]:
    try:
        if container == CONTAINER_AVI:
            return _avi_duration(path)
        if container == CONTAINER_MP4:
            duration = _mp4_mvhd_duration(path)
            if duration:
                return duration
            if engine_output_dir:
                return _timeline_fallback(engine_output_dir)
    except (OSError, struct.error, ValueError, IndexError):
        return None
    return None


def _avi_duration(path: str) -> Optional[float]:
    import mmap

    with open(path, "rb") as f:
        mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
        try:
            hdrl, _movi, _idx1, _avix = _avi.find_top_level_sections(mm)
            if hdrl is None:
                return None
            dw_streams, streams = _avi.parse_hdrl(mm, hdrl)
            stream_table = _avi.build_stream_table(dw_streams, streams, [])
            duration, _source = _avi.compute_video_duration(mm, hdrl, stream_table)
            return duration
        finally:
            mm.close()


def _mp4_mvhd_duration(path: str) -> Optional[float]:
    filesize = os.path.getsize(path)
    with open(path, "rb") as f:
        top_boxes = _mp4.scan_top_level(f, filesize)
        moov = _mp4.find_box(top_boxes, b"moov")
        if moov is None:
            return None
        moov_children = list(_mp4.iter_boxes(f, moov.payload_start, moov.end,
                                              context="duration-moov"))
        mvhd = _mp4.find_box(moov_children, b"mvhd")
        if mvhd is None:
            return None
        f.seek(mvhd.payload_start)
        payload = f.read(mvhd.size - mvhd.header_size)

    if len(payload) < 4:
        return None
    version = payload[0]
    if version == 1:
        if len(payload) < 32:
            return None
        timescale = struct.unpack_from(">I", payload, 20)[0]
        duration = struct.unpack_from(">Q", payload, 24)[0]
    else:
        if len(payload) < 20:
            return None
        timescale = struct.unpack_from(">I", payload, 12)[0]
        duration = struct.unpack_from(">I", payload, 16)[0]
    if not timescale or not duration:
        return None
    return duration / timescale


def _timeline_fallback(engine_output_dir: str) -> Optional[float]:
    import csv
    import glob

    best_end = None
    pattern = os.path.join(engine_output_dir, "**", "timeline.csv")
    for csv_path in glob.glob(pattern, recursive=True):
        try:
            with open(csv_path, newline="", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    raw = (row.get("end_time_sec") or "").strip()
                    if not raw:
                        continue
                    try:
                        value = float(raw)
                    except ValueError:
                        continue
                    if best_end is None or value > best_end:
                        best_end = value
        except OSError:
            continue
    return best_end
