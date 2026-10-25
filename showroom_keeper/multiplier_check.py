"""イベントの倍率 (有料ギフトの 2.5 倍など) が、本当に付いていたかを、記録したギフトと貢献ランキングでくらべて見当を付ける (読むだけ。写しで動かす)。

記録しているルームで、同じ人・同じルーム・同じイベントについて
「記録したギフトの元 pt (カタログの単価 × 個数。倍率未反映)」と「貢献ランキングの pt (SHOWROOM の取得値)」をくらべる。

- くらべる人: そのイベントの貢献ランキング (名前で推測した行は除く) に出ていて、イベントの間に、そのルームで記録したギフトがある人。
  無料のギフト (SMS 認証の有無で入るかが変わる) や、カタログに無いギフト (pt が分からない) を送った人は、割合がずれるので除き、数だけ出す
- 割合 = 貢献ランキングの pt ÷ 記録した有料ギフトの元 pt。2.5 倍が付いていれば 2.5 前後 (500 pt 以上・まとめ投げの 1.2 倍で 3 前後)、
  付いていなければ 1 前後 (1.2 倍だけなら 1.2 前後)。記録が抜けている (記録を止めていた・落ちていた) と大きく出る
- イベントごとに、割合の中央値で「2.5 倍が付いていそう」「付いていなさそう」「分からない」に分ける (くらべる人が MIN_USERS 人に満たなければ「分からない」)。
  これは見当で、確かめたものではない。1 人ずつのギフトの倍率を決めるものではない (整合を見る診断)
- 2.5 倍は 2021-03-01 18 時以降のイベントから (docs/SHOWROOM公式の決まり.md 2-2)。その前に終わったイベントは別に数える

使い方: python -m showroom_keeper.multiplier_check <写しのあるフォルダ> [--catalog FILE] [--out FILE]
写しのフォルダには research.db と、ギフトの一覧 (cache/gift_catalog.json) が要る。写しは読み取り専用で開く。サーバーでは動かさない。
出力に名前・ユーザー ID は入れない (イベントの番号と数だけ)。
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
import time
from pathlib import Path
from statistics import median
from typing import Any

from . import backtest_event

log = logging.getLogger(__name__)

VERSION = 1
FILE = "multiplier-check.json"
MIN_USERS = 5  # これより少ない人数のイベントは「分からない」
START_2_5 = 1614589200  # 2021-03-01 18:00 JST。これより前に始まったイベントには 2.5 倍が無い
WITH_RATE = (2.0, 3.6)  # 中央値がこの間なら「2.5 倍が付いていそう」(2.5〜3.0 に、記録の小さな抜けの余裕)
WITHOUT_RATE = (0.8, 1.5)  # この間なら「付いていなさそう」(1.0〜1.2)
BANDS = ((0, 0.8, "0.8 未満"), (0.8, 1.5, "0.8〜1.5"), (1.5, 2.0, "1.5〜2"), (2.0, 3.6, "2〜3.6"), (3.6, float("inf"), "3.6 以上"))


def load_catalog(path: Path) -> dict[int, dict[str, Any]]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return {int(k): v for k, v in data["gifts"].items()}


def _class(med: float | None, n: int) -> str:
    if med is None or n < MIN_USERS:
        return "unknown"
    if WITH_RATE[0] <= med < WITH_RATE[1]:
        return "with_2_5"
    if WITHOUT_RATE[0] <= med < WITHOUT_RATE[1]:
        return "without"
    return "unknown"


def event_room(conn: sqlite3.Connection, catalog: dict[int, dict[str, Any]], event_id: int, room_id: int, key: str,
               start: float, end: float) -> dict[str, Any]:
    """1 つのイベント × ルームの、人ごとの割合 (人は出さない) と、除いた人の数。"""
    pts = {r[0]: r[1] for r in conn.execute("""SELECT user_id, point FROM contributions
                                               WHERE event_id=? AND room_id=? AND guessed IS NULL AND point > 0""", (event_id, room_id))}
    paid: dict[int, int] = {}
    skip: dict[int, str] = {}
    for uid, gid, num in conn.execute("SELECT user_id, gift_id, num FROM gift_events WHERE room_key=? AND at >= ? AND at <= ?",
                                      (key, start, end)):
        if uid not in pts or uid in skip:
            continue
        info = catalog.get(gid)
        if not info or info.get("point") is None:
            skip[uid] = "unknown_gift"
        elif info.get("free", True):
            skip[uid] = "free_gift"
        else:
            paid[uid] = paid.get(uid, 0) + int(info["point"]) * int(num or 1)
    ratios = [pts[u] / p for u, p in paid.items() if u not in skip and p > 0]
    dropped: dict[str, int] = {}
    for why in skip.values():
        dropped[why] = dropped.get(why, 0) + 1
    return {"ratios": ratios, "dropped": dropped, "listed": len(pts)}


def run(state_dir: Path, catalog_path: Path | None = None) -> dict[str, Any]:
    started = time.time()
    catalog = load_catalog(catalog_path or Path(state_dir) / "cache" / "gift_catalog.json")
    conn = backtest_event.connect(state_dir)
    try:
        keys = [r[0] for r in conn.execute("SELECT room_key FROM gift_event_pos")]  # 記録しているルーム (ギフト 1 件ずつの表に足したルーム。gift_events を全部読まない)
        rooms = {}
        for k in keys:
            r = conn.execute("SELECT room_id FROM rooms WHERE key=? ORDER BY room_id LIMIT 1", (k,)).fetchone()
            if r:
                rooms[k] = r[0]
        events: dict[int, dict[str, Any]] = {}
        for key, rid in rooms.items():
            for eid, start, end in conn.execute("""SELECT e.event_id, e.start, e.end FROM room_events re JOIN events e USING(event_id)
                                                   WHERE re.room_id=? AND e.start IS NOT NULL AND e.end IS NOT NULL""", (rid,)):
                got = event_room(conn, catalog, eid, rid, key, start, end)
                if not got["ratios"] and not got["dropped"]:
                    continue
                ev = events.setdefault(eid, {"start": start, "ratios": [], "dropped": {}, "rooms": 0})
                ev["ratios"] += got["ratios"]
                ev["rooms"] += 1
                for k, v in got["dropped"].items():
                    ev["dropped"][k] = ev["dropped"].get(k, 0) + v
    finally:
        conn.close()
    out_events = []
    for eid, ev in sorted(events.items()):
        med = median(ev["ratios"]) if ev["ratios"] else None
        out_events.append({"event_id": eid, "month": time.strftime("%Y-%m", time.gmtime(ev["start"] + 9 * 3600)),
                           "before_2_5": ev["start"] < START_2_5, "rooms": ev["rooms"], "users": len(ev["ratios"]),
                           "dropped": ev["dropped"], "median_ratio": round(med, 3) if med is not None else None,
                           "class": _class(med, len(ev["ratios"]))})
    summary: dict[str, Any] = {}
    for part, pick in (("since_2_5", lambda e: not e["before_2_5"]), ("before_2_5", lambda e: e["before_2_5"])):
        xs = [e for e in out_events if pick(e)]
        summary[part] = {"events": len(xs), **{c: sum(1 for e in xs if e["class"] == c) for c in ("with_2_5", "without", "unknown")},
                         "median_bands": {label: sum(1 for e in xs if e["median_ratio"] is not None and lo <= e["median_ratio"] < hi)
                                          for lo, hi, label in BANDS}}
    return {"version": VERSION, "at": time.time(), "kind": "記録したギフトと貢献ランキングの整合の診断 (見当。確かめたものではない)",
            "min_users": MIN_USERS, "with_rate": WITH_RATE, "without_rate": WITHOUT_RATE, "summary": summary,
            "events": out_events, "seconds": round(time.time() - started)}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="イベントの倍率が付いていたかの見当 (写しで動かす。読むだけ)")
    p.add_argument("state_dir")
    p.add_argument("--catalog", default=None, help="ギフトの一覧 (既定: <写しのフォルダ>/cache/gift_catalog.json)")
    p.add_argument("--out", default=None)
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    out = run(Path(a.state_dir), Path(a.catalog) if a.catalog else None)
    path = Path(a.out) if a.out else Path(a.state_dir) / FILE
    path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"結果: {path} ({out['seconds']} 秒)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
