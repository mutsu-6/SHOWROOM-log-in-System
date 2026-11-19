"""管理画面の「ニュース」(集めた情報から、目立つギフター・ルーム・イベントの話題を選ぶ)。

SHOWROOM には問い合わせない。research.db と記録したギフトを読むだけ。重いので NEWS_TTL 秒使い回す。
話題 (直近 days 日に終わったイベント・記録したギフトから):
- big_gifts: 記録しているルームの大きなギフト (同じ人・同じルームの 10 分以内の続きは 1 つ)
- top_fans: イベントで 1 人がいちばん多く送ったルーム (貢献ランキングの 1 位の人のポイント)
- big_rooms: イベントでいちばん多く集めたルーム
- close_races: 1 位と 2 位の差の割合 (差 ÷ 1 位のポイント) がいちばん小さかったイベント
- comebacks: 終わる 24 時間前は下の順位だったのに、1 位で終えたルーム (1 時間ごとの推移から)
- last_shots: 終わる直前の一気投げ (終わる直前の細かい推移 event_final の、続く 2 回の間の増え方)。
  イベントごとに 1 行で残し、画面は新しい順に 30 件 (選んだ日数より前に終わった分も出す)
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
SHOT_AFTER = 60  # 終わるあと、この秒までの推移を一気投げに数える
SHOT_SHOW = 30  # 画面に出す件数。表には終わったイベントを全部 1 行で残す (1 行なので軽い)
REMEMBER_SCAN = 40  # 収集の 1 回で、一気投げを残し終えたか確かめるイベントの数
REMEMBER_HEAVY = 5  # そのうち、細かい推移を読んでまとめる件数 (1 イベント数千行になることがある)
RECENT_FILL = 5  # ニュースを開いた時に、直近の未記録を足す件数
RECENT_DAYS = 31  # その対象 (ニュースの期間の上限と同じ)
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
                ("last_shots", "終わる直前の一気投げ（新しい 30 件）", _last_shots(s, names, since, now)),
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
    このため「1 位」と「2 位」が別の時に読んだ値になっている可能性がある (見立て。実データではまだ確かめていない)。
    1 位と 2 位がちょうど 1 行ずつでない・2 位の pt が無い・1 位の pt が 2 位より少ない回は、くらべられないので載せずに数だけ出す
    (行は消さず、直しもしない)。1 位の行が無い・2 位の行が無い (参加が 1 ルーム) 回と、1 位の pt が小さい回は、接戦の候補でないので数えない。
    """
    out, odd = [], 0
    for e in _ended(s, since, now):
        top = s.all("SELECT room_id, point, rank FROM room_events WHERE event_id=? AND rank IN (1, 2) ORDER BY rank", (e["event_id"],))
        if len(top) < 2 or top[0]["rank"] != 1 or not top[0]["point"] or top[0]["point"] < CLOSE_MIN_POINT:
            continue
        if [r["rank"] for r in top] != [1, 2] or top[1]["point"] is None or top[1]["point"] > top[0]["point"]:
            odd += 1
            continue
        gap = top[0]["point"] - top[1]["point"]
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


def _best_jump(s: Store, event_id: int, end: float) -> dict[str, Any] | None:
    """終わる 10 分前からの細かい推移で、続く 2 回のあいだにいちばん増えたところ。無ければ None。"""
    last: dict[int, Any] = {}
    best = None
    for r in s.all("SELECT at, room_id, point, rank FROM event_final WHERE event_id=? AND at>=? AND at<=? ORDER BY at",
                   (event_id, end - LAST_WINDOW - 120, end + SHOT_AFTER)):
        prev = last.get(r["room_id"])
        if prev is not None and r["point"] > prev["point"] and r["at"] >= end - LAST_WINDOW:
            jump = r["point"] - prev["point"]
            if best is None or jump > best["jump"]:
                best = {"jump": jump, "room_id": r["room_id"], "left": max(0.0, end - r["at"]),
                        "before": prev["rank"], "after": r["rank"], "point": r["point"], "at": r["at"]}
        last[r["room_id"]] = r
    return best


def _save_event_shot(s: Store, event_id: int, end: float, now: float) -> None:
    s.save_last_shot(event_id, end, _best_jump(s, event_id, end), now)


def remember_pending(s: Store, now: float, scan: int = REMEMBER_SCAN, heavy: int = REMEMBER_HEAVY) -> int:
    """終わってから SHOT_AFTER 秒たったイベントの一気投げを、古い順に少しずつ 1 行で残す。残す分があれば 1。

    SHOWROOM には問い合わせない。細かい推移の無いイベントは印だけ残して先へ進む。
    """
    cursor_end = s.meta("last_shot_end") or 0
    cursor_id = int(s.meta("last_shot_event") or 0)
    rows = s.all("""SELECT event_id, end FROM events
                    WHERE end IS NOT NULL AND end <= ?
                      AND (end > ? OR (end = ? AND event_id > ?))
                    ORDER BY end, event_id LIMIT ?""",
                 (now - SHOT_AFTER, cursor_end, cursor_end, cursor_id, int(scan)))
    if not rows:
        if cursor_end < now - SHOT_AFTER:
            s.set_meta("last_shot_end", now - SHOT_AFTER)
            s.set_meta("last_shot_event", 0)
        return 0
    done = 0
    last = None
    for r in rows:
        has = s.one("SELECT 1 FROM event_final WHERE event_id=? LIMIT 1", (r["event_id"],))
        if has and done >= heavy:
            break
        if has:
            done += 1
        _save_event_shot(s, r["event_id"], r["end"], now)
        last = r
    if last is not None:
        s.set_meta("last_shot_end", last["end"])
        s.set_meta("last_shot_event", last["event_id"])
    return 0 if last is None else 1


def _fill_recent(s: Store, now: float) -> None:
    """直近のイベントで、まだ残し終えていない一気投げを新しい順に少し足す (収集が古い方から辿る間も、画面に出すため)。"""
    rows = s.all("""SELECT event_id, end FROM events e
                    WHERE e.end IS NOT NULL AND e.end <= ? AND e.end >= ?
                      AND EXISTS (SELECT 1 FROM event_final f WHERE f.event_id = e.event_id)
                      AND NOT EXISTS (SELECT 1 FROM last_shots ls
                                      WHERE ls.event_id = e.event_id AND ls.saved_at >= e.end + ?)
                    ORDER BY e.end DESC LIMIT ?""",
                 (now - SHOT_AFTER, now - RECENT_DAYS * 86400, SHOT_AFTER, RECENT_FILL))
    for r in rows:
        _save_event_shot(s, r["event_id"], r["end"], now)


def _last_shots(s: Store, names: _Names, since: float, now: float) -> list[dict[str, Any]]:
    """残している一気投げを、新しい順に SHOT_SHOW 件。選んだ日数より前に終わった分も出す。"""
    for e in _ended(s, since, now):
        _save_event_shot(s, e["event_id"], e["end"], now)
    _fill_recent(s, now)
    remember_pending(s, now)
    rows = s.all("""SELECT event_id, room_id, jump, left_sec, rank_before, rank_after, point
                    FROM last_shots WHERE jump > 0 ORDER BY event_end DESC, event_id DESC LIMIT ?""", (SHOT_SHOW,))
    return [{**names.event(r["event_id"]), **names.room(r["room_id"]), "jump": r["jump"], "left": r["left_sec"],
             "before": r["rank_before"], "after": r["rank_after"], "point": r["point"]} for r in rows]


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
