# 언제 뭘 쓰는지

- **아무것도 모르겠으면 `integration_blackbox.py`** — 파일 시그니처를 읽어 AVI/MP4를
  판별하고 알맞은 통합 스크립트로 넘기는 최상위 진입점. AVI와 MP4를 한 번에 섞어 넘겨도 된다.

  ```
  python integration_blackbox.py -o "출력루트" "영상1.avi" "영상2.mp4" ...
  python integration_blackbox.py --detect-only *.avi *.mp4     # 판별만 (-o 없이 가능)
  ```

  ```
  integration_blackbox.py                  <- 컨테이너 판별
  ├── integration_avi.py                   <- AVI 3종 통합
  │     GPS_metadata_avi.py / GPS_metadata_GPRMC.py / AVI_exception_lot_RIFF.py
  └── integration_mp4.py                   <- MP4 3종 + 슬랙 카빙 통합
        GPS_metadata_fragment_iso4_Atext.py / GPS_metadata_mp4_pvc1_Atext.py /
        GPS_metadata_mp4_udta_mamt_GNRMC_pmp42.py / mp4_slack_carve.py
  ```

  **확장자로 판별하지 않는다.** 확장자는 언제든 바뀔 수 있고 포렌식 대상이면 더 못 믿는다.
  게다가 이 프로젝트에서 이미 "선언된 메타데이터를 믿으면 틀린다"를 겪었다(MP4 통합 때
  ftyp brand로 분기하려다 같은 `avc1`이 fragmented/non-fragmented 양쪽에 쓰이는 걸 확인).
  그래서 파일 앞부분 시그니처를 직접 읽는다.

  | 판별 | 조건 |
  |---|---|
  | AVI | offset 0 == `RIFF` 이고 offset 8 == `AVI ` |
  | MP4 | offset 4 == `ftyp` (ISO BMFF 표준) |
  | MP4 | ftyp가 없어도 첫 Box가 `moov`/`mdat`/`moof`/`free`/`skip`/`wide`/`pnot` 이면 변종으로 인정 |

  확장자와 내용이 다르면 **경고를 남기고 내용을 따른다**(확장자가 틀린 것이지 데이터가
  틀린 게 아니므로). `RIFF`인데 formType이 `WAVE`거나, 12바이트 미만이거나, 둘 다 아니면
  사유를 적고 그 파일만 건너뛴다 — 나머지는 계속 처리된다.

  공용 옵션은 `--dry-run`, `--slack`(MP4 슬랙 카빙, 기본 꺼짐), `--detect-only`. 하위 스크립트
  고유 옵션은 `--avi-opt=`/`--mp4-opt=`로 넘긴다(**반드시 `=`로 붙여 쓸 것** — 값이
  `-`로 시작하면 argparse가 옵션으로 오인한다):

  ```
  python integration_blackbox.py -o out --mp4-opt="--track-id 3" a.mp4
  python integration_blackbox.py -o out --avi-opt="--select-mode by_fcctype --fcctype txts" b.avi
  ```

  한쪽 그룹이 실패해도(예: 잘못된 하위 옵션) 다른 쪽은 정상 처리되고, 요약에 사유가 남는다.

  **검증**: 샘플 19개(AVI 7 + MP4 12)를 이 진입점으로 한 번에 돌린 결과와, `integration_avi.py`
  / `integration_mp4.py`를 따로 돌린 결과를 파일 단위 sha256으로 대조 → **산출물 16,671개
  전부 일치, 누락/추가 0건**.


- **AVI 파일이면 일단 `integration_avi.py`** — 슬랙 판단/리페어(AVI_exception_lot_RIFF.py)
  → 스트림 자동 선택/추출/디코딩(GPS_metadata_avi.py + GPS_metadata_GPRMC.py)까지 한 번에
  처리하는 통합 스크립트. strl에 스트림 이름이 있든 없든, 슬랙이 있든 없든 알아서 판단하고
  처리하므로 AVI는 기본적으로 이거 하나만 쓰면 된다. 아래 `GPS_metadata_avi.py`/
  `GPS_metadata_GPRMC.py`/`AVI_exception_lot_RIFF.py` 개별 항목은 각 기능이 내부적으로
  어떻게 동작하는지 참고용으로 남겨둔 것(단독 실행도 여전히 가능).
- **MP4 파일이면 일단 `integration_mp4.py`** — 파일 구조를 보고 아래 세 경로 중 맞는 걸
  자동으로 골라 돌리는 통합 스크립트. MP4는 기본적으로 이거 하나만 쓰면 된다. 아래
  `GPS_metadata_mp4_pvc1_Atext.py`/`GPS_metadata_mp4_udta_mamt_GNRMC_pmp42.py`/
  `GPS_metadata_fragment_iso4_Atext.py` 개별 항목은 각 경로가 내부적으로 어떻게 동작하는지
  참고용으로 남겨둔 것(단독 실행도 여전히 가능).
  - MP4(INAVI 등), moov 안에 stsc/stsz/stco 있는 일반 구조 → **GPS_metadata_mp4_pvc1_Atext.py**
  - MP4인데 text/sbtl/subt handler를 가진 Track 자체가 없고, GPS가 Track이 아니라 moov 밑
    udta/mamt 커스텀 박스 안에 `$GNRMC` 텍스트로 통째로 들어있는 경우 (Land Rover 대시캠 등)
    → **GPS_metadata_mp4_udta_mamt_GNRMC_pmp42.py**
  - Fragmented MP4(moov 대신 moof/mdat이 반복), INAVI QXD8000/Mercedes-Benz Drive View 등
    → **GPS_metadata_fragment_iso4_Atext.py**
- MP4에서 이전 녹화 잔재(free Box/파일 꼬리)에 남은 과거 GPS만 따로 건지고 싶으면
  → **mp4_slack_carve.py** (integration_mp4.py가 기본으로 같이 수행하므로 보통은 불필요)

# integration_avi.py — AVI는 이거 하나로

`GPS_metadata_avi.py` + `GPS_metadata_GPRMC.py` + `AVI_exception_lot_RIFF.py`를 합친
통합 스크립트. 파일마다 자동으로 다음 순서로 처리한다.

1. **슬랙 판단**: movi 내부에 예전 녹화 파일 잔재(임베디드 RIFF)가 있거나 최상위 RIFF가
   2개 이상이면 idx1 기준으로 실제 유효 구간만 남기고 잘라낸 `<파일명>_wo_slack.avi`를
   만든다. (자세한 판단 기준은 아래 `AVI_exception_lot_RIFF.py` 항목 참고 — 슬랙 유무가
   GPS 추출 결과 자체에는 영향을 안 준다는 것까지 검증됨.)
   - 최상위 RIFF 뒤(파일 끝 이후)에 트레일링 데이터가 있는데 RIFF가 아니면(FineVu
     CustomGPS 샘플처럼 `JUNK` 태그로 시작하는 완전 바이너리) 자르지 않고 `trailing_
     unknown_data.bin`으로 원본 그대로 별도 보존만 한다 — 실제 GPS 데이터일 수 있어서
     함부로 지우지 않음.
2. **추출**: 리페어된 파일(또는 원본)을 `GPS_metadata_avi.py`와 완전히 동일한 로직으로
   처리 — 스트림 자동 선택, 청크별 내용 기반 분류(NMEA 텍스트/float 벡터/바이너리),
   80% 다수결로 스트림 종류 확정 후 `coordinates.*`/`sensor_values.csv` 생성.

