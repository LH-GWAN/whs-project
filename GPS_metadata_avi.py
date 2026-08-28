import argparse
import csv
import math
import mmap
import os
import re
import shutil #기존 출력 디렉터리 삭제용
import struct
import sys
from dataclasses import dataclass, field

if sys.stdout.encoding is None or sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except AttributeError:
        pass


SELECT_MODE = "auto_non_av"

SELECT_FCCTYPES = {b"txts"}
SELECT_INDICES = {2, 3}
SELECT_CHUNK_IDS = {b"02st", b"03st"}

STANDARD_AV_FCCTYPES = {b"vids", b"auds"}

LABEL_OVERRIDE = {
}

ANCHOR_MOVI_FOURCC_OFFSET = 0x1008
ANCHOR_SAMPLE_CHUNK_ID = b"02st"
ANCHOR_SAMPLE_IDX_OFFSET = 0x04
ANCHOR_EXPECTED_ABS_CHUNK_OFFSET = 0x100C

SAMPLE_ENTRIES_FOR_BASE_DETECT = 8
MAX_IDX1_ENTRIES = 5_000_000

DECODE_MIN_FRACTION = 0.8
EMBEDDED_NMEA_RE = re.compile(rb"\$?([A-Z]{2}(?:RMC|GGA)[ -~]*)")
FLOAT_VECTOR_MIN_N = 2
FLOAT_VECTOR_MAX_N = 8
FLOAT_VECTOR_MAX_ABS = 50.0

WARNINGS = []


def warn(msg):
    WARNINGS.append(msg)
    print(f"[WARN] {msg}")


def info(msg):
    print(msg)


def assert_riff_file(path):
    with open(path, "rb") as f:
        magic = f.read(4)
    if magic != b"RIFF":
        print(f"이 스크립트는 AVI(RIFF) 파일 전용입니다. "
              f"'{path}' 의 앞 4바이트가 {magic!r} 로 RIFF가 아닙니다 - 종료.", file=sys.stderr)
        sys.exit(1)


@dataclass
class ChunkInfo:
    ck_id: bytes
    ck_size: int
    pos: int
    data_start: int
    data_end: int
    is_list: bool
    list_type: bytes = None
    content_start: int = None
    content_end: int = None
    truncated: bool = False


@dataclass
class StreamInfo:
    index: int
    fcc_type: bytes = None
    fcc_handler: bytes = None
    name: str = None
    has_strf: bool = False
    has_indx: bool = False
    observed_chunk_ids: set = field(default_factory=set)
    role: str = "UNKNOWN"
    selected: bool = False
    unmapped: bool = False


def iter_chunks(mm, start, end, clamp_top_level=False):
    pos = start
    filesize = len(mm)
    while pos + 8 <= end:
        ck_id = bytes(mm[pos:pos + 4])
        ck_size = struct.unpack_from("<I", mm, pos + 4)[0]
        data_start = pos + 8
        data_end = data_start + ck_size
        truncated = False

        if data_end > end:
            if clamp_top_level and data_end <= filesize:
                warn(f"top-level chunk {ck_id!r}@0x{pos:X} declared size가 부모 경계를 "
                     f"넘어섬(end=0x{end:X}) - 파일 크기 기준으로 clamp")
                data_end = min(data_end, filesize)
                truncated = True
            else:
                warn(f"chunk {ck_id!r}@0x{pos:X} size가 부모 경계(0x{end:X})를 넘어섬 - "
                     f"이 레벨 순회 중단")
                return
        if data_end > filesize:
            warn(f"chunk {ck_id!r}@0x{pos:X} 가 파일 크기를 넘어섬 - 이 레벨 순회 중단")
            return

        if ck_id in (b"RIFF", b"LIST"):
            if data_start + 4 > filesize:
                warn(f"LIST/RIFF {ck_id!r}@0x{pos:X} listType 읽기 실패 - 순회 중단")
                return
            list_type = bytes(mm[data_start:data_start + 4])
            info_chunk = ChunkInfo(
                ck_id=ck_id, ck_size=ck_size, pos=pos,
                data_start=data_start, data_end=data_end, is_list=True,
                list_type=list_type, content_start=data_start + 4,
                content_end=data_end, truncated=truncated,
            )
        else:
            info_chunk = ChunkInfo(
                ck_id=ck_id, ck_size=ck_size, pos=pos,
                data_start=data_start, data_end=data_end, is_list=False,
                truncated=truncated,
            )

        yield info_chunk

        if truncated:
            return
        pos = data_end + (ck_size & 1)


def find_top_level_sections(mm):
    filesize = len(mm)
    hdrl = None
    movi_list = []
    idx1 = None
    avix_count = 0

    if filesize < 12 or bytes(mm[0:4]) != b"RIFF":
        warn("파일이 RIFF로 시작하지 않음 - 구조적 파싱 불가, fallback 사용")
        return None, [], None, 0

    for top in iter_chunks(mm, 0, filesize, clamp_top_level=True):
        if top.ck_id != b"RIFF" or not top.is_list:
            continue
        form_type = top.list_type
        if form_type == b"AVIX":
            avix_count += 1
        elif form_type != b"AVI ":
            warn(f"알 수 없는 RIFF formType {form_type!r}@0x{top.pos:X} - 건너뜀")
            continue

        movi_found_in_this_riff = False
        for child in iter_chunks(mm, top.content_start, top.content_end):
            if child.is_list and child.list_type == b"hdrl" and hdrl is None and form_type == b"AVI ":
                hdrl = child
            elif child.is_list and child.list_type == b"movi":
                if movi_found_in_this_riff:
                    warn(f"RIFF({form_type!r})@0x{top.pos:X} 안에서 movi가 두 번째로 발견됨 "
                         f"(@0x{child.pos:X}) - AVI 스펙상 RIFF 하나엔 movi가 하나뿐이어야 하므로 "
                         f"이건 압축 페이로드 안에서 우연히 매치된 것으로 보고 무시함")
                    continue
                movi_list.append(child)
                movi_found_in_this_riff = True
            elif (not child.is_list) and child.ck_id == b"idx1" and idx1 is None and form_type == b"AVI ":
                idx1 = child

    return hdrl, movi_list, idx1, avix_count


