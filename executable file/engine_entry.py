from __future__ import annotations

import importlib
import os
import sys

_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from core.paths import ensure_vendor_importable  # noqa: E402

ensure_vendor_importable()

from engine.registry import MODULE_BY_ENGINE  # noqa: E402


def run(engine_name: str, argv: list[str]) -> int:
    module_name = MODULE_BY_ENGINE.get(engine_name)
    if module_name is None:
        print(
            f"[engine_entry] 알 수 없는 engine 이름: {engine_name!r} "
            f"(선택 가능: {', '.join(MODULE_BY_ENGINE)})",
            file=sys.stderr,
        )
        return 2

    module = importlib.import_module(module_name)

    try:
        module.main(argv)
    except SystemExit as exc:
        code = exc.code
        if code is None:
            return 0
        if isinstance(code, int):
            return code
        print(str(code), file=sys.stderr)
        return 1
    return 0


def main() -> int:
    if len(sys.argv) < 3:
        print(
            "usage: engine_entry.py <engine_name> <engine args...>\n"
            '  예: engine_entry.py blackbox -o "출력폴더" "영상.mp4"',
            file=sys.stderr,
        )
        return 2
    return run(sys.argv[1], sys.argv[2:])


if __name__ == "__main__":
    sys.exit(main())
