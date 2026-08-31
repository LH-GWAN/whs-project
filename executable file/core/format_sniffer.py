from __future__ import annotations

import os
from dataclasses import dataclass

from core.paths import ensure_vendor_importable

ensure_vendor_importable()

import integration_blackbox as _blackbox  # noqa: E402

CONTAINER_AVI = _blackbox.CONTAINER_AVI
CONTAINER_MP4 = _blackbox.CONTAINER_MP4


@dataclass
class RoutingResult:
    container: str
    supported: bool
    reason: str = ""
    extension_mismatch: str = ""


def sniff(path: str) -> RoutingResult:
    if not os.path.isfile(path):
        return RoutingResult("unsupported", False, reason="파일을 찾을 수 없습니다.")
    if os.path.getsize(path) == 0:
        return RoutingResult("unsupported", False, reason="파일 크기가 0입니다.")

    container, note = _blackbox.detect_container(path)
    if container is None:
        return RoutingResult("unsupported", False, reason=note or "지원하지 않는 파일 형식입니다.")

    mismatch = _blackbox.check_extension_mismatch(path, container) or ""
    return RoutingResult(
        container=container,
        supported=True,
        reason=note or f"{container.upper()} 컨테이너로 판별됨",
        extension_mismatch=mismatch,
    )