def find_movi_fallback(mm):
    pos = mm.find(b"movi")
    if pos < 0:
        return None
    warn("구조 파싱 실패, movi 바이트 스캔 fallback 사용")
    return pos


def find_idx1_fallback(mm):
    filesize = len(mm)
    search_end = filesize
    while True:
        pos = mm.rfind(b"idx1", 0, search_end)
        if pos < 0:
            return None
        if pos + 8 <= filesize:
            size = struct.unpack_from("<I", mm, pos + 4)[0]
            if size % 16 == 0 and pos + 8 + size <= filesize:
                warn("구조 파싱 실패, idx1 바이트 스캔(rfind) fallback 사용 "
                     f"(size 16배수 검증 통과, @0x{pos:X})")
                return pos, size
        search_end = pos
        if search_end <= 0:
            return None


def parse_hdrl(mm, hdrl_chunk):
    dw_streams = None
    streams = []

    for child in iter_chunks(mm, hdrl_chunk.content_start, hdrl_chunk.content_end):
        if child.ck_id == b"avih" and not child.is_list:
            if child.ck_size >= 28:
                dw_streams = struct.unpack_from("<I", mm, child.data_start + 24)[0]
        elif child.is_list and child.list_type == b"strl":
            idx = len(streams)
            si = StreamInfo(index=idx)
            for sc in iter_chunks(mm, child.content_start, child.content_end):
                if sc.ck_id == b"strh" and not sc.is_list:
                    if sc.ck_size >= 8:
                        si.fcc_type = bytes(mm[sc.data_start:sc.data_start + 4])
                        si.fcc_handler = bytes(mm[sc.data_start + 4:sc.data_start + 8])
                elif sc.ck_id == b"strf" and not sc.is_list:
                    si.has_strf = True
                elif sc.ck_id == b"strn" and not sc.is_list:
                    raw = bytes(mm[sc.data_start:sc.data_end])
                    raw = raw.split(b"\x00", 1)[0]
                    try:
                        si.name = raw.decode("utf-8")
                    except UnicodeDecodeError:
                        si.name = raw.decode("latin1", errors="replace")
                elif sc.ck_id == b"indx" and not sc.is_list:
                    si.has_indx = True
            streams.append(si)

    if dw_streams is not None and dw_streams != len(streams):
        warn(f"avih.dwStreams={dw_streams} 인데 실제 발견된 strl 개수={len(streams)} - 불일치")

    return dw_streams, streams


