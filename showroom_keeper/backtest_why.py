"""予測が外れる原因の調べ (backtest.py の採点を、中身に分けて数える)。読むだけで、何も直さない。

M さんの指示 (2026-10-25): 「軽率に修正せずに徹底的に原因を調査せよ」。採点 (backtest.py) の 1・2 回目で、総合力の並び 55%・
「上り調子」「潜伏の疑い」などが当てずっぽう以下・地力が実際の 8 分の 1 だった。その原因の候補を、数字で確かめる:

- H1 採点の仕方: 総合力を「どのルームでもよい勝負の回」の量とくらべている (推しのルームの勝負とくらべたらどうか)。
  並びの採点が「出てくるか」と「出てきた人の中の量」を混ぜている
- H2 平均に戻る: 人を「どこかの月に多く出した人」で選び、ラベルを「増えた・減った」で決めるので、次は戻りやすい。
  人全体の月の量が、季節で動いていないか
- H3 直近を見ていない: 総合力・出てくる見込みは 12〜24 か月の中ほど・最大で、今出しているかを見ない
- H4 ルームの量の一部しか見ていない: 地力はパワーギフター (全体の上から 1 割の月がある人) の、上位 100 人の分だけ
- H5 ルームの予定: 次の月にギフターが出さないのは、気持ちではなく、推しのルームがイベントに出ていないだけではないか
- H6 1 回の大きなイベント: 「上り調子」は、直近の 1 回の大きなイベントだけで決まっていないか

backtest.py と同じく、research.db の写しで、サーバーの外で動かす。ギフター・ルームの名前と ID は結果に出さない (数の集まりだけ)。
  使い方: python -m showroom_keeper.backtest_why <写しのあるフォルダ> [--at 2026-04 --at 2026-05 --at 2026-06] [--sample 2000]
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import sys
import time
from pathlib import Path
from statistics import median
from typing import Any, Iterable

from . import backtest, gifter_analysis, gifter_status, heat
from .gifter_analysis import _add
from .research_store import MONTH_SQL, Store

log = logging.getLogger(__name__)

DAY = 86400
NEAR = backtest.NEAR


def _q(xs: list[float], qs: Iterable[float] = (0.1, 0.25, 0.5, 0.75, 0.9)) -> dict[str, float] | None:
    """分位 (10%・25%・50%・75%・90%)。"""
    v = sorted(xs)
    if not v:
        return None
    return {f"p{int(q * 100)}": round(v[min(len(v) - 1, int(q * len(v)))], 3) for q in qs}


def _auc(pos: list[float], neg: list[float], rnd: random.Random, n: int = 200_000) -> float | None:
    """出てきた人 (pos) と出てこなかった人 (neg) を 1 人ずつ選んだ時に、予測が出てきた人の方を高く言う割合 (同じ値は半分)。"""
    if not pos or not neg:
        return None
    hit = 0.0
    for _ in range(n):
        a, b = rnd.choice(pos), rnd.choice(neg)
        hit += 1.0 if a > b else 0.5 if a == b else 0.0
    return round(hit / n, 3)


def _order(pairs: list[tuple[float, float]], rnd: random.Random, n: int = 200_000) -> float | None:
    """(予測, 実際) の組から 2 つ選んで、予測の大きい方が実際も大きい割合 (どちらかが同じ値の組は数えない)。"""
    if len(pairs) < 2:
        return None
    agree = total = 0
    for _ in range(n):
        (p1, a1), (p2, a2) = rnd.sample(pairs, 2)
        if p1 == p2 or a1 == a2:
            continue
        total += 1
        agree += (p1 > p2) == (a1 > a2)
    return round(agree / total, 3) if total else None


def _room_events(s: Store, rid: int | None, t0: float, t1: float) -> int:
    if rid is None:
        return 0
    return s.scalar("""SELECT COUNT(*) FROM room_events re JOIN events e USING(event_id)
                       WHERE re.room_id=? AND re.point > 0 AND e.end > ? AND e.end <= ?""", (rid, t0, t1), 0) or 0


def why_at(state_dir: Path, at: float, sample: int | None, seed: int = 1) -> dict[str, Any]:
    """時点 at で、ギフターとルームの予測を中身に分けて数える。"""
    rnd = random.Random(seed)
    month = gifter_analysis.last_month(at)
    cur = _add(month, 1)
    end3 = backtest.month_start(_add(cur, 3))
    start3 = backtest.month_start(_add(month, -2))  # 直近 3 か月の始まり
    people_rows: list[dict[str, Any]] = []
    with Store(state_dir) as past, Store(state_dir) as full:
        backtest.hide_after(past, at)
        while gifter_status.build_rounds(past, at, limit=10_000):
            pass
        everyone = gifter_status.population(past, at)
        people = sorted(random.Random(seed).sample(everyone, sample)) if sample and len(everyone) > sample else everyone
        battles = {(r[0], r[1]) for r in full.all("SELECT event_id, room_id FROM event_rounds WHERE battle=1 AND end > ? AND end <= ?",
                                                    (at, at + NEAR))}
        edges: dict[int, int] = {}
        cache: dict[Any, Any] = {}
        for uid in people:
            st = gifter_status.compute(past, uid, at, edges, cache)
            if st is None:
                continue
            with past.tx() as c:
                c.execute("INSERT OR REPLACE INTO temp.gifter_status(user_id, version, at, overall, firepower, budget, left_pt, state, top_room, "
                          "top_share, data) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                          (uid, gifter_status.VERSION, at, st["overall"], st["fire"], st["hp"], st["mp"], st["state"], st["_top_room"],
                           st["_top_share"], json.dumps({k: v for k, v in st.items() if not k.startswith("_")}, ensure_ascii=False)))
            fut = backtest._future(full, uid, at, end3)
            near = [r for r in fut if r["end"] <= at + NEAR]
            main = st["rooms"][0]["room_id"] if st["rooms"] else None
            prev = past.all("""SELECT c.room_id, c.point, e.end FROM contributions c JOIN events e USING(event_id)
                               WHERE c.user_id=? AND c.point > 0 AND e.end > ? ORDER BY e.end DESC""", (uid, start3))
            months = {m: past.scalar("SELECT points FROM user_month WHERE user_id=? AND month=?", (uid, m), 0) or 0
                      for m in (month, _add(month, -1), _add(month, -2))}
            mind = st.get("psyche") or {}
            o = mind.get("outlook") or {}
            people_rows.append({
                "overall": float(st["overall"] or 0), "fire": float(st["fire"] or 0), "hp": float(st["hp"] or 0), "state": st["state"],
                "phase": mind.get("phase"), "appear": o.get("appear"), "outlook_room_is_main": o.get("room_id") == main,
                "factor": st.get("factor"), "ready": float(st.get("ready") or 0),
                "near_max": float(max((r["point"] for r in near), default=0)),
                "near_main_max": float(max((r["point"] for r in near if r["room_id"] == main), default=0)),
                "battle_any": max((r["point"] for r in near if (r["event_id"], r["room_id"]) in battles), default=None),
                "battle_main": max((r["point"] for r in near if r["room_id"] == main and (r["event_id"], r["room_id"]) in battles), default=None),
                "main_battle_exists": any(rid == main for _, rid in battles) if main is not None else False,
                "recent3": float(sum(months.values())), "last_month": float(months[month]),
                "recent_max_event": float(max((r["point"] for r in prev), default=0)),
                "recent_events": len(prev),
                "next3": float(sum(r["point"] for r in fut)),
                "next90_main": float(max((r["point"] for r in fut if r["room_id"] == o.get("room_id") and r["end"] <= at + 90 * DAY), default=0))
                if o.get("room_id") is not None else None,
                "main_events_prev": _room_events(past, main, start3, at),
                "main_events_next": _room_events(full, main, at, end3),
                "outlook_room_events_next90": _room_events(full, o.get("room_id"), at, at + 90 * DAY) if o.get("room_id") is not None else None,
            })
        rooms = _rooms_why(past, full, at, set(everyone), rnd)
        season = _season(full, month)
    return {"at": at, "month": month, "people": len(people_rows), "population": len(everyone),
            "gifters": _gifters_why(people_rows, rnd), "rooms": rooms, "season": season}


def _gifters_why(rows: list[dict[str, Any]], rnd: random.Random) -> dict[str, Any]:
    out: dict[str, Any] = {}
    # H1・H3: 並びを「出てくるか」と「出てきた人の中の量」に分ける
    came = [r for r in rows if r["near_max"] > 0]
    gone = [r for r in rows if r["near_max"] == 0]
    out["came_share"] = round(len(came) / len(rows), 3) if rows else None  # 次の 30 日に、どこかで出した人の割合
    preds = {"overall": "総合力", "fire": "火力", "hp": "予算", "last_month": "先月の量", "recent3": "直近 3 か月の量", "recent_max_event": "直近 3 か月の 1 回の最大"}
    out["who_comes"] = {label: _auc([r[k] for r in came], [r[k] for r in gone], rnd) for k, label in preds.items()}
    out["amount_order_among_came"] = {label: _order([(r[k], r["near_max"]) for r in came], rnd) for k, label in preds.items()}
    out["overall_zero_share"] = round(sum(1 for r in rows if not r["overall"]) / len(rows), 3) if rows else None
    out["overall_zero_by_state"] = {}
    for st in sorted({r["state"] for r in rows}):
        xs = [r for r in rows if r["state"] == st]
        out["overall_zero_by_state"][st] = {"n": len(xs), "zero": round(sum(1 for r in xs if not r["overall"]) / len(xs), 3),
                                            "came": round(sum(1 for r in xs if r["near_max"] > 0) / len(xs), 3)}
    # H1: 総合力を、どのルームでもよい勝負の回か、推しのルームの勝負の回か
    any_r = [r["battle_any"] / r["overall"] for r in rows if r["overall"] and r["battle_any"]]
    main_r = [r["battle_main"] / r["overall"] for r in rows if r["overall"] and r["battle_main"]]
    out["overall_vs_battle"] = {
        "any_room": {"n": len(any_r), "ratio": _q(any_r), "within": round(sum(1 for x in any_r if 0.5 <= x <= 2) / len(any_r), 3) if any_r else None},
        "main_room": {"n": len(main_r), "ratio": _q(main_r), "within": round(sum(1 for x in main_r if 0.5 <= x <= 2) / len(main_r), 3) if main_r else None},
        "main_room_had_battle": round(sum(1 for r in rows if r["main_battle_exists"]) / len(rows), 3) if rows else None,
    }
    fire_r = [r["near_max"] / r["fire"] for r in rows if r["fire"] and r["near_max"]]
    fire_main = [r["near_main_max"] / r["fire"] for r in rows if r["fire"] and r["near_main_max"]]
    out["fire_vs_next"] = {"any_room": _q(fire_r), "main_room": _q(fire_main), "n_any": len(fire_r), "n_main": len(fire_main)}
    # H2: 次の 3 か月 ÷ 直近 3 か月 (人全体と、状態・時期ごと)
    ratio = [(r["next3"] + 1) / (r["recent3"] + 1) for r in rows if r["recent3"]]
    out["next3_over_recent3"] = {"all": _q(ratio), "share_up": round(sum(1 for x in ratio if x >= 1) / len(ratio), 3) if ratio else None}
    for key in ("state", "phase"):
        out[f"next3_by_{key}"] = {}
        for k in sorted({r[key] for r in rows if r[key]}):
            xs = [(r["next3"] + 1) / (r["recent3"] + 1) for r in rows if r[key] == k and r["recent3"]]
            out[f"next3_by_{key}"][k] = {"n": len(xs), "ratio": _q(xs)}
    # H2: 直近 3 か月が、その人のふだん (予算 × 3) の何倍だったかで分けて、次の 3 か月が戻るか
    bands = ((0, 0.5), (0.5, 1), (1, 1.5), (1.5, 3), (3, 1e9))
    out["reversion"] = []
    for lo, hi in bands:
        xs = [r for r in rows if r["hp"] and lo <= r["recent3"] / (3 * r["hp"]) < hi]
        rr = [(r["next3"] + 1) / (r["recent3"] + 1) for r in xs]
        out["reversion"].append({"recent_vs_usual": f"{lo}〜{hi if hi < 1e8 else ''}", "n": len(xs), "next_over_recent": _q(rr),
                                 "share_up": round(sum(1 for x in rr if x >= 1) / len(rr), 3) if rr else None})
    # H5: 推しのルームのイベントの数が、直近 3 か月と次の 3 か月でどう変わったか
    out["room_schedule"] = []
    for name, test in (("推しのルームのイベントが減った (半分以下)", lambda a, b: b <= 0.5 * a),
                       ("同じくらい", lambda a, b: 0.5 * a < b < 1.5 * a),
                       ("増えた (1.5 倍以上)", lambda a, b: b >= 1.5 * a)):
        xs = [r for r in rows if r["main_events_prev"] and test(r["main_events_prev"], r["main_events_next"])]
        rr = [(r["next3"] + 1) / (r["recent3"] + 1) for r in xs if r["recent3"]]
        out["room_schedule"].append({"room": name, "n": len(xs), "next_over_recent": _q(rr),
                                     "share_up": round(sum(1 for x in rr if x >= 1) / len(rr), 3) if rr else None})
    out["main_room_no_event_next3"] = round(sum(1 for r in rows if r["main_events_prev"] and not r["main_events_next"]) / len(rows), 3) if rows else None
    # H5: 心のカルテ「出てくる見込み 高い」の外れのうち、推しのルームが 90 日イベントに出ていなかった割合
    high = [r for r in rows if r["appear"] == "高い" and r["next90_main"] is not None]
    miss = [r for r in high if not r["next90_main"]]
    out["appear_high"] = {"n": len(high), "miss": len(miss),
                          "miss_room_had_no_event": round(sum(1 for r in miss if not r["outlook_room_events_next90"]) / len(miss), 3) if miss else None,
                          "miss_gave_elsewhere": round(sum(1 for r in miss if r["near_max"] > 0) / len(miss), 3) if miss else None,
                          "outlook_room_is_main": round(sum(1 for r in high if r["outlook_room_is_main"]) / len(high), 3) if high else None}
    # H6: 「上り調子」は、直近 3 か月の 1 回の大きなイベントで決まっていないか
    up = [r for r in rows if r["state"] == "上り調子" and r["recent3"]]
    out["rising_one_event"] = {"n": len(up), "max_event_share": _q([r["recent_max_event"] / r["recent3"] for r in up]),
                               "share_one_event_half": round(sum(1 for r in up if r["recent_max_event"] >= 0.5 * r["recent3"]) / len(up), 3) if up else None,
                               "share_few_events": round(sum(1 for r in up if r["recent_events"] <= 2) / len(up), 3) if up else None}
    return out


def _rooms_why(past: Store, full: Store, at: float, population: set[int], rnd: random.Random) -> dict[str, Any]:
    """H4: 地力が、ルームのポイントのどれだけを見ているか。ルームの大きさで分ける。"""
    run = heat._PowerRun(None)
    while not run.add(past, heat.CHUNK):
        pass
    power = {rid: float(t[0]) for rid, t in run.totals.items() if t[0] > 0}
    rows = []
    for e in full.all("SELECT event_id FROM events WHERE start >= ? AND end IS NOT NULL AND end <= ?", (at, at + NEAR)):
        for r in full.all("SELECT room_id, point, c_sum, c_count FROM room_events WHERE event_id=? AND point > 0", (e["event_id"],)):
            contrib = full.all("SELECT user_id, point FROM contributions WHERE event_id=? AND room_id=?", (e["event_id"], r["room_id"]))
            pop_pt = sum(c["point"] for c in contrib if c["user_id"] in population)
            rows.append({"room": r["room_id"], "point": float(r["point"]), "top100": (r["c_sum"] or 0) / r["point"] if r["c_sum"] is not None else None,
                         "power_gifters": pop_pt / r["point"], "power": power.get(r["room_id"]), "has_contrib": bool(contrib)})
    pts = sorted(x["point"] for x in rows)
    cuts = [pts[int(len(pts) * q)] for q in (0.5, 0.9)] if pts else [0, 0]
    out: dict[str, Any] = {"room_events": len(rows), "with_power": round(sum(1 for x in rows if x["power"]) / len(rows), 3) if rows else None}
    for name, lo, hi in (("下位 5 割", 0, cuts[0]), ("5〜9 割", cuts[0], cuts[1]), ("上位 1 割", cuts[1], 1e18)):
        xs = [x for x in rows if lo <= x["point"] < hi and x["has_contrib"]]
        out[name] = {"n": len(xs), "top100_share": _q([x["top100"] for x in xs if x["top100"] is not None]),
                     "power_gifter_share": _q([x["power_gifters"] for x in xs]),
                     "with_power": round(sum(1 for x in xs if x["power"]) / len(xs), 3) if xs else None,
                     "actual_over_power": _q([x["point"] / x["power"] for x in xs if x["power"]])}
    return out


def _season(full: Store, month: str) -> list[dict[str, Any]]:
    """H2: SHOWROOM 全体の月ごとの量 (月の量の合計・出した人・中堅の下限)。採点の時点の前後 18 か月。"""
    out = []
    for k in range(-15, 4):
        m = _add(month, k)
        row = full.one("SELECT COUNT(*) AS n, SUM(points) AS pt FROM user_month WHERE month=?", (m,))
        events = full.scalar(f"SELECT COUNT(*) FROM events e WHERE e.end IS NOT NULL AND {MONTH_SQL}=?", (m,), 0)
        out.append({"month": m, "people": row["n"], "points": row["pt"], "events": events, "mid": gifter_analysis.month_cuts(full, m).get("mid")})
    return out


def run(state_dir: Path, months: list[str], sample: int | None) -> dict[str, Any]:
    started = time.time()
    out = {"at": time.time(), "months": months, "sample": sample, "points": []}
    for m in months:
        log.info("原因の調べ: %s の初めの時点", m)
        gifter_analysis._cuts.clear()
        out["points"].append(why_at(Path(state_dir), backtest.month_start(m), sample))
    gifter_analysis._cuts.clear()
    out["seconds"] = round(time.time() - started)
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="予測が外れる原因の調べ (research.db の写しで動かす。サーバーでは動かさない。読むだけ)")
    p.add_argument("state_dir")
    p.add_argument("--at", action="append")
    p.add_argument("--sample", type=int, default=2000)
    p.add_argument("--out", default=None)
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    months = a.at or backtest.default_months(time.time())
    out = run(Path(a.state_dir), months, a.sample)
    path = Path(a.out) if a.out else Path(a.state_dir) / "backtest-why.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"結果: {path} ({out['seconds']} 秒)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
