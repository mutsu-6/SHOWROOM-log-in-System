"""記録しているルームのコメントの「数」だけを、配信ごとに残す (コメントとギフトの釣り合いを見るため)。

リアルタイム通信のコメント (t=1) を数え、<state_dir>/comment_counts/<ルーム>.json に配信ごとの
コメント数・数字だけのコメント (カウント) の数・コメントした人数を書く。
コメントの本文とユーザー ID はファイルに残さない (人数を数えるための ID は、配信中だけメモリに持つ)。
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

FLUSH_EVERY = 60  # ファイルに書く間隔 (秒)
KEEP_DAYS = 180  # これより古い配信の数は消す
KEEP_LIVES = 300  # 1 ルームで残す配信の数
FORGET_AFTER = 6 * 3600  # これだけコメントが無い配信の、人数を数えるための ID を忘れる (秒)
NUMERIC = re.compile(r"^[0-9０-９]+$")


def count_dir(state_dir: Path) -> Path:
    return Path(state_dir) / "comment_counts"


def _path(state_dir: Path, room: str) -> Path:
    return count_dir(state_dir) / f"{room}.json"


def load(state_dir: Path, room: str) -> list[dict[str, Any]]:
    """ルームの配信ごとのコメントの数 (古い順)。"""
    try:
        data = json.loads(_path(state_dir, room).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    lives = data.get("lives") if isinstance(data, dict) else None
    return [x for x in lives or [] if isinstance(x, dict)]


class CommentCounter:
    def __init__(self, state_dir: Path, clock: Any = time.time):
        self.state_dir = Path(state_dir)
        self._clock = clock
        self._lock = threading.Lock()
        self._lives: dict[tuple[str, Any], dict[str, Any]] = {}
        self._users: dict[tuple[str, Any], set[Any]] = {}
        self._flushed: dict[str, float] = {}
        self._dirty: set[str] = set()

    def start(self, room: str, live_id: Any) -> None:
        """配信につながった時に呼ぶ。同じ配信に 2 回目につながった (途中が切れていた) 時は「一部だけ」の印を付ける。"""
        if live_id is None:
            return
        with self._lock:
            if (room, live_id) in self._lives:
                self._lives[(room, live_id)]["partial"] = True
            else:
                self._open(room, live_id)

    def _open(self, room: str, live_id: Any) -> dict[str, Any]:
        saved = next((x for x in load(self.state_dir, room) if x.get("live_id") == live_id), None)
        # 前に数えた続き (keeper を起動し直した時など): 人数は数え直せないので「一部だけ」にする
        live = dict(saved, partial=True) if saved else {
            "live_id": live_id, "first": None, "last": None, "comments": 0, "numeric": 0, "users": 0, "partial": False}
        self._lives[(room, live_id)] = live
        self._users[(room, live_id)] = set()
        return live

    def feed(self, room: str, live_id: Any, data: dict[str, Any]) -> None:
        """リアルタイム通信のコメント (t=1) を 1 件数える。"""
        if live_id is None or data.get("t") != 1:
            return
        now = self._clock()
        with self._lock:
            key = (room, live_id)
            live = self._lives.get(key) or self._open(room, live_id)
            live["comments"] += 1
            if NUMERIC.match(str(data.get("cm") or "").strip()):
                live["numeric"] += 1
            users = self._users.setdefault(key, set())
            if data.get("u") is not None:
                users.add(data.get("u"))
            live["users"] = max(live["users"], len(users))
            live["first"] = live["first"] or int(now)
            live["last"] = int(now)
            # 1 分ごとのコメントの数 (数だけ。誘い投げのあとにコメントが増えたかを見るため)
            minutes = live.setdefault("minutes", {})
            m = str(int(now // 60 * 60))
            minutes[m] = minutes.get(m, 0) + 1
            self._dirty.add(room)
        if now - self._flushed.get(room, 0) >= FLUSH_EVERY:
            self.flush(room)

    def flush_due(self, room: str) -> None:
        """まだ書いていない数があり、前に書いてから FLUSH_EVERY 秒たっていれば書く (配信の最後の 1 分も残すため)。"""
        if room in self._dirty and self._clock() - self._flushed.get(room, 0) >= FLUSH_EVERY:
            self.flush(room)

    def flush(self, room: str) -> None:
        """ルームの数をファイルに書く (配信の終わりにも呼ぶ)。"""
        now = self._clock()
        with self._lock:
            self._flushed[room] = now
            self._dirty.discard(room)
            current = {k[1]: dict(v) for k, v in self._lives.items() if k[0] == room}
            for k in [k for k, v in self._lives.items() if k[0] == room and now - (v["last"] or now) > FORGET_AFTER]:
                self._lives.pop(k, None)
                self._users.pop(k, None)
            saved = {x.get("live_id"): x for x in load(self.state_dir, room)}
            saved.update(current)
            lives = sorted((x for x in saved.values() if (x.get("last") or now) >= now - KEEP_DAYS * 86400),
                           key=lambda x: x.get("first") or 0)[-KEEP_LIVES:]
            path = _path(self.state_dir, room)
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"lives": lives}, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, path)


_counters: dict[str, CommentCounter] = {}
_counters_lock = threading.Lock()


def counter(state_dir: Path) -> CommentCounter:
    """state_dir ごとに 1 つ (記録とウォッチャーで同じものを使う)。"""
    with _counters_lock:
        key = str(Path(state_dir))
        if key not in _counters:
            _counters[key] = CommentCounter(Path(state_dir))
        return _counters[key]