```
python integration_avi.py -o "출력루트" "영상1.avi" "영상2.avi" ...
```

출력은 `출력루트/<파일명>/` 서브폴더에 파일마다 자동 생성됨(GPS_Sample_avi가 실제 예,
아래 7개 실측 샘플로 검증):

```
GPS_Sample_avi/
├── EVT_20240618_184124_F/            ← VUGERA, strl에 GPSR/SENS 이름 있음, 슬랙 없음
│   ├── GPSR/chunks/*.bin, coordinates.csv/.txt, ...
│   ├── SENS/chunks/*.bin, sensor_values.csv, ...
│   └── stream_table.csv, index.csv, decode_detection.csv, warnings.log
├── REC_20240916_172436_F/            ← VUGERA, movi 내부에 예전 파일 잔재 있어서 슬랙 리페어 적용됨
│   ├── REC_20240916_172436_F_wo_slack.avi   ← 슬랙 제거된 재생용 사본(4.1MB 절단)
│   ├── GPSR/, SENS/, stream_table.csv, ...  ← (리페어 전/후 GPS 추출 결과 동일함을 확인)
│   └── warnings.log
├── EVT_2025_10_12_02_01_59_S/        ← INAVI, strl에 이름 없이 txts만, 슬랙 없음
│   └── TXTS/chunks/*.bin, coordinates.csv/.txt, unparsed_lines.txt, ...
└── 20241024-11h11m18s_N/             ← FineVu X3000 CustomGPS, movi 안 TEXT 스트림은 빈 더미
    ├── TEXT/chunks/*.bin (전부 0바이트 패턴, 디코딩 안 됨)
    └── trailing_unknown_data.bin(+.README.txt)  ← RIFF 끝 뒤 23MB 커스텀 바이너리, 원본 보존만
```

CLI 옵션은 `GPS_metadata_avi.py`와 동일: `--select-mode`(`auto_non_av`/`by_fcctype`/
`by_index`/`explicit`), `--fcctype`/`--index`/`--chunk-id`, `--dry-run`.

`--dry-run`은 판단/추출 로그만 찍고 **파일을 하나도 만들지 않는다** — 슬랙 리페어본
(`_wo_slack.avi`)과 `trailing_unknown_data.bin`도 마찬가지로 안 만들고, 원본 그대로
추출을 진행한다(idx1은 현재 녹화분만 가리키므로 슬랙 유무는 GPS 추출 결과에 영향 없음).

⚠ FineVu "CustomGPS" 두 샘플(X3000/X700)은 GPS 데이터가 AVI 스트림 안에 없고 RIFF 뒤에
붙는 벤더 자체 바이너리 포맷 안에 있는 것으로 보임 — NMEA 텍스트가 아니라서 이 스크립트로는
내용을 해독하지 못했고, 원본 그대로 raw로만 잘라 보존해뒀다(위 `trailing_unknown_data.bin`).

# AVI_exception_lot_RIFF.py

이제 `integration_avi.py`에 같은 기능이 통합돼 있어서 보통은 저걸 쓰면 되고, 이 파일은
그 슬랙-리페어 부분만 떼서 단독으로 쓰고 싶을 때 쓰는 스크립트다(`integration_avi.py`가
import하는 건 아니고 같은 로직을 각자 파일 안에 복사해서 갖고 있음 — 하나 고쳐도 다른
하나는 자동으로 안 바뀜, 실측으로 두 결과물이 해시까지 동일한 것만 확인해뒀다).

처음엔 "RIFF가 2개 이상 있으면(파일 끝에 슬랙이 붙는 형태로) idx1을 찾기 어려워진다"고
생각했는데, 실제 샘플(VUGERA MB-900SB, `REC_20240916_172436_F.avi`)을 hex로 직접 뜯어보니
슬랙 위치가 예상과 달랐다 — 이 카메라는 파일을 고정 크기로 미리 만들어두고 앞부분만 새
녹화로 덮어쓰는 방식이라, 슬랙은 **파일 끝이 아니라 최상위 RIFF가 선언한 movi 영역 내부에**
예전 녹화 파일의 RIFF/hdrl/JUNK(구 파일명 포함)/movi가 통째로 남아있는 형태로 나타난다.

그래서 "movi 내부"에서 임베디드 RIFF를 찾도록 다시 짰고, 판단 기준도 특정 파일 크기(예:
80MB)를 가정하지 않고 **"이 파일 자신의 최상위 RIFF가 선언한 크기"를 매번 읽어서 기준으로
삼도록 일반화**했다 — 임베디드 RIFF의 선언 크기가 (a) 그 기준값과 똑같거나, (b) 실제
남은 공간보다 커서 다 들어갈 수 없으면 "예전 파일 잔재"로 판단해서 그 이후를 idx1 기준으로
잘라낸다. 실제로 이 샘플에서 예전 파일 2개(`REC_20240822_232548_R.avi`,
`REC_20240908_064525_F.avi`)의 흔적을 정확히 찾아냈고, 잘라낸 지점이 hex-editor로 직접
확인한 값과 정확히 일치함을 검증했다. 리페어 전/후로 GPS 추출 결과(좌표/센서값)는 완전히
동일하다 — idx1은 애초에 현재 녹화분만 가리키고 있어서 이 슬랙에 영향을 안 받기 때문.

```
python -c "import AVI_exception_lot_RIFF as fix; fix.fix_blackbox_video('입력.avi', '출력.avi')"
```

```
python AVI_exception_lot_RIFF.py -i "입력폴더" -o "출력폴더" [--pattern "*.avi"]
```

폴더 일괄 처리(`process_all_samples`). 리페어가 필요 없는 파일은 아무것도 만들지 않고
스킵만 한다 — 7개 실측 샘플 중 실제로 리페어가 필요했던 건 REC 2개(F/R)뿐이었고, 나머지
5개(EVT×2, INAVI, FineVu×2)는 전부 오탐 없이 정상 스킵됨을 확인했다.

⚠ 이전 버전은 (1) 슬랙 판단을 **movi 내부 임베디드 RIFF가 있을 때만** 했고
(`if not embedded: return False`), 최상위 RIFF가 2개 이상 이어붙은 형태는 감지 자체를
안 했다 — 문서에는 "2개 이상이면 리페어"라고 적혀 있었는데 코드에 그 절반이 빠져 있었음.
(2) 처리 대상이 `REC_*.avi`로 고정돼 있어 EVT_ 등 다른 이름은 조용히 건너뛰었고,
(3) 입출력 경로가 `.`/`./Recovered_2`로 하드코딩돼 실행 위치에 따라 결과가 달라졌다.
셋 다 고쳐서 `integration_avi.py`와 판단 기준을 일치시켰다(`_count_top_level_riffs`).
RIFF 2개짜리 합성 파일로 검증: 수정 전엔 "슬랙을 발견하지 못했습니다"로 그냥 통과했고,
수정 후엔 정상 감지·절단되며 복구본의 idx1 엔트리 수(1786)와 `coordinates.csv` 해시가
원본과 완전히 동일했다.

# GPS_metadata_avi.py 기준

idx1 상대 offset 기준 자동 파싱 스크립트. idx1이 있다는 가정하에 만든거라 없으면 나가리(GPS Sample 1-3같은거)
movi 중복 제거

