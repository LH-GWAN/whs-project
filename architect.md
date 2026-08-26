# architect.md

블랙박스 영상(AVI/MP4)에 박혀 있는 GPS/센서 메타데이터를 원본 그대로 카빙(carving)하고,
알아볼 수 있는 패턴이면 자동으로 디코딩까지 하는 스크립트 4개에 대한 구조 설명.
언제 뭘 쓰는지는 README.md 참고, 여기는 "내부적으로 어떻게 정보를 찾고 파싱하는지"에 집중.

## 파일 구성과 관계

```
GPS_metadata_avi.py            ← 핵심 엔진 (AVI/RIFF 저수준 파서 + 자동 디코딩)
GPS_metadata_GPRMC.py          ← GPS_metadata_avi.py를 import해서 재사용 (같은 폴더 필수)
GPS_metadata_mp4_pvc1_Atext.py ← 독립 스크립트 (MP4/ISO BMFF, RIFF 코드 재사용 없음)
AVI_exception_lot_RIFF.py      ← 위 세 개와 무관한 전처리용 유틸 (파싱 전 파일 복구)
```

- AVI 두 스크립트(`GPS_metadata_avi.py`, `GPS_metadata_GPRMC.py`)는 같은 RIFF 파서를 쓴다.
  `GPS_metadata_GPRMC.py`는 자체 파서가 없고 `import GPS_metadata_avi as carve`로 저수준
  함수(`validate_chunk`, `find_top_level_sections`, `parse_idx1` 등)를 그대로 갖다 씀.
- MP4 스크립트는 컨테이너 포맷 자체가 달라서(RIFF/idx1 vs ISO BMFF box 트리) 코드 공유 없이
  완전히 별도로 구현됨.
- 세 파싱 스크립트(avi, GPRMC, mp4) 모두 NMEA 파싱 로직(`parse_rmc`, `parse_gga`,
  `_dm_to_decimal`, `nmea_checksum_ok` 등)은 동일한 코드를 각자 파일에 중복 보유하고 있음
  (import로 묶지 않고 파일마다 복사됨 — 셋 중 하나만 고치면 나머지 둘은 안 바뀌니 주의).

---

## GPS_metadata_avi.py — AVI 핵심 엔진

### 정보를 찾는 방법 (구조 파싱)

AVI는 RIFF 컨테이너: `RIFF` → `LIST hdrl`(스트림 정의) / `LIST movi`(실데이터) / `idx1`(인덱스).

1. **`iter_chunks`**: RIFF 청크를 `[ID(4) | size(4) | payload]` 형식으로 순회하는 제네레이터.
   자식 청크의 선언 크기가 부모 경계를 넘으면 경고 후 그 레벨 순회를 중단(파일이 잘렸거나
   깨진 경우 무한루프/OOB 방지).
2. **`find_top_level_sections`**: 최상위 `RIFF` 청크들을 훑어서 `hdrl`, `movi`(여러 개면 첫
   RIFF의 것만), `idx1`을 찾음. `RIFF` formType이 `AVIX`면(OpenDML 확장) 개수만 세고 내용은
   처리 안 함. movi가 한 RIFF 안에 두 번 나오면(실데이터 안에서 우연히 "movi" 바이트열이
   매치된 것으로 보고) 두 번째는 무시.
   - 구조 파싱이 실패하면(RIFF 헤더가 아예 없거나 등) `find_movi_fallback`/
     `find_idx1_fallback`으로 바이트 스캔(`data.find(b'movi')`, `rfind(b'idx1')`)까지 내려감.
3. **`parse_hdrl`**: `hdrl` 안의 `avih`(dwStreams = 스트림 총 개수)와 `strl` LIST들을 순회.
   각 `strl` 안 `strh`(fccType/fccHandler), `strn`(스트림 이름 문자열), `indx`(OpenDML
   super-index 존재 여부)를 읽어 `StreamInfo` 리스트로 만듦.
4. **`parse_idx1`**: `idx1`을 16바이트 단위(`chunk_id, flags, offset, length`)로 파싱해서
   엔트리 리스트로 만듦. 이게 "movi 안 어디에 어떤 청크가 있는지"의 유일한 지도.
5. **`stream_index_from_chunk_id`**: idx1 엔트리의 chunk_id 앞 2글자(`"02"` 등, 16진수)로
   스트림 번호를 역산. AVI 표준상 chunk_id는 `<stream#2hex><type2char>`(예: `02st`=stream 2,
   subtype text) 형식이라 이걸로 idx1 엔트리를 스트림별로 묶음.
