"""AVI txts/dats 스트림에 든 FineVu 72바이트 고정 이진 레코드에서 GPS/충격센서를 뽑는다.

GPS_metadata_avi.py 는 txts 스트림을 NMEA 텍스트로 보고 디코딩한다. 그런데 같은
txts 스트림에 텍스트가 아니라 72바이트 고정 이진 레코드를 넣는 계열이 있다
(FineVu X3000/X700 등). 그 파일들은 GPS_metadata_avi.py 로 돌리면 스트림 전체가
BINARY/미상으로 빠져 raw만 남는다 - 이 스크립트가 그 raw를 해석한다.

RIFF/idx1 저수준 파싱은 GPS_metadata_avi.py 를 import 해서 그대로 쓰고
(GPS_metadata_GPRMC.py 와 같은 방식), 이 파일은 레코드 해석만 담당한다.

    python GPS_metadata_avi_txts_record72.py 입력.avi 출력디렉터리
"""
import argparse
import csv
import datetime
import math
import mmap
import os
import re
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import GPS_metadata_avi as carve


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
# 여기부터: 이 파일 고유 - 스트림 판정 + 추출 + 출력 + CLI
# ---------------------------------------------------------------------------
RECORD_MIN_FRACTION = 0.8
SNIFF_SAMPLE_SIZE = 8

COORD_FIELDS = [
    "start_time_sec", "end_time_sec", "time_source",
    "elapsed_sec", "elapsed_delta_sec", "abs_time",
    "status", "latitude", "longitude", "speed_kmh", "track_deg",
    "x_g", "y_g", "z_g",
    "hemi_flag", "lat_raw", "lon_raw", "coord_format",
    "status_valid", "trusted", "parse_warnings",
    "sequence", "idx1_entry_offset", "chunk_id", "raw_record_hex",
]

SENSOR_FIELDS = [
    "start_time_sec", "end_time_sec", "time_source",
    "sequence", "idx1_entry_offset", "chunk_id", "vector_length", "x", "y", "z",
]

TIMELINE_FIELDS = [
    "sample", "start_time_sec", "end_time_sec", "time_source",
    "abs_time", "latitude", "longitude", "speed_kmh", "track_deg",
    "latitude_last", "longitude_last", "speed_kmh_last",
    "x_g", "y_g", "z_g", "x_g_cal", "y_g_cal", "z_g_cal",
]


def sniff_stream_is_record72(mm, entries, base_offset, stream_fcc,
                              sample_size=SNIFF_SAMPLE_SIZE,
                              min_fraction=RECORD_MIN_FRACTION):
    """앞쪽 몇 개만 읽어 이 스트림이 72바이트 레코드인지 다수결로 본다."""
    sample = entries[:sample_size]
    if not sample:
        return False, 0, 0
    ok = checked = 0
    for e in sample:
        chunk_offset = base_offset + e["idx_offset"]
        reasons, payload_offset, _ = carve.validate_chunk(mm, chunk_offset, e)
        if reasons != ["OK"]:
            continue
        checked += 1
        payload = bytes(mm[payload_offset:payload_offset + e["length"]])
        if parse_finevu_record(payload, stream_fcc) is not None:
            ok += 1
    if checked == 0:
        return False, 0, 0
    return (ok / checked) >= min_fraction, ok, checked


def _fmt(v, spec="{:.6f}"):
    return "" if v is None else spec.format(v)


