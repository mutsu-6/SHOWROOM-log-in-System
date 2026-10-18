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