6. **`detect_base_offset`**: idx1의 offset이 절대 오프셋인지, movi FourCC 위치 기준인지,
   movi 데이터 시작 기준인지 장비마다 다름. 세 후보(A: movi FourCC 위치, B: A+4, C: 0)에
   대해 idx1 앞 8개 엔트리를 실제로 그 위치에서 읽어 chunk_id가 일치하는지 점수를 매겨
   가장 잘 맞는 후보를 채택(`validate_chunk`가 동일 로직으로 실제 추출 시에도 검증).
   점수가 전부 0이면 "불확실" 표시하고 기본값(A)으로 진행.

### 어떻게 파싱하는지 (선택 → 추출 → 분류 → 디코딩)

1. **스트림 선택** (`resolve_targets`): `SELECT_MODE`(기본 `auto_non_av`)에 따라 대상
   스트림 결정. `auto_non_av`는 `vids`/`auds`(표준 영상/오디오)를 제외한 모든 스트림을
   자동 선택 — GPS/센서처럼 이름 모를 스트림도 다 걸러짐. `--select-mode`로
   `by_fcctype`/`by_index`/`explicit` 오버라이드 가능.
2. **청크 추출** (`extract_payload`): 선택된 스트림에 속하는 idx1 엔트리를 원래 파일
   순서(idx1 순서) 그대로 훑으면서, `base_offset + idx_offset`에서 실제 청크를 읽음.
   `validate_chunk`가 매 엔트리마다 ID 일치/크기 일치/파일 범위 안에 있는지 검증하고
   `OK`/`ID_MISMATCH`/`SIZE_MISMATCH`/`OUT_OF_RANGE` 태그를 붙임 — 문제 있어도 멈추지
   않고 계속 진행, 결과는 `index.csv`에 전부 기록됨(raw carving은 "일단 다 뽑고 검증
   결과는 로그로 남긴다"는 원칙).
   - payload는 청크 헤더 8바이트를 뺀 나머지 그대로 저장(`chunks/*.bin`) + 스트림 전체를
     이어붙인 `{prefix}_concat.bin`도 별도 생성. **raw는 무조건 보존**.
3. **자동 분류** (`classify_payload`): 각 payload를 4가지로 판정:
   - `nmea_text`: payload 안 어디에든 `EMBEDDED_NMEA_RE`(정규식 `\$?[A-Z]{2}(?:RMC|GGA)...`)
     로 NMEA 문장이 섞여 있으면 매치.
   - `generic_text`: payload가 "0바이트부터 인쇄 가능 ASCII만 있다가 그 뒤는 전부
     0x00 패딩"인 고정폭 텍스트 레코드 패턴(`looks_like_text_record`).
   - `float_vector`: 길이가 4의 배수이고 요소 개수가 2~8개, little-endian float32로
     풀었을 때 전부 유한하고 절댓값 50 이하면 후보로 인정(`try_float_vector`) — GPS·자세
     센서가 흔히 이 범위를 쓴다는 관찰적 추정, 공식 스펙 아님.
   - 위 셋 다 아니면 `binary`.
4. **스트림 단위 최종 판정** (`decide_stream_kind`): 스트림 안 청크들의 분류 비율을
   집계해서 `nmea_text+generic_text` 비율이 80%(`DECODE_MIN_FRACTION`) 이상이면 그
   스트림 전체를 "text"로, `float_vector` 비율이 80% 이상이면 "float_vector"로 확정.
   기준 미달이면 디코딩 안 하고 raw만 남김(binary 취급) — 일부만 맞는 걸로 전체를
   오디코딩하지 않기 위한 안전장치.
5. **디코딩 산출물** (`write_decoded_outputs`):
   - text 스트림: 텍스트 라인마다 `try_parse_nmea`로 GPRMC/GPGGA 파싱 시도.
     성공하면 `coordinates.csv`/`coordinates.txt`, 실패(NMEA 형식 아닌 나머지 텍스트)는
     `unparsed_lines.txt`.
   - float_vector 스트림: 3개 값이면 x/y/z로, 아니면 `value_0..N`으로 `sensor_values.csv`.
   - 스트림별 최종 판정 근거는 `decode_detection.csv`에 남김(각 청크가 nmea_text/
     generic_text/float_vector/binary로 몇 개씩 나왔는지 + 최종 decision).

### NMEA 파싱 규칙 (`try_parse_nmea` 계열, 세 스크립트 공통)

- `$GPRMC,...*hh` 형식에서 talker(GP 등) + sentence type(RMC/GGA)만 지원.
- 위경도는 `_dm_to_decimal`로 NMEA degree-minute(`ddmm.mmmm`) → 십진도 변환,
  남/서(S/W)면 부호를 음수로 뒤집어서 **부호 있는 십진도로 통일**(N/E=+, S/W=-).
  즉 N/S, E/W 문자는 결과에 안 남기고 부호로만 표현 — 좌표를 그대로 지도에 찍으면 됨.
- `nmea_checksum_ok`: `*` 뒤 2자리를 문장 본문 XOR 체크섬과 비교해 검증 결과를
  `checksum_ok` 컬럼에 별도로 남김(파싱 성공 여부와는 무관하게 참고용).
- RMC의 속도는 knots 원본값 + km/h 환산값(`*1.852`)을 같이 저장.

### 출력 폴더 구조

```
<출력폴더>/
├── <STREAM_LABEL>/              ← fccHandler/strn 이름 기반 자동 라벨 (충돌 시 _S<idx> 접미사)
│   ├── chunks/*.bin              raw, 청크 헤더 제외한 payload 그대로
│   ├── <prefix>_concat.bin       스트림 전체를 idx1 순서로 이어붙인 raw
│   ├── coordinates.csv/.txt      text 스트림 && NMEA 인식된 것만
│   ├── sensor_values.csv         float_vector 스트림만 (⚠ 비공식 추정)
│   └── unparsed_lines.txt        text로는 인식됐지만 NMEA가 아닌 나머지 줄
├── stream_table.csv              스트림별 fccType/handler/이름/역할 요약
├── index.csv                     청크 1개당 1행: offset/size/validation 결과
├── decode_detection.csv          스트림별 분류 카운트 + 최종 decision
└── warnings.log                  파싱 중 발생한 모든 WARN 메시지
```

---

## GPS_metadata_GPRMC.py — 이름 없는 텍스트 스트림 전용

`strl`에 스트림 이름이 아예 없어서(그냥 `txts` fccType) `GPS_metadata_avi.py`의 자동
라벨링으로는 어떤 스트림이 GPS인지 알 수 없을 때 씀. 저수준 RIFF/idx1 파싱은
`GPS_metadata_avi.py`를 **import**해서 그대로 재사용하고, 이 파일은 그 위에
"이 스트림이 텍스트인지부터 먼저 확인"하는 판정 레이어만 추가로 얹은 구조.

### GPS_metadata_avi.py와의 차이 (동작 방식)

1. `carve.STANDARD_AV_FCCTYPES`(`vids`/`auds`)가 아닌 스트림을 전부 후보로 놓는 건 동일.
2. **`sniff_stream_is_text`**: 각 후보 스트림에서 idx1 엔트리 앞 8개(`--sample-size`)만
   먼저 읽어서 `looks_like_text_record`(0바이트부터 인쇄 가능 ASCII, 그 뒤는 전부
   0x00 패딩)에 몇 개나 맞는지 비율을 봄. 80%(`min_fraction`) 이상 맞으면 그 스트림
   전체를 "텍스트"로 확정하고, 그 아래면 스트림 전체를 스킵(raw carving은
   `GPS_metadata_avi.py`로 따로 하라고 안내만 하고 이 스크립트는 안 건드림).
   - `GPS_metadata_avi.py`처럼 청크 단위 4-way 분류(text/float_vector/binary)를 하는
     게 아니라, **스트림 단위로 텍스트냐 아니냐만 먼저 이진 판정**하는 게 핵심 차이.
3. 텍스트로 확정된 스트림만 전체 엔트리를 순회(`process_stream`)하면서 각 줄에
   `try_parse_nmea` 적용 — 이후 NMEA 파싱/좌표 변환 로직은 `GPS_metadata_avi.py`와
   완전히 동일한 코드(파일 안에 복사돼 있음).

### 출력 폴더 구조

```
<출력폴더>/                      ← <파일명> 서브폴더 자동 생성 안 함(자체 관리)
├── stream_table.csv             스트림 목록 (vids/txts 등)
├── text_detection.csv           스트림별 텍스트 판정 여부/샘플 근거
├── warnings.log
└── TXTS/                        텍스트로 판정된 스트림별 폴더
    ├── coordinates.txt/.csv
    ├── unparsed_lines.txt
    ├── raw_chunks/*.bin
    └── raw_concat.bin
```

---

## GPS_metadata_mp4_pvc1_Atext.py — MP4(INAVI 등) 전용

AVI와 컨테이너가 완전히 다름(ISO BMFF: `moov` → `trak` → `mdia` → `hdlr`/`minf` → `stbl`).
문자열 검색을 쓰지 않고 **box size 필드만으로 트리를 직접 순회**하는 게 핵심 — `mdat`
(실데이터) 안 임의 바이너리에 우연히 `moov` 같은 4바이트가 섞여 있을 수 있어서 바이트
스캔 fallback을 두지 않음(AVI 스크립트들과 반대되는 설계 선택).

### 정보를 찾는 방법 (box 트리 순회)

1. **`iter_boxes`**: `[size(4, big-endian) | type(4) | payload]` 형식으로 box를 순회.
   `size==1`이면 64bit extended size(`largesize`, 8바이트 추가)를, `size==0`이면
   "이 box가 부모 끝까지"로 해석 — MP4 spec 그대로.
2. 최상위에서 `ftyp`(브랜드 정보, 참고용)와 `moov`(전체 메타데이터 루트)를 찾음.
   `moov` 안 `trak`마다 `parse_track` 호출.
3. **`parse_track`**: `trak` → `mdia` → `minf`/`hdlr` 순으로 내려가서 `hdlr`의
   `handler_type`(4바이트, `vide`/`soun`/`text` 등)을 확인. **`text`가 아니면 그 자리에서
   중단**(vide/soun 트랙은 stbl까지 안 내려감 — 원칙대로 다른 트랙 payload는 안 건드림).
4. `text` 트랙만 `minf` → `stbl`까지 내려가서 `stsd`(샘플 타입 정의)/`stsc`(청크당
   샘플 개수 규칙)/`stsz`(샘플별 크기)/`stco` 또는 `co64`(청크 절대 offset, 64bit면
   co64) 네 박스를 파싱.
5. **`compute_sample_positions`**: `stsc`의 "이 청크 번호부터는 청크당 샘플 N개" 규칙을
   순서대로 적용하면서, 청크 offset(`stco`/`co64`)에서 시작해 `stsz`의 샘플 크기만큼
   순차적으로 오프셋을 누적 — 이렇게 각 Sample의 절대 offset/size를 계산.
   `stsc` 규칙과 `stsz` 샘플 개수가 안 맞으면(장비 파일이 깨졌거나) 경고를 남기고
   거기까지만 계산.

### 어떻게 파싱하는지

1. **`decode_sample_text`**: Sample 맨 앞 2바이트를 QuickTime text sample 관례대로
   "빅엔디안 길이 프리픽스"로 먼저 시도 — `declared_len + 2 == 전체 Sample 크기`가
   실제로 맞아떨어질 때만 그 프리픽스를 신뢰하고 나머지를 텍스트로 디코딩.
   안 맞으면 프리픽스 없는 걸로 보고 그냥 trailing NUL을 제거한 뒤 출력 가능 문자
   범위인지만 확인해서 fallback 디코딩(`OK_NO_LENGTH_PREFIX`로 표시) — 검증 안 된
   가정은 항상 실측값과 대조 후에만 채택하는 패턴.
2. **`split_segments`**: 디코딩된 텍스트를 세미콜론(`;`)으로 분리 —
   `gsensor...;GPRMC...;CAR...` 식으로 한 Sample 안에 여러 종류 레코드가 같이 들어있음.
3. **`classify_segment`**: 세그먼트별로
   - `gsensor` 접두어(정규식 `^gsensor(subtype)?\s*,\s*(rest)`)면 필드를 그대로
     `field_0, field_1, ...`로 보존(각 필드가 뭘 의미하는지는 공식 스펙 없음, 원본값만).
   - `try_parse_nmea`(AVI 스크립트와 동일 로직, 파일 내 복사본)로 GPRMC/GPGGA 매치되면
     GPS 좌표로.
   - 둘 다 아니면 `generic`(라벨=첫 필드, 나머지는 `field_N` + `raw` 원문 보존, 의미
     단정 안 함).
4. `scan_keywords`: 위 분류와 별개로, Sample 텍스트 안에 `gps`/`NMEA`/`latitude` 등
   키워드가 있었다는 사실만 `keyword_hits.csv`에 표시(해석이 아니라 후보 힌트용).

### 출력 폴더 구조

```
<출력폴더>/
├── track_table.csv                     전체 Track: handler/이름/stsd타입/샘플수
├── warnings.log
└── TRACK<N>_TEXT/                      handler_type=='text'인 Track마다 (N은 전체 순번)
    ├── index.csv                        Sample별 chunk/offset/size/validation
    ├── coordinates.csv/.txt             GPRMC/GPGGA 인식된 것만
    ├── sensor_values.csv                gsensor 세그먼트 원본 필드 그대로 (⚠ 비공식)
    ├── other_segments_unparsed.csv      gsensor도 GPS도 아닌 나머지 (CAR 등)
    ├── keyword_hits.csv                 키워드 후보 표시(해석 아님)
    └── chunks/*.bin                     --extract 줬을 때만, Sample 원본 그대로
```

CLI 옵션: `--list-tracks`(목록만), `--dry-run`(파일 미생성), `--extract`(Sample .bin 저장),
`--track N`(특정 text Track 번호만 처리).

---

## AVI_exception_lot_RIFF.py — 파싱 전 전처리(복구) 유틸

위 세 파싱 스크립트와 달리 **메타데이터 파싱이 아니라 손상된 AVI 파일 자체를 복구**하는
스크립트. `REC_*.avi` 패턴의 파일을 훑어 슬랙(slack) 데이터를 잘라내고 재생 가능한
새 AVI로 저장. 블랙박스가 파일을 이어쓰기(순환 녹화)하면서 이전 데이터 잔재나 여러
RIFF 헤더가 한 파일에 겹쳐 남는 경우, `idx1`이 여러 개 나와서 어느 게 진짜인지
구분이 안 되는 문제를 해결하기 위한 것.

### 동작 방식

1. `movi` LIST 청크를 처음부터 순회하며 찾음(**크기 필드는 신뢰하지 않고** 실제
   `LIST`+`movi` 매치로 위치를 확정).
2. 파일 전체에서 `idx1` 문자열을 모두 찾아 후보 offset 리스트 작성(`data.find` 반복).
3. **뒤에서부터**(가장 최근에 append된 것부터) 각 idx1 후보를 검사:
   - idx1의 첫 엔트리 offset이 절대offset/movi+8/movi+12 세 기준 중 어디서
     chunk_id와 실제로 일치하는지로 `base_offset`을 추정(`GPS_metadata_avi.py`의
     `detect_base_offset`과 같은 발상, 독립 구현).
   - base가 잡히면 **마지막 엔트리**의 위치까지 계산해서 실제로 그 자리에 해당
     chunk_id가 있는지 최종 검증 — 여기 통과하는 첫 idx1이 "진짜" 인덱스.
4. 진짜 인덱스의 마지막 프레임이 끝나는 지점(`actual_movi_end`)을 실제 영상의 끝으로
   보고, 그 뒤에 붙은 건 전부 슬랙 데이터로 간주해 잘라냄.
5. 잘라낸 뒤 `movi` 청크 크기, RIFF 전체 크기를 새 길이에 맞게 재계산해서 헤더에
   써넣고, 검증된 idx1 하나만 파일 끝에 다시 붙임.
6. `avih`의 프레임 수(`dwTotalFrames`)도 실제로 남은 비디오 프레임(`00dc`/`01dc`/
   `00db`/`01db` chunk_id) 개수로 다시 세서 덮어씀 — 프레임 수 불일치로 인한 재생
   오류 방지.
7. 결과를 `Recovered_<원본파일명>`으로 저장. 실행하면 현재 폴더의 `REC_*.avi`를 모두
   찾아 `./Recovered_2/`에 일괄 처리(`process_all_samples`, 이미 `Recovered_` 접두어인
   파일은 재처리 스킵).

이 스크립트로 복구한 파일을 이후 `GPS_metadata_avi.py`/`GPS_metadata_GPRMC.py`
입력으로 넘기면 idx1 중복 문제 없이 정상 파싱됨.

---

## 공통 설계 원칙

- **raw는 항상 보존**한다 — 디코딩 결과가 의심스러우면 `chunks/*.bin`,
  `*_concat.bin`(AVI), `chunks/*.bin`(MP4, `--extract` 시)으로 원본 대조 가능.
- **파싱 실패는 프로그램을 죽이지 않고 로그로 남긴다** — `WARNINGS` 리스트에 쌓아서
  `warnings.log`로 출력, 문제 있는 엔트리/청크는 건너뛰고 나머지는 계속 처리.
- **NMEA(GPRMC/GPGGA)는 공개 표준 그대로**라 세 스크립트 모두 안전하게 신뢰 가능.
- **float32 벡터(SENS/sensor_values)와 MP4 gsensor 필드는 공식 스펙이 아닌 관찰 기반
  추정** — 값 범위(±50 이내)나 필드 개수로 판단한 것이라 다른 장비 파일에서 같은
  패턴이 나와도 곧이곧대로 믿지 말 것.
- **오프셋 기준점(base offset)은 장비마다 다를 수 있어 항상 실측 검증 후 채택** —
  AVI 두 스크립트, `AVI_exception_lot_RIFF.py` 모두 후보 여러 개를 두고 idx1 엔트리와
  실제 바이트를 대조해서 점수가 가장 높은 후보를 선택하는 동일한 패턴을 씀.
