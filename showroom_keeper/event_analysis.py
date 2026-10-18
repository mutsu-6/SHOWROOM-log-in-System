"""イベントの必要ポイントの見積もり: 同じ続きもののイベントで、何位に何 pt 要ったか。

- 同じ続きもの = 一覧の「親のイベント」(parent_event_id) が同じもの、または名前から数字 (Vol.54・第12弾・2026 など) を除くと
  同じになるもの。名前が同じでも中身が違うこともあるので、参加ルームの数とあわせて見る
- 開催中なら、今の各順位のポイントと、前の回で同じ時点 (期間の何割が過ぎたか) から最後までに何倍になったかで、最後の見込みを出す
"""

from __future__ import annotations

import re
import time
import unicodedata
from statistics import median
from typing import Any

from .research_store import Store

RANKS = (1, 2, 3, 5, 10, 20, 30)  # 見積もりを出す順位 (特典の順位も足す)
SIMILAR = 12  # くらべる前の回の数 (新しい順)
SERIES_TTL = 6 * 3600  # 名前からまとめた続きものを使い回す時間 (秒)
PROGRESS_NEAR = 0.05  # 前の回の推移で「同じ時点」とみる幅 (期間の割合)

_series: dict[str, tuple[float, dict[str, list[int]], dict[int, str]]] = {}
_NUM = re.compile(r"(vol|ver|season|no|part|第|#)?\s*[0-9]+\s*(回|弾|期生|期|年|月|日|週|周年|th|st|nd|rd)?", re.I)
_PUNCT = re.compile(r"[\s\W_]+")


def series_key(name: str | None) -> str | None:
    """名前から、回ごとに変わる数字 (Vol.54・第12弾・2026年・10月第2週など) を除いたもの。短すぎれば None。"""
    if not name:
        return None
    s = unicodedata.normalize("NFKC", name).lower()
    s = _NUM.sub("", s)
    s = _PUNCT.sub("", s)
    return s if len(s) >= 4 else None


def _name_series(s: Store) -> tuple[dict[str, list[int]], dict[int, str]]:
    key = str(s.state_dir)
    hit = _series.get(key)
    if hit and time.time() - hit[0] < SERIES_TTL:
        return hit[1], hit[2]
    groups: dict[str, list[int]] = {}
    keys: dict[int, str] = {}
    for r in s.all("SELECT event_id, name FROM events WHERE name IS NOT NULL"):
        k = series_key(r["name"])
        if k:
            groups.setdefault(k, []).append(r["event_id"])
            keys[r["event_id"]] = k
    _series[key] = (time.time(), groups, keys)
    return groups, keys


def similar_events(s: Store, eid: int) -> list[Any]:
    """同じ続きものの、終わって結果を読んだ前の回 (新しい順に SIMILAR まで)。"""
    ids: set[int] = set()
    info = s.one("SELECT parent_id FROM event_info WHERE event_id=?", (eid,))
    if info is not None and info["parent_id"]:
        ids |= {r[0] for r in s.all("SELECT event_id FROM event_info WHERE parent_id=? AND parent_id > 0", (info["parent_id"],))}
    groups, keys = _name_series(s)
    if eid in keys:
        ids |= set(groups.get(keys[eid], []))
    ids.discard(eid)
    if not ids:
        return []
    marks = ",".join("?" * len(ids))
    return s.all(f"""SELECT event_id, name, start, end, room_count FROM events
                     WHERE event_id IN ({marks}) AND end IS NOT NULL AND rooms_at >= end AND show_ranking=1
                     ORDER BY end DESC LIMIT ?""", (*ids, SIMILAR))


def _final_points(s: Store, eid: int) -> list[tuple[int, int]]:
    """(順位, ポイント) を順位の順に。順位の無いものはポイントの多い順で数える。"""
    rows = s.all("SELECT room_id, rank, point FROM room_events WHERE event_id=? AND point > 0", (eid,))
    ranked = sorted(rows, key=lambda r: (r["rank"] is None, r["rank"] or 0, -(r["point"] or 0)))
    return [(i + 1, r["point"], r["room_id"]) for i, r in enumerate(ranked)]  # type: ignore[misc]


