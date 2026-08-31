# architect.md

블랙박스 영상(AVI/MP4)에 박혀 있는 GPS/센서 메타데이터를 원본 그대로 카빙(carving)하고,
알아볼 수 있는 패턴이면 자동으로 디코딩까지 하는 스크립트 8개에 대한 구조 설명.
언제 뭘 쓰는지는 README.md 참고, 여기는 "내부적으로 어떻게 정보를 찾고 파싱하는지"에 집중.

## 파일 구성과 관계

```
integration_blackbox.py
    ← 최상위 진입점. 파일 시그니처로 AVI/MP4를 판별해 아래 두 통합 스크립트로 넘긴다.
      이 프로젝트에서 유일하게 다른 스크립트를 import하는 파일(integration_avi /
      integration_mp4를 모듈로 불러 main()을 호출). 자기 파싱 로직은 없다.
mp4_slack_carve.py
    ← MP4 슬랙(free/skip Box, Box 사이 gap, 꼬리) 카빙 단독 스크립트.
      integration_mp4.py 안에 같은 로직이 복사돼 있어 보통은 그쪽으로 자동 수행됨.
      ※ AVI_exception_lot_RIFF.py와는 목적이 반대다 - 저쪽은 슬랙을 "잘라내"
        깨끗한 재생용 사본을 만들고(AVI 슬랙은 movi 안에 박혀 재생을 방해),
        이쪽은 슬랙에서 데이터를 "건져낸다"(MP4 슬랙은 free로 선언돼 있어 자를 이유 없음).
GPS_metadata_avi.py
    ← AVI 핵심 엔진 (AVI/RIFF 저수준 파서 + 자동 디코딩)
GPS_metadata_GPRMC.py
    ← GPS_metadata_avi.py를 import해서 재사용 (같은 폴더 필수)
AVI_exception_lot_RIFF.py
    ← 파싱 전 전처리용 유틸 (손상된 AVI에서 슬랙 제거), 독립 실행 가능
GPS_metadata_avi_txts_record72.py
    ← txts/dats 스트림이 NMEA 텍스트가 아니라 72바이트 고정 이진 레코드인 경우 전용.
      GPS_metadata_GPRMC.py 와 같이 `import GPS_metadata_avi as carve` 로 RIFF/idx1
      저수준 파서를 그대로 쓰고, 이 파일은 레코드 해석만 갖는다.
integration_avi.py
    ← 위 셋(avi/GPRMC/AVI_exception_lot_RIFF)의 기능을 한 파일로 합친 통합 스크립트.
      import는 안 하고 필요한 로직을 이 파일 안에 다시 구현/복사해뒀음(아래 참고).
      AVI 파일 처리는 이제 이 스크립트 하나로 끝내는 걸 권장.
GPS_metadata_mp4_pvc1_Atext.py
    ← 독립 스크립트 (MP4/ISO BMFF, 일반 moov/stbl 구조, RIFF 코드 재사용 없음)
GPS_metadata_mp4_udta_mamt_GNRMC_pmp42.py
    ← 독립 스크립트 (MP4/ISO BMFF, pvc1과 반대로 text track이 "없는" 경우 전용 —
      moov/udta/mamt 커스텀 박스, pvc1과 코드 import 관계 없음)
GPS_metadata_fragment_iso4_Atext.py
    ← 독립 스크립트 (Fragmented MP4, moof/traf/trun 구조, pvc1과 코드 import 관계 없음
      — Atext 해석 알고리즘만 재구현, 세그먼트 구분자/미확정 벤더 레코드 처리는 pvc1보다
      일반화돼 있음, 아래 참고)
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
- MP4 스크립트 세 개(pvc1/udta_mamt/fragment)는 컨테이너 포맷 자체가 달라서(RIFF/idx1 vs
  ISO BMFF box 트리) AVI 쪽과 코드 공유가 없다. 셋끼리도 서로 import하지 않는다 — moov
  하위의 일반 Sample Table(`stsc`/`stsz`/`stco`)로 offset을 구하는 pvc1, GPS가 Sample Table이
  아니라 `moov/udta` 밑 커스텀 박스(`mamt`) 안에 통째로 들어있는 udta_mamt, `moof`/`traf`/
  `tfhd`/`tfdt`/`trun`을 매 조각마다 다시 계산해야 하는 fragment는 "GPS 데이터가 어디 있고
  어떻게 offset을 구하는지" 자체가 셋 다 달라서 그 부분은 완전히 별도 구현.
  - pvc1과 fragment는 Sample을 읽어낸 **다음부터의 Atext 문자열 해석 로직**(길이 프리픽스
    검증, 세그먼트 분리, gsensor 정규식, NMEA 판별)이 같은 계열 장비/포맷이라 알고리즘을
    복사해서 fragment 쪽에 재구현해뒀다(코드 공유 아님 — pvc1을 고쳐도 fragment는
    안 바뀜). 단, 세그먼트 분리 로직은 fragment 쪽이 실측(Mercedes-Benz)으로 더
    일반화돼 있어 둘이 완전히 동일하지는 않다 — 아래 fragment 항목 참고.
  - udta_mamt는 애초에 Atext/Sample Table을 전혀 안 쓰는 다른 장비/포맷(`mamt` 박스 안에
    `$GNRMC` NMEA 문장이 그대로 텍스트로 나열)이라 pvc1/fragment의 Atext 해석 로직과는
    아예 무관하고, box 트리 순회 방식(`iter_boxes`, size==0/1 처리, 부모 경계 체크)만
    관례적으로 동일하게 재구현했다.
- 다섯 파싱 스크립트(avi, GPRMC, mp4 pvc1, mp4 udta_mamt, mp4 fragment) 모두 NMEA 파싱
  로직(`parse_rmc`, `parse_gga`(fragment/pvc1/avi만 — udta_mamt는 RMC만 지원),
  `_dm_to_decimal`, `nmea_checksum_ok` 등)은 동일한 알고리즘을 각자 파일에 중복 보유하고
  있음(import로 묶지 않고 파일마다 복사됨 — 다섯 중 하나만 고치면 나머지는 안 바뀌니 주의).
  ⚠ 현재 `nmea_checksum_ok`는 fragment 사본만 버그 수정된 상태(`re.fullmatch`→`re.match`,
  아래 fragment 항목 참고)라 다섯 사본이 더 이상 완전히 동일하지 않다 — 다른 네 스크립트도
  체크섬 뒤에 예상 못 한 데이터가 붙는 장비를 만나면 같은 오탐이 날 수 있으므로, 그런
  증상이 보이면 이 수정을 이식할 것.

---

## integration_blackbox.py — 최상위 진입점

파일이 AVI인지 MP4인지 판별해서 `integration_avi.py` / `integration_mp4.py` 로 넘긴다.
자기 파싱 로직은 없고, 이 프로젝트에서 **유일하게 다른 스크립트를 import하는 파일**이다
(두 통합 스크립트를 모듈로 불러 `main(argv)`를 호출). 나머지 스크립트들이 코드를 복사해
갖고 있는 것과 달리, 여기서는 복사할 이유가 없다 - 하는 일이 "어느 쪽으로 보낼지 정하고
그대로 넘기기"뿐이라 중복시킬 로직 자체가 없다.

### 판별 (`detect_container`)

확장자를 안 본다. 확장자는 언제든 바뀔 수 있고 포렌식 대상이면 더 못 믿는다. 게다가 이
프로젝트에서 이미 "선언된 메타데이터를 믿으면 틀린다"를 겪었다 - MP4 통합 때 ftyp의
major_brand로 분기하려다가 같은 `avc1`이 non-fragmented(INAVI Z300)와 fragmented(신규
Ambarella) 양쪽에 다 쓰이는 걸 확인하고 구조 기반으로 바꿨다. 그래서 여기서도 파일 앞
16바이트를 직접 읽는다.

| 판별 | 조건 |
|---|---|
| AVI | `offset 0 == "RIFF"` 이고 `offset 8 == "AVI "` |
| MP4 | `offset 4 == "ftyp"` (ISO BMFF는 첫 Box가 ftyp인 게 표준) |
| MP4 | ftyp가 없어도 첫 Box 타입이 `moov`/`mdat`/`moof`/`free`/`skip`/`wide`/`pnot` 이면 ISO BMFF 변종으로 인정 |
| 불가 | RIFF인데 formType이 AVI가 아님(예: `WAVE`) / 12바이트 미만 / 둘 다 아님 |

확장자와 내용이 다르면 `check_extension_mismatch`가 경고를 남기지만 **판별은 내용을 따른다**
(확장자가 틀린 것이지 데이터가 틀린 게 아니므로). 처리 불가 파일은 사유를 적고 그 파일만
건너뛰며, 나머지는 계속 처리된다.

### 실행 (`main`)

입력을 AVI 묶음 / MP4 묶음으로 나눠 각 통합 스크립트의 `main()`을 한 번씩 호출한다.
같은 프로세스 안에서 돌리므로 출력 폴더 구조와 결과물은 각각을 직접 실행했을 때와 동일하다.

- 공용 옵션: `--dry-run`, `--no-slack`(MP4 그룹에만 전달), `--detect-only`.
- 하위 고유 옵션: `--avi-opt=` / `--mp4-opt=` 로 넘긴다. 값이 `-`로 시작하면 argparse가
  옵션으로 오인하므로 **반드시 `=` 형태로 붙여 써야 한다**(`--mp4-opt="--track-id 3"`).
  받은 문자열은 `shlex.split`으로 쪼개 그대로 전달한다.
- 하위 `main()` 호출을 `SystemExit`/`Exception`으로 감싼다. `integration_avi.py`의
  `assert_riff_file`처럼 `sys.exit`을 부르는 코드가 있어서, 안 잡으면 AVI 그룹이 죽을 때
  뒤이은 MP4 그룹까지 같이 죽는다. 실제로 잘못된 하위 옵션을 준 그룹만 실패하고 다른
  그룹은 정상 처리되는 걸 확인했다.

### 검증

샘플 19개(AVI 7 + MP4 12)를 이 진입점으로 한 번에 돌린 결과와 `integration_avi.py` /
`integration_mp4.py`를 따로 돌린 결과를 파일 단위 sha256으로 대조 - **산출물 16,671개 전부
일치, 누락/추가 0건**. 확장자 위장(내용 AVI인데 `.mp4`, 내용 MP4인데 `.avi`), RIFF/WAVE,
8바이트 파일, ftyp 없는 moov 시작 변종, 컨테이너가 아닌 바이트열을 합성해 판별 경로도
전부 확인했다.

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
- **fix가 없던 순간(`status=V`)도 행으로 남긴다**: RMC의 위경도 필드가 **비어 있고**
  `status`가 `A`가 아니면 "그 순간 위성을 못 잡음"이라는 정상 기록으로 보고 `lat`/`lon`을
  `None`으로 두되 나머지 필드는 그대로 채운 dict를 돌려준다 → `coordinates.csv`에는
  좌표/속도만 공란인 행으로 들어가고, `coordinates.txt`(좌표 목록)에서는 제외된다.
  반대로 위경도 필드에 **값은 있는데 파싱이 안 되는** 경우는 손상으로 보고 예전처럼
  `None`을 반환해 `unparsed_lines.txt`로 보낸다 — 이 둘을 구분하는 게 핵심이다.
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
    ├── coordinates.csv/.txt             GPRMC/GPGGA 인식된 것만. NMEA 필드 컬럼은 AVI 쪽
    │                                     (GPS_metadata_GPRMC.py)과 동일하게 맞춤 —
    │                                     magvar/magvar_dir/mode 포함. AVI 고유 위치 컬럼
    │                                     (sequence/idx1_entry_offset/chunk_id) 자리에는
    │                                     MP4 고유의 `sample`(Sample 번호)이 들어감
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

1. **`iter_boxes`**: pvc1/fragment와 같은 원리(`size==1`→64bit extended size, `size==0`→
   "부모 끝까지", 부모 경계를 넘으면 경고 후 그 레벨 순회 중단)로 top-level부터 순회.
   문자열 블라인드 스캔은 전혀 안 쓰고 size 필드만으로 다음 박스 위치를 계산한다.
2. **`locate_gps_source`**: 이 파일이 정말 "text track 없음 + udta/mamt" 케이스인지부터
   순서대로 확인하고, 아니면 처리하지 않고 이유를 알려준다.
   - top-level에 `moof`가 있고 `moov`가 없으면 → fragmented mp4로 보고 스킵
     (`GPS_metadata_fragment_iso4_Atext.py` 사용 권장).
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
2. **`extract_rmc_sentences`**: `mamt` payload 안에서 정규식 `\$[A-Z]{2}RMC`(talker 2글자는
   뭐든 매치 — GN/GP뿐 아니라 GL(GLONASS)/GA(Galileo)/GB·BD(BeiDou)/GQ(QZSS) 등도 전부 잡음.
   처음엔 실제 관찰된 GN/GP만 `\$G[NP]RMC`로 하드코딩했다가, 다른 talker를 쓰는 멀티-GNSS
   장비에서 조용히 다 놓치는 문제라 일반화함)로 문장 시작을 찾고, 그 지점부터 최초의
   `CRLF(0D 0A)`까지를 한 문장으로 잘라낸다. 문장 길이가 고정이 아니므로 매번 다시 CRLF를
   찾아야 하고, 다음 검색은 방금 자른 문장의 끝(CRLF 다음)부터 이어서 하므로 같은 문장을
   중복으로 못 찾는다. CRLF를 못 찾으면(데이터가 잘렸거나 손상) 그 지점에서 경고를 남기고
   추출을 종료한다.
3. **`try_parse_rmc_sentence`**: 자른 문장을 `nmea_checksum_ok`로 checksum 검증(결과는
   파싱 성공 여부와 무관하게 `checksum_ok` 컬럼에 별도 기록), `,`로 필드 분리 후
   sentence type이 `RMC`가 아니면 버림(`GGA`는 이 장비에서 관찰되지 않아 미지원). 특정
   문장 하나가 필드 파싱에 실패해도 예외를 던지지 않고 그 문장만 `unparsed_lines.txt`로
   보내고 다음 `$GxRMC` 탐색을 계속한다 — 손상된 문장 때문에 이후 데이터 전체를 놓치지
   않게 하기 위함.
   - `status=="V"`(GPS fix 없음, 위경도 필드가 비어있는 정상적인 NMEA 상태)인 문장은
     **`coordinates.csv`에 행으로 남긴다** — 좌표/속도만 공란이고 나머지(date, utc_time,
     status=V, mode=N, status_valid=False, trusted=False, checksum_ok, raw_sentence,
     절대 offset)는 전부 채워진다. 파싱 실패가 아니라 "그 순간 GPS 신호가 없었다"는
     정상 기록이라, 행 자체를 버리면 시계열에서 "끊긴 구간"과 "애초에 데이터가 없는
     구간"을 구분할 수 없기 때문. 다섯 스크립트 공통 규약이다.
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

## GPS_metadata_fragment_iso4_Atext.py — Fragmented MP4(iso4, moof/traf/trun) 전용

GPS_metadata_mp4_pvc1_Atext.py와 같은 계열 장비(Ambarella 기반, Atext 트랙에 GPS/센서를
같이 실어보내는 방식)를 다루지만, 컨테이너가 Fragmented MP4(`ftyp` major_brand=`iso4`)라
`moov` 안에 pvc1이 의존하는 `stsc`/`stsz`/`stco` 같은 일반 Sample Table이 아예 없다. 대신
`moof`+`mdat` 쌍이 파일 끝까지 반복되면서, 매 조각(`moof`)마다 `tfhd`/`tfdt`/`trun`으로
그때그때 offset/size/시간을 다시 계산해야 하는 구조라 offset 계산 부분은 pvc1과 완전히
별도로 새로 구현했다(문자열 검색은 전혀 안 쓰고 box size 필드만으로 트리를 순회하는
원칙은 동일). INAVI QXD8000(`REC_20240312_082217_F.mp4`)과 Mercedes-Benz Drive View
(`20240411_144016E.MP4`) 둘 다 이 케이스로 실측 검증했다 — 아래 "Atext 해석" 항목에서
보듯 두 장비의 Atext 내용물 포맷이 서로 달라서, 처음엔 INAVI 기준으로만 짜여 있던 세그먼트
분리/체크섬 검증 로직에 버그가 있었고 Mercedes-Benz 샘플로 실측하다가 발견해서 고쳤다.

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
2. Sample 원본 바이트를 읽은 뒤부터는 pvc1과 비슷한 알고리즘을 재구현해서 씀:
   앞 2바이트 길이 프리픽스 검증(`decode_sample_text`, `declared_len + 2 == 전체 크기`일
   때만 신뢰) → 세그먼트 분리(`split_segments`) → `classify_segment`로 `gsensor`/
   `gps_nmea`/`vendor_raw`/`generic` 판정. 단 세그먼트 분리는 pvc1의 단순 `;`-split이
   아니라 아래처럼 일반화돼 있다.
3. **세그먼트 분리 일반화(`split_segments`)**: INAVI는 `;`로 세그먼트를 구분하는데
   (`gsensor...;GPRMC...*60\r\n;CAR,...`), Mercedes-Benz 실측 샘플에서는 GPRMC 문장과
   그 뒤 벤더 서브레코드가 `\r\n`으로만 구분되고, 그 서브레코드들끼리는 아무 구분자 없이
   `$`로 시작하는 문장이 그냥 이어붙어 있음(`$M4,...$M4,...$V14400$Z55`)이 확인됐다.
   그래서 1차로 `;`/`\r\n`/`\n` 전부를 구분자로 인정하도록 정규식을 넓혔고, 남은 조각
   안에 `$`가 2개 이상 있으면(구분자 없이 여러 문장이 붙어있다는 뜻) 그 등장 위치를
   문장 시작으로 보고 2차로 재분리한다(`re.split(r"(?=\$)", chunk)`). INAVI 쪽 결과는
   이 변경 전후로 완전히 동일함을 재검증했다(GPS 60/60, GSENSOR 600/600 그대로).
4. **NMEA 체크섬 버그와 수정**: 세그먼트 분리가 안 되던 시절엔 `*36` 뒤에 다음 세그먼트
   원문이 그대로 딸려 들어가서, `nmea_checksum_ok`가 그 잔여 텍스트 전체에 대해
   `re.fullmatch(r"[0-9A-Fa-f]{2}.*", csum)`를 걸었는데 그 잔여 텍스트 안에 `\r\n`이
   섞여 있어(정규식 `.`이 기본적으로 개행을 못 건너뜀) fullmatch가 실패, 실제 체크섬은
   맞는데도 `checksum_ok=False`로 오탐이 났다(Mercedes-Benz 샘플 59건 전부 재현·확인).
   체크섬 검증에는 원래 `*` 뒤 2자리 hex만 있으면 충분하므로 `re.fullmatch`를
   `re.match`로 바꿔 앞 2자리만 보도록 고쳤다 — 위 세그먼트 분리 수정과 별개로도 유효한
   방어적 수정(다른 벤더가 체크섬 뒤에 뭘 더 붙이는 경우에도 안전). 수정 후 Mercedes-Benz
   60/60, INAVI 60/60 모두 `checksum_ok=True`로 재검증함.
5. **`vendor_raw`(미확정 벤더 레코드)**: `classify_segment`에서 gsensor/NMEA 어느 쪽에도
   안 걸리는 세그먼트 중 `^\$(?P<tag>[A-Za-z]+)(?P<rest>.*)$` 형태(`$`로 시작하고 태그
   뒤에 필드가 옴)만 별도로 `vendor_raw`로 분류해 태그+필드+원본을 그대로 보존한다(필드는
   해석하지 않고 `confirmed: False`로 표시). Mercedes-Benz 샘플의 `$M`(20Hz 정도의
   서브레코드로 보임, 뒤쪽 6개 필드가 `YY,MM,DD,HH,MM,SS` 로컬시각(KST)과 정확히 일치 —
   GPRMC의 UTC 시각과 9시간 차이로 대조 확인함)/`$V`(값이 14300~14400 범위라 배터리
   전압 `14.xxx V`로 추정)/`$Z`(20초 사이 55→56으로 서서히 증가, 내부 온도(°C) 추정)가
   이 경로로 잡힌다 — 전부 공식 스펙이 아니라 패턴 관찰로 세운 가설이라 필드 이름 자체를
   붙이지 않았다. `$` 접두어가 없는 미확정 세그먼트(INAVI의 `CAR,...` 등)는 여전히
   `generic`으로 분류돼 `KEPT_KINDS`(`gsensor`/`gps_nmea`/`vendor_raw`)에 안 들어가고
   버려진다 — pvc1이 "GPS/속도/위치 시각화에 불필요한 나머지는 버린다"는 원래 방침은
   그대로 유지하되, "$-접두 미확정 레코드는 의미를 몰라도 원본을 잃지 않는다"는 원칙을
   추가한 것.
6. gsensor 세그먼트 필드(`gsensor<subtype>,<count>,<scale>,<x>,<y>,<z>`)는 pvc1처럼
   `field_0, field_1, ...`로 원본만 보존하는 대신, 실측 데이터로 의미를 역산해서 이름을
   붙였다(⚠ 공식 스펙 아님). `x_raw/scale = x_g` 로 g 단위 값도 같이 산출해 저장한다.
   **다만 두 필드의 의미는 아직 확정되지 않았다** — 아래 "알려진 한계" 참고. 특히
   `count`를 "뒤에 오는 값 개수"로 적었던 예전 설명은 틀렸다(신규 Ambarella 샘플이
   `gsensori,1,512,384,-35,-816` 처럼 count=1인데 뒤에 오는 값은 똑같이 4개다).
   (Mercedes-Benz 샘플은 이 표준 gsensor 포맷을 안 써서 `sensor_values.csv` 자체가
   안 생기고, 위 `vendor_raw`로만 보존된다.)
7. `timeline.csv`: GPS(1Hz)와 표준 gsensor(있는 장비만, 예: INAVI 10Hz)를 sample 단위로
   한 줄씩 합친 통합 타임라인 — GPRMC가 없는 sample은 `latitude`/`longitude` 등을
   공란으로 두고, 시각화 편의를 위한 `*_last` 컬럼에만 가장 최근 GPS 값을 그대로
   이어붙인다(값 자체를 보간하지 않음 — 원본 그대로). `vendor_raw`는 sample당 여러
   건(예: Mercedes-Benz는 sample 1개당 ~21건)이라 timeline에는 안 합치고 별도
   `vendor_raw.csv`로만 남긴다.

### 출력 폴더 구조

```
<출력폴더>/
├── track_table.csv              전체 Track: handler/이름/stsd타입/샘플수 (pvc1과 같은 형식)
├── warnings.log
└── TRACK<track_ID>_TEXT/        handler_type=="text"인 Track마다 (N=tkhd.track_ID 값
    │                              그 자체, "몇 번째 trak인지"가 아님)
    ├── index.csv                 Sample별 moof_index/traf_index/trun_index, offset/size,
    │                              dts/duration, start~end 시간, validation
    ├── coordinates.csv/.txt      GPRMC/GPGGA 인식된 것만. NMEA 필드 컬럼은 AVI 쪽
    │                              (GPS_metadata_GPRMC.py)과 동일 — magvar/magvar_dir/
    │                              mode/status_valid/parse_warnings 포함. AVI 고유 위치
    │                              컬럼(sequence/idx1_entry_offset/chunk_id) 자리에는
    │                              fMP4 고유의 sample/start_time_sec/end_time_sec이 들어감
    ├── sensor_values.csv         gsensor raw + g단위 환산값 (+ start/end 시간, ⚠ count/scale
    │                              해석은 비공식 추정) — 표준 gsensor 포맷을 쓰는 장비만
    │                              생성됨(rows 없으면 파일 자체가 안 생김)
    ├── vendor_raw.csv            의미 미확인 `$TAG,...` 벤더 레코드 원본 그대로(태그/필드/
    │                              raw, ⚠ 필드 해석 안 함) — 해당 포맷을 쓰는 장비만 생성됨
    └── timeline.csv              GPS+표준gsensor를 sample 시간 기준 한 줄로 합친 통합
                                   타임라인(시각화용, vendor_raw는 미포함)
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
   올리지 않음). `-i/--input-dir`, `-o/--output-dir`, `--pattern`으로 받아 폴더 일괄
   처리(`process_all_samples`, 이미 `Recovered_` 접두어인 파일은 재처리 스킵).
