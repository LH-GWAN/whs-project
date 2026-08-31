# ---- 이 파일 고유: MP4 슬랙(free/skip Box, Box 사이 gap, 꼬리) GPS/G센서 카빙 ----
import argparse
import csv
import math
import os
import re
import struct
import sys
from dataclasses import dataclass

if sys.stdout.encoding is None or sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except AttributeError:
        pass

DEBUG = False
WARNINGS = []


def warn(msg):
    WARNINGS.append(msg)
    print(f"[WARN] {msg}")


def info(msg):
    print(msg)


def debug(msg):
    if DEBUG:
        print(f"[DEBUG] {msg}")

# ---- 여기부터: integration_mp4.py / GPS_metadata_* 와 동일한 공통 파서 (관례대로 import 없이 복사) ----

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

TEXT_LENGTH_PREFIX_SIZE = 2

GSENSOR_PREFIX_RE = re.compile(
    r"^\$?gsensor(?P<subtype>[A-Za-z0-9]*)\s*,\s*(?P<rest>.*)$", re.IGNORECASE)

VENDOR_DOLLAR_RE = re.compile(r"^\$(?P<tag>[A-Za-z]+)(?P<rest>.*)$", re.DOTALL)

NMEA_TYPES_WITH_POSITION = ("RMC", "GGA")

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


# ==========================================================================
# 1. 슬랙 영역 찾기 (Box size를 따라가며 순회)
# ==========================================================================
SLACK_BOX_TYPES = {b"free", b"skip"}
# 최소 이 정도는 돼야 "레코드가 들어있을 수 있는 영역"으로 본다. 4바이트짜리
# 정렬용 free를 잡아봐야 의미가 없다.
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


# ==========================================================================
# 2. 슬랙 영역 안에서 레코드 카빙
#
# 슬랙에는 sample table이 없어서 offset/size를 계산할 수가 없다. 그래서 패턴으로
# 시작점만 찾고, 거기서부터 이어지는 printable ASCII 구간을 하나의 텍스트로 보고
# 정상 경로와 똑같은 파서(split_segments -> classify_segment)에 넘긴다.
# 매치 위치가 곧 절대 offset이라 원본 대조가 가능하다.
# ==========================================================================
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


# ==========================================================================
# 3. 출력 (정상 추출분과 같은 컬럼 구성 + 어디서 나왔는지)
# ==========================================================================
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


def carve_file(input_path, out_dir, dry_run=False, regions_only=False):
    WARNINGS.clear()
    filesize = os.path.getsize(input_path)
    if filesize == 0:
        warn(f"{input_path}: 파일 크기가 0")
        return None

    with open(input_path, "rb") as f:
        regions, boxes = find_slack_regions(f, filesize)
        box_kinds = {}
        for b in boxes:
            k = b.box_type.decode("ascii", errors="replace")
            box_kinds[k] = box_kinds.get(k, 0) + 1
        info(f"  top-level box: {box_kinds}")
        total_slack = sum(e - s for _, s, e in regions)
        info(f"  슬랙 영역 {len(regions)}개 / 합계 {total_slack:,} bytes "
             f"(전체의 {total_slack / filesize * 100:.1f}%)")
        for kind, s, e in regions:
            info(f"    [{kind}] 0x{s:08X} ~ 0x{e:08X}  ({e - s:,} bytes)")

        if regions_only:
            return {"regions": regions, "coord_rows": [], "sensor_rows": [], "filesize": filesize}

        coord_rows, sensor_rows, unparsed = [], [], []
        region_stats = []
        for kind, s, e in regions:
            f.seek(s)
            raw = f.read(e - s)
            # finditer에 넘길 절대 offset을 유지하려고 앞을 잘라 붙이지 않고,
            # 영역 raw를 따로 읽은 뒤 offset을 더해준다.
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
            region_stats.append({"region_kind": kind,
                                 "start": f"0x{s:08X}", "end": f"0x{e:08X}",
                                 "size_bytes": e - s, "gps_records": n_gps,
                                 "gsensor_records": n_sen})

    coord_rows.sort(key=lambda r: int(r["absolute_offset"], 16))
    sensor_rows.sort(key=lambda r: int(r["absolute_offset"], 16))

    if not dry_run:
        slack_dir = os.path.join(out_dir, "slack")
        os.makedirs(slack_dir, exist_ok=True)
        write_csv(os.path.join(slack_dir, "slack_regions.csv"), region_stats)
        write_csv(os.path.join(slack_dir, "slack_coordinates.csv"), coord_rows)
        apply_gsensor_calibration(sensor_rows)
        write_csv(os.path.join(slack_dir, "slack_sensor_values.csv"), sensor_rows)
        with_coord = [r for r in coord_rows if r["latitude"]]
        if with_coord:  # 좌표가 하나도 없으면 빈 파일을 만들지 않는다(csv 쪽과 동일한 규칙).
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
        with open(os.path.join(slack_dir, "warnings.log"), "w", encoding="utf-8") as fp:
            for w in WARNINGS:
                fp.write(w + "\n")

    return {"regions": regions, "region_stats": region_stats, "coord_rows": coord_rows,
            "sensor_rows": sensor_rows, "filesize": filesize}