def _growth(s: Store, ev: Any, frac: float, room_id: int, final: int) -> float | None:
    """前の回で、期間の frac が過ぎた時のポイントから、最後までに何倍になったか (その順位で終えたルーム)。"""
    if not ev["start"] or not ev["end"] or not final:
        return None
    t = ev["start"] + frac * (ev["end"] - ev["start"])
    row = s.one("SELECT at, point FROM event_progress WHERE event_id=? AND room_id=? AND at<=? ORDER BY at DESC LIMIT 1",
                (ev["event_id"], room_id, t))
    if row is None or not row["point"] or t - row["at"] > PROGRESS_NEAR * (ev["end"] - ev["start"]):
        return None
    return final / row["point"]


def needed_points(s: Store, eid: int, now: float | None = None) -> dict[str, Any]:
    """必要ポイントの見積もり。開催中なら最後の見込みも出す。"""
    now = time.time() if now is None else now
    e = s.one("SELECT event_id, start, end, room_count FROM events WHERE event_id=?", (eid,))
    info = s.one("SELECT reward_rank FROM event_info WHERE event_id=?", (eid,))
    reward = info["reward_rank"] if info is not None and info["reward_rank"] else None
    ranks = sorted(set(RANKS) | ({reward} if reward else set()))
    sims = similar_events(s, eid)
    past = []
    for ev in sims:
        pts = _final_points(s, ev["event_id"])
        past.append({"event_id": ev["event_id"], "name": ev["name"], "end": ev["end"], "rooms": ev["room_count"] or len(pts),
                     "points": {k: p for k, p, _ in pts if k in ranks}, "_rows": pts})
    summary = []
    for k in ranks:
        vals = [p["points"][k] for p in past if k in p["points"]]
        if vals:
            summary.append({"rank": k, "events": len(vals), "min": min(vals), "median": median(vals), "max": max(vals)})
    out: dict[str, Any] = {"reward_rank": reward, "similar": len(past), "ranks": summary,
                           "past": [{k: v for k, v in p.items() if k != "_rows"} for p in past]}
    if e is not None and e["start"] and e["end"] and e["start"] < now < e["end"]:
        frac = (now - e["start"]) / (e["end"] - e["start"])
        current = {k: p for k, p, _ in _final_points(s, eid) if k in ranks}
        rows = []
        for k in ranks:
            growth = [g for p, ev in zip(past, sims) for kk, final, rid in p["_rows"] if kk == k
                      for g in [_growth(s, ev, frac, rid, final)] if g]
            g = median(growth) if growth else None
            rows.append({"rank": k, "now": current.get(k), "growth": g, "events": len(growth),
                         "projected": current[k] * g if g and current.get(k) else None})
        out["current"] = {"elapsed": frac, "ranks": rows}
    return out


# ------------------------------------------------------------------ 参加ルームの強さ・出るイベントの選び方
STRENGTH_EVENTS = 10  # ルームの強さを見る、終わったイベントの数 (新しい順)
CHOICE_TTL = 600  # 出るイベントの選び方を使い回す時間 (秒)
CHOICE_MIN_ROOMS = 3  # 参加ルームがこれより少ないイベントは選び方に出さない

STRENGTH_TTL = 6 * 3600  # ルームの強さを使い回す時間 (秒。終わったイベントの結果が入るのは 1 日 1 回ほど)
_choice: dict[tuple[str, tuple[int, ...]], tuple[float, dict[str, Any]]] = {}
_strengths: dict[str, tuple[float, dict[int, Any]]] = {}


def room_strength(s: Store, rid: int, now: float, cache: dict[int, Any] | None = None) -> dict[str, Any] | None:
    """ルームの強さ = 最近の終わったイベントで、1 日あたりに取ったポイントの真ん中の値 (期間の長さのちがいをならすため)。"""
    if cache is not None and rid in cache:
        return cache[rid]
    rows = s.all("""SELECT re.point, e.start, e.end FROM room_events re JOIN events e USING(event_id)
                    WHERE re.room_id=? AND re.point > 0 AND e.end < ? AND e.start IS NOT NULL AND e.rooms_at >= e.end
                    ORDER BY e.end DESC LIMIT ?""", (int(rid), now, STRENGTH_EVENTS))
    per_day = [r["point"] / max(1.0, (r["end"] - r["start"]) / 86400) for r in rows]
    out = {"per_day": median(per_day), "events": len(per_day), "best": max(per_day)} if per_day else None
    if cache is not None:
        cache[rid] = out
    return out


