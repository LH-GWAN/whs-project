# ---- 여기부터: GPS_metadata_avi.py (RIFF/idx1 파서 + 스트림 선택/추출/디코딩) ----
# ---- GPS_metadata_GPRMC.py 의 텍스트 스트림 사전 판정은 decide_stream_kind 다수결로 흡수됨 ----
import argparse
import csv
import datetime
import math
import mmap
import os
import re
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

SAMPLE_ENTRIES_FOR_BASE_DETECT = 8
MAX_IDX1_ENTRIES = 5_000_000

DECODE_MIN_FRACTION = 0.8
EMBEDDED_NMEA_RE = re.compile(rb"\$?([A-Z]{2}(?:RMC|GGA)[ -~]*)")
FLOAT_VECTOR_MIN_N = 2
FLOAT_VECTOR_MAX_N = 8
FLOAT_VECTOR_MAX_ABS = 50.0

# 슬랙 판단: 트레일링 영역이 이 값(바이트) 미만이면 정렬용 패딩으로 보고 그냥 무시한다.
TRAILING_IGNORE_THRESHOLD = 16

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
    dw_scale: int = None
    dw_rate: int = None
    dw_length: int = None


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
                    # 재생 시간축용 - strh 레이아웃상 dwScale(20)/dwRate(24)/dwLength(32).
                    # 텍스트 스트림은 이 값이 0으로 깨져 있는 기기가 많아서(실측: VUGERA는
                    # dwScale=0, INAVI FXD900은 dwRate=0) 여기서 읽어만 두고, 실제 시간
                    # 계산은 영상 스트림 값을 기준으로 한다.
                    if sc.ck_size >= 36:
                        si.dw_scale = struct.unpack_from("<I", mm, sc.data_start + 20)[0]
                        si.dw_rate = struct.unpack_from("<I", mm, sc.data_start + 24)[0]
                        si.dw_length = struct.unpack_from("<I", mm, sc.data_start + 32)[0]
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


def classify_payload(payload, stream_fcc=None):
    nmea_line = find_embedded_nmea_text(payload)
    if nmea_line:
        return "nmea_text", nmea_line
    if looks_like_text_record(payload):
        return "generic_text", decode_text_record(payload)
    # 72바이트 고정 레코드는 앞 40바이트가 0x00이라 텍스트/float 벡터 판정에 안 걸린다.
    # float_vector(최대 8개=32바이트)와 길이가 겹치지 않아 순서를 앞에 둬도 안전하다.
    record = parse_finevu_record(payload, stream_fcc)
    if record is not None:
        return "record72", record
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
        # 위경도 필드가 "비어 있고" status가 A가 아니면 = 그 순간 GPS fix가 없었던
        # 정상 기록(status=V, mode=N)이므로 좌표만 공란으로 두고 행은 살린다.
        # 필드에 값은 있는데 파싱이 안 되는 경우는 손상으로 보고 기존대로 버린다.
        _status = fields[2].strip().upper() if len(fields) > 2 else ""
        if lat_str or lon_str or _status == "A":
            return None
        lat = lon = None

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


# ---------------------------------------------------------------------------
# FineVu txts/dats 72바이트 고정 레코드 (FineVu Player 2.0 역분석 결과 기반)
#
# 같은 txts 스트림을 쓰면서도 NMEA 텍스트가 아니라 72바이트 고정 이진 레코드를 넣는
# 계열이 있다(FineVu X3000/X700 등). 앞 40바이트가 전부 0x00이라 텍스트 판정에도
# float 벡터 판정에도 안 걸려서, 예전에는 이 스트림이 통째로 BINARY/미상으로 빠지고
# raw만 보존됐다. 데이터가 없었던 게 아니라 디코딩 규칙이 없었던 것.
#
# 레코드 배치 (리틀엔디언, 청크 데이터 선두를 오프셋 0으로 봄)
#     0~39      미해석. 뷰어가 읽지 않는 구간이라 기기마다 내용이 다르다.
#               (LX2000은 선두 7바이트가 20 00 00 00 20 33 00 고정 + 8번째가 프레임
#                카운터. X3000/X700 실측 샘플은 이 구간이 전부 0x00 - 그래서 이 값을
#                시그니처로 삼으면 안 된다.)
#     40/44/48  float32  충격센서 X/Y/Z (단위 g)
#     52        uint32   반구 플래그. (v & 3) == 1 이면 북위 아니면 남위,
#                        ((v >> 2) & 3) == 1 이면 동경 아니면 서경
#     56        float32  속도 (km/h - 별도 환산 불필요)
#     60        float32  위도  txts=DDMM.MMMM 도분 / dats=십진 도
#     64        float32  경도  txts=DDDMM.MMMM 도분 / dats=십진 도
#     68        uint8    경과 초 (1초마다 +1). 뷰어는 안 읽지만 우리는 쓴다.
#     69~71     미해석
#
# 주의할 점 세 가지.
#  1) 위경도가 둘 다 0.0이면 그 레코드는 측위 실패다. 좌표 0,0은 기니만 해상의 실제
#     좌표라 값만 보고는 구분이 안 되므로 반드시 결측으로 처리한다(뷰어도 txts 경로는
#     NaN을 채운다). 여기서는 좌표/속도 칸을 비우고 status=V로 남긴다.
#  2) txts와 dats는 필드 배치가 같은데 좌표 해석만 다르다. dats는 이미 십진 도라
#     도분 변환을 걸면 안 된다. dats는 선두 7바이트 매직으로 구분된다.
#  3) 도분->십진 변환은 반드시 float32로 해야 한다. 같은 식을 float64로 계산하면
#     소수점 여섯째 자리에서 어긋난다(지상 거리로 약 0.2m).
# ---------------------------------------------------------------------------

FINEVU_OFF_GX = 40
FINEVU_OFF_GY = 44
FINEVU_OFF_GZ = 48
FINEVU_OFF_HEMI = 52
FINEVU_OFF_SPEED = 56
FINEVU_OFF_LAT = 60
FINEVU_OFF_LON = 64
FINEVU_OFF_ELAPSED = 68
FINEVU_RECORD_LEN = 72

# 뷰어가 실제로 건드리는 최대 오프셋이 67이라 68바이트면 좌표까지는 읽을 수 있다.
# 경과 초(68)까지 있어야 완전한 레코드로 본다.
FINEVU_RECORD_MIN_LEN = 69

# dats 스트림 레코드의 선두 매직 (뷰어 코드 안에 상수로 박혀 있고, 뷰어는 dats에
# 대해서만 이 값을 검사해 일치하는 레코드만 처리한다. txts에는 검사가 없다.)
FINEVU_DATS_MAGIC = b"\xff\x01\x00\x00\x0a\x26\x03"

