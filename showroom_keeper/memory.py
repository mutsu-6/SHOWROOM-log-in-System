"""重い集計 (分析・ギフターの一覧・集まり具合) のメモリの気づかい。

サーバー (e2-small・メモリ 2GB) では、視聴のブラウザが配信中のルーム 2 つで約 1GB 使う。そこに重い集計が重なると、
メモリがディスク (swap) に追い出されて全部が重くなる (2026-10-19 20:30 ごろ、空き 96MB・CPU の時間の約 3 割がディスク待ち。Cursor が見つけた)。
そこで、前の結果がある集計の数え直しは、空きが少ない時は後回しにし (前の結果を出し続ける)、終わったら使い終えたメモリを OS に返す。
"""

from __future__ import annotations

import ctypes
import ctypes.util
import logging
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

log = logging.getLogger(__name__)

HEAVY_MIN_FREE_MB = 400  # 前の結果がある集計を、裏で数え直してよいメモリの空き (MB)。これより少なければ次の機会に回す


def free_mb() -> float | None:
    """今のメモリの空き (MemAvailable、MB)。分からなければ None。"""
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) / 1024
    except (OSError, ValueError):
        pass
    return None


def tight(need_mb: float = HEAVY_MIN_FREE_MB) -> bool:
    """メモリの空きが need_mb より少ないか (分からなければ False = 気にせず数える)。"""
    free = free_mb()
    return free is not None and free < need_mb


_heavy = threading.RLock()


@contextmanager
def heavy(wait: bool = True) -> Iterator[bool]:
    """重い集計を 1 つずつ行う (同時に読むと、ディスクの読み出しを取り合って、どれも何倍も遅くなる。2026-10-19 にサーバーで
    analyze と集まり具合の数え直しが重なっていた)。wait=False なら、ほかが数えている時は待たずに False を返す (裏の数え直しは次の機会に)。"""
    got = _heavy.acquire(blocking=wait)
    try:
        yield got
    finally:
        if got:
            _heavy.release()


_libc = None


def release() -> None:
    """使い終えたメモリを OS に返す (Python は、まとめて使ったメモリを手元に残しがちで、集計のあとも数十 MB 増えたままになる)。
    glibc の malloc_trim。使えない所 (Mac など) では何もしない。"""
    global _libc
    try:
        if _libc is None:
            _libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6")
        trim = getattr(_libc, "malloc_trim", None)
        if trim is not None:
            trim(0)
    except OSError as e:
        log.debug("メモリを返せませんでした: %s", e)
        _libc = False  # type: ignore[assignment]
    except (AttributeError, TypeError):
        pass
