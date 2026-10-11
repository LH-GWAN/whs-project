# ---- 여기부터: GPS_metadata_avi.py (RIFF/idx1 파서 + 스트림 선택/추출/디코딩) ----
# ---- GPS_metadata_GPRMC.py 의 텍스트 스트림 사전 판정은 decide_stream_kind 다수결로 흡수됨 ----
import argparse
import bisect
import csv
import json
import datetime
import math
import mmap
import os
import re
import struct
import sys
from dataclasses import dataclass, field

# PyInstaller windowed(console=False) 빌드나 pythonw에서는 sys.stdout이 아예
# None이다. 여기서 .encoding을 바로 읽으면 import 단계에서 AttributeError로
# 죽고, 콘솔이 없어 트레이스백조차 안 보인다.
if sys.stdout is not None and (
        sys.stdout.encoding is None or sys.stdout.encoding.lower() != "utf-8"):
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


_CSV_FORMULA_PREFIX = ("=", "+", "-", "@", "\t", "\r")


def _csv_guard(v):
    """파일에서 읽은 문자열이 '='로 시작하면 Excel이 수식으로 실행한다(리뷰 #123). 숫자는 그대로 둔다."""
    if isinstance(v, str) and v and v[0] in _CSV_FORMULA_PREFIX:
        try:
            float(v)
            return v
        except ValueError:
            return "'" + v
    return v


class _GuardDictWriter(csv.DictWriter):
    def _dict_to_list(self, rowdict):
        return [_csv_guard(v) for v in super()._dict_to_list(rowdict)]


class _GuardListWriter:
    def __init__(self, writer):
        self._writer = writer

    def writerow(self, row):
        self._writer.writerow([_csv_guard(v) for v in row])

    def writerows(self, rows):
        for row in rows:
            self.writerow(row)


def warn(msg):
    WARNINGS.append(msg)
    print(f"[WARN] {msg}")


def info(msg):
    print(msg)


def assert_riff_file(path):
    with open(path, "rb") as f:
        magic = f.read(4)
    if magic != b"RIFF":
        # 앞부분이 0으로 덮인 손상 파일도 뒤의 hdrl/movi/idx1을 찾아 처리한다(손상 파일 복구).
        warn(f"'{path}' 의 앞 4바이트가 {magic!r} 로 RIFF가 아님 - 시그니처 손상으로 보고 내용으로 구조를 찾음")


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
            if clamp_top_level:
                # 끝이 잘린 파일(주행 중 전원 차단 등): 선언 크기가 경계를 넘으면 경계까지만 읽는다.
                # 예전 조건(data_end <= filesize)은 이 분기에서 절대 참이 될 수 없어 clamp가 한 번도
                # 실행되지 않았고, 잘린 AVI는 구조 파싱 자체가 실패했다(리뷰 #10).
                warn(f"chunk {ck_id!r}@0x{pos:X} declared size가 경계를 "
                     f"넘어섬(end=0x{end:X}) - 경계 기준으로 clamp")
                data_end = end
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
        warn("파일이 RIFF로 시작하지 않음 - 구조적 파싱 불가, 바이트 스캔으로 hdrl/movi/idx1 탐색")
        return find_top_level_sections_damaged(mm)

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
        # 잘린 RIFF 안의 마지막 자식(movi)도 경계까지만 읽는다.
        for child in iter_chunks(mm, top.content_start, top.content_end, clamp_top_level=top.truncated):
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

    if idx1 is not None and movi_list:
        # 사전 할당 파일은 RIFF 선언 크기가 계획값이라 옛 녹화의 idx1까지 구조 안에 들어온다. 현재 movi의
        # 첫 청크들과 맞물리는 색인만 받는다 - 옛 idx1을 쓰면 옛 GPS가 현재 궤적으로 나온다(리뷰 #7).
        usable = min(idx1.ck_size, max(0, filesize - idx1.data_start)) // 16 * 16
        base = _idx1_entries_plausible(mm, idx1.pos, usable, chain_start=movi_list[0].content_start) if usable else None
        if base is None:
            warn(f"idx1 @0x{idx1.pos:X}가 현재 movi의 청크와 맞지 않음(옛 녹화 잔재 또는 손상) - 색인으로 쓰지 않음")
            idx1 = None
    return hdrl, movi_list, idx1, avix_count


def _scan_list(mm, list_type, start=0, limit=None):
    """'LIST....<list_type>' 머리를 바이트 스캔으로 찾아 ChunkInfo로 만든다(선언 크기는 파일 끝으로 clamp)."""
    filesize = len(mm)
    limit = filesize if limit is None else min(limit, filesize)
    pos = start
    while True:
        pos = mm.find(list_type, pos, limit)
        if pos < 0:
            return None
        if pos >= 8 and bytes(mm[pos - 8:pos - 4]) == b"LIST":
            ck_size = struct.unpack_from("<I", mm, pos - 4)[0]
            data_start = pos
            data_end = min(data_start + ck_size, filesize)
            truncated = data_start + ck_size > filesize
            if ck_size >= 4:
                return ChunkInfo(ck_id=b"LIST", ck_size=ck_size, pos=pos - 8, data_start=data_start,
                                 data_end=data_end, is_list=True, list_type=list_type,
                                 content_start=data_start + 4, content_end=data_end, truncated=truncated)
        pos += 4


def _count_avix_after(mm, pos):
    """pos부터 'RIFF....AVIX'가 선언 크기대로 이어지는 개수(시그니처 손상 파일용 - 머리부터 걸을 수 없다)."""
    filesize = len(mm)
    count = 0
    while pos + 12 <= filesize and bytes(mm[pos:pos + 4]) == b"RIFF" and bytes(mm[pos + 8:pos + 12]) == b"AVIX":
        size = struct.unpack_from("<I", mm, pos + 4)[0]
        count += 1
        if size < 4 or pos + 8 + size > filesize:
            break
        pos = pos + 8 + size + (size & 1)
    return count


def find_top_level_sections_damaged(mm):
    """RIFF 머리가 없는(앞부분 손상) 파일: hdrl·movi·idx1을 각각 바이트 스캔으로 찾는다. 손상 파일 복구용.
    기준점은 '현재 녹화의 첫 청크 사슬'(first_chunk_chain)이다. 사전 할당 파일 뒤쪽의 옛 녹화 잔재
    (옛 RIFF/hdrl/movi/idx1)가 현재 녹화로 섞이지 않게, hdrl은 movi보다 앞에 있는 것만, idx1은 현재
    movi 시작과 맞물리는 것만 인정한다(리뷰 #7, #9)."""
    filesize = len(mm)
    chain = first_chunk_chain(mm)
    idx1 = None
    base = None
    fb = find_idx1_fallback(mm, chain_start=chain)
    if fb is not None:
        pos, size = fb
        idx1 = ChunkInfo(ck_id=b"idx1", ck_size=size, pos=pos, data_start=pos + 8,
                         data_end=min(pos + 8 + size, filesize), is_list=False)
        base = _idx1_entries_plausible(mm, pos, size, chain_start=chain)
    movi = None
    if base is not None and base > 0:
        # 'LIST....movi' 머리가 0으로 덮였어도 idx1 엔트리가 가리키는 기준점이 movi FourCC 위치다.
        # 바이트 스캔으로 잡은 'movi' 문자열은 옛 녹화 잔재(슬랙)일 수 있어 믿지 않는다.
        content_start = chain if (chain is not None and base <= chain <= base + 8) else base + 4
        movi = ChunkInfo(ck_id=b"LIST", ck_size=idx1.pos - base - 4, pos=base - 8, data_start=base,
                         data_end=idx1.pos, is_list=True, list_type=b"movi",
                         content_start=content_start, content_end=idx1.pos)
        warn(f"movi 머리를 idx1 엔트리 역산으로 복원: FourCC 위치 0x{base:X}")
    elif chain is not None:
        movi_end = idx1.pos if (idx1 is not None and idx1.pos > chain) else filesize
        if chain >= 12 and bytes(mm[chain - 12:chain - 8]) == b"LIST" and bytes(mm[chain - 4:chain]) == b"movi":
            ck_size = struct.unpack_from("<I", mm, chain - 8)[0]
            declared_end = chain - 4 + ck_size
            if 4 <= ck_size and declared_end <= movi_end:
                movi_end = declared_end
            movi = ChunkInfo(ck_id=b"LIST", ck_size=ck_size, pos=chain - 12, data_start=chain - 4,
                             data_end=movi_end, is_list=True, list_type=b"movi",
                             content_start=chain, content_end=movi_end,
                             truncated=declared_end > filesize)
        else:
            # movi 머리까지 지워진 파일: 첫 청크 사슬 바로 앞을 가상 FourCC 위치로 둔다(색인 재구성용 base).
            movi = ChunkInfo(ck_id=b"LIST", ck_size=movi_end - (chain - 4), pos=chain - 12, data_start=chain - 4,
                             data_end=movi_end, is_list=True, list_type=b"movi",
                             content_start=chain, content_end=movi_end)
            warn(f"movi 머리 소실 - 첫 청크 사슬 0x{chain:X}을 현재 녹화 데이터의 시작으로 봄")
    # hdrl은 현재 movi보다 앞에 있어야 한다. movi 안에서 RIFF와 함께 나오는 hdrl은 옛 녹화 잔재다(리뷰 #9).
    hdrl_limit = movi.pos if movi is not None else 4 * 1024 * 1024
    hdrl = _scan_list(mm, b"hdrl", 0, max(hdrl_limit, 0)) if hdrl_limit > 0 else None
    if hdrl is None and movi is not None and mm.find(b"hdrl", movi.content_start) >= 0:
        warn("movi 뒤(옛 녹화 잔재)에서만 hdrl이 발견됨 - 현재 녹화의 헤더로 쓰지 않음(헤더 소실)")
    avix = 0
    if idx1 is not None:
        avix = _count_avix_after(mm, idx1.data_end + (idx1.ck_size & 1))
    elif movi is not None and movi.data_end < filesize:
        avix = _count_avix_after(mm, movi.data_end + (movi.ck_size & 1))
    warn(f"손상 파일 구조 탐색: hdrl={'있음' if hdrl else '없음'}, movi={'있음' if movi else '없음'}, "
         f"idx1={'있음' if idx1 else '없음'}" + (f", AVIX {avix}개" if avix else ""))
    return hdrl, ([movi] if movi else []), idx1, avix


# 스트림 청크 id(NNdc/NNwb/NNtx…)와 OpenDML 표준 색인(ixNN). JUNK·'LIST....rec '는 정상 구조라 걷기에서
# 통과시키되 색인 엔트리로는 만들지 않는다(리뷰 #16).
STREAM_CHUNK_ID_RE = re.compile(rb"[0-9]{2}(?:dc|db|wb|tx|st|pc|ix)|ix[0-9]{2}")
CHUNK_ID_RE = re.compile(rb"[0-9]{2}(?:dc|db|wb|tx|st|pc|ix)|ix[0-9]{2}|JUNK")
_RESYNC_RE = re.compile(rb"[0-9]{2}(?:dc|db|wb|tx|st|pc|ix)|ix[0-9]{2}|JUNK|LIST")
MAX_CHUNK_BYTES = 64 * 1024 * 1024
DAMAGE_GAPS = []   # [(start, end)] movi 안에서 청크 머리를 읽을 수 없어 건너뛴 바이트 구간(파일 절대 위치)
# 마지막 색인 재구성(synthesize_idx1)의 판정 근거. process_single_file이 DAMAGE_JSON으로 앱에 넘긴다.
SYNTH_INFO = {}
ZERO_TAIL_PROBE = 64 * 1024


