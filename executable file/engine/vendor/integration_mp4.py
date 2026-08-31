# ---- 여기부터: GPS_metadata_fragment_iso4_Atext.py (공통 Box/NMEA/Atext 파서 + 루트 A, fragmented) ----
import argparse
import csv
import math
import os
import re
import struct
import sys
from dataclasses import dataclass, field
from typing import Optional

if sys.stdout.encoding is None or sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except AttributeError:
        pass

DEBUG = False

WARNINGS = []
MAX_SAMPLE_COUNT_PER_RUN = 200_000

KEPT_KINDS = {"gsensor", "gps_nmea", "vendor_raw"}

def warn(msg):
    WARNINGS.append(msg)
    print(f"[WARN] {msg}")

def info(msg):
    print(msg)

def debug(msg):
    if DEBUG:
        print(f"[DEBUG] {msg}")

def hex_preview_lines(payload, n=48):
    chunk = payload[:n]
    hex_part = " ".join(f"{b:02X}" for b in chunk)
    ascii_part = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
    return hex_part, ascii_part

@dataclass
class Box:
    box_type: bytes
    start: int
    size: int
    header_size: int

    @property
    def payload_start(self):
        return self.start + self.header_size

    @property
    def end(self):
        return self.start + self.size

def read_box_header(f, pos, limit, context="", allow_size_zero=False):
    if pos + 8 > limit:
        return None

    f.seek(pos)
    header = f.read(8)
    if len(header) < 8:
        warn(f"{context} @ 0x{pos:X}: Box 헤더(8바이트)를 다 읽지 못함 - 파일이 잘렸을 수 있음")
        return None

    size32 = struct.unpack(">I", header[0:4])[0]
    box_type = header[4:8]
    header_size = 8

    if size32 == 1:
        if pos + 16 > limit:
            warn(f"{context} @ 0x{pos:X}: largesize를 읽기엔 부모 경계를 벗어남")
            return None
        f.seek(pos + 8)
        ext = f.read(8)
        if len(ext) < 8:
            warn(f"{context} @ 0x{pos:X}: extended size(64bit) 읽기 실패")
            return None
        size = struct.unpack(">Q", ext)[0]
        header_size = 16
    elif size32 == 0:
        if not allow_size_zero:
            warn(f"{context} Box {box_type!r} @ 0x{pos:X}: child Box에서 size==0이 나옴 "
                 f"(비정상 - size==0은 Top-Level에서만 '끝까지'라는 뜻이어야 함)")
        size = limit - pos
    else:
        size = size32

    if size < header_size:
        warn(f"{context} Box {box_type!r} @ 0x{pos:X}: size({size})가 header_size"
             f"({header_size})보다 작음 - 손상된 Box, 순회 중단")
        return None
    if pos + size > limit:
        warn(f"{context} Box {box_type!r} @ 0x{pos:X}: box_end(0x{pos+size:X})가 "
             f"부모 경계(0x{limit:X})를 넘어감 - 손상된 Box, 순회 중단")
        return None

    box = Box(box_type, pos, size, header_size)
    debug(f"[BOX] offset=0x{pos:X} size={size} type={box_type!r} end=0x{box.end:X}")
    return box

def iter_child_boxes(f, parent_start, parent_end, context="", allow_size_zero=False):
    pos = parent_start
    while pos < parent_end:
        box = read_box_header(f, pos, parent_end, context=context, allow_size_zero=allow_size_zero)
        if box is None:
            break
        yield box
        pos = box.end

def find_box(boxes, box_type):
    for b in boxes:
        if b.box_type == box_type:
            return b
    return None

def find_all(boxes, box_type):
    return [b for b in boxes if b.box_type == box_type]

def read_payload(f, box):
    f.seek(box.payload_start)
    return f.read(box.size - box.header_size)

def parse_ftyp(f, box):
    payload = read_payload(f, box)
    if len(payload) < 8:
        warn("ftyp payload가 너무 짧음")
        return None
    major_brand = payload[0:4]
    minor_version = struct.unpack(">I", payload[4:8])[0]
    compat = payload[8:]
    compatible_brands = [compat[i:i + 4] for i in range(0, len(compat) - (len(compat) % 4), 4)]
    return {"major_brand": major_brand, "minor_version": minor_version,
            "compatible_brands": compatible_brands}

@dataclass
class TrexDefault:
    track_id: int
    default_sample_description_index: int
    default_sample_duration: int
    default_sample_size: int
    default_sample_flags: int

@dataclass
class TextTrackInfo:
    track_id: int
    timescale: int
    handler_type: bytes
    handler_name: str = ""

def parse_tkhd(f, tkhd_box):
    payload = read_payload(f, tkhd_box)
    if len(payload) < 1:
        warn(f"tkhd @ 0x{tkhd_box.start:X}: payload가 비어있음")
        return None
    version = payload[0]
    if version == 0:
        need = 4 + 4 + 4 + 4
        if len(payload) < need:
            warn(f"tkhd(v0) @ 0x{tkhd_box.start:X}: payload가 짧아 track_ID를 읽을 수 없음")
            return None
        track_id = struct.unpack(">I", payload[12:16])[0]
    elif version == 1:
        need = 4 + 8 + 8 + 4
        if len(payload) < need:
            warn(f"tkhd(v1) @ 0x{tkhd_box.start:X}: payload가 짧아 track_ID를 읽을 수 없음")
            return None
        track_id = struct.unpack(">I", payload[20:24])[0]
    else:
        warn(f"tkhd @ 0x{tkhd_box.start:X}: Unsupported tkhd version={version} - 해당 Track 건너뜀")
        return None
    return track_id

def parse_mdhd(f, mdhd_box):
    payload = read_payload(f, mdhd_box)
    if len(payload) < 1:
        warn(f"mdhd @ 0x{mdhd_box.start:X}: payload가 비어있음")
        return None
    version = payload[0]
    if version == 0:
        if len(payload) < 16:
            warn(f"mdhd(v0) @ 0x{mdhd_box.start:X}: payload가 짧음")
            return None
        timescale = struct.unpack(">I", payload[12:16])[0]
    elif version == 1:
        if len(payload) < 24:
            warn(f"mdhd(v1) @ 0x{mdhd_box.start:X}: payload가 짧음")
            return None
        timescale = struct.unpack(">I", payload[20:24])[0]
    else:
        warn(f"mdhd @ 0x{mdhd_box.start:X}: Unsupported mdhd version={version}")
        return None
    if timescale == 0:
        warn(f"mdhd @ 0x{mdhd_box.start:X}: timescale==0 (0으로 나누기 방지 필요)")
        return None
    return timescale

def parse_hdlr(f, hdlr_box):
    payload = read_payload(f, hdlr_box)
    if len(payload) < 24:
        warn(f"hdlr @ 0x{hdlr_box.start:X}: payload가 너무 짧음")
        return None, ""
    handler_type = payload[8:12]
    name_bytes = payload[24:]
    name = ""
    if name_bytes:
        plen = name_bytes[0]
        if 0 < plen <= len(name_bytes) - 1:
            try:
                name = name_bytes[1:1 + plen].decode("utf-8", errors="replace")
            except Exception:
                name = ""
        if not name:
            raw = name_bytes.split(b"\x00", 1)[0]
            try:
                name = raw.decode("utf-8", errors="replace")
            except Exception:
                name = ""
    return handler_type, name

def parse_stsd_types(f, mdia_box):
    mdia_children = list(iter_child_boxes(f, mdia_box.payload_start, mdia_box.end,
                                           context=f"mdia@0x{mdia_box.start:X}"))
    minf_box = find_box(mdia_children, b"minf")
    if minf_box is None:
        return []
    minf_children = list(iter_child_boxes(f, minf_box.payload_start, minf_box.end,
                                           context=f"minf@0x{minf_box.start:X}"))
    stbl_box = find_box(minf_children, b"stbl")
    if stbl_box is None:
        return []
    stbl_children = list(iter_child_boxes(f, stbl_box.payload_start, stbl_box.end,
                                           context=f"stbl@0x{stbl_box.start:X}"))
    stsd_box = find_box(stbl_children, b"stsd")
    if stsd_box is None:
        return []
    payload = read_payload(f, stsd_box)
    if len(payload) < 8:
        return []
    entry_count = struct.unpack(">I", payload[4:8])[0]
    types = []
    pos = stsd_box.payload_start + 8
    end = stsd_box.end
    for _ in range(entry_count):
        if pos + 8 > end:
            break
        f.seek(pos)
        eh = f.read(8)
        entry_size = struct.unpack(">I", eh[0:4])[0]
        entry_type = eh[4:8]
        if entry_size < 8 or pos + entry_size > end:
            break
        types.append(entry_type.decode("ascii", errors="replace"))
        pos += entry_size
    return types

def parse_trak(f, trak_box):
    trak_children = list(iter_child_boxes(f, trak_box.payload_start, trak_box.end,
                                           context=f"trak@0x{trak_box.start:X}"))
    tkhd_box = find_box(trak_children, b"tkhd")
    mdia_box = find_box(trak_children, b"mdia")

    track_id = parse_tkhd(f, tkhd_box) if tkhd_box else None
    if tkhd_box is None:
        warn(f"trak @ 0x{trak_box.start:X}: tkhd를 찾지 못함")

    handler_type = None
    handler_name = ""
    timescale = None
    stsd_types = []
    if mdia_box is not None:
        mdia_children = list(iter_child_boxes(f, mdia_box.payload_start, mdia_box.end,
                                               context=f"mdia@0x{mdia_box.start:X}"))
        mdhd_box = find_box(mdia_children, b"mdhd")
        hdlr_box = find_box(mdia_children, b"hdlr")
        if mdhd_box is not None:
            timescale = parse_mdhd(f, mdhd_box)
        else:
            warn(f"trak @ 0x{trak_box.start:X}: mdia 안에 mdhd가 없음")
        if hdlr_box is not None:
            handler_type, handler_name = parse_hdlr(f, hdlr_box)
        else:
            warn(f"trak @ 0x{trak_box.start:X}: mdia 안에 hdlr이 없음")
        stsd_types = parse_stsd_types(f, mdia_box)
    else:
        warn(f"trak @ 0x{trak_box.start:X}: mdia를 찾지 못함")

    return track_id, handler_type, handler_name, timescale, stsd_types

def parse_trex(f, trex_box):
    payload = read_payload(f, trex_box)
    if len(payload) < 24:
        warn(f"trex @ 0x{trex_box.start:X}: payload가 24바이트보다 짧음")
        return None
    track_id, sdi, dur, size, flags = struct.unpack(">IIIII", payload[4:24])
    trex = TrexDefault(track_id=track_id, default_sample_description_index=sdi,
                        default_sample_duration=dur, default_sample_size=size,
                        default_sample_flags=flags)
    debug(f"[TREX] track_ID={track_id} default_duration={dur} default_size={size} "
          f"default_flags=0x{flags:08X}")
    return trex

def parse_mvex(f, mvex_box):
    trex_defaults = {}
    children = list(iter_child_boxes(f, mvex_box.payload_start, mvex_box.end,
                                      context=f"mvex@0x{mvex_box.start:X}"))
    for trex_box in find_all(children, b"trex"):
        trex = parse_trex(f, trex_box)
        if trex is not None:
            trex_defaults[trex.track_id] = trex
    return trex_defaults

def parse_moov(f, moov_box):
    moov_children = list(iter_child_boxes(f, moov_box.payload_start, moov_box.end,
                                           context=f"moov@0x{moov_box.start:X}"))

    text_tracks = {}
    all_tracks = []
    for trak_box in find_all(moov_children, b"trak"):
        track_id, handler_type, handler_name, timescale, stsd_types = parse_trak(f, trak_box)
        all_tracks.append({
            "track_id": track_id, "handler_type": handler_type, "handler_name": handler_name,
            "timescale": timescale, "stsd_types": stsd_types,
        })
        if track_id is None or handler_type is None or timescale is None:
            continue
        debug(f"[TRACK] track_ID={track_id} handler_type={handler_type!r} timescale={timescale}")
        if handler_type == b"text":
            text_tracks[track_id] = TextTrackInfo(track_id=track_id, timescale=timescale,
                                                   handler_type=handler_type,
                                                   handler_name=handler_name)

    trex_defaults = {}
    mvex_box = find_box(moov_children, b"mvex")
    if mvex_box is not None:
        trex_defaults = parse_mvex(f, mvex_box)
    else:
        warn("moov 안에 mvex가 없음 - tfhd에 default 값이 없는 필드는 fallback 불가")

    info("\n[Track 목록 (moov)]")
    for t in all_tracks:
        htxt = t["handler_type"].decode("ascii", errors="replace") if t["handler_type"] else "?"
        info(f"  track_ID={t['track_id'] if t['track_id'] is not None else '?'} "
             f"handler_type={htxt} handler_name={t['handler_name'] or '-'} timescale={t['timescale']}")

    return text_tracks, trex_defaults, all_tracks

TFHD_BASE_DATA_OFFSET_PRESENT = 0x000001
TFHD_SAMPLE_DESCRIPTION_INDEX_PRESENT = 0x000002
TFHD_DEFAULT_SAMPLE_DURATION_PRESENT = 0x000008
TFHD_DEFAULT_SAMPLE_SIZE_PRESENT = 0x000010
TFHD_DEFAULT_SAMPLE_FLAGS_PRESENT = 0x000020
TFHD_DURATION_IS_EMPTY = 0x010000
TFHD_DEFAULT_BASE_IS_MOOF = 0x020000

TRUN_DATA_OFFSET_PRESENT = 0x000001
TRUN_FIRST_SAMPLE_FLAGS_PRESENT = 0x000004
TRUN_SAMPLE_DURATION_PRESENT = 0x000100
TRUN_SAMPLE_SIZE_PRESENT = 0x000200
TRUN_SAMPLE_FLAGS_PRESENT = 0x000400
TRUN_SAMPLE_COMPOSITION_TIME_OFFSET_PRESENT = 0x000800

@dataclass
class TfhdInfo:
    track_id: int
    flags: int
    base_data_offset: Optional[int] = None
    sample_description_index: Optional[int] = None
    default_sample_duration: Optional[int] = None
    default_sample_size: Optional[int] = None
    default_sample_flags: Optional[int] = None

    @property
    def duration_is_empty(self):
        return bool(self.flags & TFHD_DURATION_IS_EMPTY)

    @property
    def default_base_is_moof(self):
        return bool(self.flags & TFHD_DEFAULT_BASE_IS_MOOF)

def parse_tfhd(f, tfhd_box):
    payload = read_payload(f, tfhd_box)
    if len(payload) < 8:
        warn(f"tfhd @ 0x{tfhd_box.start:X}: payload가 8바이트보다 짧음")
        return None

    flags = int.from_bytes(payload[1:4], "big")
    track_id = struct.unpack(">I", payload[4:8])[0]
    off = 8

    base_data_offset = None
    sdi = None
    default_duration = None
    default_size = None
    default_flags = None

    if flags & TFHD_BASE_DATA_OFFSET_PRESENT:
        base_data_offset = struct.unpack(">Q", payload[off:off + 8])[0]
        off += 8
    if flags & TFHD_SAMPLE_DESCRIPTION_INDEX_PRESENT:
        sdi = struct.unpack(">I", payload[off:off + 4])[0]
        off += 4
    if flags & TFHD_DEFAULT_SAMPLE_DURATION_PRESENT:
        default_duration = struct.unpack(">I", payload[off:off + 4])[0]
        off += 4
    if flags & TFHD_DEFAULT_SAMPLE_SIZE_PRESENT:
        default_size = struct.unpack(">I", payload[off:off + 4])[0]
        off += 4
    if flags & TFHD_DEFAULT_SAMPLE_FLAGS_PRESENT:
        default_flags = struct.unpack(">I", payload[off:off + 4])[0]
        off += 4

    tfhd = TfhdInfo(track_id=track_id, flags=flags, base_data_offset=base_data_offset,
                     sample_description_index=sdi, default_sample_duration=default_duration,
                     default_sample_size=default_size, default_sample_flags=default_flags)

    debug(f"[TFHD] track_ID={track_id} flags=0x{flags:06X} base_data_offset={base_data_offset} "
          f"default_duration={default_duration} default_size={default_size} "
          f"default_flags={default_flags}")
    if tfhd.duration_is_empty:
        warn(f"tfhd @ 0x{tfhd_box.start:X}: duration-is-empty flag 설정됨 "
             f"(track_ID={track_id}) - 이 Track Fragment는 sample duration이 없는 특수 케이스")
    return tfhd

