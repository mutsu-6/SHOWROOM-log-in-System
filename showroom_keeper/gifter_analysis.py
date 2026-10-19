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
TIER_PEAK = 0.001  # 上から 0.1% まで = 頂点 (1 か月で全体の大きな割合を 1 人で出す人。人数の少ない月でも 1 人はいる)
TIER_TOP = 0.01  # その月に貢献ランキングに出た全員の上から 1% まで = 大口
TIER_MID = 0.10  # 上から 10% まで = 中堅 (それより下は少額)
TIER_MONTHS = 12  # 大口・中堅・少額を出す月数
STATE_ROOMS = 20  # 状態を出すルームの数 (最近の量の多い順)
# 見えた回数と壁ぎわ (docs/ギフターの分類.md の「見えない範囲の共通ルール」)。どれも仮の値で、データの分布を見て決め直す
WALL = 1.2  # そのイベント×ルームの 100 位のポイントの、これ倍までなら「壁ぎわ」(実際はもっと送っていた回があるかもしれない)
WALL_RATE = 0.5  # 見えた回 (100 人に届いた回) の、これだけ以上が壁ぎわなら「壁ぎわの少額」
MIN_OBS = 3  # 見えた回 (イベント×ルーム) がこれより少ない人は、型を決めない (未分類)
PILLAR_SHARE = 0.3  # ルームの上位 100 人の合計のうち、これだけ以上を 1 人で出していれば「ルームの柱」
PILLAR_ROOMS = 10  # 柱かどうかを出すルームの数 (送った量の多い順)
# 集中のしかた (docs/ギフターの分類.md の軸 4)。線はどれも仮の値
ONE_ROOM = 0.8  # 集中度 (各ルームの割合の 2 乗の合計) がこれ以上なら「一本推し」
MAIN_SHARE = 0.5  # 一番のルームの割合がこれ以上 (一本推しでない) なら「本命と掛け持ち」
SPREAD_ROOMS = 3.0  # 実効ルーム数 (1 ÷ 集中度) がこれ以上なら「箱推し・分散」
QUARTERS = 8  # 渡り鳥を見る四半期 (3 か月) の数 (2 年)
MOVED = 0.3  # 隣り合う四半期の送り先の近さ (コサイン類似度) がこれ未満なら「送り先が替わった」
BIRD_MOVES = 2  # 直近 1 年 (4 回の替わり目) に、これだけ以上替わったら「渡り鳥」
BIRD_QUARTERS = 3  # 送った四半期がこれより少なければ、渡り鳥かどうかは決めない
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
                    (month, max(0, int(n * frac + 1e-9) - 1)))
        return row[0] if row is not None else None

    out = {"month": month, "people": n, "peak": at(max(TIER_PEAK, 1 / n)) if n else None,
           "top": at(TIER_TOP), "mid": at(TIER_MID)}
    _cuts[key] = (time.time(), out)
    return out


def tier(points: int, cuts: dict[str, Any]) -> str | None:
    if not points or cuts["top"] is None:
        return None
    if cuts.get("peak") is not None and points >= cuts["peak"]:
        return "頂点"
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
        tiers.append({"month": m, "points": pt, "tier": tier(pt, cuts), "peak": cuts.get("peak"), "top": cuts["top"],
                      "mid": cuts["mid"], "people": cuts["people"]})
    first = _add(month, -(TIER_MONTHS - 1))
    seen = visibility(s, uid, first, month)
    return {"month": month, "rooms": rooms, "switch": switch, "tiers": tiers, "visibility": seen["visibility"],
            "pillars": seen["pillars"], "focus": focus(s, uid, per, month, seen["visibility"])}


def _cos(a: dict[int, float], b: dict[int, float]) -> float:
    dot = sum(v * b.get(k, 0) for k, v in a.items())
    na, nb = sum(v * v for v in a.values()) ** 0.5, sum(v * v for v in b.values()) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