7. 임베디드 RIFF도 없고 최상위 RIFF도 1개뿐이면(이미 깨끗한 파일이거나 이 스크립트가
   다루는 손상 패턴이 아니면) 아무것도 만들지 않고 `False`를 반환 — 오탐 방지를 위해
   "일단 잘라보고 보는" 동작은 하지 않는다.

> **고친 이력**: 위 3번의 "또는 최상위 RIFF 자체가 2개 이상"은 원래 이 문서에만 적혀
> 있었고 코드에는 없었다 — `fix_blackbox_video`가 `_find_first_riff`로 맨 앞 RIFF 하나만
> 읽고 트리거를 `if not embedded: return False`로 걸어둬서, 최상위 RIFF가 2개 이상
> 이어붙은 형태(원래 슬랙 판단 기준으로 삼았던 바로 그 케이스)를 통째로 놓치고 있었다.
> `_count_top_level_riffs()`를 추가해 `integration_avi.py`의 `need_repair = bool(embedded)
> or top_count >= 2`와 기준을 일치시켰다. 같이 고친 것: 처리 대상이 `REC_*.avi`로 고정돼
> EVT_ 등을 건너뛰던 문제(→ `--pattern`, 기본 `*.avi`), 입출력 경로 하드코딩(→ CLI 인자).
> 실측 샘플 7개는 전부 최상위 RIFF가 1개(슬랙은 movi 내부 형태)라 이 경로를 타지 않으므로,
> 원본을 두 번 이어붙인 합성 파일로 검증했다 — 수정 전엔 미검출, 수정 후엔 정상 절단되고
> 복구본의 idx1 엔트리 수와 `coordinates.csv` 해시가 원본과 동일함을 확인.

