"""
GPS_metadata_mp4_udta_mamt_GNRMC.py

non-fragmented MP4 파일 중 "GPS text track이 없고, moov/udta/mamt 안에
$GNRMC NMEA 문장이 저장되어 있는" 예외적인 블랙박스 포맷 전용 GPS 추출 스크립트.

이 스크립트는 다음과 같은 조건을 만족하는 MP4 파일에 적용된다 (자동으로 판별함).
    1. non-fragmented MP4 (moof 박스가 아니라 moov 박스를 사용)
    2. moov 안의 모든 trak을 확인했을 때 handler_type == "text" 인 트랙이 없음
    3. moov -> udta -> mamt 커스텀 박스 안에 "$GNRMC,...*XX\\r\\n" 형태의
       NMEA RMC 문장이 가변 길이로 여러 개 연속 저장되어 있음

위 조건 중 하나라도 맞지 않으면(예: text track이 존재, udta/mamt가 없음, moof
기반 fragmented mp4 등) 해당 파일은 건너뛰고 이유를 알려준다 - 즉 이 파일은
다른 스크립트(GPS_metadata_mp4_pvc1_Atext.py, GPS_metadata_fregment_iso4_Atext.py
등)로 처리해야 하는 케이스라는 뜻이다.

CSV 출력 형식은 GPS_metadata_GPRMC.py(AVI용)가 만드는 coordinates.csv와
동일한 컬럼 구성을 따른다. 다만 AVI 고유 개념인 idx1_entry_offset/chunk_id는
MP4에 그대로 존재하지 않으므로 다음과 같이 의미를 대체했다.
    - idx1_entry_offset -> 파일 내에서 해당 NMEA 문장이 시작하는 절대 byte offset
    - chunk_id           -> 항상 "mamt" 고정값 (모든 GPS 문장이 mamt 박스 하나에서 나오므로)

사용법:
    python GPS_metadata_mp4_udta_mamt_GNRMC.py <output_dir> <input1.mp4> [input2.mp4 ...]

입력 파일마다 <output_dir>/<파일명(확장자 제외)>/GPS_GNRMC/ 아래에
coordinates.csv, coordinates.txt, unparsed_lines.txt, raw_concat.bin,
raw_chunks/*.bin, warnings.log 를 생성한다.
"""

import argparse
import csv
import math
import os
import re
import struct
import sys

if sys.stdout.encoding is None or sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except AttributeError:
        pass


WARNINGS = []
TEXT_HANDLER_TYPES = {b"text", b"sbtl", b"subt"}


def warn(msg):
    WARNINGS.append(msg)
    print(f"[WARN] {msg}")


def info(msg):
    print(msg)


# ---------------------------------------------------------------------------
# 1. MP4 Box 구조 순회 (top-level 공통으로 재사용)
#
# MP4의 모든 Box는 [4-byte size][4-byte type][payload] 형태이고 size는
# Big Endian이며 "해당 Box 시작 위치부터 계산한 전체 크기"이다. 따라서
# 다음 Box의 시작 위치는 "현재 Box 시작 offset + size" 로 계산할 수 있고,
# 이 규칙만 있으면 특정 Box가 어떤 Box 바로 뒤에 오는지 몰라도 순서대로
# 모든 형제(sibling) Box를 훑을 수 있다.
# ---------------------------------------------------------------------------
class Box:
    __slots__ = ("box_type", "start", "size", "header_size")

    def __init__(self, box_type, start, size, header_size):
        self.box_type = box_type
        self.start = start
        self.size = size
        self.header_size = header_size

    @property
    def payload_start(self):
        return self.start + self.header_size

    @property
    def end(self):
        return self.start + self.size


def iter_boxes(f, start, end, context=""):
    """[start, end) 구간 안에서 형제 Box들을 순서대로 yield한다.

    - size가 1이면 뒤이어 오는 8바이트가 64bit 실제 크기(extended size)이다.
    - size가 0이면 "이 Box가 부모 영역 끝까지 이어진다"는 뜻이다.
    - size가 이상하면(0보다 작거나, 부모 경계를 넘어가면) 더 이상 이 구간을
      신뢰할 수 없으므로 경고를 남기고 순회를 중단한다. 이렇게 하면 손상된
      size 값 때문에 무한 루프에 빠지거나 엉뚱한 위치를 계속 읽는 것을 막는다.
    """
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
            warn(f"{context} Box {box_type!r} @ 0x{pos:X}: size({size})가 "
                 f"header_size({header_size})보다 작음 - 순회 중단")
            break
        if pos + size > end:
            warn(f"{context} Box {box_type!r} @ 0x{pos:X}: size가 부모 경계"
                 f"(0x{end:X})를 넘어감(box end=0x{pos + size:X}) - 순회 중단")
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


