"""ギフターの分析: ルームごとの毎月の状態・乗り換え・規模をそろえた大口/中堅/少額。

もとにするのは、イベントの貢献ランキング (上位 100 人) の月ごとの合計 (イベントが終わった月、日本時間)。
- 「送っていない」は「上位 100 人に入っていない」こと (100 位より下で送っていた場合も含む)
- ルームがその月にイベントに出ていなければ、送る場が無かったので「休眠」にはしない (「ルームが休み」)
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from .research_store import MONTH_SQL, Store
from .status import JST

PRIOR = 3  # くらべる前の月数
GROW = 1.5  # 前の月の平均のこれ以上なら「増加」
SHRINK = 0.5  # これ以下なら「減少」
SWITCH_BEFORE = 6  # 乗り換え: 前の最推しを決める月数 (くらべる前の月より前)
SWITCH_RECENT = 3  # 乗り換え: 今の最推しを決める月数 (いちばん新しい月まで)
TIER_TOP = 0.01  # その月に貢献ランキングに出た全員の上から 1% まで = 大口
TIER_MID = 0.10  # 上から 10% まで = 中堅 (それより下は少額)
TIER_MONTHS = 12  # 大口・中堅・少額を出す月数
STATE_ROOMS = 20  # 状態を出すルームの数 (最近の量の多い順)
CUT_TTL = 86400  # 月ごとの区切りを覚えておく時間 (古い月も集め直しで増えるので、1 日で読み直す)

_cuts: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}


def _add(month: str, n: int) -> str:
    y, m = map(int, month.split("-"))
    m += n
    y, m = y + (m - 1) // 12, (m - 1) % 12 + 1
    return f"{y:04d}-{m:02d}"


def last_month(now: float) -> str:
    """集め終わった、いちばん新しい月 (今の月は途中なので、その前の月)。"""
    return _add(datetime.fromtimestamp(now, JST).strftime("%Y-%m"), -1)


def month_cuts(s: Store, month: str) -> dict[str, Any]:
    """その月の、大口・中堅の下限のポイント (その月に貢献ランキングに出た人の、上から 1%・10% の人の量)。"""
    key = (str(s.state_dir), month)
    hit = _cuts.get(key)
    if hit and time.time() - hit[0] < CUT_TTL:
        return hit[1]
    n = s.scalar("SELECT COUNT(*) FROM user_month WHERE month=?", (month,)) or 0

    def at(frac: float) -> int | None:
        if not n:
            return None
        row = s.one("SELECT points FROM user_month WHERE month=? ORDER BY points DESC LIMIT 1 OFFSET ?",
                    (month, max(0, int(n * frac) - 1)))
        return row[0] if row is not None else None

    out = {"month": month, "people": n, "top": at(TIER_TOP), "mid": at(TIER_MID)}
    _cuts[key] = (time.time(), out)
    return out


def tier(points: int, cuts: dict[str, Any]) -> str | None:
    if not points or cuts["top"] is None:
        return None
    return "大口" if points >= cuts["top"] else "中堅" if points >= (cuts["mid"] or 0) else "少額"


def _room_state(per: dict[str, int], month: str, active: bool) -> str | None:
    now_pt = per.get(month, 0)
    prior = [per.get(_add(month, -k), 0) for k in range(1, PRIOR + 1)]
    earlier = any(pt for m, pt in per.items() if m < _add(month, -PRIOR))
    if now_pt:
        if not any(prior):
            return "復帰" if earlier else "新しく出た"
        base = sum(prior) / sum(1 for pt in prior if pt)
        return "増加" if now_pt >= GROW * base else "減少" if now_pt <= SHRINK * base else "安定"
    if any(prior):
        return "休眠" if active else "ルームが休み"
    return None


def gifter_states(s: Store, uid: int, now: float | None = None) -> dict[str, Any]:
    """1 人のギフターの、ルームごとの今の状態 (いちばん新しい集め終わった月)・乗り換え・月ごとの大口/中堅/少額。"""
    now = time.time() if now is None else now
    month = last_month(now)
    per: dict[int, dict[str, int]] = {}
    for r in s.all(f"""SELECT c.room_id, {MONTH_SQL} AS m, SUM(c.point) AS pt FROM contributions c JOIN events e USING(event_id)
                       WHERE c.user_id=? AND e.end IS NOT NULL GROUP BY c.room_id, m""", (int(uid),)):
        if r["m"] and r["m"] <= month:
            per.setdefault(r["room_id"], {})[r["m"]] = r["pt"]
    since = _add(month, -PRIOR)
    recent = sorted(per, key=lambda rid: -sum(pt for m, pt in per[rid].items() if m >= since))[:STATE_ROOMS]
    rooms = []
    for rid in recent:
        active = s.one(f"""SELECT 1 FROM room_events re JOIN events e USING(event_id)
                           WHERE re.room_id=? AND e.end IS NOT NULL AND {MONTH_SQL}=? LIMIT 1""", (rid, month)) is not None
        state = _room_state(per[rid], month, active)
        if state:
            prior = [per[rid].get(_add(month, -k), 0) for k in range(1, PRIOR + 1)]
            rooms.append({"room_id": rid, "state": state, "points": per[rid].get(month, 0),
                          "prior_avg": sum(prior) / PRIOR, "active": active})

    def main(first: str, last: str) -> tuple[int | None, int]:
        sums = {rid: sum(pt for m, pt in v.items() if first <= m <= last) for rid, v in per.items()}
        rid = max(sums, key=lambda k: sums[k], default=None)
        return (rid, sums[rid]) if rid is not None and sums[rid] else (None, 0)

    recent_first = _add(month, -(SWITCH_RECENT - 1))
    old, old_pt = main(_add(recent_first, -SWITCH_BEFORE), _add(recent_first, -1))
    new, _ = main(recent_first, month)
    switch = None
    if old is not None and new is not None and old != new:
        old_recent = sum(pt for m, pt in per[old].items() if m >= recent_first) / SWITCH_RECENT
        if old_recent <= SHRINK * old_pt / SWITCH_BEFORE:  # 前の最推しへの量が、月あたり半分以下に減った
            switch = {"from": old, "to": new, "from_before": old_pt / SWITCH_BEFORE, "from_recent": old_recent}

    totals = {r["month"]: r["points"] for r in s.all("SELECT month, points FROM user_month WHERE user_id=? AND month>=?",
                                                       (int(uid), _add(month, -(TIER_MONTHS - 1))))}
    tiers = []
    for k in range(TIER_MONTHS - 1, -1, -1):
        m = _add(month, -k)
        pt = totals.get(m, 0)
        cuts = month_cuts(s, m) if pt else {"top": None, "mid": None, "people": None}
        tiers.append({"month": m, "points": pt, "tier": tier(pt, cuts), "top": cuts["top"], "mid": cuts["mid"],
                      "people": cuts["people"]})
    return {"month": month, "rooms": rooms, "switch": switch, "tiers": tiers}