원래 원칙은 "raw만 뽑고 절대 디코딩 안 함"이었는데, strl에 GPSR/SENS로 이름이 나와있는 스트림도
실제로 열어보면 안에 표준 NMEA 문장(GPRMC 등)이나 고정폭 float32 벡터가 그대로 들어있는 경우가
있어서(VUGERA가 이 케이스) — **raw 보존은 그대로 하면서 동시에, 인식되면 자동으로 디코딩까지 함.**
인식 안 되는 진짜 알 수 없는 바이너리(FineVu 등)는 예전처럼 raw만 남기고 안 건드림.

```
python GPS_metadata_avi.py "영상.avi" "출력폴더"
```

폴더 구조 (출력폴더는 내가 넘긴 경로 그대로 씀, `<파일명>` 단계는 자동생성 아니고
파일 여러 개 처리할 때 안 겹치게 내가 직접 넣어준 것 — GPS_Sample_1이 실제 예, VUGERA 4개 파일):

```
GPS_Sample_1/<파일명>/
├── GPSR/ (fccHandler 기반 자동 라벨. SENS/TXTS 등도 이런 식)
│   ├── chunks/
│   │   ├── gpsr_000000.bin   ← movi 안 chunk 1개 payload 원본 그대로 (chunk 헤더 8바이트 제외)
│   │   └── ...
│   ├── gpsr_concat.bin       ← 그 스트림 전체를 idx1 순서로 이어붙인 것 (원본 그대로)
│   ├── coordinates.csv       ← payload 안에서 GPRMC/GPGGA 문장이 인식되면 자동 생성 (신규)
│   ├── coordinates.txt       ← "1. 위도, 경도" 형식 (신규)
│   └── unparsed_lines.txt    ← 텍스트이긴 한데 NMEA는 아닌 나머지 라인 원문 (신규)
├── SENS/
│   ├── chunks/, sens_concat.bin   ← 원본 그대로
│   └── sensor_values.csv          ← 12바이트=float32 3개로 떨어지고 값이 -50~50 범위면
│                                     x,y,z로 추정해서 자동 생성 (신규 — ⚠ 비공식 추정치,
│                                     제조사가 문서화해준 스펙 아님, 값 범위로만 판단한 것)
├── stream_table.csv          ← 이 AVI에 어떤 스트림이 몇 개 있는지 요약
├── index.csv                 ← 청크 하나하나의 offset/길이/검증결과 로그
├── decode_detection.csv      ← 스트림별로 TEXT/FLOAT_VECTOR/BINARY 중 뭘로 판정났는지, 근거(신규)
└── warnings.log
```

디코딩 안 되는 스트림(BINARY 판정)은 위 `coordinates.*`, `sensor_values.csv` 없이 raw만 나옴 —
이게 기존 동작이고 기본값.

Sample 1-1, 1-2만 가능

# GPS_metadata_GPRMC.py 기준

strl에 스트림 이름이 아예 없어서(그냥 txt) 뭔지 모르는 경우 전용. GPS_metadata_avi.py 를
import해서 저수준 RIFF/idx1 파싱을 그대로 재사용함 (같은 폴더에 있어야 됨).

동작 방식이 GPS_metadata_avi.py 와 다름: 스트림 후보를 몇 개 샘플링해서 "0바이트부터 통째로
텍스트 + 나머지는 전부 0x00 패딩"인 패턴인지부터 판정하고, 텍스트로 판정된 스트림만 전체를
훑어서 그 안에서 GPRMC/GPGGA 찾아 파싱함. 이 패턴에 안 걸리면 그 스트림은 통째로 건너뜀
(raw carving은 GPS_metadata_avi.py 로 따로 하라고 안내만 함).

```
python GPS_metadata_GPRMC.py "영상.avi" "출력폴더"
```

출력폴더도 내가 넘긴 경로 바로 밑에 스트림 라벨 폴더 생김 (자동으로 `<파일명>` 서브폴더
안 만듦 — GPS_Sample_6 처럼 파일 하나면 그냥 평평하게 써도 됨):

```
GPS_Sample_6/
├── stream_table.csv        ← 이 AVI에 어떤 스트림이 몇 개 있는지 (vids/txts 등)
├── text_detection.csv      ← 각 스트림이 텍스트로 판정됐는지/샘플 근거
├── warnings.log            ← 파싱 중 발생한 경고
└── TXTS/                   ← 텍스트로 판정된 스트림(01tx)의 결과 폴더
    ├── coordinates.txt      ← GPRMC만 추려서 "1. 위도, 경도" 형식 (39줄)
    ├── coordinates.csv      ← 컬럼 순서: date, utc_time, status, latitude, longitude,
    │                            speed_knots, speed_kmh, track_deg, magvar, magvar_dir,
    │                            mode, checksum_ok, status_valid, trusted, parse_warnings,
    │                            (뒤는 원본대조용) sequence,
    │                            idx1_entry_offset, chunk_id, sentence_type, raw_sentence
    │                            ※ latitude/longitude는 부호 있는 십진도 (N/E=+, S/W=-)
    │                            → N/S,E/W 글자 따로 안 남김, 이 값 그대로 지도에 찍으면 됨
    ├── unparsed_lines.txt   ← GPRMC가 아닌 나머지 텍스트 라인 원문 (gsensor 582줄)
    ├── raw_chunks/*.bin     ← 621개 레코드 각각 원본 그대로 (112바이트)
    └── raw_concat.bin       ← 위 621개를 이어붙인 것 (원본 payload 그대로)
```

# integration_mp4.py — MP4는 이거 하나로

`GPS_metadata_fragment_iso4_Atext.py` + `GPS_metadata_mp4_pvc1_Atext.py` +
`GPS_metadata_mp4_udta_mamt_GNRMC_pmp42.py` 를 합친 통합 스크립트.
`integration_avi.py`가 AVI 3종에 하는 역할을 MP4 쪽에서 한다.

```
python integration_mp4.py -o "출력루트" "영상1.mp4" "영상2.MP4" ...
python integration_mp4.py --probe-only *.mp4     # 구조 판별만(‑o 없이 가능)
python integration_mp4.py -o out --dry-run *.mp4 # 파싱은 하되 파일은 안 만듦
```

## 분기 기준을 brand가 아니라 "구조"로 잡은 이유

처음 계획은 "fragmented 여부 + 포맷(pvc1/iso4/mp42) + 텍스트 형식(Atext)"으로 나누는
것이었는데, 실측 샘플의 ftyp을 열어보니 브랜드가 기대와 달랐다.

| 폴더 | ftyp major_brand | 영상 stsd | moof | text trak | udta/mamt |
|---|---|---|---|---|---|
| 02 INAVI Z300 | **avc1** (pvc1 아님) | avc1 | 0 | ✔ | ✘ |
| 03 INAVI QXD8000 | iso4 | hvc1 | 60 | ✔ | ✘ |
| 04 Land Rover | **mp42** | avc1 | 0 | ✘ | ✔ |
| 05 Mercedes-Benz | iso4 | hvc1 | 60 | ✔ | ✘ |

`pvc1`은 실제로 `avc1`(영상 코덱 fourCC)이었다. 브랜드로 분기하면 02번은 영원히 안 잡히고,
`iso4`는 fragmented/non-fragmented 양쪽에 쓰일 수 있어 기준이 되지 못한다. 그래서 **어떤
Box가 실제로 존재하는가**만 보고 분기한다(brand는 로그/리포트용으로만 기록).

