"""配信ごとの順位 (ステージの上位 100 人。SHOWROOM の /api/live/stage_user_list。ポイントは出ない)。

ギフトを記録しているルームで、配信中に STAGE_EVERY ごとと、配信の終わりに読み、<state_dir>/live_ranks/<ルーム>.jsonl に足す。
イベントの外の、ふだんの配信でだれが上位にいるかを見るため (ポイントは記録したギフトから出す)。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


def rank_dir(state_dir: Path) -> Path:
    return Path(state_dir) / "live_ranks"


def append(state_dir: Path, room: str, live_id: Any, rows: list[list[Any]], now: float | None = None) -> None:
    path = rank_dir(state_dir) / f"{room}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    line = {"at": int(time.time() if now is None else now), "live_id": live_id, "ranks": rows}
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(line) + "\n")


def last_per_live(state_dir: Path, room: str, since: float | None = None) -> dict[Any, dict[str, Any]]:
    """配信ごとの、最後に読んだ順位 {live_id: {"at", "ranks": [[順位, ユーザー ID], ...]}}。"""
    out: dict[Any, dict[str, Any]] = {}
    try:
        lines = (rank_dir(state_dir) / f"{room}.jsonl").read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for line in lines:
        try:
            x = json.loads(line)
        except ValueError:
            continue
        if isinstance(x, dict) and (since is None or x.get("at", 0) >= since):
            if x.get("live_id") not in out or x["at"] >= out[x["live_id"]]["at"]:
                out[x.get("live_id")] = x
    return out
