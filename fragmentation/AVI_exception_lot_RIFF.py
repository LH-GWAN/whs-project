"""
블랙박스 AVI 파일에서 "슬랙"(예전 녹화 파일의 잔재)을 제거해 깨끗한 사본을 만든다.

실제 샘플(VUGERA MB-900SB, REC_20240916_172436_F.avi)을 구조 분석해보니, 이
계열 카메라는 파일을 고정 크기로 미리 만들어두고 앞부분만 새 녹화로 덮어쓰는
방식이라, 슬랙은 파일 끝 뒤가 아니라 최상위 RIFF가 선언한 movi 영역 *내부에*
예전 녹화 파일의 RIFF/hdrl/JUNK(구 파일명 포함)/movi가 통째로 남아있는 형태로
나타난다.

파일 크기는 카메라/모델마다 다를 수 있으므로 어떤 값도 하드코딩하지 않는다.
대신 "현재 파일 자신의 최상위 RIFF가 선언한 크기"(reference_size)를 그때그때
읽어서 기준으로 삼아, movi 내부에서 찾은 임베디드 RIFF가

  (a) reference_size와 정확히 같은 크기를 선언하고 있거나
      (=같은 카메라/포맷이 쓰는 고정 컨테이너 크기 관례를 그대로 물려받은
      예전 파일이라는 강한 정황 증거), 또는
  (b) 선언된 크기가 실제 남은 공간보다 커서 원래 있어야 할 만큼 다 들어있지
      않을 때 (=일부만 덮어써지고 잘려나간 옛날 파일의 잔재라는 확실한 증거)

둘 중 하나면 "예전 파일 잔재"로 판단한다.

idx1은 현재 녹화분 chunk만 정확히 가리키므로, idx1 엔트리를 뒤에서부터 실제
chunk 헤더와 대조 검증해서 진짜 마지막으로 유효한 지점을 찾고 그 뒤(예전 파일
잔재 포함)를 전부 잘라낸다. 문자열(mm.find) 검색은 movi의 구조적으로 확정된
범위 안으로만 제한해서 쓰고, idx1은 전체 파일을 뒤지지 않고 RIFF 안의 자식
chunk로 구조적으로 직접 찾는다 - 예전 스크립트처럼 전체 파일에서 "idx1" 문자열을
찾는 방식은 JUNK나 압축 데이터 안에서 우연히 매치되는 위험이 있어 쓰지 않는다.
"""

import argparse
import glob
import mmap
import os
import re
import struct

VIDEO_CHUNK_RE = re.compile(rb"^[0-9]{2}(?:dc|db)$")


def _u32le(mm, off):
    if off < 0 or off + 4 > len(mm):
        return None
    return struct.unpack_from("<I", mm, off)[0]


def _iter_chunks(mm, start, end):
    """start~end 범위 안에서만, 선언된 크기를 따라가며 chunk를 순회한다."""
    pos = start
    while pos + 8 <= end:
        cid = bytes(mm[pos:pos + 4])
        size = _u32le(mm, pos + 4)
        if size is None:
            return
        data_start = pos + 8
        data_end = data_start + size
        if data_end > end:
            return
        yield cid, pos, data_start, data_end, size
        pos = data_end + (size & 1)


def _find_first_riff(mm):
    filesize = len(mm)
    if filesize < 12 or bytes(mm[0:4]) != b"RIFF":
        return None
    size = _u32le(mm, 4)
    if size is None or bytes(mm[8:12]) != b"AVI ":
        return None
    return {"declared_size": size, "content_start": 12, "content_end": min(8 + size, filesize)}


def _count_top_level_riffs(mm):
    """최상위에서 연속으로 이어붙어 있는 유효한 RIFF 청크 개수를 선언된 크기만
    따라가며 센다(문자열 검색 아님). 반환값: (개수, 마지막 RIFF가 끝나는 offset).

    슬랙은 두 가지 형태로 나타난다 - (a) movi가 선언한 영역 *안에* 예전 녹화
    파일이 통째로 남아있는 형태(find_embedded_riffs가 담당), (b) 최상위 RIFF가
    2개 이상 그냥 이어붙어 있는 형태. 원래 이 스크립트는 (a)만 보고 있어서
    (b)를 놓쳤다 - integration_avi.py와 같은 기준으로 맞춘다.
    """
    filesize = len(mm)
    pos = 0
    count = 0
    while pos + 8 <= filesize:
        if bytes(mm[pos:pos + 4]) != b"RIFF":
            break
        size = _u32le(mm, pos + 4)
        if size is None:
            break
        end = pos + 8 + size
        if end > filesize:
            break
        count += 1
        pos = end + (size & 1)
    return count, pos