def _chunk_header_ok(mm, pos, end):
    """pos에 그럴듯한 청크 머리가 있는가. 있으면 (id, size), 없으면 None.
    'LIST....rec '(OpenDML 레코드 그룹)도 머리로 인정한다 - 걷기는 그 안으로 들어간다."""
    if pos + 8 > end:
        return None
    ck_id = bytes(mm[pos:pos + 4])
    if ck_id == b"LIST":
        if pos + 12 > end or bytes(mm[pos + 8:pos + 12]) != b"rec ":
            return None
        size = struct.unpack_from("<I", mm, pos + 4)[0]
        if size < 4 or size > MAX_CHUNK_BYTES or pos + 8 + size > end:
            return None
        return ck_id, size
    if not CHUNK_ID_RE.fullmatch(ck_id):
        return None
    size = struct.unpack_from("<I", mm, pos + 4)[0]
    if size > MAX_CHUNK_BYTES or pos + 8 + size > end:
        return None
    return ck_id, size


def _next_header_ok(mm, nxt, end):
    """nxt(또는 패딩 1바이트 뒤)에 머리가 있는가 - 재동기화 후보가 진짜인지 확인용."""
    if nxt >= end:
        return True
    if _chunk_header_ok(mm, nxt, end) is not None:
        return True
    return bytes(mm[nxt:nxt + 1]) == b"\0" and _chunk_header_ok(mm, nxt + 1, end) is not None


def _is_zero_region(mm, start, end, block=1024 * 1024):
    """[start, end)가 전부 0인가(블록 단위 비교)."""
    pos = start
    zero = bytes(block)
    while pos < end:
        n = min(block, end - pos)
        if bytes(mm[pos:pos + n]) != zero[:n]:
            return False
        pos += n
    return True


def _zero_fraction(mm, start, end, block=1024 * 1024):
    """[start, end)에서 0 바이트의 비율. 0으로 덮인 손상 구간은 양끝에 잘린 청크의 꼬리가 조금 남는다."""
    if end <= start:
        return 0.0
    zeros = 0
    pos = start
    while pos < end:
        n = min(block, end - pos)
        zeros += bytes(mm[pos:pos + n]).count(0)
        pos += n
    return zeros / (end - start)


ZERO_GAP_MIN_FRACTION = 0.5
ZERO_GAP_PROBE = 4096


def first_chunk_chain(mm, start=0, end=None, max_scan=None):
    """start부터 스트림 청크 머리 두 개가 연달아 맞는 첫 위치. 현재 녹화의 movi 내용이 시작하는 자리로,
    'LIST....movi' 머리가 지워진 파일에서도 잡힌다. JUNK·LIST는 사슬의 시작으로 치지 않는다(hdrl 뒤
    JUNK 패딩이 movi 앞에 오는 정상 구조). 없으면 None."""
    end = len(mm) if end is None else min(end, len(mm))
    limit = end if max_scan is None else min(end, start + max_scan)
    scan = start
    while scan + 8 <= limit:
        m = STREAM_CHUNK_ID_RE.search(mm, scan, limit)
        if m is None:
            return None
        cand = m.start()
        h = _chunk_header_ok(mm, cand, end)
        if h is not None:
            nxt = cand + 8 + h[1] + (h[1] & 1)
            if nxt < end and _next_header_ok(mm, nxt, end):
                return cand
        scan = cand + 1
    return None


def walk_movi_chunks(mm, start, end, gaps=None, tail_info=None):
    """movi 영역을 청크 머리로 순서대로 걷는다. idx1 없이 색인을 다시 만들거나(녹화 중단) 0으로 덮인
    구간을 건너뛰기 위한 것. 읽을 수 없는 머리를 만나면 다음 그럴듯한 머리(그 다음 머리까지 맞아야 함)를
    찾아 재동기화하고 건너뛴 구간을 gaps에 남긴다. yield (pos, ck_id, size).
    'LIST....rec ' 그룹은 안으로 들어가 걷고(그룹 머리는 yield하지 않음), 마지막 청크 뒤의 0 꼬리(사전
    할당의 미사용 영역)와 파일 끝에서 잘린 마지막 청크는 손상 구간(gaps)이 아니라 tail_info에 남긴다
    (리뷰 #11, #16): tail_info["zero_tail"]=(pos, end), tail_info["truncated_tail"]=pos."""
    pos = start
    while pos + 8 <= end:
        head = _chunk_header_ok(mm, pos, end)
        if head is None and pos + 1 <= end and bytes(mm[pos:pos + 1]) == b"\0" and _chunk_header_ok(mm, pos + 1, end):
            pos += 1   # 홀수 길이 청크 뒤 패딩
            continue
        if head is None:
            # 재동기화: 머리 두 개가 연달아 맞는 위치를 찾는다.
            found = None
            scan = pos + 1
            while scan + 8 <= end:
                m = _RESYNC_RE.search(mm, scan, min(end, scan + 4 * 1024 * 1024))
                if m is None:
                    scan = min(end, scan + 4 * 1024 * 1024 - 8)
                    continue
                cand = m.start()
                h = _chunk_header_ok(mm, cand, end)
                if h is not None:
                    nxt = cand + 12 if h[0] == b"LIST" else cand + 8 + h[1] + (h[1] & 1)
                    if _next_header_ok(mm, nxt, end):
                        found = cand
                        break
                scan = cand + 1
            if found is None:
                ck_id = bytes(mm[pos:pos + 4])
                if end >= len(mm) and STREAM_CHUNK_ID_RE.fullmatch(ck_id) \
                        and pos + 8 + struct.unpack_from("<I", mm, pos + 4)[0] > end:
                    # 파일 끝에서 잘린 마지막 청크(전원 차단) - 손상 구간이 아니라 절단이다.
                    if tail_info is not None:
                        tail_info["truncated_tail"] = pos
                    warn(f"movi @0x{pos:X}: 마지막 청크 {ck_id!r}가 파일 끝에서 잘림")
                elif _is_zero_region(mm, pos, min(end, pos + ZERO_TAIL_PROBE)):
                    # 마지막 실제 청크 뒤의 0 꼬리: 사전 할당 파일의 미사용 영역이지 손상이 아니다. 빈 자리를
                    # 넣으면 실제 기록이 앞으로 몰려 시간축이 압축된다(리뷰 #11).
                    if tail_info is not None:
                        tail_info["zero_tail"] = (pos, end)
                    warn(f"movi @0x{pos:X}: 마지막 청크 뒤 {end - pos:,}바이트가 0으로 채워짐 - 미사용 할당 영역으로 봄")
                elif gaps is not None:
                    gaps.append((pos, end))
                return
            if gaps is not None:
                gaps.append((pos, found))
            warn(f"movi @0x{pos:X}: 청크 머리를 읽을 수 없음 - 0x{found:X}에서 재동기화 ({found - pos}바이트 건너뜀)")
            pos = found
            continue
        ck_id, size = head
        if ck_id == b"LIST":
            pos += 12   # 'LIST....rec ' 그룹 안으로
            continue
        yield pos, ck_id, size
        nxt = pos + 8 + size
        if size & 1:
            # RIFF 규격은 홀수 청크 뒤에 패딩 1바이트지만, VUGERA 등 일부 기기는 패딩 없이 바로 다음
            # 청크를 붙인다. 실제 머리가 있는 쪽을 따른다.
            if _chunk_header_ok(mm, nxt, end) is None and bytes(mm[nxt:nxt + 1]) == b"\0" \
                    and _chunk_header_ok(mm, nxt + 1, end) is not None:
                nxt += 1
        pos = nxt


def synthesize_idx1(mm, movi_fourcc_pos, movi_end):
    """idx1이 없을 때 movi를 걸어 색인을 만든다(offset은 'movi' FourCC 기준 = 기본 base A). 건너뛴 손상
    구간에는 스트림별 빈 자리(placeholder, idx_offset=-1)를 넣어 시간축(균등 분할)이 뒤로 밀리지 않게 한다.
    빈 자리 수는 손상 구간 앞뒤 GPS UTC 차이로 구하고, 그게 없으면 직전 2MB의 청크 밀도로 추정한다.
    녹화 끝은 마지막 실제 청크다: 0 꼬리·잘린 마지막 청크에는 빈 자리를 넣지 않고(리뷰 #11), 옛 녹화 경계
    (recording_cut_at)가 잡히면 그 뒤는 버린다. 판정 근거는 SYNTH_INFO에 남긴다."""
    SYNTH_INFO.clear()
    entries = []
    gaps = []
    tail = {}
    content_start = movi_fourcc_pos + 4
    walked = []   # (pos, ck_id, size)
    for pos, ck_id, size in walk_movi_chunks(mm, content_start, movi_end, gaps, tail_info=tail):
        if ck_id == b"JUNK":
            continue
        walked.append((pos, ck_id, size))
    gaps = sorted(gaps)
    cut_info = {}
    cut_at = recording_cut_at(mm, walked, gaps, info=cut_info)
    if cut_at is not None:
        walked = [w for w in walked if w[0] < cut_at]
        gaps = [g for g in gaps if g[0] < cut_at]
        tail = {}
    gap_iter = iter(gaps)
    pending_gap = next(gap_iter, None)
    for pos, ck_id, size in walked:
        while pending_gap is not None and pending_gap[0] < pos:
            entries.extend(_placeholder_entries(walked, pending_gap, mm=mm))
            pending_gap = next(gap_iter, None)
        entries.append({"chunk_id": ck_id, "flags": 0, "idx_offset": pos - movi_fourcc_pos, "length": size})
    while pending_gap is not None:
        entries.extend(_placeholder_entries(walked, pending_gap, mm=mm))
        pending_gap = next(gap_iter, None)
    DAMAGE_GAPS.extend(gaps)
    last_chunk_end = (walked[-1][0] + 8 + walked[-1][2]) if walked else content_start
    SYNTH_INFO.update({
        "cut_at": cut_at, "cut_reason": cut_info.get("reason", ""),
        "boundary_uncertain": bool(cut_info.get("uncertain")),
        "zero_tail": list(tail["zero_tail"]) if tail.get("zero_tail") else None,
        "truncated_tail": tail.get("truncated_tail"),
        "last_chunk_end": last_chunk_end,
        "placeholders": sum(1 for e in entries if e["idx_offset"] < 0),
    })
    warn(f"idx1 없음 - movi를 걸어 색인 {len(entries)}개를 만듦"
         + (f", 손상 구간 {len(gaps)}곳(빈 자리 {SYNTH_INFO['placeholders']}개)" if gaps else ""))
    return entries