def parse_tfdt(f, tfdt_box):
    payload = read_payload(f, tfdt_box)
    if len(payload) < 1:
        warn(f"tfdt @ 0x{tfdt_box.start:X}: payload가 비어있음")
        return None
    version = payload[0]
    if version == 0:
        if len(payload) < 8:
            warn(f"tfdt(v0) @ 0x{tfdt_box.start:X}: payload가 짧음")
            return None
        base_decode_time = struct.unpack(">I", payload[4:8])[0]
    elif version == 1:
        if len(payload) < 12:
            warn(f"tfdt(v1) @ 0x{tfdt_box.start:X}: payload가 짧음")
            return None
        base_decode_time = struct.unpack(">Q", payload[4:12])[0]
    else:
        warn(f"tfdt @ 0x{tfdt_box.start:X}: Unsupported tfdt version={version}")
        return None
    debug(f"[TFDT] baseMediaDecodeTime={base_decode_time}")
    return base_decode_time

@dataclass
class RunSample:
    duration: Optional[int]
    size: Optional[int]
    flags: Optional[int]
    composition_time_offset: Optional[int]

@dataclass
class TrunInfo:
    version: int
    flags: int
    sample_count: int
    data_offset: Optional[int]
    first_sample_flags: Optional[int]
    samples: list

def parse_trun(f, trun_box):
    payload = read_payload(f, trun_box)
    if len(payload) < 8:
        warn(f"trun @ 0x{trun_box.start:X}: payload가 8바이트보다 짧음")
        return None

    version = payload[0]
    flags = int.from_bytes(payload[1:4], "big")
    sample_count = struct.unpack(">I", payload[4:8])[0]

    if sample_count > MAX_SAMPLE_COUNT_PER_RUN:
        warn(f"trun @ 0x{trun_box.start:X}: sample_count={sample_count}가 비정상적으로 큼 - 순회 중단")
        return None

    off = 8
    data_offset = None
    first_sample_flags = None

    if flags & TRUN_DATA_OFFSET_PRESENT:
        if off + 4 > len(payload):
            warn(f"trun @ 0x{trun_box.start:X}: data_offset을 읽을 공간이 부족함")
            return None
        data_offset = struct.unpack(">i", payload[off:off + 4])[0]
        off += 4
    if flags & TRUN_FIRST_SAMPLE_FLAGS_PRESENT:
        if off + 4 > len(payload):
            warn(f"trun @ 0x{trun_box.start:X}: first_sample_flags를 읽을 공간이 부족함")
            return None
        first_sample_flags = struct.unpack(">I", payload[off:off + 4])[0]
        off += 4

    per_sample_size = 0
    if flags & TRUN_SAMPLE_DURATION_PRESENT:
        per_sample_size += 4
    if flags & TRUN_SAMPLE_SIZE_PRESENT:
        per_sample_size += 4
    if flags & TRUN_SAMPLE_FLAGS_PRESENT:
        per_sample_size += 4
    if flags & TRUN_SAMPLE_COMPOSITION_TIME_OFFSET_PRESENT:
        per_sample_size += 4

    samples = []
    for i in range(sample_count):
        if off + per_sample_size > len(payload):
            warn(f"trun @ 0x{trun_box.start:X}: sample #{i+1}/{sample_count} 읽는 중 "
                 f"payload 범위를 벗어남 - 이후 sample은 버림")
            break
        duration = None
        size = None
        s_flags = None
        cto = None
        if flags & TRUN_SAMPLE_DURATION_PRESENT:
            duration = struct.unpack(">I", payload[off:off + 4])[0]
            off += 4
        if flags & TRUN_SAMPLE_SIZE_PRESENT:
            size = struct.unpack(">I", payload[off:off + 4])[0]
            off += 4
        if flags & TRUN_SAMPLE_FLAGS_PRESENT:
            s_flags = struct.unpack(">I", payload[off:off + 4])[0]
            off += 4
        if flags & TRUN_SAMPLE_COMPOSITION_TIME_OFFSET_PRESENT:
            if version == 0:
                cto = struct.unpack(">I", payload[off:off + 4])[0]
            else:
                cto = struct.unpack(">i", payload[off:off + 4])[0]
            off += 4
        samples.append(RunSample(duration=duration, size=size, flags=s_flags,
                                  composition_time_offset=cto))

    trun = TrunInfo(version=version, flags=flags, sample_count=sample_count,
                     data_offset=data_offset, first_sample_flags=first_sample_flags,
                     samples=samples)
    debug(f"[TRUN] flags=0x{flags:06X} sample_count={sample_count} "
          f"data_offset={data_offset} first_sample_flags={first_sample_flags}")
    return trun

def resolve_sample_duration(run_sample, tfhd, trex):
    if run_sample.duration is not None:
        return run_sample.duration
    if tfhd is not None and tfhd.default_sample_duration is not None:
        return tfhd.default_sample_duration
    if trex is not None:
        return trex.default_sample_duration
    return None

def resolve_sample_size(run_sample, tfhd, trex):
    if run_sample.size is not None:
        return run_sample.size
    if tfhd is not None and tfhd.default_sample_size is not None:
        return tfhd.default_sample_size
    if trex is not None:
        return trex.default_sample_size
    return None

def resolve_base_data_offset(tfhd, moof_start, is_first_traf_in_moof, prev_traf_data_end):
    if tfhd.base_data_offset is not None:
        return tfhd.base_data_offset, "explicit(tfhd.base_data_offset)"
    if tfhd.default_base_is_moof:
        return moof_start, "default-base-is-moof"
    if is_first_traf_in_moof:
        return moof_start, "implicit(first traf in moof)"
    if prev_traf_data_end is not None:
        return prev_traf_data_end, "implicit(end of previous traf's data)"
    warn("base_data_offset을 결정할 수 없음 (첫 traf도 아니고 이전 traf의 데이터 끝도 모름) "
         "- 안전을 위해 moof 시작 offset으로 대체")
    return moof_start, "fallback(unresolvable)"

def extract_sample(f, offset, size, filesize, mdat_ranges):
    if size is None:
        return None, "sample size를 알 수 없음(trun/tfhd/trex 어디에도 값이 없음)"
    if offset is None or offset < 0:
        return None, f"sample_offset이 유효하지 않음(offset={offset})"
    if offset + size > filesize:
        return None, f"offset(0x{offset:X})+size({size})가 파일 크기({filesize})를 초과함"

    in_mdat = any(m_start <= offset and offset + size <= m_end for m_start, m_end in mdat_ranges)
    if not in_mdat:
        warn(f"sample @ 0x{offset:X} size={size}: 어떤 mdat payload 범위에도 속하지 않음 "
             f"(offset 계산이 잘못됐을 가능성)")

    f.seek(offset)
    raw = f.read(size)
    if len(raw) != size:
        return None, f"read 결과 길이({len(raw)})가 요청 size({size})와 다름 (파일이 잘렸을 수 있음)"
    return raw, None

TEXT_LENGTH_PREFIX_SIZE = 2

GSENSOR_PREFIX_RE = re.compile(
    r"^\$?gsensor(?P<subtype>[A-Za-z0-9]*)\s*,\s*(?P<rest>.*)$", re.IGNORECASE)

VENDOR_DOLLAR_RE = re.compile(r"^\$(?P<tag>[A-Za-z]+)(?P<rest>.*)$", re.DOTALL)

NMEA_TYPES_WITH_POSITION = ("RMC", "GGA")

def decode_sample_text(raw_bytes):
    if len(raw_bytes) >= TEXT_LENGTH_PREFIX_SIZE:
        declared_len = struct.unpack(">H", raw_bytes[:2])[0]
        if declared_len + TEXT_LENGTH_PREFIX_SIZE == len(raw_bytes):
            text_bytes = raw_bytes[2:2 + declared_len]
            try:
                return text_bytes.decode("utf-8"), True
            except UnicodeDecodeError:
                return text_bytes.decode("latin1", errors="replace"), True

    stripped = raw_bytes.rstrip(b"\x00")
    if stripped and all((32 <= b < 127) or b in (9, 10, 13) for b in stripped):
        return stripped.decode("ascii", errors="replace"), False
    return None, False

def nmea_checksum_ok(sentence):
    sentence = sentence.strip().lstrip("$")
    if "*" not in sentence:
        return None
    body, _, csum = sentence.partition("*")
    csum = csum.strip()
    if len(csum) < 2 or not re.match(r"[0-9A-Fa-f]{2}", csum):
        return False
    calc = 0
    for ch in body:
        calc ^= ord(ch)
    return f"{calc:02X}" == csum[:2].upper()

def _dm_to_decimal(value_str, deg_digits, hemisphere, neg_hemi):
    if not value_str or len(value_str) <= deg_digits:
        return None
    allowed = {"N", "S"} if deg_digits == 2 else {"E", "W"}
    if hemisphere not in allowed:
        return None
    try:
        deg = int(value_str[:deg_digits])
        minutes = float(value_str[deg_digits:])
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(minutes) or not (0.0 <= minutes < 60.0):
        return None
    max_deg = 90 if deg_digits == 2 else 180
    if not (0 <= deg <= max_deg) or (deg == max_deg and minutes != 0):
        return None
    decimal = deg + minutes / 60.0
    return -decimal if hemisphere == neg_hemi else decimal

def format_nmea_date(ddmmyy):
    if not ddmmyy or len(ddmmyy) != 6 or not ddmmyy.isdigit():
        return ddmmyy
    dd, mm, yy = int(ddmmyy[:2]), int(ddmmyy[2:4]), int(ddmmyy[4:6])
    year = 1900 + yy if yy >= 80 else 2000 + yy
    try:
        import datetime as _dt
        return _dt.date(year, mm, dd).isoformat()
    except ValueError:
        return ddmmyy

def format_nmea_time(hhmmss):
    if not hhmmss or len(hhmmss) < 6:
        return hhmmss
    try:
        hh = int(hhmmss[:2])
        mm = int(hhmmss[2:4])
        ss = float(hhmmss[4:])
    except (TypeError, ValueError, OverflowError):
        return hhmmss
    if not (0 <= hh <= 23 and 0 <= mm <= 59 and 0 <= ss < 60):
        return hhmmss
    return f"{hh:02d}:{mm:02d}:{hhmmss[4:]}"

def parse_rmc(fields):
    if len(fields) < 10:
        return None
    lat_str, lat_hemi = fields[3].strip(), fields[4].strip().upper()
    lon_str, lon_hemi = fields[5].strip(), fields[6].strip().upper()
    lat = _dm_to_decimal(lat_str, 2, lat_hemi, "S")
    lon = _dm_to_decimal(lon_str, 3, lon_hemi, "W")
    if lat is None or lon is None:
        # 위경도 필드가 "비어 있고" status가 A가 아니면 = 그 순간 GPS fix가 없었던
        # 정상 기록(status=V, mode=N)이므로 좌표만 공란으로 두고 행은 살린다.
        # 필드에 값은 있는데 파싱이 안 되는 경우는 손상으로 보고 기존대로 버린다.
        _status = fields[2].strip().upper() if len(fields) > 2 else ""
        if lat_str or lon_str or _status == "A":
            return None
        lat = lon = None
    warnings = []
    speed_knots = fields[7].strip() if len(fields) > 7 else ""
    speed_kmh = None
    if speed_knots:
        try:
            v = float(speed_knots)
            if math.isfinite(v) and v >= 0:
                speed_kmh = v * 1.852
            else:
                warnings.append("invalid_speed")
        except (ValueError, OverflowError):
            warnings.append("invalid_speed")
    status = fields[2].strip().upper() if len(fields) > 2 else ""
    if status not in {"A", "V", ""}:
        warnings.append("invalid_status")
    return {"lat": lat, "lon": lon, "date": format_nmea_date(fields[9].strip()),
            "utc_time": format_nmea_time(fields[1].strip()), "status": status,
            "status_valid": status == "A", "speed_knots": speed_knots, "speed_kmh": speed_kmh,
            "track_deg": fields[8].strip() if len(fields) > 8 else "",
            "magvar": fields[10].strip() if len(fields) > 10 else "",
            "magvar_dir": fields[11].strip().upper() if len(fields) > 11 else "",
            "mode": fields[12].strip().upper() if len(fields) > 12 else "",
            "parse_warnings": ";".join(warnings)}

def parse_gga(fields):
    if len(fields) < 10:
        return None
    lat = _dm_to_decimal(fields[2].strip(), 2, fields[3].strip().upper(), "S")
    lon = _dm_to_decimal(fields[4].strip(), 3, fields[5].strip().upper(), "W")
    if lat is None or lon is None:
        return None
    quality = fields[6].strip() if len(fields) > 6 else ""
    return {"lat": lat, "lon": lon, "date": "", "utc_time": format_nmea_time(fields[1].strip()),
            "status": quality, "status_valid": quality.isdigit() and int(quality) > 0,
            "speed_knots": "", "speed_kmh": None, "track_deg": "", "magvar": "", "magvar_dir": "",
            "mode": "", "altitude_m": fields[9].strip() if len(fields) > 9 else "",
            "parse_warnings": "" if quality.isdigit() else "invalid_fix_quality"}

NMEA_PARSERS = {"RMC": parse_rmc, "GGA": parse_gga}

def try_parse_nmea(line):
    if not isinstance(line, str):
        return None
    raw = line.strip("\x00\r\n\t ")
    body_with_checksum = raw[1:] if raw.startswith("$") else raw
    checksum_ok = nmea_checksum_ok(body_with_checksum)
    body = body_with_checksum.split("*", 1)[0]
    fields = body.split(",")
    if not fields or len(fields[0]) != 5:
        return None
    talker, sentence_type = fields[0][:2], fields[0][2:].upper()
    if not talker.isalpha() or sentence_type not in NMEA_TYPES_WITH_POSITION:
        return None
    parser = NMEA_PARSERS.get(sentence_type)
    if parser is None:
        return None
    try:
        parsed = parser(fields)
    except (ValueError, TypeError, OverflowError, IndexError) as exc:
        warn(f"NMEA {sentence_type} 파싱 실패: {exc}")
        return None
    if parsed is None:
        return None
    parsed.update({"talker": talker, "sentence_type": sentence_type, "raw": raw,
                    "checksum_ok": checksum_ok})
    parsed["trusted"] = bool(parsed.get("status_valid", True) and checksum_ok is not False
                              and not parsed.get("parse_warnings"))
    return parsed

# 세그먼트 구분자는 벤더마다 다름: ";"(INAVI), "\r\n"(Mercedes) 등을 모두 인정.
SEGMENT_DELIMITER_RE = re.compile(r"[;\r\n]+")