def _find_hdrl_movi_idx1(mm, riff):
    """RIFF의 직계 자식만 구조적으로 순회해 hdrl/movi/idx1을 찾는다."""
    hdrl = movi = idx1 = None
    for cid, pos, data_start, data_end, size in _iter_chunks(mm, riff["content_start"], riff["content_end"]):
        if cid == b"LIST" and data_start + 4 <= len(mm):
            list_type = bytes(mm[data_start:data_start + 4])
            if list_type == b"hdrl" and hdrl is None:
                hdrl = {"content_start": data_start + 4, "content_end": data_end}
            elif list_type == b"movi" and movi is None:
                movi = {"pos": pos, "fourcc_pos": data_start,
                        "content_start": data_start + 4, "content_end": data_end}
        elif cid == b"idx1" and idx1 is None:
            idx1 = {"pos": pos, "data_start": data_start, "size": size}
    return hdrl, movi, idx1


def _find_avih_total_frames_pos(mm, hdrl):
    for cid, pos, data_start, data_end, size in _iter_chunks(mm, hdrl["content_start"], hdrl["content_end"]):
        if cid == b"avih" and size >= 56:
            return data_start + 16
    return None


def _parse_idx1_entries(mm, idx1):
    n = idx1["size"] // 16
    entries = []
    for i in range(n):
        off = idx1["data_start"] + i * 16
        if off + 16 > len(mm):
            break
        entries.append({
            "chunk_id": bytes(mm[off:off + 4]),
            "idx_offset": int.from_bytes(mm[off + 8:off + 12], "little"),
            "length": int.from_bytes(mm[off + 12:off + 16], "little"),
        })
    return entries


def _detect_base_offset(mm, movi_fourcc_pos, idx1_entries, sample_n=8):
    filesize = len(mm)
    candidates = {"movi FourCC 위치": movi_fourcc_pos,
                  "movi 데이터 시작(+4)": movi_fourcc_pos + 4,
                  "절대 offset(base=0)": 0}
    sample = idx1_entries[:sample_n]
    best_label, best_base, best_score = "movi FourCC 위치", movi_fourcc_pos, -1
    for label, base in candidates.items():
        score = 0
        for e in sample:
            off = base + e["idx_offset"]
            if 0 <= off and off + 4 <= filesize and bytes(mm[off:off + 4]) == e["chunk_id"]:
                score += 1
        if score > best_score:
            best_score, best_base, best_label = score, base, label
    return best_base, best_label, best_score, len(sample)


def _validate_chunk(mm, chunk_offset, entry):
    filesize = len(mm)
    if chunk_offset < 0 or chunk_offset + 8 > filesize:
        return False, None
    if bytes(mm[chunk_offset:chunk_offset + 4]) != entry["chunk_id"]:
        return False, None
    if _u32le(mm, chunk_offset + 4) != entry["length"]:
        return False, None
    payload_offset = chunk_offset + 8
    if payload_offset + entry["length"] > filesize:
        return False, None
    return True, payload_offset


def find_embedded_riffs(mm, search_start, search_end, reference_size):
    """movi의 구조적으로 확정된 content 범위 안에서만 b"RIFF" + AVI/AVIX 폼타입
    조합을 찾는다(전체 파일 스캔이 아니라 이 범위로 엄격히 제한됨). 위 모듈
    docstring에서 설명한 (a)/(b) 조건 중 하나를 만족해야 "예전 파일 잔재"로
    인정한다."""
    results = []
    pos = search_start
    while True:
        idx = mm.find(b"RIFF", pos, search_end)
        if idx < 0:
            break
        if idx + 12 <= search_end and bytes(mm[idx + 8:idx + 12]) in (b"AVI ", b"AVIX"):
            declared_size = _u32le(mm, idx + 4)
            remaining = search_end - (idx + 8)
            same_as_reference = declared_size == reference_size
            overruns_remaining = declared_size is not None and declared_size > remaining
            if same_as_reference or overruns_remaining:
                results.append({
                    "pos": idx,
                    "declared_size": declared_size,
                    "same_as_reference": same_as_reference,
                    "overruns_remaining": overruns_remaining,
                })
        pos = idx + 4
    return results


