"""同じ重いページの要求が同時に来た時は、1 回だけ数えて、後から来た方はその結果を待って使う。

管理画面を 2 枚開いた時や、画面が読み直した時に、同じ重いページが 2 本ずつ同時に動き、ディスクを取り合っていた
(気になるギフターの一覧・お知らせ。2026-10-20 に Cursor が py-spy で見た)。
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

_lock = threading.Lock()
_running: dict[Any, "_Call"] = {}
_done: dict[Any, tuple[float, Any]] = {}  # ttl を付けて数えた結果 (作った時刻, 結果)
_forgot = 0  # forget を呼んだ回数 (数えている最中に中身が変わった時は、その結果を使い回さない)
DONE_KEEP = 200  # 覚えておく結果の数の上限 (超えたら古いものから忘れる)


class _Call:
    def __init__(self) -> None:
        self.event = threading.Event()
        self.result: Any = None
        self.error: BaseException | None = None
        self.forgot = _forgot


def shared(key: Any, fn: Callable[[], Any], ttl: float = 0.0) -> Any:
    """fn() の結果。同じ key が数えている最中なら、それが終わるのを待って同じ結果を返す (失敗も同じ失敗にする)。
    ttl があれば、数え終えた結果を ttl 秒使い回す。結果は何本もの要求で分け合うので、受け取った側は書き換えない。"""
    with _lock:
        hit = _done.get(key) if ttl > 0 else None
        if hit is not None and time.time() - hit[0] < ttl:
            return hit[1]
        call = _running.get(key)
        if call is not None and call.forgot != _forgot:
            call = None  # 数えている最中のものは、中身が変わる前の分: 待たずに数え直す (そちらが終わっても、こちらを残す)
        owner = call is None
        if owner:
            call = _running[key] = _Call()
        forgot = _forgot
    if not owner:
        call.event.wait()
        if call.error is not None:
            raise call.error
        return call.result
    try:
        call.result = fn()
        if ttl > 0:
            with _lock:
                if forgot != _forgot:
                    return call.result  # 数えている間に中身が変わった: この結果は使い回さない (今の要求には返す)
                _done.pop(key, None)
                _done[key] = (time.time(), call.result)
                while len(_done) > DONE_KEEP:
                    _done.pop(next(iter(_done)))
        return call.result
    except BaseException as e:
        call.error = e
        raise
    finally:
        with _lock:
            if _running.get(key) is call:
                del _running[key]
        call.event.set()


def forget(match: Callable[[Any], bool]) -> None:
    """使い回している結果のうち、match(key) が真のものを忘れる (中身が変わった時)。"""
    global _forgot
    with _lock:
        _forgot += 1
        for key in [k for k in _done if match(k)]:
            del _done[key]
