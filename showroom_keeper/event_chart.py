"""イベントのページの「チャート」(株の取引の画面のような見方)。

新しく集めるものは無く、もう残している 1 時間ごとの推移 (event_progress) だけから作る。
- 出来高: 記録と記録のあいだに増えたポイント (1 時間足・6 時間足・日足)。間が空いた所 (GAP より長い) は 0 とせず「観測なし」の印を付ける
- 順位のローソク足: 日ごと (日本時間) の始め・いちばん良い・いちばん悪い・終わりの順位 (ポイントは下がらないので、ローソク足は順位で作る)
- 上下のルームとの差: 今 1 つ上・1 つ下にいるルームとの差の動き (その時その時の上下ではない。それを出すには毎回全ルームを読むことになり重い)
- 勢い (前日比): 直近 6 時間の増え方と、前の日の同じ時間帯 (24 時間前からの 6 時間) の増え方の比。
  夜は少なく夜の配信の時間は多い、という 1 日の波があるので、その前の 24 時間の平均と比べると、時間帯だけで高く・低く出てしまう
  (試しのデータで、夜中に見ると全ルームが 0.3 倍になった)。今のペースが続いた時の見込み (予測)
- 値上がり率の一覧: 参加ルーム全体の、直近 6 時間の増え方 (時刻ごとの全ルームの記録を 4 回分だけ読む)

ポイントは SHOWROOM のイベントのポイントそのまま (倍率は入っている) で、掛け直さない。
読むのは、索引で引ける分だけ: ルームごとの推移 (主キー event_id, room_id) と、時刻ごとの全ルーム (event_progress_at) を 3 回。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from .research_store import Store

GAP = 5400  # 記録と記録のあいだがこれより長いと、その間は「観測なし」(ふだんは 1 時間ごと。秒)
RECENT = 6 * 3600  # 勢いを見る直近の長さ (秒)
BASE = 24 * 3600  # 勢いを比べる、前の日の同じ時間帯までの長さ。移動平均の長さにも使う (秒)
JST = 9 * 3600
COMPARE_MAX = 4  # 重ねて比べられるルームの数
MOVERS = 300  # 値上がり率の一覧に出すルームの数 (今のポイントの多い順)
CACHE = 120  # 同じイベント・ルームの結果を使い回す時間 (秒。推移は 1 時間ごとにしか増えない)


def event_chart(state_dir: Path, event_id: Any, room: Any = None, compare: list[Any] | None = None,
                support: list[str] | None = None) -> dict[str, Any]:
    from . import once

    ref = str(event_id or "").strip()
    if not ref.isdigit():
        raise ValueError("イベント番号を指定してください")
    rooms = tuple(str(x) for x in (compare or []) if str(x).strip().isdigit())[:COMPARE_MAX]
    key = ("event_chart", str(state_dir), ref, str(room or ""), rooms, tuple(support or ()))
    return once.shared(key, lambda: _event_chart(state_dir, int(ref), room, [int(x) for x in rooms], support), ttl=CACHE)


def _name(s: Store, rid: int) -> tuple[str | None, str]:
    r = s.room(room_id=rid)
    if r is None:
        return None, str(rid)
    return r["key"], r["name"] or r["key"] or str(rid)


def _series(s: Store, eid: int, rid: int) -> list[tuple[float, int, int | None]]:
    return [(r[0], r[1] or 0, r[2]) for r in s.all(
        "SELECT at, point, rank FROM event_progress WHERE event_id=? AND room_id=? ORDER BY at", (eid, rid))]


def _snapshot_at(s: Store, eid: int, t: float) -> float | None:
    return s.scalar("SELECT MAX(at) FROM event_progress WHERE event_id=? AND at<=?", (eid, t), None)


def _snapshot(s: Store, eid: int, at: float | None) -> dict[int, tuple[int, int | None]]:
    if at is None:
        return {}
    return {r[0]: (r[1] or 0, r[2]) for r in s.all("SELECT room_id, point, rank FROM event_progress WHERE event_id=? AND at=?", (eid, at))}


def _jst_day(t: float) -> int:
    return int((t + JST) // 86400)


def _day_label(day: int) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(day * 86400))


def steps(series: list[tuple[float, int, int | None]]) -> list[dict[str, Any]]:
    """記録と記録のあいだの増え方 (1 時間足)。間が GAP より長い所は gap (その間のどこで増えたかは分からない)。"""
    out = []
    for (a, pa, _), (b, pb, _) in zip(series, series[1:]):
        dt = b - a
        if dt <= 0:
            continue
        gain = pb - pa
        out.append({"t0": a, "t1": b, "gain": gain, "rate": gain * 3600 / dt, "hours": dt / 3600, "gap": dt > GAP,
                    "drop": gain < 0})  # 減った (不正なポイントが引かれた・読み違い)。0 には直さずそのまま出す
    # 24 時間の移動平均 (その足までの 24 時間に入る足の、増えた合計 ÷ 時間)
    for i, x in enumerate(out):
        gain = hours = 0.0
        for y in reversed(out[: i + 1]):
            if x["t1"] - y["t0"] > BASE + 1:
                break
            gain += y["gain"]
            hours += y["hours"]
        x["ma24"] = gain / hours if hours else None
    return out


def buckets(st: list[dict[str, Any]], size: int) -> list[dict[str, Any]]:
    """6 時間足・日足: 足 (記録のあいだ) を、そのまん中の時刻の入る区切り (日本時間で揃える) にまとめる。
    区切りの中に観測の無い時間 (間が空いた足・区切りの頭や終わりの記録の無い所) があれば partial にして、見た時間を添える。"""
    out: dict[int, dict[str, Any]] = {}
    for x in st:
        k = int(((x["t0"] + x["t1"]) / 2 + JST) // size)
        b = out.setdefault(k, {"t0": k * size - JST, "t1": (k + 1) * size - JST, "gain": 0, "hours": 0.0, "gap": False})
        b["gain"] += x["gain"]
        b["hours"] += min(x["hours"], size / 3600)
        b["gap"] = b["gap"] or x["gap"]
    rows = [out[k] for k in sorted(out)]
    for b in rows:
        b["partial"] = b["gap"] or b["hours"] < size / 3600 * 0.8
        b["rate"] = b["gain"] / b["hours"] if b["hours"] else None
    return rows


def candles(series: list[tuple[float, int, int | None]]) -> list[dict[str, Any]]:
    """順位の日足 (日本時間)。高値 = いちばん良い (小さい) 順位、安値 = いちばん悪い順位。順位の無い記録は使わない。"""
    days: dict[int, list[int]] = {}
    for at, _, rank in series:
        if rank:
            days.setdefault(_jst_day(at), []).append(int(rank))
    return [{"day": _day_label(d), "open": r[0], "close": r[-1], "high": min(r), "low": max(r), "n": len(r)}
            for d, r in sorted(days.items())]


def _rate(a: tuple[int, float] | None, b: tuple[int, float] | None) -> float | None:
    """a → b の 1 時間あたりの増え方 (pt, 時刻)。どちらかの記録が無い・間が 1 時間に満たなければ None (0 とはしない)。"""
    if a is None or b is None or b[1] - a[1] < 3600:
        return None
    return (b[0] - a[0]) * 3600 / (b[1] - a[1])


def _momentum(now: tuple[int, float], before: tuple[int, float] | None,
              y0: tuple[int, float] | None, y1: tuple[int, float] | None) -> dict[str, Any]:
    """直近 (before → now) と、前の日の同じ時間帯 (y0 → y1) の 1 時間あたりの増え方と、その比 (前日比)。"""
    rate = _rate(before, now)
    base = _rate(y0, y1)
    return {"rate": rate, "gain": now[0] - before[0] if rate is not None and before else None,
            "base_rate": base, "ratio": rate / base if rate is not None and base else None}


def _event_chart(state_dir: Path, eid: int, room: Any, compare: list[int], support: list[str] | None) -> dict[str, Any]:
    now = time.time()
    with Store(state_dir) as s:
        e = s.one("SELECT event_id, name, start, end, thinned_at FROM events WHERE event_id=?", (eid,))
        if e is None:
            return {"event_id": eid, "found": False}
        last_at = s.scalar("SELECT MAX(at) FROM event_progress WHERE event_id=?", (eid,), None)
        base = {"event_id": eid, "found": True, "now": now, "start": e["start"], "end": e["end"], "gap": GAP,
                "thinned": bool(e["thinned_at"]), "ongoing": bool(e["end"] and e["end"] > now)}
        if last_at is None:
            return {**base, "room": None, "movers": []}
        latest = _snapshot(s, eid, last_at)
        # 今のポイントの多い順の位置 (順位が入っていない記録のため)
        order = sorted(latest, key=lambda r: -latest[r][0])
        place = {rid: i + 1 for i, rid in enumerate(order)}

        # 値上がり率の一覧 (直近 6 時間と、前の日の同じ時間帯)
        b_at = _snapshot_at(s, eid, last_at - RECENT)
        y1_at = _snapshot_at(s, eid, last_at - BASE)
        y0_at = _snapshot_at(s, eid, (b_at or last_at) - BASE)
        snaps = {at: _snapshot(s, eid, at) for at in {b_at, y0_at, y1_at} if at is not None}
        pick = lambda at, rid: (snaps[at][rid][0], at) if at is not None and rid in snaps[at] else None  # noqa: E731

        def momentum(rid: int) -> dict[str, Any]:
            return _momentum((latest[rid][0], last_at), pick(b_at, rid), pick(y0_at, rid), pick(y1_at, rid))

        movers = []
        for rid in order[:MOVERS]:
            pt, rank = latest[rid]
            m = momentum(rid)
            was = snaps[b_at].get(rid, (None, None))[1] if b_at else None
            movers.append({"room_id": rid, "point": pt, "rank": rank or place[rid], **m,
                           "rank_was": was, "rank_change": (was - (rank or place[rid])) if was and (rank or place[rid]) else None})
        for m in movers:
            m["key"], m["name"] = _name(s, m["room_id"])
        recent = {"from": b_at, "to": last_at, "base_from": y0_at, "base_to": y1_at}

        # 見るルーム: 指定 → 応援ルーム (参加していれば) → 1 位
        rid = None
        ref = str(room or "").strip()
        if ref.isdigit() and int(ref) in latest:
            rid = int(ref)
        elif ref:
            x = s.room(key=ref)
            rid = int(x["room_id"]) if x is not None and int(x["room_id"]) in latest else None
        if rid is None:
            for k in support or []:
                x = s.room(key=k)
                if x is not None and int(x["room_id"]) in latest:
                    rid = int(x["room_id"])
                    break
        if rid is None:
            rid = order[0]
        i = order.index(rid)
        above = order[i - 1] if i > 0 else None
        below = order[i + 1] if i + 1 < len(order) else None

        mine = _series(s, eid, rid)
        st = steps(mine)
        key, name = _name(s, rid)
        lines = [{"room_id": rid, "key": key, "name": name, "role": "target", "series": mine}]
        for role, x in (("above", above), ("below", below)):
            if x is not None:
                k, n = _name(s, x)
                lines.append({"room_id": x, "key": k, "name": n, "role": role, "series": _series(s, eid, x)})
        for x in compare:
            if x in latest and x not in {l["room_id"] for l in lines}:
                k, n = _name(s, x)
                lines.append({"room_id": x, "key": k, "name": n, "role": "compare", "series": _series(s, eid, x)})
        times = sorted({at for l in lines for at, _, _ in l["series"]})
        mine_at = {at: p for at, p, _ in mine}
        out_lines = []
        for l in lines:
            pts = {at: (p, r) for at, p, r in l["series"]}
            out_lines.append({k: v for k, v in l.items() if k != "series"} | {
                "points": [pts[t][0] if t in pts else None for t in times], "ranks": [pts[t][1] if t in pts else None for t in times],
                # 見るルームとの差 (見るルーム − このルーム)。どちらかの記録が無い時刻は None
                "diff": [mine_at[t] - pts[t][0] if t in pts and t in mine_at else None for t in times]})

        me = next(m for m in movers if m["room_id"] == rid) if rid in order[:MOVERS] else {
            "room_id": rid, "point": latest[rid][0], "rank": latest[rid][1] or place[rid], **momentum(rid)}
        stats: dict[str, Any] = {"point": latest[rid][0], "rank": latest[rid][1] or place[rid], "rate": me.get("rate"),
                                 "base_rate": me.get("base_rate"), "ratio": me.get("ratio"), "gain": me.get("gain"),
                                 "rank_change": me.get("rank_change"), "rooms": len(order), "at": last_at}
        if base["ongoing"] and e["end"]:
            left = max(0.0, (e["end"] - last_at) / 3600)
            stats["hours_left"] = left
            if me.get("rate") is not None:
                stats["projected"] = latest[rid][0] + me["rate"] * left  # 今のペースのまま (予測。終盤の追い上げは入らない)
            if above is not None and left > 0:
                up = next((m for m in movers if m["room_id"] == above), None)
                gap = latest[above][0] - latest[rid][0]
                stats["above_gap"] = gap
                if up is not None and up.get("rate") is not None:
                    stats["need_rate"] = up["rate"] + gap / left  # 上のルームが今のペースのままなら、抜くのに要る 1 時間あたり
        if below is not None:
            stats["below_gap"] = latest[rid][0] - latest[below][0]
        return {**base, "room": {"room_id": rid, "key": key, "name": name, "first_at": mine[0][0] if mine else None,
                                 "first_point": mine[0][1] if mine else None},
                "stats": stats, "bars": {"1h": st, "6h": buckets(st, 6 * 3600), "1d": buckets(st, 86400)},
                "candles": candles(mine), "times": times, "lines": out_lines, "movers": movers, "recent": recent}