def recording_cut_at(mm, walked, gaps, info=None):
    """사전 할당 파일은 실제 녹화 끝 뒤에 옛 녹화 잔재가 남는다. idx1이 없으면 어디가 끝인지 알 수 없으니
    손상 구간 앞뒤의 GPS 연속성으로 판단한다(리뷰 #8). 근거는 체크섬이 맞고 날짜가 있는 A 상태 RMC
    (날짜+UTC, 자정 넘김 처리)뿐이다 - V 문장의 RTC 시각이나 체크섬이 깨진 문장은 근거가 아니다.
      - 구간 뒤의 첫 기록이 앞의 마지막 기록보다 과거이면 → 옛 녹화 잔재(확실한 경계)
      - 손상 바이트 수로 추정한 경과 시간보다 훨씬 더 뛰면 → 이어지지 않음(경계)
      - 앞이나 뒤에 근거가 없으면: 구간이 0으로 덮인 것이면 같은 녹화로 보고, 아니면 '경계 불확실'로
        표시하고 거기서 자른다(fail-open으로 옛 녹화를 섞지 않되, 뒤는 슬랙으로 따로 남긴다)
    반환: 끝 위치 또는 None. info(dict)에 reason/uncertain/before/after를 남긴다."""
    for g0, g1 in sorted(gaps):
        before_chunks = [w for w in walked if w[0] < g0]
        after_chunks = [w for w in walked if w[0] >= g1]
        before = _last_rmc_anchor(mm, before_chunks)
        after = _first_rmc_anchor(mm, after_chunks)
        if before is not None and after is not None:
            du = after[1] - before[1]
            rate = _bytes_per_second(mm, before_chunks)
            expected = (g1 - g0) / rate if rate else None
            allow = (expected * 3 + 600) if expected is not None else 600
            if du < -1:
                reason = (f"손상 구간 0x{g0:X} 뒤의 GPS 시각이 과거(앞 {_fmt_anchor(before)} → 뒤 {_fmt_anchor(after)})")
            elif du > allow:
                reason = (f"손상 구간 0x{g0:X} 뒤의 GPS 시각이 손상 길이로 설명되지 않게 뜀"
                          f"(앞 {_fmt_anchor(before)} → 뒤 {_fmt_anchor(after)}, {du:.0f}초"
                          + (f", 손상 바이트로 추정한 경과 {expected:.0f}초" if expected is not None else "") + ")")
            else:
                continue
            warn(reason + " - 여기까지를 현재 녹화로 보고 뒤는 옛 녹화 잔재로 제외")
            if info is not None:
                info.update({"reason": reason, "uncertain": False, "before": before, "after": after})
            return g0
        if _is_zero_region(mm, g0, min(g1, g0 + ZERO_GAP_PROBE)) and _zero_fraction(mm, g0, g1) >= ZERO_GAP_MIN_FRACTION:
            # 0으로 덮인 구간은 같은 파일 안의 손상이지 옛 녹화와의 경계가 아니다(경계라면 옛 녹화의 청크
            # 데이터가 바로 이어진다). 구간은 0으로 시작하고(머리가 지워진 자리) 끝에는 반쯤 지워진 청크의
            # 꼬리가 남는다. GPS 근거가 없어도 같은 녹화로 본다.
            warn(f"손상 구간 0x{g0:X}: 앞뒤 GPS 시각 근거가 없지만 0으로 덮인 구간이라 같은 녹화로 봄")
            continue
        reason = (f"손상 구간 0x{g0:X} 앞뒤에 날짜 있는 A 상태 RMC가 없어 녹화 경계를 확인할 수 없음"
                  f"(앞 {_fmt_anchor(before)}, 뒤 {_fmt_anchor(after)})")
        warn(reason + " - 경계 불확실: 여기까지를 현재 녹화로 보고 뒤는 슬랙으로 분리")
        if info is not None:
            info.update({"reason": reason, "uncertain": True, "before": before, "after": after})
        return g0
    return None


_RMC_UTC_RE = re.compile(rb"\$[A-Z]{2}RMC,(\d{2})(\d{2})(\d{2}(?:\.\d+)?),")
_RMC_SENTENCE_RE = re.compile(rb"\$([A-Z]{2}RMC,[ -~]*?)\*([0-9A-Fa-f]{2})")
ANCHOR_SCAN_CHUNKS = 20000   # 경계 판정 때 손상 구간 앞뒤로 살펴보는 청크 수(30fps+오디오로 약 5분)
ANCHOR_MAX_CHUNK_BYTES = 4096


def _chunk_rmc_utc(mm, pos, size):
    m = _RMC_UTC_RE.search(mm, pos + 8, min(pos + 8 + size, len(mm)))
    if not m:
        return None
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))


def _chunk_rmc_anchor(mm, pos, size):
    """청크 안의 '근거가 되는' RMC: 체크섬이 맞고 상태 A이며 날짜가 있는 문장. 반환 (pos, 절대초) 또는 None.
    절대초 = 날짜(서수)*86400 + UTC초 - 자정을 넘겨도 연속성 비교가 된다."""
    end = min(pos + 8 + size, len(mm))
    for m in _RMC_SENTENCE_RE.finditer(mm, pos + 8, end):
        body = m.group(1)
        calc = 0
        for b in body:
            calc ^= b
        if calc != int(m.group(2), 16):
            continue
        fields = body.split(b",")
        if len(fields) < 10 or fields[2] != b"A":
            continue
        t, d = fields[1], fields[9]
        if len(t) < 6 or not t[:6].isdigit() or len(d) != 6 or not d.isdigit():
            continue
        try:
            day = datetime.date(2000 + int(d[4:6]), int(d[2:4]), int(d[0:2]))
            utc = int(t[0:2]) * 3600 + int(t[2:4]) * 60 + float(t[4:])
        except ValueError:
            continue
        if utc >= 86400:
            continue
        return pos, day.toordinal() * 86400 + utc
    return None


def _last_rmc_anchor(mm, chunks, limit=ANCHOR_SCAN_CHUNKS):
    for pos, _cid, size in reversed(chunks[-limit:]):
        if size <= ANCHOR_MAX_CHUNK_BYTES:
            a = _chunk_rmc_anchor(mm, pos, size)
            if a is not None:
                return a
    return None


def _first_rmc_anchor(mm, chunks, limit=ANCHOR_SCAN_CHUNKS):
    for pos, _cid, size in chunks[:limit]:
        if size <= ANCHOR_MAX_CHUNK_BYTES:
            a = _chunk_rmc_anchor(mm, pos, size)
            if a is not None:
                return a
    return None


def _fmt_anchor(anchor):
    if anchor is None:
        return "없음"
    day, sec = divmod(anchor[1], 86400)
    return f"{datetime.date.fromordinal(int(day)):%Y-%m-%d} {int(sec) // 3600:02d}:{(int(sec) // 60) % 60:02d}:{sec % 60:05.2f}"


def _bytes_per_second(mm, chunks, window=2 * 1024 * 1024):
    """손상 구간 직전 window 바이트 안의 앵커 두 개로 기록 속도(바이트/초)를 구한다. 못 구하면 None."""
    if not chunks:
        return None
    g0 = chunks[-1][0] + 8 + chunks[-1][2]
    recent = [c for c in chunks[-ANCHOR_SCAN_CHUNKS:] if c[0] >= g0 - window]
    last = _last_rmc_anchor(mm, recent)
    first = _first_rmc_anchor(mm, recent)
    if last is None or first is None or last[1] - first[1] < 5:
        return None
    return (last[0] - first[0]) / (last[1] - first[1])


def _last_rmc_utc(mm, chunks):
    for pos, _cid, size in reversed(chunks):
        if size <= 4096:
            u = _chunk_rmc_utc(mm, pos, size)
            if u is not None:
                return u
    return None


def _first_rmc_utc(mm, chunks):
    for pos, _cid, size in chunks:
        if size <= 4096:
            u = _chunk_rmc_utc(mm, pos, size)
            if u is not None:
                return u
    return None


def _placeholder_entries(walked, gap, mm=None):
    """손상 구간(gap)에 넣을 스트림별 빈 자리 수. 구간 앞뒤에 날짜 있는 A 상태 RMC가 있으면 그 시각 차이로
    (잃어버린 초 × 스트림별 초당 청크 수) 정하고, 없으면 직전 2MB의 청크 밀도(바이트÷밀도)로 추정한다."""
    g0, g1 = gap
    window_start = max(0, g0 - 2 * 1024 * 1024)
    before = [(p, cid, sz) for p, cid, sz in walked if window_start <= p < g0]
    if not before:
        before = [(p, cid, sz) for p, cid, sz in walked if p >= g1][:200]
    if not before:
        return []
    counts = {}
    for _p, cid, _sz in before:
        counts[cid] = counts.get(cid, 0) + 1
    lost_by_cid = None
    if mm is not None:
        lost_by_cid = _placeholder_counts_from_anchors(mm, walked, gap, counts)
    if lost_by_cid is None:
        span = max(1, (before[-1][0] + 8 + before[-1][2]) - before[0][0])
        lost_by_cid = {cid: round(n * (g1 - g0) / span) for cid, n in counts.items()}
    out = []
    for cid, lost in lost_by_cid.items():
        out.extend({"chunk_id": cid, "flags": 0, "idx_offset": -1, "length": 0, "placeholder": True}
                   for _ in range(max(0, lost)))
    # 스트림이 섞여 있던 순서를 흉내 내지는 않는다(균등 분할 시간축은 개수만 쓴다).
    return out


def _placeholder_counts_from_anchors(mm, walked, gap, counts):
    """앞뒤 앵커(날짜 있는 A 상태 RMC)의 시각 차이로 스트림별 잃어버린 청크 수를 센다. 근거가 없으면 None."""
    g0, g1 = gap
    before_chunks = [w for w in walked if w[0] < g0]
    after_chunks = [w for w in walked if w[0] >= g1]
    a1 = _last_rmc_anchor(mm, before_chunks)
    a2 = _first_rmc_anchor(mm, after_chunks)
    if a1 is None or a2 is None:
        return None
    du = a2[1] - a1[1]
    if du < 0:
        return None
    recent = [c for c in before_chunks[-ANCHOR_SCAN_CHUNKS:] if c[0] >= a1[0] - 2 * 1024 * 1024]
    a0 = _first_rmc_anchor(mm, recent)
    if a0 is None or a1[1] - a0[1] < 5:
        return None
    span_sec = a1[1] - a0[1]
    anchor_cid = next((cid for p, cid, _sz in walked if p == a1[0]), None)
    out = {}
    for cid in counts:
        n_window = sum(1 for p, c, _sz in recent if c == cid and a0[0] < p <= a1[0])
        rate = n_window / span_sec
        present = sum(1 for p, c, _sz in walked if c == cid and a1[0] < p < a2[0])
        expected = rate * du - (1 if cid == anchor_cid else 0)
        out[cid] = max(0, round(expected - present))
    return out


def find_movi_fallback(mm):
    pos = mm.find(b"movi")
    if pos < 0:
        return None
    warn("구조 파싱 실패, movi 바이트 스캔 fallback 사용")
    return pos


