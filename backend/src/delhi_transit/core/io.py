"""Atomic file writers: write a sibling temporary file, then os.replace it into place."""
from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq


def _atomic_write(path: Path, write: Callable[[Path], None]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.{time.time_ns()}.tmp")
    try:
        write(tmp)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def atomic_write_text(path: Path, text: str) -> None:
    def write(tmp: Path) -> None:
        with tmp.open("w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())

    _atomic_write(path, write)


def atomic_write_json(
    path: Path,
    payload: Any,
    *,
    sort_keys: bool = False,
    ensure_ascii: bool = True,
) -> None:
    """Write `payload` as two-space indented JSON plus a trailing newline."""
    text = json.dumps(payload, indent=2, sort_keys=sort_keys, ensure_ascii=ensure_ascii) + "\n"
    atomic_write_text(path, text)


def atomic_write_parquet(path: Path, table: pa.Table, **write_options: Any) -> None:
    _atomic_write(path, lambda tmp: pq.write_table(table, tmp, **write_options))
