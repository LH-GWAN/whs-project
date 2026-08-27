# 언제 뭘 쓰는지

- AVI, strl에 스트림 이름/핸들러가 이미 나와있음(GPSR, SENS 이런 식) → **GPS_metadata_avi.py**
- AVI, strl에 이름 없이 그냥 txt 타입이라고만 나옴, 뭔지 모름 → **GPS_metadata_GPRMC.py**
- MP4(INAVI 등, moov/trak 구조) → **GPS_metadata_mp4_pvc1_Atext.py**

# AVI_exception_lot_RIFF.py

RIFF가 2개 이상 있을 경우, 슬랙 데이터 때문에 한번에 idx1을 찾기 어려운 경우가 있다. 이를 방지하고자 RIFF 가 2개 이상 있는 경우 내부 알고리즘을 통해 슬랙 데이터들을 제거하고 영상을 재추출하는 스크립트다.

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

# 공통 주의사항

- GPRMC/GPGGA 파싱은 NMEA 0183 필드 형식을 기준으로 하되, 손상 레코드 예외처리와 좌표/방향 범위 검증을 추가함. checksum/status는 별도 기록하며 `trusted` 값으로 1차 신뢰 여부를 확인할 수 있음.
- float32 벡터(SENS/sensor_values.csv, AVI 쪽)와 MP4 쪽 gsensor 필드는 둘 다 공식 스펙이 아니라
  관찰/추정한 것 — 다른 파일에서 같은 패턴이 나와도 곧이곧대로 믿지 말고 값 범위부터 확인할 것
- MP4 Sample 맨 앞 "2바이트 길이 프리픽스"도 마찬가지로 QuickTime text sample 관례를 보고
  넣은 거고, `길이+2 == Sample 크기`가 실제로 맞는지 매번 검증한 다음에만 씀 — 안 맞는 장비면
  자동으로 raw 텍스트 후보 취급으로 빠짐
- 구조/offset/크기가 `OK`로 검증된 레코드는 raw를 그대로 보존함 (chunks/*.bin, `{prefix}_concat.bin` 또는
  raw_chunks/*.bin, raw_concat.bin, MP4 쪽은 --extract 옵션). AVI에서 `ID_MISMATCH`/`SIZE_MISMATCH`/
  `OUT_OF_RANGE`인 idx1 entry는 false positive 방지를 위해 자동 추출·디코딩하지 않고 `index.csv`에만 남김.
