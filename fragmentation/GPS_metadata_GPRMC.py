import argparse
import csv
import mmap
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import GPS_metadata_avi as carve


def looks_like_text_record(*args, **kwargs):
    return carve.looks_like_text_record(*args, **kwargs)

def decode_text_record(*args, **kwargs):
    return carve.decode_text_record(*args, **kwargs)

def nmea_checksum_ok(*args, **kwargs):
    return carve.nmea_checksum_ok(*args, **kwargs)

def _dm_to_decimal(*args, **kwargs):
    return carve._dm_to_decimal(*args, **kwargs)

def format_nmea_date(*args, **kwargs):
    return carve.format_nmea_date(*args, **kwargs)

def format_nmea_time(*args, **kwargs):
    return carve.format_nmea_time(*args, **kwargs)

def parse_rmc(*args, **kwargs):
    return carve.parse_rmc(*args, **kwargs)

def parse_gga(*args, **kwargs):
    return carve.parse_gga(*args, **kwargs)

NMEA_PARSERS = {"RMC": parse_rmc, "GGA": parse_gga}


def try_parse_nmea(*args, **kwargs):
    return carve.try_parse_nmea(*args, **kwargs)

def sniff_stream_is_text(mm, entries, base_offset, sample_size=8, min_fraction=0.8):
    sample = entries[:sample_size]
    if not sample:
        return False, 0, 0
    ok = 0
    checked = 0
    for e in sample:
        chunk_offset = base_offset + e["idx_offset"]
        reasons, payload_offset, _ = carve.validate_chunk(mm, chunk_offset, e)
        if reasons != ["OK"]:
            continue
        checked += 1
        payload = bytes(mm[payload_offset:payload_offset + e["length"]])
        if looks_like_text_record(payload):
            ok += 1
    if checked == 0:
        return False, 0, 0
    is_text = (ok / checked) >= min_fraction
    if is_text and ok < checked:
        carve.warn(f"텍스트 판정 샘플 중 {checked - ok}/{checked}개가 패턴에 안 맞았지만 완화된 기준"
                   f"({min_fraction:.0%})으로 통과함 - RIFF 구조/base offset이 정확하게 검출되지 "
                   f"않았을 가능성도 있으니 결과(coordinates.csv/unparsed_lines.txt)를 확인해볼 것")
    return is_text, ok, checked


