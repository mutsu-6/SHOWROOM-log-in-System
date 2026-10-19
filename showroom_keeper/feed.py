"""管理画面の「お知らせ」(管理画面の中だけで出す、最近の出来事の一覧)。

集めた情報 (research.db・記録したギフト) を読むだけで作る。SHOWROOM には問い合わせない。
出すもの (新しい順):
- big_gift: 記録しているルームの大きなギフト (1 回に min_point pt 以上。同じ人・同じルームの 10 分以内の続きは 1 つにまとめる)
- watch_gift / watch_visit: 気になるギフターが、記録しているルームでギフトを送った・来場した
- live_start / live_end: 視聴するルーム・応援ルームの配信が始まった・終わった
- event_rank / event_ending / event_end: 応援ルームの、イベントの順位が変わった・終わりが近い・終わった
- showroom_notice: SHOWROOM のお知らせ
すぐ直す必要のあること (ログイン切れ・ディスクなど) は、管理画面が /api/overview の alerts から足す (ここでは作らない)。
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

from .research_store import Store

FEED_TTL = 60  # 作った一覧を使い回す秒数 (画面は 1 分ごとに読み直す。何人もが開いても数え直さない)
MAX_HOURS = 7 * 24  # さかのぼれる時間の上限
DEFAULT_MIN_POINT = 10_000  # 大きなギフトの、1 回のポイントの基準 (画面で変えられる)
BURST_GAP = 600  # 同じ人・同じルームの大きなギフトを 1 つにまとめる間 (秒)
LIVE_GONE = 900  # 最後に配信中と見えてからこれだけたったら、終わったとみる (配信中の一覧は 5 分ごと)
ENDING_SOON = 86400  # イベントの終わりがこれより近くなったら知らせる
MAX_ITEMS = {"big_gift": 200, "watch_gift": 100, "watch_visit": 100, "live": 100, "event": 100, "showroom_notice": 30}

_cache: dict[tuple[Any, ...], tuple[float, dict[str, Any]]] = {}
_lock = threading.Lock()


def build(state_dir: Path, support: list[str], watch_rooms: list[str], hours: float = 24,
          min_point: int = DEFAULT_MIN_POINT, now: float | None = None, use_cache: bool = True) -> dict[str, Any]:
    """お知らせの一覧 {items: [...], since, now, min_point}。items は新しい順。"""
    hours = max(1.0, min(float(hours), MAX_HOURS))
    min_point = max(1, int(min_point))
    key = (str(state_dir), tuple(support), tuple(watch_rooms), hours, min_point)
    with _lock:  # 同時に開いても、数えるのは 1 回だけ
        hit = _cache.get(key)
        if use_cache and now is None and hit and time.time() - hit[0] < FEED_TTL:
            return hit[1]
        now = time.time() if now is None else now
        since = now - hours * 3600
        with Store(state_dir) as s:
            _catch_up(state_dir, s)
            rooms = _Rooms(s, state_dir)
            items = (_big_gifts(state_dir, s, rooms, since, min_point) + _watched(state_dir, s, rooms, since)
                     + _lives(s, rooms, dict.fromkeys([*watch_rooms, *support]), since, now)
                     + _events(s, rooms, support, since, now) + _notices(s, since))
        items.sort(key=lambda x: x["at"], reverse=True)
        out = {"items": items, "since": since, "now": now, "hours": hours, "min_point": min_point}
        _cache[key] = (time.time(), out)
        return out


def _catch_up(state_dir: Path, s: Store) -> None:
    """記録のファイルの新しい分だけを、ギフト 1 件ずつの表と来場の表に足す (ふだんは 10 分ごとに収集の合間に足してある)。"""
    from .explore import update_all_gift_senders, update_all_live_visits

    for fn in (update_all_gift_senders, update_all_live_visits):
        try:
            fn(state_dir, s)
        except Exception:  # noqa: BLE001 - 足せなくても、前に足した分で作る
            pass


class _Rooms:
    """ルームの名前と room_id (覚えているルーム名と、research.db の rooms 表から)。"""

    def __init__(self, s: Store, state_dir: Path):
        from .listing import room_names

        self.s = s
        self.names = room_names(state_dir)

    def name(self, key: str) -> str:
        known = (self.names.get(key) or {}).get("name")
        if known:
            return known
        row = self.s.one("SELECT name FROM rooms WHERE key=?", (key,))
        return (row["name"] if row else None) or key

    def id(self, key: str) -> int | None:
        rid = (self.names.get(key) or {}).get("room_id")
        if rid:
            return int(rid)
        row = self.s.one("SELECT room_id FROM rooms WHERE key=?", (key,))
        return int(row["room_id"]) if row else None


def _user_names(s: Store, uids: set[int]) -> dict[int, str]:
    """ユーザーの名前 (ランキングで分かった名前、無ければ記録したギフトの最後の名前)。"""
    out: dict[int, str] = {}
    for uid in uids:
        row = s.one("SELECT name FROM users WHERE user_id=?", (uid,))
        if not (row and row["name"]):
            row = s.one("SELECT name FROM gift_sender_names WHERE user_id=? ORDER BY at DESC LIMIT 1", (uid,))
        out[uid] = (row["name"] if row else "") or ""
    return out


def _catalog(state_dir: Path) -> dict[int, dict[str, Any]]:
    """保存してあるギフトの一覧 (ここでは SHOWROOM に取りに行かない)。"""
    from .listing import _read_catalog

    return (_read_catalog(state_dir) or ({}, {}))[0]


def _big_gifts(state_dir: Path, s: Store, rooms: _Rooms, since: float, min_point: int) -> list[dict[str, Any]]:
    catalog = _catalog(state_dir)
    paid = [(gid, int(g.get("point") or 0)) for gid, g in catalog.items() if not g.get("free", True) and int(g.get("point") or 0) > 0]
    if not paid:
        return []
    s.conn.execute("CREATE TEMP TABLE IF NOT EXISTS feed_points (gift_id INTEGER PRIMARY KEY, point INTEGER)")
    s.conn.execute("DELETE FROM temp.feed_points")
    s.conn.executemany("INSERT INTO temp.feed_points(gift_id, point) VALUES (?, ?)", paid)
    # ルームごとに索引 (room_key, at) で直近の分だけを読む (gift_events は全部で数千万行になる)。
    # CROSS JOIN で、ルームの表 (gift_event_pos) から先に読む順を決める (ふつうの JOIN だと gift_events を全部読むことがある)
    rows = s.all("""SELECT e.room_key, e.user_id, e.at, e.gift_id, e.num, p.point FROM gift_event_pos r
                    CROSS JOIN gift_events e ON e.room_key = r.room_key AND e.at >= ?
                    JOIN temp.feed_points p ON p.gift_id = e.gift_id
                    WHERE e.num * p.point >= ?""", (since, min_point))
    rows.sort(key=lambda r: (r["room_key"], r["user_id"], r["at"]))
    out: list[dict[str, Any]] = []
    for r in rows:
        pt, name = r["num"] * r["point"], catalog.get(r["gift_id"], {}).get("gift_name") or f"ギフト{r['gift_id']}"
        last = out[-1] if out else None
        if last and last["room_key"] == r["room_key"] and last["user_id"] == r["user_id"] and r["at"] - last["at"] <= BURST_GAP:
            last.update(at=r["at"], point=last["point"] + pt, times=last["times"] + 1)
            last["gifts"][name] = last["gifts"].get(name, 0) + r["num"]
            continue
        out.append({"kind": "big_gift", "level": "high", "at": r["at"], "first_at": r["at"], "room_key": r["room_key"],
                    "user_id": r["user_id"], "point": pt, "times": 1, "gifts": {name: r["num"]}})
    out = sorted(out, key=lambda x: x["at"], reverse=True)[:MAX_ITEMS["big_gift"]]
    names = _user_names(s, {x["user_id"] for x in out})
    for x in out:
        x.update(name=names.get(x["user_id"], ""), room_name=rooms.name(x["room_key"]),
                 gifts=[{"name": k, "num": v} for k, v in x["gifts"].items()])
    return out


def _watched(state_dir: Path, s: Store, rooms: _Rooms, since: float) -> list[dict[str, Any]]:
    """気になるギフターの、記録しているルームでのギフト (人 × ルームごとの合計) と来場。"""
    watch = {int(r["user_id"]): (r["memo"] or "") for r in s.all("SELECT user_id, memo FROM watch")}
    if not watch:
        return []
    catalog = _catalog(state_dir)
    names = _user_names(s, set(watch))
    gifts: dict[tuple[int, str], dict[str, Any]] = {}
    visits: list[dict[str, Any]] = []
    for uid, memo in watch.items():
        base = {"user_id": uid, "name": names.get(uid, ""), "memo": memo}
        for r in s.all("SELECT at, room_key, gift_id, num FROM gift_events WHERE user_id=? AND at>=? ORDER BY at", (uid, since)):
            info = catalog.get(r["gift_id"]) or {}
            x = gifts.setdefault((uid, r["room_key"]), {"kind": "watch_gift", **base, "room_key": r["room_key"],
                                                        "first_at": r["at"], "points": 0, "paid_points": 0, "times": 0})
            pt = int(info.get("point") or 0) * int(r["num"] or 1)
            x["points"] += pt
            x["paid_points"] += 0 if info.get("free", True) else pt
            x["times"] += 1
            x["at"] = r["at"]
        for r in s.all("SELECT at, room_key, n FROM live_visits WHERE user_id=? AND at>=? ORDER BY at", (uid, int(since))):
            visits.append({"kind": "watch_visit", "level": "normal", **base, "at": r["at"], "room_key": r["room_key"], "n": r["n"]})
    for x in gifts.values():  # 無料の星・種だけなら、大事なものにはしない
        x["level"] = "high" if x["paid_points"] > 0 else "normal"
    out = sorted(gifts.values(), key=lambda x: x["at"], reverse=True)[:MAX_ITEMS["watch_gift"]]
    out += sorted(visits, key=lambda x: x["at"], reverse=True)[:MAX_ITEMS["watch_visit"]]
    for x in out:
        x["room_name"] = rooms.name(x["room_key"])
    return out


def _lives(s: Store, rooms: _Rooms, keys: dict[str, None], since: float, now: float) -> list[dict[str, Any]]:
    """視聴するルーム・応援ルームの配信の始まりと終わり (配信中のルームの一覧から 5 分ごとに残した room_lives)。"""
    out: list[dict[str, Any]] = []
    for key in keys:
        rid = rooms.id(key)
        if rid is None:
            continue
        base = {"room_key": key, "room_name": rooms.name(key)}
        for r in s.all("""SELECT started_at, last_seen, view_max, followers_start, followers_end FROM room_lives
                          WHERE room_id=? AND (started_at>=? OR last_seen>=?) ORDER BY started_at""", (rid, int(since), since)):
            if r["started_at"] >= since:
                out.append({"kind": "live_start", "level": "normal", "at": float(r["started_at"]), **base})
            if r["last_seen"] and now - r["last_seen"] > LIVE_GONE and r["last_seen"] >= since:
                gained = (r["followers_end"] - r["followers_start"]) if r["followers_end"] is not None and r["followers_start"] is not None else None
                out.append({"kind": "live_end", "level": "normal", "at": float(r["last_seen"]), **base,
                            "minutes": round((r["last_seen"] - r["started_at"]) / 60), "view_max": r["view_max"], "followers": gained})
    return sorted(out, key=lambda x: x["at"], reverse=True)[:MAX_ITEMS["live"]]


def _events(s: Store, rooms: _Rooms, support: list[str], since: float, now: float) -> list[dict[str, Any]]:
    """応援ルームの、開催中・終わったばかりのイベント: 順位が変わった (1 時間ごとの推移から)・終わりが近い・終わった。"""
    out: list[dict[str, Any]] = []
    for key in dict.fromkeys(support):
        rid = rooms.id(key)
        if rid is None:
            continue
        base = {"room_key": key, "room_name": rooms.name(key)}
        for e in s.all("""SELECT re.event_id, re.point, re.rank, e.name, e.end FROM room_events re JOIN events e USING(event_id)
                          WHERE re.room_id=? AND e.end>=?""", (rid, since)):
            ev = {**base, "event_id": e["event_id"], "event_name": e["name"] or f"イベント {e['event_id']}", "end": e["end"]}
            prev = None
            for p in s.all("SELECT at, rank, point FROM event_progress WHERE event_id=? AND room_id=? AND at>=? ORDER BY at",
                           (e["event_id"], rid, since - 2 * 3600)):
                if prev is not None and p["rank"] and prev["rank"] and p["rank"] != prev["rank"] and p["at"] >= since:
                    out.append({"kind": "event_rank", "level": "high" if p["rank"] < prev["rank"] and p["rank"] <= 3 else "normal",
                                "at": p["at"], **ev, "before": prev["rank"], "after": p["rank"], "point": p["point"]})
                prev = p
            if e["end"] <= now:
                out.append({"kind": "event_end", "level": "high", "at": e["end"], **ev, "rank": e["rank"], "point": e["point"]})
            elif e["end"] - now <= ENDING_SOON and e["end"] - ENDING_SOON >= since:
                out.append({"kind": "event_ending", "level": "normal", "at": e["end"] - ENDING_SOON, **ev,
                            "rank": prev["rank"] if prev else e["rank"], "point": prev["point"] if prev else e["point"]})
    return sorted(out, key=lambda x: x["at"], reverse=True)[:MAX_ITEMS["event"]]


def _notices(s: Store, since: float) -> list[dict[str, Any]]:
    return [{"kind": "showroom_notice", "level": "normal", "at": r["created_at"], "notice_id": r["notice_id"],
             "title": r["title"] or "", "label": r["label"] or ""}
            for r in s.all("SELECT notice_id, label, title, created_at FROM notices WHERE created_at>=? ORDER BY created_at DESC LIMIT ?",
                           (since, MAX_ITEMS["showroom_notice"]))]