def fix_blackbox_video(file_path, output_path):
    print(f"[*] 처리 시작: {file_path}")
    if os.path.getsize(file_path) == 0:
        print(f"[-] {file_path}: 빈 파일입니다.")
        return False

    with open(file_path, "rb") as src:
        mm = mmap.mmap(src.fileno(), 0, access=mmap.ACCESS_READ)
        try:
            riff = _find_first_riff(mm)
            if riff is None:
                print(f"[-] {file_path}: RIFF/AVI(RIFF....AVI ) 헤더가 아닙니다.")
                return False

            hdrl, movi, idx1 = _find_hdrl_movi_idx1(mm, riff)
            if movi is None or idx1 is None:
                print(f"[-] {file_path}: movi 또는 idx1을 구조적으로 찾을 수 없습니다.")
                return False

            embedded = find_embedded_riffs(mm, movi["content_start"], movi["content_end"],
                                            riff["declared_size"])
            top_riff_count, consumed_end = _count_top_level_riffs(mm)
            if top_riff_count >= 2:
                print(f"    [+] 최상위 RIFF가 {top_riff_count}개 이어붙어 있음 "
                      f"(첫 RIFF 끝=0x{riff['content_end']:X}, 전체 소비 끝=0x{consumed_end:X}) "
                      f"- 중복 RIFF 슬랙으로 판단")
            if not embedded and top_riff_count < 2:
                print(f"[-] {file_path}: movi 내부 예전 파일 잔재도 없고 최상위 RIFF도 1개뿐 "
                      f"- 이미 깨끗하거나 이 스크립트가 다루는 손상 패턴이 아닙니다.")
                return False

            for e in embedded:
                reasons = []
                if e["same_as_reference"]:
                    reasons.append("현재 파일과 동일한 선언 크기")
                if e["overruns_remaining"]:
                    reasons.append("선언 크기가 남은 공간보다 큼(잘려나간 잔재)")
                print(f"    [+] 예전 파일 잔재로 보이는 RIFF 발견: 0x{e['pos']:X} "
                      f"({', '.join(reasons)})")

            idx1_entries = _parse_idx1_entries(mm, idx1)
            if not idx1_entries:
                print(f"[-] {file_path}: idx1에 엔트리가 없습니다.")
                return False

            base_offset, base_label, matched, checked = _detect_base_offset(
                mm, movi["fourcc_pos"], idx1_entries)
            print(f"    [+] base offset 선택: {base_label} -> 0x{base_offset:X} "
                  f"({matched}/{checked} 샘플 일치)")
            if checked == 0 or matched == 0:
                print(f"[-] {file_path}: base offset을 신뢰할 수 없습니다(샘플 매치 0).")
                return False

            actual_movi_end = None
            kept_entry_count = len(idx1_entries)
            for i in range(len(idx1_entries) - 1, -1, -1):
                e = idx1_entries[i]
                chunk_offset = base_offset + e["idx_offset"]
                ok, payload_offset = _validate_chunk(mm, chunk_offset, e)
                if ok:
                    end = payload_offset + e["length"]
                    end += end & 1
                    actual_movi_end = end
                    kept_entry_count = i + 1
                    break

            if actual_movi_end is None:
                print(f"[-] {file_path}: idx1 안에 검증 통과하는 엔트리가 하나도 없습니다.")
                return False

            print(f"    [+] 매칭 성공! 실제 영상 데이터 끝 지점 추적 완료: {hex(actual_movi_end)}")
            if kept_entry_count < len(idx1_entries):
                print(f"    [!] idx1 꼬리에서 {len(idx1_entries) - kept_entry_count}개 엔트리가 "
                      f"검증 실패해 버려짐 (원래 {len(idx1_entries)}개 -> {kept_entry_count}개 유지)")

            new_movi_size = actual_movi_end - movi["pos"] - 8
            new_idx1_size = kept_entry_count * 16
            idx1_out_pos = actual_movi_end
            output_size = idx1_out_pos + 8 + new_idx1_size
            new_riff_size = output_size - 8
            if new_movi_size < 0 or new_riff_size < 0:
                print(f"[-] {file_path}: 복구 결과 크기 계산이 잘못됐습니다(음수).")
                return False
            if not (0 <= new_movi_size <= 0xFFFFFFFF and 0 <= new_riff_size <= 0xFFFFFFFF):
                print(f"[-] {file_path}: 복구 결과가 AVI 1.0 32bit RIFF 크기 범위를 벗어납니다.")
                return False

            os.makedirs(os.path.dirname(os.path.abspath(output_path)) or ".", exist_ok=True)
            copy_block = 8 * 1024 * 1024
            with open(output_path, "w+b") as out:
                src.seek(0)
                remaining = actual_movi_end
                while remaining:
                    block = src.read(min(copy_block, remaining))
                    if not block:
                        print(f"[-] {file_path}: 영상 데이터 복사 중 예상보다 일찍 EOF 도달")
                        return False
                    out.write(block)
                    remaining -= len(block)

                out.write(bytes(mm[idx1["pos"]:idx1["pos"] + 8]))
                for i in range(kept_entry_count):
                    off = idx1["data_start"] + i * 16
                    out.write(bytes(mm[off:off + 16]))

                out.seek(4)
                out.write(new_riff_size.to_bytes(4, "little"))
                out.seek(movi["pos"] + 4)
                out.write(new_movi_size.to_bytes(4, "little"))
                out.seek(idx1_out_pos + 4)
                out.write(new_idx1_size.to_bytes(4, "little"))

                if hdrl is not None:
                    frames_pos = _find_avih_total_frames_pos(mm, hdrl)
                    if frames_pos is not None and frames_pos + 4 <= actual_movi_end:
                        frame_count = 0
                        for i in range(kept_entry_count):
                            off = idx1["data_start"] + i * 16
                            cid = bytes(mm[off:off + 4])
                            if VIDEO_CHUNK_RE.fullmatch(cid):
                                frame_count += 1
                        out.seek(frames_pos)
                        out.write(frame_count.to_bytes(4, "little"))
                        print(f"    [+] 헤더 프레임 수 복구 완료 ({frame_count} Frames)")

            print(f"    [+] 파일 복구 완료! (결과물 크기: {output_size:,} Bytes, "
                  f"원본 대비 {len(mm) - output_size:,} bytes 절단)\n")
            return True
        finally:
            mm.close()


