# ---- 이 파일은 파싱 로직이 없다. 시그니처로 AVI/MP4를 판별해 integration_avi.py / integration_mp4.py 로 넘기기만 한다 ----
import argparse
import os
import shlex
import sys

if sys.stdout.encoding is None or sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except AttributeError:
        pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import integration_avi
import integration_mp4


CONTAINER_AVI = "avi"
CONTAINER_MP4 = "mp4"

# ftyp 없이 시작하는 ISO BMFF 변종에서 첫 Box로 올 수 있는 타입들
ISO_BMFF_LEADING_BOXES = {b"moov", b"mdat", b"moof", b"free", b"skip", b"wide", b"pnot"}

EXTENSION_HINT = {".avi": CONTAINER_AVI, ".mp4": CONTAINER_MP4, ".m4v": CONTAINER_MP4,
                  ".mov": CONTAINER_MP4}


def detect_container(path):
    """파일 앞부분 시그니처로 컨테이너를 판별한다.
    반환값: (container, note) - container가 None이면 지원하지 않는 형식이고
    note에 이유가 들어간다."""
    try:
        with open(path, "rb") as f:
            head = f.read(16)
    except OSError as exc:
        return None, f"파일을 열 수 없음: {exc}"

    if len(head) < 12:
        return None, f"파일이 너무 작음({len(head)} bytes) - 컨테이너 헤더를 읽을 수 없음"

    if head[0:4] == b"RIFF":
        form = head[8:12]
        if form == b"AVI ":
            return CONTAINER_AVI, None
        return None, (f"RIFF이지만 formType이 {form.decode('ascii', errors='replace')!r} "
                      f"- AVI가 아니라 처리 대상이 아님")

    box_type = head[4:8]
    if box_type == b"ftyp":
        return CONTAINER_MP4, None
    if box_type in ISO_BMFF_LEADING_BOXES:
        return CONTAINER_MP4, (f"ftyp Box가 없지만 첫 Box가 "
                               f"{box_type.decode('ascii', errors='replace')!r} "
                               f"- ISO BMFF 변종으로 보고 MP4로 처리")

    return None, (f"RIFF/AVI도 아니고 ISO BMFF Box로도 시작하지 않음 "
                  f"(앞 8바이트 {head[:8]!r})")


def check_extension_mismatch(path, container):
    """확장자와 실제 내용이 다르면 알려준다(판별은 내용을 따른다)."""
    ext = os.path.splitext(path)[1].lower()
    hint = EXTENSION_HINT.get(ext)
    if hint is None or hint == container:
        return None
    return (f"확장자({ext})는 {hint.upper()}인데 실제 내용은 {container.upper()} "
            f"- 내용 기준으로 처리함")