def parse_idx1(mm, idx1_start, idx1_size):
    entries = []
    if idx1_start < 0 or idx1_start > len(mm):
        warn(f"idx1 시작 위치(0x{idx1_start:X})가 파일 범위를 벗어남")
        return entries
    available = max(0, len(mm) - idx1_start)
    bounded_size = min(idx1_size, available)
    if bounded_size != idx1_size:
        warn(f"idx1 선언 size(0x{idx1_size:X})가 파일 경계를 넘어 {bounded_size}바이트로 제한")
    usable = bounded_size - (bounded_size % 16)
    if usable != bounded_size:
        warn(f"idx1 size(0x{bounded_size:X})가 16의 배수가 아님 - 마지막 잘린 엔트리 버림")
    n = min(usable // 16, MAX_IDX1_ENTRIES)
    if usable // 16 > MAX_IDX1_ENTRIES:
        warn(f"idx1 entry 개수가 안전 한도({MAX_IDX1_ENTRIES})를 넘어 이후 entry는 분석하지 않음")
    for i in range(n):
        off = idx1_start + i * 16
        entry = bytes(mm[off:off + 16])
        if len(entry) < 16:
            break
        chunk_id = entry[0:4]
        flags = int.from_bytes(entry[4:8], "little")
        idx_offset = int.from_bytes(entry[8:12], "little")
        length = int.from_bytes(entry[12:16], "little")
        entries.append({
            "chunk_id": chunk_id, "flags": flags,
            "idx_offset": idx_offset, "length": length,
        })
    return entries


def stream_index_from_chunk_id(chunk_id):
    """AVI chunk id의 앞 두 자리(00~99)를 10진 stream number로 해석한다."""
    if not isinstance(chunk_id, (bytes, bytearray)) or len(chunk_id) < 2:
        return None
    prefix = bytes(chunk_id[:2])
    if not all(48 <= b <= 57 for b in prefix):
        return None
    try:
        return int(prefix.decode("ascii"), 10)
    except (UnicodeDecodeError, ValueError):
        return None

def build_stream_table(dw_streams, streams, idx1_entries):
    by_index = {s.index: s for s in streams}
    observed_stream_nums = set()

    for e in idx1_entries:
        cid = e["chunk_id"]
        if cid in (b"idx1", b"JUNK", b"rec "):
            continue
        sidx = stream_index_from_chunk_id(cid)
        if sidx is None:
            continue
        observed_stream_nums.add(sidx)
        if sidx in by_index:
            by_index[sidx].observed_chunk_ids.add(cid)
        else:
            si = StreamInfo(index=sidx, unmapped=True)
            si.observed_chunk_ids.add(cid)
            by_index[sidx] = si
            warn(f"idx1에 stream #{sidx} (chunk_id={cid!r}) 존재하지만 "
                 f"hdrl 스트림 테이블엔 없음 - unmapped 스트림으로 추가")

    for s in by_index.values():
        if s.fcc_type in STANDARD_AV_FCCTYPES:
            s.role = "VIDEO (std)" if s.fcc_type == b"vids" else "AUDIO (std)"
        elif s.fcc_type is not None:
            s.role = "DATA"
        else:
            s.role = "DATA (unknown type)"

    table = sorted(by_index.values(), key=lambda s: s.index)
    return table


def resolve_targets(stream_table, select_mode, select_fcctypes, select_indices, select_chunk_ids):
    selected_chunk_ids = set()
    reason = ""

    if select_mode == "auto_non_av":
        for s in stream_table:
            if s.fcc_type not in STANDARD_AV_FCCTYPES:
                s.selected = True
                selected_chunk_ids |= s.observed_chunk_ids
        reason = "표준 vids/auds 를 제외한 모든 스트림"
    elif select_mode == "by_fcctype":
        for s in stream_table:
            if s.fcc_type in select_fcctypes:
                s.selected = True
                selected_chunk_ids |= s.observed_chunk_ids
        reason = f"fccType in {select_fcctypes!r}"
    elif select_mode == "by_index":
        for s in stream_table:
            if s.index in select_indices:
                s.selected = True
                selected_chunk_ids |= s.observed_chunk_ids
        reason = f"stream index in {select_indices!r}"
    elif select_mode == "explicit":
        for s in stream_table:
            if s.observed_chunk_ids & select_chunk_ids:
                s.selected = True
                selected_chunk_ids |= (s.observed_chunk_ids & select_chunk_ids)
        reason = f"explicit chunk ids {select_chunk_ids!r}"
    else:
        raise ValueError(f"알 수 없는 SELECT_MODE: {select_mode}")

    for s in stream_table:
        if s.selected:
            s.role += " -> selected"

    info(f"[선택] SELECT_MODE={select_mode} 기준: {reason} "
         f"-> 대상 chunk id {sorted(selected_chunk_ids)!r}")
    return selected_chunk_ids


def detect_base_offset(mm, movi_fourcc_pos, idx1_entries, sample_n=SAMPLE_ENTRIES_FOR_BASE_DETECT):
    filesize = len(mm)
    candidates = {}
    if movi_fourcc_pos is not None:
        candidates["A(movi FourCC 위치)"] = movi_fourcc_pos
        candidates["B(movi 데이터 시작=A+4)"] = movi_fourcc_pos + 4
    candidates["C(절대 offset, base=0)"] = 0

    sample = idx1_entries[:sample_n] if idx1_entries else []
    scores = {}
    for label, base in candidates.items():
        score = 0
        for e in sample:
            off = base + e["idx_offset"]
            if 0 <= off and off + 4 <= filesize and bytes(mm[off:off + 4]) == e["chunk_id"]:
                score += 1
        scores[label] = score

    info("[Base offset 후보 매치 점수]")
    for label, score in scores.items():
        info(f"  {label}: {score}/{len(sample)}")

    if not scores or max(scores.values()) == 0:
        warn("어떤 base 후보도 idx1 엔트리와 일치하지 않음 - base 불확실, 기본값(A) 사용")
        chosen_label = "A(movi FourCC 위치)" if movi_fourcc_pos is not None else "C(절대 offset, base=0)"
        chosen_base = candidates.get(chosen_label, 0)
        return chosen_base, chosen_label, scores, True

    chosen_label = max(scores, key=scores.get)
    chosen_base = candidates[chosen_label]
    return chosen_base, chosen_label, scores, False


def detect_opendml(mm, movi_list, avix_count, stream_table):
    has_indx = any(s.has_indx for s in stream_table)
    has_rec = False
    for movi in movi_list[:1]:
        for child in iter_chunks(mm, movi.content_start, movi.content_end):
            if child.is_list and child.list_type == b"rec ":
                has_rec = True
                break

    if avix_count > 0:
        warn(f"OpenDML AVIX RIFF {avix_count}개 발견 - 확장 movi의 데이터는 "
             f"이 버전에서 별도 처리하지 않음(일부만 추출됐을 수 있음)")
    if has_indx:
        warn("strl 내 indx(super-index) 존재 - OpenDML 확장 인덱스, 이 버전은 idx1만 사용")
    if has_rec:
        warn("movi 내부 'rec ' LIST 그룹핑 발견 - idx1 offset 기반 추출은 영향 없이 동작하나 참고")

    return {
        "avix_count": avix_count,
        "has_indx": has_indx,
        "has_rec": has_rec,
        "movi_count": len(movi_list),
    }


def validate_chunk(mm, chunk_offset, entry):
    filesize = len(mm)
    reasons = []

    if chunk_offset < 0 or chunk_offset + 8 > filesize:
        reasons.append("OUT_OF_RANGE")
        return reasons, None, None

    actual_id = bytes(mm[chunk_offset:chunk_offset + 4])
    if actual_id != entry["chunk_id"]:
        reasons.append("ID_MISMATCH")

    header_size = struct.unpack_from("<I", mm, chunk_offset + 4)[0]
    if header_size != entry["length"]:
        reasons.append("SIZE_MISMATCH")

    payload_offset = chunk_offset + 8
    payload_end = payload_offset + entry["length"]
    if payload_end > filesize:
        reasons.append("OUT_OF_RANGE")

    if not reasons:
        reasons.append("OK")

    return reasons, payload_offset, header_size


def hex_preview_lines(payload, n=32):
    chunk = payload[:n]
    hex_part = " ".join(f"{b:02X}" for b in chunk)
    ascii_part = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
    return hex_part, ascii_part


def sanitize_label(text):
    text = re.sub(r"[^A-Za-z0-9_]+", "_", text).strip("_")
    return text or "stream"


def _meaningful_ascii(raw_bytes):
    if not raw_bytes:
        return None
    stripped = raw_bytes.rstrip(b"\x00").strip()
    if not stripped:
        return None
    try:
        text = stripped.decode("ascii")
    except UnicodeDecodeError:
        return None
    if not all(32 <= ord(c) < 127 for c in text):
        return None
    return text


def make_label(stream_info):
    primary_chunk_id = None
    if stream_info.observed_chunk_ids:
        primary_chunk_id = sorted(stream_info.observed_chunk_ids)[0]

    if primary_chunk_id in LABEL_OVERRIDE:
        return LABEL_OVERRIDE[primary_chunk_id]

    handler_text = _meaningful_ascii(stream_info.fcc_handler)

    if stream_info.name:
        base = stream_info.name
    elif handler_text:
        base = handler_text
    elif stream_info.fcc_type:
        base = stream_info.fcc_type.decode("ascii", errors="replace").strip()
    elif primary_chunk_id:
        base = primary_chunk_id.decode("ascii", errors="replace")
    else:
        base = f"stream{stream_info.index}"

    sanitized = sanitize_label(base)
    display_label = sanitized.upper()
    dir_label = display_label
    prefix = sanitized.lower()
    return display_label, dir_label, prefix


def make_unique_labels(selected_streams):
    labels = {}
    dir_label_counts = {}
    for s in selected_streams:
        labels[s.index] = make_label(s)
        dir_label_counts[labels[s.index][1]] = dir_label_counts.get(labels[s.index][1], 0) + 1

    colliding = {d for d, c in dir_label_counts.items() if c > 1}
    if colliding:
        for s in selected_streams:
            display_label, dir_label, prefix = labels[s.index]
            if dir_label in colliding:
                new_display = f"{display_label}_S{s.index}"
                new_dir = f"{dir_label}_S{s.index}"
                new_prefix = f"{prefix}_s{s.index}"
                warn(f"라벨 충돌 감지: '{dir_label}' 을(를) stream #{s.index}에서 "
                     f"'{new_dir}' 로 재명명 (동일 fccType/이름 없음으로 인한 충돌 방지)")
                labels[s.index] = (new_display, new_dir, new_prefix)

    return labels


def looks_like_text_record(payload, min_text_len=4):
    """NUL padding을 허용하되 본문에는 printable ASCII와 CR/LF/TAB만 허용한다."""
    nul_idx = payload.find(b"\x00")
    text_part = payload if nul_idx == -1 else payload[:nul_idx]
    pad_part = b"" if nul_idx == -1 else payload[nul_idx:]
    text_part = text_part.rstrip(b"\r\n\t ")

    if len(text_part) < min_text_len:
        return False
    if not all((32 <= b < 127) or b in (9, 10, 13) for b in text_part):
        return False
    if any(b != 0 for b in pad_part):
        return False
    return True

def decode_text_record(payload):
    nul_idx = payload.find(b"\x00")
    text_part = payload if nul_idx == -1 else payload[:nul_idx]
    return text_part.rstrip(b"\r\n\t ").decode("ascii", errors="replace")

def find_embedded_nmea_text(payload):
    m = EMBEDDED_NMEA_RE.search(payload)
    if not m:
        return None
    return m.group(1).decode("ascii", errors="replace")


def try_float_vector(payload):
    if len(payload) == 0 or len(payload) % 4 != 0:
        return None
    n = len(payload) // 4
    if n < FLOAT_VECTOR_MIN_N or n > FLOAT_VECTOR_MAX_N:
        return None
    try:
        vals = struct.unpack(f"<{n}f", payload)
    except struct.error:
        return None
    if not all(math.isfinite(v) and abs(v) <= FLOAT_VECTOR_MAX_ABS for v in vals):
        return None
    return vals


def classify_payload(payload):
    nmea_line = find_embedded_nmea_text(payload)
    if nmea_line:
        return "nmea_text", nmea_line
    if looks_like_text_record(payload):
        return "generic_text", decode_text_record(payload)
    fvec = try_float_vector(payload)
    if fvec is not None:
        return "float_vector", fvec
    return "binary", None


def nmea_checksum_ok(sentence):
    """NMEA checksum을 검증한다. checksum이 없으면 None, 형식이 잘못되면 False."""
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
    if not (0 <= deg <= max_deg):
        return None
    if deg == max_deg and minutes != 0:
        return None
    decimal = deg + minutes / 60.0
    if hemisphere == neg_hemi:
        decimal = -decimal
    return decimal

def format_nmea_date(ddmmyy):
    """ddmmyy를 ISO 날짜로 변환. 80~99는 19xx, 00~79는 20xx로 해석한다."""
    if not ddmmyy or len(ddmmyy) != 6 or not ddmmyy.isdigit():
        return ddmmyy
    dd, mm, yy = int(ddmmyy[0:2]), int(ddmmyy[2:4]), int(ddmmyy[4:6])
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
        hh = int(hhmmss[0:2]); mm = int(hhmmss[2:4]); ss = float(hhmmss[4:])
    except (TypeError, ValueError, OverflowError):
        return hhmmss
    if not (0 <= hh <= 23 and 0 <= mm <= 59 and 0 <= ss < 60):
        return hhmmss
    sec_text = hhmmss[4:]
    return f"{hh:02d}:{mm:02d}:{sec_text}"

def parse_rmc(fields):
    if len(fields) < 10:
        return None
    lat_str, lat_hemi = fields[3].strip(), fields[4].strip().upper()
    lon_str, lon_hemi = fields[5].strip(), fields[6].strip().upper()
    lat = _dm_to_decimal(lat_str, 2, lat_hemi, "S")
    lon = _dm_to_decimal(lon_str, 3, lon_hemi, "W")
    if lat is None or lon is None:
        return None

    parse_warnings = []
    speed_knots = fields[7].strip() if len(fields) > 7 else ""
    speed_kmh = None
    if speed_knots:
        try:
            speed = float(speed_knots)
            if math.isfinite(speed) and speed >= 0:
                speed_kmh = speed * 1.852
            else:
                parse_warnings.append("invalid_speed")
        except (ValueError, OverflowError):
            parse_warnings.append("invalid_speed")

    status = fields[2].strip().upper() if len(fields) > 2 else ""
    status_valid = status == "A"
    if status not in {"A", "V", ""}:
        parse_warnings.append("invalid_status")

    magvar_dir = fields[11].strip().upper() if len(fields) > 11 else ""
    if magvar_dir and magvar_dir not in {"E", "W"}:
        parse_warnings.append("invalid_magvar_dir")

    mode = fields[12].strip().upper() if len(fields) > 12 else ""
    return {
        "lat": lat, "lon": lon,
        "date": format_nmea_date(fields[9].strip() if len(fields) > 9 else ""),
        "utc_time": format_nmea_time(fields[1].strip()), "status": status,
        "status_valid": status_valid,
        "speed_knots": speed_knots, "speed_kmh": speed_kmh,
        "track_deg": fields[8].strip() if len(fields) > 8 else "",
        "magvar": fields[10].strip() if len(fields) > 10 else "",
        "magvar_dir": magvar_dir, "mode": mode,
        "parse_warnings": ";".join(parse_warnings),
    }

def parse_gga(fields):
    if len(fields) < 10:
        return None
    lat_str, lat_hemi = fields[2].strip(), fields[3].strip().upper()
    lon_str, lon_hemi = fields[4].strip(), fields[5].strip().upper()
    lat = _dm_to_decimal(lat_str, 2, lat_hemi, "S")
    lon = _dm_to_decimal(lon_str, 3, lon_hemi, "W")
    if lat is None or lon is None:
        return None
    quality = fields[6].strip() if len(fields) > 6 else ""
    status_valid = quality.isdigit() and int(quality) > 0
    return {
        "lat": lat, "lon": lon, "date": "",
        "utc_time": format_nmea_time(fields[1].strip()), "status": quality,
        "status_valid": status_valid,
        "speed_knots": "", "speed_kmh": None, "track_deg": "",
        "magvar": "", "magvar_dir": "", "mode": "",
        "altitude_m": fields[9].strip() if len(fields) > 9 else "",
        "parse_warnings": "" if quality.isdigit() else "invalid_fix_quality",
    }

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
    if not talker.isalpha():
        return None
    parser = NMEA_PARSERS.get(sentence_type)
    if parser is None:
        return None
    try:
        parsed = parser(fields)
    except (ValueError, TypeError, OverflowError, IndexError) as exc:
        warn(f"NMEA {sentence_type} 파싱 실패: {exc} - 원문은 미분류 텍스트로 보존")
        return None
    if parsed is None:
        return None
    parsed["talker"] = talker
    parsed["sentence_type"] = sentence_type
    parsed["raw"] = raw
    parsed["checksum_ok"] = checksum_ok
    parsed["trusted"] = bool(parsed.get("status_valid", True) and checksum_ok is not False and not parsed.get("parse_warnings"))
    return parsed

def extract_payload(mm, out_dir, selected_streams, idx1_entries, base_offset,
                     dry_run=False):
    filesize = len(mm)
    chunk_id_to_stream = {}
    for s in selected_streams:
        for cid in s.observed_chunk_ids:
            chunk_id_to_stream[cid] = s

    labels = make_unique_labels(selected_streams)

    file_handles = {}
    seq_counters = {s.index: 0 for s in selected_streams}
    index_rows = []
    validation_counts = {}
    bytes_per_stream = {s.index: 0 for s in selected_streams}
    chunks_per_stream = {s.index: 0 for s in selected_streams}
    preview_printed = {s.index: 0 for s in selected_streams}

    classify_counts = {s.index: {"nmea_text": 0, "generic_text": 0, "float_vector": 0, "binary": 0}
                        for s in selected_streams}
    coord_rows_by_stream = {s.index: [] for s in selected_streams}
    unparsed_by_stream = {s.index: [] for s in selected_streams}
    sensor_rows_by_stream = {s.index: [] for s in selected_streams}

    if not dry_run:
        os.makedirs(out_dir, exist_ok=True)
        for s in selected_streams:
            display_label, dir_label, prefix = labels[s.index]
            stream_dir = os.path.join(out_dir, dir_label)
            os.makedirs(os.path.join(stream_dir, "chunks"), exist_ok=True)
            concat_path = os.path.join(stream_dir, f"{prefix}_concat.bin")
            file_handles[s.index] = open(concat_path, "wb")

    for e in idx1_entries:
        stream = chunk_id_to_stream.get(e["chunk_id"])
        if stream is None:
            continue

        chunk_offset = base_offset + e["idx_offset"]
        reasons, payload_offset, header_size = validate_chunk(mm, chunk_offset, e)
        status = "|".join(reasons)
        validation_counts[status] = validation_counts.get(status, 0) + 1

        display_label, dir_label, prefix = labels[stream.index]
        seq = seq_counters[stream.index]
        seq_counters[stream.index] += 1

        output_file = ""
        if "OUT_OF_RANGE" not in reasons:
            payload = bytes(mm[payload_offset:payload_offset + e["length"]])

            if preview_printed[stream.index] < 3:
                hex_part, ascii_part = hex_preview_lines(payload)
                info(f"\n[{display_label}(idx={stream.index}, "
                     f"{e['chunk_id'].decode('ascii', errors='replace')}) #{seq:04d}]")
                info(f"IDX offset      : 0x{e['idx_offset']:08X}")
                info(f"Chunk offset    : 0x{chunk_offset:08X}")
                info(f"Payload offset  : 0x{payload_offset:08X}")
                info(f"Length          : {e['length']}")
                info("HEX:")
                info(hex_part)
                info("ASCII:")
                info(ascii_part)
                preview_printed[stream.index] += 1

            if not dry_run:
                chunk_filename = f"{prefix}_{seq:06d}.bin"
                chunk_path = os.path.join(out_dir, dir_label, "chunks", chunk_filename)
                with open(chunk_path, "wb") as cf:
                    cf.write(payload)
                file_handles[stream.index].write(payload)
                output_file = os.path.join(dir_label, "chunks", chunk_filename)

            bytes_per_stream[stream.index] += e["length"]
            chunks_per_stream[stream.index] += 1

            if reasons != ["OK"]:
                # raw는 그대로 보존하되, ID/SIZE가 선언과 다른 청크는 base offset이나
                # 파일 구조 추정이 이 위치에서 틀렸을 가능성이 있어 자동 디코딩(좌표/센서
                # 값 산출)은 건너뛰고 raw만 남김 - 잘못된 좌표가 만들어지는 걸 방지.
                warn(f"엔트리 #{seq} (stream={stream.index}, {e['chunk_id']!r}) "
                     f"validation={status} - raw는 보존하지만 신뢰할 수 없어 자동 디코딩은 생략, "
                     f"chunk_offset=0x{chunk_offset:X}")
            else:
                kind, value = classify_payload(payload)
                classify_counts[stream.index][kind] += 1
                if kind in ("nmea_text", "generic_text"):
                    parsed = try_parse_nmea(value)
                    if parsed is not None:
                        speed_kmh = parsed.get("speed_kmh")
                        coord_rows_by_stream[stream.index].append({
                            "date": parsed.get("date", ""),
                            "utc_time": parsed.get("utc_time", ""),
                            "status": parsed.get("status", ""),
                            "latitude": f"{parsed['lat']:.6f}",
                            "longitude": f"{parsed['lon']:.6f}",
                            "speed_knots": parsed.get("speed_knots", ""),
                            "speed_kmh": f"{speed_kmh:.3f}" if speed_kmh is not None else "",
                            "track_deg": parsed.get("track_deg", ""),
                            "magvar": parsed.get("magvar", ""),
                            "magvar_dir": parsed.get("magvar_dir", ""),
                            "mode": parsed.get("mode", ""),
                            "checksum_ok": parsed["checksum_ok"],
                            "status_valid": parsed.get("status_valid", ""),
                            "trusted": parsed.get("trusted", ""),
                            "parse_warnings": parsed.get("parse_warnings", ""),
                            "sequence": seq,
                            "idx1_entry_offset": f"0x{e['idx_offset']:08X}",
                            "chunk_id": e["chunk_id"].decode("ascii", errors="replace"),
                            "sentence_type": parsed["sentence_type"],
                            "raw_sentence": parsed["raw"],
                        })
                    elif value:
                        unparsed_by_stream[stream.index].append((seq, value))
                elif kind == "float_vector":
                    row = {
                        "sequence": seq,
                        "idx1_entry_offset": f"0x{e['idx_offset']:08X}",
                        "chunk_id": e["chunk_id"].decode("ascii", errors="replace"),
                    }
                    row["vector_length"] = len(value)
                    if len(value) == 3:
                        row["x"], row["y"], row["z"] = (f"{v:.6f}" for v in value)
                    else:
                        for i, v in enumerate(value):
                            row[f"value_{i}"] = f"{v:.6f}"
                    sensor_rows_by_stream[stream.index].append(row)
        else:
            warn(f"엔트리 #{seq} (stream={stream.index}, {e['chunk_id']!r}) "
                 f"validation={status} - 안전을 위해 payload 추출/자동 디코딩 생략, "
                 f"chunk_offset=0x{chunk_offset:X}")

        index_rows.append({
            "sequence": seq,
            "stream_index": stream.index,
            "stream_label": display_label,
            "fcc_type": (stream.fcc_type or b"").decode("ascii", errors="replace"),
            "chunk_id": e["chunk_id"].decode("ascii", errors="replace"),
            "idx1_entry_offset": f"0x{e['idx_offset']:08X}",
            "relative_offset": f"0x{e['idx_offset']:08X}",
            "absolute_chunk_offset": f"0x{chunk_offset:08X}",
            "payload_offset": f"0x{payload_offset:08X}" if payload_offset is not None else "",
            "idx1_length": e["length"],
            "chunk_header_length": header_size if header_size is not None else "",
            "flags": f"0x{e['flags']:08X}",
            "validation": status,
            "output_file": output_file,
        })

    for fh in file_handles.values():
        fh.close()

    return {
        "index_rows": index_rows,
        "validation_counts": validation_counts,
        "bytes_per_stream": bytes_per_stream,
        "chunks_per_stream": chunks_per_stream,
        "labels": labels,
        "classify_counts": classify_counts,
        "coord_rows_by_stream": coord_rows_by_stream,
        "unparsed_by_stream": unparsed_by_stream,
        "sensor_rows_by_stream": sensor_rows_by_stream,
    }


def save_metadata(out_dir, index_rows, stream_table, labels, dry_run=False):
    if dry_run:
        return
    os.makedirs(out_dir, exist_ok=True)

    index_csv = os.path.join(out_dir, "index.csv")
    fieldnames = [
        "sequence", "stream_index", "stream_label", "fcc_type", "chunk_id",
        "idx1_entry_offset", "relative_offset", "absolute_chunk_offset",
        "payload_offset", "idx1_length", "chunk_header_length", "flags",
        "validation", "output_file",
    ]
    with open(index_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(index_rows)

    stream_csv = os.path.join(out_dir, "stream_table.csv")
    with open(stream_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["stream_index", "fcc_type", "fcc_handler", "strn_name",
                    "observed_chunk_ids", "role", "selected"])
        for s in stream_table:
            w.writerow([
                s.index,
                (s.fcc_type or b"").decode("ascii", errors="replace"),
                (s.fcc_handler or b"").decode("ascii", errors="replace"),
                s.name or "",
                ";".join(sorted(cid.decode("ascii", errors="replace")
                                 for cid in s.observed_chunk_ids)),
                s.role,
                s.selected,
            ])

    warnings_log = os.path.join(out_dir, "warnings.log")
    with open(warnings_log, "w", encoding="utf-8") as f:
        for w_msg in WARNINGS:
            f.write(w_msg + "\n")


def decide_stream_kind(counts, min_fraction=DECODE_MIN_FRACTION):
    total = sum(counts.values())
    if total == 0:
        return None
    text_ratio = (counts["nmea_text"] + counts["generic_text"]) / total
    float_ratio = counts["float_vector"] / total
    if text_ratio >= min_fraction:
        return "text"
    if float_ratio >= min_fraction:
        return "float_vector"
    return None


def write_decoded_outputs(out_dir, selected_streams, labels, extract_result, dry_run=False):
    decode_summary = {}
    classify_counts = extract_result["classify_counts"]

    for s in selected_streams:
        counts = classify_counts.get(s.index, {})
        kind = decide_stream_kind(counts)
        decode_summary[s.index] = {"kind": kind, "counts": counts}
        if kind is None:
            continue

        display_label, dir_label, prefix = labels[s.index]
        stream_dir = os.path.join(out_dir, dir_label)

        if kind == "text":
            coord_rows = extract_result["coord_rows_by_stream"][s.index]
            unparsed_lines = extract_result["unparsed_by_stream"][s.index]
            decode_summary[s.index]["coord_count"] = len(coord_rows)
            decode_summary[s.index]["unparsed_count"] = len(unparsed_lines)
            if dry_run:
                continue

            coordinates_txt = os.path.join(stream_dir, "coordinates.txt")
            with open(coordinates_txt, "w", encoding="utf-8") as f:
                for i, row in enumerate(coord_rows, start=1):
                    f.write(f"{i}. {row['latitude']}, {row['longitude']}\n")

            coordinates_csv = os.path.join(stream_dir, "coordinates.csv")
            with open(coordinates_csv, "w", newline="", encoding="utf-8") as f:
                fieldnames = [
                    "date", "utc_time", "status", "latitude", "longitude",
                    "speed_knots", "speed_kmh", "track_deg", "magvar", "magvar_dir",
                    "mode", "checksum_ok", "status_valid", "trusted", "parse_warnings",
                    "sequence", "idx1_entry_offset", "chunk_id", "sentence_type", "raw_sentence",
                ]
                w = csv.DictWriter(f, fieldnames=fieldnames)
                w.writeheader()
                w.writerows(coord_rows)

            unparsed_txt = os.path.join(stream_dir, "unparsed_lines.txt")
            with open(unparsed_txt, "w", encoding="utf-8") as f:
                for i, (seq, line) in enumerate(unparsed_lines, start=1):
                    f.write(f"{i}. (entry #{seq}) {line}\n")

        elif kind == "float_vector":
            sensor_rows = extract_result["sensor_rows_by_stream"][s.index]
            decode_summary[s.index]["sensor_count"] = len(sensor_rows)
            if dry_run:
                continue

            sensor_csv = os.path.join(stream_dir, "sensor_values.csv")
            all_value_fields = []
            seen_value_fields = set()
            for row in sensor_rows:
                for key in row:
                    if key not in {"sequence", "idx1_entry_offset", "chunk_id", "vector_length"} and key not in seen_value_fields:
                        seen_value_fields.add(key)
                        all_value_fields.append(key)
            fieldnames = ["sequence", "idx1_entry_offset", "chunk_id", "vector_length"] + all_value_fields
            with open(sensor_csv, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
                w.writeheader()
                w.writerows(sensor_rows)

    decode_detect_csv = os.path.join(out_dir, "decode_detection.csv")
    if not dry_run:
        with open(decode_detect_csv, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["stream_index", "nmea_text", "generic_text", "float_vector",
                        "binary", "decision"])
            for s in selected_streams:
                c = classify_counts.get(s.index, {})
                info_row = decode_summary[s.index]
                decision = {"text": "TEXT (디코딩됨)", "float_vector": "FLOAT_VECTOR (추정, 비공식)"}.get(
                    info_row["kind"], "BINARY/미상 (raw만 보존)")
                w.writerow([s.index, c.get("nmea_text", 0), c.get("generic_text", 0),
                            c.get("float_vector", 0), c.get("binary", 0), decision])

    return decode_summary

#출력 폴더를 준비하는 함수 추가
def prepare_output_dir(out_dir, input_path, dry_run=False, overwrite=False):
    """Stale 결과 혼입을 막기 위해 출력 디렉터리를 안전하게 준비한다."""
    if dry_run:
        return

    out_abs = os.path.abspath(out_dir)
    input_abs = os.path.abspath(input_path)

    protected = {
        os.path.abspath(os.sep),
        os.path.abspath(os.path.expanduser("~")),
        os.path.abspath(os.getcwd()),
        os.path.abspath(os.path.dirname(input_abs)),
    }

    if os.path.exists(out_abs) and not os.path.isdir(out_abs):
        raise SystemExit(
            f"출력 경로가 디렉터리가 아닙니다: {out_abs}"
        )

    nonempty = os.path.isdir(out_abs) and any(os.scandir(out_abs))

    if nonempty and not overwrite:
        raise SystemExit(
            f"출력 디렉터리가 비어 있지 않습니다: {out_abs}\n"
            "이전 분석 결과가 섞이는 것을 막기 위해 실행을 중단합니다. "
            "기존 결과를 지우고 새로 분석하려면 --overwrite 를 지정하세요."
        )

    if nonempty and overwrite:
        if out_abs in protected:
            raise SystemExit(
                f"안전을 위해 이 경로는 --overwrite 할 수 없습니다: {out_abs}"
            )

        shutil.rmtree(out_abs)

    os.makedirs(out_abs, exist_ok=True)

def print_stream_table(stream_table):
    info("\n[Stream Table]")
    info(f"{'idx':<4} {'fccType':<8} {'handler':<9} {'name':<12} "
         f"{'chunk_ids':<20} {'role':<20}")
    for s in stream_table:
        chunk_ids_str = ";".join(sorted(cid.decode("ascii", errors="replace")
                                         for cid in s.observed_chunk_ids)) or "-"
        info(f"{s.index:<4} "
             f"{(s.fcc_type or b'-').decode('ascii', errors='replace'):<8} "
             f"{(s.fcc_handler or b'-').decode('ascii', errors='replace'):<9} "
             f"{(s.name or '-'):<12} "
             f"{chunk_ids_str:<20} "
             f"{s.role:<20}")


def parse_args(argv):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("input", help="입력 AVI 파일 경로")
    p.add_argument("output", help="출력 디렉터리")
    # --overwrite 추가
    p.add_argument("--overwrite", action="store_true",
                   help="출력 디렉터리가 비어 있지 않으면 기존 결과를 삭제하고 새로 생성")
    p.add_argument("--list-streams", action="store_true",
                    help="스트림 테이블만 발견/출력하고 종료(추출 안 함)")
    p.add_argument("--dry-run", action="store_true",
                    help="파일 미생성, base 감지 + 스트림 선택 + validation + 요약만 출력")
    p.add_argument("--assert-anchor", action="store_true",
                    help="코드 상단에 설정된 ground-truth anchor 값으로 회귀 검증")
    p.add_argument("--select-mode", choices=["auto_non_av", "by_fcctype", "by_index", "explicit"],
                    default=None, help="SELECT_MODE 오버라이드")
    p.add_argument("--fcctype", action="append", default=None,
                    help="by_fcctype 모드용 fccType (예: txts). 여러 번 지정 가능")
    p.add_argument("--index", action="append", type=int, default=None,
                    help="by_index 모드용 스트림 번호. 여러 번 지정 가능")
    p.add_argument("--chunk-id", action="append", default=None,
                    help="explicit 모드용 movi chunk ID (예: 02st). 여러 번 지정 가능")
    return p.parse_args(argv)


def main(argv=None):
    WARNINGS.clear()
    args = parse_args(sys.argv[1:] if argv is None else argv)

    assert_riff_file(args.input)

    select_mode = args.select_mode or SELECT_MODE
    select_fcctypes = {v.encode("ascii") for v in args.fcctype} if args.fcctype else SELECT_FCCTYPES
    select_indices = set(args.index) if args.index else SELECT_INDICES
    select_chunk_ids = {v.encode("ascii") for v in args.chunk_id} if args.chunk_id else SELECT_CHUNK_IDS

    filesize = os.path.getsize(args.input)
    if filesize == 0:
        print("입력 파일 크기가 0입니다.", file=sys.stderr)
        sys.exit(1)

    with open(args.input, "rb") as f:
        mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)

        hdrl, movi_list, idx1_chunk, avix_count = find_top_level_sections(mm)

        movi_fourcc_pos = movi_list[0].data_start if movi_list else None
        if movi_fourcc_pos is None:
            movi_fourcc_pos = find_movi_fallback(mm)
            if movi_fourcc_pos is None:
                warn("movi 를 전혀 찾지 못함")

        if idx1_chunk is not None:
            idx1_start = idx1_chunk.data_start
            idx1_size = idx1_chunk.ck_size
        else:
            fb = find_idx1_fallback(mm)
            if fb is None:
                print("idx1을 찾지 못했습니다. 이 파일은 처리할 수 없습니다.", file=sys.stderr)
                mm.close()
                sys.exit(1)
            idx1_start, idx1_size = fb
            idx1_start += 8

        idx1_entries = parse_idx1(mm, idx1_start, idx1_size)

        dw_streams, streams = (None, [])
        if hdrl is not None:
            dw_streams, streams = parse_hdrl(mm, hdrl)
        else:
            warn("hdrl 을 찾지 못함 - 스트림 타입 불명, chunk id 기반 fallback만 사용")

        stream_table = build_stream_table(dw_streams, streams, idx1_entries)
        print_stream_table(stream_table)

        selected_chunk_ids = resolve_targets(
            stream_table, select_mode, select_fcctypes, select_indices, select_chunk_ids)
        selected_streams = [s for s in stream_table if s.selected]

        opendml_info = detect_opendml(mm, movi_list, avix_count, stream_table)

        if args.list_streams:
            info("\n--list-streams 모드: 추출 없이 종료")
            mm.close()
            return
        
        prepare_output_dir(
            args.output,
            args.input,
            dry_run=args.dry_run,
            overwrite=args.overwrite
        )

        base_offset, base_label, base_scores, base_uncertain = detect_base_offset(
            mm, movi_fourcc_pos, idx1_entries)
        info(f"\n[Base offset 선택] {base_label} -> base=0x{base_offset:X}"
             f"{' (불확실)' if base_uncertain else ''}")

        if args.assert_anchor:
            if movi_fourcc_pos != ANCHOR_MOVI_FOURCC_OFFSET:
                warn(f"[anchor] movi 위치 불일치: 감지값=0x{movi_fourcc_pos:X}, "
                     f"anchor=0x{ANCHOR_MOVI_FOURCC_OFFSET:X}")
            anchor_entry = next(
                (e for e in idx1_entries
                 if e["chunk_id"] == ANCHOR_SAMPLE_CHUNK_ID
                 and e["idx_offset"] == ANCHOR_SAMPLE_IDX_OFFSET),
                None)
            if anchor_entry is None:
                warn(f"[anchor] 기준 엔트리({ANCHOR_SAMPLE_CHUNK_ID!r}, "
                     f"idx_offset=0x{ANCHOR_SAMPLE_IDX_OFFSET:X})를 idx1에서 찾지 못함")
            else:
                abs_off = base_offset + anchor_entry["idx_offset"]
                if abs_off != ANCHOR_EXPECTED_ABS_CHUNK_OFFSET:
                    warn(f"[anchor] 계산된 absolute_chunk_offset=0x{abs_off:X} != "
                         f"기대값 0x{ANCHOR_EXPECTED_ABS_CHUNK_OFFSET:X}")
                else:
                    info(f"[anchor] PASS - absolute_chunk_offset=0x{abs_off:X} 일치")

        if not selected_streams:
            warn("선택된 스트림이 없습니다. SELECT_MODE/조건을 확인하세요.")

        result = extract_payload(
            mm, args.output, selected_streams, idx1_entries, base_offset,
            dry_run=args.dry_run)

        save_metadata(args.output, result["index_rows"], stream_table,
                       result["labels"], dry_run=args.dry_run)

        decode_summary = write_decoded_outputs(
            args.output, selected_streams, result["labels"], result, dry_run=args.dry_run)

        info("\n" + "=" * 60)
        info("[요약]")
        info(f"movi offset            : 0x{movi_fourcc_pos:X} "
             f"(발견된 movi 개수: {opendml_info['movi_count']})" if movi_fourcc_pos is not None
             else "movi offset            : 없음")
        info(f"idx1 offset             : 0x{idx1_start:X}")
        info(f"선택된 base 후보        : {base_label} (점수: {base_scores})")
        info(f"발견된 스트림 수        : hdrl dwStreams={dw_streams} vs "
             f"실제 관측={len(stream_table)}")
        info("스트림 테이블           : 위 [Stream Table] 참고")
        info(f"선택 모드/대상          : {select_mode} -> {sorted(selected_chunk_ids)!r}")
        info(f"총 idx1 entry 개수      : {len(idx1_entries)}")
        for s in selected_streams:
            display_label = result["labels"][s.index][0]
            ds = decode_summary.get(s.index, {})
            kind = ds.get("kind")
            if kind == "text":
                extra = f", GPRMC/GPGGA 좌표 파싱 {ds.get('coord_count', 0)}개, 미분류 텍스트 {ds.get('unparsed_count', 0)}개"
            elif kind == "float_vector":
                extra = f", float 벡터(추정, 비공식) 파싱 {ds.get('sensor_count', 0)}개"
            else:
                extra = " (디코딩 안 됨 - raw만 보존)"
            info(f"  - {display_label}(idx={s.index}): "
                 f"{result['chunks_per_stream'][s.index]}개 chunk, "
                 f"{result['bytes_per_stream'][s.index]} bytes{extra}")
        info(f"검증 결과 (사유별)      : {result['validation_counts']}")
        info(f"OpenDML/AVIX/indx/rec   : AVIX={opendml_info['avix_count']}, "
             f"indx={opendml_info['has_indx']}, rec={opendml_info['has_rec']}")
        info(f"경고 총 개수            : {len(WARNINGS)} "
             f"({'dry-run이라 파일 미생성' if args.dry_run else os.path.join(args.output, 'warnings.log') + ' 참조'})")
        info("=" * 60)

        mm.close()


if __name__ == "__main__":
    main()

