"""ギフター × ルームの、過去の支援の記録 (4 つの量のうち、事実の 3 つ)。ギフターのページの「ルームごとの支援の記録」。

全体の見直し (2026-10-27、project の資料「全体見直し_2026-10-27」の 4 章の ④) で M さんが選んだ。
システムの目的「だれが、どのルームに、何 pt 投げる実力があるか」の、過去の最大・ふつう・直近を、ルームごとに出す。
**予測ではない** (次のイベントの見込みは、採点 backtest_pair で何も考えない予測に勝ってから出す)。

- 量は、イベントの貢献 pt (SHOWROOM の取得値。イベントの倍率はもう入っている。掛け直さない)
- 使う回 = 貢献ランキングを、イベントが終わったあとに読んだ回 (開催中の途中の値は使わない)・名前で推測した行が無い・1 人以上
  (正式な結果のあとに読んだかは、ここでは分けない。終わったことと正式な結果を確かめたことは同じではない)
- ルームが出たのに使えない回 (読めていない・推測の行がある) は「分からない」、使える回で 100 人の一覧に載っていない回は「100 位の外」(0 ではない)、
  100 人に届かない一覧に載っていない回だけ「0」。出てきた割合の分母は、使える回だけ (取れなかった回を「出てこなかった」に数えない)
- どれも「何回分か」と信頼度 (gifter_status.reliability と同じ線) を付ける
"""

from __future__ import annotations

from typing import Any

from .gifter_status import reliability
from .research_store import CONTRIB_FULL, median

DAY = 86400
YEAR = 365 * DAY
RATE_DAYS = 180 * DAY
RECENT_DAYS = 90 * DAY
ROOMS = 8  # 出すルームの数 (365 日に多く送った順)

# この人の、推測でない貢献ランキングの行 (contributions_user_pt の索引で 1 人分を読み、ルームの回は主キーで引く)
MINE_SQL = """SELECT c.event_id, c.room_id, c.point, e.name, e.end, e.show_ranking, r.ranking_at, r.c_count
              FROM contributions c JOIN events e ON e.event_id = c.event_id
              JOIN room_events r ON r.room_id = c.room_id AND r.event_id = c.event_id
              WHERE c.user_id=? AND c.guessed IS NULL AND e.end IS NOT NULL AND e.end <= ?"""
# ルームが出た回 (主キー (room_id, event_id) で 1 ルーム分)
ROOM_SQL = """SELECT r.event_id, r.point, r.ranking_at, r.c_count, e.end, e.show_ranking
              FROM room_events r JOIN events e ON e.event_id = r.event_id
              WHERE r.room_id=? AND e.end IS NOT NULL AND e.end > ? AND e.end <= ?"""


def usable(r: Any) -> bool:
    """正解・履歴に使える回か (終わったあとに読んだ・推測の行が無い・1 人以上)。推測の行がある回は c_count が NULL。"""
    return bool(r["show_ranking"] and r["ranking_at"] is not None and r["ranking_at"] >= r["end"] and (r["c_count"] or 0) > 0)


def _quartiles(xs: list[float]) -> tuple[float, float] | None:
    if len(xs) < 4:
        return None
    ys = sorted(xs)
    return ys[len(ys) // 4], ys[(3 * len(ys)) // 4]


def by_room(s: Any, uid: int, now: float, label: Any = None, limit: int = ROOMS) -> dict[str, Any]:
    """ルームごとの、過去の最大・ふつう・直近と、回の数。label(room_id) → (key, name)。"""
    mine = [r for r in s.all(MINE_SQL, (int(uid), now)) if usable(r)]
    by: dict[int, list[Any]] = {}
    for r in mine:
        by.setdefault(r["room_id"], []).append(r)
    year = {rid: sum(r["point"] or 0 for r in rs if r["end"] > now - YEAR) for rid, rs in by.items()}
    order = sorted(by, key=lambda rid: (-year[rid], -max(r["point"] or 0 for r in by[rid]), rid))
    rooms = []
    for rid in order[:limit]:
        rs = sorted(by[rid], key=lambda r: -r["end"])
        best = max(rs, key=lambda r: (r["point"] or 0, r["end"]))
        listed = {r["event_id"] for r in rs}
        y = [float(r["point"] or 0) for r in rs if r["end"] > now - YEAR]
        recent = [float(r["point"] or 0) for r in rs if r["end"] > now - RECENT_DAYS]
        ran = [r for r in s.all(ROOM_SQL, (rid, now - YEAR, now)) if (r["point"] or 0) > 0]
        use = [r for r in ran if usable(r)]
        absent = [r for r in use if r["event_id"] not in listed]
        use180 = [r for r in use if r["end"] > now - RATE_DAYS]
        normal = median(y)
        q = _quartiles(y)
        key, name = label(rid) if label else (str(rid), str(rid))
        rooms.append({
            "room_id": rid, "key": key, "name": name,
            # 最大: 記録のある全部の期間の、1 イベントの最大 (何回の中の最大か)
            "max": {"point": best["point"], "event_id": best["event_id"], "event": best["name"] or str(best["event_id"]), "end": best["end"],
                    "rounds": len(rs)},
            # ふつう: 365 日に載った回の中ほどと、ばらつき (4 回以上で、下から 4 分の 1〜上から 4 分の 1)
            "normal": {"point": normal, "rounds": len(y), "low": q[0] if q else None, "high": q[1] if q else None},
            # 直近: 90 日に載った回の中ほどが、ふつうの何倍だったか (過去の説明。これから増えるとは言わない)
            "recent": {"point": median(recent), "rounds": len(recent),
                       "ratio": round(median(recent) / normal, 2) if recent and normal else None},
            # 365 日のルームの回: 載った・100 位の外 (量は分からない)・0 (100 人に届かない一覧にいない)・分からない (読めていない回)
            "year": {"ran": len(ran), "usable": len(use), "listed": len(y), "outside_top100": sum(1 for r in absent if r["c_count"] >= CONTRIB_FULL),
                     "zero": sum(1 for r in absent if r["c_count"] < CONTRIB_FULL), "unknown": len(ran) - len(use)},
            # 出てきた割合 (180 日。分母は使える回だけ)
            "share180": {"listed": sum(1 for r in use180 if r["event_id"] in listed), "usable": len(use180)},
            "reliability": reliability(len(y), None) if y else "参考",
        })
    return {"rooms": rooms, "room_total": len(by), "unit": "イベントの貢献 pt (SHOWROOM の取得値。倍率は入っている)"}