**실측 검증**: `REC_20240916_172436_F.avi`에서 임베디드 RIFF 2개(`0x4CC0000`, `0x4E60000`,
각각 예전 파일 `REC_20240822_232548_R.avi`/`REC_20240908_064525_F.avi`의 JUNK 청크 파일명
흔적까지 확인됨)를 정확히 찾아냈고, 실제 유효 데이터 끝(`0x4BEE472`)과 idx1 위치
(`0x4F00000`~`0x4F1BE68`)까지 hex-editor로 직접 뜯어본 값과 정확히 일치함을 확인했다.
이 스크립트로 복구한 파일을 `GPS_metadata_avi.py`/`GPS_metadata_GPRMC.py`/
`integration_avi.py` 입력으로 넘기면 (idx1은 원래부터 예전 파일 잔재를 참조하지 않으므로)
GPS/센서 추출 결과는 리페어 전/후로 완전히 동일하다 — 이 리페어는 GPS 추출의 전제조건이
아니라 "깨끗한 재생용 사본"을 별도로 남겨주는 보너스에 가깝다.

---

## integration_avi.py — 위 네 개를 합친 통합 스크립트

`GPS_metadata_avi.py` + `GPS_metadata_GPRMC.py` + `AVI_exception_lot_RIFF.py` +
`GPS_metadata_avi_txts_record72.py`를 파일 하나로 합친 것. 여러 입력 파일을 한 번에
받아 파일마다 아래 순서로 처리한다.

