"""ギフター × ルーム × イベントの単位で、「次のイベントで、だれが、何 pt 出すか」の当て方をくらべる (読むだけ。写しで動かす)。

全体の見直し (2026-10-27、project の資料「全体見直し_2026-10-27」の 4 章の ③) で、M さんが「採点の物差しを先に」を選んだ。
今の採点 (backtest.py) は人ごとで、答えは「次の 30 日の、どれか 1 イベント × 1 ルームの最大」。どのルームに出すかは採点していない。
ここでは、ルームがイベントに出た 1 回ごとに、そのルームの前からのギフターが出すかと量を当てる。式 (画面) は変えない。

- 1 件 = ギフター × ルーム × イベント。予測の時点 T は、イベントが始まる 24 時間前 (backtest_event と同じ)
- **ルームがそのイベントに出ることは、分かっているとする** (予定既知仮定。Codex の 15.1。参加を知らずに当てたのではない)
- だれを当てるか: T までの 365 日に、そのルームの使える回 (下) で、貢献ランキングに載った人 (推測の名前の行は使わない)。
  初めて載った人 (新顔) は当てられないので、件数だけ数える
- 正解に使える回 (ルーム × イベント)。外れた回は理由ごとに数え、0 にはしない:
  - 貢献ランキングを、イベントが終わったあとに読んである (`ranking_at >= end`。開催中に読んだ途中の値は使わない)
  - 名前で推測した行 (Internet Archive) が無い・1 人以上いる (空の一覧は、取れたのか確かめられない。Codex の EMPTY_UNVERIFIED)
  - さらに、正式な結果のあと (次の日の 13:00 ごろ) に読んだ回を「確定のあと」として、別にも数える (終わっただけの回と同じにしない)
- 正解の分け方: 載った = その pt。載っていない + 載った人が 100 人より少ない = 0 (全員が載っているので)。
  載っていない + 100 人ちょうど = 「100 位の外」で量は分からない (0 にしない)。出てくるか (載ったか) の採点には「載っていない」として入れる
- 履歴にも同じ条件を当て、正式な結果が T より前に出ていた回だけを使う
- **遡及の比較**: 貢献ランキングは読み直すたびに上書きされ、いつ分かった値かが残らない。T の時に本当に知っていたかは確かめられない
- 開発の期間 (--dev) と確かめる期間 (--check) を分ける。分け目 (履歴の回数の区分) は前から決めたものだけを使う

方式 (どれも T までの履歴だけ。ギフター × ルーム):
- 出てくるか (AUC): 直前の回に載ったか・直近 90 日に載ったか・直近 180 日の「載った回 ÷ ルームの回」。
  分母は 2 通り: 使える回だけ (取れなかった回を「出てこなかった」に数えない) と、ルームが出た回すべて (今のステータスの出動率の数え方)
- 出てきた時の量 (載った人だけ): 最後に載った回の pt・直近 3 回の中ほど・2 番目に大きい回 (1 回だけならその回。ステータスの火力と同じ取り方)
- 次のイベントの量 (0 も入れる): 直前の回の pt (載っていなければ 0)・直近 90 日の 1 回あたり・出てくる割合 × 出てきた時の量 の組み合わせ

使い方: python -m showroom_keeper.backtest_pair <写しのあるフォルダ> [--dev 2026-04 ...] [--check 2026-07 ...] [--rooms 500] [--out FILE]
写しは読み取り専用で開く。サーバーでは動かさない。
"""

from __future__ import annotations

import argparse
import bisect
import json
import logging
import random
import sys
import time
from array import array
from pathlib import Path
from statistics import median
from typing import Any, Iterable

from . import backtest, backtest_event, research
from .gifter_analysis import _add
from .research_store import CONTRIB_FULL

log = logging.getLogger(__name__)

VERSION = 1
FILE = "backtest-pair.json"
DAY = 86400
LEAD = backtest_event.LEAD  # 予測の時点: イベントが始まるこれだけ前
YEAR = 365 * DAY  # だれを当てるか (この間に載った人)・2 番目に大きい回を見る日数
RATE_DAYS = 180 * DAY  # 出てくる割合を見る日数
RECENT_DAYS = 90 * DAY  # 「直近」の日数
TOP = 10  # ルームの上の何人を当てるか
HISTORY_BANDS = ((1, 1, "1 回"), (2, 3, "2〜3 回"), (4, 10**9, "4 回以上"))  # 載った回の数の区分 (前から決めたもの。結果を見て変えない)