def split_segments(text):
    segments = []
    for chunk in SEGMENT_DELIMITER_RE.split(text):
        chunk = chunk.strip()
        if not chunk:
            continue
        # 일부 벤더(Mercedes)는 구분자 없이 "$"로 시작하는 문장을 그냥 이어붙임
        # (예: "$M1,...$M2,...$V14400$Z55"). "$" 등장 위치를 문장 시작으로 보고 재분리.
        if chunk.count("$") > 1:
            segments.extend(p.strip() for p in re.split(r"(?=\$)", chunk) if p.strip())
        else:
            segments.append(chunk)
    return segments

def classify_segment(segment):
    m = GSENSOR_PREFIX_RE.match(segment)
    if m:
        rest = m.group("rest")
        raw_fields = [x.strip() for x in rest.split(",")] if rest else []
        payload = {"subtype": m.group("subtype"), "raw_fields": raw_fields}
        if len(raw_fields) >= 5:
            try:
                count = int(raw_fields[0])
                scale = int(raw_fields[1])
                x_raw = int(raw_fields[2])
                y_raw = int(raw_fields[3])
                z_raw = int(raw_fields[4])
                payload.update({
                    "count": count, "scale": scale,
                    "x_raw": x_raw, "y_raw": y_raw, "z_raw": z_raw,
                    "x_g": (x_raw / scale) if scale else None,
                    "y_g": (y_raw / scale) if scale else None,
                    "z_g": (z_raw / scale) if scale else None,
                })
            except (ValueError, ZeroDivisionError):
                pass
        return "gsensor", payload

    nmea = try_parse_nmea(segment)
    if nmea is not None:
        return "gps_nmea", nmea

    # 의미가 아직 확인되지 않은 벤더 고유 "$TAG,..." 형식(예: Mercedes의 $M/$V/$Z).
    # 필드를 해석하지 않고 원본 그대로 "미확정" 상태로 보존한다.
    vm = VENDOR_DOLLAR_RE.match(segment)
    if vm:
        rest = vm.group("rest")
        raw_fields = [x.strip() for x in rest.split(",")] if rest else []
        return "vendor_raw", {"tag": vm.group("tag"), "raw_fields": raw_fields,
                               "raw": segment, "confirmed": False}

    parts = segment.split(",")
    label = parts[0] if parts else segment
    return "generic", {"label": label, "fields": parts[1:], "raw": segment}

def parse_atext_metadata(raw_bytes):
    text, used_length_prefix = decode_sample_text(raw_bytes)
    if text is None:
        return [], used_length_prefix
    results = []
    for segment in split_segments(text):
        kind, payload = classify_segment(segment)
        if kind in KEPT_KINDS:
            results.append((kind, payload))
    return results, used_length_prefix

@dataclass
class SampleMetadata:
    track_id: int
    moof_index: int
    traf_index: int
    trun_index: int
    sample_index: int

    file_offset: int
    size: int

    dts: int
    duration: Optional[int]
    composition_time_offset: Optional[int]

    start_time: Optional[float]
    end_time: Optional[float]

    raw_data: bytes = b""
    parsed_segments: list = field(default_factory=list)
    error: Optional[str] = None

def parse_traf(f, traf_box, moof_start, moof_index, traf_index, text_track_id,
               timescale, trex_defaults, filesize, mdat_ranges,
               is_first_traf_in_moof, prev_traf_data_end, track_dts_state,
               out_samples):
    traf_children = list(iter_child_boxes(f, traf_box.payload_start, traf_box.end,
                                           context=f"traf@0x{traf_box.start:X}"))
    tfhd_box = find_box(traf_children, b"tfhd")
    if tfhd_box is None:
        warn(f"traf @ 0x{traf_box.start:X}: tfhd가 없음 - 이 traf는 건너뜀")
        return prev_traf_data_end

    tfhd = parse_tfhd(f, tfhd_box)
    if tfhd is None:
        return prev_traf_data_end

    track_id = tfhd.track_id
    trex = trex_defaults.get(track_id)
    is_target_track = (track_id == text_track_id)

    tfdt_box = find_box(traf_children, b"tfdt")
    if tfdt_box is not None:
        base_decode_time = parse_tfdt(f, tfdt_box)
        if base_decode_time is None:
            base_decode_time = track_dts_state.get(track_id, 0)
    else:
        base_decode_time = track_dts_state.get(track_id, 0)
        if is_target_track:
            warn(f"traf @ 0x{traf_box.start:X} (track_ID={track_id}): tfdt가 없음 - "
                 f"직전까지 누적된 시간({base_decode_time})으로 이어붙임")

    if tfhd.duration_is_empty:
        track_dts_state[track_id] = base_decode_time
        return prev_traf_data_end

    base_data_offset, base_reason = resolve_base_data_offset(
        tfhd, moof_start, is_first_traf_in_moof, prev_traf_data_end)
    debug(f"  traf track_ID={track_id} base_data_offset=0x{base_data_offset:X} ({base_reason})")

    current_dts = base_decode_time
    current_offset = None
    traf_data_end = base_data_offset

    trun_boxes = find_all(traf_children, b"trun")
    for trun_index, trun_box in enumerate(trun_boxes):
        trun = parse_trun(f, trun_box)
        if trun is None:
            break

        if trun.flags & TRUN_DATA_OFFSET_PRESENT:
            current_offset = base_data_offset + trun.data_offset
        elif current_offset is None:
            current_offset = base_data_offset

        for sample_number, run_sample in enumerate(trun.samples, start=1):
            duration = resolve_sample_duration(run_sample, tfhd, trex)
            size = resolve_sample_size(run_sample, tfhd, trex)

            sample_offset = current_offset
            start_time = current_dts / timescale if (is_target_track and timescale) else None
            end_time = None
            if is_target_track and timescale and duration is not None:
                end_time = (current_dts + duration) / timescale

            if is_target_track:
                sm = SampleMetadata(
                    track_id=track_id, moof_index=moof_index, traf_index=traf_index,
                    trun_index=trun_index, sample_index=len(out_samples) + 1,
                    file_offset=sample_offset if sample_offset is not None else -1,
                    size=size if size is not None else 0,
                    dts=current_dts, duration=duration,
                    composition_time_offset=run_sample.composition_time_offset,
                    start_time=start_time, end_time=end_time,
                )
                if size is None:
                    sm.error = "sample size를 어디에서도 구하지 못함(trun/tfhd/trex 전부 없음)"
                    warn(f"moof#{moof_index} traf#{traf_index} sample#{sample_number}: {sm.error} "
                         f"- 이후 sample offset도 알 수 없어 이 trun 처리를 중단함")
                    out_samples.append(sm)
                    break
                if duration is None:
                    warn(f"moof#{moof_index} traf#{traf_index} sample#{sample_number}: "
                         f"duration unavailable (trun/tfhd/trex 전부 없음)")
                raw, err = extract_sample(f, sample_offset, size, filesize, mdat_ranges)
                if err is not None:
                    sm.error = err
                    warn(f"moof#{moof_index} traf#{traf_index} sample#{sample_number} "
                         f"@0x{sample_offset:X}: {err}")
                else:
                    sm.raw_data = raw
                    segments, _ = parse_atext_metadata(raw)
                    sm.parsed_segments = segments
                out_samples.append(sm)
            else:
                if size is None:
                    warn(f"traf(track_ID={track_id}) sample#{sample_number}: size를 구하지 못해 "
                         f"offset 추적 중단 (base_data_offset 연쇄 계산에 영향 가능)")
                    break

            if size is not None:
                current_offset += size
                traf_data_end = current_offset
            if duration is not None:
                current_dts += duration

    track_dts_state[track_id] = current_dts
    return traf_data_end

def parse_moof(f, moof_box, moof_index, text_track_id, timescale, trex_defaults,
               filesize, mdat_ranges, track_dts_state, out_samples):
    debug(f"[MOOF] offset=0x{moof_box.start:X}")
    moof_children = list(iter_child_boxes(f, moof_box.payload_start, moof_box.end,
                                           context=f"moof@0x{moof_box.start:X}"))
    traf_boxes = find_all(moof_children, b"traf")
    prev_traf_data_end = None
    for traf_index, traf_box in enumerate(traf_boxes):
        prev_traf_data_end = parse_traf(
            f, traf_box, moof_box.start, moof_index, traf_index, text_track_id,
            timescale, trex_defaults, filesize, mdat_ranges,
            is_first_traf_in_moof=(traf_index == 0),
            prev_traf_data_end=prev_traf_data_end,
            track_dts_state=track_dts_state, out_samples=out_samples)

def scan_top_level(f, filesize):
    top_boxes = []
    for box in iter_child_boxes(f, 0, filesize, context="top-level", allow_size_zero=True):
        top_boxes.append(box)
    return top_boxes

def format_gsensor(payload):
    lines = [f"    [GSENSOR] subtype={payload.get('subtype') or '-'}"]
    if "x_g" in payload and payload.get("scale"):
        lines.append(f"      raw(x,y,z) = ({payload['x_raw']}, {payload['y_raw']}, {payload['z_raw']}) "
                      f"/ scale={payload['scale']}")
        lines.append(f"      g(x,y,z)   = ({payload['x_g']:.4f}, {payload['y_g']:.4f}, {payload['z_g']:.4f})")
    else:
        lines.append(f"      fields = {payload.get('raw_fields')}")
    return "\n".join(lines)

def format_gps(payload):
    speed_kmh = payload.get("speed_kmh")
    lat_disp = f"{payload['lat']:.6f}" if payload.get("lat") is not None else "(fix 없음)"
    lon_disp = f"{payload['lon']:.6f}" if payload.get("lon") is not None else "(fix 없음)"
    return (f"    [GPS {payload['sentence_type']}] "
            f"lat={lat_disp} lon={lon_disp} "
            f"date={payload.get('date','')} time={payload.get('utc_time','')} "
            f"speed_kmh={f'{speed_kmh:.3f}' if speed_kmh is not None else '-'} "
            f"checksum_ok={payload.get('checksum_ok')} trusted={payload.get('trusted')}\n"
            f"      raw: {payload['raw']}")

def format_vendor_raw(payload):
    return (f"    [VENDOR_RAW ${payload['tag']}] (미확정 - 필드 의미 미해석) "
            f"fields={payload.get('raw_fields')}\n"
            f"      raw: {payload['raw']}")

def print_sample(sample: SampleMetadata):
    info("=" * 50)
    info(f"Track ID      : {sample.track_id}")
    info(f"Sample        : {sample.sample_index}")
    info(f"Moof/Traf/Run : #{sample.moof_index} / #{sample.traf_index} / #{sample.trun_index}")
    info("")
    info(f"File Offset   : 0x{sample.file_offset:08X}" if sample.file_offset >= 0
         else "File Offset   : (알 수 없음)")
    info(f"Sample Size   : {sample.size} bytes")
    info("")
    info(f"DTS           : {sample.dts}")
    info(f"Duration      : {sample.duration if sample.duration is not None else 'unavailable'}")
    info("")
    info(f"Start Time    : {sample.start_time:.3f} sec" if sample.start_time is not None
         else "Start Time    : (알 수 없음)")
    info(f"End Time      : {sample.end_time:.3f} sec" if sample.end_time is not None
         else "End Time      : (알 수 없음)")
    info("=" * 50)

    if sample.error:
        info(f"  [ERROR] {sample.error}")
        return

    if not sample.parsed_segments:
        info("  (GPRMC/GPGGA/GSENSOR 형식의 데이터 없음)")
        return

    for kind, payload in sample.parsed_segments:
        if kind == "gsensor":
            info(format_gsensor(payload))
        elif kind == "gps_nmea":
            info(format_gps(payload))
        elif kind == "vendor_raw":
            info(format_vendor_raw(payload))

# gsensor 자가 보정: "1g에 해당하는 카운트"를 데이터 자체에서 추정한다.
#
# 레코드의 <scale> 필드를 "1g당 카운트"로 보는 기존 x_g/y_g/z_g는 기기마다 결과가
# 제각각이라 절대값을 믿을 수 없다는 게 실측으로 확인됐다(자세한 근거는 architect.md
# "알려진 한계" 참고).
#
#     기기                    scale   |v|/scale
#     INAVI Z300               512     0.266g
#     INAVI QXD8000           2048     0.248g
#     Ambarella(avc1/fMP4)     512     1.99g     <- count를 곱하는 보정도 여기서 반증됨
#
# 대신 물리를 쓴다. 차에 고정된 센서는 중력 1g를 항상 받고, 주행 가감속은 급브레이크도
# 0.3g 수준에 방향이 계속 바뀐다. 그래서 |(x,y,z)| 크기의 중앙값을 1g로 보면 기기
# 스펙을 몰라도 감도를 역산할 수 있다. 실제로 같은 기기의 다른 파일 3개에서 1023 /
# 1015 / 1019 로 거의 같은 값이 나와, 노이즈가 아니라 하드웨어 상수를 짚는다는 게
# 확인됐다.
#
# 기존 x_g/y_g/z_g는 건드리지 않고 x_g_cal/y_g_cal/z_g_cal 과 기준값
# calibration_counts_per_g 를 추가만 한다.
#
# 한계: 크기만 보정하고 장착 각도는 보정하지 않는다(중력이 여러 축에 갈린 채로 남는다).
# 영상 전체가 급가속 구간이면 중앙값 기준이 밀릴 수 있다. 축별 영점 오프셋도 안 본다.
MIN_CALIBRATION_SAMPLES = 30


def apply_gsensor_calibration(sensor_rows):
    """sensor_rows에 x_g_cal/y_g_cal/z_g_cal/calibration_counts_per_g를 채운다.
    반환값은 추정한 '1g당 카운트'(표본이 부족하면 None)."""
    mags = []
    for r in sensor_rows:
        try:
            mags.append(math.sqrt(int(r["x_raw"]) ** 2 + int(r["y_raw"]) ** 2
                                   + int(r["z_raw"]) ** 2))
        except (TypeError, ValueError, KeyError):
            continue

    counts_per_g = None
    if len(mags) >= MIN_CALIBRATION_SAMPLES:
        mags.sort()
        n = len(mags)
        median = mags[n // 2] if n % 2 else (mags[n // 2 - 1] + mags[n // 2]) / 2.0
        if median > 0:
            counts_per_g = median

    for r in sensor_rows:
        r["calibration_counts_per_g"] = f"{counts_per_g:.1f}" if counts_per_g else ""
        for axis in ("x", "y", "z"):
            value = None
            if counts_per_g:
                try:
                    value = int(r[f"{axis}_raw"]) / counts_per_g
                except (TypeError, ValueError, KeyError):
                    value = None
            r[f"{axis}_g_cal"] = value

    if counts_per_g is None and sensor_rows:
        warn(f"gsensor 자가 보정 생략 - 해석 가능한 레코드가 {len(mags)}개로 "
             f"{MIN_CALIBRATION_SAMPLES}개 미만이라 중앙값 기준을 신뢰할 수 없음 "
             f"(x_g_cal 계열은 공란으로 둠)")
    return counts_per_g

def write_csv(path, rows):
    if not rows:
        return
    fieldnames = []
    seen = set()
    for row in rows:
        for k in row.keys():
            if k not in seen:
                seen.add(k)
                fieldnames.append(k)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)

def save_track_table(out_dir, all_tracks, target_track_id, sample_count):
    rows = []
    for t in all_tracks:
        is_target = (t["track_id"] == target_track_id)
        rows.append({
            "track": t["track_id"],
            "handler_type": t["handler_type"].decode("ascii", errors="replace") if t["handler_type"] else "",
            "handler_name": t["handler_name"],
            "stsd_types": ";".join(t["stsd_types"]),
            "sample_count": sample_count if is_target else 0,
            "is_text_track": is_target,
        })
    write_csv(os.path.join(out_dir, "track_table.csv"), rows)

