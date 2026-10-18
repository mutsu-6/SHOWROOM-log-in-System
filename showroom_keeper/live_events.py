"""配信中のシステムの知らせ (来場・ファンレベルが上がった・ファンの人数・ギフトランキング) を残す。

リアルタイム通信の t=18 (来場・ファンレベル)、t=1001 (ギフトランキングの順位)、t=5 (数字の知らせ。中身はまだ分からない)
を、ギフトと同じように <state_dir>/live_events/<ルーム>.jsonl に 1 行ずつ書く。
来場とファンレベルは、ポイントに出ない常連が「何回目の来場か」「何か月続けて通っているか」を見るため。
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

_VISIT = re.compile(r"さんが(?:(初)訪問|(\d+)[回度]目の訪問)")
_LEVEL = re.compile(r"ファンレベルが(\d+)にあがりました！?(?:[(（](.*?)[)）])?\s*$")
_STREAK = re.compile(r"(\d+)ヶ月連続(\d+)回目")
_AGAIN = re.compile(r"(\d+)ヶ月ぶり(\d+)回目")
_FANS = re.compile(r"が(\d+)人になりました")


def event_dir(state_dir: Path) -> Path:
    return Path(state_dir) / "live_events"


def parse(data: dict[str, Any]) -> dict[str, Any] | None:
    """リアルタイム通信の 1 件を、残す形にする (残さないものは None)。分けられた知らせは、中身 (回数・レベル) だけを残す。
    分けられなかった知らせは、形を見てあとで分けるため、本文もそのまま残す。"""
    t = data.get("t")
    if t == 18:
        m = str(data.get("m") or "")
        out: dict[str, Any] = {"user_id": data.get("u")}
        v = _VISIT.search(m)
        lv = _LEVEL.search(m)
        if v:
            out.update(kind="visit", n=1 if v.group(1) else int(v.group(2)))
        elif lv:
            out.update(kind="fan_level", level=int(lv.group(1)))
            note = lv.group(2) or ""
            if "はじめて" in note:
                out["first"] = True
            elif s := _STREAK.search(note):
                out.update(streak=int(s.group(1)), times=int(s.group(2)))
            elif a := _AGAIN.search(note):
                out.update(gap=int(a.group(1)), times=int(a.group(2)))
        elif f := _FANS.search(m):
            out.update(kind="fans", n=int(f.group(1)))
        else:
            out.update(kind="other", m=m[:200])  # まだ分けていない知らせ (形を見て、あとで分ける)
        return out
    if t == 1001:
        return {"kind": "notice", "m": str(data.get("message") or "")[:200]}
    if t == 5 and data.get("p") is not None:
        return {"kind": "p5", "p": data.get("p"), "c": data.get("c")}
    return None


def record(state_dir: Path, room: str, live_id: Any, data: dict[str, Any]) -> bool:
    """残すものなら 1 行書く。戻り値は書いたか。"""
    ev = parse(data) if isinstance(data, dict) else None
    if ev is None:
        return False
    ev = {"at": int(data.get("created_at") or time.time()), "live_id": live_id, **ev}
    path = event_dir(state_dir) / f"{room}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(ev, ensure_ascii=False) + "\n")
    return True


def load(state_dir: Path, room: str, since: float | None = None) -> list[dict[str, Any]]:
    path = event_dir(state_dir) / f"{room}.jsonl"
    out = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    for line in lines:
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if isinstance(ev, dict) and (since is None or ev.get("at", 0) >= since):
            out.append(ev)
    return out