APPEAR = {"naive_last": "何も考えない予測: 直前の回に載ったか", "flag90": "直近 90 日に載ったか",
          "rate180": "直近 180 日の 載った回 ÷ 使える回 (取れなかった回を分母に入れない)",
          "rate180_old": "直近 180 日の 載った回 ÷ ルームが出た回すべて (今の出動率の数え方)",
          "rate90": "直近 90 日の 載った回 ÷ 使える回"}
CAME = {"naive_last_seen": "何も考えない予測: 最後に載った回の pt", "med3": "直近 3 回 (載った回) の中ほど",
        "second": "365 日で 2 番目に大きい回 (1 回だけならその回。ステータスの火力の取り方)"}
EXPECTED = {"naive_last": "何も考えない予測: 直前の回の pt (載っていなければ 0)",
            "naive_90": "何も考えない予測: 直近 90 日の合計 ÷ その間の使える回",
            "med3_flag90": "直近 3 回の中ほど × 直近 90 日に載ったか (10-27 の候補)",
            "med3_rate180": "直近 3 回の中ほど × 直近 180 日の出てくる割合",
            "med3_rate180_old": "直近 3 回の中ほど × 出てくる割合 (分母はルームが出た回すべて)",
            "med90_rate90": "直近 90 日の中ほど × 直近 90 日の出てくる割合 (Gemini の案)",
            "second_rate180": "2 番目に大きい回 × 直近 180 日の出てくる割合"}
HISTORY_KEYS = ("naive_last", "naive_90", "med3_rate180")  # 履歴の回数ごと・確定のあとだけ でも見る方式
STRICT_APPEAR = ("naive_last", "rate180")

# ルームが出たイベント (正解・履歴の両方に使う)。room_events の主キー (room_id, event_id) で引く
ROOM_EVENTS_SQL = f"""SELECT r.event_id, r.point, r.ranking_at, e.start, e.end, e.show_ranking, {research.CONFIRMED_SQL} AS confirmed_at
                      FROM room_events r JOIN events e ON e.event_id = r.event_id
                      WHERE r.room_id=? AND e.end IS NOT NULL AND e.end >= ? AND e.end <= ?"""
# 1 回分の貢献ランキング (主キー (event_id, room_id, user_id) で引く)
ROWS_SQL = "SELECT user_id, point, guessed FROM contributions WHERE event_id=? AND room_id=?"


def _second(values: list[float]) -> float:
    v = sorted(values, reverse=True)
    return v[1] if len(v) > 1 else v[0]


class _Round:
    """ルームの 1 回 (イベント)。usable = 正解・履歴に使える。rows = 載った人 → pt。"""

    __slots__ = ("event_id", "start", "end", "confirmed_at", "ran", "why", "confirmed", "rows", "full")

    def __init__(self, r: Any, rows: list[Any] | None) -> None:
        self.event_id, self.start, self.end = r["event_id"], float(r["start"] or r["end"]), float(r["end"])
        self.confirmed_at = float(r["confirmed_at"])
        self.ran = bool((r["point"] or 0) > 0)  # ルームがポイントを集めた (出た) 回
        self.rows: dict[int, float] = {}
        self.full = False
        self.confirmed = bool(r["ranking_at"] is not None and r["ranking_at"] >= self.confirmed_at)
        if not r["show_ranking"]:
            self.why = "no_ranking"
        elif r["ranking_at"] is None or r["ranking_at"] < self.end:
            self.why = "not_read_after_end"
        elif rows is None or not rows:
            self.why = "empty_unverified"
        elif any(x["guessed"] for x in rows):
            self.why = "guessed"
        else:
            self.why = "usable"
            self.rows = {int(x["user_id"]): float(x["point"] or 0) for x in rows}
            self.full = len(self.rows) >= CONTRIB_FULL

    @property
    def usable(self) -> bool:
        return self.why == "usable"