# 오탐 방지용 범위. 랜덤 바이트가 우연히 이 조건을 다 통과하기는 어렵다.
FINEVU_MAX_SPEED_KMH = 400.0
FINEVU_MAX_G = 100.0          # 99.0(기록된 이상치)/100.0(데이터 없음) 센티넬 포함
FINEVU_G_SENTINELS = (99.0, 100.0)
FINEVU_MAX_LAT_DM = 9000.0    # 90도 00.0000분
FINEVU_MAX_LON_DM = 18000.0   # 180도 00.0000분
FINEVU_ELAPSED_WRAP = 256     # 경과 초가 1바이트라 256에서 되돌아간다

# 파일명에서 녹화 시작 시각을 뽑는 패턴. 기기마다 구분자가 달라서 몇 가지를 본다.
#   20241024-11h11m18s_N / 20260812-10h55m02s_N   (FineVu)
#   EVT_20240618_184124_F / REC_20240916_172436_F (VUGERA)
#   EVT_2025_10_12_02_01_59_S                     (INAVI)
FINEVU_FILENAME_TS_RES = [
    re.compile(r"(\d{4})(\d{2})(\d{2})[-_ ]?(\d{2})h(\d{2})m(\d{2})s"),
    re.compile(r"(\d{4})(\d{2})(\d{2})[-_ ](\d{2})(\d{2})(\d{2})"),
    re.compile(r"(\d{4})[-_](\d{2})[-_](\d{2})[-_](\d{2})[-_](\d{2})[-_](\d{2})"),
]


def _f32(x):
    """double -> float32 반올림. 뷰어의 float 연산을 그대로 재현하기 위한 것."""
    return struct.unpack("<f", struct.pack("<f", x))[0]


def finevu_dm_to_decimal(value):
    """DDMM.MMMM 도분 -> 십진 도. 뷰어의 변환 함수를 연산 순서까지 그대로 옮겼다.

        return (float)(int)(float)(v / 100.0)
             + (float)((v - (float)(100 * (int)(float)(v / 100.0))) / 60.0);

    나눗셈과 캐스트 위치를 바꾸거나 전 과정을 float64로 계산하면 뷰어와 값이
    소수점 여섯째 자리에서 어긋난다."""
    v = _f32(value)
    q = _f32(v / 100.0)
    deg_i = int(q)                      # C의 (int) 캐스트 = 0 방향 절삭
    deg = _f32(float(deg_i))
    minutes = _f32((v - _f32(100 * deg_i)) / 60.0)
    return _f32(deg + minutes)


def finevu_is_dats_record(payload, stream_fcc=None):
    """이 레코드의 좌표가 십진 도(dats)인지 도분(txts)인지 판단한다."""
    if payload[:len(FINEVU_DATS_MAGIC)] == FINEVU_DATS_MAGIC:
        return True
    return stream_fcc == b"dats"


def parse_finevu_record(payload, stream_fcc=None):
    """72바이트 고정 레코드를 해석한다. 형식이 아니면 None.

    반환 dict의 lat/lon/speed_kmh는 측위 실패 시 None이다. 0으로 채우지 않는다."""
    if len(payload) < FINEVU_RECORD_MIN_LEN:
        return None

    try:
        gx, gy, gz, hemi, speed, lat_raw, lon_raw = struct.unpack_from(
            "<fffIfff", payload, FINEVU_OFF_GX)
    except struct.error:
        return None
    elapsed = payload[FINEVU_OFF_ELAPSED]

    for v in (gx, gy, gz, speed, lat_raw, lon_raw):
        if not math.isfinite(v):
            return None
    if any(abs(v) > FINEVU_MAX_G for v in (gx, gy, gz)):
        return None
    if not (0.0 <= speed <= FINEVU_MAX_SPEED_KMH):
        return None
    # 반구 플래그는 2비트짜리 두 개라 상위 비트가 켜져 있으면 이 형식이 아니다.
    if hemi >> 4:
        return None

    decimal_coords = finevu_is_dats_record(payload, stream_fcc)
    no_fix = (lat_raw == 0.0 and lon_raw == 0.0)

    parse_warnings = []
    lat = lon = None
    speed_kmh = None

    if not no_fix:
        if decimal_coords:
            if not (abs(lat_raw) <= 90.0 and abs(lon_raw) <= 180.0):
                return None
            lat, lon = _f32(lat_raw), _f32(lon_raw)
        else:
            if not (0.0 <= lat_raw < FINEVU_MAX_LAT_DM
                    and 0.0 <= lon_raw < FINEVU_MAX_LON_DM):
                return None
            lat = finevu_dm_to_decimal(lat_raw)
            lon = finevu_dm_to_decimal(lon_raw)
        # 뷰어는 1이 아니면 전부 남위/서경으로 본다. 0이나 3처럼 뷰어도 정의하지
        # 않은 값이 오면 부호는 뷰어와 똑같이 처리하되 경고를 남긴다.
        lat_flag, lon_flag = hemi & 3, (hemi >> 2) & 3
        if lat_flag not in (1, 2) or lon_flag not in (1, 2):
            parse_warnings.append("unexpected_hemisphere_flag")
        if lat_flag != 1:
            lat = -lat
        if lon_flag != 1:
            lon = -lon
        speed_kmh = speed

    gvals = []
    for v in (gx, gy, gz):
        if any(abs(v - s) < 1e-6 for s in FINEVU_G_SENTINELS):
            # 99.0 = 기록된 이상치, 100.0 = 데이터 없음. 실제 가속도로 쓰면 그래프가
            # 크게 왜곡되므로 둘 다 결측으로 뺀다.
            gvals.append(None)
            parse_warnings.append("gsensor_sentinel")
        else:
            gvals.append(v)

    return {
        "lat": lat,
        "lon": lon,
        "status": "A" if not no_fix else "V",
        "status_valid": not no_fix,
        "speed_kmh": speed_kmh,
        "track_deg": "",          # txts/dats 경로는 진행 방향을 채우지 않는다
        "x_g": gvals[0], "y_g": gvals[1], "z_g": gvals[2],
        "hemi_flag": hemi,
        "lat_raw": lat_raw, "lon_raw": lon_raw,
        "coord_format": "decimal(dats)" if decimal_coords else "ddmm(txts)",
        "elapsed_sec": elapsed,
        "parse_warnings": ";".join(sorted(set(parse_warnings))),
        "trusted": bool(not no_fix and not parse_warnings),
        "raw": payload[:FINEVU_RECORD_LEN].hex(),
    }


