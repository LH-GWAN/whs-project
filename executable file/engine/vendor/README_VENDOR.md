# vendor 폴더에 대하여

여기 있는 파이썬 파일은 **이 프로젝트에서 수정하지 않는다.**
원본: https://github.com/LH-GWAN/whs-project (`ENGINE_COMMIT.txt`의 커밋)

- `integration_blackbox.py` — 앱이 부르는 유일한 진입점(시그니처로 AVI/MP4 판별)
- `integration_avi.py` / `integration_mp4.py` — 위 파일이 import하는 하위 통합 스크립트

원본 레포의 `fragmentation/` 폴더(단독 스크립트 7종)는 같은 기능의 분해판이라
여기 두지 않았다 — 포렌식 도구에서 "실제로 어느 코드가 돌았는지"가 모호해지는 것을
피하기 위함. 위 3개는 표준 라이브러리만 쓰는 자립 스크립트라 그것만으로 동작한다.

## 엔진을 새 버전으로 갱신하려면

1. 위 3개 파일을 원본 레포에서 다시 복사
2. `ENGINE_COMMIT.txt`를 새 커밋 해시로 갱신
3. `ENGINE_README.md` / `ENGINE_ARCHITECT.md`도 같이 갱신(출력 형식 변경 여부 확인용)
4. `gpstracer.spec`의 `hiddenimports` 재확인 (아래 명령으로 목록 추출 — vendor는
   데이터 파일로 번들되어 PyInstaller 정적 분석 대상이 아니라, 빠지면 얼린 뒤에만
   ModuleNotFoundError로 터진다):
   ```bash
   python3 - <<'EOF'
   import ast, pathlib
   mods=set()
   for f in ["integration_blackbox.py","integration_avi.py","integration_mp4.py"]:
       for n in ast.walk(ast.parse(pathlib.Path("engine/vendor/"+f).read_text())):
           if isinstance(n, ast.Import):
               for a in n.names: mods.add(a.name.split(".")[0])
           elif isinstance(n, ast.ImportFrom):
               if n.module and n.level==0: mods.add(n.module.split(".")[0])
   print(sorted(mods - {"integration_avi","integration_mp4","integration_blackbox"}))
   EOF
   ```
5. 앱 쪽에서 확인할 것:
   - `engine/registry.py` — 진입 모듈 이름이 그대로인지
   - `engine/engine_adapter.py` — CLI 형태(`-o <출력> <입력>`), `--slack`/`--mp4-opt`
     옵션, `timeline.csv` 컬럼(`start_time_sec`/`time_source`/`latitude`/`speed_kmh`/
     `x_g_cal` 등)이 그대로인지
   - `core/duration.py` — 재사용 중인 `find_top_level_sections`/`parse_hdrl`/
     `build_stream_table`/`compute_video_duration`/`scan_top_level`/`iter_boxes`/
     `find_box` 시그니처가 그대로인지
   - `core/format_sniffer.py` — `detect_container`/`check_extension_mismatch` 가 그대로인지
   - `track_table.csv`의 `is_text_track` 의미 — fragmented 경로는 "이번에 처리한 Track만
     True"라서 나머지를 추가 실행해야 하고, sample table 경로는 "모든 text Track이 True"라
     추가 실행이 필요 없다. 이 규칙이 바뀌면 `engine_adapter._pending_track_ids`도 고칠 것.