def process_stream(mm, out_dir, stream, entries, base_offset, label):
    display_label, dir_label, prefix = label
    stream_dir = os.path.join(out_dir, dir_label)
    raw_chunks_dir = os.path.join(stream_dir, "raw_chunks")
    os.makedirs(raw_chunks_dir, exist_ok=True)

    coord_rows = []
    unparsed_lines = []

    raw_concat_path = os.path.join(stream_dir, "raw_concat.bin")
    with open(raw_concat_path, "wb") as raw_concat_f:
        for seq, e in enumerate(entries):
            chunk_offset = base_offset + e["idx_offset"]
            reasons, payload_offset, header_size = carve.validate_chunk(mm, chunk_offset, e)
            if reasons != ["OK"]:
                validation_text = "|".join(reasons)
                carve.warn(f"[{display_label}] entry #{seq} validation={validation_text} - 건너뜀 "
                           f"(chunk_offset=0x{chunk_offset:X})")
                continue

            payload = bytes(mm[payload_offset:payload_offset + e["length"]])

            raw_chunk_path = os.path.join(raw_chunks_dir, f"{prefix}_{seq:06d}.bin")
            with open(raw_chunk_path, "wb") as cf:
                cf.write(payload)
            raw_concat_f.write(payload)

            if not looks_like_text_record(payload):
                carve.warn(f"[{display_label}] entry #{seq} 텍스트 패턴이 아님(오염/예외 레코드) "
                            f"- raw만 보존, 파싱 생략")
                continue

            line = decode_text_record(payload)
            parsed = try_parse_nmea(line)
            if parsed is not None:
                speed_kmh = parsed.get("speed_kmh")
                coord_rows.append({
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
            elif line:
                unparsed_lines.append((seq, line))

    coordinates_txt = os.path.join(stream_dir, "coordinates.txt")
    with open(coordinates_txt, "w", encoding="utf-8") as f:
        # coordinates.txt는 "좌표 목록"이라 fix가 없어 좌표가 빈 행은 제외한다
        # (그 행도 coordinates.csv에는 status=V로 그대로 남는다).
        for i, row in enumerate([r for r in coord_rows if r["latitude"]], start=1):
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

    return {
        "total_entries": len(entries),
        "coord_count": len(coord_rows),
        "unparsed_count": len(unparsed_lines),
    }


def parse_args(argv):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("input", help="입력 AVI 파일 경로")
    p.add_argument("output", help="출력 디렉터리")
    p.add_argument("--sample-size", type=int, default=8,
                    help="스트림별 텍스트 판정에 쓸 샘플 entry 개수 (기본 8)")
    return p.parse_args(argv)


def main(argv=None):
    carve.WARNINGS.clear()
    args = parse_args(sys.argv[1:] if argv is None else argv)

    carve.assert_riff_file(args.input)

    filesize = os.path.getsize(args.input)
    if filesize == 0:
        print("입력 파일 크기가 0입니다.", file=sys.stderr)
        sys.exit(1)

    with open(args.input, "rb") as f:
        mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)

        hdrl, movi_list, idx1_chunk, avix_count = carve.find_top_level_sections(mm)
        movi_fourcc_pos = movi_list[0].data_start if movi_list else carve.find_movi_fallback(mm)

        if idx1_chunk is not None:
            idx1_start, idx1_size = idx1_chunk.data_start, idx1_chunk.ck_size
        else:
            fb = carve.find_idx1_fallback(mm)
            if fb is None:
                print("idx1을 찾지 못했습니다.", file=sys.stderr)
                mm.close()
                sys.exit(1)
            idx1_start, idx1_size = fb
            idx1_start += 8

        idx1_entries = carve.parse_idx1(mm, idx1_start, idx1_size)

        dw_streams, streams = (None, [])
        if hdrl is not None:
            dw_streams, streams = carve.parse_hdrl(mm, hdrl)
        else:
            carve.warn("hdrl 을 찾지 못함 - 스트림 이름/핸들러 정보 없이 진행")

        stream_table = carve.build_stream_table(dw_streams, streams, idx1_entries)
        carve.print_stream_table(stream_table)

        base_offset, base_label, base_scores, base_uncertain = carve.detect_base_offset(
            mm, movi_fourcc_pos, idx1_entries)
        carve.info(f"\n[Base offset 선택] {base_label} -> base=0x{base_offset:X}"
                   f"{' (불확실)' if base_uncertain else ''}")

        entries_by_stream = {}
        for e in idx1_entries:
            sidx = carve.stream_index_from_chunk_id(e["chunk_id"])
            if sidx is None:
                continue
            entries_by_stream.setdefault(sidx, []).append(e)

        candidates = [s for s in stream_table if s.fcc_type not in carve.STANDARD_AV_FCCTYPES]

        detection_rows = []
        text_streams = []
        for s in candidates:
            entries = entries_by_stream.get(s.index, [])
            is_text, ok, checked = sniff_stream_is_text(
                mm, entries, base_offset, sample_size=args.sample_size)
            detection_rows.append({
                "stream_index": s.index,
                "fcc_type": (s.fcc_type or b"").decode("ascii", errors="replace"),
                "fcc_handler": (s.fcc_handler or b"").decode("ascii", errors="replace"),
                "chunk_ids": ";".join(sorted(cid.decode("ascii", errors="replace")
                                              for cid in s.observed_chunk_ids)),
                "total_entries": len(entries),
                "sample_checked": checked,
                "sample_text_ok": ok,
                "decision": "TEXT" if is_text else "BINARY (건너뜀)",
            })
            if is_text:
                text_streams.append(s)
            else:
                carve.warn(f"stream #{s.index} ({s.observed_chunk_ids}) 은 텍스트 패턴이 아님 "
                           f"({ok}/{checked} 샘플만 통과) - GPS_metadata_avi.py 로 raw carving 권장")

        carve.info("\n[텍스트 스트림 판정]")
        for row in detection_rows:
            carve.info(f"  stream #{row['stream_index']} "
                       f"({row['fcc_type']}/{row['fcc_handler']}, {row['chunk_ids']}): "
                       f"{row['decision']} ({row['sample_text_ok']}/{row['sample_checked']} 샘플)")

        if not text_streams:
            carve.warn("텍스트로 판정된 스트림이 없습니다. 이 파일은 GPS_metadata_avi.py 로 "
                       "raw carving 하세요.")

        labels = carve.make_unique_labels(text_streams)

        os.makedirs(args.output, exist_ok=True)
        summary_rows = []
        for s in text_streams:
            entries = entries_by_stream.get(s.index, [])
            result = process_stream(mm, args.output, s, entries, base_offset, labels[s.index])
            summary_rows.append((labels[s.index][0], s.index, result))

        stream_csv = os.path.join(args.output, "stream_table.csv")
        with open(stream_csv, "w", newline="", encoding="utf-8") as fcsv:
            w = csv.writer(fcsv)
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

        detect_csv = os.path.join(args.output, "text_detection.csv")
        with open(detect_csv, "w", newline="", encoding="utf-8") as fcsv:
            w = csv.DictWriter(fcsv, fieldnames=list(detection_rows[0].keys()) if detection_rows else [])
            if detection_rows:
                w.writeheader()
                w.writerows(detection_rows)

        with open(os.path.join(args.output, "warnings.log"), "w", encoding="utf-8") as fw:
            for w_msg in carve.WARNINGS:
                fw.write(w_msg + "\n")

        carve.info("\n" + "=" * 60)
        carve.info("[요약]")
        carve.info(f"발견된 스트림 수 : hdrl dwStreams={dw_streams} vs 실제 관측={len(stream_table)}")
        carve.info(f"텍스트로 판정된 스트림 : {len(text_streams)}개")
        for label_disp, sidx, result in summary_rows:
            carve.info(f"  - {label_disp}(idx={sidx}): 총 {result['total_entries']}개 entry, "
                       f"좌표 파싱 {result['coord_count']}개, 미분류 텍스트 {result['unparsed_count']}개")
        carve.info(f"경고 총 개수 : {len(carve.WARNINGS)} "
                   f"({os.path.join(args.output, 'warnings.log')} 참조)")
        carve.info("=" * 60)

        mm.close()


if __name__ == "__main__":
    main()
