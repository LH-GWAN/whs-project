import argparse
import csv
import math
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


WARNINGS = []
MAX_TABLE_ENTRIES = 1_000_000
SUPPORTED_TEXT_HANDLERS = {b"text", b"sbtl", b"subt"}


def warn(msg):
    WARNINGS.append(msg)
    print(f"[WARN] {msg}")


def info(msg):
    print(msg)


def hex_preview_lines(payload, n=32):
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


def iter_boxes(f, start, end, context=""):
    pos = start
    while pos + 8 <= end:
        f.seek(pos)
        header = f.read(8)
        if len(header) < 8:
            warn(f"{context} @ 0x{pos:X}: Box 헤더(8바이트)를 다 읽지 못함 - 순회 중단")
            break

        size32 = struct.unpack(">I", header[0:4])[0]
        box_type = header[4:8]
        header_size = 8

        if size32 == 1:
            ext = f.read(8)
            if len(ext) < 8:
                warn(f"{context} @ 0x{pos:X}: extended size(64bit) 읽기 실패 - 순회 중단")
                break
            size = struct.unpack(">Q", ext)[0]
            header_size = 16
        elif size32 == 0:
            size = end - pos
        else:
            size = size32

        if size < header_size:
            warn(f"{context} Box {box_type!r} @ 0x{pos:X}: size({size})가 header_size"
                 f"({header_size})보다 작음 - 순회 중단")
            break
        if pos + size > end:
            warn(f"{context} Box {box_type!r} @ 0x{pos:X}: size가 부모 경계(0x{end:X})를 "
                 f"넘어감(box end=0x{pos+size:X}) - 순회 중단")
            break

        yield Box(box_type, pos, size, header_size)
        pos += size


def find_box(boxes, box_type):
    for b in boxes:
        if b.box_type == box_type:
            return b
    return None


def find_all(boxes, box_type):
    return [b for b in boxes if b.box_type == box_type]


def parse_ftyp(f, box):
    f.seek(box.payload_start)
    payload = f.read(box.size - box.header_size)
    if len(payload) < 8:
        warn("ftyp payload가 너무 짧음")
        return None
    major_brand = payload[0:4]
    minor_version = struct.unpack(">I", payload[4:8])[0]
    compat = payload[8:]
    compatible_brands = [compat[i:i+4] for i in range(0, len(compat) - (len(compat) % 4), 4)]
    return {
        "major_brand": major_brand,
        "minor_version": minor_version,
        "compatible_brands": compatible_brands,
    }


def parse_hdlr(f, hdlr_box):
    f.seek(hdlr_box.payload_start)
    payload = f.read(hdlr_box.size - hdlr_box.header_size)
    if len(payload) < 24:
        warn(f"hdlr @ 0x{hdlr_box.start:X}: payload가 24바이트보다 작음")
        return None, ""
    handler_type = payload[8:12]
    name_bytes = payload[24:]
    name = ""
    if name_bytes:
        plen = name_bytes[0]
        if 0 < plen <= len(name_bytes) - 1:
            try:
                name = name_bytes[1:1+plen].decode("utf-8", errors="replace")
            except Exception:
                name = ""
        if not name:
            raw = name_bytes.split(b"\x00", 1)[0]
            try:
                name = raw.decode("utf-8", errors="replace")
            except Exception:
                name = ""
    return handler_type, name


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


@dataclass
class TrackInfo:
    track_number: int
    trak_box: Box
    handler_type: bytes = None
    handler_name: str = ""
    stsd_entries: list = field(default_factory=list)
    samples: list = field(default_factory=list)
    is_text_track: bool = False


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
    return ti


TEXT_LENGTH_PREFIX_SIZE = 2

GSENSOR_PREFIX_RE = re.compile(r"^gsensor(?P<subtype>[A-Za-z0-9]*)\s*,\s*(?P<rest>.*)$",
                                re.IGNORECASE)

KEYWORD_CANDIDATES = [
    "gps", "GPS", "gsensor", "G-sensor", "NMEA", "GPRMC", "GPGGA",
    "latitude", "longitude", "speed",
]