def process_stream(mm, out_dir, stream, entries, base_offset, label,
                    stream_times, start_dt, dry_run=False):
    display_label, dir_label, prefix = label
    stream_dir = os.path.join(out_dir, dir_label)
    if not dry_run:
        os.makedirs(os.path.join(stream_dir, "chunks"), exist_ok=True)

    parsed_rows = []      # (seq, entry, record dict 또는 None)
    index_rows = []
    validation_counts = {}
    total_bytes = 0

    concat_f = None
    if not dry_run:
        concat_f = open(os.path.join(stream_dir, f"{prefix}_concat.bin"), "wb")

    for seq, e in enumerate(entries):
        chunk_offset = base_offset + e["idx_offset"]
        reasons, payload_offset, header_size = carve.validate_chunk(mm, chunk_offset, e)
        status = "|".join(reasons)
        validation_counts[status] = validation_counts.get(status, 0) + 1

        output_file = ""
        record = None
        if "OUT_OF_RANGE" not in reasons:
            payload = bytes(mm[payload_offset:payload_offset + e["length"]])
            total_bytes += len(payload)
            if not dry_run:
                chunk_filename = f"{prefix}_{seq:06d}.bin"
                with open(os.path.join(stream_dir, "chunks", chunk_filename), "wb") as cf:
                    cf.write(payload)
                concat_f.write(payload)
                output_file = os.path.join(dir_label, "chunks", chunk_filename)
            if reasons != ["OK"]:
                # raw는 남기되 자동 디코딩은 건너뛴다. base offset 추정이 이 위치에서
                # 틀렸을 수 있어 잘못된 좌표를 만드는 것보다 비워두는 쪽이 낫다.
                carve.warn(f"[{display_label}] entry #{seq} validation={status} - "
                           f"raw는 보존하지만 레코드 해석은 생략 "
                           f"(chunk_offset=0x{chunk_offset:X})")
            else:
                record = parse_finevu_record(payload, stream.fcc_type)
                if record is None:
                    carve.warn(f"[{display_label}] entry #{seq} 가 72바이트 레코드 형식에 "
                               f"맞지 않음 - raw만 보존")
        else:
            carve.warn(f"[{display_label}] entry #{seq} validation={status} - "
                       f"payload 추출/해석 생략 (chunk_offset=0x{chunk_offset:X})")

        parsed_rows.append((seq, e, record))
        index_rows.append({
            "sequence": seq,
            "stream_index": stream.index,
            "stream_label": display_label,
            "fcc_type": (stream.fcc_type or b"").decode("ascii", errors="replace"),
            "chunk_id": e["chunk_id"].decode("ascii", errors="replace"),
            "idx1_entry_offset": f"0x{e['idx_offset']:08X}",
            "absolute_chunk_offset": f"0x{chunk_offset:08X}",
            "payload_offset": f"0x{payload_offset:08X}" if payload_offset is not None else "",
            "idx1_length": e["length"],
            "chunk_header_length": header_size if header_size is not None else "",
            "flags": f"0x{e['flags']:08X}",
            "validation": status,
            "decoded": record is not None,
            "output_file": output_file,
        })

    if concat_f is not None:
        concat_f.close()

    # 경과 초는 1바이트 자유 진행 카운터라 파일 전체를 모아 한 번에 편다.
    deltas = finevu_unwrap_elapsed([r["elapsed_sec"] if r else None
                                     for _, _, r in parsed_rows])

    coord_rows, sensor_rows = [], []
    for (seq, e, record), delta in zip(parsed_rows, deltas):
        if record is None:
            continue
        times = stream_times or []
        start_sec, end_sec = times[seq] if seq < len(times) else (None, None)
        start_disp = f"{start_sec:.3f}" if start_sec is not None else ""
        end_disp = f"{end_sec:.3f}" if end_sec is not None else ""
        abs_time = ""
        if start_dt is not None and delta is not None:
            abs_time = (start_dt + datetime.timedelta(seconds=delta)).strftime(
                "%Y-%m-%d %H:%M:%S")
        chunk_id_txt = e["chunk_id"].decode("ascii", errors="replace")
        coord_rows.append({
            "start_time_sec": start_disp,
            "end_time_sec": end_disp,
            "time_source": "avi_video_duration" if start_sec is not None else "",
            "elapsed_sec": record["elapsed_sec"],
            "elapsed_delta_sec": "" if delta is None else delta,
            "abs_time": abs_time,
            "status": record["status"],
            "latitude": _fmt(record["lat"]),
            "longitude": _fmt(record["lon"]),
            "speed_kmh": _fmt(record["speed_kmh"], "{:.3f}"),
            "track_deg": record["track_deg"],
            "x_g": _fmt(record["x_g"]), "y_g": _fmt(record["y_g"]), "z_g": _fmt(record["z_g"]),
            "hemi_flag": record["hemi_flag"],
            "lat_raw": f"{record['lat_raw']:.4f}",
            "lon_raw": f"{record['lon_raw']:.4f}",
            "coord_format": record["coord_format"],
            "status_valid": record["status_valid"],
            "trusted": record["trusted"],
            "parse_warnings": record["parse_warnings"],
            "sequence": seq,
            "idx1_entry_offset": f"0x{e['idx_offset']:08X}",
            "chunk_id": chunk_id_txt,
            "raw_record_hex": record["raw"],
        })
        sensor_rows.append({
            "start_time_sec": start_disp,
            "end_time_sec": end_disp,
            "time_source": "avi_video_duration" if start_sec is not None else "",
            "sequence": seq,
            "idx1_entry_offset": f"0x{e['idx_offset']:08X}",
            "chunk_id": chunk_id_txt,
            "vector_length": 3,
            "x": _fmt(record["x_g"]), "y": _fmt(record["y_g"]), "z": _fmt(record["z_g"]),
        })

    if not dry_run:
        with open(os.path.join(stream_dir, "coordinates.csv"), "w", newline="",
                   encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=COORD_FIELDS)
            w.writeheader()
            w.writerows(coord_rows)

        with open(os.path.join(stream_dir, "coordinates.txt"), "w", encoding="utf-8") as f:
            # 좌표 목록이라 측위 실패(status=V) 행은 뺀다. 그 행도 CSV에는 남아 있다.
            for i, row in enumerate([r for r in coord_rows if r["latitude"]], start=1):
                f.write(f"{i}. {row['latitude']}, {row['longitude']}\n")

        with open(os.path.join(stream_dir, "sensor_values.csv"), "w", newline="",
                   encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=SENSOR_FIELDS)
            w.writeheader()
            w.writerows(sensor_rows)

    return {
        "index_rows": index_rows,
        "coord_rows": coord_rows,
        "validation_counts": validation_counts,
        "total_entries": len(entries),
        "decoded_count": len(coord_rows),
        "fix_count": sum(1 for r in coord_rows if r["latitude"]),
        "total_bytes": total_bytes,
    }


