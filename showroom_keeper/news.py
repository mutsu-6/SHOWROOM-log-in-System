"""管理画面の「ニュース」(集めた情報から、目立つギフター・ルーム・イベントの話題を選ぶ)。

SHOWROOM には問い合わせない。research.db と記録したギフトを読むだけ。重いので NEWS_TTL 秒使い回す。
話題 (直近 days 日に終わったイベント・記録したギフトから):
- big_gifts: 記録しているルームの大きなギフト (同じ人・同じルームの 10 分以内の続きは 1 つ)
- top_fans: イベントで 1 人がいちばん多く送ったルーム (貢献ランキングの 1 位の人のポイント)
- big_rooms: イベントでいちばん多く集めたルーム
- close_races: 1 位と 2 位の差の割合 (差 ÷ 1 位のポイント) がいちばん小さかったイベント
- comebacks: 終わる 24 時間前は下の順位だったのに、1 位で終えたルーム (1 時間ごとの推移から)
- last_shots: 終わる直前の一気投げ (終わる直前の細かい推移 event_final の、続く 2 回の間の増え方)
- new_stars: 今月の貢献が多いのに、前の月の記録が無いギフター
- top_month: 今月いちばん送っているギフター (イベントの貢献ランキングで見えた分)
- rising_rooms: 勢いが上り調子のルーム (ルームの分析。ルーム自身のポイントの伸び)
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .research_store import Store

NEWS_TTL = 1800  # 作った話題を使い回す秒数
TOP = 5  # 話題ごとに出す数
BIG_GIFT_MIN = 10_000  # 大きなギフトに数える 1 回のポイント
CLOSE_MIN_POINT = 100_000  # 接戦に数える、1 位のポイントの下限 (小さいイベントの僅差を除く)
COMEBACK_MIN_RANK = 3  # 大逆転に数える、終わる 24 時間前の順位 (これより下から 1 位)
LAST_WINDOW = 600  # 一気投げを探す、終わる前の秒数
NEW_STAR_POOL = 200  # 新星を探す、今月の多い順の人数
JST = timezone(timedelta(hours=9))

_cache: dict[tuple[Any, ...], tuple[float, dict[str, Any]]] = {}
_lock = threading.Lock()


def build(state_dir: Path, days: float = 7, now: float | None = None, use_cache: bool = True) -> dict[str, Any]:
    """話題 {topics: [{key, title, items}], days, since, now, made_at}。"""
    days = max(1.0, min(float(days), 31.0))
    key = (str(state_dir), days)
    with _lock:
        hit = _cache.get(key)
        if use_cache and now is None and hit and time.time() - hit[0] < NEWS_TTL:
            return hit[1]
        now = time.time() if now is None else now
        since = now - days * 86400
        with Store(state_dir) as s:
            from .feed import _catch_up

            _catch_up(state_dir, s)  # 記録したギフトの新しい分を足すよう、裏の仕事に頼む (待たずに、前に足した分で作る)
            names = _Names(s)
            topics = [
                ("big_gifts", "大きなギフト", _big_gifts(state_dir, s, since)),
                ("top_fans", "イベントで 1 人がいちばん送った", _top_fans(s, names, since, now)),
                ("big_rooms", "イベントでいちばん集めたルーム", _big_rooms(s, names, since, now)),
                ("close_races", "接戦のイベント", *_close_races(s, names, since, now)),
                ("comebacks", "最終日の大逆転", _comebacks(s, names, since, now)),
                ("last_shots", "終わる直前の一気投げ", _last_shots(s, names, since, now)),
                ("new_stars", "今月の新星ギフター", _new_stars(s, names, now)),
                ("top_month", "今月いちばん送っているギフター", _top_month(s, names, now)),
                ("rising_rooms", "上り調子のルーム", _rising_rooms(s)),
            ]
        # 4 つ目は、順位と pt が合わなくて載せなかった数 (接戦だけ)
        out = {"topics": [{"key": k, "title": t, "items": items, **({"skipped": rest[0]} if rest else {})} for k, t, items, *rest in topics],
               "days": days, "since": since, "now": now, "made_at": time.time()}
        _cache[key] = (time.time(), out)
        return out


class _Names:
    """ルーム・イベント・ユーザーの名前 (research.db から。何度も引く分は覚えておく)。"""

    def __init__(self, s: Store):
        self.s = s
        self._rooms: dict[int, dict[str, Any]] = {}
        self._events: dict[int, str] = {}
        self._users: dict[int, str] = {}

    def room(self, rid: int) -> dict[str, Any]:
        if rid not in self._rooms:
            r = self.s.one("SELECT key, name FROM rooms WHERE room_id=?", (rid,))
            self._rooms[rid] = {"room_id": rid, "room_key": (r["key"] if r else None) or "",
                                "room_name": (r["name"] if r else None) or f"ルーム {rid}"}
        return self._rooms[rid]

    def event(self, eid: int) -> dict[str, Any]:
        if eid not in self._events:
            r = self.s.one("SELECT name FROM events WHERE event_id=?", (eid,))
            self._events[eid] = (r["name"] if r else None) or f"イベント {eid}"
        return {"event_id": eid, "event_name": self._events[eid]}

    def user(self, uid: int) -> dict[str, Any]:
        if uid not in self._users:
            r = self.s.one("SELECT name FROM users WHERE user_id=?", (uid,))
            if not (r and r["name"]):
                r = self.s.one("SELECT name FROM gift_sender_names WHERE user_id=? ORDER BY at DESC LIMIT 1", (uid,))
            self._users[uid] = (r["name"] if r else "") or ""
        return {"user_id": uid, "name": self._users[uid]}


def _ended(s: Store, since: float, now: float) -> list[Any]:
    """期間のうちに終わったイベント (終わった時刻の索引で読む)。"""
    return s.all("SELECT event_id, end FROM events WHERE end>=? AND end<=?", (since, now))


def _big_gifts(state_dir: Path, s: Store, since: float) -> list[dict[str, Any]]:
    from . import feed

    rows = feed._big_gifts(state_dir, s, feed._Rooms(s, state_dir), since, BIG_GIFT_MIN)
    return sorted(rows, key=lambda x: -x["point"])[:TOP]


def _top_fans(s: Store, names: _Names, since: float, now: float) -> list[dict[str, Any]]:
    rows = s.all("""SELECT re.event_id, re.room_id, re.c_top1, re.point, re.rank FROM events e
                    JOIN room_events re ON re.event_id = e.event_id
                    WHERE e.end>=? AND e.end<=? AND re.c_top1>0 ORDER BY re.c_top1 DESC LIMIT ?""", (since, now, TOP))
    out = []
    for r in rows:
        top = s.one("SELECT user_id FROM contributions WHERE event_id=? AND room_id=? ORDER BY point DESC LIMIT 1",
                    (r["event_id"], r["room_id"]))
        share = r["c_top1"] / r["point"] if r["point"] else None
        out.append({**names.event(r["event_id"]), **names.room(r["room_id"]), **(names.user(top["user_id"]) if top else {}),
                    "point": r["c_top1"], "room_point": r["point"], "rank": r["rank"], "share": share})
    return out


def _big_rooms(s: Store, names: _Names, since: float, now: float) -> list[dict[str, Any]]:
    rows = s.all("""SELECT re.event_id, re.room_id, re.point, re.rank FROM events e JOIN room_events re ON re.event_id = e.event_id
                    WHERE e.end>=? AND e.end<=? AND re.point>0 ORDER BY re.point DESC LIMIT ?""", (since, now, TOP))
    return [{**names.event(r["event_id"]), **names.room(r["room_id"]), "point": r["point"], "rank": r["rank"]} for r in rows]


def _close_races(s: Store, names: _Names, since: float, now: float) -> tuple[list[dict[str, Any]], int]:
    """接戦のイベントと、順位と pt が合わなくて載せなかったイベントの数。

    room_events の順位と pt は、その時の参加ルームの一覧に出たルームだけ上書きされ、一覧から消えたルームの行は前の値のまま残る。
    このため「1 位」と「2 位」が別の時に読んだ値になることがある (1 位の pt が 2 位より少ない・1 位が 2 つある)。
    そういう回は、正しい接戦とは言えないので載せずに数だけ出す (行は消さず、直しもしない)。
    """
    out, odd = [], 0
    for e in _ended(s, since, now):
        top = s.all("SELECT room_id, point, rank FROM room_events WHERE event_id=? AND rank IN (1, 2) ORDER BY rank", (e["event_id"],))
        if len(top) < 2 or top[0]["rank"] != 1 or not top[0]["point"] or top[0]["point"] < CLOSE_MIN_POINT:
            continue
        if [r["rank"] for r in top] != [1, 2] or (top[1]["point"] or 0) > top[0]["point"]:
            odd += 1
            continue
        gap = top[0]["point"] - (top[1]["point"] or 0)
        out.append({**names.event(e["event_id"]), "first": {**names.room(top[0]["room_id"]), "point": top[0]["point"]},
                    "second": {**names.room(top[1]["room_id"]), "point": top[1]["point"]},
                    "gap": gap, "gap_share": gap / top[0]["point"]})
    return sorted(out, key=lambda x: x["gap_share"])[:TOP], odd


def _comebacks(s: Store, names: _Names, since: float, now: float) -> list[dict[str, Any]]:
    out = []
    for e in _ended(s, since, now):
        win = s.one("SELECT room_id, point FROM room_events WHERE event_id=? AND rank=1", (e["event_id"],))
        if not win:
            continue
        before = s.one("""SELECT rank, point FROM event_progress WHERE event_id=? AND room_id=? AND at<=?
                          ORDER BY at DESC LIMIT 1""", (e["event_id"], win["room_id"], e["end"] - 86400))
        if before and before["rank"] and before["rank"] >= COMEBACK_MIN_RANK:
            out.append({**names.event(e["event_id"]), **names.room(win["room_id"]), "before": before["rank"],
                        "point": win["point"], "gained": (win["point"] or 0) - (before["point"] or 0)})
    return sorted(out, key=lambda x: (-x["before"], -(x["gained"] or 0)))[:TOP]


def _last_shots(s: Store, names: _Names, since: float, now: float) -> list[dict[str, Any]]:
    out = []
    for e in _ended(s, since, now):
        last: dict[int, Any] = {}
        best = None
        for r in s.all("SELECT at, room_id, point, rank FROM event_final WHERE event_id=? AND at>=? AND at<=? ORDER BY at",
                       (e["event_id"], e["end"] - LAST_WINDOW - 120, e["end"] + 60)):
            prev = last.get(r["room_id"])
            if prev is not None and r["point"] > prev["point"] and r["at"] >= e["end"] - LAST_WINDOW:
                jump = r["point"] - prev["point"]
                if best is None or jump > best["jump"]:
                    best = {"jump": jump, "room_id": r["room_id"], "left": max(0, e["end"] - r["at"]),
                            "before": prev["rank"], "after": r["rank"], "point": r["point"]}
            last[r["room_id"]] = r
        if best:
            out.append({**names.event(e["event_id"]), **names.room(best.pop("room_id")), **best})
    return sorted(out, key=lambda x: -x["jump"])[:TOP]


def _month(now: float) -> str:
    return datetime.fromtimestamp(now, JST).strftime("%Y-%m")


def _new_stars(s: Store, names: _Names, now: float) -> list[dict[str, Any]]:
    month = _month(now)
    out = []
    for r in s.all("SELECT user_id, points FROM user_month WHERE month=? AND points>0 ORDER BY points DESC LIMIT ?", (month, NEW_STAR_POOL)):
        if s.one("SELECT 1 FROM user_month WHERE user_id=? AND month<? AND points>0 LIMIT 1", (r["user_id"], month)):
            continue
        out.append({**names.user(r["user_id"]), "point": r["points"],
                    "rooms": s.scalar("SELECT COUNT(*) FROM user_room WHERE user_id=?", (r["user_id"],))})
        if len(out) >= TOP:
            break
    return out


def _top_month(s: Store, names: _Names, now: float) -> list[dict[str, Any]]:
    return [{**names.user(r["user_id"]), "point": r["points"]}
            for r in s.all("SELECT user_id, points FROM user_month WHERE month=? ORDER BY points DESC LIMIT ?", (_month(now), TOP))]


def _rising_rooms(s: Store) -> list[dict[str, Any]]:
    rows = s.all("""SELECT p.room_id, p.momentum, p.momentum_people, p.momentum_detail, p.type, r.key, r.name, r.followers
                    FROM room_profile p LEFT JOIN rooms r USING(room_id)
                    WHERE p.momentum_label='上り調子' ORDER BY p.momentum DESC LIMIT ?""", (TOP,))
    return [{"room_id": r["room_id"], "room_key": r["key"] or "", "room_name": r["name"] or f"ルーム {r['room_id']}",
             "momentum": r["momentum"], "people": r["momentum_people"], "detail": r["momentum_detail"],
             "type": r["type"], "followers": r["followers"]} for r in rows]