BASE_SAMPLE_ENTRIES = 32   # base 판정·idx1 검증에 쓰는 표본 수(색인 전체에서 고르게 뽑는다)


def _entry_tuple(e):
    """idx1 엔트리(dict 또는 (cid, flags, off, length) 튜플)를 (cid, flags, off, length)로."""
    if isinstance(e, dict):
        return e["chunk_id"], e.get("flags", 0), e["idx_offset"], e["length"]
    return e[0], e[1], e[2], e[3]


def _spread_sample(entries, n=BASE_SAMPLE_ENTRIES):
    """색인 전체에서 고르게 n개(앞 8개 포함). 앞부분이 손상된 파일에서 앞 엔트리만 보면 정답 base가 0점이
    된다(리뷰 #27, #80)."""
    total = len(entries)
    if total <= n:
        return list(entries)
    picks = set(range(min(8, total)))
    picks |= {round(i * (total - 1) / (n - 1)) for i in range(n)}
    return [entries[i] for i in sorted(picks)]


def _entry_target_state(mm, base, entry):
    """엔트리가 가리키는 자리의 상태: 'match'(머리 일치) / 'zero'(0으로 덮임) / 'out'(파일 밖) / 'mismatch'."""
    cid, _flags, off, length = _entry_tuple(entry)
    p = base + off
    if p < 0 or p + 8 > len(mm):
        return "out"
    head = bytes(mm[p:p + 8])
    actual_id, actual_len = head[:4], struct.unpack_from("<I", head, 4)[0]
    want_id = b"LIST" if cid == b"rec " else cid
    if actual_id == want_id and actual_len == length:
        return "match"
    if head == bytes(8):
        return "zero"
    return "mismatch"


def _score_base(mm, base, sample):
    """표본 엔트리 가운데 (일치 수, 판정 가능 수). 0으로 덮인 자리·파일 밖은 분모에서 뺀다."""
    hits = decided = 0
    for e in sample:
        st = _entry_target_state(mm, base, e)
        if st == "match":
            hits += 1
            decided += 1
        elif st == "mismatch":
            decided += 1
    return hits, decided


def infer_base_from_entries(mm, entries, limit=16 * 1024 * 1024):
    """엔트리의 (id, size) 머리를 파일 앞부분에서 직접 찾아 base(offset 기준점)를 역산한다. 'movi'
    FourCC가 0으로 덮인 헤더 손상 파일에서도 색인을 쓸 수 있게 한다. 후보 base는 색인 전체에서 고르게 뽑은
    표본과 과반 일치해야 받는다 - 고정 크기 청크(오디오 등)의 우연 일치로 엉뚱한 base가 뽑히면 idx1 전체가
    다른 청크에 대응된다(리뷰 #27). 못 찾으면 None."""
    if not entries:
        return None
    sample = _spread_sample(entries)
    votes = {}
    limit = min(len(mm), limit)
    for e in sample:
        cid, _flags, off, length = _entry_tuple(e)
        if cid == b"rec ":
            continue
        needle = cid + struct.pack("<I", length)
        p = mm.find(needle, 0, limit)
        hops = 0
        while p >= 0 and hops < 64:
            base = p - off
            if base >= 0:
                votes[base] = votes.get(base, 0) + 1
            p = mm.find(needle, p + 1, limit)
            hops += 1
    if not votes:
        return None
    best = None
    for base, _v in sorted(votes.items(), key=lambda kv: -kv[1])[:8]:
        hits, decided = _score_base(mm, base, sample)
        if hits >= 3 and hits * 2 >= decided and (best is None or hits > best[1]):
            best = (base, hits)
    return best[0] if best else None


def _index_reaches_chain(mm, base, entries, chain_start, max_chunks=256):
    """색인이 현재 녹화의 첫 청크 사슬(chain_start)과 맞물리는가: 엔트리가 가리키는 가장 앞 자리가 사슬
    시작보다 앞이거나, 사슬 시작에서 청크를 따라가 그 자리에 정확히 닿아야 한다. movi 안에 통째로 남은
    옛 AVI의 idx1이나 뒤에 이어붙은 옛 파일의 idx1은 가리키는 자리가 사슬 밖이라 걸러진다(리뷰 #7)."""
    if chain_start is None:
        return True
    targets = [base + _entry_tuple(e)[2] for e in entries]
    if not targets:
        return False
    first = min(targets)
    if first <= chain_start:
        return True
    pos = chain_start
    end = len(mm)
    for _ in range(max_chunks):
        if pos == first:
            return True
        if pos > first:
            return False
        head = _chunk_header_ok(mm, pos, end)
        if head is None:
            if bytes(mm[pos:pos + 1]) == b"\0" and _chunk_header_ok(mm, pos + 1, end) is not None:
                pos += 1
                continue
            return False
        pos = pos + 12 if head[0] == b"LIST" else pos + 8 + head[1] + (head[1] & 1)
    return False


def _idx1_entries_plausible(mm, pos, size, chain_start=None):
    """바이트 스캔으로 찾은 idx1이 진짜(현재 녹화의 것)인지. 조건:
      - 표본 엔트리의 chunk id가 규격('rec ' 그룹 엔트리 포함)이고,
      - movi 기준/절대/역산 base 가운데 하나로, 0으로 덮이지 않은 자리의 절반 이상이 실제 청크 머리와 맞고
        (앞부분 손상 파일은 앞 엔트리가 0 영역을 가리켜도 통과 - 리뷰 #80),
      - 판정 가능한 첫 엔트리들(앞 8개)이 맞아야 한다. 사전 할당 파일 끝에 남은 옛 idx1은 앞 엔트리가
        현재 녹화의 청크를 가리켜 어긋나고 뒤 엔트리만 옛 청크와 맞는다(리뷰 #7),
      - chain_start가 주어지면 색인이 현재 movi의 첫 청크 사슬과 맞물려야 한다.
    반환: 맞으면 base(int), 아니면 None."""
    n_total = size // 16
    if n_total == 0:
        return None
    n_head = min(n_total, 16)
    head_entries = [struct.unpack_from("<4sIII", mm, pos + 8 + i * 16) for i in range(n_head)]
    if any(not (CHUNK_ID_RE.fullmatch(cid) or cid == b"rec ") or length > MAX_CHUNK_BYTES
           for cid, _f, _o, length in head_entries):
        return None
    if n_total > n_head:
        picks = sorted({round(i * (n_total - 1) / (BASE_SAMPLE_ENTRIES - 1)) for i in range(BASE_SAMPLE_ENTRIES)}
                       - set(range(n_head)))
        sample = head_entries + [struct.unpack_from("<4sIII", mm, pos + 8 + i * 16) for i in picks]
        if any(not (CHUNK_ID_RE.fullmatch(cid) or cid == b"rec ") or length > MAX_CHUNK_BYTES
               for cid, _f, _o, length in sample[n_head:]):
            return None
    else:
        sample = head_entries
    movi = mm.find(b"movi", 0, pos)
    bases = [0]
    if movi >= 0:
        bases += [movi, movi + 4]
    inferred = infer_base_from_entries(mm, sample)
    if inferred is not None and inferred not in bases:
        bases.append(inferred)
    # 'movi' FourCC까지 0으로 덮인 앞부분 손상 파일: 첫 청크 사슬(chain_start)에 어느 엔트리가 닿는다고 보고
    # base를 역산한 후보도 넣는다. 뒤에 붙은 옛 녹화의 청크 배치가 현재와 같으면 (id, size) 역산은 옛 쪽으로
    # 끌려가므로(위 inferred) 이 후보가 없으면 진짜 idx1을 놓친다.
    if chain_start is not None:
        for e in head_entries:
            cand = chain_start - _entry_tuple(e)[2]
            if cand >= 0 and cand not in bases:
                bases.append(cand)
    best = None
    for base in bases:
        # 현재 녹화의 색인은 자기 movi 뒤에 있다 - 표본이 idx1 뒤를 가리키는 base는 옛 녹화 것이다.
        if any(base + _entry_tuple(e)[2] >= pos for e in sample):
            continue
        states = [_entry_target_state(mm, base, e) for e in sample]
        decided = [s for s in states if s in ("match", "mismatch")]
        hits = decided.count("match")
        if hits == 0 or hits * 2 < len(decided):
            continue
        lead = decided[:8]
        if lead.count("match") * 2 < len(lead):
            continue
        if not _index_reaches_chain(mm, base, sample, chain_start):
            continue
        if best is None or hits > best[1]:
            best = (base, hits)
    return best[0] if best else None


