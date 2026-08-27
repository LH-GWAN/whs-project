import glob
import mmap
import os
import re

VIDEO_CHUNK_RE = re.compile(rb"^[0-9]{2}(?:dc|db)$")


def _u32le(buf, off):
    if off < 0 or off + 4 > len(buf):
        return None
    return int.from_bytes(buf[off:off+4], "little")


def _find_movi(mm):
    # 첫 RIFF(AVI )의 top-level chunk만 순회한다.
    if len(mm) < 12 or mm[0:4] != b"RIFF" or mm[8:12] != b"AVI ":
        return -1
    riff_size = _u32le(mm, 4)
    end = min(len(mm), 8 + riff_size) if riff_size is not None else len(mm)
    pos = 12
    while pos + 8 <= end:
        cid = bytes(mm[pos:pos+4])
        size = _u32le(mm, pos+4)
        if size is None:
            break
        if cid == b"LIST" and pos + 12 <= len(mm) and mm[pos+8:pos+12] == b"movi":
            return pos
        nxt = pos + 8 + size + (size & 1)
        if nxt <= pos or nxt > len(mm):
            break
        pos = nxt
    # 손상된 RIFF size 때문에 구조 순회가 실패한 경우 제한적 fallback.
    marker = mm.find(b"movi", 12)
    while marker >= 8:
        candidate = marker - 8
        if bytes(mm[candidate:candidate+4]) == b"LIST":
            size = _u32le(mm, candidate + 4)
            if size is not None and size >= 4 and candidate + 8 + size <= len(mm):
                return candidate
        marker = mm.find(b"movi", marker + 4)
    return -1


def _iter_valid_idx1_candidates(mm):
    pos = 0
    while True:
        idx = mm.find(b"idx1", pos)
        if idx < 0:
            break
        size = _u32le(mm, idx + 4)
        if size is not None and size >= 16 and size % 16 == 0 and idx + 8 + size <= len(mm):
            yield idx, size
        pos = idx + 4



def _score_idx_base(mm, idx_offset, idx_size, base, sample_n=32):
    """idx1 후보의 여러 엔트리를 실제 chunk header와 대조해 base 신뢰도를 계산한다."""
    n = idx_size // 16
    if n <= 0:
        return 0, 0
    if n <= sample_n:
        indexes = list(range(n))
    else:
        indexes = sorted({round(i * (n - 1) / (sample_n - 1)) for i in range(sample_n)})
    matched = 0
    for i in indexes:
        epos = idx_offset + 8 + i * 16
        cid = bytes(mm[epos:epos+4])
        off = _u32le(mm, epos + 8)
        size = _u32le(mm, epos + 12)
        if off is None or size is None:
            continue
        cpos = base + off
        if not (0 <= cpos <= len(mm) - 8):
            continue
        if bytes(mm[cpos:cpos+4]) != cid:
            continue
        hsize = _u32le(mm, cpos + 4)
        if hsize != size or cpos + 8 + size > len(mm):
            continue
        matched += 1
    return matched, len(indexes)

def _find_avih_total_frames_pos(mm, movi_start):
    # movi 앞의 avih만 대상으로 하고 최소 표준 크기(56 bytes)를 확인한다.
    pos = mm.find(b"avih", 12, movi_start if movi_start > 0 else len(mm))
    while pos >= 0:
        size = _u32le(mm, pos + 4)
        if size is not None and size >= 56 and pos + 8 + size <= len(mm):
            return pos + 8 + 16
        pos = mm.find(b"avih", pos + 4, movi_start if movi_start > 0 else len(mm))
    return None


