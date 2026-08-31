from __future__ import annotations

import os
import sys


def is_frozen() -> bool:
    return getattr(sys, "frozen", False)


def resource_root() -> str:
    if is_frozen():
        return getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(sys.executable)))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def vendor_dir() -> str:
    return os.path.join(resource_root(), "engine", "vendor")


def ensure_vendor_importable() -> str:
    path = vendor_dir()
    if path not in sys.path:
        sys.path.insert(0, path)
    return path


def engine_entry_script() -> str:
    return os.path.join(resource_root(), "engine_entry.py")