def find_idx1_fallback(mm, chain_start=None):
    """'idx1'을 뒤에서부터 찾되 현재 녹화의 것만 받는다(_idx1_entries_plausible). 사전 할당 파일 뒤쪽의
    옛 idx1은 검증에서 떨어지므로 계속 앞으로 찾는다. 반환 (pos, size) 또는 None."""
    filesize = len(mm)
    if chain_start is None:
        chain_start = first_chunk_chain(mm)
    search_end = filesize
    # 검증을 통과한 후보를 모두 모은 뒤, 자기가 가리키는 영역(마지막 엔트리 끝) 뒤에 있는 것 가운데 가장 앞의
    # idx1을 고른다. 뒤에 붙은 옛 녹화의 idx1이 현재 녹화와 청크 배치가 같아 검증을 통과해도, 현재 녹화의
    # idx1이 그보다 앞(영역 바로 뒤)에 있으므로 그것이 뽑힌다(리뷰 #7 후속).
    candidates = []
    while True:
        pos = mm.rfind(b"idx1", 0, search_end)
        if pos < 0:
            break
        if pos + 8 <= filesize and (chain_start is None or pos > chain_start):
            size = struct.unpack_from("<I", mm, pos + 4)[0]
            usable = None
            if size % 16 == 0 and pos + 8 + size <= filesize:
                usable = size
            elif size % 16 == 0 and size > 0 and pos + 8 < filesize and pos + 8 + size > filesize:
                usable = ((filesize - pos - 8) // 16) * 16      # 끝이 잘린 idx1(리뷰 #10)
            if usable:
                base = _idx1_entries_plausible(mm, pos, usable, chain_start=chain_start)
                if base is not None:
                    candidates.append((pos, usable, usable < size, _index_region_end(mm, base, pos, usable)))
        search_end = pos
        if search_end <= 0:
            break
    if not candidates:
        return None
    after = [c for c in candidates if c[3] <= c[0]]
    pos, usable, truncated, _end = min(after or candidates, key=lambda c: c[0])
    if truncated:
        warn(f"idx1 @0x{pos:X}가 파일 끝에서 잘림 - 남은 {usable}바이트만 사용")
    else:
        warn("구조 파싱 실패, idx1 바이트 스캔(rfind) fallback 사용 "
             f"(size 16배수·엔트리 검증 통과, @0x{pos:X})")
    return pos, usable


def _index_region_end(mm, base, pos, size):
    """색인이 가리키는 영역의 끝(표본 엔트리 가운데 가장 뒤 청크의 끝). 마지막 엔트리를 포함한다."""
    n = size // 16
    picks = {n - 1} | {round(i * (n - 1) / 7) for i in range(8)} if n > 0 else set()
    end = 0
    for i in picks:
        e = struct.unpack_from("<4sIII", mm, pos + 8 + i * 16)
        end = max(end, base + e[2] + 8 + e[3])
    return end


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
            if s.fcc_type in STANDARD_AV_FCCTYPES:
                continue
            ids = set(s.observed_chunk_ids)
            if s.fcc_type is None:
                # hdrl을 못 읽으면 타입을 모른다. 청크 ID 접미사가 표준 영상(dc/db)·오디오(wb)·팔레트(pc)면
                # 그 스트림은 뺀다 - 예전엔 영상·오디오까지 통째로 두 벌 덤프했다(리뷰 #61).
                av = {cid for cid in ids if cid[2:4] in (b"dc", b"db", b"wb", b"pc")}
                if av:
                    warn(f"stream {s.index}: hdrl 없이 청크 ID {sorted(av)!r}가 영상/오디오 규격이라 추출 대상에서 제외")
                    ids -= av
            if not ids:
                continue
            s.selected = True
            selected_chunk_ids |= ids
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

    # 표본은 색인 전체에서 고르게 뽑고(빈 자리 제외), 0으로 덮인 자리는 분모에서 뺀다. 앞 엔트리만 보면
    # movi 앞부분이 손상된 파일에서 정답 후보 A가 0점이 되고, 고정 크기 청크의 우연 일치로 역산 base가
    # 뽑혔다(리뷰 #27).
    real_entries = [e for e in idx1_entries if e.get("idx_offset", -1) >= 0] if idx1_entries else []
    sample = _spread_sample(real_entries, max(sample_n, BASE_SAMPLE_ENTRIES)) if real_entries else []
    # 후보 D: 'movi' FourCC가 0으로 덮였거나 엉뚱한 곳에서 잡힌 경우, 엔트리의 (id, size) 머리를
    # 파일 앞 16MB에서 직접 찾아 base를 역산한다(헤더 손상 파일 복구). 표본과 과반 일치할 때만 나온다.
    best = infer_base_from_entries(mm, real_entries) if real_entries else None
    if best is not None and best not in candidates.values():
        candidates[f"D(엔트리 역산 base=0x{best:X})"] = best
    scores = {}
    decided_n = {}
    for label, base in candidates.items():
        hits, decided = _score_base(mm, base, sample)
        scores[label] = hits
        decided_n[label] = decided

    info("[Base offset 후보 매치 점수]")
    for label, score in scores.items():
        info(f"  {label}: {score}/{decided_n[label]} (표본 {len(sample)}개, 0으로 덮인 자리 제외)")

    if not scores or max(scores.values()) == 0:
        warn("어떤 base 후보도 idx1 엔트리와 일치하지 않음 - base 불확실, 기본값(A) 사용")
        chosen_label = "A(movi FourCC 위치)" if movi_fourcc_pos is not None else "C(절대 offset, base=0)"
        chosen_base = candidates.get(chosen_label, 0)
        return chosen_base, chosen_label, scores, True

    # 동점이면 먼저 적힌 후보(A·B·C 순)가 이긴다 - 역산 후보 D는 A·B·C가 모두 밀릴 때만 쓴다.
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
    """청크 안의 NMEA 문장 중 쓸 것 하나. 첫 매치만 쓰면 GGA→GSA→RMC 순서의 청크에서 속도·날짜가
    사라졌다(리뷰 #53). RMC(속도·날짜·방위 포함)를 우선하고, 같은 종류 안에서는 checksum이 맞는
    첫 문장을 고른다."""
    cands = [m.group(1).decode("ascii", errors="replace") for m in EMBEDDED_NMEA_RE.finditer(payload)]
    if not cands:
        return None
    # 체크섬이 맞는 RMC → 체크섬이 맞는 GGA → (체크섬 없는) RMC → GGA → 나머지. 종류를 먼저 보면 체크섬이
    # 틀린 RMC가 정상 GGA를 밀어내 측위 자체가 없어졌다(리뷰 #107).
    for ok in (True, None):
        for want in ("RMC", "GGA"):
            for c in cands:
                if c[2:5] == want and nmea_checksum_ok(c) is ok:
                    return c
    for want in ("RMC", "GGA"):
        typed = [c for c in cands if c[2:5] == want]
        if typed:
            return typed[0]
    return cands[0]


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
    if not payload or payload == bytes(len(payload)):
        # 0으로 채워진 청크(측위 전 자리 채움·사전 할당 잔여): 데이터가 아니라 '없음'이다. binary로 세면
        # 정상 GPS 스트림이 다수결(80%)에 못 미쳐 통째로 버려졌다(리뷰 #23).
        return "empty", None
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
    if not (FINEVU_RECORD_MIN_LEN <= len(payload) <= FINEVU_RECORD_LEN + 8):
        return None   # 72바이트 레코드다. 긴 청크의 앞부분만 보고 인정하지 않는다(리뷰 #50)
    if not any(payload[:FINEVU_RECORD_LEN]):
        return None   # 0으로 채운 청크는 레코드가 아니다 - 가짜 0g가 생겼다(리뷰 #50)

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
        # 신뢰도는 GPS 쪽 근거(측위 실패·반구 플래그)로만 정한다. G센서 센티넬(99/100)은 센서 결측일 뿐
        # 좌표와 무관한데, 예전엔 경고가 하나라도 있으면 GPS까지 불신해 정상 좌표가 전부 빠졌다(리뷰 #5).
        "trusted": bool(not no_fix and "unexpected_hemisphere_flag" not in parse_warnings),
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


def first_riff_video_duration(stream_table, idx1_entries):
    """OpenDML(AVIX가 이어지는) 파일에서 idx1이 가리키는 첫 RIFF만의 영상 길이. strh dwLength는 전체
    프레임 수라 첫 RIFF idx1의 레코드 수로 나누면 시간축이 늘어났다(리뷰 #51). 못 구하면 None."""
    for s in stream_table:
        if s.fcc_type != b"vids" or not (s.dw_rate and s.dw_scale):
            continue
        fps = s.dw_rate / s.dw_scale
        frames = sum(1 for e in idx1_entries if stream_index_from_chunk_id(e["chunk_id"]) == s.index)
        if fps > 0 and frames:
            return frames / fps, f"첫 RIFF idx1 영상 프레임 {frames}개 / {fps:.3f}fps (OpenDML)"
    return None


def measured_video_duration(stream_table, idx1_entries):
    """색인에 실제로 있는 영상 프레임 수(손상 구간의 빈 자리 포함) ÷ fps. 헤더의 dwLength는 녹화가 중단되거나
    끝이 잘린 파일에서 계획값·전체값이라, 그걸 남은 레코드 수로 나누면 시간축이 늘어나거나 줄어든다
    (리뷰 #10, #11, #61). 정상 파일에서는 dwLength와 같다. 못 구하면 None."""
    for s in stream_table:
        if s.fcc_type != b"vids" or not (s.dw_rate and s.dw_scale):
            continue
        fps = s.dw_rate / s.dw_scale
        frames = sum(1 for e in idx1_entries if stream_index_from_chunk_id(e["chunk_id"]) == s.index)
        if fps > 0 and frames:
            return frames / fps, f"색인 영상 프레임 {frames}개 / {fps:.3f}fps"
    return None


def build_avi_stream_times(video_duration, record_count):
    """레코드 수로 균등 분할한 (start_sec, end_sec) 목록."""
    if not video_duration or record_count <= 0:
        return []
    step = video_duration / record_count
    return [(i * step, (i + 1) * step) for i in range(record_count)]


def supplement_truncated_index(mm, idx1_entries, base_offset, movi_end):
    """끝이 잘린 idx1(파일 끝에서 절단) 뒤에도 movi가 남아 있으면 마지막 색인 청크 다음부터 걸어 색인을
    보충한다(리뷰 #10). 보충 엔트리는 supplemented=True. 반환: (추가 엔트리 수, 손상 구간 수)."""
    last_end = None
    for e in idx1_entries:
        if e.get("idx_offset", -1) < 0:
            continue
        if _entry_target_state(mm, base_offset, e) == "match":
            end = base_offset + e["idx_offset"] + 8 + e["length"]
            if last_end is None or end > last_end:
                last_end = end
    if last_end is None or last_end + 8 > movi_end:
        return 0, 0
    if _chunk_header_ok(mm, last_end, movi_end) is None and bytes(mm[last_end:last_end + 1]) == b"\0":
        last_end += 1
    gaps = []
    tail = {}
    walked = []
    for pos, ck_id, size in walk_movi_chunks(mm, last_end, movi_end, gaps, tail_info=tail):
        if ck_id != b"JUNK":
            walked.append((pos, ck_id, size))
    if not walked:
        return 0, 0
    cut_info = {}
    cut_at = recording_cut_at(mm, walked, sorted(gaps), info=cut_info)
    if cut_at is not None:
        walked = [w for w in walked if w[0] < cut_at]
        gaps = [g for g in gaps if g[0] < cut_at]
    added = 0
    gap_iter = iter(sorted(gaps))
    pending_gap = next(gap_iter, None)
    for pos, ck_id, size in walked:
        while pending_gap is not None and pending_gap[0] < pos:
            idx1_entries.extend(_placeholder_entries(walked, pending_gap, mm=mm))
            pending_gap = next(gap_iter, None)
        idx1_entries.append({"chunk_id": ck_id, "flags": 0, "idx_offset": pos - base_offset, "length": size,
                             "supplemented": True})
        added += 1
    DAMAGE_GAPS.extend(sorted(gaps))
    SYNTH_INFO.update({"supplemented": added, "cut_at": cut_at, "cut_reason": cut_info.get("reason", ""),
                       "boundary_uncertain": bool(cut_info.get("uncertain")),
                       "zero_tail": list(tail["zero_tail"]) if tail.get("zero_tail") else None,
                       "truncated_tail": tail.get("truncated_tail")})
    warn(f"잘린 idx1 뒤의 movi를 걸어 색인 {added}개를 보충함" + (f", 손상 구간 {len(gaps)}곳" if gaps else ""))
    return added, len(gaps)


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
                                  "float_vector": 0, "binary": 0, "empty": 0}
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

        display_label, dir_label, prefix = labels[stream.index]
        seq = seq_counters[stream.index]
        seq_counters[stream.index] += 1
        times = (stream_times or {}).get(stream.index) or []
        start_sec, end_sec = times[seq] if seq < len(times) else (None, None)
        time_source = "avi_video_duration" if start_sec is not None else ""
        start_disp = f"{start_sec:.3f}" if start_sec is not None else ""
        end_disp = f"{end_sec:.3f}" if end_sec is not None else ""

        if e.get("placeholder") or e["idx_offset"] < 0:
            # 색인 재구성 때 손상 구간에 넣은 빈 자리: 실제 청크가 아니므로 검증·파일 쓰기·경고 없이 자리만
            # 센다(시간축 균등 분할용). index.csv에는 DAMAGED_PLACEHOLDER로 따로 적는다(리뷰 #63).
            validation_counts["DAMAGED_PLACEHOLDER"] = validation_counts.get("DAMAGED_PLACEHOLDER", 0) + 1
            index_rows.append({
                "sequence": seq, "stream_index": stream.index, "stream_label": display_label,
                "fcc_type": (stream.fcc_type or b"").decode("ascii", errors="replace"),
                "chunk_id": e["chunk_id"].decode("ascii", errors="replace"),
                "idx1_entry_offset": "", "relative_offset": "", "absolute_chunk_offset": "",
                "payload_offset": "", "idx1_length": "", "chunk_header_length": "", "flags": "",
                "validation": "DAMAGED_PLACEHOLDER", "output_file": "",
            })
            continue

        chunk_offset = base_offset + e["idx_offset"]
        reasons, payload_offset, header_size = validate_chunk(mm, chunk_offset, e)
        status = "|".join(reasons)
        validation_counts[status] = validation_counts.get(status, 0) + 1

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
                            "chunk_offset": chunk_offset,
                            "chunk_id": e["chunk_id"].decode("ascii", errors="replace"),
                            "sentence_type": parsed["sentence_type"],
                            "raw_sentence": parsed["raw"],
                        })
                    elif value:
                        unparsed_by_stream[stream.index].append((seq, value))
                elif kind == "record72":
                    record72_by_stream[stream.index].append((seq, e, value,
                                                              start_disp, end_disp, time_source, chunk_offset))
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
        deltas = finevu_unwrap_elapsed([rec["elapsed_sec"] for _, _, rec, _, _, _, _ in items])
        for (seq, e, rec, start_disp, end_disp, time_source, chunk_offset72), delta in zip(items, deltas):
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
                "chunk_offset": chunk_offset72,
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
        w = _GuardDictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(index_rows)

    stream_csv = os.path.join(out_dir, "stream_table.csv")
    with open(stream_csv, "w", newline="", encoding="utf-8") as f:
        w = _GuardListWriter(csv.writer(f))
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