def fix_blackbox_video(file_path, output_path):
    print(f"[*] 처리 시작: {file_path}")
    with open(file_path, "rb") as src:
        if os.path.getsize(file_path) == 0:
            print(f"[-] {file_path}: 빈 파일입니다.")
            return False
        mm = mmap.mmap(src.fileno(), 0, access=mmap.ACCESS_READ)
        try:
            movi_start = _find_movi(mm)
            if movi_start < 0:
                print(f"[-] {file_path}: movi 청크를 찾을 수 없습니다.")
                return False

            candidates = list(_iter_valid_idx1_candidates(mm))
            if not candidates:
                print(f"[-] {file_path}: 구조적으로 유효한 idx1을 찾을 수 없습니다.")
                return False

            target_idx_offset = -1
            target_idx_size = 0
            actual_movi_end = -1
            for idx_offset, idx_size in reversed(candidates):
                num_entries = idx_size // 16
                first_entry = idx_offset + 8
                last_entry = first_entry + (num_entries - 1) * 16
                if last_entry + 16 > len(mm):
                    continue
                bases = (0, movi_start + 8, movi_start + 12)
                scored = [(b, *_score_idx_base(mm, idx_offset, idx_size, b)) for b in bases]
                base_offset, matched, checked = max(scored, key=lambda x: x[1])
                required = max(1, (checked * 8 + 9) // 10)  # 약 80% 이상 일치 요구
                if checked == 0 or matched < required:
                    continue

                last_id = bytes(mm[last_entry:last_entry+4])
                last_off = _u32le(mm, last_entry + 8)
                last_size = _u32le(mm, last_entry + 12)
                if last_off is None or last_size is None:
                    continue
                last_chunk_pos = base_offset + last_off
                if not (0 <= last_chunk_pos <= len(mm)-8):
                    continue
                if bytes(mm[last_chunk_pos:last_chunk_pos+4]) != last_id:
                    continue
                header_size = _u32le(mm, last_chunk_pos + 4)
                if header_size is None or header_size != last_size:
                    continue
                end = last_chunk_pos + 8 + last_size + (last_size & 1)
                if end > len(mm):
                    continue

                target_idx_offset = idx_offset
                target_idx_size = idx_size
                actual_movi_end = end
                print(f"    [+] 매칭 성공! 실제 영상 데이터 끝 지점 추적 완료: {hex(actual_movi_end)}")
                break

            if target_idx_offset < 0:
                print(f"[-] {file_path}: 1번 영상에 매칭되는 올바른 인덱스가 없습니다.")
                return False

            new_movi_size = actual_movi_end - movi_start - 8
            output_size = actual_movi_end + 8 + target_idx_size
            new_riff_size = output_size - 8
            if not (0 <= new_movi_size <= 0xFFFFFFFF and 0 <= new_riff_size <= 0xFFFFFFFF):
                print(f"[-] {file_path}: 복구 결과가 AVI 1.0 32bit RIFF 크기 범위를 벗어납니다.")
                return False

            # 전체 파일을 메모리에 복사하지 않고 결과 구간과 idx1을 스트리밍 복사한다.
            os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
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

                src.seek(target_idx_offset)
                remaining = 8 + target_idx_size
                while remaining:
                    block = src.read(min(copy_block, remaining))
                    if not block:
                        print(f"[-] {file_path}: idx1 복사 중 예상보다 일찍 EOF 도달")
                        return False
                    out.write(block)
                    remaining -= len(block)

                out.seek(movi_start + 4)
                out.write(new_movi_size.to_bytes(4, "little"))
                out.seek(4)
                out.write(new_riff_size.to_bytes(4, "little"))

                avih_frames_pos = _find_avih_total_frames_pos(mm, movi_start)
                if avih_frames_pos is not None and avih_frames_pos + 4 <= actual_movi_end:
                    frame_count = 0
                    first_entry = target_idx_offset + 8
                    for i in range(target_idx_size // 16):
                        cid = bytes(mm[first_entry+i*16:first_entry+i*16+4])
                        if VIDEO_CHUNK_RE.fullmatch(cid):
                            frame_count += 1
                    out.seek(avih_frames_pos)
                    out.write(frame_count.to_bytes(4, "little"))
                    print(f"    [+] 헤더 프레임 수 복구 완료 ({frame_count} Frames)")

            print(f"    [+] 파일 복구 완료! (결과물 크기: {output_size:,} Bytes)\n")
            return True
        finally:
            mm.close()

def process_all_samples(input_folder, output_folder):
    os.makedirs(output_folder, exist_ok=True)
    files = glob.glob(os.path.join(input_folder, "REC_*.avi"))
    if not files:
        print(f"'{input_folder}' 폴더 내에 처리할 .avi 파일이 없습니다.")
        return
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


if __name__ == "__main__":
    INPUT_DIR = "."
    OUTPUT_DIR = "./Recovered_2"
    process_all_samples(INPUT_DIR, OUTPUT_DIR)
