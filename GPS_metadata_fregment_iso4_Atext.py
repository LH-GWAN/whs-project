
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

KEPT_KINDS = {"gsensor", "gps_nmea"}

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
    r"^gsensor(?P<subtype>[A-Za-z0-9]*)\s*,\s*(?P<rest>.*)$", re.IGNORECASE)

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
    if len(csum) < 2 or not re.fullmatch(r"[0-9A-Fa-f]{2}.*", csum):
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
    lat = _dm_to_decimal(fields[3].strip(), 2, fields[4].strip().upper(), "S")
    lon = _dm_to_decimal(fields[5].strip(), 3, fields[6].strip().upper(), "W")
    if lat is None or lon is None:
        return None
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

def split_segments(text):
    return [seg for seg in (s.strip() for s in text.split(";")) if seg]

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
    return (f"    [GPS {payload['sentence_type']}] "
            f"lat={payload['lat']:.6f} lon={payload['lon']:.6f} "
            f"date={payload.get('date','')} time={payload.get('utc_time','')} "
            f"speed_kmh={f'{speed_kmh:.3f}' if speed_kmh is not None else '-'} "
            f"checksum_ok={payload.get('checksum_ok')} trusted={payload.get('trusted')}\n"
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
                    "date": payload.get("date", ""), "utc_time": payload.get("utc_time", ""),
                    "status": payload.get("status", ""),
                    "latitude": f"{payload['lat']:.6f}", "longitude": f"{payload['lon']:.6f}",
                    "speed_knots": payload.get("speed_knots", ""),
                    "speed_kmh": f"{speed_kmh:.3f}" if speed_kmh is not None else "",
                    "track_deg": payload.get("track_deg", ""),
                    "sentence_type": payload["sentence_type"],
                    "checksum_ok": payload.get("checksum_ok"), "trusted": payload.get("trusted"),
                    "raw_sentence": payload["raw"],
                })
            elif kind == "gsensor":
                gsensor_payload = payload
                sensor_rows.append({
                    "sample": s.sample_index,
                    "start_time_sec": f"{s.start_time:.3f}" if s.start_time is not None else "",
                    "end_time_sec": f"{s.end_time:.3f}" if s.end_time is not None else "",
                    "absolute_offset": f"0x{s.file_offset:08X}" if s.file_offset >= 0 else "",
                    "subtype": payload.get("subtype", ""),
                    "x_raw": payload.get("x_raw"), "y_raw": payload.get("y_raw"), "z_raw": payload.get("z_raw"),
                    "scale": payload.get("scale"),
                    "x_g": payload.get("x_g"), "y_g": payload.get("y_g"), "z_g": payload.get("z_g"),
                })

        if gps_payload is not None:
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
            "latitude": f"{gps_payload['lat']:.6f}" if gps_payload else "",
            "longitude": f"{gps_payload['lon']:.6f}" if gps_payload else "",
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
        })

    write_csv(os.path.join(track_dir, "index.csv"), index_rows)
    write_csv(os.path.join(track_dir, "coordinates.csv"), coord_rows)
    write_csv(os.path.join(track_dir, "sensor_values.csv"), sensor_rows)
    write_csv(os.path.join(track_dir, "timeline.csv"), timeline_rows)
    with open(os.path.join(track_dir, "coordinates.txt"), "w", encoding="utf-8") as f:
        for i, row in enumerate(coord_rows, start=1):
            f.write(f"{i}. {row['latitude']}, {row['longitude']}\n")

    save_track_table(out_dir, all_tracks, target_track_id, len(samples))

    with open(os.path.join(out_dir, "warnings.log"), "w", encoding="utf-8") as f:
        for w_msg in WARNINGS:
            f.write(w_msg + "\n")

def parse_args(argv):
    p = argparse.ArgumentParser(
        description="Fragmented MP4의 text(Atext) Track에서 GPRMC/GPGGA GPS 좌표와 "
                     "GSENSOR 값을, 영상 재생 시간(초)과 함께 추출한다.")
    p.add_argument("input", help="입력 fMP4 파일 경로")
    p.add_argument("output", help="CSV 결과를 저장할 출력 디렉터리")
    p.add_argument("--list-tracks", action="store_true", help="Track 목록만 출력하고 종료")
    p.add_argument("--dry-run", action="store_true", help="CSV 파일을 만들지 않고 콘솔 출력만 수행")
    p.add_argument("--track-id", type=int, default=None,
                    help="text handler_type Track이 여러 개일 때 사용할 track_ID 지정 "
                         "(미지정시 첫 번째 text Track 자동 사용)")
    p.add_argument("--max-print", type=int, default=None,
                    help="콘솔에 출력할 최대 sample 개수 (CSV에는 항상 전부 기록됨)")
    p.add_argument("--debug", action="store_true", help="Box/Track/Tfhd/Tfdt/Trun 상세 디버그 출력")
    return p.parse_args(argv)