def save_outputs(out_dir, samples, all_tracks, target_track_id, dry_run=False):
    if dry_run:
        return
    os.makedirs(out_dir, exist_ok=True)
    track_dir = os.path.join(out_dir, f"TRACK{target_track_id}_TEXT")
    os.makedirs(track_dir, exist_ok=True)

    index_rows = []
    coord_rows = []
    sensor_rows = []
    vendor_raw_rows = []
    timeline_rows = []
    last_gps = {}
    for s in samples:
        validation = "OK"
        if s.error:
            validation = "OUT_OF_RANGE" if "파일 크기" in s.error or "유효하지" in s.error else "ERROR"
        elif not s.parsed_segments:
            validation = "OK_NO_GPRMC_OR_GSENSOR"

        index_rows.append({
            "track": s.track_id, "sample": s.sample_index,
            "moof_index": s.moof_index, "traf_index": s.traf_index, "trun_index": s.trun_index,
            "absolute_offset": f"0x{s.file_offset:08X}" if s.file_offset >= 0 else "",
            "size": s.size, "dts": s.dts, "duration": s.duration,
            "start_time_sec": f"{s.start_time:.3f}" if s.start_time is not None else "",
            "end_time_sec": f"{s.end_time:.3f}" if s.end_time is not None else "",
            "validation": validation, "output_file": "",
        })
        gps_payload = None
        gsensor_payload = None
        for kind, payload in s.parsed_segments:
            if kind == "gps_nmea":
                gps_payload = payload
                speed_kmh = payload.get("speed_kmh")
                coord_rows.append({
                    "sample": s.sample_index,
                    "start_time_sec": f"{s.start_time:.3f}" if s.start_time is not None else "",
                    "end_time_sec": f"{s.end_time:.3f}" if s.end_time is not None else "",
                    "time_source": "tfdt_trun" if s.start_time is not None else "",
                    "date": payload.get("date", ""), "utc_time": payload.get("utc_time", ""),
                    "status": payload.get("status", ""),
                    "latitude": f"{payload['lat']:.6f}" if payload.get("lat") is not None else "",
                    "longitude": f"{payload['lon']:.6f}" if payload.get("lon") is not None else "",
                    "speed_knots": payload.get("speed_knots", ""),
                    "speed_kmh": f"{speed_kmh:.3f}" if speed_kmh is not None else "",
                    "track_deg": payload.get("track_deg", ""),
                    "magvar": payload.get("magvar", ""),
                    "magvar_dir": payload.get("magvar_dir", ""),
                    "mode": payload.get("mode", ""),
                    "sentence_type": payload["sentence_type"],
                    "checksum_ok": payload.get("checksum_ok"),
                    "status_valid": payload.get("status_valid", ""),
                    "trusted": payload.get("trusted"),
                    "parse_warnings": payload.get("parse_warnings", ""),
                    "raw_sentence": payload["raw"],
                })
            elif kind == "gsensor":
                gsensor_payload = payload
                sensor_rows.append({
                    "sample": s.sample_index,
                    "start_time_sec": f"{s.start_time:.3f}" if s.start_time is not None else "",
                    "end_time_sec": f"{s.end_time:.3f}" if s.end_time is not None else "",
                    "time_source": "tfdt_trun" if s.start_time is not None else "",
                    "absolute_offset": f"0x{s.file_offset:08X}" if s.file_offset >= 0 else "",
                    "subtype": payload.get("subtype", ""),
                    "count": payload.get("count"),
                    "x_raw": payload.get("x_raw"), "y_raw": payload.get("y_raw"), "z_raw": payload.get("z_raw"),
                    "scale": payload.get("scale"),
                    "x_g": payload.get("x_g"), "y_g": payload.get("y_g"), "z_g": payload.get("z_g"),
                })
            elif kind == "vendor_raw":
                vendor_raw_rows.append({
                    "sample": s.sample_index,
                    "start_time_sec": f"{s.start_time:.3f}" if s.start_time is not None else "",
                    "end_time_sec": f"{s.end_time:.3f}" if s.end_time is not None else "",
                    "absolute_offset": f"0x{s.file_offset:08X}" if s.file_offset >= 0 else "",
                    "tag": payload.get("tag", ""),
                    "raw_fields": "|".join(payload.get("raw_fields", [])),
                    "raw": payload.get("raw", ""),
                    "note": "unconfirmed_field_meaning",
                })

        # fix가 없던 sample(status=V, 좌표 없음)은 "가장 최근 GPS 값"을 갱신하지 않는다 -
        # 갱신해버리면 *_last 컬럼이 공란으로 덮여 직전 위치를 잃는다.
        if gps_payload is not None and gps_payload.get("lat") is not None:
            last_gps = {
                "latitude": f"{gps_payload['lat']:.6f}", "longitude": f"{gps_payload['lon']:.6f}",
                "speed_kmh": f"{gps_payload.get('speed_kmh'):.3f}" if gps_payload.get("speed_kmh") is not None else "",
                "track_deg": gps_payload.get("track_deg", ""),
                "gps_date": gps_payload.get("date", ""), "gps_utc_time": gps_payload.get("utc_time", ""),
            }

        timeline_rows.append({
            "sample": s.sample_index,
            "start_time_sec": f"{s.start_time:.3f}" if s.start_time is not None else "",
            "end_time_sec": f"{s.end_time:.3f}" if s.end_time is not None else "",
            "time_source": "tfdt_trun" if s.start_time is not None else "",
            "latitude": (f"{gps_payload['lat']:.6f}"
                         if gps_payload and gps_payload.get("lat") is not None else ""),
            "longitude": (f"{gps_payload['lon']:.6f}"
                          if gps_payload and gps_payload.get("lon") is not None else ""),
            "speed_kmh": (f"{gps_payload.get('speed_kmh'):.3f}"
                          if gps_payload and gps_payload.get("speed_kmh") is not None else ""),
            "track_deg": gps_payload.get("track_deg", "") if gps_payload else "",
            "gps_date": gps_payload.get("date", "") if gps_payload else "",
            "gps_utc_time": gps_payload.get("utc_time", "") if gps_payload else "",
            "gps_checksum_ok": gps_payload.get("checksum_ok") if gps_payload else "",
            "latitude_last": last_gps.get("latitude", ""),
            "longitude_last": last_gps.get("longitude", ""),
            "speed_kmh_last": last_gps.get("speed_kmh", ""),
            "x_g": gsensor_payload.get("x_g") if gsensor_payload else "",
            "y_g": gsensor_payload.get("y_g") if gsensor_payload else "",
            "z_g": gsensor_payload.get("z_g") if gsensor_payload else "",
            # x_g_cal 계열은 센서 보정이 끝난 뒤 아래에서 채운다(2-pass).
            "x_g_cal": "", "y_g_cal": "", "z_g_cal": "",
        })

    write_csv(os.path.join(track_dir, "index.csv"), index_rows)
    write_csv(os.path.join(track_dir, "coordinates.csv"), coord_rows)
    apply_gsensor_calibration(sensor_rows)
    write_csv(os.path.join(track_dir, "sensor_values.csv"), sensor_rows)
    write_csv(os.path.join(track_dir, "vendor_raw.csv"), vendor_raw_rows)
    # 센서 보정이 끝난 뒤라야 x_g_cal 계열이 채워져 있으므로, sample 번호로 되짚어
    # timeline에 옮겨 담는다(다른 경로의 timeline.csv와 컬럼 구성을 맞추기 위함).
    cal_by_sample = {r["sample"]: r for r in sensor_rows}
    for row in timeline_rows:
        sen = cal_by_sample.get(row["sample"])
        if sen:
            row["x_g_cal"] = sen.get("x_g_cal", "")
            row["y_g_cal"] = sen.get("y_g_cal", "")
            row["z_g_cal"] = sen.get("z_g_cal", "")
    write_csv(os.path.join(track_dir, "timeline.csv"), timeline_rows)
    with open(os.path.join(track_dir, "coordinates.txt"), "w", encoding="utf-8") as f:
        # coordinates.txt는 "좌표 목록"이라 fix가 없어 좌표가 빈 행은 제외한다
        # (그 행도 coordinates.csv에는 status=V로 그대로 남는다).
        for i, row in enumerate([r for r in coord_rows if r["latitude"]], start=1):
            f.write(f"{i}. {row['latitude']}, {row['longitude']}\n")

    save_track_table(out_dir, all_tracks, target_track_id, len(samples))

    with open(os.path.join(out_dir, "warnings.log"), "w", encoding="utf-8") as f:
        for w_msg in WARNINGS:
            f.write(w_msg + "\n")



# ---- 여기부터: GPS_metadata_mp4_pvc1_Atext.py (루트 B, non-fragmented sample table) ----


# pvc1 원본이 쓰던 이름을 공통 Box 순회 함수에 그대로 연결한다(동작 동일).

iter_boxes = iter_child_boxes


MAX_TABLE_ENTRIES = 1_000_000

SUPPORTED_TEXT_HANDLERS = {b"text", b"sbtl", b"subt"}

KEYWORD_CANDIDATES = [
    "gps", "GPS", "gsensor", "G-sensor", "NMEA", "GPRMC", "GPGGA",
    "latitude", "longitude", "speed",
]

@dataclass
class SampleDescEntry:
    index: int
    entry_type: bytes
    size: int
    box_offset: int

def parse_stsd(f, stsd_box):
    f.seek(stsd_box.payload_start)
    header = f.read(8)
    if len(header) < 8:
        warn(f"stsd @ 0x{stsd_box.start:X}: 헤더 읽기 실패")
        return []
    entry_count = struct.unpack(">I", header[4:8])[0]

    entries = []
    pos = stsd_box.payload_start + 8
    end = stsd_box.end
    for i in range(entry_count):
        if pos + 8 > end:
            warn(f"stsd @ 0x{stsd_box.start:X}: entry_count={entry_count}인데 "
                 f"entry #{i+1} 위치(0x{pos:X})가 stsd 경계(0x{end:X})를 넘어감")
            break
        f.seek(pos)
        eh = f.read(8)
        entry_size = struct.unpack(">I", eh[0:4])[0]
        entry_type = eh[4:8]
        if entry_size < 8 or pos + entry_size > end:
            warn(f"stsd entry #{i+1} @ 0x{pos:X}: size({entry_size}) 이상함 - 순회 중단")
            break
        entries.append(SampleDescEntry(index=i+1, entry_type=entry_type,
                                        size=entry_size, box_offset=pos))
        pos += entry_size

    if len(entries) != entry_count:
        warn(f"stsd @ 0x{stsd_box.start:X}: 선언된 entry_count={entry_count}, "
             f"실제 파싱된 entry={len(entries)}")
    return entries

@dataclass
class StscEntry:
    first_chunk: int
    samples_per_chunk: int
    sample_description_index: int