1. `moof` ≥ 1 → **fragmented_atext** (루트 A)
2. `moof` 0 + text/sbtl/subt handler trak 있음 → **sampletable_atext** (루트 B)
3. `moof` 0 + text trak 없음 + `moov/udta/mamt` 있음 → **udta_mamt** (루트 C)

순서가 중요하다. fragmented MP4도 moov 안에 초기화용 trak(빈 stbl)을 갖고 있어서 2번 조건에
같이 걸리므로 moof를 먼저 본다.

## 합치면서 공통화한 것

- Box 순회 / NMEA 파싱 / Atext 세그먼트 해석을 한 벌만 둔다. 세그먼트 분리는 상위호환인
  fragment 쪽 구현(`;` / CRLF / `$` 재분리)을 쓴다 — 그래서 **루트 B도 `$`로 시작하는 미확정
  벤더 레코드를 `vendor_raw.csv`로 보존**하게 됐다(기존 pvc1 단독 실행엔 없던 산출물이지만,
  버리던 걸 남기는 방향이라 손실 없음).
- gsensor 해석도 공통 구현을 쓴다. **루트 B의 `sensor_values.csv`는 기존 `field_0..N` 컬럼을
  그대로 유지한 채 `count/scale/x_raw/y_raw/z_raw/x_g/y_g/z_g`가 추가된 상위호환**이 된다
  (실측 확인: 기존 8컬럼 값 200행 전부 단독본과 동일).
- 좌표 CSV 컬럼과 파일 구성은 각 루트의 기존 산출물을 그대로 따른다.

## 출력 구조

`integration_avi.py`와 동일하게 입력 파일마다 서브폴더를 만든다.

```
<출력루트>/<파일명>/
├── 루트 A: track_table.csv, warnings.log
│          TRACK<track_ID>_TEXT/{index,coordinates,sensor_values,vendor_raw,timeline}.csv
│                              coordinates.txt
├── 루트 B: track_table.csv, warnings.log
│          TRACK<N>_TEXT/{index,coordinates,sensor_values,vendor_raw,
│                         other_segments_unparsed,keyword_hits}.csv, coordinates.txt
│                        (--extract 주면 chunks/*.bin)
└── 루트 C: warnings.log
           GPS_GNRMC/{coordinates.csv,coordinates.txt,unparsed_lines.txt,
                      raw_concat.bin,raw_chunks/*.bin}
```

## 실측 검증 (GPS Sample 2/3/4/5, 6개 파일)

| 파일 | 판별된 route | GPS | GSENSOR | VENDOR_RAW |
|---|---|---|---|---|
| 02 EVT_2019_12_18_01_22_41_F | sampletable_atext | 20 | 200 | 0 |
| 03 REC_20240312_082217_F | fragmented_atext | 60 | 600 | 0 |
| 04 20250901_204119D | udta_mamt | 60 | 0 | 0 |
| 04 20250901_215628D | udta_mamt | 60 | 0 | 0 |
| 04 20250901_215728D | udta_mamt | 60 (fix 없음 24 포함) | 0 | 0 |
| 05 20240411_144016E | fragmented_atext | 60 | 0 | 1277 |

- 단독 스크립트 3종의 산출물과 **파일 단위 sha256 대조**: `sensor_values.csv`(위에 적은
  의도적 상위호환) 하나만 빼고 전부 일치.
- **누락 0건**: 원본 바이트에서 `$G?RMC`/`gsensor` 문자열을 전수 검색해 sample table이
  참조하는 범위와 대조. 참조되지 않은 레코드(QXD8000 RMC 8건, Mercedes 5건, Z300 gsensor
  1건)는 전부 **어떤 mdat 밖**에 있고 **파일 자체 녹화일보다 과거 날짜**였다 → 이전 녹화
  잔재(슬랙)이지 누락이 아니다. 자세한 건 아래 "MP4에도 슬랙이 있다" 참고.
- **값 검증**: 좌표에서 계산한 이동속도 vs 기록된 `speed_kmh` 중앙 오차 0.21~4.64 km/h,
  UTC 시각 전부 단조 증가, checksum 실패 0건, 좌표는 전부 한국 범위.

## 슬랙 카빙 (기본 꺼짐, `--slack`으로 켬)

`--slack`을 주면 정상 경로 추출이 끝난 뒤 **컨테이너가 참조하지 않는 영역**을 추가로 훑어
GPS/G센서를 카빙하고 `<출력폴더>/slack/` 에 따로 남긴다. 원본 파일은 수정하지 않는다.
같은 로직을 단독 스크립트 **`mp4_slack_carve.py`** 로도 빼놨다(아래 항목 참고).

⚠ **기본을 꺼둔 이유**: 슬랙에서 나오는 건 전부 *예전 녹화분*이라 **지금 영상의 재생 시각에
매핑할 수 없다**(sample table이 없는 영역이므로 절대 byte offset만 남는다). 영상 재생과
동기화해서 속도/가속도를 보여주는 용도라면 쓸 수 없는 데이터다. 게다가 `free` 영역이 수십
MB라 스캔 비용도 크다(실측 2파일 기준 0.36초 -> 1.98초). **과거 주행 이력을 캐는 포렌식
목적일 때만 켜면 된다.**

# mp4_slack_carve.py — MP4 슬랙 카빙 (단독)

```
python mp4_slack_carve.py -o "출력루트" "영상1.mp4" ...
python mp4_slack_carve.py --regions-only "영상.mp4"   # 슬랙 영역 목록만
```

## 슬랙을 어디로 보는가

Box size를 따라가며 최상위를 순회한 뒤, **컨테이너가 "안 쓴다"고 선언했거나 아예 선언조차
안 한 영역**을 슬랙으로 본다. 문자열 검색으로 영역을 찾지 않는다.

1. `free` / `skip` Box의 payload — 파일이 직접 "이 영역은 데이터가 아니다"라고 선언한 곳
2. Box와 Box 사이의 gap — 선언이 끊긴 구간
3. 마지막 Box 뒤의 꼬리 gap — 박스 순회로 소비되지 않고 남은 파일 끝

실측 4종에서 이 규칙으로 전부 잡힌다:

| 폴더 | 슬랙 영역 | 크기 | 카빙 결과 |
|---|---|---|---|
| 02 INAVI Z300 | `free@0x19AB79C` | 2.4MB (8.3%) | GSENSOR 1건 |
| 03 INAVI QXD8000 | `free@0x4A61338` | 7.99MB (9.3%) | **GPS 9건 + GSENSOR 90건** |
| 04 Land Rover | `trailing@0x8618000` | 16.7MB (10.6%) | 없음 |
| 05 Mercedes-Benz | `free@0x113FAC99` | 56.6MB (16.4%) | **GPS 5건** |

04번만 `free`가 없고 마지막 mdat 뒤 꼬리 영역이라 3번 규칙으로 잡힌다.

## 카빙 방법과 신뢰도

슬랙에는 sample table이 없어 offset/size를 계산할 방법이 없다. 그래서 **찾는 방법만** 다르고
(패턴으로 시작점을 찾음), 해석·검증·출력 컬럼은 정상 경로와 **완전히 같은 파서**를 쓴다
(`split_segments` → `classify_segment` → `try_parse_nmea`, checksum 검증 포함).

실측 결과 **checksum 실패 0건** — 우연히 만들어진 바이트열이 아니라 진짜 NMEA 문장이라는 뜻.
그리고 카빙된 GPS의 기록일이 전부 파일 자체 녹화일보다 **과거**였다:

