"""管理画面のルーム・イベントのページと、上の検索欄に出す情報 (research.db を読むだけ)。

SHOWROOM には問い合わせない。分析 (research.analyze) のように全体を数え直さず、
1 つのルーム・イベントの分だけを、索引のある列から読む。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from . import event_analysis, room_analysis
from .research import LEFT_NAMES, _user_names, PENDING_RANKING, RANKING_RETRIES
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
RECORDED_TTL = 60  # 記録したギフトの送り主の名前の一覧を使い回す時間 (秒)
_recorded_names: dict[str, tuple[float, dict[int, dict[str, Any]]]] = {}


def _recorded(state_dir: Path) -> dict[int, dict[str, Any]]:
    """記録したギフトの送り主 {user_id: {name: 最後の名前, names: 使っていた名前, times: 回数}}。

    少額でランキングに出ない人は、ランキングから作る users 表に入らないので、記録からも探す。
    """
    from .listing import _load_file, _record_files

    key = str(Path(state_dir).resolve())
    hit = _recorded_names.get(key)
    if hit and time.time() - hit[0] < RECORDED_TTL:
        return hit[1]
    out: dict[int, dict[str, Any]] = {}
    for path in _record_files(state_dir).values():
        for g in _load_file(path):
            uid, name = g.get("user_id"), g.get("name")
            if uid is None:
                continue
            u = out.setdefault(int(uid), {"name": "", "names": set(), "times": 0, "at": 0})
            u["times"] += 1
            if name:
                u["names"].add(name)
                if g["at"] >= u["at"]:
                    u["name"], u["at"] = name, g["at"]
    _recorded_names[key] = (time.time(), out)
    return out


def _fold(text: Any) -> str:
    """探すためにそろえた形: 全角・半角をそろえ (NFKC)、小文字にし、空白を除き、カタカナをひらがなにする。"""
    import unicodedata

    t = unicodedata.normalize("NFKC", str(text or "")).lower()
    t = "".join(t.split())
    return "".join(chr(ord(c) - 0x60) if "ァ" <= c <= "ヶ" else c for c in t)


def _kana_variants(q: str) -> list[str]:
    """SQL の LIKE で探す形 (空白を除いた形と、ひらがな・カタカナの両方)。"""
    import unicodedata

    t = "".join(unicodedata.normalize("NFKC", q).split())
    hira = "".join(chr(ord(c) - 0x60) if "ァ" <= c <= "ヶ" else c for c in t)
    kata = "".join(chr(ord(c) + 0x60) if "ぁ" <= c <= "ゖ" else c for c in t)
    return list(dict.fromkeys(x.replace("%", "").replace("_", "") for x in (t, hira, kata) if x))


FOLDED_TTL = 600  # ルームの名前をそろえた形を使い回す時間 (秒)
_folded_rooms: dict[str, tuple[float, list[tuple[str, int]]]] = {}


def _rooms_by_folded(s: Store, folded: str, limit: int) -> list[dict[str, Any]]:
    """ルームの名前を、そろえた形 (ひらがな・カタカナがまざった名前も) でくらべて探す。名前の一覧は FOLDED_TTL 使い回す。"""
    key = str(s.state_dir)
    hit = _folded_rooms.get(key)
    if hit is None or time.time() - hit[0] > FOLDED_TTL:
        # メモリを使いすぎないように、そろえた名前と room_id だけを持つ (フォロワーの多い順)
        rows = [(_fold(r[1]), r[0]) for r in s.all("SELECT room_id, name FROM rooms WHERE name IS NOT NULL ORDER BY COALESCE(followers, 0) DESC")]
        hit = _folded_rooms[key] = (time.time(), rows)
    ids = [rid for name, rid in hit[1] if folded in name][:limit]
    return [dict(r) for rid in ids for r in [s.one("SELECT room_id, key, name, followers, live_at, collected_at FROM rooms WHERE room_id=?",
                                                   (rid,))] if r]


# 名前から空白 (半角・全角) を除いた形 (「渡辺 みく」を「渡辺みく」で見つけるため)
_NOSPACE = "REPLACE(REPLACE(LOWER({col}), ' ', ''), '　', '')"


def search(state_dir: Path, q: str, showroom: bool = False) -> dict[str, Any]:
    """上の検索欄: ギフター・ルーム・イベントを探す。

    名前は、空白・全角半角・大文字小文字・ひらがなカタカナのちがいを気にせずに探す。
    - ギフター: ランキングに出た人 (users) の名前・ユーザー ID、前の名前 (user_names)、記録したギフトの送り主の名前
    - ルーム: 見つけたルーム (rooms) の名前・URL キー・room_id と、視聴・記録の設定や記録したギフトにあるルーム。
      showroom なら、見つかったルームが少ない時に SHOWROOM のルーム検索でも探し、見つけたルームは集める対象に足す
    - イベント: 見つけたイベントの名前・URL キー・番号 (名前のまだ分からないイベントは番号で)
    """
    from .listing import room_names

    q = str(q or "").strip()[:100]
    if not q:
        return {"gifters": [], "rooms": [], "events": []}
    variants = _kana_variants(q)
    likes = [f"%{v.lower()}%" for v in variants]
    folded = _fold(q)
    digits = q.isdigit()
    num = int(q) if digits else -1
    now = time.time()

    def name_match(col: str) -> str:
        return "(" + " OR ".join(f"{_NOSPACE.format(col=col)} LIKE ?" for _ in likes) + ")"

    with Store(state_dir) as s:
        points = "(SELECT SUM(MAX(ur.cumulative, ur.events)) FROM user_room ur WHERE ur.user_id=u.user_id)"
        gifters = [{**dict(r), "match": None} for r in s.all(
            f"""SELECT u.user_id, u.name, {points} AS points FROM users u WHERE u.user_id = ? OR {name_match("u.name")}
                ORDER BY u.user_id = ? DESC, points DESC LIMIT ?""", (num, *likes, num, SEARCH_LIMIT))]
        seen = {g["user_id"] for g in gifters}
        # 前の名前で見つかった人 (今の名前と、どの名前で見つかったかを出す)
        for r in s.all(f"""SELECT n.user_id, n.name AS old, u.name, {points} AS points FROM user_names n
                           LEFT JOIN users u USING(user_id) WHERE {name_match("n.name")} ORDER BY points DESC LIMIT ?""",
                       (*likes, SEARCH_LIMIT)):
            if r["user_id"] not in seen:
                seen.add(r["user_id"])
                gifters.append({"user_id": r["user_id"], "name": r["name"] or r["old"], "points": r["points"],
                                "match": {"kind": "former", "name": r["old"]}})
        # 記録したギフトにしか出ていない人 (少額でランキングに出ない人など)
        rec = [(uid, u) for uid, u in _recorded(state_dir).items()
               if uid not in seen and (uid == num or any(folded in _fold(n) for n in u["names"]))]
        rec.sort(key=lambda x: (x[0] != num, -x[1]["times"]))
        for uid, u in rec[:SEARCH_LIMIT]:
            hit = u["name"] if folded in _fold(u["name"]) else next((n for n in u["names"] if folded in _fold(n)), u["name"])
            gifters.append({"user_id": uid, "name": u["name"] or hit, "points": None, "times": u["times"],
                            "match": {"kind": "recorded", "name": hit}})

        rooms = [{**dict(r), "source": "research"} for r in s.all(
            f"""SELECT room_id, key, name, followers, live_at, collected_at FROM rooms
                WHERE room_id = ? OR key = ? OR {name_match("name")} OR {name_match("key")}
                ORDER BY room_id = ? DESC, key = ? DESC, COALESCE(followers, 0) DESC LIMIT ?""",
            (num, q, *likes, *likes, num, q, SEARCH_LIMIT))]
        known = {r["key"] for r in rooms if r["key"]} | {str(r["room_id"]) for r in rooms}
        if len(rooms) < SEARCH_LIMIT and not digits:  # ひらがな・カタカナがまざった名前 (SQL ではそろえられない)
            for r in _rooms_by_folded(s, folded, SEARCH_LIMIT):
                if r["key"] not in known and str(r["room_id"]) not in known:
                    known.add(r["key"])
                    rooms.append({**r, "source": "research"})
        # 視聴・記録の設定や記録したギフトにあるルーム (全ルームの収集でまだ見つかっていないものも)
        for key, info in room_names(state_dir).items():
            name = (info or {}).get("name") or key
            rid = (info or {}).get("room_id")
            if key in known or (rid is not None and str(rid) in known):
                continue
            if folded in _fold(key) or folded in _fold(name) or (digits and rid is not None and int(rid) == num):
                known.add(key)
                rooms.append({"room_id": rid, "key": key, "name": name, "followers": None, "live_at": None, "source": "known"})
                if len(rooms) >= SEARCH_LIMIT * 2:
                    break
        # SHOWROOM のルーム検索 (集めた情報で少ししか見つからない時。見つけたルームは集める対象に足す)
        if showroom and len(rooms) < SEARCH_LIMIT and len(q) >= 2 and not digits:
            from .listing import search_showroom_rooms

            try:
                hits = search_showroom_rooms(q)
            except Exception:  # noqa: BLE001 - SHOWROOM に聞けなくても、集めた情報の分は返す
                hits = []
            new = [h for h in hits if h.get("key") and h["key"] not in known and h.get("room_id")]
            if new:
                s.upsert_rooms([{"room_id": h["room_id"], "key": h["key"], "name": h["name"], "followers": h.get("followers")}
                                for h in new], "search", now)
            for h in new[:SEARCH_LIMIT * 2 - len(rooms)]:
                rooms.append({"room_id": h["room_id"], "key": h["key"], "name": h["name"], "followers": h.get("followers"),
                              "live_at": now if h.get("is_live") else None, "source": "showroom", "is_live": h.get("is_live")})
        # 名前がそのまま合うものを先に (そのあとはフォロワーの多い順のまま)
        rooms.sort(key=lambda r: _fold(r["name"]) != folded)

        events = [{**_event_row(r, now), "named": bool(r["name"])} for r in s.all(
            f"""SELECT * FROM events WHERE event_id = ? OR key = ? OR {name_match("name")} OR {name_match("key")}
                ORDER BY event_id = ? DESC, COALESCE(end, 0) DESC LIMIT ?""",
            (num, q, *likes, *likes, num, SEARCH_LIMIT))]
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


def _room_congestion(s: Store, rid: int, now: float) -> dict[str, Any] | None:
    """時間帯の混み具合 (全ルームと、このルームと同じリーグ)。"""
    c = room_analysis.congestion(s, now)
    if c is None:
        return None
    info = s.one("SELECT league_id FROM room_info WHERE room_id=?", (rid,))
    lg = info["league_id"] if info is not None else None
    return {"days": c["days"], "all": c["all"], "league": c["leagues"].get(str(lg)) if lg is not None else None,
            "league_name": event_analysis.LEAGUES.get(lg) if lg is not None else None}


METRIC_DAYS = 400  # 日ごとの数字を出す日数の上限


def room_daily_metrics(state_dir: Path, key: Any, days: int = METRIC_DAYS) -> dict[str, Any]:
    """ルームの日ごとの数字 (日本時間の日): 配信の時間・回数・盛り上がり度・フォロワー・ギフト・コメント・来場。

    ギフト・コメント・来場は、記録しているルームだけ (記録が無い日は 0 ではなく「無し」)。
    """
    from datetime import datetime

    from . import comment_counts, listing, live_events
    from .status import JST

    now = time.time()
    since = now - days * 86400

    def day_of(t: float) -> str:
        return datetime.fromtimestamp(t, JST).strftime("%Y-%m-%d")

    out: dict[str, dict[str, float]] = {}

    def put(d: str, k: str, v: float, add: bool = True) -> None:
        row = out.setdefault(d, {})
        row[k] = row.get(k, 0) + v if add else v

    with Store(state_dir) as s:
        row = _find_room(s, key)
        if row is None:
            return {"found": False}
        rid, rkey = int(row["room_id"]), row["key"]
        for r in s.all("SELECT day, followers, view_max FROM room_daily WHERE room_id=? AND day >= ? ORDER BY day", (rid, day_of(since))):
            if r["followers"] is not None:
                put(r["day"], "followers", r["followers"], add=False)
            if r["view_max"]:
                put(r["day"], "view_max", r["view_max"], add=False)
        for r in s.all("SELECT started_at, last_seen FROM room_lives WHERE room_id=? AND started_at >= ?", (rid, since)):
            d = day_of(r["started_at"])
            put(d, "lives", 1)
            put(d, "hours", max(0.0, r["last_seen"] - r["started_at"] + room_analysis.LIVE_SEEN_GAP / 2) / 3600)
    recorded = False
    if rkey:
        files = listing._record_files(state_dir)
        if rkey in files:
            recorded = True
            catalog = listing.gift_catalog(state_dir, [rkey])
            gifters: dict[str, set[Any]] = {}
            for g in listing._load_file(files[rkey]):
                if g["at"] < since:
                    continue
                d = day_of(g["at"])
                pt, free = listing._points(catalog, g)
                put(d, "free_points" if free else "paid_points", pt)
                put(d, "gifts", int(g.get("num") or 1))
                if g.get("user_id") is not None and not free:
                    gifters.setdefault(d, set()).add(g["user_id"])
            for d, us in gifters.items():
                put(d, "paid_gifters", len(us), add=False)
        for live in comment_counts.load(state_dir, rkey):
            if live.get("first") and live["first"] >= since:
                d = day_of(live["first"])
                recorded = True
                put(d, "comments", live.get("comments") or 0)
                put(d, "commenters", live.get("users") or 0)
        for e in live_events.load(state_dir, rkey, since):
            if e.get("kind") == "visit":
                recorded = True
                d = day_of(e["at"])
                put(d, "visits", 1)
                if e.get("n") == 1:
                    put(d, "first_visits", 1)
            elif e.get("kind") == "fan_level":
                put(day_of(e["at"]), "level_ups", 1)
    # フォロワーの増減 (前に記録した日から)
    prev = None
    for d in sorted(out):
        f = out[d].get("followers")
        if f is not None:
            if prev is not None:
                out[d]["follower_gain"] = f - prev
            prev = f
    rec_keys = ("paid_points", "free_points", "gifts", "comments", "visits")
    rec_days = [d for d in sorted(out) if any(k in out[d] for k in rec_keys)]
    return {"found": True, "key": rkey, "recorded": recorded, "rec_from": rec_days[0] if rec_days else None,
            "days": [{"day": d, **out[d]} for d in sorted(out)]}


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
        for r in s.all("""SELECT e.*, re.point, re.rank, re.ranking_at, re.final, re.c_count, re.c_sum,
                                 (SELECT COUNT(*) FROM room_events o WHERE o.event_id=e.event_id) AS rooms,
                                 (SELECT COUNT(*) FROM contributions c WHERE c.event_id=e.event_id AND c.room_id=re.room_id) AS contributors
                          FROM room_events re JOIN events e USING(event_id) WHERE re.room_id=?
                          ORDER BY COALESCE(e.end, e.start, 0) DESC, e.event_id DESC LIMIT ?""", (rid, ROOM_EVENTS)):
            events.append({**_event_row(r, now), "point": r["point"], "rank": r["rank"], "rooms": r["rooms"],
                           "contributors": r["contributors"], "ranking": bool(r["final"]),
                           # 100 位より下の人たちの分 (100 人に届いたイベントだけ。ポイント - 上位 100 人の合計)
                           "below": max(0, (r["point"] or 0) - r["c_sum"]) if r["final"] and (r["c_count"] or 0) >= 100
                           and r["c_sum"] is not None and r["end"] and (r["rooms_at"] or 0) >= r["end"] else None})
        overlap = _overlap(s, rid)
        info = s.one("SELECT * FROM room_info WHERE room_id=?", (rid,))
        profile = None
        if info is not None:
            profile = {k: info[k] for k in ("at", "show_rank", "prev_show_rank", "grade", "continuous_days", "prev_score", "style",
                                            "group_type", "official")}
            profile.update({k: event_analysis.LEAGUES.get(info[k]) if info[k] is not None else None
                            for k in ("league_id", "next_league_id", "prev_league_id")})
            profile["ranks"] = [dict(r) for r in s.all("SELECT ym, show_rank, grade FROM room_rank_month WHERE room_id=? ORDER BY ym DESC LIMIT 12",
                                                       (rid,))]
        return {"key": row["key"], "room_id": rid, "found": True, "profile": profile, "name": row["name"] or row["key"] or str(rid),
                "followers": row["followers"], "live_at": row["live_at"], "collected_at": row["collected_at"],
                "found_at": row["found_at"], "cumulative": cumulative, "gifters": gifters, "gifter_total": gifter_total,
                "events": events, "event_total": event_total, "overlap": overlap,
                "event_stats": _room_event_stats(s, rid), "analysis": room_analysis.room_analysis(s, rid, now),
                "balance": room_analysis.balance(state_dir, row["key"], now) if row["key"] else None,
                "lure": room_analysis.lure_throws(state_dir, row["key"], now, store=s, room_id=rid) if row["key"] else None,
                "stage": room_analysis.stage_regulars(state_dir, row["key"], now, store=s) if row["key"] else None,
                "live_habits": room_analysis.live_habits(s, rid, now),
                "followers_daily": [[r[0], r[1]] for r in s.all(
                    "SELECT day, followers FROM room_daily WHERE room_id=? AND followers IS NOT NULL ORDER BY day", (rid,))],
                "congestion": _room_congestion(s, rid, now), "effort": room_analysis.event_effort(s, rid, now)}


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


def room_profiles(state_dir: Path, rtype: str | None = None, style: str | None = None, order: str = "momentum",
                  depth: str | None = None) -> dict[str, Any]:
    """ルームの一覧の、タイプ・勢い・戦術・層の厚さ (1 日 1 回作り直した分)。"""
    with Store(state_dir) as s:
        return room_analysis.profile_list(s, rtype or None, style or None, order or "momentum", depth=depth or None)


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
                "api_lag": event_analysis.api_lag(state_dir, now) if state == "ongoing" else None,
                "events": [{**_event_row(r, now), "rooms": r["rooms"], "rankings": r["rankings"], "snapshots": r["snapshots"],
                            "leader": r["leader"], "type": r["type"]} for r in rows]}


def _room_ids(s: Store, keys: list[str] | None) -> list[int]:
    out = []
    for k in keys or []:
        r = s.one("SELECT room_id FROM rooms WHERE key=?", (k,))
        if r is not None:
            out.append(int(r[0]))
    return out


def _room_labels(s: Store, rows: list[dict[str, Any]]) -> None:
    for r in rows:
        x = s.one("SELECT key, name FROM rooms WHERE room_id=?", (r["room_id"],))
        r["key"] = (x["key"] if x is not None else None) or str(r["room_id"])
        r["name"] = (x["name"] if x is not None else None) or r["key"]


def event_choice(state_dir: Path, support: list[str]) -> dict[str, Any]:
    """応援ルームに合うイベント: 開催中・これからのイベントで、応援ルームが出たら何位くらいか。"""
    with Store(state_dir) as s:
        ids = _room_ids(s, support)
        out = dict(event_analysis.event_choice(s, ids))
        out["events"] = [dict(e, focus=[dict(f) for f in e["focus"]]) for e in out["events"]]
        for e in out["events"]:
            _room_labels(s, e["focus"])
        out["support"] = len(ids)
        return out


def event_detail(state_dir: Path, event_id: Any, support: list[str] | None = None) -> dict[str, Any]:
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
        styles = {r["room_id"]: r for r in s.all("SELECT room_id, style, early, final, last_day_share FROM room_event_style WHERE event_id=?",
                                                  (eid,))}
        for r in rooms:
            r["place"] = r["rank"] or (points.index(r["point"] or 0) + 1 if r["point"] else None)
            st = styles.get(r["room_id"])
            r["style"] = st["style"] if st is not None else None
        room_total = s.scalar("SELECT COUNT(*) FROM room_events WHERE event_id=?", (eid,))
        pending = s.scalar(f"""SELECT COUNT(*) FROM room_events re JOIN events e USING(event_id)
                               WHERE re.event_id=? AND {PENDING_RANKING}""", (eid, now - 3600, RANKING_RETRIES))
        # 退会したユーザーも出す (イベントの実際の顔ぶれなので)。前の名前が分かれば、それも出す
        contributors = [dict(r) for r in s.all(
            """SELECT c.user_id, COALESCE(u.name, '') AS name, SUM(c.point) AS point, COUNT(*) AS rooms, MIN(c.rank) AS best_rank,
                      MAX(c.guessed IS NOT NULL) AS guessed,
                      (SELECT COALESCE(r.name, r.key) FROM contributions c2 LEFT JOIN rooms r USING(room_id)
                        WHERE c2.event_id=c.event_id AND c2.user_id=c.user_id ORDER BY c2.point DESC LIMIT 1) AS top_room
               FROM contributions c LEFT JOIN users u USING(user_id) WHERE c.event_id=?
               GROUP BY c.user_id ORDER BY point DESC LIMIT ?""", (eid, CONTRIBUTORS))]
        shown = _user_names(s, None, [c["user_id"] for c in contributors if c["name"] in LEFT_NAMES])
        for c in contributors:
            c["left"] = c["name"] in LEFT_NAMES
            c["name"] = shown.get(c["user_id"], c["name"])
        contributor_total = s.scalar("SELECT COUNT(DISTINCT user_id) FROM contributions WHERE event_id=?", (eid,))
        info = s.one("SELECT * FROM event_info WHERE event_id=?", (eid,))
        data = json.loads(info["data"]) if info is not None and info["data"] else {}
        pg = s.one("SELECT at, status, text FROM event_pages WHERE event_id=?", (eid,))
        series = [dict(r) for r in s.all("""SELECT e.event_id, e.name, e.end FROM event_info i JOIN events e USING(event_id)
                                            WHERE i.parent_id=? AND i.parent_id > 0 AND i.event_id != ? ORDER BY e.end DESC LIMIT 20""",
                                         (info["parent_id"], eid))] if info is not None and info["parent_id"] else []
        details = {"description": data.get("event_description"), "reward_rank": data.get("reward_rank"),
                   "block": data.get("event_block_label"), "leagues": data.get("league_ids"), "parent_id": data.get("parent_event_id") or None,
                   "required_level": data.get("required_level"), "required_level_max": data.get("required_level_max"),
                   "box": data.get("is_box_event"), "official": data.get("is_official"), "type_name": data.get("type_name"),
                   "series": series, "page": dict(pg) if pg is not None else None}
        return {**_event_row(e, now), "found": True, "type": e["type"], "show_ranking": bool(e["show_ranking"]),
                "rooms": rooms, "room_total": room_total, "rankings": sum(1 for r in rooms if r["final"]),
                "rankings_pending": pending, "contributors": contributors, "contributor_total": contributor_total,
                "progress": _progress(s, eid, rooms), "styled": bool(e["style_at"]) and bool(styles), "details": details,
                "needed": event_analysis.needed_points(s, eid, now), "strength": _strength(s, eid, now, support)}


def _strength(s: Store, eid: int, now: float, support: list[str] | None) -> dict[str, Any]:
    st = event_analysis.event_strength(s, eid, now, _room_ids(s, support))
    st["rooms"] = st["rooms"][:EVENT_ROOMS]
    _room_labels(s, st["rooms"])
    _room_labels(s, st["focus"])
    return st


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