def finevu_filename_start_time(path):
    """파일명에 박힌 녹화 시작 시각을 datetime으로. 못 찾으면 None."""
    stem = os.path.basename(path)
    for rx in FINEVU_FILENAME_TS_RES:
        m = rx.search(stem)
        if not m:
            continue
        try:
            return datetime.datetime(*(int(g) for g in m.groups()))
        except ValueError:
            continue
    return None


def finevu_unwrap_elapsed(values):
    """1바이트 경과 초를 단조 증가하는 초로 편다.

    보고서는 '파일명 시각 + 경과 초'라고 적었지만, 실측 X3000 샘플은 첫 레코드가
    86에서 시작한다(자유 진행 카운터). 그대로 더하면 86초가 밀리므로 첫 값과의
    차이를 쓰고, 256에서 되돌아가는 것만 펴 준다."""
    out = []
    base = None
    prev = None
    carry = 0
    for v in values:
        if v is None:
            out.append(None)
            continue
        if base is None:
            base = v
            prev = v
        if v < prev:
            carry += FINEVU_ELAPSED_WRAP
        prev = v
        out.append(v + carry - base)
    return out


# ---------------------------------------------------------------------------
# 재생 시간축 (AVI)
#
# AVI의 텍스트 스트림은 strh의 dwScale/dwRate가 깨져 있는 기기가 많다. 실측:
#     VUGERA MB-900SB : txts dwScale=0,   dwRate=30   -> 0으로 나누기
#     INAVI FXD900    : txts dwScale=100, dwRate=0    -> 0 Hz
# 그래서 텍스트 스트림 자신의 rate는 못 쓴다. 반면 영상 스트림은 멀쩡하다.
#     VUGERA : vids 30.000 Hz, dwLength=1150 -> 38.3초 (txts dwLength도 1150 = 프레임 동기)
#     FXD900 : vids 29.970 Hz, dwLength=1165 -> 38.9초 (txts dwLength=621 = 약 16Hz)
#
# 그래서 "영상 길이 / 텍스트 스트림 레코드 수"로 레코드 간격을 낸다. FXD900 교차검증:
# GPS가 621개 중 39개라 약 16레코드마다 1개 = 1초 간격이 되는데, 실제 GPRMC의 UTC
# 시각도 정확히 1초 간격이라 일치한다.
#
# ffprobe 같은 외부 도구를 쓰지 않는 이유: 그건 컨테이너 총 길이만 주지 "이 레코드가
# 몇 초 지점인가"는 안 알려준다. 결국 여기서 하는 것과 같은 나눗셈이 필요하고,
# 서드파티 디코더가 손상 구간을 임의로 보정해버리면 우리가 잡아낸 이상이 가려진다.
# ---------------------------------------------------------------------------
def compute_video_duration(mm, hdrl_chunk, stream_table):
    """영상 스트림 strh로 재생 길이(초)를 구한다. 실패하면 avih로 폴백.
    반환값: (duration_sec, source) - 못 구하면 (None, 사유)."""
    for s in stream_table:
        if s.fcc_type != b"vids":
            continue
        if s.dw_rate and s.dw_scale and s.dw_length:
            fps = s.dw_rate / s.dw_scale
            if fps > 0:
                return s.dw_length / fps, f"vids strh({fps:.3f}fps x {s.dw_length}프레임)"

    if hdrl_chunk is not None:
        for child in iter_chunks(mm, hdrl_chunk.content_start, hdrl_chunk.content_end):
            if child.ck_id == b"avih" and not child.is_list and child.ck_size >= 20:
                usec = struct.unpack_from("<I", mm, child.data_start)[0]
                frames = struct.unpack_from("<I", mm, child.data_start + 16)[0]
                if usec and frames:
                    return frames * usec / 1e6, f"avih({1e6/usec:.3f}fps x {frames}프레임)"
    return None, "영상 스트림 strh와 avih 어디서도 재생 길이를 구할 수 없음"


def build_avi_stream_times(video_duration, record_count):
    """레코드 수로 균등 분할한 (start_sec, end_sec) 목록."""
    if not video_duration or record_count <= 0:
        return []
    step = video_duration / record_count
    return [(i * step, (i + 1) * step) for i in range(record_count)]

