"""配信中の全ルームの一覧 (/api/live/onlives。1MB ほど) を、入室・ギフトの記録・収集で使い回す。

前は、入室が 60 秒ごと・ギフトの記録が 120 秒ごと・収集が 300 秒ごとに別々に取り、1 日に約 2.7GB を受け取っていた
(入室はブラウザを通していたので、呼ぶたびに重かった。2026-10-19 に Cursor が測った)。
取り方は呼ぶ側が決める (収集は research.THROTTLE を通し、入室・ギフトの記録は通さない)。
fresh() で新しい一覧があれば使い、無ければ取って remember() で覚える。
"""

from __future__ import annotations

import asyncio
import threading
import time
from concurrent.futures import Executor
from typing import Any, Callable

PATH = "/api/live/onlives"
SHARE_TTL = 60  # この秒数より新しい一覧は、取り直さずに使う

_cache: dict[str, Any] = {"at": 0.0, "data": None}
_lock = threading.Lock()
_fetching = threading.Lock()


def fresh(max_age: float = SHARE_TTL, now: float | None = None) -> dict[str, Any] | None:
    with _lock:
        data, at = _cache["data"], _cache["at"]
    if data is None or (time.time() if now is None else now) - at >= max_age:
        return None
    return data


def remember(data: Any, now: float | None = None) -> None:
    if isinstance(data, dict) and isinstance(data.get("onlives"), list):
        with _lock:
            _cache.update(at=time.time() if now is None else now, data=data)


def get(fetch: Callable[[], Any], max_age: float = SHARE_TTL) -> Any:
    with _fetching:  # 同じ時に何か所からも取らない (取っている間は待って、取れた一覧を使う)
        data = fresh(max_age)
        if data is None:
            data = fetch()
            remember(data)
        return data


def public_fetch() -> Any:
    """ログインの Cookie を付けずに取る (配信中の一覧は誰が見ても同じ)。"""
    from .recorder import get_json

    return get_json(PATH)


async def aget(fetch: Callable[[], Any] = public_fetch, max_age: float = SHARE_TTL,
               executor: Executor | None = None) -> Any:
    data = fresh(max_age)
    if data is not None:
        return data
    return await asyncio.get_running_loop().run_in_executor(executor, get, fetch, max_age)


def clear() -> None:
    with _lock:
        _cache.update(at=0.0, data=None)