def main(argv=None):
    global DEBUG
    WARNINGS.clear()
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.debug:
        DEBUG = True

    filesize = os.path.getsize(args.input)
    if filesize == 0:
        print("입력 파일 크기가 0입니다.", file=sys.stderr)
        sys.exit(1)

    with open(args.input, "rb") as f:
        top_boxes = scan_top_level(f, filesize)

        moov_boxes = find_all(top_boxes, b"moov")
        moof_boxes = find_all(top_boxes, b"moof")
        mdat_boxes = find_all(top_boxes, b"mdat")
        mdat_ranges = [(b.payload_start, b.end) for b in mdat_boxes]
        is_fragmented = len(moof_boxes) > 0

        info("=" * 60)
        info("MP4 FILE")
        info("=" * 60)
        info(f"파일               : {args.input}")
        info(f"크기               : {filesize:,} bytes")
        info(f"Fragmented MP4     : {'예 (moof ' + str(len(moof_boxes)) + '개 발견)' if is_fragmented else '아니오'}")

        info("\nTop-level Boxes")
        for i, b in enumerate(top_boxes):
            info(f"  [{i}] {b.box_type.decode('ascii', errors='replace')} "
                 f"offset=0x{b.start:08X} size={b.size}")

        ftyp_box = find_box(top_boxes, b"ftyp")
        if ftyp_box:
            ftyp = parse_ftyp(f, ftyp_box)
            if ftyp:
                info("\nftyp")
                info(f"  major_brand       : {ftyp['major_brand'].decode('ascii', errors='replace')}")
                compat_str = ", ".join(b.decode('ascii', errors='replace')
                                        for b in ftyp['compatible_brands'])
                info(f"  compatible_brands : {compat_str}")

        if not moov_boxes:
            print("moov Box를 찾지 못했습니다 - 처리할 수 없습니다.", file=sys.stderr)
            sys.exit(1)
        if not is_fragmented:
            warn("moof Box가 하나도 없음 - Fragmented MP4가 아닌 것으로 보입니다. "
                 "일반 MP4는 GPS_metadata_mp4_pvc1_Atext.py를 사용하세요.")

        text_tracks, trex_defaults, all_tracks = parse_moov(f, moov_boxes[0])
        if not text_tracks:
            print("handler_type == 'text' 인 Track을 찾지 못했습니다.", file=sys.stderr)
            sys.exit(1)

        if args.track_id is not None:
            if args.track_id not in text_tracks:
                print(f"--track-id {args.track_id}는 text Track이 아니거나 존재하지 않습니다. "
                      f"사용 가능: {sorted(text_tracks)}", file=sys.stderr)
                sys.exit(1)
            target_track_id = args.track_id
        else:
            target_track_id = sorted(text_tracks)[0]

        target = text_tracks[target_track_id]
        info(f"\n대상 text Track: track_ID={target.track_id} timescale={target.timescale}")

        if args.list_tracks:
            info("\n--list-tracks 모드: 추출 없이 종료")
            return

        if not is_fragmented:
            return

        track_dts_state = {}
        out_samples = []
        for moof_index, moof_box in enumerate(moof_boxes):
            parse_moof(f, moof_box, moof_index, target_track_id, target.timescale,
                       trex_defaults, filesize, mdat_ranges, track_dts_state, out_samples)

        info(f"\n총 {len(out_samples)}개 text Track sample 발견\n")

        printed = 0
        for s in out_samples:
            if args.max_print is not None and printed >= args.max_print:
                break
            print_sample(s)
            printed += 1

        save_outputs(args.output, out_samples, all_tracks, target_track_id, dry_run=args.dry_run)

        gps_count = sum(1 for s in out_samples for k, _ in s.parsed_segments if k == "gps_nmea")
        gsensor_count = sum(1 for s in out_samples for k, _ in s.parsed_segments if k == "gsensor")
        error_count = sum(1 for s in out_samples if s.error)

        info("\n" + "=" * 60)
        info("[요약]")
        info(f"text Track (track_ID={target_track_id}) sample 총 개수 : {len(out_samples)}")
        info(f"GPS(GPRMC/GPGGA) 좌표 개수                            : {gps_count}")
        info(f"GSENSOR 레코드 개수                                   : {gsensor_count}")
        info(f"오류(offset/size 계산 실패 등) sample 개수            : {error_count}")
        if out_samples:
            valid_start = [s.start_time for s in out_samples if s.start_time is not None]
            valid_end = [s.end_time for s in out_samples if s.end_time is not None]
            if valid_start and valid_end:
                info(f"시간 범위                                             : "
                     f"{min(valid_start):.3f} sec ~ {max(valid_end):.3f} sec")
        info(f"경고 총 개수                                          : {len(WARNINGS)} "
             f"({'dry-run이라 파일 미생성' if args.dry_run else os.path.join(args.output, 'warnings.log') + ' 참조'})")
        info("=" * 60)

if __name__ == "__main__":
    main()