def parse_stsc(f, stsc_box):
    payload_len = stsc_box.size - stsc_box.header_size
    if payload_len < 8:
        warn(f"stsc @ 0x{stsc_box.start:X}: payload가 8바이트보다 짧음")
        return []
    f.seek(stsc_box.payload_start)
    header = f.read(8)
    if len(header) < 8:
        warn(f"stsc @ 0x{stsc_box.start:X}: 헤더 읽기 실패")
        return []
    declared = struct.unpack(">I", header[4:8])[0]
    max_by_box = max(0, (stsc_box.end - (stsc_box.payload_start + 8)) // 12)
    entry_count = min(declared, max_by_box, MAX_TABLE_ENTRIES)
    if declared != entry_count:
        warn(f"stsc @ 0x{stsc_box.start:X}: entry_count={declared}를 box 경계/안전 한도에 따라 {entry_count}로 제한")
    payload = f.read(entry_count * 12)
    entries = []
    prev_first_chunk = 0
    for i in range(entry_count):
        chunk = payload[i*12:(i+1)*12]
        if len(chunk) < 12:
            warn(f"stsc @ 0x{stsc_box.start:X}: entry #{i+1} 데이터 부족")
            break
        first_chunk, spc, sdi = struct.unpack(">III", chunk)
        if first_chunk == 0 or spc == 0 or sdi == 0:
            warn(f"stsc entry #{i+1}: 0 값(first_chunk={first_chunk}, samples_per_chunk={spc}, sdi={sdi}) - 무효 entry 건너뜀")
            continue
        if first_chunk <= prev_first_chunk:
            warn(f"stsc entry #{i+1}: first_chunk({first_chunk})가 이전 값({prev_first_chunk})보다 크지 않음 - 무효 entry 건너뜀")
            continue
        prev_first_chunk = first_chunk
        entries.append(StscEntry(first_chunk, spc, sdi))
    return entries

def parse_stsz(f, stsz_box):
    payload_len = stsz_box.size - stsz_box.header_size
    if payload_len < 12:
        warn(f"stsz @ 0x{stsz_box.start:X}: payload가 12바이트보다 짧음")
        return []
    f.seek(stsz_box.payload_start)
    header = f.read(12)
    if len(header) < 12:
        warn(f"stsz @ 0x{stsz_box.start:X}: 헤더 읽기 실패")
        return []
    sample_size = struct.unpack(">I", header[4:8])[0]
    declared = struct.unpack(">I", header[8:12])[0]
    if declared > MAX_TABLE_ENTRIES:
        warn(f"stsz @ 0x{stsz_box.start:X}: sample_count={declared}가 안전 한도({MAX_TABLE_ENTRIES}) 초과 - Track 파싱 중단")
        return []
    if sample_size != 0:
        return [sample_size] * declared
    max_by_box = max(0, (stsz_box.end - (stsz_box.payload_start + 12)) // 4)
    sample_count = min(declared, max_by_box)
    if declared != sample_count:
        warn(f"stsz @ 0x{stsz_box.start:X}: sample_count={declared}, box 내부 실제 가능한 entry={sample_count}로 제한")
    raw = f.read(sample_count * 4)
    n = len(raw) // 4
    return list(struct.unpack(f">{n}I", raw[:n*4])) if n else []

def parse_chunk_offsets(f, stbl_children):
    def _parse(box, width, kind):
        payload_len = box.size - box.header_size
        if payload_len < 8:
            warn(f"{kind} @ 0x{box.start:X}: payload가 8바이트보다 짧음")
            return []
        f.seek(box.payload_start)
        header = f.read(8)
        if len(header) < 8:
            warn(f"{kind} @ 0x{box.start:X}: 헤더 읽기 실패")
            return []
        declared = struct.unpack(">I", header[4:8])[0]
        max_by_box = max(0, (box.end - (box.payload_start + 8)) // width)
        count = min(declared, max_by_box, MAX_TABLE_ENTRIES)
        if declared != count:
            warn(f"{kind} @ 0x{box.start:X}: entry_count={declared}를 box 경계/안전 한도에 따라 {count}로 제한")
        raw = f.read(count * width)
        n = len(raw) // width
        fmt = "Q" if width == 8 else "I"
        return list(struct.unpack(f">{n}{fmt}", raw[:n*width])) if n else []

    stco = find_box(stbl_children, b"stco")
    co64 = find_box(stbl_children, b"co64")
    if co64 is not None:
        return _parse(co64, 8, "co64"), "co64"
    if stco is not None:
        return _parse(stco, 4, "stco"), "stco"
    warn("stco/co64 둘 다 없음 - Chunk offset을 알 수 없음")
    return [], None

@dataclass
class SampleInfo:
    sample_number: int
    chunk_number: int
    sample_description_index: int
    absolute_offset: int
    size: int

def compute_sample_positions(stsc_entries, chunk_offsets, sample_sizes):
    num_chunks = len(chunk_offsets)
    if not stsc_entries:
        warn("stsc entry가 없어 Sample 위치를 계산할 수 없음")
        return []

    samples = []
    sample_idx = 0
    stsc_ptr = 0

    for chunk_number in range(1, num_chunks + 1):
        while (stsc_ptr + 1 < len(stsc_entries)
               and stsc_entries[stsc_ptr + 1].first_chunk <= chunk_number):
            stsc_ptr += 1
        rule = stsc_entries[stsc_ptr]
        if rule.first_chunk > chunk_number:
            warn(f"chunk #{chunk_number}: 적용 가능한 stsc 규칙이 없음(첫 규칙의 "
                 f"first_chunk={rule.first_chunk}) - 이 chunk는 건너뜀")
            continue

        samples_per_chunk = rule.samples_per_chunk
        sdi = rule.sample_description_index
        offset = chunk_offsets[chunk_number - 1]

        for _ in range(samples_per_chunk):
            if sample_idx >= len(sample_sizes):
                warn(f"chunk #{chunk_number}에서 stsz Sample이 모자람 "
                     f"(stsz sample_count={len(sample_sizes)}) - 계산 중단")
                return samples
            size = sample_sizes[sample_idx]
            samples.append(SampleInfo(
                sample_number=sample_idx + 1,
                chunk_number=chunk_number,
                sample_description_index=sdi,
                absolute_offset=offset,
                size=size,
            ))
            offset += size
            sample_idx += 1

    if sample_idx != len(sample_sizes):
        warn(f"계산된 Sample 개수({sample_idx}) != stsz sample_count"
             f"({len(sample_sizes)}) - stsc/stsz/stco 불일치 가능성")

    return samples

def find_mdia(f, trak_box):
    trak_children = list(iter_boxes(f, trak_box.payload_start, trak_box.end,
                                     context=f"trak@0x{trak_box.start:X}"))
    return find_box(trak_children, b"mdia"), trak_children

def find_minf(f, mdia_box):
    mdia_children = list(iter_boxes(f, mdia_box.payload_start, mdia_box.end,
                                     context=f"mdia@0x{mdia_box.start:X}"))
    return find_box(mdia_children, b"minf"), mdia_children

def find_stbl(f, minf_box):
    minf_children = list(iter_boxes(f, minf_box.payload_start, minf_box.end,
                                     context=f"minf@0x{minf_box.start:X}"))
    return find_box(minf_children, b"stbl"), minf_children

@dataclass
class TrackInfo:
    track_number: int
    trak_box: Box
    handler_type: bytes = None
    handler_name: str = ""
    stsd_entries: list = field(default_factory=list)
    samples: list = field(default_factory=list)
    is_text_track: bool = False
    timescale: int = None
    sample_times: list = field(default_factory=list)

# ---------------------------------------------------------------------------
# 재생 시간축 (stts)
#
# 영상 재생에 맞춰 GPS/센서를 시각화하려면 "이 레코드가 영상 몇 초 지점인가"가
# 있어야 한다. non-fragmented MP4는 그 정보가 stbl/stts(Decoding Time to Sample)에
# 이미 들어 있다. sample마다 delta(ticks)가 있고 mdhd.timescale로 나누면 초가 된다.
#
# 실측(INAVI Z300): mdhd.timescale=1000, stts=[(200, 100)]
#   -> sample 200개 x 100/1000초 = 0.100초 간격, 총 20.000초
#
# 외부 도구(ffprobe 등)로 총 길이만 받아 "1초씩 증가"로 채우는 방식은 쓰지 않는다.
# GPS는 1Hz라도 gsensor는 10Hz(INAVI)/30Hz(신규 Ambarella)라 균일 가정이 깨지고,
# 실제로 Mercedes 샘플은 GPS 시각이 18건 중복돼 순번=경과초가 최대 1초 어긋난다.
# ---------------------------------------------------------------------------
def parse_mdhd_timescale(f, mdia_children):
    """mdia/mdhd에서 timescale(초당 tick 수)을 읽는다."""
    mdhd_box = find_box(mdia_children, b"mdhd")
    if mdhd_box is None:
        return None
    f.seek(mdhd_box.payload_start)
    payload = f.read(mdhd_box.size - mdhd_box.header_size)
    if len(payload) < 1:
        return None
    version = payload[0]
    if version == 0 and len(payload) >= 16:
        timescale = struct.unpack(">I", payload[12:16])[0]
    elif version == 1 and len(payload) >= 24:
        timescale = struct.unpack(">I", payload[20:24])[0]
    else:
        warn(f"mdhd @ 0x{mdhd_box.start:X}: version={version} 또는 길이가 예상과 다름")
        return None
    if timescale == 0:
        warn(f"mdhd @ 0x{mdhd_box.start:X}: timescale==0 - 시간축 계산 불가")
        return None
    return timescale


def parse_stts(f, stts_box):
    """stts를 (sample_count, sample_delta) 목록으로 읽는다."""
    payload_len = stts_box.size - stts_box.header_size
    if payload_len < 8:
        warn(f"stts @ 0x{stts_box.start:X}: payload가 8바이트보다 짧음")
        return []
    f.seek(stts_box.payload_start)
    header = f.read(8)
    declared = struct.unpack(">I", header[4:8])[0]
    max_by_box = max(0, (stts_box.end - (stts_box.payload_start + 8)) // 8)
    entry_count = min(declared, max_by_box, MAX_TABLE_ENTRIES)
    if declared != entry_count:
        warn(f"stts @ 0x{stts_box.start:X}: entry_count={declared}를 box 경계/안전 "
             f"한도에 따라 {entry_count}로 제한")
    payload = f.read(entry_count * 8)
    entries = []
    for i in range(entry_count):
        chunk = payload[i * 8:(i + 1) * 8]
        if len(chunk) < 8:
            warn(f"stts @ 0x{stts_box.start:X}: entry #{i+1} 데이터 부족")
            break
        count, delta = struct.unpack(">II", chunk)
        if count == 0:
            continue
        entries.append((count, delta))
    return entries


def build_sample_times(stts_entries, timescale, sample_count):
    """stts를 펼쳐서 sample별 (start_sec, end_sec)를 만든다.
    stts가 sample_count를 못 채우면 마지막 delta로 이어 붙이고 경고를 남긴다."""
    if not stts_entries or not timescale:
        return []
    times = []
    ticks = 0
    for count, delta in stts_entries:
        for _ in range(count):
            if len(times) >= sample_count:
                break
            times.append((ticks / timescale, (ticks + delta) / timescale))
            ticks += delta
        if len(times) >= sample_count:
            break
    if len(times) < sample_count:
        last_delta = stts_entries[-1][1]
        warn(f"stts가 다루는 sample 수({len(times)})가 실제 sample 수({sample_count})보다 "
             f"적음 - 나머지는 마지막 delta({last_delta})로 이어붙여 추정함")
        while len(times) < sample_count:
            times.append((ticks / timescale, (ticks + last_delta) / timescale))
            ticks += last_delta
    return times


def parse_track(f, trak_box, track_number):
    ti = TrackInfo(track_number=track_number, trak_box=trak_box)

    mdia_box, _trak_children = find_mdia(f, trak_box)
    if mdia_box is None:
        warn(f"Track #{track_number}: mdia를 찾지 못함")
        return ti

    minf_box, mdia_children = find_minf(f, mdia_box)
    hdlr_box = find_box(mdia_children, b"hdlr")
    if hdlr_box is None:
        warn(f"Track #{track_number}: hdlr을 찾지 못함")
        return ti

    handler_type, handler_name = parse_hdlr(f, hdlr_box)
    ti.handler_type = handler_type
    ti.handler_name = handler_name

    if handler_type not in SUPPORTED_TEXT_HANDLERS:
        return ti

    ti.is_text_track = True

    if minf_box is None:
        warn(f"Track #{track_number}(text): minf를 찾지 못함")
        return ti

    stbl_box, _minf_children = find_stbl(f, minf_box)
    if stbl_box is None:
        warn(f"Track #{track_number}(text): stbl을 찾지 못함")
        return ti

    stbl_children = list(iter_boxes(f, stbl_box.payload_start, stbl_box.end,
                                     context=f"stbl@0x{stbl_box.start:X}"))

    stsd_box = find_box(stbl_children, b"stsd")
    stsc_box = find_box(stbl_children, b"stsc")
    stsz_box = find_box(stbl_children, b"stsz")

    if stsd_box is None or stsc_box is None or stsz_box is None:
        warn(f"Track #{track_number}(text): stsd/stsc/stsz 중 일부가 없음 "
             f"(stsd={stsd_box is not None}, stsc={stsc_box is not None}, "
             f"stsz={stsz_box is not None})")
        return ti

    ti.stsd_entries = parse_stsd(f, stsd_box)
    stsc_entries = parse_stsc(f, stsc_box)
    sample_sizes = parse_stsz(f, stsz_box)
    chunk_offsets, offset_box_kind = parse_chunk_offsets(f, stbl_children)

    if not chunk_offsets:
        warn(f"Track #{track_number}(text): Chunk offset을 하나도 못 구함")
        return ti

    valid_sdi = {e.index for e in ti.stsd_entries}
    for rule in stsc_entries:
        if rule.sample_description_index not in valid_sdi:
            warn(f"Track #{track_number}: stsc가 가리키는 "
                 f"sample_description_index={rule.sample_description_index}가 "
                 f"stsd entry 범위({sorted(valid_sdi)})를 벗어남")

    ti.samples = compute_sample_positions(stsc_entries, chunk_offsets, sample_sizes)

    # 재생 시간축: mdhd.timescale + stts 로 sample별 시작/종료 시각(초)을 만든다.
    ti.timescale = parse_mdhd_timescale(f, mdia_children)
    stts_box = find_box(stbl_children, b"stts")
    if stts_box is None:
        warn(f"Track #{track_number}(text): stts가 없어 재생 시간축을 만들 수 없음 "
             f"- start_time_sec 계열은 공란으로 둠")
    elif ti.timescale:
        ti.sample_times = build_sample_times(parse_stts(f, stts_box), ti.timescale,
                                              len(ti.samples))
    return ti

def scan_keywords(text):
    hits = []
    for kw in KEYWORD_CANDIDATES:
        if kw in text:
            hits.append(kw)
    return hits

def print_track_table(tracks):
    info("\n[Track Table]")
    info(f"{'#':<4} {'handler':<8} {'name':<16} {'stsd_types':<20} {'samples':<8}")
    for t in tracks:
        stsd_types = ";".join(sorted(set(e.entry_type.decode('ascii', errors='replace')
                                          for e in t.stsd_entries))) or "-"
        n_samples = len(t.samples)
        info(f"{t.track_number:<4} "
             f"{(t.handler_type or b'-').decode('ascii', errors='replace'):<8} "
             f"{(t.handler_name or '-'):<16} "
             f"{stsd_types:<20} "
             f"{n_samples:<8}")

def sanitize_label(text):
    text = re.sub(r"[^A-Za-z0-9_]+", "_", text).strip("_")
    return text or "track"

def _fmt_sec(value):
    return f"{value:.3f}" if value is not None else ""


def sample_time_range(track_info, sample_number):
    """sample 번호(1-based)에 대응하는 (start_sec, end_sec). 없으면 (None, None)."""
    times = track_info.sample_times
    idx = sample_number - 1
    if 0 <= idx < len(times):
        return times[idx]
    return (None, None)


def write_timeline(track_dir, timeline_rows, sensor_rows):
    """GPS와 G센서를 sample 시간 기준으로 한 줄씩 합친 통합 타임라인.
    루트 A(fragmented)의 timeline.csv와 같은 목적/컬럼 구성이다.
    latitude/speed_kmh 등은 그 sample에 GPS가 실제로 실려온 경우에만 채우고,
    `*_last` 컬럼에만 가장 최근 GPS 값을 이어붙인다(보간하지 않음)."""
    if not timeline_rows:
        return
    cal = {}
    for r in sensor_rows:
        cal[r["sample"]] = r
    last = {}
    out = []
    for row in timeline_rows:
        gps = row.get("_gps")
        if gps is not None and gps.get("lat") is not None:
            last = {
                "latitude_last": f"{gps['lat']:.6f}",
                "longitude_last": f"{gps['lon']:.6f}",
                "speed_kmh_last": (f"{gps['speed_kmh']:.3f}"
                                   if gps.get("speed_kmh") is not None else ""),
            }
        sen = cal.get(row["sample"], {})
        out.append({
            "sample": row["sample"],
            "start_time_sec": row["start_time_sec"],
            "end_time_sec": row["end_time_sec"],
            "time_source": row["time_source"],
            "latitude": (f"{gps['lat']:.6f}"
                         if gps and gps.get("lat") is not None else ""),
            "longitude": (f"{gps['lon']:.6f}"
                          if gps and gps.get("lon") is not None else ""),
            "speed_kmh": (f"{gps['speed_kmh']:.3f}"
                          if gps and gps.get("speed_kmh") is not None else ""),
            "track_deg": gps.get("track_deg", "") if gps else "",
            "gps_date": gps.get("date", "") if gps else "",
            "gps_utc_time": gps.get("utc_time", "") if gps else "",
            "gps_checksum_ok": gps.get("checksum_ok", "") if gps else "",
            "latitude_last": last.get("latitude_last", ""),
            "longitude_last": last.get("longitude_last", ""),
            "speed_kmh_last": last.get("speed_kmh_last", ""),
            "x_g": sen.get("x_g", ""), "y_g": sen.get("y_g", ""), "z_g": sen.get("z_g", ""),
            "x_g_cal": sen.get("x_g_cal", ""), "y_g_cal": sen.get("y_g_cal", ""),
            "z_g_cal": sen.get("z_g_cal", ""),
        })
    _write_csv(os.path.join(track_dir, "timeline.csv"), out)

def extract_text_track(f, filesize, track_info, out_dir, dry_run=False, do_bin_extract=False):
    t = track_info
    dir_label = f"TRACK{t.track_number}_TEXT"
    track_dir = os.path.join(out_dir, dir_label)

    if not dry_run:
        os.makedirs(track_dir, exist_ok=True)
        if do_bin_extract:
            os.makedirs(os.path.join(track_dir, "chunks"), exist_ok=True)

    index_rows = []
    coord_rows = []
    sensor_rows = []
    generic_rows = []
    vendor_rows = []
    keyword_hits_rows = []
    timeline_rows = []

    classify_counts = {"gsensor": 0, "gps_nmea": 0, "generic": 0, "length_prefix_mismatch": 0,
                        "undecodable": 0}
    preview_count = 0

    for s in t.samples:
        offset = s.absolute_offset
        size = s.size
        out_of_range = (offset < 0) or (offset + size > filesize)

        start_sec, end_sec = sample_time_range(t, s.sample_number)
        time_source = "stts" if start_sec is not None else ""
        index_row = {
            "track": t.track_number,
            "sample": s.sample_number,
            "chunk": s.chunk_number,
            "sample_description_index": s.sample_description_index,
            "start_time_sec": _fmt_sec(start_sec),
            "end_time_sec": _fmt_sec(end_sec),
            "time_source": time_source,
            "absolute_offset": f"0x{offset:08X}",
            "size": size,
            "validation": "OK",
            "output_file": "",
        }

        if out_of_range:
            index_row["validation"] = "OUT_OF_RANGE"
            index_rows.append(index_row)
            warn(f"Track#{t.track_number} Sample#{s.sample_number}: "
                 f"offset(0x{offset:X})+size({size})가 파일 크기({filesize})를 넘어감 - 건너뜀")
            continue

        f.seek(offset)
        raw = f.read(size)

        if preview_count < 3:
            hex_part, ascii_part = hex_preview_lines(raw)
            info(f"\n[Track#{t.track_number} Sample#{s.sample_number} "
                 f"(Chunk#{s.chunk_number}, stsd#{s.sample_description_index})]")
            info(f"Absolute Offset : 0x{offset:08X}")
            info(f"Size            : {size}")
            info("HEX:")
            info(hex_part)
            info("ASCII:")
            info(ascii_part)
            preview_count += 1

        if not dry_run and do_bin_extract:
            bin_name = (f"track_{t.track_number:02d}_sample_{s.sample_number:06d}"
                        f"_offset_{offset:X}_size_{size:X}.bin")
            bin_path = os.path.join(track_dir, "chunks", bin_name)
            with open(bin_path, "wb") as bf:
                bf.write(raw)
            index_row["output_file"] = os.path.join(dir_label, "chunks", bin_name)

        text, used_prefix = decode_sample_text(raw)
        if text is None:
            classify_counts["undecodable"] += 1
            index_row["validation"] = "UNDECODABLE_PAYLOAD"
            index_rows.append(index_row)
            continue
        if not used_prefix:
            classify_counts["length_prefix_mismatch"] += 1
            index_row["validation"] = "OK_NO_LENGTH_PREFIX"

        index_rows.append(index_row)

        for kw in scan_keywords(text):
            keyword_hits_rows.append({
                "track": t.track_number, "sample": s.sample_number, "keyword": kw,
                "context": text[:120],
            })

        timeline_gps = None
        for segment in split_segments(text):
            kind, payload = classify_segment(segment)
            classify_counts[kind] = classify_counts.get(kind, 0) + 1

            if kind == "gsensor":
                # 공통 classify_segment(루트 A/B 공용)는 gsensor를 해석해서
                # count/scale/x_raw/y_raw/z_raw와 g 환산값까지 같이 돌려준다.
                # 기존 pvc1 출력(field_0, field_1, ...)도 그대로 유지한다(상위호환).
                row = {
                    "sample": s.sample_number,
                    "start_time_sec": _fmt_sec(start_sec),
                    "end_time_sec": _fmt_sec(end_sec),
                    "time_source": time_source,
                    "absolute_offset": f"0x{offset:08X}",
                    "subtype": payload.get("subtype", ""),
                    "count": payload.get("count"), "scale": payload.get("scale"),
                    "x_raw": payload.get("x_raw"), "y_raw": payload.get("y_raw"),
                    "z_raw": payload.get("z_raw"),
                    "x_g": payload.get("x_g"), "y_g": payload.get("y_g"),
                    "z_g": payload.get("z_g"),
                }
                for i, v in enumerate(payload.get("raw_fields", [])):
                    row[f"field_{i}"] = v
                sensor_rows.append(row)

            elif kind == "gps_nmea":
                parsed = payload
                speed_kmh = parsed.get("speed_kmh")
                timeline_gps = parsed
                coord_rows.append({
                    "sample": s.sample_number,
                    "start_time_sec": _fmt_sec(start_sec),
                    "end_time_sec": _fmt_sec(end_sec),
                    "time_source": time_source,
                    "date": parsed.get("date", ""),
                    "utc_time": parsed.get("utc_time", ""),
                    "status": parsed.get("status", ""),
                    "latitude": f"{parsed['lat']:.6f}" if parsed.get("lat") is not None else "",
                    "longitude": f"{parsed['lon']:.6f}" if parsed.get("lon") is not None else "",
                    "speed_knots": parsed.get("speed_knots", ""),
                    "speed_kmh": f"{speed_kmh:.3f}" if speed_kmh is not None else "",
                    "track_deg": parsed.get("track_deg", ""),
                    "magvar": parsed.get("magvar", ""),
                    "magvar_dir": parsed.get("magvar_dir", ""),
                    "mode": parsed.get("mode", ""),
                    "sentence_type": parsed["sentence_type"],
                    "checksum_ok": parsed["checksum_ok"],
                    "status_valid": parsed.get("status_valid", ""),
                    "trusted": parsed.get("trusted", ""),
                    "parse_warnings": parsed.get("parse_warnings", ""),
                    "raw_sentence": parsed["raw"],
                })

            elif kind == "vendor_raw":
                # 공통 classify_segment가 "$TAG,..." 형태의 미확정 벤더 레코드를
                # 따로 분류한다. pvc1 원본에는 없던 갈래라 여기서 받아 보존한다.
                vendor_rows.append({
                    "sample": s.sample_number,
                    "absolute_offset": f"0x{offset:08X}",
                    "tag": payload.get("tag", ""),
                    "raw_fields": "|".join(payload.get("raw_fields", [])),
                    "raw": payload.get("raw", ""),
                    "note": "unconfirmed_field_meaning",
                })

            else:
                row = {"sample": s.sample_number, "label": payload["label"]}
                for i, v in enumerate(payload["fields"]):
                    row[f"field_{i}"] = v
                row["raw"] = payload["raw"]
                generic_rows.append(row)

        timeline_rows.append({
            "sample": s.sample_number,
            "start_time_sec": _fmt_sec(start_sec),
            "end_time_sec": _fmt_sec(end_sec),
            "time_source": time_source,
            "_gps": timeline_gps,
        })

    if not dry_run:
        _write_csv(os.path.join(track_dir, "index.csv"), index_rows)
        if coord_rows:
            _write_coord_outputs(track_dir, coord_rows)
        if sensor_rows:
            apply_gsensor_calibration(sensor_rows)
            _write_csv(os.path.join(track_dir, "sensor_values.csv"), sensor_rows)
        # timeline.csv는 센서 보정 뒤에 만들어야 x_g_cal 계열이 채워진 상태로 들어간다.
        write_timeline(track_dir, timeline_rows, sensor_rows)
        if vendor_rows:
            _write_csv(os.path.join(track_dir, "vendor_raw.csv"), vendor_rows)
        if generic_rows:
            _write_csv(os.path.join(track_dir, "other_segments_unparsed.csv"), generic_rows)
        if keyword_hits_rows:
            _write_csv(os.path.join(track_dir, "keyword_hits.csv"), keyword_hits_rows)

    return {
        "classify_counts": classify_counts,
        "coord_count": len(coord_rows),
        "sensor_count": len(sensor_rows),
        "vendor_count": len(vendor_rows),
        "generic_count": len(generic_rows),
    }

def _write_csv(path, rows):
    if not rows:
        return
    fieldnames = []
    seen = set()
    for row in rows:
        for k in row.keys():
            if k not in seen:
                seen.add(k)
                fieldnames.append(k)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)

def _write_coord_outputs(track_dir, coord_rows):
    _write_csv(os.path.join(track_dir, "coordinates.csv"), coord_rows)
    with open(os.path.join(track_dir, "coordinates.txt"), "w", encoding="utf-8") as f:
        # coordinates.txt는 "좌표 목록"이라 fix가 없어 좌표가 빈 행은 제외한다
        # (그 행도 coordinates.csv에는 status=V로 그대로 남는다).
        for i, row in enumerate([r for r in coord_rows if r["latitude"]], start=1):
            f.write(f"{i}. {row['latitude']}, {row['longitude']}\n")

def save_track_summary(out_dir, tracks, dry_run=False):
    if dry_run:
        return
    os.makedirs(out_dir, exist_ok=True)
    rows = []
    for t in tracks:
        stsd_types = ";".join(sorted(set(e.entry_type.decode('ascii', errors='replace')
                                          for e in t.stsd_entries)))
        rows.append({
            "track": t.track_number,
            "handler_type": (t.handler_type or b"").decode("ascii", errors="replace"),
            "handler_name": t.handler_name,
            "stsd_types": stsd_types,
            "sample_count": len(t.samples),
            "is_text_track": t.is_text_track,
        })
    _write_csv(os.path.join(out_dir, "track_table.csv"), rows)

    with open(os.path.join(out_dir, "warnings.log"), "w", encoding="utf-8") as f:
        for w_msg in WARNINGS:
            f.write(w_msg + "\n")



# ---- 여기부터: GPS_metadata_mp4_udta_mamt_GNRMC_pmp42.py (루트 C, udta/mamt) ----


TEXT_HANDLER_TYPES = {b"text", b"sbtl", b"subt"}

def get_handler_type(f, trak_box):
    trak_children = list(iter_boxes(f, trak_box.payload_start, trak_box.end,
                                     context=f"trak@0x{trak_box.start:X}"))
    mdia_box = find_box(trak_children, b"mdia")
    if mdia_box is None:
        return None
    mdia_children = list(iter_boxes(f, mdia_box.payload_start, mdia_box.end,
                                     context=f"mdia@0x{mdia_box.start:X}"))
    hdlr_box = find_box(mdia_children, b"hdlr")
    if hdlr_box is None:
        return None
    f.seek(hdlr_box.payload_start)
    payload = f.read(hdlr_box.size - hdlr_box.header_size)
    if len(payload) < 12:
        warn(f"hdlr @ 0x{hdlr_box.start:X}: payload가 너무 짧음")
        return None
    return payload[8:12]

def try_parse_rmc_sentence(raw):
    raw = raw.strip("\x00\r\n\t ")
    body_with_checksum = raw[1:] if raw.startswith("$") else raw
    checksum_ok = nmea_checksum_ok(body_with_checksum)
    body = body_with_checksum.split("*", 1)[0]
    fields = body.split(",")
    if not fields or len(fields[0]) != 5:
        return None
    talker, sentence_type = fields[0][:2], fields[0][2:].upper()
    if not talker.isalpha() or sentence_type != "RMC":
        return None
    parsed = parse_rmc(fields)
    if parsed is None:
        return None
    parsed["talker"] = talker
    parsed["sentence_type"] = sentence_type
    parsed["raw"] = raw
    parsed["checksum_ok"] = checksum_ok
    parsed["trusted"] = bool(parsed.get("status_valid", True) and checksum_ok is not False
                              and not parsed.get("parse_warnings"))
    return parsed

def locate_gps_source(f, filesize):
    top_boxes = list(iter_boxes(f, 0, filesize, context="top-level"))
    top_types = [b.box_type for b in top_boxes]

    if b"moof" in top_types and b"moov" not in top_types:
        return None, ("moof(fragmented) Box는 있지만 moov가 없음 - "
                       "fragmented mp4로 보이며 이 스크립트의 대상이 아님 "
                       "(GPS_metadata_fregment_iso4_Atext.py 사용 권장)")

    moov_box = find_box(top_boxes, b"moov")
    if moov_box is None:
        return None, "moov Box를 찾지 못함"

    if b"moof" in top_types:
        warn("moov와 moof가 모두 존재함 - non-fragmented 전제가 완전히 맞지 않을 "
             "수 있으나 moov 기준으로 계속 진행함")

    moov_children = list(iter_boxes(f, moov_box.payload_start, moov_box.end,
                                     context=f"moov@0x{moov_box.start:X}"))
    trak_boxes = find_all(moov_children, b"trak")
    if not trak_boxes:
        return None, "moov 안에 trak Box가 하나도 없음"

    handler_types = []
    for trak_box in trak_boxes:
        handler_type = get_handler_type(f, trak_box)
        handler_types.append(handler_type)
        info(f"  trak@0x{trak_box.start:X} handler_type="
             f"{handler_type.decode('ascii', errors='replace') if handler_type else '(없음)'}")

    text_traks = [h for h in handler_types if h in TEXT_HANDLER_TYPES]
    if text_traks:
        return None, (f"handler_type={text_traks[0]!r} 인 text 계열 track이 존재함 - "
                       "일반적인 text-track 기반 GPS 추출 대상이며 이 스크립트의 "
                       "예외 케이스(udta/mamt)가 아님 (GPS_metadata_mp4_pvc1_Atext.py 사용 권장)")

    udta_box = find_box(moov_children, b"udta")
    if udta_box is None:
        return None, "text track도 없고 moov 안에 udta Box도 없음 - 지원하지 않는 구조"

    udta_children = list(iter_boxes(f, udta_box.payload_start, udta_box.end,
                                     context=f"udta@0x{udta_box.start:X}"))
    mamt_box = find_box(udta_children, b"mamt")
    if mamt_box is None:
        return None, "udta는 있지만 그 안에 mamt Box가 없음 - 지원하지 않는 구조"

    return mamt_box, None

def extract_rmc_sentences(payload, payload_abs_start):
    sentences = []
    search_pos = 0
    n = len(payload)
    pattern = re.compile(rb"\$[A-Z]{2}RMC")
    while search_pos < n:
        m = pattern.search(payload, search_pos)
        if not m:
            break
        start = m.start()
        end = payload.find(b"\r\n", start)
        if end == -1:
            warn(f"mamt payload offset 0x{payload_abs_start + start:X}: "
                 f"'$GxRMC' 시작은 찾았지만 종료 CRLF를 찾지 못함 - 이후 데이터가 "
                 f"잘렸거나 손상된 것으로 보고 탐색을 종료함")
            break
        raw = payload[start:end].decode("ascii", errors="replace")
        sentences.append((payload_abs_start + start, raw))
        search_pos = end + 2
    return sentences



# ---- 여기부터: 이 파일 고유 - 컨테이너 구조 판별 디스패처 ----
ROUTE_FRAGMENTED = "fragmented_atext"
ROUTE_SAMPLETABLE = "sampletable_atext"
ROUTE_UDTA_MAMT = "udta_mamt"


def probe_container(f, filesize):
    """반환값: dict(route, brand, compatible, moof_count, handlers, reason)
    route가 None이면 지원하지 않는 구조이고 reason에 이유가 들어간다."""
    top = list(iter_child_boxes(f, 0, filesize, context="top-level", allow_size_zero=True))
    top_types = [b.box_type for b in top]

    brand, compat = "", []
    ftyp_box = find_box(top, b"ftyp")
    if ftyp_box is not None:
        ftyp = parse_ftyp(f, ftyp_box)
        if ftyp:
            brand = ftyp["major_brand"].decode("ascii", errors="replace")
            compat = [b.decode("ascii", errors="replace") for b in ftyp["compatible_brands"]]

    moof_count = sum(1 for t in top_types if t == b"moof")
    moov_box = find_box(top, b"moov")

    result = {"route": None, "brand": brand, "compatible": compat,
              "moof_count": moof_count, "handlers": [], "reason": None,
              "top_types": [t.decode("ascii", errors="replace") for t in top_types]}

    if moov_box is None:
        result["reason"] = ("moov Box가 없음 - MP4 초기화 정보를 읽을 수 없어 지원 불가"
                            if moof_count == 0 else
                            "moof만 있고 moov가 없음 - 초기화 세그먼트가 분리된 파일로 보임")
        return result

    moov_children = list(iter_child_boxes(f, moov_box.payload_start, moov_box.end,
                                           context=f"moov@0x{moov_box.start:X}"))
    handlers = []
    for trak_box in find_all(moov_children, b"trak"):
        ht = get_handler_type(f, trak_box)
        handlers.append(ht.decode("ascii", errors="replace") if ht else "?")
    result["handlers"] = handlers
    has_text_track = any(h.encode("ascii", errors="replace") in TEXT_HANDLER_TYPES
                         for h in handlers)

    # (1) fragmented가 최우선 - moov의 빈 trak에 낚이지 않도록.
    if moof_count > 0:
        result["route"] = ROUTE_FRAGMENTED
        return result

    # (2) non-fragmented인데 text 계열 handler track이 있으면 sample table 경로.
    if has_text_track:
        result["route"] = ROUTE_SAMPLETABLE
        return result

    # (3) text track이 없으면 moov/udta/mamt 예외 케이스인지 확인.
    udta_box = find_box(moov_children, b"udta")
    if udta_box is not None:
        udta_children = list(iter_child_boxes(f, udta_box.payload_start, udta_box.end,
                                               context=f"udta@0x{udta_box.start:X}"))
        if find_box(udta_children, b"mamt") is not None:
            result["route"] = ROUTE_UDTA_MAMT
            return result
        result["reason"] = ("text track도 없고 udta 안에 mamt도 없음 (udta children="
                            + ",".join(b.box_type.decode("ascii", errors="replace")
                                       for b in udta_children) + ")")
        return result

    result["reason"] = "non-fragmented인데 text track도 없고 moov 안에 udta Box도 없음"
    return result


# ---- 여기부터: 이 파일 고유 - 루트별 실행부 ----
def run_fragmented(f, filesize, out_dir, args):
    """루트 A - GPS_metadata_fragment_iso4_Atext.py 의 main과 동일한 호출 순서."""
    top_boxes = scan_top_level(f, filesize)
    moov_boxes = find_all(top_boxes, b"moov")
    moof_boxes = find_all(top_boxes, b"moof")
    mdat_ranges = [(b.payload_start, b.end) for b in find_all(top_boxes, b"mdat")]

    if not moov_boxes:
        warn("moov Box를 찾지 못함")
        return None
    if not moof_boxes:
        warn("moof Box가 하나도 없음 - fragmented MP4가 아님")
        return None

    text_tracks, trex_defaults, all_tracks = parse_moov(f, moov_boxes[0])
    if not text_tracks:
        warn("handler_type == 'text' 인 Track을 찾지 못함")
        return None

    if args.track_id is not None:
        if args.track_id not in text_tracks:
            warn(f"--track-id {args.track_id}는 text Track이 아니거나 없음 "
                 f"(사용 가능: {sorted(text_tracks)})")
            return None
        target_track_id = args.track_id
    else:
        target_track_id = sorted(text_tracks)[0]
    target = text_tracks[target_track_id]
    info(f"\n대상 text Track: track_ID={target.track_id} timescale={target.timescale}")

    track_dts_state = {}
    out_samples = []
    for moof_index, moof_box in enumerate(moof_boxes):
        parse_moof(f, moof_box, moof_index, target_track_id, target.timescale,
                    trex_defaults, filesize, mdat_ranges, track_dts_state, out_samples)
    info(f"\n총 {len(out_samples)}개 text Track sample 발견")

    printed = 0
    for s in out_samples:
        if printed >= args.max_print:
            break
        print_sample(s)
        printed += 1

    save_outputs(out_dir, out_samples, all_tracks, target_track_id, dry_run=args.dry_run)

    gps = sum(1 for s in out_samples for k, _ in s.parsed_segments if k == "gps_nmea")
    gsen = sum(1 for s in out_samples for k, _ in s.parsed_segments if k == "gsensor")
    vend = sum(1 for s in out_samples for k, _ in s.parsed_segments if k == "vendor_raw")
    starts = [s.start_time for s in out_samples if s.start_time is not None]
    ends = [s.end_time for s in out_samples if s.end_time is not None]
    return {"track": f"track_ID={target_track_id}", "samples": len(out_samples),
            "gps": gps, "gsensor": gsen, "vendor_raw": vend,
            "errors": sum(1 for s in out_samples if s.error),
            "time_range": (min(starts), max(ends)) if starts and ends else None}


def run_sampletable(f, filesize, out_dir, args):
    """루트 B - GPS_metadata_mp4_pvc1_Atext.py 의 main과 동일한 호출 순서."""
    top_boxes = list(iter_child_boxes(f, 0, filesize, context="top-level", allow_size_zero=True))
    moov_boxes = find_all(top_boxes, b"moov")
    if not moov_boxes:
        warn("moov Box를 찾지 못함")
        return None

    all_tracks = []
    track_number = 0
    for moov_box in moov_boxes:
        moov_children = list(iter_child_boxes(f, moov_box.payload_start, moov_box.end,
                                               context=f"moov@0x{moov_box.start:X}"))
        for trak_box in find_all(moov_children, b"trak"):
            track_number += 1
            all_tracks.append(parse_track(f, trak_box, track_number))

    print_track_table(all_tracks)

    text_tracks = [t for t in all_tracks if t.is_text_track]
    if args.track:
        wanted = set(args.track)
        text_tracks = [t for t in text_tracks if t.track_number in wanted]
        for m in wanted - {t.track_number for t in text_tracks}:
            warn(f"--track {m} 지정했지만 해당 번호는 text Track이 아니거나 존재하지 않음")
    if not text_tracks:
        warn("지원 text/subtitle handler(text/sbtl/subt) Track을 하나도 찾지 못함")
        return None

    total = {"samples": 0, "gps": 0, "gsensor": 0, "vendor_raw": 0, "generic": 0, "tracks": []}
    for t in text_tracks:
        info(f"\n{'=' * 60}\nTrack #{t.track_number} (text) 추출\n{'=' * 60}")
        r = extract_text_track(f, filesize, t, out_dir, dry_run=args.dry_run,
                                do_bin_extract=args.extract)
        total["samples"] += len(t.samples)
        total["gps"] += r["coord_count"]
        total["gsensor"] += r["sensor_count"]
        total["vendor_raw"] += r.get("vendor_count", 0)
        total["generic"] += r["generic_count"]
        total["tracks"].append((t.track_number, r))

    save_track_summary(out_dir, all_tracks, dry_run=args.dry_run)
    total["track"] = "TRACK" + ",".join(str(t.track_number) for t in text_tracks)
    return total


# ---------------------------------------------------------------------------
# 재생 시간축 (루트 C: udta/mamt)
#
# 이 경로의 GPS 문장은 sample이 아니라 udta 밑 커스텀 박스에 그냥 나열돼 있어서
# sample table(stts/tfdt)이 아예 없다. 즉 구조에서 시간을 뽑을 방법이 없다.
# 대신 문장 자체가 UTC 시각을 들고 있고, 실측상 정확히 1Hz로 기록된다.
#
#   [20250901_215728D] 60행, 중복 시각 0개, '순번=경과초' 오차 0초
#
# 그래서 첫 문장의 UTC를 영상 0초로 놓고 경과초를 계산한다. 순번을 그대로 쓰지 않는
# 이유는 GPS가 끊겨 문장이 빠지면 순번과 실제 시각이 어긋나기 때문이다.
# ---------------------------------------------------------------------------
def _utc_to_seconds(utc_time):
    """'HH:MM:SS.ss' -> 자정 기준 초. 파싱 실패면 None."""
    if not utc_time:
        return None
    try:
        hh, mm, ss = utc_time.split(":")
        return int(hh) * 3600 + int(mm) * 60 + float(ss)
    except (ValueError, AttributeError):
        return None


def assign_utc_elapsed_times(coord_rows, nominal_interval=1.0):
    """coord_rows에 start_time_sec/end_time_sec/time_source를 채운다.
    첫 문장의 UTC가 0초. 자정을 넘어가면 하루(86400초)를 더해 이어붙인다."""
    base = None
    prev = None
    carry = 0.0
    assigned = 0
    for row in coord_rows:
        secs = _utc_to_seconds(row.get("utc_time"))
        if secs is None:
            row["start_time_sec"] = ""
            row["end_time_sec"] = ""
            row["time_source"] = ""
            continue
        if prev is not None and secs + carry < prev:
            # 23:59:59 -> 00:00:00 처럼 되감기면 자정을 넘긴 것으로 본다.
            carry += 86400.0
        value = secs + carry
        prev = value
        if base is None:
            base = value
        start = value - base
        row["start_time_sec"] = f"{start:.3f}"
        row["end_time_sec"] = f"{start + nominal_interval:.3f}"
        row["time_source"] = "gps_utc_elapsed"
        assigned += 1
    if coord_rows and assigned == 0:
        warn("UTC 시각을 읽을 수 있는 GPS 문장이 없어 재생 시간축을 만들지 못함 "
             "- start_time_sec 계열은 공란으로 둠")
    return assigned

def write_timeline_from_coords(out_dir, coord_rows, sensor_rows=None, dry_run=False):
    """좌표 행(이미 start_time_sec가 채워진 상태)만으로 통합 타임라인을 만든다.
    G센서가 없는 경로(udta/mamt 등)에서 쓰며, 컬럼 구성은 루트 A/B의 timeline.csv와
    같게 맞춘다 - 시각화 쪽에서 경로마다 다른 파일을 읽지 않아도 되도록."""
    if dry_run or not coord_rows:
        return
    by_sample = {}
    for r in (sensor_rows or []):
        by_sample[str(r.get("sample", ""))] = r

    rows = []
    last_lat = last_lon = last_speed = ""
    for i, r in enumerate(coord_rows, start=1):
        if r.get("latitude"):
            last_lat, last_lon = r["latitude"], r["longitude"]
            last_speed = r.get("speed_kmh", "")
        sen = by_sample.get(str(i), {})
        rows.append({
            "sample": i,
            "start_time_sec": r.get("start_time_sec", ""),
            "end_time_sec": r.get("end_time_sec", ""),
            "time_source": r.get("time_source", ""),
            "latitude": r.get("latitude", ""),
            "longitude": r.get("longitude", ""),
            "speed_kmh": r.get("speed_kmh", ""),
            "track_deg": r.get("track_deg", ""),
            "gps_date": r.get("date", ""),
            "gps_utc_time": r.get("utc_time", ""),
            "gps_checksum_ok": r.get("checksum_ok", ""),
            "latitude_last": last_lat,
            "longitude_last": last_lon,
            "speed_kmh_last": last_speed,
            "x_g": sen.get("x_g", ""), "y_g": sen.get("y_g", ""), "z_g": sen.get("z_g", ""),
            "x_g_cal": sen.get("x_g_cal", ""), "y_g_cal": sen.get("y_g_cal", ""),
            "z_g_cal": sen.get("z_g_cal", ""),
        })
    write_csv(os.path.join(out_dir, "timeline.csv"), rows)

def run_udta_mamt(f, filesize, out_dir, args):
    """루트 C - GPS_metadata_mp4_udta_mamt_GNRMC_pmp42.py 와 동일한 처리."""
    mamt_box, reason = locate_gps_source(f, filesize)
    if mamt_box is None:
        warn(f"udta/mamt 위치 확인 실패: {reason}")
        return None

    info(f"  mamt payload: 0x{mamt_box.payload_start:X} - 0x{mamt_box.end:X} "
         f"({mamt_box.end - mamt_box.payload_start} bytes)")
    f.seek(mamt_box.payload_start)
    payload = f.read(mamt_box.end - mamt_box.payload_start)

    sentences = extract_rmc_sentences(payload, mamt_box.payload_start)
    info(f"  '$GxRMC' 문장 {len(sentences)}개 발견")

    stream_dir = os.path.join(out_dir, "GPS_GNRMC")
    if not args.dry_run:
        os.makedirs(os.path.join(stream_dir, "raw_chunks"), exist_ok=True)

    coord_rows = []
    unparsed_lines = []
    raw_concat = []
    for seq, (abs_offset, raw) in enumerate(sentences):
        raw_bytes = raw.encode("ascii", errors="replace")
        raw_concat.append(raw_bytes)
        if not args.dry_run:
            with open(os.path.join(stream_dir, "raw_chunks", f"gnrmc_{seq:06d}.bin"), "wb") as cf:
                cf.write(raw_bytes)

        parsed = try_parse_rmc_sentence(raw)
        if parsed is None:
            unparsed_lines.append((seq, raw))
            warn(f"entry #{seq} (offset 0x{abs_offset:X}) RMC 파싱 실패 - "
                 f"원문 보존만 하고 다음 문장으로 계속 진행: {raw!r}")
            continue

        speed_kmh = parsed.get("speed_kmh")
        coord_rows.append({
            "start_time_sec": "", "end_time_sec": "", "time_source": "",
            "date": parsed.get("date", ""), "utc_time": parsed.get("utc_time", ""),
            "status": parsed.get("status", ""),
            "latitude": f"{parsed['lat']:.6f}" if parsed.get("lat") is not None else "",
            "longitude": f"{parsed['lon']:.6f}" if parsed.get("lon") is not None else "",
            "speed_knots": parsed.get("speed_knots", ""),
            "speed_kmh": f"{speed_kmh:.3f}" if speed_kmh is not None else "",
            "track_deg": parsed.get("track_deg", ""),
            "magvar": parsed.get("magvar", ""), "magvar_dir": parsed.get("magvar_dir", ""),
            "mode": parsed.get("mode", ""), "checksum_ok": parsed["checksum_ok"],
            "status_valid": parsed.get("status_valid", ""),
            "trusted": parsed.get("trusted", ""),
            "parse_warnings": parsed.get("parse_warnings", ""),
            "sequence": seq,
            "idx1_entry_offset": f"0x{abs_offset:08X}",
            "chunk_id": "mamt",
            "sentence_type": parsed["sentence_type"], "raw_sentence": parsed["raw"],
        })

    # 재생 시간축: 이 경로는 sample table이 없어 GPS UTC 경과초로 만든다.
    assign_utc_elapsed_times(coord_rows)
    write_timeline_from_coords(stream_dir, coord_rows, dry_run=args.dry_run)

    if not args.dry_run:
        with open(os.path.join(stream_dir, "raw_concat.bin"), "wb") as rf:
            for rb in raw_concat:
                rf.write(rb + b"\r\n")
        _write_csv(os.path.join(stream_dir, "coordinates.csv"), coord_rows)
        with open(os.path.join(stream_dir, "coordinates.txt"), "w", encoding="utf-8") as cf:
            # coordinates.txt는 "좌표 목록"이라 fix가 없어 좌표가 빈 행은 제외한다
            # (그 행도 coordinates.csv에는 status=V로 그대로 남는다).
            for i, row in enumerate([r for r in coord_rows if r["latitude"]], start=1):
                cf.write(f"{i}. {row['latitude']}, {row['longitude']}\n")
        with open(os.path.join(stream_dir, "unparsed_lines.txt"), "w", encoding="utf-8") as uf:
            for i, (seq, line) in enumerate(unparsed_lines, start=1):
                uf.write(f"{i}. (entry #{seq}) {line}\n")

    return {"track": "udta/mamt", "samples": len(sentences),
            "gps": len(coord_rows), "gsensor": 0, "vendor_raw": 0,
            "unparsed": len(unparsed_lines),
            "no_fix": sum(1 for r in coord_rows if r["status"] == "V")}



# ---- 여기부터: mp4_slack_carve.py (슬랙 카빙, --slack 일 때만 동작) ----

SLACK_BOX_TYPES = {b"free", b"skip"}

MIN_SLACK_REGION = 64

def find_slack_regions(f, filesize):
    """반환값: [(kind, start, end)] - kind는 free/skip/gap/trailing.

    Box size만 따라가며 최상위를 순회한다(문자열 검색 안 함). 순회 도중 size가
    깨져서 더 못 가면 거기서 멈추고, 남은 뒷부분은 통째로 trailing 슬랙으로 본다.
    """
    regions = []
    boxes = []
    pos = 0
    while pos + 8 <= filesize:
        box = read_box_header(f, pos, filesize, context="slack-scan", allow_size_zero=True)
        if box is None:
            break
        boxes.append(box)
        if box.start > pos:
            regions.append(("gap", pos, box.start))
        pos = box.end

    prev_end = 0
    for box in boxes:
        if box.start > prev_end:
            regions.append(("gap", prev_end, box.start))
        if box.box_type in SLACK_BOX_TYPES:
            regions.append((box.box_type.decode("ascii", errors="replace"),
                            box.payload_start, box.end))
        prev_end = box.end

    if prev_end < filesize:
        regions.append(("trailing", prev_end, filesize))

    regions = [(k, s, e) for k, s, e in regions if e - s >= MIN_SLACK_REGION]
    regions.sort(key=lambda r: r[1])
    return regions, boxes

SLACK_NMEA_RE = re.compile(rb"\$?[A-Z]{2}(?:RMC|GGA),[ -~]*")

SLACK_GSENSOR_RE = re.compile(rb"\$?gsensor[A-Za-z0-9]*,[ -~]*", re.IGNORECASE)

def carve_region(raw, start, end, region_kind):
    """한 슬랙 영역에서 gps_nmea / gsensor 레코드를 뽑는다."""
    found = {}
    for pattern in (SLACK_NMEA_RE, SLACK_GSENSOR_RE):
        for m in pattern.finditer(raw, start, end):
            off = m.start()
            if off in found:
                continue
            text = m.group().decode("ascii", errors="replace")
            segments = split_segments(text)
            if not segments:
                continue
            # 매치 시작점에 실제로 놓인 세그먼트는 첫 번째 것이다. 그 뒤에 붙은
            # 세그먼트들은 각자의 offset에서 따로 매치되므로 여기서 안 다룬다.
            kind, payload = classify_segment(segments[0])
            if kind not in ("gps_nmea", "gsensor"):
                continue
            found[off] = (region_kind, kind, payload, segments[0])
    return [(off,) + v for off, v in sorted(found.items())]

def build_slack_coord_row(off, region_kind, parsed):
    speed_kmh = parsed.get("speed_kmh")
    return {
        "date": parsed.get("date", ""), "utc_time": parsed.get("utc_time", ""),
        "status": parsed.get("status", ""),
        "latitude": f"{parsed['lat']:.6f}" if parsed.get("lat") is not None else "",
        "longitude": f"{parsed['lon']:.6f}" if parsed.get("lon") is not None else "",
        "speed_knots": parsed.get("speed_knots", ""),
        "speed_kmh": f"{speed_kmh:.3f}" if speed_kmh is not None else "",
        "track_deg": parsed.get("track_deg", ""),
        "magvar": parsed.get("magvar", ""), "magvar_dir": parsed.get("magvar_dir", ""),
        "mode": parsed.get("mode", ""), "checksum_ok": parsed.get("checksum_ok"),
        "status_valid": parsed.get("status_valid", ""),
        "trusted": parsed.get("trusted", ""),
        "parse_warnings": parsed.get("parse_warnings", ""),
        "slack_region": region_kind,
        "absolute_offset": f"0x{off:08X}",
        "sentence_type": parsed.get("sentence_type", ""),
        "raw_sentence": parsed.get("raw", ""),
    }

def build_slack_sensor_row(off, region_kind, payload, segment):
    row = {
        "slack_region": region_kind,
        "absolute_offset": f"0x{off:08X}",
        "subtype": payload.get("subtype", ""),
        "count": payload.get("count"), "scale": payload.get("scale"),
        "x_raw": payload.get("x_raw"), "y_raw": payload.get("y_raw"),
        "z_raw": payload.get("z_raw"),
        "x_g": payload.get("x_g"), "y_g": payload.get("y_g"), "z_g": payload.get("z_g"),
    }
    for i, v in enumerate(payload.get("raw_fields", [])):
        row[f"field_{i}"] = v
    row["raw"] = segment
    return row



def run_slack_carve(f, filesize, out_dir, args):
    """정상 경로 추출이 끝난 뒤, 컨테이너가 참조하지 않는 영역(free/skip Box,
    Box 사이 gap, 마지막 Box 뒤 꼬리)에서 GPS/G센서 레코드를 추가로 카빙한다.

    mp4_slack_carve.py 와 같은 로직이며(이 프로젝트 관례대로 import가 아니라
    각자 복사해서 들고 있음), 원본 파일은 절대 수정하지 않고 <out_dir>/slack/
    아래에만 결과를 남긴다.
    """
    regions, boxes = find_slack_regions(f, filesize)
    if not regions:
        info("[슬랙] free/skip Box도 gap도 없음 - 카빙 대상 없음")
        return None

    total = sum(e - s for _, s, e in regions)
    info(f"\n[슬랙] 컨테이너가 참조하지 않는 영역 {len(regions)}개 / {total:,} bytes "
         f"(전체의 {total / filesize * 100:.1f}%)")
    for kind, s, e in regions:
        info(f"  [{kind}] 0x{s:08X} ~ 0x{e:08X}  ({e - s:,} bytes)")

    coord_rows, sensor_rows, region_stats = [], [], []
    for kind, s, e in regions:
        f.seek(s)
        raw = f.read(e - s)
        recs = carve_region(raw, 0, len(raw), kind)
        n_gps = n_sen = 0
        for rel_off, region_kind, rkind, payload, segment in recs:
            off = s + rel_off
            if rkind == "gps_nmea":
                coord_rows.append(build_slack_coord_row(off, region_kind, payload))
                n_gps += 1
            else:
                sensor_rows.append(build_slack_sensor_row(off, region_kind, payload, segment))
                n_sen += 1
        region_stats.append({"region_kind": kind, "start": f"0x{s:08X}", "end": f"0x{e:08X}",
                             "size_bytes": e - s, "gps_records": n_gps,
                             "gsensor_records": n_sen})

    coord_rows.sort(key=lambda r: int(r["absolute_offset"], 16))
    sensor_rows.sort(key=lambda r: int(r["absolute_offset"], 16))

    dates = sorted({r["date"] for r in coord_rows if r["date"]})
    csfail = sum(1 for r in coord_rows if r["checksum_ok"] is False)
    info(f"  -> GPS {len(coord_rows)}건 / GSENSOR {len(sensor_rows)}건 "
         f"(checksum 실패 {csfail}건)")
    if dates:
        info(f"  -> 슬랙 GPS 기록일: {dates}")

    if not args.dry_run:
        slack_dir = os.path.join(out_dir, "slack")
        os.makedirs(slack_dir, exist_ok=True)
        write_csv(os.path.join(slack_dir, "slack_regions.csv"), region_stats)
        write_csv(os.path.join(slack_dir, "slack_coordinates.csv"), coord_rows)
        apply_gsensor_calibration(sensor_rows)
        write_csv(os.path.join(slack_dir, "slack_sensor_values.csv"), sensor_rows)
        with_coord = [r for r in coord_rows if r["latitude"]]
        if with_coord:
            with open(os.path.join(slack_dir, "slack_coordinates.txt"), "w",
                       encoding="utf-8") as fp:
                for i, row in enumerate(with_coord, start=1):
                    fp.write(f"{i}. {row['latitude']}, {row['longitude']}\n")
        with open(os.path.join(slack_dir, "README.txt"), "w", encoding="utf-8") as fp:
            fp.write(
                "이 폴더의 내용은 MP4 컨테이너가 '데이터로 쓰지 않는다'고 선언했거나\n"
                "아예 선언조차 하지 않은 영역(free/skip Box, Box 사이 gap, 마지막 Box 뒤\n"
                "꼬리 영역)에서 카빙한 것입니다.\n\n"
                "- 현재 녹화분이 아니라 같은 저장매체에 예전에 기록됐던 내용일 가능성이\n"
                "  높습니다. date 컬럼이 파일 자체 녹화일보다 과거면 그 근거가 됩니다.\n"
                "- sample table이 없는 영역이라 시간축(재생 시각)에 매핑할 수 없습니다.\n"
                "  절대 byte offset만 남깁니다.\n"
                "- 해석/검증 로직은 정상 추출분과 완전히 동일합니다(checksum 포함).\n"
                "  checksum_ok=False 인 행은 우연히 만들어진 바이트열일 수 있습니다.\n"
                "- 원본 파일은 수정하지 않았습니다.\n")

    return {"regions": len(regions), "slack_bytes": total,
            "gps": len(coord_rows), "gsensor": len(sensor_rows), "dates": dates}


ROUTE_RUNNERS = {
    ROUTE_FRAGMENTED: (run_fragmented, "Fragmented MP4 (moof/traf/trun) + Atext text track"),
    ROUTE_SAMPLETABLE: (run_sampletable, "non-fragmented MP4 (stsc/stsz/stco) + Atext text track"),
    ROUTE_UDTA_MAMT: (run_udta_mamt, "non-fragmented MP4, text track 없음 -> moov/udta/mamt NMEA"),
}


# ---- 여기부터: 이 파일 고유 - 파일 단위 처리 + CLI ----
def process_single_file(input_path, output_root, args):
    WARNINGS.clear()
    info("=" * 70)
    info(f"[처리 시작] {input_path}")

    filesize = os.path.getsize(input_path)
    if filesize == 0:
        warn("파일 크기가 0바이트")
        return {"ok": False, "reason": "empty file", "route": None}

    stem = os.path.splitext(os.path.basename(input_path))[0]
    # --probe-only 는 -o 없이도 돌 수 있어야 하므로 출력 경로 계산 자체를 미룬다.
    out_dir = os.path.join(output_root, stem) if output_root is not None else None
    # --dry-run / --probe-only 는 파일은 물론 폴더도 만들지 않는다.
    if out_dir is not None and not (args.dry_run or args.probe_only):
        os.makedirs(out_dir, exist_ok=True)

    with open(input_path, "rb") as f:
        probe = probe_container(f, filesize)
        info(f"  ftyp brand={probe['brand'] or '(없음)'} "
             f"compatible=[{', '.join(probe['compatible'])}]")
        info(f"  top-level={probe['top_types'][:8]}{'...' if len(probe['top_types']) > 8 else ''} "
             f"moof={probe['moof_count']}개")
        info(f"  moov trak handler={probe['handlers']}")

        route = probe["route"]
        if route is None:
            info(f"[SKIP] {input_path}: {probe['reason']}")
            return {"ok": False, "reason": probe["reason"], "route": None, "probe": probe}

        runner, desc = ROUTE_RUNNERS[route]
        info(f"  -> 판별 결과: [{route}] {desc}")

        if args.probe_only:
            return {"ok": True, "route": route, "probe": probe, "result": None}

        result = runner(f, filesize, out_dir, args)

        # 정상 경로가 끝난 뒤, 컨테이너가 참조하지 않는 영역을 추가로 카빙한다.
        # 정상 추출이 실패해도(route 처리 실패) 슬랙은 남아있을 수 있으므로 따로 돈다.
        slack = None
        if args.slack:
            try:
                slack = run_slack_carve(f, filesize, out_dir, args)
            except Exception as exc:
                warn(f"슬랙 카빙 중 예외 - 정상 추출 결과에는 영향 없음: {exc}")

    if result is None:
        return {"ok": False, "reason": f"{route} 처리 실패(위 경고 참조)", "route": route,
                "probe": probe}

    if not args.dry_run:
        with open(os.path.join(out_dir, "warnings.log"), "w", encoding="utf-8") as wf:
            for w_msg in WARNINGS:
                wf.write(w_msg + "\n")

    info("\n" + "-" * 70)
    info(f"[요약] {os.path.basename(input_path)}  (route={route})")
    info(f"  대상            : {result.get('track', '-')}")
    info(f"  sample/문장 수  : {result.get('samples', 0)}")
    info(f"  GPS 좌표        : {result.get('gps', 0)}"
         + (f" (그중 fix 없음 status=V: {result['no_fix']})" if result.get("no_fix") else ""))
    info(f"  GSENSOR         : {result.get('gsensor', 0)}")
    info(f"  VENDOR_RAW      : {result.get('vendor_raw', 0)}")
    if "generic" in result:
        info(f"  기타 세그먼트   : {result['generic']}")
    if "unparsed" in result:
        info(f"  미파싱(손상)    : {result['unparsed']}")
    if slack:
        info(f"  슬랙(미참조영역): {slack['slack_bytes']:,} bytes에서 "
             f"GPS {slack['gps']}건 / GSENSOR {slack['gsensor']}건"
             + (f", 기록일 {slack['dates']}" if slack["dates"] else ""))
    info(f"  경고            : {len(WARNINGS)}개")
    info(f"  출력            : {'(dry-run, 파일 미생성)' if args.dry_run else out_dir}")
    info("-" * 70)

    out = {"ok": True, "route": route, "probe": probe, "warnings": len(WARNINGS),
           "slack": slack}
    out.update(result)
    return out


def parse_args(argv):
    p = argparse.ArgumentParser(
        description="MP4 블랙박스 GPS/센서 메타데이터 통합 추출기. "
                    "파일 구조를 보고 fragmented / sample-table / udta-mamt 경로를 "
                    "자동으로 골라 처리한다.")
    p.add_argument("inputs", nargs="+", help="입력 MP4 파일 경로(들)")
    p.add_argument("-o", "--output", default=None,
                    help="결과를 저장할 루트 디렉터리 (--probe-only 일 때는 생략 가능)")
    p.add_argument("--dry-run", action="store_true",
                    help="파일을 만들지 않고 판별 + 파싱 + 요약만 출력")
    p.add_argument("--probe-only", action="store_true",
                    help="컨테이너 구조 판별 결과만 출력하고 추출은 하지 않음")
    p.add_argument("--max-print", type=int, default=0,
                    help="콘솔에 상세 출력할 sample 개수(루트 A). CSV에는 항상 전부 기록됨")
    p.add_argument("--track-id", type=int, default=None,
                    help="루트 A: text Track이 여러 개일 때 쓸 tkhd.track_ID")
    p.add_argument("--track", action="append", type=int, default=None,
                    help="루트 B: 처리할 text Track 번호(전체 trak 기준 1-based). 여러 번 지정 가능")
    p.add_argument("--extract", action="store_true",
                    help="루트 B: Sample 원본도 .bin으로 저장")
    # 슬랙 카빙은 기본 꺼둔다. 슬랙에서 나오는 건 "예전 녹화분"이라 지금 영상의
    # 재생 시각에 매핑할 수 없어서, 영상과 동기화된 시각화에는 쓸 수 없다.
    # 게다가 free 영역이 수십 MB라 스캔 비용도 크다(실측: 2파일 0.30s -> 2.13s).
    # 과거 주행 이력을 캐는 포렌식 목적일 때만 켜면 된다.
    p.add_argument("--slack", action="store_true",
                    help="슬랙(free/skip Box, gap, 꼬리)에서 과거 녹화분 GPS/G센서를 "
                         "추가로 카빙한다. 기본은 안 함 - 현재 영상 재생 시각에 매핑할 수 "
                         "없는 데이터라 시각화에는 못 쓴다")
    p.add_argument("--debug", action="store_true", help="Box/Tfhd/Tfdt/Trun 상세 디버그 출력")
    args = p.parse_args(argv)
    if args.output is None and not args.probe_only:
        p.error("-o/--output 은 --probe-only 가 아닐 때 반드시 필요합니다")
    return args


def main(argv=None):
    global DEBUG
    args = parse_args(sys.argv[1:] if argv is None else argv)
    DEBUG = args.debug
    if not (args.dry_run or args.probe_only):
        os.makedirs(args.output, exist_ok=True)

    results = []
    for input_path in args.inputs:
        if not os.path.isfile(input_path):
            info(f"[SKIP] {input_path}: 파일을 찾을 수 없음")
            results.append((input_path, {"ok": False, "reason": "file not found", "route": None}))
            continue
        try:
            r = process_single_file(input_path, args.output, args)
        except Exception as exc:
            warn(f"예외 발생으로 처리 중단: {exc}")
            r = {"ok": False, "reason": f"exception: {exc}", "route": None}
        results.append((input_path, r))

    info("\n" + "=" * 70)
    info("[전체 요약]")
    for input_path, r in results:
        name = os.path.basename(input_path)
        if r.get("ok"):
            sl = r.get("slack")
            extra = (f"  |  슬랙 GPS={sl['gps']} GSENSOR={sl['gsensor']}" if sl else "")
            info(f"  OK   {name:38} route={r['route']:18} "
                 f"GPS={r.get('gps', 0)} GSENSOR={r.get('gsensor', 0)} "
                 f"VENDOR={r.get('vendor_raw', 0)}" + extra)
        else:
            info(f"  SKIP {name:38} {r.get('reason')}")
    info("=" * 70)


if __name__ == "__main__":
    main()