# ---------------------------------------------------------------------------
# 2. trak -> mdia -> hdlr 탐색
#
# 각 trak이 어떤 종류의 트랙인지는 trak/mdia/hdlr Box 안의 handler_type
# 필드(4바이트)로 알 수 있다. hdlr payload 레이아웃은 다음과 같다.
#   version(1) + flags(3) + pre_defined(4) + handler_type(4) + ...
# 즉 payload 시작에서 8바이트를 건너뛰면 handler_type 4바이트가 나온다.
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# 3. NMEA GNRMC/GPRMC 파싱 (talker ID만 다르고 필드 구조는 동일하므로 공통 처리)
# ---------------------------------------------------------------------------
def nmea_checksum_ok(sentence):
    """'$' 로 시작하고 '*XX'로 끝나는 NMEA 문장의 checksum(XOR)을 검증한다."""
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
    """NMEA의 ddmm.mmmmm / dddmm.mmmmm 형식을 decimal degree로 변환한다.

    예) 3732.55779, N, deg_digits=2  ->  37 + 32.55779/60
        12702.16154, E, deg_digits=3 ->  127 + 02.16154/60
    """
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
    return f"{hh:02d}:{mm:02d}:{hhmmss[4:]}"


def parse_rmc(fields):
    """RMC 필드를 파싱한다. talker가 GP든 GN이든 필드 위치는 동일하다."""
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


def try_parse_rmc_sentence(raw):
    """'$GNRMC,...*XX' 형태의 한 문장을 checksum 검증 + 필드 파싱까지 수행한다."""
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


# ---------------------------------------------------------------------------
# 4. 파일 하나를 분석해서 "이 케이스에 해당하는가"를 먼저 판별
# ---------------------------------------------------------------------------
def locate_gps_source(f, filesize):
    """moov를 찾고, 모든 trak의 hdlr을 확인한 뒤, text track이 없으면
    moov -> udta -> mamt 를 찾아 (mamt Box)를 반환한다.

    반환값: (mamt_box, reason)
        - 이 파일이 대상 케이스이면 mamt_box는 Box 인스턴스, reason은 None
        - 대상 케이스가 아니면 mamt_box는 None, reason은 사람이 읽을 수 있는 설명
    """
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

    # 모든 trak의 handler_type을 끝까지 확인한다 (첫 trak만 보고 판단하지 않는다).
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


# ---------------------------------------------------------------------------
# 5. mamt payload 안에서 $GNRMC/$GPRMC 문장을 가변 길이로 추출
# ---------------------------------------------------------------------------
def extract_rmc_sentences(payload, payload_abs_start):
    """payload(bytes) 안에서 '$..RMC' 로 시작해 CRLF로 끝나는 문장들을 순서대로
    추출한다. 문장 길이는 고정되어 있지 않으므로 매번 다음 CRLF까지 찾는다.
    파싱에 실패한 문장이 있어도 다음 '$..RMC'부터 계속 진행한다.
    """
    sentences = []  # (abs_offset, raw_text)
    search_pos = 0
    n = len(payload)
    pattern = re.compile(rb"\$G[NP]RMC")
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
        search_pos = end + 2  # CRLF 다음부터 다음 문장 탐색
    return sentences