```
REC_20240312_082217_F.mp4 (녹화 2024-03-12)
  0x04A6B2F9 [free] 2024-03-09 09:07:56 37.505475,126.778933   0.0 km/h  cs=True
  0x04A7BA21 [free] 2024-02-24 10:27:35 37.497624,126.756001   0.0 km/h  cs=True
  0x04AA3BD1 [free] 2024-02-11 08:30:56 37.499096,126.775774   0.0 km/h  cs=True
  0x04AF0645 [free] 2023-12-25 20:31:24 37.505434,126.778829   0.0 km/h  cs=True
  0x04C7ABAD [free] (fix 없음: GPRMC,,V,,,,,,,,,,N,V*29)
  0x04EC04A7 [free] 2023-05-26 05:45:10 36.836781,127.175034  65.2 km/h  cs=True
  ...
20240411_144016E.MP4 (녹화 2024-04-11)
  0x11401A33 [free] 2024-04-08 22:33:18 37.237074,126.980263  cs=True
  0x11444EF2 [free] 2023-03-04 03:01:29 37.189292,127.090017  cs=True
  ... (총 5건)
```

즉 이 기기가 **과거에 다녔던 경로**가 남아 있는 것이다. 2023-05-26 건은 65~67 km/h로
연속 3초치가 이어져 나와 실제 주행 구간임을 알 수 있다.

## 출력 구조

```
<출력루트>/<파일명>/slack/
├── slack_regions.csv        영역별 kind/start/end/size와 거기서 나온 레코드 수
├── slack_coordinates.csv    정상 coordinates.csv와 같은 NMEA 컬럼 + slack_region,
│                            absolute_offset (시간축 매핑은 불가하므로 offset만)
├── slack_coordinates.txt    "1. 위도, 경도" (좌표가 있을 때만 생성)
├── slack_sensor_values.csv  gsensor 레코드
├── README.txt               이 데이터를 어떻게 해석해야 하는지 주의사항
└── warnings.log
```

## 검증

`integration_mp4.py`가 만든 `slack/` 산출물과 단독 `mp4_slack_carve.py` 산출물을 파일 단위
sha256으로 대조 → **18개 전부 일치**. 슬랙 카빙을 붙인 뒤 정상 경로 산출물 **211개도 전부
이전과 동일**(회귀 없음). `--slack`을 안 주면 `slack/` 폴더 자체가 안 생기고(정상 산출물은 켰을 때와 바이트 단위로
동일함을 확인), `--dry-run`이면 파일을 하나도 만들지 않는다.

# GPS_metadata_mp4_pvc1_Atext.py 기준

AVI 두 개랑 완전히 다른 컨테이너(MP4/ISO BMFF, moov→trak→mdia→hdlr→minf→stbl
구조)라 별도 스크립트. RIFF/idx1 파싱을 재사용하는 위 두 개와 달리 이건
box size 기반으로 moov/trak을 직접 순회해서 `handler_type`이 `text`/`sbtl`/`subt`인 Track을
찾고, `stsd/stsc/stsz/stco(or co64)`를 조합해서 각 Sample의 절대 offset/size를
계산하는 방식(문자열 검색 안 씀 — mdat 안 바이너리에 우연히 "moov" 같은 문자열이
섞여 있을 수 있어서). INAVI Z300 파일 기준으로 검증했지만 이 box 구조 자체는
MP4 표준이라 같은 계열 다른 장비 MP4에도 그대로 적용될 걸로 봄.

Sample 안 내용물은 세미콜론(;)으로 구분된 레코드고(`gsensor...;GPRMC...;CAR...`),
GPS 없이 촬영된 파일이면 GPRMC 세그먼트가 아예 안 나오고 gsensor/CAR만 나옴 —
자동으로 있는 것만 뽑히니까 파일마다 어떤 CSV가 생기는지는 다를 수 있음.

```
python GPS_metadata_mp4_pvc1_Atext.py "영상.mp4" "출력폴더"
python GPS_metadata_mp4_pvc1_Atext.py "영상.mp4" "출력폴더" --list-tracks   # Track 목록만
python GPS_metadata_mp4_pvc1_Atext.py "영상.mp4" "출력폴더" --dry-run      # 파일 미생성, 로그만
python GPS_metadata_mp4_pvc1_Atext.py "영상.mp4" "출력폴더" --extract      # Sample 원본도 .bin으로
python GPS_metadata_mp4_pvc1_Atext.py "영상.mp4" "출력폴더" --track 3      # 특정 text Track 번호만
```

출력폴더도 내가 넘긴 경로 바로 밑에 생김 (GPS_Sample_2가 실제 예):

```
GPS_Sample_2/
├── track_table.csv               ← 이 MP4에 Track이 몇 개, 각각 handler(vide/soun/text)/
│                                     이름/stsd 타입/Sample 개수 요약
├── warnings.log                  ← box 경계 초과, stsc/stsz 불일치, offset 범위초과 등 경고
└── TRACK{N}_TEXT/                ← 지원 text/subtitle handler(text/sbtl/subt) Track마다 하나 (N=전체 Track
    │                                 순번, vide/soun 포함해서 센 번호라 3부터 시작할 수 있음)
    ├── index.csv                 ← Sample 하나하나의 chunk/offset/size/검증결과 로그
    ├── coordinates.csv           ← GPRMC/GPGGA 인식된 것만 자동 생성 (GPS 없는 파일이면 안 생김)
    │                                 NMEA 필드 컬럼(date~raw_sentence)은 AVI 쪽과 동일하고,
    │                                 AVI 고유 위치 컬럼(sequence/idx1_entry_offset/chunk_id)
    │                                 자리에 MP4 고유의 `sample`(Sample 번호)이 들어감
    ├── coordinates.txt           ← "1. 위도, 경도" 형식
    ├── sensor_values.csv         ← gsensor 세그먼트 원본 필드 그대로 (field_0, field_1, ...
    │                                 — ⚠ 각 필드가 실제로 뭘 의미하는지는 공식 스펙이 아니라
    │                                 모름, 값만 원본 그대로 옮긴 것)
    ├── other_segments_unparsed.csv ← gsensor도 GPRMC도 아닌 세그먼트(CAR,... 등) 원문+필드
    │                                 그대로, 의미 단정 안 함
    ├── keyword_hits.csv          ← gps/GPS/NMEA/latitude/speed 등 키워드가 그 Sample 텍스트
    │                                 안에 있었다는 것만 표시 (후보 표시일 뿐 해석 아님)
    └── chunks/*.bin               ← --extract 줬을 때만 생김, Sample 원본 그대로 개별 저장
```

# GPS_metadata_mp4_udta_mamt_GNRMC_pmp42.py 기준

pvc1이랑 정반대 케이스 전용. non-fragmented MP4(moov 있음)인데 `moov`의 모든 trak을 다
확인해도 text/sbtl/subt handler를 가진 Track이 아예 없는 장비가 있음(Land Rover
대시캠 등) — 이런 파일은 GPS(`$GNRMC`)가 Sample Table이 아니라 `moov → udta → mamt`
(커스텀 User Data 박스) 안에 그냥 NMEA 텍스트로 나열돼 있음. 다른 스크립트 import 없이
혼자 완결된 파일(box 순회 + NMEA 파싱 다 자체 구현).