def focus(s: Store, uid: int, per: dict[int, dict[str, int]], month: str, seen: dict[str, Any]) -> dict[str, Any]:
    """集中のしかた: 一本推し・本命と掛け持ち・数ルームに分散・箱推し・分散と、渡り鳥。

    - 直近 12 か月の、ルームごとの割合 s = そのルームに送った pt ÷ 全部。集中度 H = Σ s²、実効ルーム数 = 1 ÷ H
    - 上位 100 人に入らなかったルームの小口は見えないので、集中度は高めに出る (見かけの一本推し)。
      アクティブファンに出たのに貢献ランキングに出ていないルームの数 (見えないルーム) を一緒に出す
    - 渡り鳥: 四半期ごとの送り先の近さが、くり返し低い人 (乗り換えが一度きりの変化なのに対し、くり返す性質)
    """
    first = _add(month, -(TIER_MONTHS - 1))
    recent = {rid: sum(pt for m, pt in v.items() if first <= m <= month) for rid, v in per.items()}
    recent = {rid: pt for rid, pt in recent.items() if pt > 0}
    total = sum(recent.values())
    out: dict[str, Any] = {"months": [first, month], "rooms": len(recent)}
    if total:
        shares = sorted((pt / total for pt in recent.values()), reverse=True)
        h = sum(x * x for x in shares)
        out.update(index=h, effective=1 / h, top_share=shares[0])
        if seen.get("unclassified"):
            out["type"] = None
        elif h >= ONE_ROOM:
            out["type"] = "一本推し"
        elif shares[0] >= MAIN_SHARE:
            out["type"] = "本命と掛け持ち"
        elif 1 / h >= SPREAD_ROOMS:
            out["type"] = "箱推し・分散"
        else:
            out["type"] = "数ルームに分散"
    # 見えないルーム: その期間にアクティブファンに出たのに、貢献ランキングには出ていないルーム
    ym = (first.replace("-", ""), month.replace("-", ""))
    fan_rooms = {r[0] for r in s.all("SELECT DISTINCT room_id FROM active_fans WHERE user_id=? AND ym BETWEEN ? AND ?",
                                     (int(uid), *ym))}
    out["fan_only_rooms"] = len(fan_rooms - set(recent))
    # 渡り鳥: 四半期 (3 か月) ごとの送り先の割合
    quarters = []
    for q in range(QUARTERS - 1, -1, -1):
        last = _add(month, -3 * q)
        start = _add(last, -2)
        vec = {rid: float(sum(pt for m, pt in v.items() if start <= m <= last)) for rid, v in per.items()}
        quarters.append({"from": start, "to": last, "vec": {k: x for k, x in vec.items() if x > 0}})
    sent = [q for q in quarters if q["vec"]]
    sims = []
    for a, b in zip(quarters, quarters[1:]):
        sims.append(_cos(a["vec"], b["vec"]) if a["vec"] and b["vec"] else None)
    year = [x for x in sims[-4:] if x is not None]
    moves = sum(1 for x in year if x < MOVED)
    out["quarters"] = [{"from": q["from"], "to": q["to"], "rooms": len(q["vec"]),
                        "top": max(q["vec"], key=lambda k: q["vec"][k]) if q["vec"] else None} for q in quarters]
    out["similarity"] = sims
    out["moves"] = moves
    out["bird"] = None if len(sent) < BIRD_QUARTERS else moves >= BIRD_MOVES
    return out


def visibility(s: Store, uid: int, first: str, last: str) -> dict[str, Any]:
    """見えた回数・壁ぎわ率と、ルームの柱 (first〜last の月。イベントが終わった月)。

    - 見えた回 = 貢献ランキング (上位 100 人) に出たイベント×ルーム。名前で推測した分は数えない
    - 壁ぎわ = 100 人に届いたイベント×ルームで、ポイントが 100 位の WALL 倍まで。壁ぎわの多い人は、見えている額が下限
    - ルームの柱 = そのルームの、期間のイベントの上位 100 人の合計のうち、PILLAR_SHARE 以上を出したか、出た回の半分以上で 1 位
    """
    rows = s.all(f"""SELECT c.room_id, c.point, c.rank, re.c_p100 FROM contributions c
                     JOIN room_events re ON re.room_id=c.room_id AND re.event_id=c.event_id JOIN events e ON e.event_id=c.event_id
                     WHERE c.user_id=? AND c.guessed IS NULL AND e.end IS NOT NULL AND {MONTH_SQL} BETWEEN ? AND ?""",
                 (int(uid), first, last))
    full = [r for r in rows if r["c_p100"]]
    wall = sum(1 for r in full if (r["point"] or 0) <= WALL * r["c_p100"])
    rate = wall / len(full) if full else None
    seen = {"months": [first, last], "obs": len(rows), "full": len(full), "wall": wall, "wall_rate": rate,
            "unclassified": len(rows) < MIN_OBS, "wall_low": bool(full) and len(full) >= MIN_OBS and rate is not None and rate >= WALL_RATE}
    per: dict[int, dict[str, Any]] = {}
    for r in rows:
        x = per.setdefault(r["room_id"], {"points": 0, "obs": 0, "first": 0})
        x["points"] += r["point"] or 0
        x["obs"] += 1
        x["first"] += 1 if r["rank"] == 1 else 0
    pillars = []
    for rid, x in sorted(per.items(), key=lambda kv: -kv[1]["points"])[:PILLAR_ROOMS]:
        total = s.scalar(f"""SELECT SUM(re.c_sum) FROM room_events re JOIN events e USING(event_id)
                             WHERE re.room_id=? AND re.c_sum IS NOT NULL AND e.end IS NOT NULL AND {MONTH_SQL} BETWEEN ? AND ?""",
                         (rid, first, last), default=0)
        share = x["points"] / total if total else None
        pillar = (share is not None and share >= PILLAR_SHARE) or (x["first"] >= 2 and x["first"] * 2 >= x["obs"])
        pillars.append({"room_id": rid, "points": x["points"], "obs": x["obs"], "first": x["first"], "share": share,
                        "pillar": pillar})
    return {"visibility": seen, "pillars": pillars}