# ---------------------------------------------------------------------------
# 6. 파일 하나 처리 -> CSV 등 출력
# ---------------------------------------------------------------------------
def process_file(input_path, output_root):
    WARNINGS.clear()
    info("=" * 70)
    info(f"[처리 시작] {input_path}")

    filesize = os.path.getsize(input_path)
    if filesize == 0:
        warn("파일 크기가 0바이트")
        return {"ok": False, "reason": "empty file"}

    with open(input_path, "rb") as f:
        mamt_box, reason = locate_gps_source(f, filesize)
        if mamt_box is None:
            info(f"[SKIP] {input_path}: {reason}")
            return {"ok": False, "reason": reason}

        mamt_payload_start = mamt_box.payload_start
        mamt_payload_end = mamt_box.end
        info(f"  mamt payload: 0x{mamt_payload_start:X} - 0x{mamt_payload_end:X} "
             f"({mamt_payload_end - mamt_payload_start} bytes)")

        f.seek(mamt_payload_start)
        payload = f.read(mamt_payload_end - mamt_payload_start)

    sentences = extract_rmc_sentences(payload, mamt_payload_start)
    info(f"  '$GxRMC' 문장 {len(sentences)}개 발견")

    stem = os.path.splitext(os.path.basename(input_path))[0]
    stream_dir = os.path.join(output_root, stem, "GPS_GNRMC")
    raw_chunks_dir = os.path.join(stream_dir, "raw_chunks")
    os.makedirs(raw_chunks_dir, exist_ok=True)

    coord_rows = []
    unparsed_lines = []

    raw_concat_path = os.path.join(stream_dir, "raw_concat.bin")
    with open(raw_concat_path, "wb") as raw_concat_f:
        for seq, (abs_offset, raw) in enumerate(sentences):
            raw_bytes = raw.encode("ascii", errors="replace")
            raw_chunk_path = os.path.join(raw_chunks_dir, f"gnrmc_{seq:06d}.bin")
            with open(raw_chunk_path, "wb") as cf:
                cf.write(raw_bytes)
            raw_concat_f.write(raw_bytes + b"\r\n")

            parsed = try_parse_rmc_sentence(raw)
            if parsed is None:
                unparsed_lines.append((seq, raw))
                warn(f"entry #{seq} (offset 0x{abs_offset:X}) RMC 파싱 실패 - "
                     f"원문 보존만 하고 다음 문장으로 계속 진행: {raw!r}")
                continue

            speed_kmh = parsed.get("speed_kmh")
            coord_rows.append({
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
                # AVI 원본의 idx1_entry_offset(스트림 청크의 idx1 offset) 대신,
                # MP4에서는 해당 NMEA 문장이 파일 안에서 시작하는 절대 offset을 사용한다.
                "idx1_entry_offset": f"0x{abs_offset:08X}",
                # AVI 원본의 chunk_id(RIFF 4바이트 fourCC) 대신, 모든 GPS 문장이
                # 단일 mamt 커스텀 박스에서 나오므로 고정값 "mamt"를 사용한다.
                "chunk_id": "mamt",
                "sentence_type": parsed["sentence_type"],
                "raw_sentence": parsed["raw"],
            })

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

    with open(os.path.join(stream_dir, "warnings.log"), "w", encoding="utf-8") as fw:
        for w_msg in WARNINGS:
            fw.write(w_msg + "\n")

    checksum_fail = sum(1 for r in coord_rows if r["checksum_ok"] is False)
    info(f"  결과: 좌표 {len(coord_rows)}개 파싱 / 미파싱 {len(unparsed_lines)}개 / "
         f"checksum 실패 {checksum_fail}개")
    info(f"  출력 위치: {stream_dir}")

    return {
        "ok": True,
        "coord_count": len(coord_rows),
        "unparsed_count": len(unparsed_lines),
        "checksum_fail": checksum_fail,
        "output_dir": stream_dir,
    }


def parse_args(argv):
    p = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("output_dir", help="결과를 저장할 출력 디렉터리")
    p.add_argument("inputs", nargs="+", help="입력 MP4 파일 경로(여러 개 가능)")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    os.makedirs(args.output_dir, exist_ok=True)

    results = []
    for input_path in args.inputs:
        if not os.path.isfile(input_path):
            info(f"[SKIP] {input_path}: 파일을 찾을 수 없음")
            results.append((input_path, {"ok": False, "reason": "file not found"}))
            continue
        try:
            result = process_file(input_path, args.output_dir)
        except Exception as exc:  # 배치 처리 중 한 파일이 실패해도 나머지는 계속 진행
            warn(f"예외 발생으로 처리 중단: {exc}")
            result = {"ok": False, "reason": f"exception: {exc}"}
        results.append((input_path, result))

    info("\n" + "=" * 70)
    info("[전체 요약]")
    for input_path, result in results:
        if result["ok"]:
            info(f"  OK   {input_path} -> 좌표 {result['coord_count']}개 "
                 f"(미파싱 {result['unparsed_count']}, checksum 실패 {result['checksum_fail']})")
        else:
            info(f"  SKIP {input_path} -> {result['reason']}")
    info("=" * 70)


if __name__ == "__main__":
    main()