72바이트 레코드 해석은 `GPS_metadata_avi_txts_record72.py`의 코드 블록을 **그대로
복사**해 갖고 있다(다른 통합 스크립트들과 같은 방식 — 한쪽을 고쳐도 다른 쪽은 안
바뀌니 주의). 붙는 지점은 세 군데다: `classify_payload`에 `record72` 분기,
`decide_stream_kind`에 `record72` 다수결, `write_decoded_outputs`에 `coordinates.*` +
`sensor_values.csv` 출력. 경과 초는 파일 전체를 모은 뒤 한 번에 펴야 해서
`extract_payload` 순회 중에는 `(seq, entry, record)`만 쌓아두고 순회가 끝난 뒤
`finevu_unwrap_elapsed`로 처리한다.

### 처리 순서

1. **슬랙 판단** (`analyze_slack` → `handle_slack`): 위 `AVI_exception_lot_RIFF.py`와 완전히
   같은 기준(임베디드 RIFF 있음/최상위 RIFF 2개 이상 → 리페어, 그 외 판단은 아래로)으로
   판단하되, 최상위 RIFF **밖**(파일 끝 이후)에 남는 트레일링 바이트도 별도로 확인한다.
   - 트레일링이 진짜 두 번째 RIFF로 시작하면 리페어 대상에 포함.
   - 트레일링이 있는데 RIFF가 아니면(예: FineVu CustomGPS 샘플들 — `JUNK` 태그로 시작하는
     수 MB짜리 완전 바이너리, NMEA 텍스트 패턴 없음) **절대 잘라내지 않고** 원본은 그대로
     둔 채 raw로 별도 보존(`trailing_unknown_data.bin` + 설명 `.README.txt`) — 실제
     데이터일 수 있는데 슬랙으로 오인해 지우는 사고를 막기 위함.
     ※ 한동안 이 블록을 FineVu의 GPS 저장 위치로 의심했는데 아니었다. 그 계열의
       GPS/충격센서는 movi 안 txts 스트림의 72바이트 레코드에 있고(위 record72 항목),
       이 블록은 그것과 무관한 용도 미상 영역이다. 보존만 하는 정책은 그대로 둔다.
   - 리페어가 필요하면 결과 폴더 안에 `<파일명>_wo_slack.avi`를 생성(임시 폴더가 아니라
     최종 산출물로 바로 남김).
   - `--dry-run`이면 이 단계도 판단 결과만 로그로 남기고 `_wo_slack.avi`/
     `trailing_unknown_data.bin`을 **만들지 않는다**(출력 폴더 자체도 안 만듦). 리페어를
     건너뛰고 원본으로 추출을 이어가는데, idx1이 애초에 현재 녹화분만 가리키므로 슬랙
     유무는 추출 결과에 영향을 주지 않아 dry-run 요약 수치는 실제 실행과 동일하다.
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