def write_timeline(out_dir, coord_rows, dry_run=False):
    """GPS와 충격센서가 한 레코드에 같이 들어 있어서 그대로 한 줄로 펴면 된다.
    컬럼 구성은 다른 경로의 timeline.csv와 맞춰서 시각화 쪽이 경로마다 다른 파일을
    읽지 않아도 되게 한다. SENS float 벡터처럼 이미 g 단위라 카운트->g 보정은 없다."""
    if dry_run or not coord_rows:
        return None
    rows = []
    last_lat = last_lon = last_speed = ""
    for i, r in enumerate(coord_rows, start=1):
        if r["latitude"]:
            last_lat, last_lon, last_speed = r["latitude"], r["longitude"], r["speed_kmh"]
        rows.append({
            "sample": i,
            "start_time_sec": r["start_time_sec"], "end_time_sec": r["end_time_sec"],
            "time_source": r["time_source"], "abs_time": r["abs_time"],
            "latitude": r["latitude"], "longitude": r["longitude"],
            "speed_kmh": r["speed_kmh"], "track_deg": r["track_deg"],
            "latitude_last": last_lat, "longitude_last": last_lon,
            "speed_kmh_last": last_speed,
            "x_g": r["x_g"], "y_g": r["y_g"], "z_g": r["z_g"],
            "x_g_cal": "", "y_g_cal": "", "z_g_cal": "",
        })
    with open(os.path.join(out_dir, "timeline.csv"), "w", newline="",
               encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=TIMELINE_FIELDS)
        w.writeheader()
        w.writerows(rows)
    return len(rows)