EMBEDDED_NMEA_RE = re.compile(rb"\$?([A-Z]{2}(?:RMC|GGA)[^\x00\r\n;]*)")


def decode_sample_text(raw_bytes):
    if len(raw_bytes) >= TEXT_LENGTH_PREFIX_SIZE:
        declared_len = struct.unpack(">H", raw_bytes[:2])[0]
        if declared_len + TEXT_LENGTH_PREFIX_SIZE == len(raw_bytes):
            text_bytes = raw_bytes[2:2+declared_len]
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
        deg = int(value_str[:deg_digits]); minutes = float(value_str[deg_digits:])
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
        hh = int(hhmmss[:2]); mm = int(hhmmss[2:4]); ss = float(hhmmss[4:])
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
            if math.isfinite(v) and v >= 0: speed_kmh = v * 1.852
            else: warnings.append("invalid_speed")
        except (ValueError, OverflowError): warnings.append("invalid_speed")
    status = fields[2].strip().upper() if len(fields) > 2 else ""
    if status not in {"A", "V", ""}: warnings.append("invalid_status")
    return {"lat":lat,"lon":lon,"date":format_nmea_date(fields[9].strip()),
            "utc_time":format_nmea_time(fields[1].strip()),"status":status,
            "status_valid":status=="A","speed_knots":speed_knots,"speed_kmh":speed_kmh,
            "track_deg":fields[8].strip() if len(fields)>8 else "",
            "magvar":fields[10].strip() if len(fields)>10 else "",
            "magvar_dir":fields[11].strip().upper() if len(fields)>11 else "",
            "mode":fields[12].strip().upper() if len(fields)>12 else "",
            "parse_warnings":";".join(warnings)}

def parse_gga(fields):
    if len(fields) < 10:
        return None
    lat = _dm_to_decimal(fields[2].strip(), 2, fields[3].strip().upper(), "S")
    lon = _dm_to_decimal(fields[4].strip(), 3, fields[5].strip().upper(), "W")
    if lat is None or lon is None:
        return None
    quality = fields[6].strip() if len(fields)>6 else ""
    return {"lat":lat,"lon":lon,"date":"","utc_time":format_nmea_time(fields[1].strip()),
            "status":quality,"status_valid":quality.isdigit() and int(quality)>0,
            "speed_knots":"","speed_kmh":None,"track_deg":"","magvar":"","magvar_dir":"","mode":"",
            "altitude_m":fields[9].strip() if len(fields)>9 else "",
            "parse_warnings":"" if quality.isdigit() else "invalid_fix_quality"}

NMEA_PARSERS = {"RMC": parse_rmc, "GGA": parse_gga}


def try_parse_nmea(line):
    if not isinstance(line, str):
        return None
    raw = line.strip("\x00\r\n\t ")
    body_with_checksum = raw[1:] if raw.startswith("$") else raw
    checksum_ok = nmea_checksum_ok(body_with_checksum)
    body = body_with_checksum.split("*",1)[0]
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
        warn(f"NMEA {sentence_type} 파싱 실패: {exc} - segment는 generic으로 보존")
        return None
    if parsed is None:
        return None
    parsed.update({"talker":talker,"sentence_type":sentence_type,"raw":raw,"checksum_ok":checksum_ok})
    parsed["trusted"] = bool(parsed.get("status_valid", True) and checksum_ok is not False and not parsed.get("parse_warnings"))
    return parsed

def split_segments(text):
    return [seg for seg in (s.strip() for s in text.split(";")) if seg]


