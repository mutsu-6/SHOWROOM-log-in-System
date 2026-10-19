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
import time
from collections import deque
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

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


_hold_until = 0.0  # この時刻までは、サーバーが重いので、裏の数え直しを後回しにする (自己点検が決める)


def hold_off(seconds: float) -> None:
    """サーバーが重い間 (視聴の処理が待たされた・ディスク待ちが多い)、前の結果がある数え直しを後回しにする。"""
    global _hold_until
    _hold_until = max(_hold_until, time.time() + seconds)


def holding() -> float:
    """後回しにしている残りの秒数 (していなければ 0)。"""
    return max(0.0, _hold_until - time.time())


def tight(need_mb: float = HEAVY_MIN_FREE_MB) -> bool:
    """メモリの空きが need_mb より少ないか (分からなければ False = 気にせず数える)。
    サーバーが重くて後回しにしている間も True (前の結果がある数え直しを、空くまで待つ)。"""
    if time.time() < _hold_until:
        return True
    free = free_mb()
    return free is not None and free < need_mb


_heavy = threading.RLock()
_holder: dict[str, Any] = {}  # 今、重い集計をしているスレッド (自己点検が、長く続いていないかを見る)
runs: deque[dict[str, Any]] = deque(maxlen=50)  # 終わった重い集計 {name, at, seconds} (自己点検が、かかった時間を見る)


@contextmanager
def heavy(wait: bool = True) -> Iterator[bool]:
    """重い集計を 1 つずつ行う (同時に読むと、ディスクの読み出しを取り合って、どれも何倍も遅くなる。2026-10-19 にサーバーで
    analyze と集まり具合の数え直しが重なっていた)。wait=False なら、ほかが数えている時は待たずに False を返す (裏の数え直しは次の機会に)。"""
    got = _heavy.acquire(blocking=wait)
    outer = got and not _holder  # 同じスレッドの中で重ねて使った時は、いちばん外側の始まりを覚える
    if outer:
        _holder.update(ident=threading.get_ident(), name=threading.current_thread().name, since=time.time())
    try:
        yield got
    finally:
        if got:
            if outer:
                runs.append({"name": _holder.get("name"), "at": time.time(), "seconds": time.time() - _holder.get("since", time.time())})
                _holder.clear()
            _heavy.release()


def heavy_holder() -> dict[str, Any]:
    """重い集計をしているスレッド {ident, name, since} (していなければ空)。"""
    return dict(_holder)


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