def load_room(conn: Any, room_id: int, lo: float, hi: float) -> list[_Round]:
    """ルームの、終わりが lo〜hi の回 (古い順)。貢献ランキングは、読み直してある回だけ読む。"""
    out = []
    for r in conn.execute(ROOM_EVENTS_SQL, (room_id, lo, hi)).fetchall():
        rows = None
        if r["show_ranking"] and r["ranking_at"] is not None and r["ranking_at"] >= r["end"]:
            rows = conn.execute(ROWS_SQL, (r["event_id"], room_id)).fetchall()
        out.append(_Round(r, rows))
    return sorted(out, key=lambda x: (x.end, x.event_id))


def features(seen: list[tuple[float, float]], last: float, n: dict[str, int], at: float) -> dict[str, Any]:
    """1 人の T までの履歴から、各方式の値。seen = 載った回の (終わり, pt) (新しい順)、last = 直前の使える回の pt (載っていなければ 0)、
    n = ルームの回の数 (use180・use90 = 使える回、ran180 = ルームが出た回すべて)。"""
    pts = [p for _, p in seen]
    in90 = [p for end, p in seen if end > at - RECENT_DAYS]
    got180 = sum(1 for end, _ in seen if end > at - RATE_DAYS)
    rate180 = got180 / n["use180"] if n["use180"] else 0.0
    rate180_old = got180 / n["ran180"] if n["ran180"] else 0.0
    rate90 = len(in90) / n["use90"] if n["use90"] else 0.0
    med3, second = median(pts[:3]), _second(pts)
    med90 = median(in90) if in90 else 0.0
    flag90 = 1.0 if in90 else 0.0
    return {
        "rounds": len(pts),
        "appear": {"naive_last": 1.0 if last > 0 else 0.0, "flag90": flag90, "rate180": rate180, "rate180_old": rate180_old, "rate90": rate90},
        "came": {"naive_last_seen": pts[0], "med3": med3, "second": second},
        "expected": {"naive_last": last, "naive_90": sum(in90) / n["use90"] if n["use90"] else 0.0,
                     "med3_flag90": med3 * flag90, "med3_rate180": med3 * rate180, "med3_rate180_old": med3 * rate180_old,
                     "med90_rate90": med90 * rate90, "second_rate180": second * rate180},
        # 量の幅 (載った直近 6 回)。3 回以上で最小〜最大、4 回以上で下から 4 分の 1〜上から 4 分の 1
        "band": pts[:6],
    }


def _band_of(rounds: int) -> str:
    return next(name for lo, hi, name in HISTORY_BANDS if lo <= rounds <= hi)


class _Err:
    """量の誤差 (予測 − 実際) を貯める。偏りは平均で、正なら大きく出ている。"""

    __slots__ = ("diff", "ratio")

    def __init__(self) -> None:
        self.diff: array[float] = array("d")
        self.ratio: array[float] = array("d")

    def add(self, pred: float, actual: float) -> None:
        self.diff.append(pred - actual)
        if pred > 0:
            self.ratio.append(actual / pred)

    def result(self) -> dict[str, Any]:
        if not self.diff:
            return {"n": 0}
        r = self.ratio
        return {"n": len(self.diff), "mae": round(sum(abs(x) for x in self.diff) / len(self.diff)),
                "median_abs": round(median(abs(x) for x in self.diff)), "bias": round(sum(self.diff) / len(self.diff)),
                "actual_over_pred_median": round(median(r), 3) if r else None,
                # 補助: 実際が予測の 0.5〜2 倍に入った割合 (予測が正のものだけ。MAE の代わりにはしない)
                "within_half_to_double": round(sum(1 for x in r if 0.5 <= x <= 2) / len(r), 3) if r else None}


class _Auc:
    """出てくるかの並べ方。載った人と載らなかった人を 1 人ずつ組にして、載った人の値の方が大きかった割合 (同じ値は半分)。正答率ではない。"""

    __slots__ = ("pos", "neg")

    def __init__(self) -> None:
        self.pos: array[float] = array("d")
        self.neg: array[float] = array("d")

    def add(self, pred: float, listed: bool) -> None:
        (self.pos if listed else self.neg).append(pred)

    def result(self) -> dict[str, Any]:
        neg = sorted(self.neg)
        agree = 0.0
        for v in self.pos:
            lo, hi = bisect.bisect_left(neg, v), bisect.bisect_right(neg, v)
            agree += lo + (hi - lo) / 2
        total = len(self.pos) * len(neg)
        return {"auc": round(agree / total, 3) if total else None, "pairs": total}