NMEA_PARSED_MIN_FRACTION = 0.5   # 구조가 맞는 NMEA(또는 FineVu 레코드)가 절반 이상이면 손상 청크가 섞여도 GPS 스트림


def decide_stream_kind(counts, min_fraction=DECODE_MIN_FRACTION):
    """스트림 종류 다수결. 0으로 채워진 청크(empty)는 분모에서 뺀다. 텍스트·레코드 비율이 80%에 못 미쳐도
    실제로 해석된 NMEA 문장·FineVu 레코드가 절반 이상이면 그 종류로 본다 - 손상으로 깨진 청크 몇 개 때문에
    정상 GPS가 'GPS 없음'이 되면 안 된다(리뷰 #23). 임의 바이트열은 NMEA·레코드 검증을 통과하지 못한다."""
    total = sum(v for k, v in counts.items() if k != "empty")
    if total == 0:
        return None
    text_ratio = (counts["nmea_text"] + counts["generic_text"]) / total
    nmea_ratio = counts["nmea_text"] / total
    record72_ratio = counts.get("record72", 0) / total
    float_ratio = counts["float_vector"] / total
    if text_ratio >= min_fraction or nmea_ratio >= NMEA_PARSED_MIN_FRACTION:
        return "text"
    if record72_ratio >= min_fraction or record72_ratio >= NMEA_PARSED_MIN_FRACTION:
        return "record72"
    if float_ratio >= min_fraction:
        return "float_vector"
    return None


