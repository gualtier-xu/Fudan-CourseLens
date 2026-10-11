"""Process-wide UTF-8 policy for Windows and runner subprocesses."""

from __future__ import annotations

import os
import sys
from typing import TextIO


def _configure_stream(stream: TextIO) -> None:
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is None:
        return
    try:
        reconfigure(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        return


def configure_stdio() -> None:
    """Make Python output and descendants deterministic across Windows code pages."""
    os.environ["PYTHONUTF8"] = "1"
    os.environ["PYTHONIOENCODING"] = "utf-8"
    _configure_stream(sys.stdout)
    _configure_stream(sys.stderr)
