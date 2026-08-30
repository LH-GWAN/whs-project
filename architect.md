# architect.md

블랙박스 영상(AVI/MP4)에 박혀 있는 GPS/센서 메타데이터를 원본 그대로 카빙(carving)하고,
알아볼 수 있는 패턴이면 자동으로 디코딩까지 하는 스크립트 7개에 대한 구조 설명.
언제 뭘 쓰는지는 README.md 참고, 여기는 "내부적으로 어떻게 정보를 찾고 파싱하는지"에 집중.

## 파일 구성과 관계

```
GPS_metadata_avi.py
    ← AVI 핵심 엔진 (AVI/RIFF 저수준 파서 + 자동 디코딩)
GPS_metadata_GPRMC.py
    ← GPS_metadata_avi.py를 import해서 재사용 (같은 폴더 필수)
AVI_exception_lot_RIFF.py
    ← 파싱 전 전처리용 유틸 (손상된 AVI에서 슬랙 제거), 독립 실행 가능
integration_avi.py
    ← 위 셋(avi/GPRMC/AVI_exception_lot_RIFF)의 기능을 한 파일로 합친 통합 스크립트.
      import는 안 하고 필요한 로직을 이 파일 안에 다시 구현/복사해뒀음(아래 참고).
      AVI 파일 처리는 이제 이 스크립트 하나로 끝내는 걸 권장.
GPS_metadata_mp4_pvc1_Atext.py
    ← 독립 스크립트 (MP4/ISO BMFF, 일반 moov/stbl 구조, RIFF 코드 재사용 없음)
GPS_metadata_mp4_udta_mamt_GNRMC_pmp42.py
    ← 독립 스크립트 (MP4/ISO BMFF, pvc1과 반대로 text track이 "없는" 경우 전용 —
      moov/udta/mamt 커스텀 박스, pvc1과 코드 import 관계 없음)
GPS_metadata_fregment_iso4_Atext.py
    ← 독립 스크립트 (Fragmented MP4, moof/traf/trun 구조, pvc1과 코드 import 관계 없음
      — Atext 해석 알고리즘만 재구현)
```

- AVI 두 스크립트(`GPS_metadata_avi.py`, `GPS_metadata_GPRMC.py`)는 같은 RIFF 파서를 쓴다.
  `GPS_metadata_GPRMC.py`는 자체 파서가 없고 `import GPS_metadata_avi as carve`로 저수준
  함수(`validate_chunk`, `find_top_level_sections`, `parse_idx1` 등)를 그대로 갖다 씀.
- `integration_avi.py`는 `GPS_metadata_avi.py`의 저수준 RIFF 파서 + 자동 분류/디코딩 로직을
  **import가 아니라 파일 안에 그대로 복사**해서 갖고 있고(원본을 고쳐도 이 파일은 자동으로
  안 바뀜, 반대도 마찬가지), 그 위에 `AVI_exception_lot_RIFF.py`와 같은 슬랙 판단/리페어
  로직을 앞단 전처리로 얹었다. `GPS_metadata_GPRMC.py`의 "스트림이 텍스트인지 먼저 샘플링
  판정" 기능은 따로 안 가져왔는데, `GPS_metadata_avi.py`의 `decide_stream_kind`(청크 80%
  다수결로 스트림 종류 확정)가 이미 같은 목적을 커버하기 때문. 출력 폴더 형식은 `GPS_metadata_
  GPRMC.py`(`raw_chunks/`, `text_detection.csv`)가 아니라 `GPS_metadata_avi.py` 쪽(`chunks/`,
  `index.csv`, `decode_detection.csv` 등, 더 상위 호환) 하나로 통일했다.
- MP4 스크립트 세 개(pvc1/udta_mamt/fregment)는 컨테이너 포맷 자체가 달라서(RIFF/idx1 vs
  ISO BMFF box 트리) AVI 쪽과 코드 공유가 없다. 셋끼리도 서로 import하지 않는다 — moov
  하위의 일반 Sample Table(`stsc`/`stsz`/`stco`)로 offset을 구하는 pvc1, GPS가 Sample Table이
  아니라 `moov/udta` 밑 커스텀 박스(`mamt`) 안에 통째로 들어있는 udta_mamt, `moof`/`traf`/
  `tfhd`/`tfdt`/`trun`을 매 조각마다 다시 계산해야 하는 fregment는 "GPS 데이터가 어디 있고
  어떻게 offset을 구하는지" 자체가 셋 다 달라서 그 부분은 완전히 별도 구현.
  - pvc1과 fregment는 Sample을 읽어낸 **다음부터의 Atext 문자열 해석 로직**(길이 프리픽스
    검증, `;` 세그먼트 분리, gsensor 정규식, NMEA 판별)이 같은 장비/같은 포맷이라 알고리즘을
    그대로 복사해서 fregment 쪽에 재구현해뒀다(코드 공유 아님 — pvc1을 고쳐도 fregment는
    안 바뀜).
  - udta_mamt는 애초에 Atext/Sample Table을 전혀 안 쓰는 다른 장비/포맷(`mamt` 박스 안에
    `$GNRMC` NMEA 문장이 그대로 텍스트로 나열)이라 pvc1/fregment의 Atext 해석 로직과는
    아예 무관하고, box 트리 순회 방식(`iter_boxes`, size==0/1 처리, 부모 경계 체크)만
    관례적으로 동일하게 재구현했다.