문장 탐색 정규식이 처음엔 실제 샘플에서 관찰된 `$GN`/`$GP`만 잡도록 짜여 있어서
GLONASS/Galileo/BeiDou 등 다른 talker(`$GLRMC`, `$GARMC`, `$BDRMC` ...)를 쓰는 멀티-GNSS
장비 파일은 조용히 못 잡는 문제가 있었음 — talker 2글자는 뭐든 매치하도록 일반화함
(`$[A-Z]{2}RMC`). 기존 Land Rover 샘플 3개는 결과가 정규식 일반화 전/후 완전히 동일함을
해시로 재검증했고, `$GLRMC`/`$GARMC` 합성 데이터로 실제로 잡히는 것도 확인함.

파일이 이 케이스가 맞는지부터 자동으로 확인함 — moof만 있고 moov가 없으면(fragmented),
text 계열 Track이 하나라도 있으면(pvc1 케이스), udta/mamt가 없으면 각각 이유를 출력하고
그 파일은 건너뜀(에러로 안 죽음). 그래서 여러 파일을 한 번에 넘겨도 케이스 아닌 파일만
자동으로 스킵되고 나머지는 정상 처리됨.

```
python GPS_metadata_mp4_udta_mamt_GNRMC_pmp42.py "출력폴더" "영상1.mp4" "영상2.mp4" ...
```

⚠ 다른 스크립트랑 인자 순서가 다름 — **출력폴더가 먼저, 입력 파일(들)이 뒤**이고 입력을
여러 개 한 번에 받음(파일마다 자동으로 서브폴더 생성).

출력폴더 구조(GPS_Sample_4가 실제 예 — Land Rover 대시캠 3개 파일, 각 60초/GNRMC 60개):

```
GPS_Sample_4/
├── 20250901_204119D/
│   └── GPS_GNRMC/
│       ├── coordinates.csv     ← GPS_metadata_GPRMC.py와 컬럼 동일(date, utc_time, status,
│       │                          latitude, longitude, speed_knots, speed_kmh, track_deg,
│       │                          magvar, magvar_dir, mode, checksum_ok, status_valid,
│       │                          trusted, parse_warnings, sequence, idx1_entry_offset,
│       │                          chunk_id, sentence_type, raw_sentence) — 단, AVI 전용
│       │                          개념이라 대응 안 되는 두 컬럼만 의미를 바꿈:
│       │                          idx1_entry_offset → 문장이 시작하는 파일 내 절대 byte offset,
│       │                          chunk_id → 항상 고정값 "mamt"
│       ├── coordinates.txt     ← "1. 위도, 경도" 형식
│       ├── unparsed_lines.txt  ← 필드 파싱이 실패한 손상 문장 원문
│       │                          (status=V는 여기 안 옴 — coordinates.csv에 행으로 남음)
│       ├── raw_chunks/*.bin    ← 문장 1개당 1개 원본
│       ├── raw_concat.bin      ← 찾은 순서대로 이어붙인 원본
│       └── warnings.log
├── 20250901_215628D/GPS_GNRMC/   ← 위와 동일 구조
└── 20250901_215728D/GPS_GNRMC/   ← 위와 동일 구조 (구간 중 24초 GPS 끊김 → coordinates.csv에
                                     status=V/좌표 공란 행 24개로 남음, coordinates.txt는 36줄)
```

# GPS_metadata_fragment_iso4_Atext.py 기준

GPS_metadata_mp4_pvc1_Atext.py 랑 같은 계열 장비(Ambarella 기반, Atext 트랙에 GPS/센서를
같이 실어보내는 방식)를 다루지만, 컨테이너 구조가 다름 — moov 안에 stsc/stsz/stco 같은 일반
Sample Table이 아예 없고 Fragmented MP4(ftyp major_brand=iso4, `moof`+`mdat`이 파일 끝까지
반복되는 구조)라서 위 스크립트로 돌리면 `Track #3(text): Chunk offset을 하나도 못 구함` 경고만
뜨고 아무것도 안 뽑힘. INAVI QXD8000(WHS4_Blackbox GPS Sample 3, `REC_20240312_082217_F.mp4`)과
Mercedes-Benz Drive View(GPS Sample 5, `20240411_144016E.MP4`) 둘 다 이 케이스로 검증됨.