def write_avi_timeline(out_dir, selected_streams, labels, extract_result, dry_run=False,
                       decode_summary=None):
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

    def decoded(s):
        # 다수결(decide_stream_kind)에 못 미친 스트림은 coordinates.csv도 없는데 timeline에는 실려
        # 신뢰도 정보 없이 유효 측위가 됐다(리뷰 #49). 판정된 스트림만 쓴다.
        if decode_summary is None:
            return True
        return decode_summary.get(s.index, {}).get("kind") in ("text", "record72")

    gps_rows = []
    for s in selected_streams:
        if not decoded(s):
            continue
        rows = [r for r in coord_by_stream.get(s.index, []) if r.get("start_time_sec")]
        if len(rows) > len(gps_rows):
            gps_rows = rows
    if not gps_rows:
        # 헤더(hdrl)가 사라져 재생 시각을 못 만든 파일: 좌표 행을 시각 없이 남긴다. 앱이 GPS UTC로
        # 시간축을 추정해 쓴다(손상 파일 복구). 예전엔 timeline 자체가 없어 GPS 전멸로 보였다.
        for s in selected_streams:
            if not decoded(s):
                continue
            rows = list(coord_by_stream.get(s.index, []))
            if len(rows) > len(gps_rows):
                gps_rows = rows
        if gps_rows:
            warn(f"재생 시각 없는 좌표 {len(gps_rows)}행을 timeline에 남김(헤더 소실 - UTC로 시간축 추정 필요)")
    if not gps_rows:
        return None

    # 센서는 가장 가까운 재생 시각으로 붙인다. 스트림마다 따로 만든 균등 격자를 '%.3f' 문자열로 완전
    # 일치시키면 레코드 수가 하나만 달라도 G값이 거의 다 빠졌다(리뷰 #54).
    sensor_times = []
    for s in selected_streams:
        for r in sensor_by_stream.get(s.index, []):
            try:
                sensor_times.append((float(r.get("start_time_sec")), r))
            except (TypeError, ValueError):
                continue
    sensor_times.sort(key=lambda x: x[0])
    sensor_keys = [t for t, _ in sensor_times]

    def sensor_near(key, tolerance=1.0):
        try:
            t = float(key)
        except (TypeError, ValueError):
            return {}
        k = bisect.bisect_left(sensor_keys, t)
        best = None
        for j in (k - 1, k):
            if 0 <= j < len(sensor_keys) and abs(sensor_keys[j] - t) <= tolerance:
                if best is None or abs(sensor_keys[j] - t) < abs(sensor_keys[best] - t):
                    best = j
        return sensor_times[best][1] if best is not None else {}

    rows = []
    last_lat = last_lon = last_speed = ""
    for i, r in enumerate(gps_rows, start=1):
        if r.get("latitude"):
            last_lat, last_lon = r["latitude"], r["longitude"]
            last_speed = r.get("speed_kmh", "")
        sen = sensor_near(r.get("start_time_sec"))
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
            "gps_trusted": r.get("trusted", ""),   # status=V·파싱 경고 행을 앱이 유효 측위로 보지 않게(리뷰 #49)
            # UTC가 없는 기기(FineVu 72바이트 레코드)도 측위 상태(A/V)와 레코드 순번(경과 초)을 남긴다 -
            # 앱이 측위 실패 행을 '수신 끊김'으로 구분하고 정차 중 같은 값을 다른 측정으로 세게(리뷰 #6, #7).
            "gps_status": r.get("status", ""),
            "gps_elapsed_sec": r.get("elapsed_delta_sec", ""),
            # 이 기록이 파일 어디에서 왔는지(손상 구간과 시간 대응용).
            "source_offset": r.get("chunk_offset", ""),
            "latitude_last": last_lat,
            "longitude_last": last_lon,
            "speed_kmh_last": last_speed,
            "x_g": sen.get("x", ""), "y_g": sen.get("y", ""), "z_g": sen.get("z", ""),
            "x_g_cal": "", "y_g_cal": "", "z_g_cal": "",
        })

    path = os.path.join(out_dir, "timeline.csv")
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = _GuardDictWriter(f, fieldnames=list(rows[0].keys()))
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
                w = _GuardDictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
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
                w = _GuardDictWriter(f, fieldnames=RECORD72_COORD_FIELDS, extrasaction="ignore")
                w.writeheader()
                w.writerows(coord_rows)

            with open(os.path.join(stream_dir, "coordinates.txt"), "w", encoding="utf-8") as f:
                # 좌표 목록이라 측위 실패(status=V) 행은 뺀다. CSV에는 그대로 남는다.
                for i, row in enumerate(fix_rows, start=1):
                    f.write(f"{i}. {row['latitude']}, {row['longitude']}\n")

            with open(os.path.join(stream_dir, "sensor_values.csv"), "w", newline="",
                       encoding="utf-8") as f:
                w = _GuardDictWriter(f, fieldnames=RECORD72_SENSOR_FIELDS, extrasaction="ignore")
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
                w = _GuardDictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
                w.writeheader()
                w.writerows(sensor_rows)

    decode_detect_csv = os.path.join(out_dir, "decode_detection.csv")
    if not dry_run:
        with open(decode_detect_csv, "w", newline="", encoding="utf-8") as f:
            w = _GuardListWriter(csv.writer(f))
            w.writerow(["stream_index", "nmea_text", "generic_text", "record72",
                        "float_vector", "binary", "decision", "empty"])
            for s in selected_streams:
                c = classify_counts.get(s.index, {})
                info_row = decode_summary[s.index]
                decision = {"text": "TEXT (디코딩됨)",
                            "record72": "RECORD72 (FineVu 72바이트 고정 레코드, 디코딩됨)",
                            "float_vector": "FLOAT_VECTOR (추정, 비공식)"}.get(
                    info_row["kind"], "BINARY/미상 (raw만 보존)")
                w.writerow([s.index, c.get("nmea_text", 0), c.get("generic_text", 0),
                            c.get("record72", 0), c.get("float_vector", 0),
                            c.get("binary", 0), decision, c.get("empty", 0)])

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
    따라가며 센다(문자열 검색 아님). 선언 크기가 0이거나 파일을 넘는 RIFF(미종결·헤더 손상)는
    세지 않고 거기서 멈춘다 - 예전엔 크기 0을 유효한 빈 RIFF로 세서 파일 전체가 트레일링이
    됐다(리뷰 #52)."""
    filesize = len(mm)
    pos = 0
    count = 0
    while pos + 8 <= filesize:
        if bytes(mm[pos:pos + 4]) != b"RIFF":
            break
        size = struct.unpack_from("<I", mm, pos + 4)[0]
        end = pos + 8 + size
        if size < 4 or end > filesize:
            break
        count += 1
        pos = end + (size & 1)
    return count, pos


def current_recording_end(mm):
    """현재 녹화분이 끝나는 위치와 상태. 반환값: (end_pos, status)
      status "ok"        첫 RIFF('AVI ')와 그 뒤에 이어지는 OpenDML AVIX RIFF까지가 현재 녹화
             "invalid"   첫 RIFF 선언 크기가 0이거나 파일을 넘음(미종결·헤더 손상) - 경계를 알 수 없음
    AVIX는 같은 녹화의 연속 구간이라 슬랙이 아니다(리뷰 #11). 뒤에 오는 'AVI ' RIFF나 미상 데이터가
    슬랙이다."""
    filesize = len(mm)
    if filesize < 12 or bytes(mm[0:4]) != b"RIFF":
        return filesize, "invalid"
    size = struct.unpack_from("<I", mm, 4)[0]
    end = 8 + size
    if size < 4 or end > filesize:
        return filesize, "invalid"
    # 선언 크기가 실제 구조보다 작은 경우(기록 중간의 헤더 값이 남은 미종결 파일): idx1이 선언한 끝
    # 너머에 있으면 경계를 믿을 수 없다. 그대로 두면 현재 녹화의 뒷부분이 슬랙으로 중복된다.
    try:
        _hdrl, _movi, idx1, _avix = find_top_level_sections(mm)
        if idx1 is not None:
            idx1_end = idx1.data_start + idx1.ck_size
        else:
            # 현재 녹화의 idx1만 본다. 뒤에 이어붙은 옛 AVI의 idx1을 집으면 선언 크기가 잘못됐다고 판정해
            # 트레일링 슬랙을 통째로 놓쳤다(리뷰 #62).
            fb = find_idx1_fallback(mm, chain_start=first_chunk_chain(mm, 0, end))
            idx1_end = fb[0] + 8 + fb[1] if fb is not None else 0
        if idx1_end > end + 1:
            return filesize, "invalid"
    except Exception:  # noqa: BLE001
        pass
    pos = end + (size & 1)
    while pos + 12 <= filesize and bytes(mm[pos:pos + 4]) == b"RIFF" and bytes(mm[pos + 8:pos + 12]) == b"AVIX":
        size = struct.unpack_from("<I", mm, pos + 4)[0]
        end = pos + 8 + size
        if size < 4 or end > filesize:
            return filesize, "ok"   # 잘린 AVIX: 현재 녹화가 파일 끝까지라고 본다(슬랙 아님)
        pos = end + (size & 1)
    return pos, "ok"


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
        pos, end = first_end, first_end + trailing
        while pos < end:   # 수 GB 꼬리도 통째로 메모리에 올리지 않는다(리뷰 #52)
            f.write(mm[pos:min(pos + 8 * 1024 * 1024, end)])
            pos += 8 * 1024 * 1024
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


def slack_regions_avi(mm, analysis=None):
    """컨테이너(idx1)가 참조하지 않는 영역: [(kind, start, end)].
      embedded      : movi 안에서 옛 녹화 파일 잔재(RIFF)가 처음 나타난 곳부터 movi 끝까지
      appended_riff : 첫 RIFF가 선언한 크기 뒤에 온전한 RIFF(옛 녹화 파일)가 통째로 이어붙은 경우
      trailing      : 첫 RIFF가 끝난 뒤의 미상 꼬리(정렬 패딩 수준은 무시)
    현재 녹화분은 idx1이 가리키는 chunk뿐이라 그 뒤는 전부 예전 기록이 남은 자리다."""
    if analysis is None:
        analysis = analyze_slack(mm)
    if analysis.get("kind") != "riff":
        return []
    regions = []
    embedded = analysis["embedded_riffs"]
    if embedded and analysis["movi_list"]:
        movi = analysis["movi_list"][0]
        start = min(e["pos"] for e in embedded)
        if movi.content_end > start:
            regions.append(("embedded", start, movi.content_end))
    # 현재 녹화분(첫 RIFF + 이어지는 OpenDML AVIX)이 끝난 뒤는 전부 슬랙이다 - 미상 꼬리든, 온전한
    # RIFF(옛 녹화 파일)가 통째로 이어붙어 있든 마찬가지다. 첫 RIFF의 선언 크기가 비정상이면
    # (미종결·헤더 손상) 경계를 모르니 트레일링 판정을 생략한다(리뷰 #11, #52).
    filesize = len(mm)
    end, status = current_recording_end(mm)
    if status == "ok" and end < filesize - TRAILING_IGNORE_THRESHOLD:
        kind = "appended_riff" if bytes(mm[end:end + 4]) == b"RIFF" else "trailing"
        regions.append((kind, end, filesize))
    return regions


def detect_slack_info(path):
    """앱이 영상을 고를 때 묻는 용도: 슬랙이 있는지와 크기. 추출은 하지 않는다."""
    with open(path, "rb") as f:
        mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
        try:
            analysis = analyze_slack(mm)
            regions = slack_regions_avi(mm, analysis)
        finally:
            mm.close()
    total = sum(e - s for _, s, e in regions)
    return {
        "container": "avi",
        "has_slack": bool(regions),
        "slack_bytes": total,
        "regions": [{"kind": k, "start": s, "end": e} for k, s, e in regions],
        "embedded_riffs": len(analysis.get("embedded_riffs", [])) if analysis.get("kind") == "riff" else 0,
        "top_riffs": analysis.get("top_riff_count", 0) if analysis.get("kind") == "riff" else 0,
    }


SLACK_NMEA_RE = re.compile(rb"\$?[A-Z]{2}(?:RMC|GGA),[ -~]*")


def run_slack_carve_avi(mm, out_dir, dry_run=False, extra_regions=None):
    """슬랙 영역에서 NMEA GPS 문장을 카빙해 <out_dir>/slack/ 에 남긴다. 원본은 수정하지 않는다.
    열 구성은 integration_mp4.py 의 슬랙 카빙과 같다(앱이 같은 코드로 읽는다). FineVu 72바이트
    이진 레코드는 텍스트가 아니라 여기서 찾지 못한다(NMEA 기기만 해당).
    extra_regions: 색인 재구성 때 녹화 경계 뒤로 잘라낸 구간 [(kind, start, end)] - 본 궤적과 분리해 남긴다."""
    regions = slack_regions_avi(mm)
    for kind, s, e in (extra_regions or []):
        if e > s and not any(rs <= s and e <= re_ for _k, rs, re_ in regions):
            regions.append((kind, s, e))
    if not regions:
        info("[슬랙] 옛 녹화 잔재도 트레일링 영역도 없음 - 카빙 대상 없음")
        return None
    total = sum(e - s for _, s, e in regions)
    info(f"\n[슬랙] 컨테이너가 참조하지 않는 영역 {len(regions)}개 / {total:,} bytes "
         f"(전체의 {total / len(mm) * 100:.1f}%)")
    coord_rows, region_stats = [], []
    for kind, s, e in regions:
        info(f"  [{kind}] 0x{s:08X} ~ 0x{e:08X}  ({e - s:,} bytes)")
        n_gps = 0
        for m in SLACK_NMEA_RE.finditer(mm, s, e):
            parsed = try_parse_nmea(m.group().decode("ascii", errors="replace"))
            if parsed is None:
                continue
            speed_kmh = parsed.get("speed_kmh")
            coord_rows.append({
                "date": parsed.get("date", ""), "utc_time": parsed.get("utc_time", ""),
                "status": parsed.get("status", ""),
                "latitude": f"{parsed['lat']:.6f}" if parsed.get("lat") is not None else "",
                "longitude": f"{parsed['lon']:.6f}" if parsed.get("lon") is not None else "",
                "speed_knots": parsed.get("speed_knots", ""),
                "speed_kmh": f"{speed_kmh:.3f}" if speed_kmh is not None else "",
                "track_deg": parsed.get("track_deg", ""),
                "checksum_ok": parsed.get("checksum_ok"),
                "status_valid": parsed.get("status_valid", ""),
                "trusted": parsed.get("trusted", ""),
                "parse_warnings": parsed.get("parse_warnings", ""),
                "slack_region": kind,
                "absolute_offset": f"0x{m.start():08X}",
                "sentence_type": parsed.get("sentence_type", ""),
                "raw_sentence": parsed.get("raw", ""),
            })
            n_gps += 1
        region_stats.append({"region_kind": kind, "start": f"0x{s:08X}", "end": f"0x{e:08X}",
                             "size_bytes": e - s, "gps_records": n_gps})
    dates = sorted({r["date"] for r in coord_rows if r["date"]})
    info(f"  -> GPS {len(coord_rows)}건" + (f", 슬랙 GPS 기록일: {dates}" if dates else ""))
    if not dry_run:
        slack_dir = os.path.join(out_dir, "slack")
        os.makedirs(slack_dir, exist_ok=True)
        _write_dict_csv(os.path.join(slack_dir, "slack_regions.csv"), region_stats)
        _write_dict_csv(os.path.join(slack_dir, "slack_coordinates.csv"), coord_rows)
        with open(os.path.join(slack_dir, "README.txt"), "w", encoding="utf-8") as fp:
            fp.write(
                "이 폴더의 내용은 AVI 컨테이너(idx1)가 참조하지 않는 영역 - movi 안에 남은 예전\n"
                "녹화 파일의 잔재(embedded)와 최상위 RIFF 뒤의 꼬리(trailing) - 에서 카빙한\n"
                "NMEA GPS 문장입니다.\n\n"
                "- 현재 녹화분이 아니라 같은 저장매체에 예전에 기록됐던 내용입니다.\n"
                "- after_cut_old_recording: 색인(idx1)이 없어 movi를 걸어 색인을 만들 때 GPS 날짜·시각이\n"
                "  이어지지 않아 옛 녹화 잔재로 판정해 잘라낸 구간입니다.\n"
                "- after_cut_uncertain: 손상 구간 앞뒤에 판정 근거(날짜 있는 A 상태 RMC)가 없어 현재 녹화인지\n"
                "  옛 녹화인지 확인하지 못한 구간입니다. 본 궤적에 넣지 않고 여기 따로 남깁니다.\n"
                "- 재생 시각(영상 시간축)에 매핑할 수 없습니다. 절대 byte offset만 남깁니다.\n"
                "- 해석/검증은 정상 추출분과 같습니다(checksum 포함). checksum_ok=False 인 행은\n"
                "  우연히 만들어진 바이트열일 수 있습니다.\n"
                "- 원본 파일은 수정하지 않았습니다.\n")
    return {"regions": len(regions), "slack_bytes": total, "gps": len(coord_rows), "dates": dates}


def _write_dict_csv(path, rows):
    if not rows:
        return
    fieldnames = []
    for row in rows:
        for k in row.keys():
            if k not in fieldnames:
                fieldnames.append(k)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = _GuardDictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)


