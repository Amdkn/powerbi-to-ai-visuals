"""Shared bootstrap and helpers for the Power BI -> AI visuals toolchain.

Keeps every tool importable as a plain script:  python3 tools/<tool>.py ...
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
VENDOR = PROJECT_ROOT / "vendor"
if VENDOR.is_dir() and str(VENDOR) not in sys.path:
    sys.path.insert(0, str(VENDOR))


def require(*modules: str) -> None:
    """Fail with an actionable message instead of a bare ImportError."""
    missing = []
    for name in modules:
        try:
            __import__(name)
        except ImportError:
            missing.append(name)
    if missing:
        raise SystemExit(
            "Missing Python packages: %s\n"
            "Run:  bash setup.sh   (installs pbixray + native codecs into ./vendor)"
            % ", ".join(missing)
        )


def write_json(path: Path, obj) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, default=str), encoding="utf-8")
    return path


def read_json(path: Path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


AUTO_TABLE_RE = re.compile(r"^(LocalDateTable_|DateTableTemplate_)")


def is_auto_date_table(name: str) -> bool:
    """Power BI's hidden auto date/time tables. Noise in every downstream view."""
    return bool(AUTO_TABLE_RE.match(name))


def slug(name: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_")
    return s or "unnamed"


def log(msg: str) -> None:
    print(msg, file=sys.stderr)