## GPS_metadata_avi_txts_record72.py — txts/dats의 72바이트 고정 이진 레코드 전용

`GPS_metadata_GPRMC.py`와 같은 방식으로 `import GPS_metadata_avi as carve` 해서
RIFF/idx1 저수준 파서(`validate_chunk`, `find_top_level_sections`, `parse_idx1`,
`detect_base_offset`, `compute_video_duration` 등)를 그대로 쓴다. 자체 파서는 없고
**레코드 해석만** 갖는다. 컨테이너에서 청크를 꺼내는 데까지는 AVI 쪽과 완전히 같은
코드를 타므로, 슬랙/base offset/OpenDML 같은 이슈는 저쪽에서 이미 처리된 상태로 온다.

### 왜 별도 경로가 필요한가

`GPS_metadata_avi.py`의 `classify_payload`는 청크를 nmea_text / generic_text /
float_vector / binary 넷으로 나눈다. 이 계열 레코드는 넷 중 어디에도 안 걸린다.

- `looks_like_text_record`: 첫 바이트가 0x00이라 본문 길이 0 → 탈락
- `try_float_vector`: 72바이트는 float 18개인데 상한이 8개라 → 탈락

그래서 `binary`로 떨어지고 `decide_stream_kind`의 80% 다수결에서 아무 kind도 못
받아 raw만 보존됐다. 데이터가 없었던 게 아니라 판정 규칙이 없었던 것이다. 이 스크립트가
다섯 번째 분류(`record72`)를 추가한다.

### 판정 방식 — 선두 서명을 안 쓰는 이유

레코드 앞 40바이트는 뷰어가 읽지 않는 구간이라 기기마다 내용이 다르다. 어떤 모델은
고정 서명 + 프레임 카운터를 넣고, X3000/X700 실측 샘플은 이 구간이 전부 0x00이다.
그래서 선두 바이트로 판정하면 모델이 바뀔 때마다 깨진다. 대신 **값의 범위**로 본다.

```
길이 >= 69                       (경과 초 오프셋 68까지 있어야 완전한 레코드)
충격센서 3축이 유한하고 |v| <= 100  (99.0/100.0 센티넬 포함)
0 <= 속도 <= 400 km/h
반구 플래그 >> 4 == 0             (2비트 필드 두 개라 상위 비트는 항상 0)
측위된 레코드면 0 <= 위도 < 9000, 0 <= 경도 < 18000  (도분 기준)
```

이 조건을 앞쪽 8개 샘플 중 80% 이상이 통과해야 그 스트림을 채택한다
(`GPS_metadata_avi.py`의 `DECODE_MIN_FRACTION`과 같은 기준).

`float_vector`(최대 8개 = 32바이트)와 길이가 겹치지 않아서 분류 순서를 앞에 둬도
기존 VUGERA SENS 판정을 가로채지 않는다 — 실측으로 AVI 샘플 7개 전부 대조해
기존 5개의 결과가 컬럼 단위로 완전히 동일함을 확인했다.

### 좌표 변환을 float32로 하는 이유

도분 → 십진 도 변환식은 전 과정이 32비트 부동소수점 연산이다. 같은 식을 float64로
계산하면 결과가 소수점 여섯째 자리에서 어긋난다(지상 거리로 약 0.2m). 그래서
`_f32()`로 매 단계 반올림을 강제하고 캐스트 위치까지 그대로 옮겼다.

```python
v = _f32(value)
q = _f32(v / 100.0)
deg_i = int(q)                    # C의 (int) 캐스트 = 0 방향 절삭
deg = _f32(float(deg_i))
minutes = _f32((v - _f32(100 * deg_i)) / 60.0)
return _f32(deg + minutes)
```

### txts와 dats

둘은 필드 배치가 같은데 좌표 해석만 다르다. txts는 도분, dats는 이미 십진 도라
변환을 걸면 안 된다. dats는 레코드 선두 7바이트가 `FF 01 00 00 0A 26 03`으로 정해져
있어 그걸로 구분한다(txts에는 그런 검사가 없다). 스트림 fccType이 `dats`인 경우도
같이 본다. dats 형식 실물 샘플은 아직 없어서 이 경로는 미검증이다.

### 측위 실패 처리