def parse_args(argv):
    p = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("input", help="입력 AVI 파일 경로")
    p.add_argument("output", help="출력 디렉터리")
    p.add_argument("--dry-run", action="store_true",
                    help="파일 미생성, 판정 + 해석 + 요약만 출력")
    p.add_argument("--sample-size", type=int, default=SNIFF_SAMPLE_SIZE,
                    help=f"스트림 형식 판정에 쓸 앞쪽 레코드 개수 (기본 {SNIFF_SAMPLE_SIZE})")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    carve.WARNINGS.clear()
    carve.assert_riff_file(args.input)

    if os.path.getsize(args.input) == 0:
        print(f"{args.input}: 파일 크기가 0입니다 - 종료.", file=sys.stderr)
        return

    with open(args.input, "rb") as f:
        mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)

        hdrl, movi_list, idx1_chunk, avix_count = carve.find_top_level_sections(mm)

        movi_fourcc_pos = movi_list[0].data_start if movi_list else carve.find_movi_fallback(mm)
        if movi_fourcc_pos is None:
            carve.warn("movi 를 전혀 찾지 못함")

        if idx1_chunk is not None:
            idx1_start, idx1_size = idx1_chunk.data_start, idx1_chunk.ck_size
        else:
            fb = carve.find_idx1_fallback(mm)
            if fb is None:
                print(f"{args.input}: idx1을 찾지 못했습니다. 처리할 수 없습니다.",
                      file=sys.stderr)
                mm.close()
                return
            idx1_start, idx1_size = fb
            idx1_start += 8

        idx1_entries = carve.parse_idx1(mm, idx1_start, idx1_size)

        dw_streams, streams = (None, [])
        if hdrl is not None:
            dw_streams, streams = carve.parse_hdrl(mm, hdrl)
        else:
            carve.warn("hdrl 을 찾지 못함 - 스트림 타입 불명, chunk id 기반 fallback만 사용")

        stream_table = carve.build_stream_table(dw_streams, streams, idx1_entries)
        carve.print_stream_table(stream_table)

        base_offset, base_label, base_scores, base_uncertain = carve.detect_base_offset(
            mm, movi_fourcc_pos, idx1_entries)
        carve.info(f"\n[Base offset 선택] {base_label} -> base=0x{base_offset:X}"
                   f"{' (불확실)' if base_uncertain else ''}")

        entries_by_stream = {}
        for e in idx1_entries:
            sidx = carve.stream_index_from_chunk_id(e["chunk_id"])
            if sidx is not None:
                entries_by_stream.setdefault(sidx, []).append(e)

        candidates = [s for s in stream_table if s.fcc_type not in carve.STANDARD_AV_FCCTYPES]

        detection_rows = []
        record_streams = []
        for s in candidates:
            entries = entries_by_stream.get(s.index, [])
            is_rec, ok, checked = sniff_stream_is_record72(
                mm, entries, base_offset, s.fcc_type, sample_size=args.sample_size)
            detection_rows.append({
                "stream_index": s.index,
                "fcc_type": (s.fcc_type or b"").decode("ascii", errors="replace"),
                "fcc_handler": (s.fcc_handler or b"").decode("ascii", errors="replace"),
                "chunk_ids": ";".join(sorted(cid.decode("ascii", errors="replace")
                                              for cid in s.observed_chunk_ids)),
                "total_entries": len(entries),
                "sample_checked": checked,
                "sample_record_ok": ok,
                "decision": "RECORD72" if is_rec else "NOT_RECORD72 (건너뜀)",
            })
            if is_rec:
                record_streams.append(s)
            else:
                carve.warn(f"stream #{s.index} 은 72바이트 레코드 형식이 아님 "
                           f"({ok}/{checked} 샘플만 통과) - NMEA 텍스트라면 "
                           f"GPS_metadata_avi.py 를 쓸 것")

        carve.info("\n[72바이트 레코드 스트림 판정]")
        for row in detection_rows:
            carve.info(f"  stream #{row['stream_index']} "
                       f"({row['fcc_type']}/{row['fcc_handler']}, {row['chunk_ids']}): "
                       f"{row['decision']} ({row['sample_record_ok']}/{row['sample_checked']} 샘플)")

        if not record_streams:
            carve.warn("72바이트 레코드로 판정된 스트림이 없습니다. 이 파일은 이 스크립트의 "
                       "대상이 아닙니다.")

        video_duration, duration_source = carve.compute_video_duration(mm, hdrl, stream_table)
        if video_duration:
            carve.info(f"[시간축] 영상 길이 {video_duration:.3f}초 ({duration_source})")
        else:
            carve.warn(f"[시간축] {duration_source} - start_time_sec 계열은 공란으로 둠")

        start_dt = finevu_filename_start_time(args.input)
        if start_dt is None:
            carve.warn("파일명에서 녹화 시작 시각을 찾지 못함 - abs_time 은 공란으로 둠")
        else:
            carve.info(f"[시간축] 파일명 기준 녹화 시작 시각 {start_dt:%Y-%m-%d %H:%M:%S}")

        labels = carve.make_unique_labels(record_streams)
        if not args.dry_run:
            os.makedirs(args.output, exist_ok=True)

        all_index_rows = []
        summary_rows = []
        best_coord_rows = []
        for s in record_streams:
            entries = entries_by_stream.get(s.index, [])
            stream_times = carve.build_avi_stream_times(video_duration, len(entries))
            result = process_stream(mm, args.output, s, entries, base_offset,
                                     labels[s.index], stream_times, start_dt,
                                     dry_run=args.dry_run)
            all_index_rows.extend(result["index_rows"])
            summary_rows.append((labels[s.index][0], s.index, result))
            if len(result["coord_rows"]) > len(best_coord_rows):
                best_coord_rows = result["coord_rows"]

        n_timeline = write_timeline(args.output, best_coord_rows, dry_run=args.dry_run)
        if n_timeline:
            carve.info(f"[시간축] timeline.csv {n_timeline}행 생성")

        if not args.dry_run:
            with open(os.path.join(args.output, "index.csv"), "w", newline="",
                       encoding="utf-8") as f:
                fieldnames = ["sequence", "stream_index", "stream_label", "fcc_type",
                              "chunk_id", "idx1_entry_offset", "absolute_chunk_offset",
                              "payload_offset", "idx1_length", "chunk_header_length",
                              "flags", "validation", "decoded", "output_file"]
                w = csv.DictWriter(f, fieldnames=fieldnames)
                w.writeheader()
                w.writerows(all_index_rows)

            with open(os.path.join(args.output, "stream_table.csv"), "w", newline="",
                       encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["stream_index", "fcc_type", "fcc_handler", "strn_name",
                            "observed_chunk_ids", "role"])
                for s in stream_table:
                    w.writerow([
                        s.index, (s.fcc_type or b"").decode("ascii", errors="replace"),
                        (s.fcc_handler or b"").decode("ascii", errors="replace"),
                        s.name or "",
                        ";".join(sorted(cid.decode("ascii", errors="replace")
                                         for cid in s.observed_chunk_ids)),
                        s.role,
                    ])

            with open(os.path.join(args.output, "record_detection.csv"), "w", newline="",
                       encoding="utf-8") as f:
                if detection_rows:
                    w = csv.DictWriter(f, fieldnames=list(detection_rows[0].keys()))
                    w.writeheader()
                    w.writerows(detection_rows)

            with open(os.path.join(args.output, "warnings.log"), "w", encoding="utf-8") as f:
                for msg in carve.WARNINGS:
                    f.write(msg + "\n")

        carve.info("\n" + "=" * 60)
        carve.info(f"[요약] {args.input}")
        carve.info(f"발견된 스트림 수        : hdrl dwStreams={dw_streams} vs "
                   f"실제 관측={len(stream_table)}")
        carve.info(f"72바이트 레코드 스트림  : {len(record_streams)}개")
        for label_disp, sidx, result in summary_rows:
            carve.info(f"  - {label_disp}(idx={sidx}): 총 {result['total_entries']}개 entry, "
                       f"{result['total_bytes']} bytes, "
                       f"레코드 해석 {result['decoded_count']}개, "
                       f"측위 성공 {result['fix_count']}개")
            carve.info(f"      검증 결과(사유별): {result['validation_counts']}")
        carve.info(f"경고 총 개수            : {len(carve.WARNINGS)} "
                   f"({'dry-run이라 파일 미생성' if args.dry_run else os.path.join(args.output, 'warnings.log') + ' 참조'})")
        carve.info("=" * 60)

        mm.close()


if __name__ == "__main__":
    main()
