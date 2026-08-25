import os
import glob

def fix_blackbox_video(file_path, output_path):
    print(f"[*] 처리 시작: {file_path}")
    
    with open(file_path, 'rb') as f:
        data = f.read()

    # 1. 1번 RIFF의 movi 시작점 찾기 (크기는 믿지 않음)
    offset = 12
    movi_start = -1
    
    while offset < len(data) - 8:
        chunk_id = data[offset:offset+4]
        chunk_size = int.from_bytes(data[offset+4:offset+8], byteorder='little')

        if chunk_id == b'LIST':
            list_type = data[offset+8:offset+12]
            if list_type == b'movi':
                movi_start = offset
                break

        offset += 8 + chunk_size + (chunk_size % 2)

    if movi_start == -1:
        print(f"[-] {file_path}: movi 청크를 찾을 수 없습니다.")
        return False

    # 2. 파일 내의 모든 인덱스(idx1) 탐색
    search_offset = 0
    idx1_offsets = []
    
    while True:
        idx = data.find(b'idx1', search_offset)
        if idx == -1:
            break
        idx1_offsets.append(idx)
        search_offset = idx + 4

    if not idx1_offsets:
        print(f"[-] {file_path}: 인덱스를 찾을 수 없습니다.")
        return False

    # 3. 진짜 인덱스 찾기 및 슬랙 데이터 절단점(actual_movi_end) 정밀 계산
    target_idx_data = None
    target_idx_offset = -1
    actual_movi_end = -1
    
    # 뒤에서부터 최신(진짜) 인덱스를 찾음
    for idx_offset in reversed(idx1_offsets):
        idx_size = int.from_bytes(data[idx_offset+4:idx_offset+8], byteorder='little')
        if idx_size < 16:
            continue
            
        num_entries = idx_size // 16
        
        # 첫 번째 인덱스 엔트리 정보
        first_id = data[idx_offset+8 : idx_offset+12]
        first_off = int.from_bytes(data[idx_offset+16 : idx_offset+20], byteorder='little')
        
        # 주소 계산 기준점 찾기
        base_offset = -1
        if first_off < len(data) and data[first_off : first_off+4] == first_id:
            base_offset = 0
        elif (movi_start + 8 + first_off) < len(data) and data[movi_start + 8 + first_off : movi_start + 8 + first_off + 4] == first_id:
            base_offset = movi_start + 8
        elif (movi_start + 12 + first_off) < len(data) and data[movi_start + 12 + first_off : movi_start + 12 + first_off + 4] == first_id:
            base_offset = movi_start + 12
        
        # 1번 RIFF와 매칭된다면, '마지막 프레임'을 찾아 실제 영상 끝 지점 계산
        if base_offset != -1:
            last_entry_idx = idx_offset + 8 + (num_entries - 1) * 16
            last_id = data[last_entry_idx : last_entry_idx+4]
            last_off = int.from_bytes(data[last_entry_idx+8 : last_entry_idx+12], byteorder='little')
            last_size = int.from_bytes(data[last_entry_idx+12 : last_entry_idx+16], byteorder='little')
            
            last_chunk_pos = base_offset + last_off
            
            # 마지막 프레임 데이터까지 정확하게 일치하는지(죽은 인덱스가 아닌지) 최종 검증
            if last_chunk_pos < len(data) and data[last_chunk_pos : last_chunk_pos+4] == last_id:
                target_idx_offset = idx_offset
                target_idx_data = data[idx_offset : idx_offset + 8 + idx_size]
                
                # 영상 프레임이 끝나는 진짜 위치 (이 뒤부터가 진짜 슬랙 데이터임)
                actual_movi_end = last_chunk_pos + 8 + last_size + (last_size % 2)
                
                print(f"    [+] 매칭 성공! 실제 영상 데이터 끝 지점 추적 완료: {hex(actual_movi_end)}")
                break
                
    if target_idx_data is None:
        print(f"[-] {file_path}: 1번 영상에 매칭되는 올바른 인덱스가 없습니다.")
        return False

    if actual_movi_end > len(data):
        actual_movi_end = len(data)

    # 4. 영상 데이터 자르기 (슬랙 데이터 완벽 제거)
    recovered_data = bytearray(data[:actual_movi_end])
    
    # 5. movi 청크 가짜 크기 -> 진짜 크기로 교정
    new_movi_size = actual_movi_end - movi_start - 8
    recovered_data[movi_start+4 : movi_start+8] = new_movi_size.to_bytes(4, byteorder='little')
    
    # 6. 제대로 된 인덱스 1개 이어 붙이기
    recovered_data.extend(target_idx_data)

    # 7. 전체 RIFF Size 크기 맞춰주기
    new_riff_size = len(recovered_data) - 8
    recovered_data[4:8] = new_riff_size.to_bytes(4, byteorder='little')
    
    # 8. 헤더(avih)의 프레임 수 강제 교정 (재생 불가 오류 원천 차단)
    avih_idx = data.find(b'avih')
    if avih_idx != -1:
        video_frame_count = 0
        for i in range(len(target_idx_data[8:]) // 16):
            entry_idx = 8 + i * 16
            chunk_id = target_idx_data[entry_idx : entry_idx+4]
            # 영상 프레임(00dc 등) 갯수 카운트
            if chunk_id in [b'00dc', b'01dc', b'00db', b'01db']:
                video_frame_count += 1
                
        avih_frames_pos = avih_idx + 8 + 16
        recovered_data[avih_frames_pos : avih_frames_pos+4] = video_frame_count.to_bytes(4, byteorder='little')
        print(f"    [+] 헤더 프레임 수 복구 완료 ({video_frame_count} Frames)")

    # 9. 새 파일로 저장
    with open(output_path, 'wb') as f:
        f.write(recovered_data)

    print(f"    [+] 파일 복구 완료! (결과물 크기: {len(recovered_data):,} Bytes)\n")
    return True

def process_all_samples(input_folder, output_folder):
    if not os.path.exists(output_folder):
        os.makedirs(output_folder)

    search_pattern = os.path.join(input_folder, "REC_*.avi")
    files = glob.glob(search_pattern)

    if not files:
        print(f"'{input_folder}' 폴더 내에 처리할 .avi 파일이 없습니다.")
        return

    print(f"총 {len(files)}개의 파일을 발견했습니다. 작업을 시작합니다...\n")
    print("-" * 40)

    success_count = 0
    for file_path in files:
        file_name = os.path.basename(file_path)
        # 덮어쓰기 방지를 위해 파일명 명확하게 지정
        output_path = os.path.join(output_folder, f"Recovered_{file_name}")
        
        # 이미 복구된 파일을 다시 읽는 것 방지
        if not file_name.startswith("Recovered_"):
            if fix_blackbox_video(file_path, output_path):
                success_count += 1

    print("-" * 40)
    print(f"작업 완료! (성공: {success_count}개)")

if __name__ == "__main__":
    INPUT_DIR = "."
    OUTPUT_DIR = "./Recovered_2"
    
    process_all_samples(INPUT_DIR, OUTPUT_DIR)