# リーグの番号 (ルームのプロフィールの league_id と、イベントの「出られるリーグ」league_ids は同じ番号。2026-10-18 に確かめた)
LEAGUES = {9: "SS", 10: "S", 20: "A", 30: "B", 40: "C", 0: "ランク外"}


def room_league(s: Store, rid: int, start: float | None, now: float) -> int | None:
    """そのイベントの時のルームのリーグ。次の月から始まるイベントは、次の月のリーグ (月末のランクで決まる)。"""
    from datetime import datetime

    from .status import JST

    r = s.one("SELECT league_id, next_league_id FROM room_info WHERE room_id=?", (int(rid),))
    if r is None:
        return None
    later = start is not None and datetime.fromtimestamp(start, JST).strftime("%Y%m") > datetime.fromtimestamp(now, JST).strftime("%Y%m")
    return r["next_league_id"] if later and r["next_league_id"] is not None else r["league_id"]


def event_leagues(s: Store, eid: int) -> list[int] | None:
    import json

    r = s.one("SELECT leagues FROM event_info WHERE event_id=?", (int(eid),))
    return json.loads(r[0]) if r is not None and r[0] else None


def _place(value: float, others: list[float]) -> int:
    return 1 + sum(1 for v in others if v > value)


def event_strength(s: Store, eid: int, now: float | None = None, focus: list[int] | None = None,
                   cache: dict[int, Any] | None = None) -> dict[str, Any]:
    """イベントの参加ルームの強さと、強さの順に並べた時の見込みの順位。focus のルーム (応援ルーム) は、
    参加していなくても、出た時に何位くらいかを出す。"""
    now = time.time() if now is None else now
    cache = {} if cache is None else cache
    rooms = [r[0] for r in s.all("SELECT room_id FROM room_events WHERE event_id=?", (int(eid),))]
    known = {rid: st for rid in rooms for st in [room_strength(s, rid, now, cache)] if st}
    vals = {rid: st["per_day"] for rid, st in known.items()}
    order = sorted(vals, key=lambda r: -vals[r])
    out_rooms = [{"room_id": rid, **known[rid], "place": i + 1} for i, rid in enumerate(order)]
    ev = s.one("SELECT start FROM events WHERE event_id=?", (int(eid),))
    start = ev["start"] if ev is not None else None
    leagues = event_leagues(s, eid)
    for r in out_rooms:
        lg = room_league(s, r["room_id"], start, now)
        r["league"] = LEAGUES.get(lg) if lg is not None else None
    focus_out = []
    for rid in focus or []:
        st = room_strength(s, rid, now, cache)
        lg = room_league(s, rid, start, now)
        # 出られるか: イベントの「出られるリーグ」に、ルームのリーグが入っているか (どちらかが分からなければ None)
        can = None if lg is None or not leagues else lg in leagues
        base = {"room_id": rid, "joined": rid in rooms, "league": LEAGUES.get(lg) if lg is not None else None, "can_enter": can}
        if st:
            others = [v for r, v in vals.items() if r != rid]
            focus_out.append({**base, **st, "place": _place(st["per_day"], others), "of": len(others) + 1})
        else:
            focus_out.append({**base, "per_day": None, "place": None, "of": len(vals) + 1})
    info = s.one("SELECT reward_rank FROM event_info WHERE event_id=?", (int(eid),))
    return {"rooms": out_rooms, "unknown": len(rooms) - len(known), "total": len(rooms), "focus": focus_out,
            "reward_rank": info["reward_rank"] if info is not None else None,
            "leagues": [LEAGUES.get(x, str(x)) for x in leagues] if leagues else None}