라이브러리 없이 직접(struct/seek/read) `moof → traf → tfhd/tfdt/trun`을 box size 기반으로
순회해서 text Track(`moov/trak/mdia/hdlr`의 handler_type==text로 식별, track_ID는 "몇 번째
trak"이 아니라 tkhd.track_ID 값 그 자체) Sample의 절대 offset/size를 계산하고,
tfdt(`baseMediaDecodeTime`) + duration으로 영상 재생 시간(초) 구간까지 계산함.
duration/size는 `trun → tfhd → trex(moov/mvex)` 순으로 fallback.

Atext payload 해석(길이 프리픽스 검증, gsensor/GPRMC 정규식, NMEA 파싱)은
GPS_metadata_mp4_pvc1_Atext.py 로직을 기반으로 하되, 장비마다 세그먼트 구분자와 내용 포맷이
다르다는 게 Mercedes-Benz 샘플로 드러나서 일반화함:

- **세그먼트 구분자**: INAVI는 `;`(`gsensor...;GPRMC...;CAR...`)를 쓰는데 Mercedes-Benz는
  `\r\n`을 쓰고(`$GNRMC,...*36\r\n$M4,...`), 그마저도 그 뒤 서브레코드들은 구분자 없이
  `$`로 시작하는 문장이 그냥 이어붙어 있음(`$M4,...$M4,...$V14400$Z55`). `split_segments`가
  `;`/`\r\n`/`\n` 전부를 1차 구분자로 인정하고, 남은 조각 안에 `$`가 2개 이상 있으면 그
  등장 위치를 문장 시작으로 보고 2차로 재분리하도록 고침 — INAVI 결과는 그대로 유지하면서
  Mercedes-Benz도 처리되게 됨.
- **NMEA 체크섬 버그**: 구분자를 못 찾던 상태에선 `*36` 뒤에 다음 세그먼트 원문이 그대로
  딸려 들어가 `nmea_checksum_ok`가 그 잔여 텍스트까지 포함해서 `re.fullmatch`를 걸었고,
  그 안에 `\r\n`(정규식 `.`이 기본적으로 매칭 못 함)이 섞여 있어 실제 체크섬이 맞는데도
  `checksum_ok=False`로 오탐이 났음(Mercedes-Benz 샘플 59건 전부 재현). 체크섬 뒤 2자리
  hex만 보면 되므로 `re.fullmatch` → `re.match`로 수정 — 위 구분자 수정과 별개로도 유효한
  방어적 수정(다른 벤더가 체크섬 뒤에 뭘 더 붙이더라도 안전).
- **미확정 벤더 레코드(`vendor_raw`)**: `$TAG,필드...` 형태인데 의미를 모르는 세그먼트
  (Mercedes-Benz의 `$M`/`$V`/`$Z` 등 — `$M`은 20Hz 정도의 서브샘플로 보이고 뒷부분 6개
  필드가 로컬 타임스탬프(KST)와 일치, `$V`는 값 범위상 배터리 전압(예: 14400→14.400V),
  `$Z`는 서서히 증가하는 값이라 내부 온도(°C)로 추정되나 전부 공식 스펙 없이 패턴만 보고
  추정한 것이라 필드를 해석하지 않고 태그+원본 그대로 `vendor_raw`로 보존함(⚠ 미확정).
  기존처럼 `;`구분 + 접두어 없는 `CAR,...` 같은 세그먼트(INAVI)는 여전히 `generic`으로
  분류돼 버려짐 — `vendor_raw`는 `$`로 시작하는 미확정 세그먼트만 해당.

```
python GPS_metadata_fragment_iso4_Atext.py "영상.mp4" "출력폴더"
python GPS_metadata_fragment_iso4_Atext.py "영상.mp4" "출력폴더" --list-tracks   # Track 목록만
python GPS_metadata_fragment_iso4_Atext.py "영상.mp4" "출력폴더" --dry-run      # 파일 미생성, 콘솔 출력만
python GPS_metadata_fragment_iso4_Atext.py "영상.mp4" "출력폴더" --track-id 3   # text handler Track이 여러 개일 때 지정
python GPS_metadata_fragment_iso4_Atext.py "영상.mp4" "출력폴더" --max-print 20 # 콘솔 출력 개수 제한(CSV는 항상 전체 기록)
python GPS_metadata_fragment_iso4_Atext.py "영상.mp4" "출력폴더" --debug       # Box/Tfhd/Tfdt/Trun 값까지 콘솔에 출력(hex editor 대조용)
```

출력폴더 구조(GPS_Sample_3가 실제 예, `REC_20240312_082217_F.mp4` 기준 — 60초 분량, 600 sample):

```
GPS_Sample_3/
├── track_table.csv            ← 이 MP4에 Track이 몇 개, 각각 handler(vide/soun/text)/이름/
│                                  stsd 타입(hvc1/sowt/text 등)/Sample 개수 요약
├── warnings.log                ← box 경계 초과, tfhd/trun 불일치, offset 범위초과 등 경고
├── console_output.txt          ← (실행 결과를 리다이렉트해서 남긴 것) Sample 하나하나의 상세 로그
└── TRACK{track_ID}_TEXT/       ← text handler Track 1개당 폴더 (N=tkhd.track_ID 값 그대로,
    │                               "몇 번째 trak인지"가 아님)
    ├── index.csv                ← Sample마다 moof_index/traf_index/trun_index, absolute_offset,
    │                                size, dts, duration, start_time_sec, end_time_sec, validation
    ├── coordinates.csv          ← GPRMC/GPGGA 인식된 것만. NMEA 필드 컬럼(date~raw_sentence)은
    │                                AVI 쪽과 동일하고, AVI 고유 위치 컬럼(sequence/
    │                                idx1_entry_offset/chunk_id) 자리에 fMP4 고유의
    │                                sample/start_time_sec/end_time_sec이 들어감.
    │                                magvar/magvar_dir은 QXD8000 샘플에서 실제로 값이
    │                                채워지는 유일한 케이스(60/60 = `6.1`,`W`)
    ├── coordinates.txt          ← "1. 위도, 경도" 형식
    ├── sensor_values.csv        ← gsensor 값(x_raw/y_raw/z_raw/scale + x_g/y_g/z_g 환산값,
    │                                start_time_sec 포함 — count/scale 필드 의미는 실측 데이터로
    │                                역산한 것, 공식 스펙 아님 ⚠) — 표준 gsensor 포맷을 쓰는
    │                                장비(INAVI 등)에만 생김, 없으면 파일 자체가 안 생성됨
    ├── vendor_raw.csv            ← 의미 미확인 벤더 고유 `$TAG,...` 레코드 원본 그대로
    │                                (Mercedes-Benz `$M`/`$V`/`$Z` 등, ⚠ 필드 해석 안 함) —
    │                                해당 포맷을 쓰는 장비에만 생김
    └── timeline.csv             ← GPS(1Hz)+G센서(gsensor 있는 장비만)를 sample 시간 기준 한
                                     줄로 합친 통합 타임라인(시각화용). latitude/longitude/
                                     speed_kmh 등은 GPRMC가 실제로 실려온 sample에만 값이
                                     채워지고 나머지는 공란, `*_last` 컬럼은 가장 최근 GPS
                                     값을 그대로 이어붙인 것(보간 안 함)
```

Mercedes-Benz 샘플(GPS Sample 5)은 gsensor 표준 포맷이 없어서 `sensor_values.csv`는 안 생기고
대신 `vendor_raw.csv`에 60 sample × 약 21개(≈20개 `$M` + `$V` 1 + `$Z` 1)씩, 총 1,277건이
쌓임 — `coordinates.csv`는 60/60 checksum_ok=True로 INAVI와 동일하게 정상 동작.

MP4가 Fragmented가 아니거나(moof가 하나도 없음), text handler Track을 못 찾으면 그 자리에서
바로 경고 찍고 종료함 — GPS_metadata_mp4_pvc1_Atext.py 쪽을 대신 쓰라고 안내 메시지 남김.

# 재생 시간축과 timeline.csv (시각화용)

영상 재생에 맞춰 GPS/속도/가감속을 보여주려면 "이 레코드가 영상 몇 초 지점인가"가 필요하다.
**네 경로 전부** `start_time_sec` / `end_time_sec` / `time_source` 컬럼과 통합
`timeline.csv`를 만든다. 시각화 쪽은 `timeline.csv` 하나만 읽으면 된다.

| 경로 | `time_source` | 근거 |
|---|---|---|
| 루트 A (fragmented) | `tfdt_trun` | `tfdt.baseMediaDecodeTime` + `trun` duration ÷ `mdhd.timescale` |
| 루트 B (sample table) | `stts` | `stts` sample delta ÷ `mdhd.timescale` |
| 루트 C (udta/mamt) | `gps_utc_elapsed` | sample table이 없어 GPS UTC 경과초 |
| AVI | `avi_video_duration` | 영상 길이 ÷ 텍스트 스트림 레코드 수 |

**외부 도구(ffprobe 등)를 쓰지 않는다.** 그건 컨테이너 총 길이만 주지 "37번째 GPS가 몇 초
지점인가"는 안 알려줘서 결국 보간이 필요하고, 서드파티 디코더가 손상 구간을 임의로
보정하면 우리가 잡아낸 이상이 가려진다. 위 정보는 전부 파일 안에 이미 있다.

**AVI만 텍스트 스트림의 `strh`를 못 쓴다** — 실측상 깨져 있다(VUGERA `dwScale=0`,
INAVI FXD900 `dwRate=0`). 대신 멀쩡한 영상 스트림 길이를 레코드 수로 나눈다.

## 검증 — 구조 기반 시간축 vs GPS UTC 경과초 (독립 대조)

| 파일 | source | 중앙오차 | 드리프트 |
|---|---|---|---|
| Z300 | `stts` | 0.000s | 없음 |
| 신규 Ambarella 6개 | `tfdt_trun` | 0.000s | 없음 |
| VUGERA AVI | `avi_video_duration` | 0.233s | 없음 |
| FXD900 AVI | `avi_video_duration` | 0.029s | 없음 |
| QXD8000 / Mercedes | `tfdt_trun` | 1.000s / 0.867s | **GPS 쪽 문제** |

마지막 두 개는 우리 축이 아니라 **GPS 수신이 튄 것**이다. 우리가 만든 간격은 완전 균일한데
GPS UTC가 불규칙하다:

```
[QXD8000]  우리 간격: 1.0초 x 59 (균일)   GPS UTC: 1.0초 x 58 + 0.0초 x 1  (한 번 반복)
[Mercedes] 우리 간격: 1.0초 x 59 (균일)   GPS UTC: 0.0초 x 18 / 1.0초 x 24 / 2.0초 x 17
```

영상 동기화가 목적이면 균일한 구조 기반 축이 맞는 값이고, 튀는 쪽은 GPS다.
AVI의 잔여 오차(0.03~0.27초)는 드리프트가 아니라 초 단위 양자화다 — VUGERA는 1초에 GPS
레코드가 ~30개씩 같은 UTC 값을 공유하므로 그 안에서의 위치 차이일 뿐이고, 파일 끝까지
누적되지 않는다(앞 1/4 평균 -0.007s vs 뒤 1/4 평균 -0.108s).
`gps_utc_elapsed`(루트 C)는 UTC로 만든 값이라 UTC와의 대조가 자기검증이 되지 않는다 —
대신 60초 영상에서 0.0~60.0초가 정확히 나오는 것으로 확인했다.

## timeline.csv 컬럼 (네 경로 공통)

```
sample, start_time_sec, end_time_sec, time_source,
latitude, longitude, speed_kmh, track_deg, gps_date, gps_utc_time, gps_checksum_ok,
latitude_last, longitude_last, speed_kmh_last,      <- 직전 GPS 값 이어붙임(보간 아님)
x_g, y_g, z_g, x_g_cal, y_g_cal, z_g_cal
```

GPS가 안 실린 sample은 `latitude` 등이 공란이고 `*_last`에만 직전 값이 들어간다.
급가속/급감속은 `x_g_cal`/`y_g_cal`/`z_g_cal`(자가 보정된 g)을 쓰면 된다.
AVI의 `SENS`는 이미 g 단위 float32라 `*_g`에 그대로 들어가고 `*_g_cal`은 비어 있다.

# 공통 주의사항

- GPRMC/GPGGA 파싱은 NMEA 0183 필드 형식을 기준으로 하되, 손상 레코드 예외처리와 좌표/방향 범위 검증을 추가함. checksum/status는 별도 기록하며 `trusted` 값으로 1차 신뢰 여부를 확인할 수 있음.
- **`status` 컬럼과 `mode` 컬럼은 다른 필드다.** `status`(RMC 2번 필드)는 이 문장을 써도
  되냐는 O/X — `A`=유효, `V`=무효(그 순간 위성 못 잡음, 위경도 필드가 빈 칸으로 옴).
  `mode`(RMC 12번 필드, NMEA 2.3~)는 fix를 *어떻게* 구했냐 — `A`=자립측위, `D`=DGPS 보정,
  `E`=추측항법, `M`=수동입력, `S`=시뮬레이터, `N`=무효. fix가 없으면 보통 `V`+`N`이 세트로 나온다.
- **`status=V` 행도 `coordinates.csv`에 남는다** (5개 스크립트 공통). `latitude`/`longitude`/
  `speed_*`만 공란이고 `date`/`utc_time`/`status=V`/`mode=N`/`status_valid=False`/
  `trusted=False`/`checksum_ok`/`raw_sentence`/offset은 채워진다 — 시계열에서 "GPS가 끊긴
  구간"과 "애초에 데이터가 없는 구간"을 구분하기 위함. 좌표 목록인 `coordinates.txt`에는
  당연히 안 들어간다(그래서 CSV 행수 > txt 줄수가 될 수 있음). 위경도 필드에 값은 있는데
  파싱이 안 되는 진짜 손상 문장은 예전처럼 `unparsed_lines.txt`로만 간다.
- **gsensor는 `x_g_cal`/`y_g_cal`/`z_g_cal`을 써라.** 기존 `x_g`/`y_g`/`z_g`는 레코드의
  `scale` 필드를 "1g당 카운트"로 가정하고 나눈 값인데, 그 가정이 기기마다 안 맞는다
  (같은 정차/정속 구간에서 0.27g / 0.25g / 1.99g로 제각각). 그래서 `scale` 대신 **1g에
  해당하는 카운트를 데이터에서 역산**해서(중력은 항상 걸려 있으므로 `|(x,y,z)|`의 중앙값을
  1g로 봄) 나눈 `*_g_cal` 컬럼과, 그때 쓴 기준값 `calibration_counts_per_g`를 같이 넣는다.
  기존 컬럼은 호환을 위해 그대로 둔다(순수 추가).
  검증: 같은 기기의 다른 파일들에서 1023 / 1024 / 1015 / 1017 / 1019 / 1019로 일치하고,
  **몇 달 전 녹화분인 슬랙에서도** 1016 / 1009 / 1019 / 1014 / 1014 / 1015로 같은 값이
  나온다 — 클립별 잡음이 아니라 하드웨어 상수를 짚는다는 뜻. 보정 후 축 분해도 맞는다
  (신규 기기 `z=-0.82, x=+0.37` → 장착 기울기 24.5°, 앞유리 각도와 일치).
  ⚠ 크기만 보정하고 장착 각도는 안 고친다. 해석 가능한 레코드가 30개 미만이면 보정을
  생략하고 `*_g_cal`을 공란으로 둔다. AVI의 `SENS`는 이미 g 단위 float32라 해당 없음.
- **⚠ gsensor의 `x_g`/`y_g`/`z_g` 절대값은 신뢰하지 말 것.** `x_raw/scale`로 계산하는데,
  중력 때문에 `|(x,y,z)|`가 ~1g여야 하는 정차/정속 구간에서 기기마다 0.27g / 0.25g / 1.99g로
  제각각 나온다. `count`를 곱하는 보정도 신규 Ambarella 기기(count=1)에서 반증됐다.
  `count`가 "뒤에 오는 값 개수"라는 해석도 틀렸다(count=4든 1이든 뒤에 오는 값은 4개).
  축 방향과 상대적 변화 추이는 유효하고, 원시값(`x_raw`/`scale`/`count`)은 전부 보존된다.
  절대값이 필요하면 파일 단위로 `|(x,y,z)|` 중앙값을 1g로 놓고 정규화하는 자가 보정을 쓰면
  기기 스펙 없이도 세 기기 모두 맞는다. 자세한 근거는 architect.md "알려진 한계" 참고.
- float32 벡터(SENS/sensor_values.csv, AVI 쪽)와 MP4 쪽 gsensor 필드는 둘 다 공식 스펙이 아니라
  관찰/추정한 것 — 다른 파일에서 같은 패턴이 나와도 곧이곧대로 믿지 말고 값 범위부터 확인할 것
- MP4 Sample 맨 앞 "2바이트 길이 프리픽스"도 마찬가지로 QuickTime text sample 관례를 보고
  넣은 거고, `길이+2 == Sample 크기`가 실제로 맞는지 매번 검증한 다음에만 씀 — 안 맞는 장비면
  자동으로 raw 텍스트 후보 취급으로 빠짐
- raw는 `OUT_OF_RANGE`만 아니면 항상 그대로 보존함 (chunks/*.bin, `{prefix}_concat.bin` 또는
  raw_chunks/*.bin, raw_concat.bin, MP4 쪽은 --extract 옵션). AVI에서 `ID_MISMATCH`/`SIZE_MISMATCH`인
  idx1 entry도 raw는 뽑되, false positive 방지를 위해 자동 디코딩(분류/좌표 산출)만 생략하고
  `index.csv`에 사유를 남김. `OUT_OF_RANGE`(파일 범위를 벗어난 entry)만 raw 추출 자체를 생략함.