class Tally:
    """1 つの期間 (または 1 か月) の採点。ケースは貯めずに、その場で数える (数百万件になるため)。"""

    def __init__(self) -> None:
        self.n = {"cases": 0, "room_events": 0, "listed": 0, "true_zero": 0, "outside_top100": 0, "newcomers": 0, "listed_all": 0,
                  "confirmed_cases": 0}
        self.dropped: dict[str, int] = {}
        self.appear = {k: _Auc() for k in APPEAR}
        self.came = {k: _Err() for k in CAME}
        self.expected = {k: _Err() for k in EXPECTED}
        self.order = {k: [0, 0] for k in EXPECTED}
        self.top = {k: [0, 0] for k in EXPECTED}
        self.band = {"min_max6": [0, 0, 0, 0], "quartiles6": [0, 0, 0, 0]}  # 件数・入った・下に外れた・上に外れた
        self.within = [0, 0]
        self.history = {name: {k: _Err() for k in HISTORY_KEYS} for _, _, name in HISTORY_BANDS}
        self.strict_appear = {k: _Auc() for k in STRICT_APPEAR}
        self.strict_expected = {k: _Err() for k in HISTORY_KEYS}

    light_only = False

    @classmethod
    def light(cls) -> "Tally":
        """件数と、使えなかった理由だけを数えるもの。"""
        t = cls()
        t.light_only = True
        return t

    def drop(self, why: str) -> None:
        self.dropped[why] = self.dropped.get(why, 0) + 1

    def add(self, f: dict[str, Any], listed: bool, amount: float | None, confirmed: bool) -> None:
        n = self.n
        n["cases"] += 1
        n["listed"] += listed
        n["true_zero"] += amount == 0
        n["outside_top100"] += amount is None
        n["confirmed_cases"] += confirmed
        if self.light_only:
            return
        for k, v in f["appear"].items():
            self.appear[k].add(v, listed)
        if confirmed:
            for k in STRICT_APPEAR:
                self.strict_appear[k].add(f["appear"][k], listed)
        if amount is None:
            return
        band = self.history[_band_of(f["rounds"])]
        for k, v in f["expected"].items():
            self.expected[k].add(v, amount)
            if k in band:
                band[k].add(v, amount)
                if confirmed:
                    self.strict_expected[k].add(v, amount)
        if not listed:
            return
        for k, v in f["came"].items():
            self.came[k].add(v, amount)
        xs = f["band"]
        for name, need in (("min_max6", 3), ("quartiles6", 4)):
            if len(xs) < need:
                continue
            ys = sorted(xs)
            lo, hi = (ys[0], ys[-1]) if name == "min_max6" else (ys[len(ys) // 4], ys[(3 * len(ys)) // 4])
            c = self.band[name]
            c[0] += 1
            c[1] += lo <= amount <= hi
            c[2] += amount < lo
            c[3] += amount > hi
        if f["came"]["med3"] > 0:
            self.within[0] += backtest.within(amount, f["came"]["med3"])
            self.within[1] += 1

    def add_group(self, rows: list[tuple[int, dict[str, Any], float | None]], actual: dict[int, float], newcomers: int) -> None:
        """1 つのルーム × イベント: ルームの中の 2 人の並びと、ルームの上の人を当てたか。"""
        self.n["room_events"] += 1
        self.n["newcomers"] += newcomers
        self.n["listed_all"] += len(actual)
        if self.light_only:
            return
        known = [(u, f, a) for u, f, a in rows if a is not None]
        k_top = min(TOP, len(actual))
        for k in EXPECTED:
            a, t = backtest.pairs_agree({u: f["expected"][k] for u, f, _ in known}, {u: x for u, _, x in known})
            self.order[k][0] += a
            self.order[k][1] += t
            if k_top:  # ルームの上の k_top 人 (新顔も入れた実際) のうち、予測の上の k_top 人に入っていた人
                self.top[k][0] += backtest.top_overlap({u: f["expected"][k] for u, f, _ in rows}, actual, k_top)
                self.top[k][1] += k_top

    def result(self) -> dict[str, Any]:
        def rate(x: list[int]) -> float | None:
            return round(x[0] / x[1], 3) if x[1] else None
        if self.light_only:
            return {**self.n, "label_dropped": self.dropped}
        return {
            **self.n, "label_dropped": self.dropped,
            "appear": {k: v.result() for k, v in self.appear.items()},
            "came": {k: v.result() for k, v in self.came.items()},
            "expected": {k: v.result() for k, v in self.expected.items()},
            "order_in_room": {k: {"rate": rate(v), "pairs": v[1]} for k, v in self.order.items()},
            "top": {k: {"rate": rate(v), "slots": v[1]} for k, v in self.top.items()},
            "band": {**{name: {"n": c[0], **({"inside": round(c[1] / c[0], 3), "below": round(c[2] / c[0], 3), "above": round(c[3] / c[0], 3)}
                                            if c[0] else {})} for name, c in self.band.items()},
                     "med3_half_to_double": {"n": self.within[1], "rate": rate(self.within)}},
            "by_history": {name: {k: v.result() for k, v in errs.items()} for name, errs in self.history.items()},
            "confirmed_only": {"appear": {k: v.result() for k, v in self.strict_appear.items()},
                               "expected": {k: v.result() for k, v in self.strict_expected.items()}},
        }


def score_room(rounds: list[_Round], targets: list[int], tallies: list[Tally]) -> None:
    """1 ルームの、目当てのイベントごとに、前からのギフターの予測を作って tallies に数える。"""
    by_id = {x.event_id: x for x in rounds}
    for eid in targets:
        e = by_id.get(eid)
        if e is None:
            continue
        if not e.usable:
            for t in tallies:
                t.drop(e.why)
            continue
        at = e.start - LEAD
        hist = [x for x in reversed(rounds) if x.usable and x.event_id != eid and x.end <= at and x.confirmed_at <= at and x.end > at - YEAR]
        ran = [x for x in reversed(rounds) if x.ran and x.event_id != eid and x.end <= at and x.end > at - YEAR]
        seen: dict[int, list[tuple[float, float]]] = {}
        for x in hist:  # 新しい順なので、1 人ずつの載った回も新しい順になる
            for uid, pt in x.rows.items():
                seen.setdefault(uid, []).append((x.end, pt))
        if not seen:
            for t in tallies:
                t.drop("no_history")
            continue
        n = {"use180": sum(1 for x in hist if x.end > at - RATE_DAYS), "use90": sum(1 for x in hist if x.end > at - RECENT_DAYS),
             "ran180": sum(1 for x in ran if x.end > at - RATE_DAYS)}
        rows = []
        for uid in sorted(seen):
            f = features(seen[uid], hist[0].rows.get(uid, 0.0), n, at)
            listed = uid in e.rows
            amount = e.rows[uid] if listed else (None if e.full else 0.0)  # 100 位の外は量が分からない
            rows.append((uid, f, amount))
            for t in tallies:
                t.add(f, listed, amount, e.confirmed)
        newcomers = sum(1 for u in e.rows if u not in seen)
        for t in tallies:
            t.add_group(rows, e.rows, newcomers)


def targets_in(conn: Any, month: str) -> list[tuple[int, int]]:
    """month (JST) に始まったイベントの (ルーム, イベント)。ルームがポイントを集めた回だけ。"""
    t0, t1 = backtest.month_start(month), backtest.month_start(_add(month, 1))
    out = []
    for e in conn.execute("SELECT event_id FROM events INDEXED BY events_start WHERE start >= ? AND start < ? ORDER BY start, event_id",
                          (t0, t1)).fetchall():
        for r in conn.execute("SELECT room_id FROM room_events INDEXED BY room_events_event WHERE event_id=? AND point > 0",
                              (e["event_id"],)).fetchall():
            out.append((int(r["room_id"]), int(e["event_id"])))
    return out


def run(state_dir: Path, dev: list[str], check: list[str], rooms: int | None = None, seed: int = 1) -> dict[str, Any]:
    started = time.time()
    months = dev + check
    period = {**{m: "dev" for m in dev}, **{m: "check" for m in check}}
    tallies = {"dev": Tally(), "check": Tally()}
    per_month = {m: Tally.light() for m in months}  # 月ごとは件数だけ (全部を月ごとにも貯めると、メモリが倍になる)
    conn = backtest_event.connect(state_dir)
    try:
        want: dict[int, list[tuple[str, int]]] = {}
        for m in months:
            for rid, eid in targets_in(conn, m):
                want.setdefault(rid, []).append((m, eid))
        all_rooms = sorted(want)
        if rooms and len(all_rooms) > rooms:
            all_rooms = sorted(random.Random(seed).sample(all_rooms, rooms))
        lo = backtest.month_start(min(months)) - LEAD - YEAR
        hi = backtest.month_start(_add(max(months), 2))  # 月の終わりに始まった回が終わるまで
        for k, rid in enumerate(all_rooms):
            rounds = load_room(conn, rid, lo, hi)
            for m in months:
                score_room(rounds, [e for mm, e in want[rid] if mm == m], [tallies[period[m]], per_month[m]])
            if k % 500 == 0:
                log.info("ギフター × ルームの採点: %d / %d ルーム", k, len(all_rooms))
    finally:
        conn.close()
    out: dict[str, Any] = {
        "version": VERSION, "at": time.time(),
        "kind": "遡及の参考比較 (今の記録で過去を振り返った。当時の情報での予測ではない)。ルームがそのイベントに出ることは分かっているとした (予定既知仮定)",
        "label": "終わったあとに読んだ貢献ランキング (推測の行が無く、1 人以上)。載っていない人は、100 人に届かない回だけ 0、100 人の回は量が分からない (出てくるかの採点には入れる)",
        "dev": dev, "check": check, "rooms": len(all_rooms), "rooms_all": len(want), "rooms_sampled": rooms, "lead_hours": LEAD / 3600,
        "methods": {"appear": APPEAR, "came": CAME, "expected": EXPECTED},
        "periods": {label: {"all": tallies[label].result(), "months": {m: per_month[m].result() for m in ms}}
                    for label, ms in (("dev", dev), ("check", check))}}
    out["seconds"] = round(time.time() - started)
    return out


def _print(out: dict[str, Any]) -> None:
    for label in ("dev", "check"):
        d = out["periods"][label]["all"]
        print(f"[{label}] ケース {d['cases']:,} ・ ルーム × イベント {d['room_events']:,} ・ 載った {d['listed']:,} ・ 新顔 {d['newcomers']:,} / {d['listed_all']:,}")
        print("  出てくるか (AUC): " + " / ".join(f"{k} {v['auc']}" for k, v in d["appear"].items()))
        print("  出てきた時の量 (MAE): " + " / ".join(f"{k} {v.get('mae')}" for k, v in d["came"].items()))
        print("  次のイベントの量 (MAE): " + " / ".join(f"{k} {v.get('mae')}" for k, v in d["expected"].items()))
        print("  ルームの中の並び: " + " / ".join(f"{k} {v['rate']}" for k, v in d["order_in_room"].items()))
        print("  ルームの上の 10 人: " + " / ".join(f"{k} {v['rate']}" for k, v in d["top"].items()))


def main(argv: Iterable[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="ギフター × ルームの、次のイベントの量の当て方をくらべる (写しで動かす。読むだけ)")
    p.add_argument("state_dir")
    p.add_argument("--dev", action="append", help="開発の期間 (イベントの始まった月。既定 2026-04〜06)")
    p.add_argument("--check", action="append", help="確かめる期間 (既定 2026-07・08)")
    p.add_argument("--rooms", type=int, default=None, help="ルームを何部屋まで選ぶか (省くと全部)")
    p.add_argument("--out", default=None)
    a = p.parse_args(list(argv) if argv is not None else None)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    out = run(Path(a.state_dir), a.dev or ["2026-04", "2026-05", "2026-06"], a.check or ["2026-07", "2026-08"], a.rooms)
    path = Path(a.out) if a.out else Path(a.state_dir) / FILE
    path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    _print(out)
    print(f"結果: {path} ({out['seconds']} 秒)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