def event_choice(s: Store, focus: list[int], now: float | None = None) -> dict[str, Any]:
    """開催中・これからのイベントごとに、応援ルームが出たら何位くらいか (参加ルームの強さから)。"""
    now = time.time() if now is None else now
    key = (str(s.state_dir), tuple(sorted(focus)))
    hit = _choice.get(key)
    if hit and now - hit[0] < CHOICE_TTL:
        return hit[1]
    # ルームの強さは、イベントをまたいで使い回す (1 ルームはいくつものイベントに出るので)。STRENGTH_TTL ごとに作り直す
    held = _strengths.get(str(s.state_dir))
    if held is None or now - held[0] > STRENGTH_TTL:
        held = _strengths[str(s.state_dir)] = (now, {})
    cache = held[1]
    events = s.all("""SELECT e.event_id, e.name, e.start, e.end, COUNT(re.room_id) AS rooms FROM events e
                      JOIN room_events re USING(event_id) WHERE e.end > ? GROUP BY e.event_id HAVING rooms >= ?
                      ORDER BY e.start""", (now, CHOICE_MIN_ROOMS))
    out = []
    for e in events:
        st = event_strength(s, e["event_id"], now, focus, cache)
        out.append({"event_id": e["event_id"], "name": e["name"], "start": e["start"], "end": e["end"], "rooms": st["total"],
                    "leagues": st["leagues"],
                    "unknown": st["unknown"], "reward_rank": st["reward_rank"], "focus": st["focus"],
                    "top": st["rooms"][0]["per_day"] if st["rooms"] else None})
    result = {"now": now, "events": out}
    _choice[key] = (now, result)
    return result


# ------------------------------------------------------------------ イベントの API の遅れ
LAG_DAYS = 7  # 遅れを測った記録を見る日数
LAG_RATE_WINDOW = 600  # 伸びの速さを見る、前後のリアルタイム通信のポイントの幅 (秒)
LAG_TTL = 3600
_lag: dict[str, tuple[float, dict[str, Any]]] = {}


def api_lag(state_dir: Any, now: float | None = None) -> dict[str, Any] | None:
    """イベントの API (event_and_support) が、リアルタイム通信のイベントのポイント (t=5) よりどれだけ遅れているか。

    記録しているルームで、t=5 を受けた直後に API を読んだ記録 (live_events の kind=lag) から。
    遅れの秒数 = (t=5 のポイント - API のポイント) ÷ その前後の t=5 から出した 1 秒あたりの伸び。
    """
    from pathlib import Path

    from . import live_events

    now = time.time() if now is None else now
    hit = _lag.get(str(state_dir))
    if hit and now - hit[0] < LAG_TTL:
        return hit[1]
    folder = live_events.event_dir(Path(state_dir))
    pts, secs, behind, n = [], [], 0, 0
    for path in folder.glob("*.jsonl") if folder.is_dir() else []:
        evs = live_events.load(Path(state_dir), path.stem, now - LAG_DAYS * 86400)
        p5: dict[Any, list[tuple[float, float]]] = {}
        for e in evs:
            if e.get("kind") == "p5" and e.get("p") is not None:
                p5.setdefault(e.get("live_id"), []).append((e["at"], float(e["p"])))
        for e in evs:
            if e.get("kind") != "lag" or e.get("ws") is None or e.get("api") is None:
                continue
            n += 1
            diff = float(e["ws"]) - float(e["api"])
            behind += diff > 0
            pts.append(diff)
            t = e.get("ws_at") or e["at"]
            near = sorted(x for x in p5.get(e.get("live_id"), []) if abs(x[0] - t) <= LAG_RATE_WINDOW)
            if diff > 0 and len(near) >= 2 and near[-1][0] > near[0][0] and near[-1][1] > near[0][1]:
                secs.append(diff / ((near[-1][1] - near[0][1]) / (near[-1][0] - near[0][0])))
    out = {"samples": n, "behind": behind / n if n else None, "median_pt": median(pts) if pts else None,
           "median_s": median(secs) if secs else None, "timed": len(secs)} if n else None
    _lag[str(state_dir)] = (now, out)
    return out