위경도가 둘 다 0.0이면 그 레코드는 측위 실패다. 좌표 0,0은 기니만 해상의 실제
좌표라 값만으로는 구분되지 않으므로 좌표/속도를 **비워두고**(0으로 채우지 않고)
`status=V`로 남긴다. `coordinates.txt`(좌표 목록)에서는 그 행이 빠지고
`coordinates.csv`에는 그대로 남는다 — 다른 경로의 status=V 처리와 같은 규칙이다.

충격센서 값 99.0(기록된 이상치)과 100.0(데이터 없음)은 실제 가속도가 아니라
센티넬이라 결측으로 빼고 `parse_warnings`에 사유를 남긴다.

### 경과 초와 절대 시각

오프셋 68의 1바이트는 1초마다 1씩 증가하는 카운터다. 이걸 파일명 시각과 합치면
레코드별 절대 시각이 나온다. 다만 0부터 시작하지 않는 자유 진행 카운터라(X3000
실측 샘플은 86에서 시작) 그대로 더하면 86초가 밀린다. 그래서 첫 값과의 차이를 쓰고
256에서 되돌아가는 것만 편다(`finevu_unwrap_elapsed`). 파일명에서 시각을 못 찾으면
`abs_time`은 공란으로 두고 경고를 남긴다.

이 값은 `start_time_sec`(영상 길이 / 레코드 수)과 독립적으로 구해지므로 서로
교차검증이 된다 — X3000은 카운터 폭 60초, 영상 길이 60.002초로 일치한다.

---

## integration_mp4.py — MP4 세 개를 합친 통합 스크립트

`GPS_metadata_fragment_iso4_Atext.py` + `GPS_metadata_mp4_pvc1_Atext.py` +
`GPS_metadata_mp4_udta_mamt_GNRMC_pmp42.py` 를 파일 하나로 합친 것. 여러 입력 파일을 한 번에
받아 파일마다 구조를 판별하고 알맞은 경로로 보낸다.

### 판별 (`probe_container`)

브랜드가 아니라 **구조**로 분기한다. 실측 ftyp이 기대와 달랐기 때문이다 — 02번 INAVI Z300은
`pvc1`이 아니라 `avc1`(영상 코덱 fourCC)이었고, 04번 Land Rover는 `mp42`, 03/05번은 둘 다
`iso4`인데 그 `iso4`는 fragmented 전용 브랜드가 아니다. 즉 브랜드로는 02번을 못 찾고
`iso4`만으로 fragmented를 단정할 수도 없다. brand/compatible_brands는 로그에만 남긴다.

1. top-level Box를 순회해서 `moof` 개수, `moov` 유무, 각 `trak`의 `hdlr.handler_type`,
   `moov/udta/mamt` 유무를 모은다.
2. **`moof >= 1`** -> `ROUTE_FRAGMENTED`.
3. `moof == 0` 이고 handler가 `text`/`sbtl`/`subt`인 trak이 있으면 -> `ROUTE_SAMPLETABLE`.
4. `moof == 0` 이고 text trak이 없는데 `moov/udta/mamt`가 있으면 -> `ROUTE_UDTA_MAMT`.
5. 셋 다 아니면 사유를 적어 SKIP(에러로 죽지 않음).

**순서가 중요하다.** fragmented MP4도 moov 안에 초기화용 trak(내용 없는 stbl)을 갖고 있어서
3번 조건에 같이 걸린다. 그래서 moof를 가장 먼저 본다 — 실제로 03/05번은 moof=60이면서
동시에 text handler trak도 갖고 있어 이 순서가 없으면 잘못된 경로로 간다.

### 루트별 처리

세 `run_*` 함수는 각 원본 스크립트의 `main`이 하던 **호출 순서를 그대로** 따른다(파일 열기와
CLI 처리만 디스패처가 대신함). 저수준 함수들은 원본에서 그대로 가져와 한 벌만 둔다.

- `run_fragmented`: `scan_top_level` -> `parse_moov` -> moof마다 `parse_moof` ->
  `save_outputs`. 시간축(tfdt+duration) 계산과 `timeline.csv`가 여기에만 있다.
- `run_sampletable`: moov마다 trak을 `parse_track` -> `print_track_table` ->
  text track마다 `extract_text_track` -> `save_track_summary`.
- `run_udta_mamt`: `locate_gps_source`로 mamt를 찾고 payload에서 `$GxRMC` 문장을 잘라
  `try_parse_rmc_sentence`로 파싱.

### 합치면서 공통화한 것 (동작이 바뀌는 부분)

- 세그먼트 분리/분류를 fragment 쪽 구현으로 통일했다. pvc1 원본은 `;`만 구분자로 썼고
  `vendor_raw` 갈래 자체가 없었는데, 공통 구현은 `;`/CRLF/`$` 재분리를 하고 `$TAG,...`를
  `vendor_raw`로 분류한다. 그래서 **루트 B에 `vendor_raw.csv`가 새로 생길 수 있다**. 02번
  샘플은 `$` 시작 세그먼트가 없어 실제로는 안 생겼고, 기존 산출물은 전부 해시 동일했다.
- gsensor 해석도 공통 구현을 쓴다. 루트 B의 `sensor_values.csv`는 기존 `field_0..N`을 유지한
  채 `count/scale/x_raw/y_raw/z_raw/x_g/y_g/z_g`가 앞에 추가된 **상위호환**이 된다.
  루트 A에는 원래 `count` 컬럼이 없었는데 이번에 추가해 두 루트를 맞췄다.
- 그 외 좌표 CSV 컬럼과 파일 구성은 각 루트의 기존 산출물을 그대로 유지한다.

### 검증 방법