def classify_segment(segment):
    m = GSENSOR_PREFIX_RE.match(segment)
    if m:
        rest = m.group("rest")
        raw_fields = [x.strip() for x in rest.split(",")] if rest else []
        return "gsensor", {
            "subtype": m.group("subtype"),
            "fields": raw_fields,
        }

    nmea = try_parse_nmea(segment)
    if nmea is not None:
        return "gps_nmea", nmea

    parts = segment.split(",")
    label = parts[0] if parts else segment
    return "generic", {"label": label, "fields": parts[1:], "raw": segment}


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
    keyword_hits_rows = []

    classify_counts = {"gsensor": 0, "gps_nmea": 0, "generic": 0, "length_prefix_mismatch": 0,
                        "undecodable": 0}
    preview_count = 0

    for s in t.samples:
        offset = s.absolute_offset
        size = s.size
        out_of_range = (offset < 0) or (offset + size > filesize)

        index_row = {
            "track": t.track_number,
            "sample": s.sample_number,
            "chunk": s.chunk_number,
            "sample_description_index": s.sample_description_index,
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

        for segment in split_segments(text):
            kind, payload = classify_segment(segment)
            classify_counts[kind] = classify_counts.get(kind, 0) + 1

            if kind == "gsensor":
                row = {
                    "sample": s.sample_number,
                    "absolute_offset": f"0x{offset:08X}",
                    "subtype": payload["subtype"],
                }
                for i, v in enumerate(payload["fields"]):
                    row[f"field_{i}"] = v
                sensor_rows.append(row)

            elif kind == "gps_nmea":
                parsed = payload
                speed_kmh = parsed.get("speed_kmh")
                coord_rows.append({
                    "sample": s.sample_number,
                    "date": parsed.get("date", ""),
                    "utc_time": parsed.get("utc_time", ""),
                    "status": parsed.get("status", ""),
                    "latitude": f"{parsed['lat']:.6f}",
                    "longitude": f"{parsed['lon']:.6f}",
                    "speed_knots": parsed.get("speed_knots", ""),
                    "speed_kmh": f"{speed_kmh:.3f}" if speed_kmh is not None else "",
                    "track_deg": parsed.get("track_deg", ""),
                    "sentence_type": parsed["sentence_type"],
                    "checksum_ok": parsed["checksum_ok"],
                    "status_valid": parsed.get("status_valid", ""),
                    "trusted": parsed.get("trusted", ""),
                    "parse_warnings": parsed.get("parse_warnings", ""),
                    "raw_sentence": parsed["raw"],
                })

            else:
                row = {"sample": s.sample_number, "label": payload["label"]}
                for i, v in enumerate(payload["fields"]):
                    row[f"field_{i}"] = v
                row["raw"] = payload["raw"]
                generic_rows.append(row)

    if not dry_run:
        _write_csv(os.path.join(track_dir, "index.csv"), index_rows)
        if coord_rows:
            _write_coord_outputs(track_dir, coord_rows)
        if sensor_rows:
            _write_csv(os.path.join(track_dir, "sensor_values.csv"), sensor_rows)
        if generic_rows:
            _write_csv(os.path.join(track_dir, "other_segments_unparsed.csv"), generic_rows)
        if keyword_hits_rows:
            _write_csv(os.path.join(track_dir, "keyword_hits.csv"), keyword_hits_rows)

    return {
        "classify_counts": classify_counts,
        "coord_count": len(coord_rows),
        "sensor_count": len(sensor_rows),
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
        for i, row in enumerate(coord_rows, start=1):
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


def parse_args(argv):
    p = argparse.ArgumentParser()
    p.add_argument("input", help="입력 MP4 파일 경로")
    p.add_argument("output", help="출력 디렉터리")
    p.add_argument("--list-tracks", action="store_true",
                    help="Track 목록만 발견/출력하고 종료(추출 안 함)")
    p.add_argument("--dry-run", action="store_true",
                    help="파일 미생성, 구조 파싱 + 미리보기 + 요약만 출력")
    p.add_argument("--extract", action="store_true",
                    help="각 Sample 원본도 .bin 파일로 저장")
    p.add_argument("--track", action="append", type=int, default=None,
                    help="분석할 text Track 번호(전체 트랙 기준 1-based) 지정. "
                         "여러 번 지정 가능, 미지정시 모든 text Track")
    return p.parse_args(argv)


def main(argv=None):
    WARNINGS.clear()
    args = parse_args(sys.argv[1:] if argv is None else argv)

    filesize = os.path.getsize(args.input)
    if filesize == 0:
        print("입력 파일 크기가 0입니다.", file=sys.stderr)
        sys.exit(1)

    with open(args.input, "rb") as f:
        top_boxes = list(iter_boxes(f, 0, filesize, context="top-level"))
        if find_box(top_boxes, b"moof") is not None:
            warn("fragmented MP4(moof) 감지 - 현재 버전은 moov/stbl sample table 기반이며 moof/traf/trun은 해석하지 않음")

        info("=" * 60)
        info("MP4 FILE")
        info("=" * 60)
        info(f"파일: {args.input}")
        info(f"크기: {filesize:,} bytes")

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
                info(f"  minor_version     : {ftyp['minor_version']}")
                compat_str = ", ".join(b.decode('ascii', errors='replace')
                                        for b in ftyp['compatible_brands'])
                info(f"  compatible_brands : {compat_str}")
        else:
            warn("ftyp Box를 찾지 못함 (필수는 아니지만 일반적으로 존재함)")

        moov_boxes = find_all(top_boxes, b"moov")
        if not moov_boxes:
            print("moov Box를 찾지 못했습니다. 이 파일은 처리할 수 없습니다.", file=sys.stderr)
            sys.exit(1)
        info(f"\nmoov 개수: {len(moov_boxes)}")

        all_tracks = []
        track_number = 0
        for mi, moov_box in enumerate(moov_boxes, start=1):
            info(f"\n{'='*60}\nMOOV #{mi} (offset=0x{moov_box.start:X}, "
                 f"size={moov_box.size}, end=0x{moov_box.end:X})\n{'='*60}")
            moov_children = list(iter_boxes(f, moov_box.payload_start, moov_box.end,
                                             context=f"moov@0x{moov_box.start:X}"))
            trak_boxes = find_all(moov_children, b"trak")
            for trak_box in trak_boxes:
                track_number += 1
                ti = parse_track(f, trak_box, track_number)
                all_tracks.append(ti)

        print_track_table(all_tracks)

        text_tracks = [t for t in all_tracks if t.is_text_track]
        if args.track:
            wanted = set(args.track)
            text_tracks = [t for t in text_tracks if t.track_number in wanted]
            missing = wanted - {t.track_number for t in text_tracks}
            for m in missing:
                warn(f"--track {m} 지정했지만 해당 번호는 text Track이 아니거나 존재하지 않음")

        if not text_tracks:
            warn("지원 text/subtitle handler(text/sbtl/subt) Track을 하나도 찾지 못함(또는 --track 필터로 모두 제외됨)")

        if args.list_tracks:
            info("\n--list-tracks 모드: 추출 없이 종료")
            return

        results = {}
        for t in text_tracks:
            info(f"\n{'='*60}")
            info(f"Track #{t.track_number} (text) 추출")
            info(f"{'='*60}")
            result = extract_text_track(f, filesize, t, args.output,
                                         dry_run=args.dry_run,
                                         do_bin_extract=args.extract)
            results[t.track_number] = result

        save_track_summary(args.output, all_tracks, dry_run=args.dry_run)

        info("\n" + "=" * 60)
        info("[요약]")
        info(f"전체 Track 수         : {len(all_tracks)}")
        info(f"text Track 수         : {len(text_tracks)} "
             f"({sorted(t.track_number for t in text_tracks)})")
        for t in text_tracks:
            r = results.get(t.track_number, {})
            cc = r.get("classify_counts", {})
            info(f"  - Track #{t.track_number}: Sample {len(t.samples)}개, "
                 f"GPS(NMEA) 좌표 {r.get('coord_count', 0)}개, "
                 f"G센서 레코드 {r.get('sensor_count', 0)}개, "
                 f"미상 세그먼트 {r.get('generic_count', 0)}개 "
                 f"(분류상세={cc})")
        info(f"경고 총 개수           : {len(WARNINGS)} "
             f"({'dry-run이라 파일 미생성' if args.dry_run else os.path.join(args.output, 'warnings.log') + ' 참조'})")
        info("=" * 60)


if __name__ == "__main__":
    main()
