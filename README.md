# 언제 뭘 쓰는지

- **AVI 파일이면 일단 `integration_avi.py`** — 슬랙 판단/리페어(AVI_exception_lot_RIFF.py)
  → 스트림 자동 선택/추출/디코딩(GPS_metadata_avi.py + GPS_metadata_GPRMC.py)까지 한 번에
  처리하는 통합 스크립트. strl에 스트림 이름이 있든 없든, 슬랙이 있든 없든 알아서 판단하고
  처리하므로 AVI는 기본적으로 이거 하나만 쓰면 된다. 아래 `GPS_metadata_avi.py`/
  `GPS_metadata_GPRMC.py`/`AVI_exception_lot_RIFF.py` 개별 항목은 각 기능이 내부적으로
  어떻게 동작하는지 참고용으로 남겨둔 것(단독 실행도 여전히 가능).
- MP4(INAVI 등), moov 안에 stsc/stsz/stco 있는 일반 구조 → **GPS_metadata_mp4_pvc1_Atext.py**
- MP4인데 위 스크립트로 돌리면 text/sbtl/subt handler를 가진 Track 자체가 없다고 나옴, GPS가
  Track이 아니라 moov 밑 udta/mamt 커스텀 박스 안에 `$GNRMC` 텍스트로 통째로 들어있는 경우
  (Land Rover 대시캠 등) → **GPS_metadata_mp4_udta_mamt_GNRMC_pmp42.py**
- MP4인데 위 두 스크립트로 돌리면 "Chunk offset을 하나도 못 구함"만 뜨고 안 나옴 → Fragmented MP4(ftyp major_brand=iso4, moov 대신 moof/mdat이 반복)일 가능성 큼, INAVI QXD8000 등 → **GPS_metadata_fregment_iso4_Atext.py**

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