단독 3종을 같은 입력에 돌려 산출물을 **파일 단위 sha256으로 대조**했다(raw_chunks/*.bin
180개 포함). 위에 적은 의도적 상위호환인 `sensor_values.csv` 하나만 다르고 나머지는 전부
일치. 누락 검사는 원본 바이트에서 `$G?RMC`/`gsensor`를 전수 검색해 sample table 참조 범위와
대조하는 방식으로 했다(아래 항목 참고).

### 슬랙 카빙 (`run_slack_carve`, 기본 꺼짐 / `--slack`으로 켬)

`--slack`을 줬을 때만 정상 경로가 끝난 뒤 `run_slack_carve`가 한 번 더 돈다.
**기본을 꺼둔 이유**: 슬랙 레코드는 예전 녹화분이라 sample table이 없고, 따라서 지금 영상의
재생 시각에 매핑할 수 없다(절대 byte offset만 남는다). 영상과 동기화한 시각화가 목적이면
쓸 수 없는 데이터인데 스캔 비용은 크다(실측 2파일 0.36s -> 1.98s). 과거 주행 이력을 캐는
포렌식 목적일 때만 켠다. 켜고 끄는 것이 정상 경로 산출물에는 영향이 없음을 바이트 단위로
확인했다. `mp4_slack_carve.py`와 같은 로직을
복사해 갖고 있고(공통 설계 원칙 참고 - import 아님), 원본은 수정하지 않고
`<out_dir>/slack/`에만 결과를 남긴다. `--no-slack`으로 끌 수 있다.

**영역 판정(`find_slack_regions`)**: Box size만 따라가며 최상위를 순회한 뒤,
(1) `free`/`skip` Box payload, (2) Box 사이 gap, (3) 마지막 Box 뒤 꼬리를 슬랙으로 본다.
문자열 검색을 쓰지 않는다. 순회 도중 size가 깨지면 거기서 멈추고 남은 뒷부분을 통째로
`trailing`으로 잡는데, Land Rover 파일이 정확히 이 경우다(mdat 뒤 16.7MB).
64바이트 미만 영역은 정렬용 `free`라 버린다.

**카빙(`carve_region`)**: 슬랙엔 sample table이 없어 offset/size를 계산할 수 없다. 그래서
`\$?[A-Z]{2}(RMC|GGA),[ -~]*` / `\$?gsensor[A-Za-z0-9]*,[ -~]*` 로 **시작점만** 찾고, 그
매치 텍스트를 정상 경로와 같은 `split_segments` -> `classify_segment` 에 넘긴다. 매치의 첫
세그먼트만 채택하고(뒤에 붙은 것들은 각자 자기 offset에서 따로 매치된다) 매치 위치가 곧
절대 offset이 되므로 hex editor로 원본 대조가 된다. checksum 검증도 정상 경로와 동일하게
하므로 우연히 생긴 바이트열은 대부분 걸러진다 - 실측 4종에서 checksum 실패 0건이었다.

**실측 결과**

| 폴더 | 영역 | 크기 | 카빙 |
|---|---|---|---|
| 02 Z300 | `free@0x19AB79C` | 2.4MB | GSENSOR 1 |
| 03 QXD8000 | `free@0x4A61338` | 7.99MB | GPS 9 + GSENSOR 90 |
| 04 Land Rover | `trailing@0x8618000` | 16.7MB | 0 |
| 05 Mercedes | `free@0x113FAC99` | 56.6MB | GPS 5 |

카빙된 GPS의 기록일이 전부 파일 자체 녹화일보다 과거였다(2024-03-12 파일 안에 2023-05-26
좌표 등). 즉 같은 저장매체에 예전에 기록됐던 주행 이력이다. AVI의 movi 내부 슬랙과 같은
현상(고정 크기 파일을 앞부분만 덮어쓰는 방식)이지만, MP4는 덮어쓰고 남은 뒷부분을 `free`
Box로 "안 쓰는 영역"이라 선언해두는 점이 다르다.

**AVI 쪽과의 차이**: `integration_avi.py`의 슬랙 처리는 *재생 가능한 깨끗한 사본을 만드는*
리페어(`_wo_slack.avi` 생성)가 목적이고, MP4 쪽은 *잔재에서 데이터를 건져내는* 카빙이
목적이다. MP4는 슬랙이 `free`로 명시돼 있어 정상 재생을 방해하지 않으므로 잘라낼 이유가 없다.

### 알려진 한계

- **gsensor g 환산은 미확정이고, 필드 의미 가설 두 개가 모두 반증됐다.**
  `x_raw/scale`로 계산한 `|(x,y,z)|`의 중앙값은 정차/정속 주행이면 중력 때문에 ~1g여야
  하는데 기기마다 다르게 나온다.

  | 기기 | count | scale | `\|v\|/scale` | `\|v\|/scale*count` | 실측 1g 카운트 |
  |---|---|---|---|---|---|
  | 02 INAVI Z300 | 4 | 512 | 0.266g | 1.064g | ~136 |
  | 03 INAVI QXD8000 | 4 | 2048 | 0.248g | 0.993g | ~508 |
  | 신규 Ambarella(avc1/fMP4) | 1 | 512 | **1.99g** | **1.99g** | ~1010 |

  - 가설 A "`scale` = 1g당 카운트"(현재 코드): 셋 다 1g가 안 나온다(0.27 / 0.25 / 1.99).
  - 가설 B "`count` = ±Ng 풀스케일": INAVI 두 기기는 1g에 맞지만 신규 기기는 count=1이라
    보정이 안 걸려 1.99g 그대로다. **기각.**
  - `count`가 "뒤에 오는 값 개수"라는 해석도 반증됐다 — `gsensori,4,2048,500,-51,-51` 과
    `gsensori,1,512,384,-35,-816` 은 둘 다 뒤에 값이 4개인데 count가 4와 1로 다르다.
  - `scale` 필드와 실측 1g 카운트의 비율도 3.76 / 4.03 / 0.507 로 일관된 관계가 없다.

  결론적으로 이 두 필드만으로 g를 유도하는 기기 공통 공식은 없다. 기존 `x_g`/`y_g`/`z_g`
  컬럼은 호환을 위해 그대로 두되 **절대값을 신뢰하지 말 것**(축 방향과 상대적 변화 추이는
  유효). 대신 아래 자가 보정 컬럼을 쓴다.

### gsensor 자가 보정 (`apply_gsensor_calibration`)

`scale` 필드 대신 **"1g에 해당하는 카운트"를 데이터 자체에서 역산**한다. 차에 고정된
센서는 중력 1g를 항상 받고, 주행 가감속은 급브레이크도 0.3g 수준에 방향이 계속 바뀌므로,
`|(x,y,z)|` 크기의 **중앙값을 1g로 본다**. 그 값으로 나눈 `x_g_cal`/`y_g_cal`/`z_g_cal` 과
기준값 `calibration_counts_per_g` 를 CSV에 추가한다(기존 컬럼은 건드리지 않는 순수 추가).
해석 가능한 레코드가 `MIN_CALIBRATION_SAMPLES`(30개) 미만이면 중앙값을 못 믿으므로 보정을
생략하고 경고를 남긴다(Z300 슬랙이 1건이라 실제로 이 경로를 탄다).

**이게 하드웨어 상수를 짚는다는 근거** — 같은 기기의 서로 다른 파일에서 같은 값이 나오고,
심지어 **몇 달 전 다른 녹화분인 슬랙에서도 같은 값**이 나온다.

| 파일 | 본체 counts/g | 슬랙 counts/g (과거 녹화분) |
|---|---|---|
| 신규 EVT_20260816_084706_F | 1023.1 | 1016.3 |
| 신규 EVT_20260816_084706_R | 1024.2 | 1009.0 |
| 신규 REC_20260816_085937_F | 1014.5 | 1018.7 |
| 신규 REC_20260816_085937_R | 1016.7 | 1014.1 |
| 신규 REC_20260816_102837_F | 1019.0 | 1014.1 |
| 신규 REC_20260816_102837_R | 1019.0 | 1015.0 |
| 03 INAVI QXD8000 | 508.5 | 503.5 |
| 02 INAVI Z300 | 136.2 | (1건이라 보정 생략) |

보정 후 축 분해도 물리적으로 맞다 — Z300/QXD8000은 `x_g_cal≈+0.97`로 x가 중력축(거의 수직
장착), 신규 기기는 `z_g_cal≈-0.82, x_g_cal≈+0.37`로 갈리는데 `atan(0.373/0.818)≈24.5°`,
앞유리 경사각과 일치한다. 보정 전에는 신규 기기가 `z_g=-1.635`(60초 내내 1.6g 가속 = 물리적
불가능)로 나왔다.

**한계**: 크기만 보정하고 장착 각도는 보정하지 않는다(중력이 여러 축에 갈린 채로 남으므로,
"전후/좌우/상하" 차량 좌표계가 필요하면 회전 보정이 별도로 필요). 영상 전체가 급가속
구간이면 중앙값 기준이 밀린다. 축별 영점 오프셋도 보지 않는다.

**AVI 쪽은 해당 없음**: AVI의 `SENS` 스트림은 카운트가 아니라 float32 벡터로 이미 g 단위에
가깝게 들어와(`|v|`가 1g 근처) 별도 보정을 붙이지 않았다.
- `probe_container`와 각 루트가 top-level을 각각 순회해서 같은 경고가 warnings.log에 두 번
  찍힐 수 있다(Land Rover 파일의 mdat 뒤 트레일링 바이트 경고). 표시상의 문제로 추출 결과에는
  영향이 없다.

## 재생 시간축 (네 경로 공통)

영상 재생에 맞춰 시각화하려면 레코드마다 "영상 몇 초 지점인가"가 있어야 한다. 경로마다
근거가 다르므로 `time_source` 컬럼에 어떤 방법으로 구한 값인지 남긴다.

| 경로 | `time_source` | 구현 | 근거 |
|---|---|---|---|
| 루트 A | `tfdt_trun` | 기존 `parse_traf` | `tfdt.baseMediaDecodeTime` + `trun` duration 누적 ÷ `mdhd.timescale` |
| 루트 B | `stts` | `parse_stts` / `build_sample_times` | `stts` (Decoding Time to Sample) ÷ `mdhd.timescale` |
| 루트 C | `gps_utc_elapsed` | `assign_utc_elapsed_times` | sample table이 없어 GPS UTC 경과초 |
| AVI | `avi_video_duration` | `compute_video_duration` / `build_avi_stream_times` | 영상 길이 ÷ 텍스트 스트림 레코드 수 |

### 왜 외부 도구(ffprobe)를 안 쓰나

ffprobe는 **컨테이너 총 길이만** 준다. "37번째 GPS 레코드가 몇 초 지점인가"는 안 알려주므로
결국 여기서 하는 것과 같은 나눗셈/보간이 필요하다. 그리고 서드파티 디코더가 손상 구간을
임의로 보정해버리면 이 프로젝트가 애써 잡아낸 이상(슬랙, 깨진 Box, Land Rover의 mdat 뒤
16.7MB 트레일링)이 가려진다. 외부 바이너리 의존성도 늘어난다. 필요한 정보는 전부 파일 안에
이미 있으므로 직접 읽는다.

"총 길이만 받아서 1초씩 증가"시키는 방식도 쓰지 않는다. GPS는 1Hz라도 gsensor는
10Hz(INAVI) / 30Hz(신규 Ambarella)라 균일 가정이 깨지고, Z300은 text track 자체가 0.1초
간격이라 순번=초가 처음부터 성립하지 않는다.

### AVI가 특수한 이유

텍스트 스트림의 `strh`가 깨져 있는 기기가 많다.

```
VUGERA MB-900SB : txts dwScale=0,   dwRate=30   -> 0으로 나누기
INAVI FXD900    : txts dwScale=100, dwRate=0    -> 0 Hz
```

반면 영상 스트림은 멀쩡하다(VUGERA 30.000fps x 1150프레임 = 38.3초, FXD900 29.970fps x
1165프레임 = 38.9초). 그래서 `영상 길이 / 텍스트 스트림 레코드 수`로 간격을 낸다. VUGERA는
txts dwLength가 영상 프레임 수와 같아 1/30초(프레임 동기), FXD900은 621 레코드라 1/16초가
나온다. `compute_video_duration`은 영상 `strh`가 실패하면 `avih`(dwMicroSecPerFrame x
dwTotalFrames)로 폴백한다.

### 검증 - GPS UTC와의 독립 대조

구조에서 뽑은 `start_time_sec`의 경과초와 GPS 문장의 UTC 경과초를 비교했다. `stts`(Z300)와
`tfdt_trun`(신규 6개)은 중앙오차 0.000초, AVI는 0.03~0.27초였고 **누적 드리프트는 없다**
(앞 1/4 평균 vs 뒤 1/4 평균 차이가 0.15초 미만). AVI의 잔여 오차는 초 단위 양자화다 -
VUGERA는 1초에 GPS 레코드가 ~30개씩 같은 UTC 값을 공유하므로 그 안에서의 위치 차이다.

QXD8000(+1.000초)과 Mercedes(-0.467초)만 어긋나는데, 추적해보면 **우리 축이 아니라 GPS
수신이 튄 것**이다.

```
[QXD8000]  우리 간격: 1.0초 x 59 (완전 균일)   GPS UTC: 1.0초 x 58 + 0.0초 x 1
[Mercedes] 우리 간격: 1.0초 x 59 (완전 균일)   GPS UTC: 0.0초 x 18 / 1.0초 x 24 / 2.0초 x 17
```

영상 동기화가 목적이면 균일한 구조 기반 축이 맞는 값이다. `gps_utc_elapsed`(루트 C)는 UTC로
만든 값이라 UTC와의 대조가 자기검증이 되지 않으므로, 60초 영상에서 0.0~60.0초가 정확히
나오는 것으로 확인했다.

### timeline.csv

네 경로 전부 같은 컬럼 구성으로 만든다(시각화 쪽이 경로마다 다른 파일을 읽지 않도록).
GPS가 안 실린 sample은 좌표를 공란으로 두고 `*_last`에만 직전 값을 이어붙인다(보간 안 함).
`x_g_cal` 계열은 센서 자가 보정이 끝난 뒤 sample 번호로 되짚어 채우므로 2-pass다.
AVI는 GPS와 G센서가 서로 다른 스트림에 있어서 `write_avi_timeline`이 재생 시각으로 붙이고,
AVI의 `SENS`는 이미 g 단위라 `*_g_cal`은 비워둔다.

## 공통 설계 원칙

- **raw는 항상 그대로 보존**한다 — 디코딩 결과가 의심스러우면 `chunks/*.bin`,
  `*_concat.bin`(AVI), `chunks/*.bin`(MP4 pvc1, `--extract` 시)으로 원본 대조 가능. AVI의
  ID_MISMATCH/SIZE_MISMATCH 엔트리도 raw는 그대로 뽑되, false positive 방지를 위해
  자동 디코딩(분류/좌표 산출)만 생략하고 `index.csv`에 사유를 남긴다.
  (예외: `GPS_metadata_fragment_iso4_Atext.py`는 raw `.bin` 저장 기능 자체가 없음 —
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