- 다섯 파싱 스크립트(avi, GPRMC, mp4 pvc1, mp4 udta_mamt, mp4 fregment) 모두 NMEA 파싱
  로직(`parse_rmc`, `parse_gga`(fregment/pvc1/avi만 — udta_mamt는 RMC만 지원),
  `_dm_to_decimal`, `nmea_checksum_ok` 등)은 동일한 알고리즘을 각자 파일에 중복 보유하고
  있음(import로 묶지 않고 파일마다 복사됨 — 다섯 중 하나만 고치면 나머지는 안 바뀌니 주의).

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
   스트림 번호를 역산. AVI 표준상 chunk_id는 `<stream#2digits><type2char>`(예: `02st`=stream 2,
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
   않고 계속 진행, 결과는 `index.csv`에 전부 기록됨. **raw 추출은 `OUT_OF_RANGE`만 아니면
   항상 수행**하고, **자동 디코딩(분류/좌표·센서값 산출)만 `OK` 엔트리로 제한**해 잘못된
   idx1 offset에서 나온 데이터를 GPS 좌표로 오인하는 것을 막음(raw는 그대로 남으므로
   ID_MISMATCH/SIZE_MISMATCH 청크도 원본 대조는 항상 가능).
   - payload는 `OUT_OF_RANGE`가 아니라면 청크 헤더 8바이트를 뺀 나머지 그대로 저장
     (`chunks/*.bin`) + 스트림 전체를 이어붙인 `{prefix}_concat.bin`도 별도 생성.
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

### NMEA 파싱 규칙 (`try_parse_nmea` 계열, 네 스크립트 공통)

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
   `handler_type`(4바이트, `vide`/`soun`/`text`/`sbtl` 등)을 확인. **지원 text/subtitle handler
   (`text`/`sbtl`/`subt`)가 아니면 그 자리에서 중단**(vide/soun 트랙은 stbl까지 안 내려감).
4. 지원 text/subtitle 트랙만 `minf` → `stbl`까지 내려가서 `stsd`(샘플 타입 정의)/`stsc`(청크당
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
└── TRACK<N>_TEXT/                      지원 handler(text/sbtl/subt) Track마다 (N은 전체 순번)
    ├── index.csv                        Sample별 chunk/offset/size/validation
    ├── coordinates.csv/.txt             GPRMC/GPGGA 인식된 것만
    ├── sensor_values.csv                gsensor 세그먼트 원본 필드 그대로 (⚠ 비공식)
    ├── other_segments_unparsed.csv      gsensor도 GPS도 아닌 나머지 (CAR 등)
    ├── keyword_hits.csv                 키워드 후보 표시(해석 아님)
    └── chunks/*.bin                     --extract 줬을 때만, Sample 원본 그대로
```

CLI 옵션: `--list-tracks`(목록만), `--dry-run`(파일 미생성), `--extract`(Sample .bin 저장),
`--track N`(특정 지원 text/subtitle Track 번호만 처리).

---

## GPS_metadata_mp4_udta_mamt_GNRMC_pmp42.py — GPS text track이 "없는" MP4(Land Rover 등) 전용

pvc1과 정확히 반대 조건을 다루는 스크립트다. non-fragmented MP4(moov 기반)인 건 같지만,
`moov`의 모든 `trak`을 확인해도 GPS/텍스트용 handler(`text`/`sbtl`/`subt`)를 가진 트랙이
**아예 없는** 장비가 있다 — 이 경우 GPS(NMEA `$GNRMC`)는 Sample Table 어디에도 없고,
`moov` 바로 밑 `udta`(User Data Box) 안의 `mamt`라는 커스텀 박스 안에 NMEA 문장이 그냥
텍스트로 나열돼 있다. pvc1의 stbl 기반 offset 계산 로직은 이 경우 아예 쓸 데가 없어서
독립 스크립트로 새로 작성했다. 다른 네 스크립트를 import하지 않는 완전 자체완결 파일이다.

### 정보를 찾는 방법 (box 트리 순회 + "이 케이스가 맞는지" 사전 판별)

1. **`iter_boxes`**: pvc1/fregment와 같은 원리(`size==1`→64bit extended size, `size==0`→
   "부모 끝까지", 부모 경계를 넘으면 경고 후 그 레벨 순회 중단)로 top-level부터 순회.
   문자열 블라인드 스캔은 전혀 안 쓰고 size 필드만으로 다음 박스 위치를 계산한다.
2. **`locate_gps_source`**: 이 파일이 정말 "text track 없음 + udta/mamt" 케이스인지부터
   순서대로 확인하고, 아니면 처리하지 않고 이유를 알려준다.
   - top-level에 `moof`가 있고 `moov`가 없으면 → fragmented mp4로 보고 스킵
     (`GPS_metadata_fregment_iso4_Atext.py` 사용 권장).
   - `moov`가 없거나 그 안에 `trak`이 하나도 없으면 → 스킵.
   - **모든 `trak`의 `trak/mdia/hdlr`을 끝까지 확인**(`get_handler_type`, 첫 trak만 보고
     판단하지 않음)해서 handler_type이 `text`/`sbtl`/`subt` 중 하나라도 있으면 → 이건
     pvc1이 다뤄야 하는 케이스이므로 스킵(`GPS_metadata_mp4_pvc1_Atext.py` 사용 권장).
   - text 계열 trak이 없을 때만 `moov` 밑 `udta` → 그 안의 `mamt` 박스를 찾는다. 여기서도
     못 찾으면(둘 중 하나라도 없으면) 지원하지 않는 구조로 보고 스킵.
   - 이렇게 "안 맞으면 조용히 건너뛰지 않고 이유를 출력" 하는 게 이 스크립트의 일반화
     방식이다 — 특정 파일 하나가 아니라 "이 조건을 만족하는 모든 파일"에 자동으로 맞춰
     적용/판별되게 하기 위함.
3. **알려진 예외 상황(실제 샘플 3개 파일 모두에서 공통 관찰됨)**: `mdat` 박스의 선언된
   size가 실제 데이터보다 작다(장비 펌웨어가 mdat size 필드를 정확히 안 맞추는 것으로
   추정 — 개별 파일 손상이 아니라 이 장비 모델의 일관된 특징). `moov`는 `mdat`보다 항상
   앞에 있어서 이미 다 읽은 뒤라 GPS 추출 자체는 영향 없지만, top-level 순회를 `mdat` 뒤
   trailing 영역까지 계속하면 그 지점을 엉뚱한 box로 오인해서 경고를 한 번 남기고 순회를
   중단한다 — `iter_boxes`의 "size가 부모 경계를 넘으면 경고 후 중단" 규칙이 그대로 이
   상황을 안전하게 처리해줘서 별도 특수 처리를 추가하지 않았다.

### 어떻게 파싱하는지 (mamt payload 안 가변 길이 NMEA 추출)

1. `mamt` 박스도 `[size|type="mamt"|payload]` 형태의 평범한 박스로 보고, **payload
   범위(`mamt_start+8` ~ `mamt_start+size`)를 먼저 확정한 뒤** 그 범위 안에서만 검색한다
   — 파일 전체에서 `$GNRMC`를 찾지 않는다.
2. **`extract_rmc_sentences`**: `mamt` payload 안에서 정규식 `\$G[NP]RMC`(talker가 GN이든
   GP든 매치)로 문장 시작을 찾고, 그 지점부터 최초의 `CRLF(0D 0A)`까지를 한 문장으로 잘라
   낸다. 문장 길이가 고정이 아니므로 매번 다시 CRLF를 찾아야 하고, 다음 검색은 방금 자른
   문장의 끝(CRLF 다음)부터 이어서 하므로 같은 문장을 중복으로 못 찾는다. CRLF를 못 찾으면
   (데이터가 잘렸거나 손상) 그 지점에서 경고를 남기고 추출을 종료한다.
3. **`try_parse_rmc_sentence`**: 자른 문장을 `nmea_checksum_ok`로 checksum 검증(결과는
   파싱 성공 여부와 무관하게 `checksum_ok` 컬럼에 별도 기록), `,`로 필드 분리 후
   sentence type이 `RMC`가 아니면 버림(`GGA`는 이 장비에서 관찰되지 않아 미지원). 특정
   문장 하나가 필드 파싱에 실패해도 예외를 던지지 않고 그 문장만 `unparsed_lines.txt`로
   보내고 다음 `$GxRMC` 탐색을 계속한다 — 손상된 문장 때문에 이후 데이터 전체를 놓치지
   않게 하기 위함.
   - `status=="V"`(GPS fix 없음, 위경도 필드가 비어있는 정상적인 NMEA 상태)인 문장은
     `GPS_metadata_avi.py`의 `parse_rmc`와 동일한 기존 규약대로 좌표를 만들 수 없어
     `coordinates.csv`에는 안 들어가고 `unparsed_lines.txt`에 원문만 보존된다 — 파싱
     실패가 아니라 "그 순간 GPS 신호가 없었다"는 정상적인 데이터로, 별도 컬럼 구분 없이
     기존 다섯 스크립트 공통 규약을 그대로 따른 것.
4. 위경도 decimal 변환(`_dm_to_decimal`), 날짜/시각 포맷(`format_nmea_date`/
   `format_nmea_time`), knots→km/h 환산(`*1.852`) 로직은 `GPS_metadata_avi.py`와 동일한
   알고리즘을 파일 안에 그대로 복사해뒀다(공통 설계 원칙 참고 — import 아님).

### 출력 CSV 컬럼이 AVI 포맷과 다른 부분

CSV 컬럼 구성은 `GPS_metadata_GPRMC.py`의 `coordinates.csv`와 동일하게 맞췄지만, 그중
AVI(RIFF/idx1) 고유 개념 두 컬럼은 MP4에 그대로 대응되는 게 없어서 의미를 바꿔 채웠다.
- `idx1_entry_offset` → (원래는 idx1 엔트리의 상대 offset) 이 스크립트에서는 해당
  `$GxRMC` 문장이 **파일 안에서 시작하는 절대 byte offset**(`0x%08X`)으로 대체.
- `chunk_id` → (원래는 RIFF 4바이트 fourCC) 모든 GPS 문장이 단일 `mamt` 박스 하나에서만
  나오므로 항상 고정값 `"mamt"`.

### 출력 폴더 구조

```
<출력폴더>/
└── <입력파일명(확장자 제외)>/       입력 파일마다 자동 서브폴더 (여러 파일 일괄 처리 가능)
    └── GPS_GNRMC/
        ├── coordinates.csv/.txt      RMC로 파싱된 좌표만 (GPS_metadata_GPRMC.py와 동일 컬럼)
        ├── unparsed_lines.txt        status=V 등 좌표를 못 만든 문장 + 필드 파싱 실패 문장
        ├── raw_chunks/*.bin          문장 1개당 1개 raw 원문
        ├── raw_concat.bin            mamt에서 찾은 문장을 순서대로 이어붙인 raw
        └── warnings.log              박스 순회/파싱 중 경고 전부
```

CLI: `python GPS_metadata_mp4_udta_mamt_GNRMC_pmp42.py <output_dir> <input1.mp4> [input2.mp4 ...]`
— 입력 파일을 여러 개 한 번에 받아 순차 처리하고, 한 파일이 이 케이스가 아니거나 처리 중
예외가 나도(`try/except`로 감쌈) 나머지 파일 처리는 계속 진행한 뒤 마지막에 파일별
OK/SKIP 요약을 출력한다. Land Rover 대시캠 샘플 3개(`20250901_204119D`/`215628D`/
`215728D`, 각 60초/GNRMC 60개)로 실측 검증함 — 특히 연속 촬영된 두 파일(`215628D`→
`215728D`)의 마지막/첫 좌표가 1초·근접 위치로 자연스럽게 이어지는 것과, 파일명의 현지
시각이 UTC+9 오프셋으로 GNRMC UTC 시각과 정확히 맞는 것까지 확인함.

---

## GPS_metadata_fregment_iso4_Atext.py — Fragmented MP4(iso4, moof/traf/trun) 전용

GPS_metadata_mp4_pvc1_Atext.py와 같은 장비/같은 Atext 텍스트 포맷(`gsensor...;GPRMC...;
CAR...`)을 다루지만, 컨테이너가 Fragmented MP4(`ftyp` major_brand=`iso4`)라 `moov` 안에
pvc1이 의존하는 `stsc`/`stsz`/`stco` 같은 일반 Sample Table이 아예 없다. 대신 `moof`+`mdat`
쌍이 파일 끝까지 반복되면서, 매 조각(`moof`)마다 `tfhd`/`tfdt`/`trun`으로 그때그때
offset/size/시간을 다시 계산해야 하는 구조라 offset 계산 부분은 pvc1과 완전히 별도로
새로 구현했다(문자열 검색은 전혀 안 쓰고 box size 필드만으로 트리를 순회하는 원칙은 동일).

### 정보를 찾는 방법 (box 트리 순회)

1. `read_box_header`/`iter_child_boxes`: pvc1과 같은 원리(`size==1`이면 64bit
   `largesize`, `size==0`이면 "이 box가 부모 끝까지")로 box를 순회하되, "자식 box가
   부모 경계(`parent_end`)를 넘을 수 없다"는 제약을 top-level뿐 아니라 `moov/trak/mdia`,
   `moof/traf` 등 모든 depth에서 명시적으로 강제한다. 깨진 box를 만나면 그 지점에서
   경고만 남기고 해당 레벨 순회를 안전하게 중단.
2. Top-level을 파일 끝까지 훑어서 `moov`, 모든 `moof`, 모든 `mdat`의 위치를 먼저 수집.
   `ftyp` 뒤에 항상 `moov`가 온다거나 `moov` 뒤에 항상 `moof`가 온다는 가정은 하지 않고,
   `moof`가 하나라도 나오면 그 파일을 Fragmented MP4로 판단한다.
3. `parse_moov`: `moov` 안 모든 `trak`에 대해 `tkhd`(track_ID, version 0/1에 따라 필드
   offset이 다름)와 `trak/mdia/hdlr`(handler_type — `trak` 바로 아래가 아니라 반드시
   `mdia`를 거쳐서 찾음)을 확인해 `handler_type == "text"`인 Track만 대상으로 등록.
   `moov/mvex` 안의 모든 `trex`도 미리 파싱해서 Track별 기본 sample duration/size/flags를
   저장해둔다 — 실제 `moof`에 값이 없을 때 쓸 fallback 값.
4. `parse_moof` → `parse_traf`: 각 `moof` 안의 모든 `traf`를 순서대로 처리한다. 대상
   Track이 아닌 `traf`도 끝까지 훑어야 다음 `traf`의 base offset 계산이 맞으므로 건너뛰지
   않는다. `tfhd`에서 `track_ID`와 `flags`를 읽어 대상 Track인지 판별하고, `tfdt`의
   `baseMediaDecodeTime`(duration이 아니라 이 조각의 절대 디코드 시작 시각)을 기준
   시각으로 삼는다.
5. `resolve_base_data_offset`: ISO/IEC 14496-12의 3가지 경우를 그대로 구현 — (1) `tfhd`에
   `base_data_offset`이 명시돼 있으면 그 값, (2) 없고 `default-base-is-moof` 플래그가
   있으면 이 `moof`의 시작 offset, (3) 둘 다 없으면 "이 `moof`의 첫 `traf`면 `moof` 시작
   offset, 아니면 바로 앞 `traf`가 써낸 데이터의 끝" — 어떤 경우에도 임의로 0이나 현재
   위치를 넣지 않는다.
6. `resolve_sample_duration`/`resolve_sample_size`: 각 sample의 duration/size는
   `trun(개별 값) → tfhd(default) → trex(default)` 순으로 fallback. 어디에도 없으면
   duration은 "unavailable"로 표시하고, size는 다음 sample 위치를 계산할 수 없으므로
   그 시점에서 추출을 중단.
7. 실제 sample 위치는 `base_data_offset + trun.data_offset`에서 시작해서 sample size를
   누적하며 다음 sample 위치를 계산한다. `trun`이 하나의 `traf`에 여러 개 있거나
   `data_offset`이 없는 경우(직전 run 데이터 바로 뒤로 이어붙는 규칙)도 규격대로 처리.

### 어떻게 파싱하는지 (시간 계산 + Atext 해석)

1. 각 `traf`의 첫 sample DTS는 `tfdt.baseMediaDecodeTime`이고, 이후 sample마다 직전
   sample의 duration만큼 누적(`current_dts += duration`)한 뒤 `mdhd.timescale`로 나눠서
   초 단위 시작/종료 시각(`start_time`/`end_time`)을 계산한다 — pvc1 스크립트에는 없던,
   "영상 재생 시간과 metadata를 동기화"하기 위해 이 스크립트에서 새로 추가한 부분.
2. Sample 원본 바이트를 읽은 뒤부터는 pvc1과 같은 알고리즘을 그대로 재구현해서 씀:
   앞 2바이트 길이 프리픽스 검증(`decode_sample_text`, `declared_len + 2 == 전체 크기`일
   때만 신뢰) → `;`로 세그먼트 분리(`split_segments`) → `classify_segment`로 `gsensor`/
   `gps_nmea`/`generic` 판정.
3. pvc1과 다른 점: `classify_segment` 결과 중 `gsensor`/`gps_nmea`(GPRMC + GPGGA) 두
   종류만 최종 결과(`KEPT_KINDS`)로 남기고 `generic`(예: `CAR,...`)은 그 자리에서 버린다
   — "GPS/속도/위치 시각화"라는 목적에 필요 없는 데이터라 사용자 요청으로 필터링한 것.
   실제 원본 파일(`REC_20240312_082217_F.mp4`)을 이 스크립트와 완전히 별개인 코드로
   직접 재파싱해서 gsensor(600)/GPRMC(60)/CAR(600) 세 종류 외 다른 세그먼트가 없다는
   것을 확인한 뒤 내린 결정 — 필터링으로 놓치는 정보가 없음을 검증함.
4. gsensor 세그먼트 필드(`gsensor<subtype>,<count>,<scale>,<x>,<y>,<z>`)는 pvc1처럼
   `field_0, field_1, ...`로 원본만 보존하는 대신, 실측 데이터로 의미를 역산해서 이름을
   붙였다(⚠ 공식 스펙 아님) — `count`는 파일 전체에서 항상 "4"(뒤에 오는 값 개수),
   `scale`은 항상 "2048"(1g당 카운트 수)로 일관되게 나타나 `x_raw/scale = x_g` 식으로
   g 단위 값도 같이 산출해서 저장.
5. `timeline.csv`: GPS(1Hz)와 G센서(10Hz)를 sample 단위(0.1초 간격)로 한 줄씩 합친 통합
   타임라인 — GPRMC가 없는 sample은 `latitude`/`longitude` 등을 공란으로 두고, 시각화
   편의를 위한 `*_last` 컬럼에만 가장 최근 GPS 값을 그대로 이어붙인다(값 자체를
   보간하지 않음 — 원본 그대로).

### 출력 폴더 구조

```
<출력폴더>/
├── track_table.csv              전체 Track: handler/이름/stsd타입/샘플수 (pvc1과 같은 형식)
├── warnings.log
└── TRACK<track_ID>_TEXT/        handler_type=="text"인 Track마다 (N=tkhd.track_ID 값
    │                              그 자체, "몇 번째 trak인지"가 아님)
    ├── index.csv                 Sample별 moof_index/traf_index/trun_index, offset/size,
    │                              dts/duration, start~end 시간, validation
    ├── coordinates.csv/.txt      GPRMC/GPGGA 인식된 것만 (+ start/end 시간 컬럼 추가)
    ├── sensor_values.csv         gsensor raw + g단위 환산값 (+ start/end 시간, ⚠ count/scale
    │                              해석은 비공식 추정)
    └── timeline.csv              GPS+G센서를 sample 시간 기준 한 줄로 합친 통합 타임라인
                                   (신규, 시각화용)
```

pvc1과 달리 `--extract`(Sample 원본 `.bin` 저장)와 `other_segments_unparsed.csv`/
`keyword_hits.csv`는 만들지 않는다 — GPS/속도/위치 시각화에 안 쓰이는 raw carving과
범용 키워드 스캔은 이 스크립트의 목적이 아니라서 의도적으로 뺐다.

CLI 옵션: `--list-tracks`(Track 목록만), `--dry-run`(파일 미생성, 콘솔 출력만),
`--track-id N`(handler_type=="text" Track이 여러 개일 때 `tkhd.track_ID` 기준으로 지정),
`--max-print N`(콘솔에 출력할 Sample 개수 제한, CSV에는 항상 전체 기록), `--debug`
(Box/Track/Trex/Tfhd/Tfdt/Trun 값을 전부 콘솔에 출력 — hex editor로 직접 값 대조할 때 사용).

---

## AVI_exception_lot_RIFF.py — 파싱 전 전처리(복구) 유틸

메타데이터 파싱이 아니라 **손상된(더 정확히는 "예전 녹화분 잔재가 섞인") AVI 파일 자체를
복구**하는 스크립트.

**실제 샘플(VUGERA MB-900SB, `REC_20240916_172436_F.avi`)을 hex 단위로 뜯어서 확인한 결과**,
처음에 짐작했던 것과 위치가 달랐다 — 처음엔 "파일 끝에 두 번째 RIFF나 슬랙이 붙는다"고
가정했는데, 실제로는 이 카메라가 파일을 **고정 크기로 미리 만들어두고 앞부분만 새 녹화로
덮어쓰는** 방식이라, 슬랙은 파일 끝 뒤가 아니라 최상위 RIFF가 선언한 **movi 영역 내부에**
예전 녹화 파일의 RIFF/hdrl/JUNK(구 파일명 포함)/movi가 통째로 남아있는 형태로 나타난다.
(파일 크기 자체는 카메라/모델마다 다를 수 있으므로 특정 값을 코드에 가정하지 않는다 —
항상 "이 파일 자신의 최상위 RIFF가 선언한 크기"를 그때그때 읽어서 기준으로 삼는다.)

### 동작 방식

1. 최상위 `RIFF`(`AVI ` formType)를 확인하고, 그 안의 `hdrl`/`movi`/`idx1`을 **구조적으로**
   (선언된 크기를 따라가며 직계 자식으로) 찾는다 — 예전 버전처럼 `LIST`+`movi` 바이트열을
   찾아 위치를 확정하거나 파일 전체에서 `idx1` 문자열을 훑는 방식은 쓰지 않는다. idx1은
   RIFF의 자식 chunk로 유일하게 정해지므로 "여러 idx1 후보 중 진짜를 고르는" 단계 자체가
   필요 없다.
2. **`find_embedded_riffs`**: movi의 구조적으로 확정된 content 범위(`[movi.content_start,
   movi.content_end)`) **안에서만** `b"RIFF"` + 그 뒤 4바이트가 `AVI `/`AVIX`인 조합을
   찾는다(문자열 검색이긴 하지만 전체 파일이 아니라 이 범위로 엄격히 제한). 찾은 자리를
   "예전 파일 잔재"로 인정하는 조건은 둘 중 하나(어느 쪽도 하드코딩된 크기 값을 안 씀,
   전부 이 파일 자신에서 읽은 값끼리 비교):
   - **(a)** 선언 크기가 이 파일 자신의 최상위 RIFF 선언 크기와 정확히 같음 — 같은 카메라/
     포맷이 쓰는 고정 컨테이너 크기 관례를 그대로 물려받은 예전 파일이라는 강한 정황 증거.
   - **(b)** 선언 크기가 실제 남은 공간(movi content 끝까지)보다 커서 원래 파일 전체가
     들어갈 수 없음 — 일부만 덮어써지고 잘려나간 잔재라는 확실한 증거.
3. 임베디드 RIFF가 하나라도 발견되면(또는 최상위 RIFF 자체가 2개 이상 이어붙어 있으면),
   idx1 엔트리를 **뒤에서부터** 실제 chunk 헤더(`chunk_id`+`size`)와 대조 검증해서 진짜
   마지막으로 유효한 지점(`actual_movi_end`)을 찾는다. idx1은 현재 녹화분 chunk만 정확히
   가리키고 있어서 대부분 첫 시도(마지막 엔트리)에서 바로 검증을 통과한다 — 실제로 이
   엔트리들이 예전 파일 잔재를 참조하는 일은 없다(잔재는 애초에 idx1에 안 잡혀 있음).
4. `actual_movi_end` 이후(예전 파일 잔재 + 나머지 슬랙)는 전부 잘라내고, `movi`/`RIFF`
   크기를 새 길이에 맞게 재계산해서 헤더에 써넣은 뒤 검증된 idx1만 그 뒤에 다시 붙인다.
5. `avih`의 프레임 수(`dwTotalFrames`)도 실제로 남은 비디오 프레임(`00dc`/`01dc`/`00db`/
   `01db` chunk_id) 개수로 다시 세서 덮어씀 — 프레임 수 불일치로 인한 재생 오류 방지.
6. 결과를 `Recovered_<원본파일명>`으로 저장(스트리밍 블록 복사, 큰 파일도 전체를 메모리에
   올리지 않음). 실행하면 현재 폴더의 `REC_*.avi`를 모두 찾아 `./Recovered_2/`에 일괄
   처리(`process_all_samples`, 이미 `Recovered_` 접두어인 파일은 재처리 스킵).
7. 임베디드 RIFF를 하나도 못 찾으면(이미 깨끗한 파일이거나 이 스크립트가 다루는 손상
   패턴이 아니면) 아무것도 만들지 않고 `False`를 반환 — 오탐 방지를 위해 "일단 잘라보고
   보는" 동작은 하지 않는다.

**실측 검증**: `REC_20240916_172436_F.avi`에서 임베디드 RIFF 2개(`0x4CC0000`, `0x4E60000`,
각각 예전 파일 `REC_20240822_232548_R.avi`/`REC_20240908_064525_F.avi`의 JUNK 청크 파일명
흔적까지 확인됨)를 정확히 찾아냈고, 실제 유효 데이터 끝(`0x4BEE472`)과 idx1 위치
(`0x4F00000`~`0x4F1BE68`)까지 hex-editor로 직접 뜯어본 값과 정확히 일치함을 확인했다.
이 스크립트로 복구한 파일을 `GPS_metadata_avi.py`/`GPS_metadata_GPRMC.py`/
`integration_avi.py` 입력으로 넘기면 (idx1은 원래부터 예전 파일 잔재를 참조하지 않으므로)
GPS/센서 추출 결과는 리페어 전/후로 완전히 동일하다 — 이 리페어는 GPS 추출의 전제조건이
아니라 "깨끗한 재생용 사본"을 별도로 남겨주는 보너스에 가깝다.

---

## integration_avi.py — 위 세 개를 합친 통합 스크립트

`GPS_metadata_avi.py` + `GPS_metadata_GPRMC.py` + `AVI_exception_lot_RIFF.py`를 파일 하나로
합친 것. 여러 입력 파일을 한 번에 받아 파일마다 아래 순서로 처리한다.

### 처리 순서

1. **슬랙 판단** (`analyze_slack` → `handle_slack`): 위 `AVI_exception_lot_RIFF.py`와 완전히
   같은 기준(임베디드 RIFF 있음/최상위 RIFF 2개 이상 → 리페어, 그 외 판단은 아래로)으로
   판단하되, 최상위 RIFF **밖**(파일 끝 이후)에 남는 트레일링 바이트도 별도로 확인한다.
   - 트레일링이 진짜 두 번째 RIFF로 시작하면 리페어 대상에 포함.
   - 트레일링이 있는데 RIFF가 아니면(예: FineVu CustomGPS 샘플들 — `JUNK` 태그로 시작하는
     수 MB짜리 완전 바이너리, NMEA 텍스트 패턴 없음) **절대 잘라내지 않고** 원본은 그대로
     둔 채 raw로 별도 보존(`trailing_unknown_data.bin` + 설명 `.README.txt`) — 실제
     데이터일 수 있는데 슬랙으로 오인해 지우는 사고를 막기 위함.
   - 리페어가 필요하면 결과 폴더 안에 `<파일명>_wo_slack.avi`를 생성(임시 폴더가 아니라
     최종 산출물로 바로 남김).
2. **추출** (`GPS_metadata_avi.py`와 동일 로직): 리페어된 파일이 있으면 그걸, 없으면 원본을
   그대로 읽어서 스트림 테이블 구성 → `SELECT_MODE` 기준 스트림 선택 → 각 idx1 엔트리
   payload를 내용 기반으로 4-way 분류 → 스트림 단위 80% 다수결로 text/float_vector 확정 →
   `coordinates.*`/`sensor_values.csv` 생성.

### GPS_metadata_avi.py/GPS_metadata_GPRMC.py 원본과의 차이

- 저수준 파서/분류/디코딩 함수는 **import가 아니라 파일 안에 복사**돼 있다 — 원본
  `GPS_metadata_avi.py`를 고쳐도 이 파일은 자동으로 안 바뀌고, 반대도 마찬가지. 실제로
  같은 입력 파일에 대해 `GPS_metadata_avi.py`를 단독 실행한 결과와 `integration_avi.py`의
  추출 결과물(`index.csv`/`coordinates.csv`/`sensor_values.csv`/모든 청크 `.bin`)이 해시
  단위로 완전히 동일함을 확인했다(슬랙 리페어가 적용된 REC 샘플 포함 — 리페어 유무가
  GPS 추출 결과에 영향을 주지 않음도 같이 검증됨).
- `GPS_metadata_GPRMC.py`의 "스트림이 텍스트인지 먼저 8개만 샘플링해서 이진 판정"하는
  사전 필터링 단계는 없다 — `decide_stream_kind`의 사후 80% 다수결 판정이 사실상 같은
  역할을 하므로, 별도 사전 판정 없이 `auto_non_av`로 고른 모든 비-AV 스트림을 그대로
  추출·분류한다.
- 출력 폴더 구조는 `GPS_metadata_avi.py` 쪽(`chunks/`, `index.csv`, `decode_detection.csv`
  등)으로 통일했다. `GPS_metadata_GPRMC.py`가 쓰던 `raw_chunks/`, `text_detection.csv`
  이름은 안 나온다.

### 출력 폴더 구조

```
<출력루트>/<파일명(확장자 제외)>/
├── <STREAM_LABEL>/...            ← GPS_metadata_avi.py와 동일 (chunks/, concat.bin,
│                                     coordinates.*, sensor_values.csv, unparsed_lines.txt)
├── stream_table.csv / index.csv / decode_detection.csv / warnings.log
├── <파일명>_wo_slack.avi         ← 슬랙(임베디드 RIFF/중복 RIFF) 리페어가 적용된 경우만
└── trailing_unknown_data.bin     ← 최상위 RIFF 뒤 트레일링이 RIFF가 아닌 경우만(+.README.txt)
```

CLI: `python integration_avi.py -o <출력루트> <입력1.avi> [<입력2.avi> ...]` — 입력을 여러
개 한 번에 받아 파일마다 위 과정을 순서대로 처리한다. `--select-mode`/`--fcctype`/
`--index`/`--chunk-id`/`--dry-run`은 `GPS_metadata_avi.py`와 동일한 의미.

---

## 공통 설계 원칙

- **raw는 항상 그대로 보존**한다 — 디코딩 결과가 의심스러우면 `chunks/*.bin`,
  `*_concat.bin`(AVI), `chunks/*.bin`(MP4 pvc1, `--extract` 시)으로 원본 대조 가능. AVI의
  ID_MISMATCH/SIZE_MISMATCH 엔트리도 raw는 그대로 뽑되, false positive 방지를 위해
  자동 디코딩(분류/좌표 산출)만 생략하고 `index.csv`에 사유를 남긴다.
  (예외: `GPS_metadata_fregment_iso4_Atext.py`는 raw `.bin` 저장 기능 자체가 없음 —
  GPS/속도/위치 시각화라는 목적에 필요 없어 의도적으로 뺀 것. 원본 대조가 필요하면
  `index.csv`의 `absolute_offset`/`size`로 원본 mp4를 직접 seek해서 확인하면 됨.)
- **지원되는 NMEA 레코드의 값 변환 실패는 프로그램을 중단하지 않고 해당 레코드를 미분류/경고 처리한다** — `WARNINGS` 리스트에 쌓아서
  `warnings.log`로 출력, 문제 있는 엔트리/청크는 건너뛰고 나머지는 계속 처리.
- **NMEA(GPRMC/GPGGA)는 표준 필드 형식을 기준으로 파싱**하지만, 실제 파일은 손상/비표준 값이 있을 수 있다. 좌표 범위·hemisphere·status/checksum·값 변환을 검증하고 `trusted`/`parse_warnings`를 함께 기록한다.
- **float32 벡터(SENS/sensor_values)와 MP4 gsensor 필드는 공식 스펙이 아닌 관찰 기반
  추정** — 값 범위(±50 이내)나 필드 개수로 판단한 것이라 다른 장비 파일에서 같은
  패턴이 나와도 곧이곧대로 믿지 말 것.
- **오프셋 기준점(base offset)은 장비마다 다를 수 있어 항상 실측 검증 후 채택** —
  AVI 관련 스크립트(`GPS_metadata_avi.py`/`GPS_metadata_GPRMC.py`/`AVI_exception_lot_RIFF.py`/
  `integration_avi.py`) 모두 후보 여러 개를 두고 idx1 엔트리와 실제 바이트를 대조해서
  점수가 가장 높은 후보를 선택하는 동일한 패턴을 씀.
- **"이 정도 크기/오프셋이면 정상"이라는 판단 기준에 리터럴 상수를 박아넣지 않는다** —
  파일 크기, 컨테이너 크기, 슬랙 판단 기준 등은 전부 그 파일 자신에서 방금 읽은 값끼리
  비교한다(예: `AVI_exception_lot_RIFF.py`/`integration_avi.py`의 슬랙 판단은 "80MB"
  같은 값을 가정하지 않고 "이 파일 자신의 최상위 RIFF가 선언한 크기"를 매번 다시 읽어서
  기준으로 삼음). 특정 샘플 하나로 검증했다고 해서 그 샘플의 구체적인 숫자를 코드에
  하드코딩하면 다른 크기/모델의 파일에서 조용히 틀린 판단을 내리게 된다.
