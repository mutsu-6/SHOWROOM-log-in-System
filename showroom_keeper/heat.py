"""ヒートマップ (株のヒートマップのように、四角の大きさと色で、どこが伸びてどこが落ちたかを一度に見る)。

SHOWROOM には問い合わせない (集めた情報を読むだけ)。
- ルーム: 毎日 3:00 / 15:00 (日本時間) で区切った 12 時間 (月曜 3:00 を週の始まりにする) ごとに、イベントで増えたポイント。
  イベントの終わりの日でまとめると動きが少ないので、1 時間ごとの推移 (event_progress) の差から、区切りが終わったあと
  1 回だけ数えて room_point_12h に残す (全ルームの収集の小さな仕事として、1 回に 1 区切りずつ)。
  四角の大きさはその期間のポイント、色は前の期間 (既定は先週の同じ区切り) からの増え減り。配信ジャンル (アイドル・モデル・芸人など。room_lives.genre) ごとにまとめる (タイプにも切り替えられる)。
- ギフター: 月ごとのイベントのポイント (user_month) を、最推しのルーム (gifter_status.top_room) ごとにまとめる。色は前の月からの増え減り。
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .research_store import Store

JST = timezone(timedelta(hours=9))
SLOT = 12 * 3600  # 区切りの長さ (秒)
ANCHOR = 324000  # 区切りの起点: 1970-01-05 (月) 3:00 日本時間。1 週間 = 14 区切りで、月曜 3:00〜15:00 が 0 番目
WEEK_SLOTS = 7 * 86400 // SLOT
READY_AFTER = 2 * 3600  # 区切りが終わって、これだけたってから数える (終わったイベントの最後の値が推移に入るのを待つ。秒)
START_NEAR = 2 * 3600  # 区切りの始まりの値が無い時、始まってこれだけのうちのイベントは 0 から数える (推移は始まって 1 時間以内に読む)
BACKFILL_DAYS = 28  # 初めて作る時に、さかのぼって作る日数 (先週との比べに 2 週間要る。それより前は推移が間引かれている)
ROOM_LIMIT = 300  # ヒートマップに出すルームの数 (今の期間の上位と、前の期間の上位)
GIFTER_LIMIT = 300  # ヒートマップに出すギフターの数 (今の月の上位と、前の月の上位)
GROUP_LIMIT = 40  # まとめる箱の数の上限 (残りは「そのほか」)


def slot_of(t: float) -> int:
    return int((t - ANCHOR) // SLOT)


def slot_start(i: int) -> float:
    return float(ANCHOR + int(i) * SLOT)


def _gains(s: Store, e: Any, t0: float, t1: float) -> dict[int, int] | None:
    """イベント e で、t0〜t1 にルームごとに増えたポイント (推移が無くて分からない時は None)。

    区切りの終わり (イベントが先に終わったら終わった時刻) までの最後の値から、始まりまでの最後の値を引く。
    イベントの始まり (イベントの詳細の started_at) が区切りの中なら、始まりは 0 から数える。減った分 (不正の取り消しなど) は数えない。
    """
    eid = e["event_id"]
    end_at = min(t1, e["end"]) if e["end"] else t1
    at1 = s.scalar("SELECT MAX(at) FROM event_progress WHERE event_id=? AND at<=?", (eid, end_at), None)
    if at1 is None or at1 <= t0:
        return None
    base: dict[int, int] = {}
    if not (e["start"] and e["start"] >= t0):
        at0 = s.scalar("SELECT MAX(at) FROM event_progress WHERE event_id=? AND at<=?", (eid, t0), None)
        if at0 is None:
            if not (e["start"] and e["start"] >= t0 - START_NEAR):
                return None  # 途中から集め始めたイベント: 区切りの始まりの値が分からない
        else:
            base = {r[0]: r[1] or 0 for r in s.all("SELECT room_id, point FROM event_progress WHERE event_id=? AND at=?", (eid, at0))}
    return {r[0]: max(0, (r[1] or 0) - base.get(r[0], 0))
            for r in s.all("SELECT room_id, point FROM event_progress WHERE event_id=? AND at=?", (eid, at1))}


def build_slot(s: Store, i: int) -> int:
    """区切り i の、ルームごとに増えたポイントを room_point_12h に入れ直す (戻り値はルームの数)。"""
    t0, t1 = slot_start(i), slot_start(i + 1)
    total: dict[int, list[int]] = {}
    for e in s.all("SELECT event_id, start, end FROM events WHERE progress_at IS NOT NULL AND start < ? AND end > ?", (t1, t0)):
        for rid, pt in (_gains(s, e, t0, t1) or {}).items():
            x = total.setdefault(rid, [0, 0])
            x[0] += pt
            x[1] += 1
    rows = [(i, rid, pt, n) for rid, (pt, n) in total.items() if pt > 0]
    with s.tx() as c:
        c.execute("DELETE FROM room_point_12h WHERE slot=?", (i,))
        c.executemany("INSERT INTO room_point_12h(slot, room_id, point, events) VALUES (?, ?, ?, ?)", rows)
    return len(rows)


def step(s: Store, now: float | None = None) -> bool:
    """終わって READY_AFTER たった区切りのうち、まだ作っていないものを 1 つ作る。することが無ければ False。"""
    now = time.time() if now is None else now
    last = slot_of(now - READY_AFTER) - 1
    nxt = s.meta("heat_next")
    nxt = slot_of(now - BACKFILL_DAYS * 86400) if nxt is None else max(int(nxt), last - BACKFILL_DAYS * 2)
    if nxt > last:
        return False
    build_slot(s, nxt)
    s.set_meta("heat_next", nxt + 1)
    return True


def _grouped(items: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    """items を key の値ごとの箱にまとめる (大きい順。GROUP_LIMIT を超えた箱は「そのほか」へ)。"""
    groups: dict[Any, dict[str, Any]] = {}
    for x in items:
        g = groups.setdefault(x[key], {"name": x[key], "items": []})
        g["items"].append(x)
    out = sorted(groups.values(), key=lambda g: -sum(x["size"] for x in g["items"]))
    if len(out) > GROUP_LIMIT:
        rest = {"name": "そのほか", "items": [x for g in out[GROUP_LIMIT - 1:] for x in g["items"]]}
        out = out[:GROUP_LIMIT - 1] + [rest]
    for g in out:
        g["items"].sort(key=lambda x: -x["size"])
        g["size"] = sum(x["size"] for x in g["items"])
        g["now"] = sum(x["now"] for x in g["items"])
        g["before"] = sum(x["before"] for x in g["items"])
    return out


def _item(now_pt: int, before_pt: int) -> dict[str, Any]:
    """大きさ (今か前の多い方。送らなくなった人・ルームも見えるように) と、増え減りの割合 (前が 0 なら None = 新しく)。"""
    return {"now": now_pt, "before": before_pt, "size": max(now_pt, before_pt),
            "change": (now_pt - before_pt) / before_pt if before_pt else None}


def rooms(state_dir: Path, slot: int | None = None, span: str = "12h", compare: str = "week", group: str = "genre") -> dict[str, Any]:
    """ルームのヒートマップ。slot は見る区切り (期間の最後の区切り。既定は作り終えた最新)、span は 12h か week、
    compare は week (先週の同じ期間と比べる) か prev (すぐ前の期間と比べる。week の時は同じ)、
    group は箱の分け方: genre (配信ジャンル。株のヒートマップの業種のように) か type (ルームのタイプ)。"""
    span = "week" if span == "week" else "12h"
    group = "type" if group == "type" else "genre"
    with Store(state_dir) as s:
        latest = s.scalar("SELECT MAX(slot) FROM room_point_12h", (), None)
        if latest is None:
            return {"slot": None, "items": [], "groups": [], "slots": []}
        slot = latest if slot is None else min(int(slot), int(latest))
        n = WEEK_SLOTS if span == "week" else 1
        back = WEEK_SLOTS if (span == "week" or compare != "prev") else 1
        cur = (slot - n + 1, slot)
        base = (cur[0] - back, cur[1] - back)

        def top(lo: int, hi: int) -> dict[int, int]:
            return {r[0]: r[1] for r in s.all("""SELECT room_id, SUM(point) FROM room_point_12h WHERE slot BETWEEN ? AND ?
                                                 GROUP BY room_id ORDER BY 2 DESC LIMIT ?""", (lo, hi, ROOM_LIMIT))}

        now_top, before_top = top(*cur), top(*base)
        ids = list(dict.fromkeys([*now_top, *before_top]))

        def points(lo: int, hi: int, known: dict[int, int]) -> dict[int, int]:
            out = dict(known)
            for rid in ids:
                if rid not in out:
                    out[rid] = s.scalar("SELECT SUM(point) FROM room_point_12h WHERE slot BETWEEN ? AND ? AND room_id=?",
                                        (lo, hi, rid), 0) or 0
            return out

        now_pt, before_pt = points(*cur, now_top), points(*base, before_top)
        items = []
        for rid in ids:
            r = s.one("""SELECT r.key, r.name, p.type FROM rooms r LEFT JOIN room_profile p USING(room_id)
                         WHERE r.room_id=?""", (rid,))
            if group == "type":
                name = (r["type"] if r else None) or "タイプ未判定"
            else:  # 最後の配信のジャンル (配信中のルームの一覧で、配信ごとに残している)
                name = s.scalar("""SELECT genre FROM room_lives WHERE room_id=? AND genre IS NOT NULL
                                   ORDER BY started_at DESC LIMIT 1""", (rid,), None) or "ジャンル未判定"
            items.append({"room_id": rid, "key": r["key"] if r else None, "name": (r["name"] if r else None) or str(rid),
                          "group": name, **_item(now_pt[rid], before_pt[rid])})
        items = [x for x in items if x["size"] > 0]
        slots = [r[0] for r in s.all("SELECT DISTINCT slot FROM room_point_12h WHERE slot > ? ORDER BY slot DESC",
                                     (latest - 4 * WEEK_SLOTS,))]
        return {"slot": slot, "span": span, "compare": "week" if back == WEEK_SLOTS else "prev", "group": group,
                "from": slot_start(cur[0]), "to": slot_start(cur[1] + 1),
                "base_from": slot_start(base[0]), "base_to": slot_start(base[1] + 1),
                "groups": _grouped(items, "group"),
                "slots": [{"slot": i, "start": slot_start(i)} for i in slots]}


def _month_before(month: str) -> str:
    y, m = map(int, month.split("-"))
    return f"{y - 1}-12" if m == 1 else f"{y}-{m - 1:02d}"


def gifters(state_dir: Path, month: str | None = None) -> dict[str, Any]:
    """ギフターのヒートマップ。month (YYYY-MM。既定は前の月 = そろった最新の月) の、イベントの貢献ポイントを、
    最推しのルームごとにまとめる。色は前の月からの増え減り。"""
    with Store(state_dir) as s:
        this_month = datetime.now(JST).strftime("%Y-%m")
        months = [r[0] for r in s.all("SELECT DISTINCT month FROM user_month WHERE month <= ? ORDER BY month DESC LIMIT 24",
                                      (this_month,))]
        if not months:
            return {"month": None, "groups": [], "months": []}
        if month not in months:
            month = next((m for m in months if m < this_month), months[0])
        prev = _month_before(month)

        def top(m: str) -> dict[int, int]:
            return {r[0]: r[1] for r in s.all("""SELECT user_id, points FROM user_month INDEXED BY user_month_points
                                                 WHERE month=? ORDER BY points DESC LIMIT ?""", (m, GIFTER_LIMIT))}

        now_top, before_top = top(month), top(prev)
        ids = list(dict.fromkeys([*now_top, *before_top]))

        def pt(uid: int, m: str, known: dict[int, int]) -> int:
            if uid in known:
                return known[uid]
            return s.scalar("SELECT points FROM user_month WHERE user_id=? AND month=?", (uid, m), 0) or 0

        status, names = {}, {}
        for k in range(0, len(ids), 500):
            part = ids[k:k + 500]
            marks = ",".join("?" * len(part))
            status.update({r[0]: r for r in s.all(f"SELECT user_id, top_room, state FROM gifter_status WHERE user_id IN ({marks})", part)})
            names.update({r[0]: r[1] for r in s.all(f"SELECT user_id, name FROM users WHERE user_id IN ({marks})", part)})
        rooms_of = {}
        for rid in {r["top_room"] for r in status.values() if r["top_room"]}:
            r = s.one("SELECT key, name FROM rooms WHERE room_id=?", (rid,))
            rooms_of[rid] = ((r["key"] if r else None), (r["name"] if r else None) or str(rid))
        items = []
        for uid in ids:
            st = status.get(uid)
            room = rooms_of.get(st["top_room"]) if st is not None and st["top_room"] else None
            items.append({"user_id": uid, "name": names.get(uid) or "",
                          "state": st["state"] if st is not None else None,
                          "group": room[1] if room else "最推しのルームが分からない", "room_key": room[0] if room else None,
                          **_item(pt(uid, month, now_top), pt(uid, prev, before_top))})
        items = [x for x in items if x["size"] > 0]
        groups = _grouped(items, "group")
        keys = {x["group"]: x["room_key"] for x in items}
        for g in groups:
            g["room_key"] = keys.get(g["name"])
        return {"month": month, "prev": prev, "partial": month == this_month, "groups": groups, "months": months}
