"""管理画面のルーム・イベントのページと、上の検索欄に出す情報 (research.db を読むだけ)。

SHOWROOM には問い合わせない。分析 (research.analyze) のように全体を数え直さず、
1 つのルーム・イベントの分だけを、索引のある列から読む。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from .research import LEFT_NAMES, PENDING_RANKING, RANKING_RETRIES
from .research_store import Store

SEARCH_LIMIT = 20  # 検索で種類ごとに返す数
ROOM_GIFTERS = 200  # ルームのページに出すギフターの数
ROOM_EVENTS = 300  # ルームのページに出すイベントの数
OVERLAP_GIFTERS = 300  # 重なるルームを調べる、このルームのギフターの数 (多い順)
OVERLAP_ROOMS = 20  # 重なるルームとして出す数
EVENT_ROOMS = 500  # イベントのページに出す参加ルームの数
EVENT_LIST = 200  # イベントの一覧に出す数 (終わったもの)
PROGRESS_ROOMS = 10  # 推移のグラフに出すルームの数 (今のポイントの多い順)
CONTRIBUTORS = 100  # イベントのページに出す貢献者の数


def _event_state(e: Any, now: float) -> str:
    if e["start"] and e["start"] > now:
        return "upcoming"
    if e["end"] and e["end"] <= now:
        return "ended"
    return "ongoing" if e["start"] else "unknown"


def _event_row(e: Any, now: float) -> dict[str, Any]:
    return {"event_id": e["event_id"], "key": e["key"], "name": e["name"] or f"イベント {e['event_id']}",
            "start": e["start"], "end": e["end"], "state": _event_state(e, now)}


# ------------------------------------------------------------------ 検索
def search(state_dir: Path, q: str) -> dict[str, Any]:
    """上の検索欄: ギフター (名前・ユーザー ID)・ルーム (名前・URL キー)・イベント (名前・番号) を探す。"""
    q = str(q or "").strip()[:100]
    if not q:
        return {"gifters": [], "rooms": [], "events": []}
    like = f"%{q.replace('%', '').replace('_', '')}%"
    digits = q.isdigit()
    now = time.time()
    with Store(state_dir) as s:
        gifters = [dict(r) for r in s.all(
            """SELECT u.user_id, u.name, (SELECT SUM(MAX(ur.cumulative, ur.events)) FROM user_room ur WHERE ur.user_id=u.user_id) AS points
               FROM users u WHERE u.user_id = ? OR u.name LIKE ? ORDER BY u.user_id = ? DESC, points DESC LIMIT ?""",
            (int(q) if digits else -1, like, int(q) if digits else -1, SEARCH_LIMIT))]
        rooms = [dict(r) for r in s.all(
            """SELECT room_id, key, name, followers, live_at FROM rooms
               WHERE room_id = ? OR key = ? OR name LIKE ? OR key LIKE ?
               ORDER BY room_id = ? DESC, key = ? DESC, COALESCE(followers, 0) DESC LIMIT ?""",
            (int(q) if digits else -1, q, like, like, int(q) if digits else -1, q, SEARCH_LIMIT))]
        events = [_event_row(r, now) for r in s.all(
            """SELECT * FROM events WHERE event_id = ? OR key = ? OR name LIKE ?
               ORDER BY event_id = ? DESC, COALESCE(end, 0) DESC LIMIT ?""",
            (int(q) if digits else -1, q, like, int(q) if digits else -1, SEARCH_LIMIT))]
    return {"gifters": gifters, "rooms": rooms, "events": events}


# ------------------------------------------------------------------ ルーム
def _find_room(s: Store, key: Any) -> Any:
    key = str(key or "").strip()
    if not key:
        raise ValueError("ルームを指定してください")
    row = s.room(key=key)
    if row is None and key.isdigit():
        row = s.room(room_id=int(key))
    return row


def room_detail(state_dir: Path, key: Any) -> dict[str, Any]:
    """1 つのルームの、集めた情報 (累計ランキング・ギフター・イベントの成績・重なるルーム)。"""
    now = time.time()
    with Store(state_dir) as s:
        row = _find_room(s, key)
        if row is None:
            return {"key": str(key), "found": False}
        rid = int(row["room_id"])
        cumulative = [dict(r) for r in s.all(
            """SELECT c.rank, c.user_id, COALESCE(u.name, '') AS name, c.point, c.visits FROM cumulative c
               LEFT JOIN users u USING(user_id) WHERE c.room_id=? ORDER BY COALESCE(c.rank, 999), c.point DESC""", (rid,))]
        gifter_total = s.scalar("SELECT COUNT(*) FROM user_room WHERE room_id=? AND MAX(cumulative, events) > 0", (rid,))
        gifters = [dict(r) for r in s.all(
            """SELECT ur.user_id, COALESCE(u.name, '') AS name, MAX(ur.cumulative, ur.events) AS points, ur.cumulative,
                      ur.cumulative_rank, ur.events, ur.visits,
                      (SELECT COUNT(*) FROM user_room o WHERE o.user_id=ur.user_id AND MAX(o.cumulative, o.events) > 0) AS rooms
               FROM user_room ur LEFT JOIN users u USING(user_id)
               WHERE ur.room_id=? AND MAX(ur.cumulative, ur.events) > 0 ORDER BY points DESC, ur.user_id LIMIT ?""",
            (rid, ROOM_GIFTERS))]
        event_total = s.scalar("SELECT COUNT(*) FROM room_events WHERE room_id=?", (rid,))
        events = []
        for r in s.all("""SELECT e.*, re.point, re.rank, re.ranking_at, re.final,
                                 (SELECT COUNT(*) FROM room_events o WHERE o.event_id=e.event_id) AS rooms,
                                 (SELECT COUNT(*) FROM contributions c WHERE c.event_id=e.event_id AND c.room_id=re.room_id) AS contributors
                          FROM room_events re JOIN events e USING(event_id) WHERE re.room_id=?
                          ORDER BY COALESCE(e.end, e.start, 0) DESC, e.event_id DESC LIMIT ?""", (rid, ROOM_EVENTS)):
            events.append({**_event_row(r, now), "point": r["point"], "rank": r["rank"], "rooms": r["rooms"],
                           "contributors": r["contributors"], "ranking": bool(r["final"])})
        overlap = _overlap(s, rid)
        return {"key": row["key"], "room_id": rid, "found": True, "name": row["name"] or row["key"] or str(rid),
                "followers": row["followers"], "live_at": row["live_at"], "collected_at": row["collected_at"],
                "found_at": row["found_at"], "cumulative": cumulative, "gifters": gifters, "gifter_total": gifter_total,
                "events": events, "event_total": event_total, "overlap": overlap,
                "event_stats": _room_event_stats(s, rid)}


def _room_event_stats(s: Store, rid: int) -> dict[str, Any]:
    r = s.one("""SELECT COUNT(*) AS n, SUM(rank = 1) AS wins, SUM(rank <= 3) AS top3, MIN(rank) AS best,
                        SUM(point) AS points FROM room_events WHERE room_id=? AND rank > 0""", (rid,))
    return {k: (r[k] or 0) if k != "best" else r[k] for k in ("n", "wins", "top3", "best", "points")}


def _overlap(s: Store, rid: int) -> list[dict[str, Any]]:
    """このルームのギフター (多い順に OVERLAP_GIFTERS 人) が、ほかに送っているルーム (共通のギフターの多い順)。"""
    rows = s.all(f"""SELECT o.room_id, COUNT(*) AS shared, SUM(MAX(o.cumulative, o.events)) AS points
                     FROM (SELECT user_id FROM user_room WHERE room_id=? AND MAX(cumulative, events) > 0
                           ORDER BY MAX(cumulative, events) DESC LIMIT {OVERLAP_GIFTERS}) g
                     JOIN user_room o USING(user_id)
                     WHERE o.room_id != ? AND MAX(o.cumulative, o.events) > 0
                     GROUP BY o.room_id ORDER BY shared DESC, points DESC LIMIT ?""", (rid, rid, OVERLAP_ROOMS))
    out = []
    for r in rows:
        info = s.room(room_id=r["room_id"])
        out.append({"room_id": r["room_id"], "key": info["key"] if info is not None else None,
                    "name": (info["name"] if info is not None else None) or str(r["room_id"]),
                    "shared": r["shared"], "points": r["points"]})
    return out


# ------------------------------------------------------------------ イベント
def event_list(state_dir: Path, state: str = "ongoing", limit: int = EVENT_LIST) -> dict[str, Any]:
    """イベントの一覧 (ongoing: 開催中・終わりが近い順 / upcoming: これから / ended: 終わった・新しい順)。"""
    now = time.time()
    where, order = {
        "ongoing": ("start <= ? AND end > ?", "end"),
        "upcoming": ("start > ? AND ? > 0", "start"),
        "ended": ("end <= ? AND ? > 0", "end DESC"),
    }.get(state, (None, None))
    if where is None:
        raise ValueError("state は ongoing・upcoming・ended のどれかです")
    with Store(state_dir) as s:
        total = s.scalar(f"SELECT COUNT(*) FROM events WHERE {where}", (now, now))
        rows = s.all(f"""SELECT e.*, (SELECT COUNT(*) FROM room_events re WHERE re.event_id=e.event_id) AS rooms,
                                (SELECT COUNT(*) FROM room_events re WHERE re.event_id=e.event_id AND re.final=1) AS rankings,
                                (SELECT COUNT(DISTINCT at) FROM event_progress p WHERE p.event_id=e.event_id) AS snapshots,
                                (SELECT r.name FROM room_events re JOIN rooms r USING(room_id)
                                  WHERE re.event_id=e.event_id AND re.rank=1 LIMIT 1) AS leader
                         FROM events e WHERE {where} ORDER BY {order} LIMIT ?""", (now, now, int(limit)))
        return {"state": state, "total": total, "now": now,
                "events": [{**_event_row(r, now), "rooms": r["rooms"], "rankings": r["rankings"], "snapshots": r["snapshots"],
                            "leader": r["leader"], "type": r["type"]} for r in rows]}


def event_detail(state_dir: Path, event_id: Any) -> dict[str, Any]:
    """1 つのイベントの参加ルームの順位・ポイントの推移・貢献者。"""
    ref = str(event_id or "").strip()
    if not ref:
        raise ValueError("イベント番号か URL キーを指定してください")
    now = time.time()
    with Store(state_dir) as s:
        e = s.one("SELECT * FROM events WHERE event_id=?", (int(ref),)) if ref.isdigit() else \
            s.one("SELECT * FROM events WHERE key=? ORDER BY event_id DESC LIMIT 1", (ref,))
        if e is None:
            return {"event_id": ref, "found": False}
        eid = int(e["event_id"])
        rooms = [dict(r) for r in s.all(
            """SELECT re.room_id, r.key, COALESCE(r.name, r.key, re.room_id) AS name, re.point, re.rank, re.final,
                      (SELECT COUNT(*) FROM contributions c WHERE c.event_id=re.event_id AND c.room_id=re.room_id) AS contributors
               FROM room_events re LEFT JOIN rooms r USING(room_id) WHERE re.event_id=?
               ORDER BY re.rank IS NULL, re.rank, re.point DESC LIMIT ?""", (eid, EVENT_ROOMS))]
        # 開催中は順位が入っていないことがあるので、ポイントの多い順の位置を使う (place)
        points = sorted((r["point"] or 0 for r in rooms), reverse=True)
        for r in rooms:
            r["place"] = r["rank"] or (points.index(r["point"] or 0) + 1 if r["point"] else None)
        room_total = s.scalar("SELECT COUNT(*) FROM room_events WHERE event_id=?", (eid,))
        pending = s.scalar(f"""SELECT COUNT(*) FROM room_events re JOIN events e USING(event_id)
                               WHERE re.event_id=? AND {PENDING_RANKING}""", (eid, now - 3600, RANKING_RETRIES))
        left = tuple(LEFT_NAMES)
        contributors = [dict(r) for r in s.all(
            f"""SELECT c.user_id, COALESCE(u.name, '') AS name, SUM(c.point) AS point, COUNT(*) AS rooms, MIN(c.rank) AS best_rank,
                       (SELECT COALESCE(r.name, r.key) FROM contributions c2 LEFT JOIN rooms r USING(room_id)
                         WHERE c2.event_id=c.event_id AND c2.user_id=c.user_id ORDER BY c2.point DESC LIMIT 1) AS top_room
                FROM contributions c LEFT JOIN users u USING(user_id) WHERE c.event_id=?
                  AND COALESCE(u.name, '') NOT IN ({','.join('?' * len(left))})
                GROUP BY c.user_id ORDER BY point DESC LIMIT ?""", (eid, *left, CONTRIBUTORS))]
        contributor_total = s.scalar("SELECT COUNT(DISTINCT user_id) FROM contributions WHERE event_id=?", (eid,))
        return {**_event_row(e, now), "found": True, "type": e["type"], "show_ranking": bool(e["show_ranking"]),
                "rooms": rooms, "room_total": room_total, "rankings": sum(1 for r in rooms if r["final"]),
                "rankings_pending": pending, "contributors": contributors, "contributor_total": contributor_total,
                "progress": _progress(s, eid, rooms)}


def _progress(s: Store, eid: int, rooms: list[dict[str, Any]]) -> dict[str, Any]:
    """ポイントの推移 (今のポイントの多い PROGRESS_ROOMS ルーム)。"""
    ats = [r[0] for r in s.all("SELECT DISTINCT at FROM event_progress WHERE event_id=? ORDER BY at", (eid,))]
    if not ats:
        return {"times": [], "rooms": []}
    last = s.all("""SELECT room_id, point FROM event_progress WHERE event_id=? AND at=(SELECT MAX(at) FROM event_progress WHERE event_id=?)
                    ORDER BY point DESC LIMIT ?""", (eid, eid, PROGRESS_ROOMS))
    names = {r["room_id"]: (r["key"], r["name"]) for r in rooms}
    out = []
    for r in last:
        pts = {p["at"]: (p["point"], p["rank"]) for p in s.all(
            "SELECT at, point, rank FROM event_progress WHERE event_id=? AND room_id=?", (eid, r["room_id"]))}
        key, name = names.get(r["room_id"]) or (None, None)
        if name is None:
            info = s.room(room_id=r["room_id"])
            key, name = (info["key"], info["name"]) if info is not None else (None, str(r["room_id"]))
        out.append({"room_id": r["room_id"], "key": key, "name": name or key or str(r["room_id"]),
                    "points": [pts.get(at, (None, None))[0] for at in ats], "ranks": [pts.get(at, (None, None))[1] for at in ats]})
    return {"times": ats, "rooms": out}