def process_all_samples(input_folder, output_folder, pattern="*.avi"):
    # 출력 폴더는 처리할 파일이 실제로 있을 때만 만든다. 예전엔 여기서 무조건
    # makedirs를 해서, 대상이 하나도 없어도 빈 Recovered_2 폴더만 남았다.
    # 원래 "REC_*.avi"로 고정돼 있어서 EVT_ 등 다른 이름은 조용히 건너뛰었다.
    # 슬랙 판단은 파일명이 아니라 구조(임베디드 RIFF / 최상위 RIFF 개수)로 하므로
    # 기본값을 모든 .avi로 넓힌다 - 슬랙이 없는 파일은 어차피 스킵된다.
    files = sorted(glob.glob(os.path.join(input_folder, pattern)))
    if not files:
        print(f"'{input_folder}' 폴더 내에 처리할 .avi 파일이 없습니다.")
        return
    os.makedirs(output_folder, exist_ok=True)
    print(f"총 {len(files)}개의 파일을 발견했습니다. 작업을 시작합니다...\n")
    print("-" * 40)
    success_count = 0
    for file_path in files:
        file_name = os.path.basename(file_path)
        output_path = os.path.join(output_folder, f"Recovered_{file_name}")
        if not file_name.startswith("Recovered_") and fix_blackbox_video(file_path, output_path):
            success_count += 1
    print("-" * 40)
    print(f"작업 완료! (성공: {success_count}개)")


def main(argv=None):
    p = argparse.ArgumentParser(
        description="AVI 슬랙(예전 녹화 파일 잔재) 제거 - integration_avi.py에 같은 기능이 "
                    "통합돼 있으므로 보통은 그쪽을 쓰고, 이 스크립트는 슬랙 리페어만 "
                    "단독으로 돌리고 싶을 때 쓴다.")
    p.add_argument("-i", "--input-dir", default=".", help="입력 폴더 (기본: 현재 폴더)")
    p.add_argument("-o", "--output-dir", default="./Recovered_2",
                   help="결과 폴더 (기본: ./Recovered_2)")
    p.add_argument("--pattern", default="*.avi",
                   help="처리할 파일 glob 패턴 (기본: *.avi)")
    args = p.parse_args(argv)
    process_all_samples(args.input_dir, args.output_dir, args.pattern)


if __name__ == "__main__":
    main()