def handle_slack(input_path, mm, out_dir, dry_run=False, repair=False):
    """반환값: (사용할 파일 경로, 새 파일을 만들었는지 여부).
    repair=True 일 때만 out_dir 안에 슬랙 제거본(<파일명>_wo_slack.avi)을 만들어 그걸로
    추출한다. 기본(repair=False)은 슬랙을 **자르지 않고 무시**한다 - idx1은 현재 녹화분만
    가리키므로 슬랙 유무는 GPS 추출 결과에 영향을 주지 않고, 원본 외 파생 영상을 만들지
    않는 편이 증거 취급에 낫다. 슬랙 안의 옛 GPS는 --slack 으로 따로 카빙한다.
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

    if top_count == 0:
        info("[슬랙 판단] 첫 RIFF의 선언 크기가 비정상(0이거나 파일을 넘음) - 미종결·헤더 손상으로 보고 "
             "트레일링 판정을 생략함")
        extra_after = 0
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

    if not repair:
        info(f"[슬랙 판단] RIFF {top_count + len(embedded)}개 감지"
             f"(최상위 {top_count}개 + movi 내부 예전 파일 잔재 {len(embedded)}개) - "
             "자르지 않고 원본 그대로 추출함(--repair-slack 을 주면 절단본을 만든다). "
             "idx1은 현재 녹화분만 가리키므로 추출 결과는 같다")
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
_USED_OUT_DIRS = set()


def unique_out_dir(output_root, input_path):
    """<output_root>/<stem>. Windows는 마지막 경로 요소의 끝 공백·점을 지워 'REC_F .avi'의 폴더가 어긋나므로
    걷어내고(리뷰 #65), 한 번에 처리하는 입력의 stem이 같으면 _2, _3을 붙인다(리뷰 #124)."""
    stem = os.path.splitext(os.path.basename(input_path))[0].rstrip(" .") or "input"
    base = os.path.join(output_root, stem)
    out_dir, n = base, 1
    while os.path.normcase(out_dir) in _USED_OUT_DIRS:
        n += 1
        out_dir = f"{base}_{n}"
    _USED_OUT_DIRS.add(os.path.normcase(out_dir))
    return out_dir


def process_single_file(input_path, output_root, args):
    WARNINGS.clear()
    assert_riff_file(input_path)

    filesize = os.path.getsize(input_path)
    if filesize == 0:
        warn(f"{input_path}: 입력 파일 크기가 0입니다 - 건너뜀")
        return

    out_dir = unique_out_dir(output_root, input_path)
    if not args.dry_run:
        os.makedirs(out_dir, exist_ok=True)

    select_mode = args.select_mode or SELECT_MODE
    select_fcctypes = {v.encode("ascii") for v in args.fcctype} if args.fcctype else SELECT_FCCTYPES
    select_indices = set(args.index) if args.index else SELECT_INDICES
    select_chunk_ids = {v.encode("ascii") for v in args.chunk_id} if args.chunk_id else SELECT_CHUNK_IDS

    work_path = None
    f0 = open(input_path, "rb")
    mm0 = mmap.mmap(f0.fileno(), 0, access=mmap.ACCESS_READ)
    try:
        process_path, is_new_file = handle_slack(input_path, mm0, out_dir,
                                                  dry_run=args.dry_run,
                                                  repair=getattr(args, "repair_slack", False))
        if is_new_file:
            work_path = process_path
        active_path = work_path or input_path
        with open(active_path, "rb") as f:
            mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
            try:
                _process_opened(input_path, active_path, work_path, mm, mm0, out_dir, args,
                                select_mode, select_fcctypes, select_indices, select_chunk_ids)
            finally:
                mm.close()
    finally:
        mm0.close()
        f0.close()


def _process_opened(input_path, active_path, work_path, mm, mm0, out_dir, args,
                    select_mode, select_fcctypes, select_indices, select_chunk_ids):
    hdrl, movi_list, idx1_chunk, avix_count = find_top_level_sections(mm)

    movi_fourcc_pos = movi_list[0].data_start if movi_list else None
    if movi_fourcc_pos is None:
        movi_fourcc_pos = find_movi_fallback(mm)
        if movi_fourcc_pos is None:
            warn("movi 를 전혀 찾지 못함")

    DAMAGE_GAPS.clear()
    SYNTH_INFO.clear()
    synthesized_index = False
    idx1_truncated = False
    if idx1_chunk is not None:
        idx1_start = idx1_chunk.data_start
        idx1_size = idx1_chunk.ck_size
        idx1_truncated = bool(idx1_chunk.truncated) or idx1_start + idx1_size > len(mm)
    else:
        fb = find_idx1_fallback(mm, chain_start=first_chunk_chain(mm))
        if fb is None:
            if movi_fourcc_pos is None:
                print(f"{input_path}: idx1도 movi도 찾지 못했습니다. 이 파일은 처리할 수 없습니다.", file=sys.stderr)
                return
            # 녹화 중단으로 idx1이 통째로 잘린 파일: movi의 청크 머리를 걸어 색인을 다시 만든다.
            idx1_start, idx1_size = -1, 0
            synthesized_index = True
        else:
            idx1_start, idx1_size = fb
            idx1_start += 8
            declared = struct.unpack_from("<I", mm, idx1_start - 4)[0]
            idx1_truncated = idx1_start + declared > len(mm)

    if synthesized_index:
        movi_end = movi_list[0].data_end if movi_list else len(mm)
        idx1_entries = synthesize_idx1(mm, movi_fourcc_pos, movi_end)
    else:
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

    if idx1_truncated and not synthesized_index and movi_fourcc_pos is not None:
        # 끝이 잘린 idx1: 남은 movi를 걸어 색인을 보충한다(리뷰 #10). 잘린 idx1 바깥의 GPS를 버리지 않는다.
        movi_end = min(movi_list[0].data_end, idx1_start - 8) if movi_list else idx1_start - 8
        supplement_truncated_index(mm, idx1_entries, base_offset, movi_end)

    # 재생 시간축: 텍스트 스트림 strh가 깨져 있는 기기가 많아 영상 스트림 길이를
    # 레코드 수로 나눠 균등 간격을 만든다(자세한 근거는 compute_video_duration 참고).
    video_duration, duration_source = compute_video_duration(mm, hdrl, stream_table)
    header_duration = video_duration
    measured = measured_video_duration(stream_table, idx1_entries)
    if measured is not None and (video_duration is None or abs(measured[0] - video_duration) > 0.5):
        # 색인의 실제 영상 프레임 수 ÷ fps. 헤더 길이(계획값·전체값)를 남은 레코드 수로 나누면 녹화 중단·
        # 절단·AVIX 파일에서 시간축이 늘거나 줄어 실측처럼 보인다(리뷰 #10, #11, #61).
        if video_duration is not None:
            warn(f"[시간축] 헤더 길이 {video_duration:.3f}초({duration_source})와 색인 영상 프레임 기준 "
                 f"{measured[0]:.3f}초가 다름 - 색인 기준(실제 남은 프레임)을 사용")
        video_duration, duration_source = measured
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
                                     result, dry_run=args.dry_run, decode_summary=decode_summary)
    if n_timeline:
        info(f"[시간축] timeline.csv {n_timeline}행 생성")

    info("\n" + "=" * 60)
    info(f"[요약] {input_path}")
    info(f"슬랙 리페어 적용 여부  : {'예 (' + work_path + ')' if work_path else '아니오'}")
    info(f"movi offset            : 0x{movi_fourcc_pos:X} "
         f"(발견된 movi 개수: {opendml_info['movi_count']})" if movi_fourcc_pos is not None
         else "movi offset            : 없음")
    info(f"idx1 offset             : {'(없음 - movi 걷기로 재구성)' if synthesized_index else f'0x{idx1_start:X}'}")
    if DAMAGE_GAPS:
        info("손상(건너뛴) 구간        : " + ", ".join(f"0x{a:X}-0x{b:X}" for a, b in DAMAGE_GAPS[:20]))
    if SYNTH_INFO.get("cut_at") is not None:
        info(f"녹화 경계               : 0x{SYNTH_INFO['cut_at']:X}"
             f"{' (경계 불확실 - 뒤는 슬랙으로 분리)' if SYNTH_INFO.get('boundary_uncertain') else ' (옛 녹화 잔재 제외)'}")
    if DAMAGE_GAPS or synthesized_index or idx1_truncated or SYNTH_INFO:
        # 앱(core/damage.py 등)이 읽는 한 줄 JSON. 색인 재구성·보충의 판정 근거와 실측 길이를 같이 넘긴다.
        print("DAMAGE_JSON " + json.dumps({
            "gaps": DAMAGE_GAPS[:1000], "synthesized_index": synthesized_index,
            "idx1_truncated": idx1_truncated,
            "supplemented_entries": SYNTH_INFO.get("supplemented", 0),
            "placeholders": SYNTH_INFO.get("placeholders", 0),
            "cut_at": SYNTH_INFO.get("cut_at"), "cut_reason": SYNTH_INFO.get("cut_reason", ""),
            "boundary_uncertain": bool(SYNTH_INFO.get("boundary_uncertain")),
            "zero_tail": SYNTH_INFO.get("zero_tail"), "truncated_tail": SYNTH_INFO.get("truncated_tail"),
            "last_chunk_end": SYNTH_INFO.get("last_chunk_end"),
            "measured_duration_sec": video_duration, "header_duration_sec": header_duration,
        }, ensure_ascii=False))
    if getattr(args, "slack", False):
        # 슬랙 카빙은 원본(mm0)에서. 녹화 경계 뒤로 잘라낸 구간(옛 녹화 잔재·경계 불확실)도 슬랙으로 넘겨
        # 본 궤적과 분리해 남긴다.
        extra = []
        if SYNTH_INFO.get("cut_at") is not None and active_path == input_path:
            cut_end = movi_list[0].data_end if movi_list else len(mm)
            if cut_end > SYNTH_INFO["cut_at"]:
                kind = "after_cut_uncertain" if SYNTH_INFO.get("boundary_uncertain") else "after_cut_old_recording"
                extra.append((kind, SYNTH_INFO["cut_at"], cut_end))
        try:
            run_slack_carve_avi(mm0, out_dir, dry_run=args.dry_run, extra_regions=extra)
        except Exception as exc:
            warn(f"슬랙 카빙 중 예외 - 정상 추출 결과에는 영향 없음: {exc}")
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
    p.add_argument("--repair-slack", action="store_true",
                    help="movi 안 옛 녹화 잔재/중복 RIFF가 있으면 <파일명>_wo_slack.avi 로 잘라낸 "
                         "뒤 추출한다. 기본은 자르지 않고 무시(추출 결과는 같다)")
    p.add_argument("--slack", action="store_true",
                    help="슬랙(옛 녹화 잔재·트레일링 영역)에서 NMEA GPS 문장을 추가로 카빙해 "
                         "<out>/slack/slack_coordinates.csv 로 남긴다(기본 안 함)")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    _USED_OUT_DIRS.clear()
    if not args.dry_run:
        os.makedirs(args.output, exist_ok=True)

    # 파일 하나가 죽어도 나머지는 계속 처리한다(integration_mp4.py와 같은 방식).
    # 앞쪽 손상 파일 하나 때문에 배치 전체가 안 돌아가는 걸 막기 위함.
    results = []
    for input_path in args.inputs:
        name = os.path.basename(input_path)
        if not os.path.isfile(input_path):
            info(f"[SKIP] {input_path}: 파일을 찾을 수 없음")
            results.append((name, "파일을 찾을 수 없음"))
            continue
        try:
            process_single_file(input_path, args.output, args)
            results.append((name, None))
        except SystemExit as exc:
            # assert_riff_file 등이 sys.exit()으로 빠져나온다. 여기서 잡아야
            # 배치의 나머지 파일이 같이 죽지 않는다.
            results.append((name, f"처리 중단 (종료 코드 {exc.code})"))
            warn(f"{name}: 처리 중단 (종료 코드 {exc.code})")
        except Exception as exc:
            results.append((name, f"예외: {exc}"))
            warn(f"{name}: 예외 발생으로 처리 중단: {exc}")

    if len(results) > 1:
        info("\n" + "=" * 70)
        info("[전체 요약]")
        for name, err in results:
            info(f"  {'OK  ' if err is None else 'FAIL'} {name:44}"
                 f"{'' if err is None else '  ' + err}")
        info("=" * 70)

    if results and all(err is not None for _, err in results):
        sys.exit(1)


if __name__ == "__main__":
    main()