또는 스크립트를 그대로 실행하면 현재 폴더의 `REC_*.avi`를 모두 찾아 `./Recovered_2/`에
일괄 처리한다(`process_all_samples`). 임베디드 RIFF를 못 찾으면(이미 깨끗한 파일이면)
아무것도 만들지 않고 스킵만 한다 — 7개 실측 샘플 중 실제로 리페어가 필요했던 건 REC 2개
(F/R)뿐이었고, 나머지 5개(EVT×2, INAVI, FineVu×2)는 전부 오탐 없이 정상 스킵됨을 확인했다.

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
│       ├── unparsed_lines.txt  ← status=V(그 순간 GPS fix 없음, 정상 상황)나 필드 파싱
│       │                          실패한 문장 원문
│       ├── raw_chunks/*.bin    ← 문장 1개당 1개 원본
│       ├── raw_concat.bin      ← 찾은 순서대로 이어붙인 원본
│       └── warnings.log
├── 20250901_215628D/GPS_GNRMC/   ← 위와 동일 구조
└── 20250901_215728D/GPS_GNRMC/   ← 위와 동일 구조 (구간 중 24초 GPS 끊김 → unparsed_lines.txt로)
```

# GPS_metadata_fregment_iso4_Atext.py 기준

GPS_metadata_mp4_pvc1_Atext.py 랑 Atext 내용물 포맷(`gsensor...;GPRMC...;CAR...` 세미콜론 구분
텍스트)은 완전히 같은 장비인데, 컨테이너 구조가 다름 — moov 안에 stsc/stsz/stco 같은 일반
Sample Table이 아예 없고 Fragmented MP4(ftyp major_brand=iso4, `moof`+`mdat`이 파일 끝까지
반복되는 구조)라서 위 스크립트로 돌리면 `Track #3(text): Chunk offset을 하나도 못 구함` 경고만
뜨고 아무것도 안 뽑힘. INAVI QXD8000이 이 케이스 (WHS4_Blackbox GPS Sample 3,
`REC_20240312_082217_F.mp4`).

라이브러리 없이 직접(struct/seek/read) `moof → traf → tfhd/tfdt/trun`을 box size 기반으로
순회해서 text Track(`moov/trak/mdia/hdlr`의 handler_type==text로 식별, track_ID는 "몇 번째
trak"이 아니라 tkhd.track_ID 값 그 자체) Sample의 절대 offset/size를 계산하고,
tfdt(`baseMediaDecodeTime`) + duration으로 영상 재생 시간(초) 구간까지 계산함.
duration/size는 `trun → tfhd → trex(moov/mvex)` 순으로 fallback.

Atext payload 해석(gsensor/GPRMC 정규식, NMEA 파싱)은 GPS_metadata_mp4_pvc1_Atext.py 로직을
그대로 재사용. 요청받은 대로 최종 결과는 GSENSOR / GPRMC(+GPGGA도 지원) 두 종류만 남기고,
같이 실려오는 `CAR,...` 같은 나머지 상태 문자열은 버림 — 파일 안에 그거 말고 다른 포맷이
더 있는지는 원본 바이트를 스크립트와 별개로 직접 재파싱해서 확인함
(`REC_20240312_082217_F.mp4` 기준 gsensor(600)/GPRMC(60)/CAR(600) 세 종류가 전부, 다른 세그먼트 없음).

```
python GPS_metadata_fregment_iso4_Atext.py "영상.mp4" "출력폴더"
python GPS_metadata_fregment_iso4_Atext.py "영상.mp4" "출력폴더" --list-tracks   # Track 목록만
python GPS_metadata_fregment_iso4_Atext.py "영상.mp4" "출력폴더" --dry-run      # 파일 미생성, 콘솔 출력만
python GPS_metadata_fregment_iso4_Atext.py "영상.mp4" "출력폴더" --track-id 3   # text handler Track이 여러 개일 때 지정
python GPS_metadata_fregment_iso4_Atext.py "영상.mp4" "출력폴더" --max-print 20 # 콘솔 출력 개수 제한(CSV는 항상 전체 기록)
python GPS_metadata_fregment_iso4_Atext.py "영상.mp4" "출력폴더" --debug       # Box/Tfhd/Tfdt/Trun 값까지 콘솔에 출력(hex editor 대조용)
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
    ├── coordinates.csv          ← GPRMC/GPGGA 인식된 것만 (start_time_sec/end_time_sec 포함)
    ├── coordinates.txt          ← "1. 위도, 경도" 형식
    ├── sensor_values.csv        ← gsensor 값(x_raw/y_raw/z_raw/scale + x_g/y_g/z_g 환산값,
    │                                start_time_sec 포함 — count/scale 필드 의미는 실측 데이터로
    │                                역산한 것, 공식 스펙 아님 ⚠)
    └── timeline.csv             ← GPS(1Hz)+G센서(10Hz)를 sample 시간 기준 한 줄로 합친 통합
                                     타임라인(시각화용, 신규). latitude/longitude/speed_kmh 등은
                                     GPRMC가 실제로 실려온 sample에만 값이 채워지고 나머지는 공란,
                                     `*_last` 컬럼은 가장 최근 GPS 값을 그대로 이어붙인 것(보간 안 함)
```

MP4가 Fragmented가 아니거나(moof가 하나도 없음), text handler Track을 못 찾으면 그 자리에서
바로 경고 찍고 종료함 — GPS_metadata_mp4_pvc1_Atext.py 쪽을 대신 쓰라고 안내 메시지 남김.

# 공통 주의사항

- GPRMC/GPGGA 파싱은 NMEA 0183 필드 형식을 기준으로 하되, 손상 레코드 예외처리와 좌표/방향 범위 검증을 추가함. checksum/status는 별도 기록하며 `trusted` 값으로 1차 신뢰 여부를 확인할 수 있음.
- float32 벡터(SENS/sensor_values.csv, AVI 쪽)와 MP4 쪽 gsensor 필드는 둘 다 공식 스펙이 아니라
  관찰/추정한 것 — 다른 파일에서 같은 패턴이 나와도 곧이곧대로 믿지 말고 값 범위부터 확인할 것
- MP4 Sample 맨 앞 "2바이트 길이 프리픽스"도 마찬가지로 QuickTime text sample 관례를 보고
  넣은 거고, `길이+2 == Sample 크기`가 실제로 맞는지 매번 검증한 다음에만 씀 — 안 맞는 장비면
  자동으로 raw 텍스트 후보 취급으로 빠짐
- raw는 `OUT_OF_RANGE`만 아니면 항상 그대로 보존함 (chunks/*.bin, `{prefix}_concat.bin` 또는
  raw_chunks/*.bin, raw_concat.bin, MP4 쪽은 --extract 옵션). AVI에서 `ID_MISMATCH`/`SIZE_MISMATCH`인
  idx1 entry도 raw는 뽑되, false positive 방지를 위해 자동 디코딩(분류/좌표 산출)만 생략하고
  `index.csv`에 사유를 남김. `OUT_OF_RANGE`(파일 범위를 벗어난 entry)만 raw 추출 자체를 생략함.
