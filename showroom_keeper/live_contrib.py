"""開催中のイベントの貢献ランキングを、イベントのページを開いた時だけ SHOWROOM から読む (管理画面の「今の貢献者」)。

軽くするための決まり:
- 読むのはページを開いている時だけ (見張りの仕事は増やさない)。ポイントの多い LIVE_ROOMS ルームまで
- 読んだ結果はメモリに LIVE_TTL 秒だけ置いて使い回す (何人・何回開いても、その間は問い合わせない)。置くのは LIVE_EVENTS イベントまで
- research.db には書かない (終わってからの取り込みは今までどおり。書くとギフターの合計などを数え直すことになる)
- 問い合わせは research.THROTTLE を通す。SHOWROOM に止められて休んでいる時は読まない (待たせない)
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, Callable

from .research_store import Store

LIVE_ROOMS = 10  # 読むルームの数 (今のポイントの多い順)
LIVE_TTL = 180  # 読んだ結果を使い回す秒数
LIVE_EVENTS = 4  # メモリに置くイベントの数
LIVE_CONTRIBUTORS = 100  # 返す貢献者の数 (全ルームの合計の多い順)

_cache: dict[int, dict[str, Any]] = {}
_lock = threading.Lock()  # 同じ時に 2 つ開いても、2 回は読まない


def forget() -> None:
    with _lock:
        _cache.clear()


def _default_get(path: str) -> Any:
    from .gifts import _get
    from .research import THROTTLE

    return THROTTLE.call(_get, path)


def _paused() -> bool:
    from .research import THROTTLE

    return THROTTLE.state()["paused"] > 0


def event_live(state_dir: Path, event_id: Any, get: Callable[[str], Any] | None = None,
               now: float | None = None, paused: Callable[[], bool] | None = None) -> dict[str, Any]:
    """開催中のイベントの、今の貢献ランキング (ポイントの多いルームから LIVE_ROOMS ルーム分)。"""
    ref = str(event_id or "").strip()
    if not ref.isdigit():
        raise ValueError("イベント番号を指定してください")
    eid = int(ref)
    now = time.time() if now is None else now
    with Store(state_dir) as s:
        e = s.one("SELECT event_id, start, end, show_ranking FROM events WHERE event_id=?", (eid,))
        if e is None:
            return {"live": False, "why": "このイベントの情報はまだ集めていません"}
        if not (e["start"] and float(e["start"]) <= now and (not e["end"] or now < float(e["end"]))):
            return {"live": False, "why": "開催中ではありません"}
        if not e["show_ranking"]:
            return {"live": False, "why": "このイベントは貢献ランキングを公開していません"}
        rooms = [dict(r) for r in s.all(
            """SELECT re.room_id, COALESCE(r.name, r.key, re.room_id) AS name, re.point FROM room_events re
               LEFT JOIN rooms r USING(room_id) WHERE re.event_id=? ORDER BY re.point DESC LIMIT ?""", (eid, LIVE_ROOMS))]
        room_total = s.scalar("SELECT COUNT(*) FROM room_events WHERE event_id=?", (eid,))
    with _lock:
        hit = _cache.get(eid)
        if hit is not None and now - hit["at"] < LIVE_TTL:
            return hit
        if (paused or _paused)():
            if hit is not None:
                return {**hit, "stale": True}
            return {"live": False, "why": "SHOWROOM への問い合わせを休んでいるので、今は読みません"}
        result = _fetch(eid, rooms, room_total, get or _default_get, now)
        _cache[eid] = result
        for old in sorted(_cache, key=lambda k: _cache[k]["at"])[:-LIVE_EVENTS]:
            del _cache[old]
        return result


def _fetch(eid: int, rooms: list[dict[str, Any]], room_total: int, get: Callable[[str], Any], now: float) -> dict[str, Any]:
    by_room: dict[str, list[dict[str, Any]]] = {}
    failed = 0
    for r in rooms:
        try:
            data = get(f"/api/event/contribution_ranking?event_id={eid}&room_id={r['room_id']}") or {}
        except Exception:  # noqa: BLE001 - 1 ルーム読めなくても、ほかは出す
            failed += 1
            continue
        rows = [{"user_id": int(x["user_id"]), "name": x.get("name") or "", "point": int(x.get("point") or 0), "rank": x.get("rank")}
                for x in data.get("ranking") or [] if isinstance(x, dict) and x.get("user_id")]
        by_room[str(r["room_id"])] = rows
        r["contributors"] = len(rows)
    total: dict[int, dict[str, Any]] = {}
    for room in rooms:
        for x in by_room.get(str(room["room_id"]), []):
            t = total.setdefault(x["user_id"], {"user_id": x["user_id"], "name": x["name"], "point": 0, "rooms": 0,
                                                "best_rank": None, "top_room": None, "_top": -1})
            t["point"] += x["point"]
            t["rooms"] += 1
            if x["rank"] and (t["best_rank"] is None or x["rank"] < t["best_rank"]):
                t["best_rank"] = x["rank"]
            if x["point"] > t["_top"]:
                t["_top"], t["top_room"] = x["point"], room["name"]
    people = sorted(total.values(), key=lambda t: -t["point"])
    for t in people:
        del t["_top"]
    return {"live": True, "at": now, "event_id": eid, "rooms": rooms, "room_total": room_total, "failed": failed,
            "by_room": by_room, "contributors": people[:LIVE_CONTRIBUTORS], "contributor_total": len(people), "ttl": LIVE_TTL}
