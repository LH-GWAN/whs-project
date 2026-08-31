# GPS Tracer

블랙박스 영상(AVI/MP4)에서 GPS·속도·G센서 메타데이터를 추출해 지도와 그래프로
시각화하고, 사건 리포트(PDF)를 생성하는 데스크톱 포렌식 도구. Windows exe로 배포한다.

분석 엔진은 별도 저장소([LH-GWAN/whs-project](https://github.com/LH-GWAN/whs-project))에서
개발된 것을 `engine/vendor/`에 그대로 가져와 쓴다. **이 저장소의 코드는 그 엔진을
호출하고 결과를 시각화하는 레이어**다.

---

## 목차

- [빠른 시작](#빠른-시작)
- [전체 구조](#전체-구조)
- [모듈별 역할](#모듈별-역할)
- [데이터 흐름](#데이터-흐름)
- [⚠ 주의할 점](#-주의할-점)
- [검증 현황](#검증-현황)

---

## 빠른 시작

```bash
python -m venv .venv          # macOS에서는 ~/Desktop 밖에 만들 것 (아래 주의사항 참고)
source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

exe 빌드는 [`BUILD.md`](BUILD.md), 배경지도 준비는 [`assets/README.md`](assets/README.md) 참고.

---

## 전체 구조

```
app.py                    진입점. --run-engine 분기를 Qt 초기화보다 먼저 처리
engine_entry.py           엔진을 서브프로세스에서 실행하기 위한 경계

core/                     플랫폼/UI 무관 핵심 로직
├── paths.py              개발 실행 vs 패키징 실행 경로 차이 흡수
├── format_sniffer.py     업로드 파일이 처리 가능한지 사전 확인
├── duration.py           영상 재생시간 계산
├── acceleration.py       급가속 의심 구간 판정
├── hashing.py            SHA-256 무결성 해시
└── pipeline.py           전체 분석 파이프라인 (위 모듈들을 순서대로 엮음)

engine/                   분석 엔진 오케스트레이션
├── registry.py           엔진 진입 모듈 이름 (엔진 교체 시 여기만 수정)
├── engine_adapter.py     서브프로세스 실행 + CSV → 데이터 모델 변환
└── vendor/               ★ 원본 엔진 (수정 금지)

storage/history_store.py  사건 이력 (SQLite)
report/report_builder.py  Extraction Report PDF 생성

ui/                       PySide6 화면
├── main_window.py        Home ↔ 분석화면 전환, 워커/리포트 연결
├── home_view.py          업로드 + 사건 이력 목록
├── case_info_dialog.py   사건 정보 입력 모달
├── analysis_view.py      공통 헤더 + 3개 탭 구성
├── tracker_tab.py        영상 재생 + 지도 (재생 위치 동기화)
├── speed_tab.py          속도 그래프 + 통계
├── speed_chart_widget.py 속도 그래프 렌더링 (QPainter)
├── location_tab.py       지도 + 좌표 테이블
├── map_view.py           지도 위젯 (QWebEngineView 래퍼)
├── map_server.py         지도 리소스 로컬 서버
├── workers.py            분석을 백그라운드 스레드에서 실행
├── styles.py             QSS
├── web/map.html          MapLibre 지도 페이지
└── vendor/               MapLibre GL JS, PMTiles (로컬 번들, CDN 금지)

assets/korea.pmtiles      오프라인 배경지도 (371MB, git 제외)
gpstracer.spec            PyInstaller 빌드 정의
```

---

## 모듈별 역할

### `app.py` — 진입점

`--run-engine` 플래그를 **QApplication을 만들기 전에** 확인하고 `engine_entry`로 위임한다.
패키징된 exe에는 별도 `python.exe`가 없어서, 엔진을 서브프로세스로 돌릴 때 앱이 자기
자신을 재호출하기 때문이다. 이 순서가 바뀌면 자식 프로세스가 GUI를 하나 더 띄운다.

### `core/paths.py` — 경로 해석

PyInstaller는 번들 리소스를 `sys._MEIPASS`에 풀어놓으므로 `__file__` 기준 경로가 전부
깨진다. frozen 여부에 따른 분기를 이 파일 하나로 모아, 나머지 모듈은 신경 쓰지 않게 한다.

### `core/format_sniffer.py` — 사전 확인

엔진을 돌리기 전에 "이 파일을 처리할 수 있는가"를 즉시 알려준다. **판별 로직을 다시
구현하지 않고 엔진의 `detect_container()`를 그대로 호출**한다 — 앱은 "지원함"이라 했는데
엔진은 건너뛰는 불일치를 원천 차단하기 위함. 세부 경로(AVI 내 스트림 종류, MP4 내
fragmented/sampletable/udta-mamt)는 엔진이 알아서 고르므로 앱은 관여하지 않는다.

### `engine/registry.py` — 엔진 진입점 정의

엔진이 `integration_blackbox.py` 하나로 통합돼 있어 항목이 하나뿐이다. 엔진이 또 바뀌면
**이 파일만 수정**하면 되도록 격리해 뒀다.

### `engine/engine_adapter.py` — 오케스트레이션

엔진을 서브프로세스로 실행하고, 결과 CSV를 `TrackPoint` 리스트로 표준화한다.
앱의 모든 화면과 리포트가 이 데이터 모델 하나만 본다.

`TrackPoint`의 세 가지 상태 구분이 이 도구의 핵심 개념이다:

| 상태 | 판별 | 의미 |
|---|---|---|
| `has_fix` | 좌표 있음 | GPS 수신 정상 |
| `is_dropout` | GPS 시각은 있는데 좌표 없음 | **수신 끊김** (NMEA status=V) |
| (둘 다 아님) | GPS 정보 자체가 없음 | 이 시점엔 GPS 미기록 (G센서 전용 행) |

### `core/acceleration.py` — 급가속 판정

이 결과 하나를 속도 그래프의 강조 구간과 좌표 테이블의 빨간 행이 함께 사용한다
(두 화면이 어긋나지 않도록 단일 소스).

### `core/pipeline.py` — 분석 파이프라인

해시 → 형식 확인 → 사건 폴더 생성 → 원본 복사 → 엔진 추출 → duration/급가속 계산 →
이력 기록. `reopen_case()`는 엔진 재실행 없이 저장된 CSV만 다시 읽는다(빠르고, 원본
영상이 이동식 매체로 사라져도 결과 조회 가능).

### `storage/history_store.py` — 사건 이력

SQLite는 **검색/색인용**이고, 실제 증거(원본 사본·엔진 출력·리포트)는 폴더에 파일로 둔다.
DB가 손상돼도 증거는 살아남고, 다른 포렌식 도구가 이 앱 없이 폴더만으로 접근할 수 있다.

```
%LOCALAPPDATA%/GPSTracer/
├── history.db
└── cases/<사건번호>_<id>/
    ├── source/           원본 영상 사본 (해시로 무결성 고정)
    ├── engine_output/    엔진 산출물 원본 그대로
    ├── case.json         DB 유실 대비 사람이 읽을 수 있는 사본
    └── report.pdf
```

### `ui/map_server.py` — 지도 리소스 서버

배경지도(PMTiles)는 한 파일에서 필요한 타일만 바이트 범위로 읽는 포맷이라 HTTP
Range(206) 의미론이 필요하다. Qt 커스텀 URL 스킴으로는 206을 제대로 돌려줄 수 없어
표준 HTTP를 쓴다. **`127.0.0.1`에만 바인딩**하고 임의 포트를 쓰며, 앱 자신의 정적
파일과 지도 파일만 서빙한다. 사건 GPS 좌표는 이 서버를 타지 않는다.

### `ui/map_view.py` — 지도 위젯

Python → JS는 `runJavaScript()` 단방향 주입만 쓴다(지도가 되물어볼 일이 없어 채널 유지
불필요, 준비 경쟁조건도 사라짐). 렌더 프로세스가 죽으면 최대 3회까지 자동 복구하고,
복구 후 보던 궤적을 다시 그린다.

---

## 데이터 흐름

```
영상 업로드
   ↓
core.format_sniffer.sniff()          엔진의 detect_container() 재사용
   ↓
core.pipeline.run_analysis_pipeline()   (ui.workers의 백그라운드 스레드에서 실행)
   ├─ core.hashing                   SHA-256
   ├─ 사건 폴더 생성 + 원본 복사
   ├─ engine.engine_adapter.run_full_extraction()
   │     └─ 서브프로세스: app.exe --run-engine blackbox -o <출력> <입력>
   │           └─ engine_entry → integration_blackbox.main()
   │                 ├─ AVI → integration_avi.py
   │                 └─ MP4 → integration_mp4.py
   │        결과: timeline.csv / coordinates.csv / ... 를 TrackPoint로 변환
   ├─ core.duration                  재생시간
   └─ core.acceleration              급가속 구간
   ↓
ui.analysis_view.load_result()
   ├─ Tracker  : 영상(QMediaPlayer) + 지도, 재생 위치 동기화
   ├─ Speed    : 속도 그래프 + 급가속 강조
   └─ Location : 지도 + 좌표 테이블
   ↓
Report 버튼 → report.report_builder → PDF
```

---

## ⚠ 주의할 점

코드를 고치기 전에 반드시 읽을 것. 대부분 실제로 문제가 발생해서 알게 된 것들이다.

### 1. 엔진은 서브프로세스로만 실행할 것

`integration_*.py`의 `main()`과 argparse는 실패 시 `sys.exit()`을 호출한다.
`SystemExit`은 `Exception`이 아니라 **`except Exception`으로 잡히지 않아** GUI 프로세스에서
직접 import해서 부르면 앱 전체가 죽는다. 서브프로세스로 격리하면 이 위험이 사라지고,
사건 이력에 남길 실행 로그(stdout/stderr/종료코드)도 자연스럽게 확보된다.

단, `detect_container()`처럼 `sys.exit()`을 부르지 않는 순수 함수는 직접 import해도 안전하다.

### 2. `engine/vendor/`는 수정하지 말 것

원본 저장소에서 그대로 가져온 파일이다. 여기를 고치면 원본과 갈라져 다음 갱신 때
충돌한다. 갱신 절차와 확인 항목은 [`engine/vendor/README_VENDOR.md`](engine/vendor/README_VENDOR.md) 참고.

특히 **엔진 갱신 시 `gpstracer.spec`의 `hiddenimports`를 반드시 다시 확인**해야 한다.
vendor는 데이터 파일로 번들되어 PyInstaller의 정적 분석 대상이 아니라서, 엔진이 새로
쓰기 시작한 표준 라이브러리가 빠지면 **얼린 뒤에야** `ModuleNotFoundError`로 터진다
(실제로 `shlex`에서 겪음).

### 3. 시간축은 엔진이 계산한 `start_time_sec`를 쓸 것

GPS 문장의 UTC로 시간축을 만들면 수신이 튀는 기기에서 축까지 같이 튄다(엔진 README
실측: QXD8000/Mercedes에서 GPS UTC 간격이 0/1/2초로 불규칙한데 구조 기반 축은 완전 균일).
영상 재생과 동기화하는 게 목적이므로 컨테이너 구조에서 계산한 값이 맞다.

경로별 시간축 근거(`time_source`): `tfdt_trun`(fragmented) / `stts`(sample table) /
`gps_utc_elapsed`(udta-mamt) / `avi_video_duration`(AVI).

### 4. 가속도는 "서로 다른 GPS 측정값" 사이에서만 계산할 것

많은 기기가 GPS 갱신 주기보다 훨씬 빠르게 레코드를 쓴다. 그 사이 행들은 **직전 측정값을
그대로 반복한 것**이라 서로 다른 시점으로 취급하면 안 된다.

> **실측 사례**: VUGERA는 초당 31행을 쓰는데 GPS는 1초에 한 번만 갱신된다. 초 경계의
> 속도 변화 3.18 km/h를 행 간격 0.033초로 나누면 **26.8 m/s²** 가 나온다. 실제로는
> 0.88 m/s²인 완만한 가속인데, 38초 영상에서 급가속이 30건 검출됐다.
> FineVu도 연속 행의 94.1%가 완전 중복이었다.

`_distinct_fix_indices()`가 중복을 걷어낸다. UTC가 있으면 UTC로 묶고(정차 중이라 좌표·속도가
같아도 측정은 매초 새로 이뤄지므로 값으로만 묶으면 안 됨), FineVu처럼 UTC가 없는 이진
포맷은 값으로 묶는다.

### 5. "좌표 없음"을 전부 수신 끊김으로 보지 말 것

GPS 1Hz / G센서 10Hz인 기기에서는 좌표 없는 행 대부분이 수신 장애가 아니라 그냥 GPS가
안 실린 샘플이다. 뭉뚱그리면 **정상 주행이 수신 장애로 오독**된다.

> **실측 사례**: INAVI QXD8000은 600행 중 540행이 G센서 전용인데, 이걸 끊김으로 세니
> "GPS 끊김 540개 지점"이라 표시되고 지도의 궤적 전체가 점선(경로 불확실)으로 그려졌다.

판별 기준은 `TrackPoint.is_dropout` — 엔진이 GPS 문장을 파싱했으면 좌표가 비어도
날짜/UTC는 채워주므로 그걸로 구분한다.

### 6. MapLibre `interpolate`는 표현식 최상위에만

줌 기반 `interpolate`를 `match` 안에 중첩하면 MapLibre가 **레이어를 통째로 거부**한다
(콘솔에 `Only one zoom-based "step" or "interpolate" subexpression may be used`).
도로가 통째로 안 그려지는데 예외는 안 나므로 알아채기 어렵다. `interpolate`를 바깥에 두고
각 줌 구간 값 안에서 `match`로 종류별 굵기를 고를 것.

### 7. 지도 초기화는 `load` 이벤트만 믿지 말 것

MapLibre의 `load`는 **렌더 프레임이 최소 한 번 돌아야** 발생한다. GPU 드라이버가
불안정하거나 원격데스크톱/가상머신이면 렌더 루프가 안 돌아 이벤트가 영영 오지 않고
화면이 "지도 준비 중…"에서 멈춘다. `map.html`은 이벤트 + 폴링을 함께 쓰고, 8초 안에
안 되면 사용자에게 사유를 표시한다.

또 Chromium은 **화면에 보이지 않는 페이지의 타이머를 억제**한다. 탭 안의 지도는 그 탭이
열릴 때 `ensure_map_loaded()`로 로드한다(두 탭의 지도를 동시에 띄우면 WebGL 컨텍스트를
두 벌 쓰는 낭비이기도 하다).

### 8. 배경지도 레이어 이름은 Protomaps 스키마

`earth` / `water` / `landuse` / `roads` / `buildings` / `boundaries` 를 쓴다.
OpenMapTiles의 `transportation` / `building` 이 **아니다**.

### 9. macOS 개발 시 venv 위치

`~/Desktop` 하위에 만든 venv는 Qt 플랫폼 플러그인 로드에 실패한다(macOS 폴더 접근
제한으로 추정). 플러그인 파일은 존재하고 직접 dlopen도 되는데 Qt가 못 찾는다.
`~/dev/` 등 Desktop 밖에 만들 것. Windows 배포에는 영향 없는 개발 환경 한정 이슈다.

### 10. 리포트는 전체 좌표를 넣지 않는다

수천~수만 지점이 나오므로 급가속 구간을 우선 포함하고 나머지는 앞부분 일부만 싣는다
(최대 200행). 원본 전체는 `engine_output/`의 CSV에 그대로 보존돼 있다.

---

## 검증 현황

### 실제 샘플 (8개 기종 13개 파일) — 전부 통과

| 기종 | 컨테이너 | 지점 | GPS 수신 | 시간축 |
|---|---|---|---|---|
| VUGERA MB-900SB (4파일) | AVI | 1150~2246 | 전부 | `avi_video_duration` |
| INAVI Z300 | MP4 | 200 | 20 | `stts` |
| INAVI QXD8000 | MP4 | 600 | 60 | `tfdt_trun` |
| Land Rover Dashcam (3파일) | MP4 | 60 | 60 / 36 | `gps_utc_elapsed` |
| Mercedes-Benz DriveView | MP4 | 60 | 60 | `tfdt_trun` |
| INAVI FXD900 | AVI | 39 | 39 | `avi_video_duration` |
| FineVu X3000 / X700 | AVI | 1035 / 1060 | 전부 | `avi_video_duration` |

Land Rover `20250901_215728D`의 GPS 수신 36개(끊김 24개)는 엔진 README에 기록된
"60 (fix 없음 24 포함)"과 일치한다.

### 그 외 확인한 것

- 컨테이너 판별이 엔진과 100% 일치 (AVI/MP4/ftyp 없는 변종/RIFF-WAVE/비디오 아님)
- 경계 조건: 지원하지 않는 파일, GPS 없는 영상, dt=0, 시간 역행, 큰 간격, 시간축 없음
- 무결성: 원본 SHA-256 == 사본 == 기록값, `case.json`/`engine_runs` 기록
- 재열람이 엔진 재실행 없이 동일 결과, **원본 파일 삭제 후에도** 조회 가능
- 실제 샘플로 PDF 리포트 생성 (133KB)
- 얼린 exe에서 실제 fragmented MP4 추출

### 아직 확인 못 한 것

- **화면에 실제로 보이는 모습** — 개발 환경이 헤드리스라 지도/UI의 시각적 확인 불가.
  실제 디스플레이가 있는 Windows에서 봐야 한다.
- **Windows exe 실물** — PyInstaller는 크로스 컴파일을 지원하지 않는다.
  빌드 설정은 macOS에서 실제로 얼려 검증했다(엔진 실행·지도 자산 번들 포함).