def extract_payload(mm, out_dir, selected_streams, idx1_entries, base_offset,
                     dry_run=False, stream_times=None, start_dt=None):
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

    classify_counts = {s.index: {"nmea_text": 0, "generic_text": 0, "record72": 0,
                                  "float_vector": 0, "binary": 0}
                        for s in selected_streams}
    coord_rows_by_stream = {s.index: [] for s in selected_streams}
    unparsed_by_stream = {s.index: [] for s in selected_streams}
    sensor_rows_by_stream = {s.index: [] for s in selected_streams}
    # 72바이트 레코드는 경과 초(offset 68)가 1바이트 자유 진행 카운터라 파일 전체를
    # 모은 뒤 한 번에 펴야 한다. 그래서 여기서는 (seq, entry, record)만 쌓아두고
    # coord/sensor 행은 순회가 끝난 뒤에 만든다.
    record72_by_stream = {s.index: [] for s in selected_streams}

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
        times = (stream_times or {}).get(stream.index) or []
        start_sec, end_sec = times[seq] if seq < len(times) else (None, None)
        time_source = "avi_video_duration" if start_sec is not None else ""
        start_disp = f"{start_sec:.3f}" if start_sec is not None else ""
        end_disp = f"{end_sec:.3f}" if end_sec is not None else ""

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
                warn(f"엔트리 #{seq} (stream={stream.index}, {e['chunk_id']!r}) "
                     f"validation={status} - raw는 보존하지만 신뢰할 수 없어 자동 디코딩은 생략, "
                     f"chunk_offset=0x{chunk_offset:X}")
            else:
                kind, value = classify_payload(payload, stream.fcc_type)
                classify_counts[stream.index][kind] += 1
                if kind in ("nmea_text", "generic_text"):
                    parsed = try_parse_nmea(value)
                    if parsed is not None:
                        speed_kmh = parsed.get("speed_kmh")
                        coord_rows_by_stream[stream.index].append({
                            "start_time_sec": start_disp,
                            "end_time_sec": end_disp,
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
                elif kind == "record72":
                    record72_by_stream[stream.index].append((seq, e, value,
                                                              start_disp, end_disp, time_source))
                elif kind == "float_vector":
                    row = {
                        "start_time_sec": start_disp,
                        "end_time_sec": end_disp,
                        "time_source": time_source,
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

    # 72바이트 레코드 후처리: 경과 초를 편 뒤 coord/sensor 행을 만든다.
    # GPS와 충격센서가 한 레코드에 같이 들어 있어서 양쪽에 같은 seq로 넣는다
    # (timeline이 start_time_sec로 둘을 붙이므로 그대로 한 줄이 된다).
    for sidx, items in record72_by_stream.items():
        deltas = finevu_unwrap_elapsed([rec["elapsed_sec"] for _, _, rec, _, _, _ in items])
        for (seq, e, rec, start_disp, end_disp, time_source), delta in zip(items, deltas):
            abs_time = ""
            if start_dt is not None and delta is not None:
                abs_time = (start_dt + datetime.timedelta(seconds=delta)).strftime(
                    "%Y-%m-%d %H:%M:%S")
            chunk_id_txt = e["chunk_id"].decode("ascii", errors="replace")
            fmt = lambda v, spec="{:.6f}": "" if v is None else spec.format(v)
            coord_rows_by_stream[sidx].append({
                "start_time_sec": start_disp,
                "end_time_sec": end_disp,
                "time_source": time_source,
                "elapsed_sec": rec["elapsed_sec"],
                "elapsed_delta_sec": "" if delta is None else delta,
                "abs_time": abs_time,
                "status": rec["status"],
                "latitude": fmt(rec["lat"]),
                "longitude": fmt(rec["lon"]),
                "speed_kmh": fmt(rec["speed_kmh"], "{:.3f}"),
                "track_deg": rec["track_deg"],
                "x_g": fmt(rec["x_g"]), "y_g": fmt(rec["y_g"]), "z_g": fmt(rec["z_g"]),
                "hemi_flag": rec["hemi_flag"],
                "lat_raw": f"{rec['lat_raw']:.4f}",
                "lon_raw": f"{rec['lon_raw']:.4f}",
                "coord_format": rec["coord_format"],
                "status_valid": rec["status_valid"],
                "trusted": rec["trusted"],
                "parse_warnings": rec["parse_warnings"],
                "sequence": seq,
                "idx1_entry_offset": f"0x{e['idx_offset']:08X}",
                "chunk_id": chunk_id_txt,
                "raw_record_hex": rec["raw"],
            })
            sensor_rows_by_stream[sidx].append({
                "start_time_sec": start_disp,
                "end_time_sec": end_disp,
                "time_source": time_source,
                "sequence": seq,
                "idx1_entry_offset": f"0x{e['idx_offset']:08X}",
                "chunk_id": chunk_id_txt,
                "vector_length": 3,
                "x": fmt(rec["x_g"]), "y": fmt(rec["y_g"]), "z": fmt(rec["z_g"]),
            })

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
    record72_ratio = counts.get("record72", 0) / total
    float_ratio = counts["float_vector"] / total
    if text_ratio >= min_fraction:
        return "text"
    if record72_ratio >= min_fraction:
        return "record72"
    if float_ratio >= min_fraction:
        return "float_vector"
    return None


def write_avi_timeline(out_dir, selected_streams, labels, extract_result, dry_run=False):
    """GPS 스트림과 센서 스트림을 재생 시각 기준으로 한 줄에 합친 통합 타임라인.
    AVI는 GPS와 G센서가 서로 다른 스트림(예: GPSR/SENS)에 들어있어서, 각 스트림에
    이미 계산해둔 start_time_sec로 붙인다. 루트 A/B/C의 timeline.csv와 컬럼 구성을
    맞춰서 시각화 쪽이 경로마다 다른 파일을 읽지 않아도 되게 한다.

    AVI의 SENS(float32 벡터)는 이미 g 단위에 가까운 값이라(|v|가 1g 근처) MP4 쪽
    gsensor처럼 카운트->g 보정을 하지 않는다. 그래서 x_g_cal 계열은 비워둔다.
    """
    if dry_run:
        return None
    coord_by_stream = extract_result["coord_rows_by_stream"]
    sensor_by_stream = extract_result["sensor_rows_by_stream"]

    gps_rows = []
    for s in selected_streams:
        rows = [r for r in coord_by_stream.get(s.index, []) if r.get("start_time_sec")]
        if len(rows) > len(gps_rows):
            gps_rows = rows
    if not gps_rows:
        return None

    # 센서는 재생 시각(ms 단위 반올림)으로 찾는다. 같은 시각이 여러 개면 첫 번째를 쓴다.
    sensor_at = {}
    for s in selected_streams:
        for r in sensor_by_stream.get(s.index, []):
            key = r.get("start_time_sec")
            if key and key not in sensor_at:
                sensor_at[key] = r

    rows = []
    last_lat = last_lon = last_speed = ""
    for i, r in enumerate(gps_rows, start=1):
        if r.get("latitude"):
            last_lat, last_lon = r["latitude"], r["longitude"]
            last_speed = r.get("speed_kmh", "")
        sen = sensor_at.get(r.get("start_time_sec"), {})
        rows.append({
            "sample": i,
            "start_time_sec": r.get("start_time_sec", ""),
            "end_time_sec": r.get("end_time_sec", ""),
            "time_source": r.get("time_source", ""),
            "abs_time": r.get("abs_time", ""),
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
            "x_g": sen.get("x", ""), "y_g": sen.get("y", ""), "z_g": sen.get("z", ""),
            "x_g_cal": "", "y_g_cal": "", "z_g_cal": "",
        })

    path = os.path.join(out_dir, "timeline.csv")
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    return len(rows)

RECORD72_COORD_FIELDS = [
    "start_time_sec", "end_time_sec", "time_source",
    "elapsed_sec", "elapsed_delta_sec", "abs_time",
    "status", "latitude", "longitude", "speed_kmh", "track_deg",
    "x_g", "y_g", "z_g",
    "hemi_flag", "lat_raw", "lon_raw", "coord_format",
    "status_valid", "trusted", "parse_warnings",
    "sequence", "idx1_entry_offset", "chunk_id", "raw_record_hex",
]

RECORD72_SENSOR_FIELDS = [
    "start_time_sec", "end_time_sec", "time_source",
    "sequence", "idx1_entry_offset", "chunk_id", "vector_length", "x", "y", "z",
]


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
                # coordinates.txt는 "좌표 목록"이라 fix가 없어 좌표가 빈 행은 제외한다
                # (그 행도 coordinates.csv에는 status=V로 그대로 남는다).
                for i, row in enumerate([r for r in coord_rows if r["latitude"]], start=1):
                    f.write(f"{i}. {row['latitude']}, {row['longitude']}\n")

            coordinates_csv = os.path.join(stream_dir, "coordinates.csv")
            with open(coordinates_csv, "w", newline="", encoding="utf-8") as f:
                fieldnames = [
                    "start_time_sec", "end_time_sec", "time_source",
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

        elif kind == "record72":
            # 한 레코드에 GPS와 충격센서가 같이 들어 있어서 coordinates.*와
            # sensor_values.csv를 같은 스트림에서 함께 만든다.
            coord_rows = extract_result["coord_rows_by_stream"][s.index]
            sensor_rows = extract_result["sensor_rows_by_stream"][s.index]
            fix_rows = [r for r in coord_rows if r.get("latitude")]
            decode_summary[s.index]["coord_count"] = len(coord_rows)
            decode_summary[s.index]["fix_count"] = len(fix_rows)
            decode_summary[s.index]["sensor_count"] = len(sensor_rows)
            if dry_run:
                continue

            with open(os.path.join(stream_dir, "coordinates.csv"), "w", newline="",
                       encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=RECORD72_COORD_FIELDS)
                w.writeheader()
                w.writerows(coord_rows)

            with open(os.path.join(stream_dir, "coordinates.txt"), "w", encoding="utf-8") as f:
                # 좌표 목록이라 측위 실패(status=V) 행은 뺀다. CSV에는 그대로 남는다.
                for i, row in enumerate(fix_rows, start=1):
                    f.write(f"{i}. {row['latitude']}, {row['longitude']}\n")

            with open(os.path.join(stream_dir, "sensor_values.csv"), "w", newline="",
                       encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=RECORD72_SENSOR_FIELDS)
                w.writeheader()
                w.writerows(sensor_rows)

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
                    if key not in {"sequence", "idx1_entry_offset", "chunk_id", "vector_length",
                                    "start_time_sec", "end_time_sec", "time_source"} and key not in seen_value_fields:
                        seen_value_fields.add(key)
                        all_value_fields.append(key)
            fieldnames = ["start_time_sec", "end_time_sec", "time_source",
                          "sequence", "idx1_entry_offset", "chunk_id", "vector_length"] + all_value_fields
            with open(sensor_csv, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
                w.writeheader()
                w.writerows(sensor_rows)

    decode_detect_csv = os.path.join(out_dir, "decode_detection.csv")
    if not dry_run:
        with open(decode_detect_csv, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["stream_index", "nmea_text", "generic_text", "record72",
                        "float_vector", "binary", "decision"])
            for s in selected_streams:
                c = classify_counts.get(s.index, {})
                info_row = decode_summary[s.index]
                decision = {"text": "TEXT (디코딩됨)",
                            "record72": "RECORD72 (FineVu 72바이트 고정 레코드, 디코딩됨)",
                            "float_vector": "FLOAT_VECTOR (추정, 비공식)"}.get(
                    info_row["kind"], "BINARY/미상 (raw만 보존)")
                w.writerow([s.index, c.get("nmea_text", 0), c.get("generic_text", 0),
                            c.get("record72", 0), c.get("float_vector", 0),
                            c.get("binary", 0), decision])

    return decode_summary


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


# ---------------------------------------------------------------------------
# 슬랙 판단 + 구조적 리페어 (AVI_exception_lot_RIFF.py 대체)
#
# 실제로 확인해보니 이 카메라(VUGERA MB-900SB 등)는 파일을 고정 크기(80MB)로
# 미리 만들어두고 앞부분만 새 녹화로 덮어쓰는 방식이라, "슬랙"은 파일 끝 뒤가
# 아니라 최상위 RIFF가 선언한 movi 영역 *내부*에 예전 녹화 파일의 RIFF 헤더
# /hdrl/JUNK(구 파일명 포함)/movi 통째로 남아있는 형태로 나타난다. idx1은
# 현재 녹화분 chunk만 정확히 가리키므로 idx1 기준 추출(GPS 포함) 자체는 이
# 슬랙에 영향을 받지 않지만, 별도로 "슬랙 없는 재생용 사본"을 만들어 두는게
# 유용하다.
#
# 문자열(mm.find) 스캔은 여전히 안 쓸 수는 없다 - 예전 파일의 RIFF 헤더는
# 정식 chunk 그래프에 속하지 않는 opaque payload 바이트이기 때문에 존재
# 여부 자체를 확인하려면 바이트 패턴을 찾아야 한다. 다만 그 탐색 범위를
# movi의 "선언된" content 영역으로 엄격히 제한하고(=RIFF 크기 기반 구조
# 파싱으로 경계를 잡음), 찾은 자리가 실제로 RIFF+AVI/AVIX 폼타입 조합인지
# 검증해서 오탐(압축 데이터 안에 우연히 "RIFF" 4바이트가 나오는 경우)을
# 줄인다. 최상위 RIFF 밖(파일 끝 이후)의 트레일링 데이터는 진짜 두 번째
# RIFF일 때만 잘라내고, 그 외(JUNK 태그+커스텀 바이너리 등, 예: FineVu
# CustomGPS)는 실제 데이터일 수 있으므로 손대지 않고 raw로만 보존한다.
# ---------------------------------------------------------------------------

VIDEO_CHUNK_RE = re.compile(rb"^[0-9]{2}(?:dc|db)$")


# ---- 여기부터: AVI_exception_lot_RIFF.py (슬랙 판단 + 리페어) ----
def find_embedded_riffs(mm, search_start, search_end, reference_size):
    """movi의 구조적으로 파싱된 content 범위 안에서만 b"RIFF" + AVI/AVIX 폼타입
    조합을 찾는다(전체 파일 스캔 아님). 파일 크기는 카메라/모델마다 다를 수
    있으므로 어떤 값도 하드코딩하지 않고, 항상 "이 파일 자신의 최상위 RIFF가
    선언한 크기"(reference_size)를 그때그때 읽어서 기준으로 삼는다.

    찾은 자리를 "예전 녹화 파일의 잔재"로 인정하는 조건은 둘 중 하나:
      (a) 선언 크기가 reference_size와 정확히 같음
          -> 같은 카메라/포맷이 쓰는 고정 컨테이너 크기 관례를 그대로 물려받은
             예전 파일이라는 강한 정황 증거.
      (b) 선언 크기가 실제 남은 공간(search_end까지)보다 커서 다 들어갈 수 없음
          -> 원래 파일 전체가 있어야 정상인데 일부만 덮어써지고 잘려나간
             잔재라는 확실한 증거.
    """
    results = []
    pos = search_start
    while True:
        idx = mm.find(b"RIFF", pos, search_end)
        if idx < 0:
            break
        if idx + 12 <= search_end and bytes(mm[idx + 8:idx + 12]) in (b"AVI ", b"AVIX"):
            declared_size = struct.unpack_from("<I", mm, idx + 4)[0]
            remaining = search_end - (idx + 8)
            same_as_reference = declared_size == reference_size
            overruns_remaining = declared_size > remaining
            if same_as_reference or overruns_remaining:
                results.append({
                    "pos": idx,
                    "form": bytes(mm[idx + 8:idx + 12]),
                    "declared_size": declared_size,
                    "same_as_reference": same_as_reference,
                    "overruns_remaining": overruns_remaining,
                })
        pos = idx + 4
    return results


def sniff_embedded_filename(mm, riff_pos, search_window=0x400):
    """임베디드 RIFF 근처 JUNK 청크에 남아있는 예전 파일명을 최선 노력으로
    읽어본다(리페어 로직에는 영향 없는 진단/로그용)."""
    end = min(riff_pos + search_window, len(mm))
    idx = mm.find(b"JUNK", riff_pos, end)
    if idx < 0 or idx + 8 > len(mm):
        return None
    size = struct.unpack_from("<I", mm, idx + 4)[0]
    if size <= 0 or size > search_window or idx + 8 + size > len(mm):
        return None
    raw = bytes(mm[idx + 8:idx + 8 + size]).split(b"\x00", 1)[0]
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError:
        return None
    if not text or not all(32 <= ord(c) < 127 for c in text):
        return None
    return text


def count_top_level_riffs(mm):
    """최상위에서 연속으로 이어지는 유효한 RIFF 청크 개수를 선언된 크기만
    따라가며 센다(문자열 검색 아님)."""
    filesize = len(mm)
    pos = 0
    count = 0
    while pos + 8 <= filesize:
        if bytes(mm[pos:pos + 4]) != b"RIFF":
            break
        size = struct.unpack_from("<I", mm, pos + 4)[0]
        end = pos + 8 + size
        if end > filesize:
            break
        count += 1
        pos = end + (size & 1)
    return count, pos


def analyze_slack(mm):
    filesize = len(mm)
    if filesize < 12 or bytes(mm[0:4]) != b"RIFF":
        return {"kind": "not_riff"}

    top_riff_count, consumed_end = count_top_level_riffs(mm)
    extra_after_bytes = filesize - consumed_end
    reference_size = struct.unpack_from("<I", mm, 4)[0]  # 이 파일 자신의 최상위 RIFF 선언 크기

    hdrl, movi_list, idx1_chunk, _avix = find_top_level_sections(mm)
    embedded_riffs = []
    if movi_list and idx1_chunk is not None:
        movi = movi_list[0]
        embedded_riffs = find_embedded_riffs(mm, movi.content_start, movi.content_end, reference_size)

    return {
        "kind": "riff",
        "top_riff_count": top_riff_count,
        "extra_after_bytes": extra_after_bytes,
        "extra_after_pos": consumed_end,
        "hdrl": hdrl,
        "movi_list": movi_list,
        "idx1_chunk": idx1_chunk,
        "embedded_riffs": embedded_riffs,
    }


def _find_avih_total_frames_pos(mm, hdrl_chunk):
    for child in iter_chunks(mm, hdrl_chunk.content_start, hdrl_chunk.content_end):
        if child.ck_id == b"avih" and not child.is_list and child.ck_size >= 56:
            return child.data_start + 16
    return None


def repair_movi_slack(mm, hdrl, movi_list, idx1_chunk, work_path):
    """idx1 엔트리를 뒤에서부터 실제 chunk 헤더와 대조 검증해서 진짜 마지막
    으로 유효한 지점을 찾고, 그 뒤(예전 녹화 파일 잔재 + slack)를 전부 잘라
    낸 뒤 idx1을 그 자리에 다시 붙여서 유효한 AVI로 재구성한다."""
    if not movi_list or idx1_chunk is None:
        warn("[슬랙 리페어] movi/idx1을 구조적으로 찾지 못해 리페어를 건너뜀 - "
             "원본 그대로 사용")
        return None

    movi_chunk = movi_list[0]
    movi_fourcc_pos = movi_chunk.data_start
    idx1_entries = parse_idx1(mm, idx1_chunk.data_start, idx1_chunk.ck_size)
    if not idx1_entries:
        warn("[슬랙 리페어] idx1에 엔트리가 없어 리페어를 건너뜀 - 원본 그대로 사용")
        return None

    base_offset, base_label, scores, uncertain = detect_base_offset(mm, movi_fourcc_pos, idx1_entries)
    info(f"[슬랙 리페어] base offset 선택: {base_label} -> 0x{base_offset:X}"
         f"{' (불확실)' if uncertain else ''}")

    actual_movi_end = None
    kept_entry_count = len(idx1_entries)
    for i in range(len(idx1_entries) - 1, -1, -1):
        e = idx1_entries[i]
        chunk_offset = base_offset + e["idx_offset"]
        reasons, payload_offset, header_size = validate_chunk(mm, chunk_offset, e)
        if reasons == ["OK"]:
            end = payload_offset + e["length"]
            end += end & 1
            actual_movi_end = end
            kept_entry_count = i + 1
            break
        warn(f"[슬랙 리페어] idx1 마지막에서부터 검증 중 엔트리 #{i} "
             f"({e['chunk_id']!r}) validation={'|'.join(reasons)} - 이 엔트리는 버리고 계속 검사")

    if actual_movi_end is None:
        warn("[슬랙 리페어] idx1 안에 검증 통과하는 엔트리가 하나도 없어 리페어를 건너뜀 - "
             "원본 그대로 사용")
        return None

    if kept_entry_count < len(idx1_entries):
        warn(f"[슬랙 리페어] idx1 꼬리에서 {len(idx1_entries) - kept_entry_count}개 엔트리가 "
             f"검증 실패해 버려짐 (원래 {len(idx1_entries)}개 -> {kept_entry_count}개 유지)")

    new_movi_size = actual_movi_end - movi_chunk.pos - 8
    new_idx1_size = kept_entry_count * 16
    idx1_out_start = actual_movi_end
    output_size = idx1_out_start + 8 + new_idx1_size
    new_riff_size = output_size - 8

    if new_movi_size < 0 or new_riff_size < 0:
        warn("[슬랙 리페어] 계산된 크기가 음수 - 리페어를 건너뜀, 원본 그대로 사용")
        return None

    os.makedirs(os.path.dirname(os.path.abspath(work_path)), exist_ok=True)
    copy_block = 8 * 1024 * 1024
    with open(work_path, "w+b") as out:
        pos = 0
        while pos < actual_movi_end:
            block_end = min(pos + copy_block, actual_movi_end)
            out.write(bytes(mm[pos:block_end]))
            pos = block_end
        out.write(bytes(mm[idx1_chunk.pos:idx1_chunk.pos + 8]))
        for i in range(kept_entry_count):
            off = idx1_chunk.data_start + i * 16
            out.write(bytes(mm[off:off + 16]))

        out.seek(4)
        out.write(new_riff_size.to_bytes(4, "little"))
        out.seek(movi_chunk.pos + 4)
        out.write(new_movi_size.to_bytes(4, "little"))
        out.seek(idx1_out_start + 4)
        out.write(new_idx1_size.to_bytes(4, "little"))

        if hdrl is not None:
            frames_pos = _find_avih_total_frames_pos(mm, hdrl)
            if frames_pos is not None:
                frame_count = 0
                for i in range(kept_entry_count):
                    off = idx1_chunk.data_start + i * 16
                    cid = bytes(mm[off:off + 4])
                    if VIDEO_CHUNK_RE.fullmatch(cid):
                        frame_count += 1
                out.seek(frames_pos)
                out.write(frame_count.to_bytes(4, "little"))
                info(f"[슬랙 리페어] avih 총 프레임 수 갱신: {frame_count}")

    info(f"[슬랙 리페어] 완료 - 결과 파일 크기: {output_size:,} bytes "
         f"(원본 대비 {len(mm) - output_size:,} bytes 절단)")
    return work_path


def save_unknown_trailing_blob(mm, first_end, trailing, tag, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "trailing_unknown_data.bin")
    with open(out_path, "wb") as f:
        f.write(bytes(mm[first_end:first_end + trailing]))
    note_path = os.path.join(out_dir, "trailing_unknown_data.README.txt")
    tag_disp = tag.decode("ascii", errors="replace")
    with open(note_path, "w", encoding="utf-8") as f:
        f.write(
            "이 파일은 원본 AVI의 최상위 RIFF가 끝난 뒤에 남아있던 트레일링 데이터를 "
            "그대로 잘라 보존한 것입니다.\n"
            f"- 시작 오프셋(원본 파일 기준): 0x{first_end:X}\n"
            f"- 크기: {trailing:,} bytes\n"
            f"- 시작 4바이트 태그: {tag_disp!r}\n"
            "- 이 영역이 두 번째 RIFF(AVI/AVIX) 헤더로 시작하지 않아 자동으로 해석하지 "
            "않았습니다. 용도 미상의 벤더 고유 데이터로 보입니다. 원본에서는 잘라내지 "
            "않았고, 이 raw 파일로만 별도 보존합니다.\n"
            "- 주의: FineVu CustomGPS 계열에서 이 트레일링 블록을 GPS 저장 위치로 의심한 "
            "적이 있는데 아니었습니다. 그 계열의 GPS/충격센서는 movi 안 txts 스트림의 "
            "72바이트 고정 레코드에 들어 있고 이 스크립트가 이미 coordinates.csv 로 "
            "뽑아냅니다. 이 블록은 그것과 무관합니다.\n"
        )
    info(f"[슬랙 판단] 미상 트레일링 데이터를 별도 보존함: {out_path}")
    return out_path


def handle_slack(input_path, mm, out_dir, dry_run=False):
    """반환값: (사용할 파일 경로, 새 파일을 만들었는지 여부).
    out_dir(이 파일의 최종 결과 폴더) 안에 슬랙 제거본/미상 트레일링 raw를
    바로 만들어서 결과물로 눈에 보이게 남긴다.
    dry_run이면 판단 결과만 출력하고 파일은 하나도 만들지 않는다."""
    analysis = analyze_slack(mm)
    if analysis["kind"] == "not_riff":
        return input_path, False

    embedded = analysis["embedded_riffs"]
    top_count = analysis["top_riff_count"]
    extra_after = analysis["extra_after_bytes"]
    extra_after_pos = analysis["extra_after_pos"]

    for e in embedded:
        fname = sniff_embedded_filename(mm, e["pos"])
        reasons = []
        if e["same_as_reference"]:
            reasons.append("현재 파일과 동일한 선언 크기")
        if e["overruns_remaining"]:
            reasons.append("선언 크기가 남은 공간보다 큼(잘려나간 잔재)")
        info(f"[슬랙 판단] movi 영역 안에서 예전 녹화분으로 보이는 RIFF 발견: "
             f"0x{e['pos']:X} ({e['form'].decode('ascii', errors='replace')}, "
             f"{'/'.join(reasons)})"
             + (f" - 파일명 흔적: {fname!r}" if fname else ""))

    if extra_after >= TRAILING_IGNORE_THRESHOLD:
        tail_tag = bytes(mm[extra_after_pos:extra_after_pos + 4])
        if tail_tag == b"RIFF" and top_count >= 2:
            info(f"[슬랙 판단] 최상위 RIFF 뒤에 두 번째 RIFF가 이어붙어 있음 "
                 f"(0x{extra_after_pos:X}, {extra_after:,} bytes) - 중복 RIFF로 보고 "
                 f"절단 대상에 포함")
        elif dry_run:
            info(f"[슬랙 판단] 최상위 RIFF 뒤 미상 트레일링 데이터 "
                 f"{extra_after:,} bytes 발견(0x{extra_after_pos:X}, 시작 태그 "
                 f"{tail_tag.decode('ascii', errors='replace')!r}) - dry-run이라 "
                 f"trailing_unknown_data.bin은 만들지 않음")
        else:
            save_unknown_trailing_blob(mm, extra_after_pos, extra_after, tail_tag, out_dir)

    need_repair = bool(embedded) or top_count >= 2
    if not need_repair:
        return input_path, False

    info(f"[슬랙 판단] RIFF {top_count + len(embedded)}개 감지"
         f"(최상위 {top_count}개 + movi 내부 예전 파일 잔재 {len(embedded)}개) - "
         f"idx1 기준으로 실제 유효 구간만 남기고 구조적으로 절단 진행")
    if dry_run:
        info("[슬랙 판단] dry-run이라 <파일명>_wo_slack.avi는 만들지 않고 원본 그대로 "
             "추출을 진행함 - idx1은 현재 녹화분만 가리키므로 슬랙 유무는 GPS 추출 "
             "결과에 영향을 주지 않음")
        return input_path, False
    stem = os.path.splitext(os.path.basename(input_path))[0]
    work_path = os.path.join(out_dir, f"{stem}_wo_slack.avi")
    fixed = repair_movi_slack(mm, analysis["hdrl"], analysis["movi_list"],
                               analysis["idx1_chunk"], work_path)
    if fixed is None:
        return input_path, False
    return fixed, True


# ---- 여기부터: 이 파일 고유 - 파일 단위 처리 + CLI ----
def process_single_file(input_path, output_root, args):
    WARNINGS.clear()
    assert_riff_file(input_path)

    filesize = os.path.getsize(input_path)
    if filesize == 0:
        warn(f"{input_path}: 입력 파일 크기가 0입니다 - 건너뜀")
        return

    stem = os.path.splitext(os.path.basename(input_path))[0]
    out_dir = os.path.join(output_root, stem)
    if not args.dry_run:
        os.makedirs(out_dir, exist_ok=True)

    select_mode = args.select_mode or SELECT_MODE
    select_fcctypes = {v.encode("ascii") for v in args.fcctype} if args.fcctype else SELECT_FCCTYPES
    select_indices = set(args.index) if args.index else SELECT_INDICES
    select_chunk_ids = {v.encode("ascii") for v in args.chunk_id} if args.chunk_id else SELECT_CHUNK_IDS

    work_path = None
    with open(input_path, "rb") as f:
        mm0 = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
        process_path, is_new_file = handle_slack(input_path, mm0, out_dir,
                                                  dry_run=args.dry_run)
        mm0.close()
        if is_new_file:
            work_path = process_path

    active_path = work_path or input_path

    with open(active_path, "rb") as f:
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
                print(f"{input_path}: idx1을 찾지 못했습니다. 이 파일은 처리할 수 없습니다.", file=sys.stderr)
                mm.close()
                return
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

        base_offset, base_label, base_scores, base_uncertain = detect_base_offset(
            mm, movi_fourcc_pos, idx1_entries)
        info(f"\n[Base offset 선택] {base_label} -> base=0x{base_offset:X}"
             f"{' (불확실)' if base_uncertain else ''}")

        if not selected_streams:
            warn("선택된 스트림이 없습니다. SELECT_MODE/조건을 확인하세요.")

        # 재생 시간축: 텍스트 스트림 strh가 깨져 있는 기기가 많아 영상 스트림 길이를
        # 레코드 수로 나눠 균등 간격을 만든다(자세한 근거는 compute_video_duration 참고).
        video_duration, duration_source = compute_video_duration(mm, hdrl, stream_table)
        if video_duration:
            info(f"[시간축] 영상 길이 {video_duration:.3f}초 ({duration_source})")
        else:
            warn(f"[시간축] {duration_source} - start_time_sec 계열은 공란으로 둠")
        entries_per_stream = {}
        for e in idx1_entries:
            sidx = stream_index_from_chunk_id(e["chunk_id"])
            if sidx is not None:
                entries_per_stream[sidx] = entries_per_stream.get(sidx, 0) + 1
        stream_times = {}
        for st in selected_streams:
            n = entries_per_stream.get(st.index, 0)
            stream_times[st.index] = build_avi_stream_times(video_duration, n)

        # 72바이트 레코드 경로에서 레코드별 절대 시각(abs_time)을 만들려면 녹화 시작
        # 시각이 필요한데, 이 계열은 그 값을 파일 안에 안 남기고 파일명에만 남긴다
        # (뷰어도 날짜/시간을 파일명에서 가져온다).
        start_dt = finevu_filename_start_time(input_path)
        if start_dt is not None:
            info(f"[시간축] 파일명 기준 녹화 시작 시각 {start_dt:%Y-%m-%d %H:%M:%S}")

        result = extract_payload(
            mm, out_dir, selected_streams, idx1_entries, base_offset,
            dry_run=args.dry_run, stream_times=stream_times, start_dt=start_dt)

        save_metadata(out_dir, result["index_rows"], stream_table,
                       result["labels"], dry_run=args.dry_run)

        decode_summary = write_decoded_outputs(
            out_dir, selected_streams, result["labels"], result, dry_run=args.dry_run)

        # GPS/센서를 재생 시각 기준 한 줄로 합친 통합 타임라인(시각화용).
        n_timeline = write_avi_timeline(out_dir, selected_streams, result["labels"],
                                         result, dry_run=args.dry_run)
        if n_timeline:
            info(f"[시간축] timeline.csv {n_timeline}행 생성")

        info("\n" + "=" * 60)
        info(f"[요약] {input_path}")
        info(f"슬랙 리페어 적용 여부  : {'예 (' + work_path + ')' if work_path else '아니오'}")
        info(f"movi offset            : 0x{movi_fourcc_pos:X} "
             f"(발견된 movi 개수: {opendml_info['movi_count']})" if movi_fourcc_pos is not None
             else "movi offset            : 없음")
        info(f"idx1 offset             : 0x{idx1_start:X}")
        info(f"선택된 base 후보        : {base_label} (점수: {base_scores})")
        info(f"발견된 스트림 수        : hdrl dwStreams={dw_streams} vs "
             f"실제 관측={len(stream_table)}")
        info(f"선택 모드/대상          : {select_mode} -> {sorted(selected_chunk_ids)!r}")
        info(f"총 idx1 entry 개수      : {len(idx1_entries)}")
        for s in selected_streams:
            display_label = result["labels"][s.index][0]
            ds = decode_summary.get(s.index, {})
            kind = ds.get("kind")
            if kind == "text":
                extra = f", GPRMC/GPGGA 좌표 파싱 {ds.get('coord_count', 0)}개, 미분류 텍스트 {ds.get('unparsed_count', 0)}개"
            elif kind == "record72":
                extra = (f", FineVu 72바이트 레코드 {ds.get('coord_count', 0)}개 "
                         f"(측위 성공 {ds.get('fix_count', 0)}개, 충격센서 {ds.get('sensor_count', 0)}개)")
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
             f"({'dry-run이라 파일 미생성' if args.dry_run else os.path.join(out_dir, 'warnings.log') + ' 참조'})")
        info("=" * 60)

        mm.close()


def parse_args(argv):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("inputs", nargs="+", help="입력 AVI 파일 경로(들)")
    p.add_argument("-o", "--output", required=True, help="결과를 저장할 루트 디렉터리")
    p.add_argument("--dry-run", action="store_true",
                    help="파일 미생성, base 감지 + 스트림 선택 + validation + 요약만 출력")
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
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if not args.dry_run:
        os.makedirs(args.output, exist_ok=True)
    for input_path in args.inputs:
        process_single_file(input_path, args.output, args)


if __name__ == "__main__":
    main()