def parse_args(argv):
    p = argparse.ArgumentParser(
        description="블랙박스 영상 GPS/센서 메타데이터 추출 최상위 진입점. "
                    "파일 시그니처로 AVI/MP4를 판별해 integration_avi.py 또는 "
                    "integration_mp4.py로 넘긴다.")
    p.add_argument("inputs", nargs="+", help="입력 영상 파일 경로(들). AVI/MP4 섞어도 됨")
    p.add_argument("-o", "--output", default=None,
                    help="결과를 저장할 루트 디렉터리 (--detect-only 면 생략 가능)")
    p.add_argument("--dry-run", action="store_true",
                    help="파일을 만들지 않고 판별 + 파싱 + 요약만 출력")
    p.add_argument("--detect-only", action="store_true",
                    help="컨테이너 판별 결과만 출력하고 추출은 하지 않음")
    p.add_argument("--slack", action="store_true",
                    help="MP4 슬랙에서 과거 녹화분 GPS/G센서를 추가로 카빙한다(기본 안 함). "
                         "AVI 쪽 슬랙 리페어는 이 옵션과 무관하게 항상 수행됨")
    # 값이 "-"로 시작하면 argparse가 옵션으로 오인하므로 반드시 = 형태로 붙여 써야 한다.
    #   O   --mp4-opt="--track-id 3"
    #   X   --mp4-opt "--track-id 3"
    p.add_argument("--avi-opt", action="append", default=[], metavar="ARGS",
                    help='integration_avi.py 로 그대로 넘길 인자. 반드시 = 로 붙여 쓸 것 '
                         '(예: --avi-opt="--select-mode by_fcctype"). 여러 번 지정 가능')
    p.add_argument("--mp4-opt", action="append", default=[], metavar="ARGS",
                    help='integration_mp4.py 로 그대로 넘길 인자. 반드시 = 로 붙여 쓸 것 '
                         '(예: --mp4-opt="--track-id 3"). 여러 번 지정 가능')
    args = p.parse_args(argv)
    if args.output is None and not args.detect_only:
        p.error("-o/--output 은 --detect-only 가 아닐 때 반드시 필요합니다")
    args.avi_opt = [tok for chunk in args.avi_opt for tok in shlex.split(chunk)]
    args.mp4_opt = [tok for chunk in args.mp4_opt for tok in shlex.split(chunk)]
    return args


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)

    groups = {CONTAINER_AVI: [], CONTAINER_MP4: []}
    skipped = []

    print("=" * 70)
    print("[컨테이너 판별]")
    for path in args.inputs:
        name = os.path.basename(path)
        if not os.path.isfile(path):
            print(f"  SKIP {name:38} 파일을 찾을 수 없음")
            skipped.append((path, "file not found"))
            continue
        container, note = detect_container(path)
        if container is None:
            print(f"  SKIP {name:38} {note}")
            skipped.append((path, note))
            continue
        mismatch = check_extension_mismatch(path, container)
        label = container.upper()
        print(f"  {label:4} {name:38}{('  ' + note) if note else ''}")
        if mismatch:
            print(f"       [WARN] {mismatch}")
        groups[container].append(path)
    print("=" * 70)

    if args.detect_only:
        print(f"\n[판별 요약] AVI {len(groups[CONTAINER_AVI])}개 / "
              f"MP4 {len(groups[CONTAINER_MP4])}개 / 처리 불가 {len(skipped)}개")
        return

    results = []

    if groups[CONTAINER_AVI]:
        print(f"\n{'#' * 70}\n# AVI {len(groups[CONTAINER_AVI])}개 -> integration_avi.py\n{'#' * 70}")
        argv_avi = ["-o", args.output]
        if args.dry_run:
            argv_avi.append("--dry-run")
        argv_avi += args.avi_opt + groups[CONTAINER_AVI]
        try:
            integration_avi.main(argv_avi)
            results.append(("AVI", len(groups[CONTAINER_AVI]), None))
        except SystemExit as exc:
            # integration_avi.assert_riff_file 등이 sys.exit을 부를 수 있다.
            # 여기서 잡아야 뒤이은 MP4 처리가 같이 죽지 않는다.
            results.append(("AVI", len(groups[CONTAINER_AVI]),
                            f"integration_avi.py가 종료 코드 {exc.code}로 중단됨"))
        except Exception as exc:
            results.append(("AVI", len(groups[CONTAINER_AVI]), f"예외: {exc}"))

    if groups[CONTAINER_MP4]:
        print(f"\n{'#' * 70}\n# MP4 {len(groups[CONTAINER_MP4])}개 -> integration_mp4.py\n{'#' * 70}")
        argv_mp4 = ["-o", args.output]
        if args.dry_run:
            argv_mp4.append("--dry-run")
        if args.slack:
            argv_mp4.append("--slack")
        argv_mp4 += args.mp4_opt + groups[CONTAINER_MP4]
        try:
            integration_mp4.main(argv_mp4)
            results.append(("MP4", len(groups[CONTAINER_MP4]), None))
        except SystemExit as exc:
            results.append(("MP4", len(groups[CONTAINER_MP4]),
                            f"integration_mp4.py가 종료 코드 {exc.code}로 중단됨"))
        except Exception as exc:
            results.append(("MP4", len(groups[CONTAINER_MP4]), f"예외: {exc}"))

    print("\n" + "=" * 70)
    print("[전체 요약]")
    for label, count, err in results:
        print(f"  {label} {count}개 -> {'정상 처리' if err is None else err}")
    for path, reason in skipped:
        print(f"  SKIP {os.path.basename(path)} -> {reason}")
    if not results and not skipped:
        print("  처리할 파일이 없습니다.")
    print(f"  출력: {'(dry-run, 파일 미생성)' if args.dry_run else args.output}")
    print("=" * 70)


if __name__ == "__main__":
    main()