def summarize(stem, result, file_date_hint=None):
    coord = result["coord_rows"]
    dates = sorted({r["date"] for r in coord if r["date"]})
    csfail = sum(1 for r in coord if r["checksum_ok"] is False or r["checksum_ok"] == "False")
    info(f"  카빙 결과: GPS {len(coord)}건 / GSENSOR {len(result['sensor_rows'])}건 "
         f"(checksum 실패 {csfail}건)")
    if dates:
        info(f"  슬랙 GPS 기록일: {dates}")
        if file_date_hint:
            older = [d for d in dates if d < file_date_hint]
            info(f"    -> 파일 녹화일({file_date_hint})보다 과거인 날짜 {len(older)}/{len(dates)}개"
                 f"{' (이전 녹화 잔재로 판단)' if older else ''}")


def guess_file_date(path):
    """파일명에 박힌 YYYYMMDD 또는 YYYY_MM_DD를 녹화일 힌트로 쓴다(판단용 참고값)."""
    base = os.path.basename(path)
    m = re.search(r"(20\d{2})[-_]?(\d{2})[-_]?(\d{2})", base)
    if not m:
        return None
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"


def parse_args(argv):
    p = argparse.ArgumentParser(
        description="MP4 슬랙(free/skip Box, Box 사이 gap, 꼬리 영역)에서 GPS/G센서 "
                    "레코드를 카빙한다. 원본은 수정하지 않는다.")
    p.add_argument("inputs", nargs="+", help="입력 MP4 파일 경로(들)")
    p.add_argument("-o", "--output", default=None,
                    help="결과 루트 디렉터리 (--regions-only/--dry-run 이면 생략 가능)")
    p.add_argument("--dry-run", action="store_true", help="파일을 만들지 않고 요약만 출력")
    p.add_argument("--regions-only", action="store_true",
                    help="슬랙 영역 목록만 출력하고 카빙은 하지 않음")
    args = p.parse_args(argv)
    if args.output is None and not (args.dry_run or args.regions_only):
        p.error("-o/--output 은 --dry-run/--regions-only 가 아닐 때 반드시 필요합니다")
    return args


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.output and not (args.dry_run or args.regions_only):
        os.makedirs(args.output, exist_ok=True)

    results = []
    for path in args.inputs:
        info("=" * 70)
        info(f"[슬랙 카빙] {path}")
        if not os.path.isfile(path):
            info("  파일을 찾을 수 없음 - 건너뜀")
            continue
        stem = os.path.splitext(os.path.basename(path))[0]
        out_dir = os.path.join(args.output, stem) if args.output else None
        r = carve_file(path, out_dir,
                        dry_run=args.dry_run or args.regions_only,
                        regions_only=args.regions_only)
        if r is None:
            continue
        if not args.regions_only:
            summarize(stem, r, guess_file_date(path))
        results.append((stem, r))

    info("\n" + "=" * 70)
    info("[전체 요약]")
    for stem, r in results:
        total = sum(e - s for _, s, e in r["regions"])
        info(f"  {stem:34} 슬랙 {total:>12,}B  GPS {len(r['coord_rows']):>4}건  "
             f"GSENSOR {len(r['sensor_rows']):>4}건")
    info("=" * 70)


if __name__ == "__main__":
    main()
