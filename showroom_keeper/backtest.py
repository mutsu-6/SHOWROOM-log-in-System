"""予測の答え合わせ (採点)。

画面の予測が、過去にどれだけ当たったかを数える (project の資料「ギフター分析の精度_2026-10-24」の 6 章。M さんが 2026-10-24 に
「Claude Code が作る」を選んだ。再設計の決定案の 12: 採点はサーバーの外で行う)。

やり方: research.db の**写し**を開き、過去の時点 T (月の初め) より後に終わったイベントの記録を、その接続の中だけで見えなくする
(同じ名前の一時のビュー・表で隠す。写しのファイルの中身は書き換えない)。そこで画面と同じ関数で予測を作り、
隠していない接続で読んだ T のあとの実際とくらべる。

- サーバーでは動かさない (全員分を作り直すので重い)。Cursor が朝 4:00 の写しを手元に持ってきて動かし、
  できた backtest.json をサーバーの state のフォルダに置く。ページはそれを読むだけ
- 隠すのは「T より後に終わったイベント」の貢献ランキング・ルームの結果・ファンの名簿の T の月から先。
  T より前に終わったイベントで、記録を取ったのが T より後のもの (さかのぼって集めた分) は見せる (式の当たり方を見るため。集める速さは見ない)
- T の時に開催中だったイベントは、途中の値が分からないので、全部隠す (少なめに見る方に外れる)
- どの予測も、同じ線で「何も考えない予測」(直前の 1 回と同じ・先月と同じ など。NAIVE) も採点する。式がこれに勝てなければ、式を使う意味が無い
- 当たったかの線はここに書いたものだけ。画面の予測の言葉 (「次の勝負で動かせる量」など) から、いちばん素直な読み方で決めた
  使い方: python -m showroom_keeper.backtest <写しのあるフォルダ> [--at 2026-05 --at 2026-06] [--sample 2000] [--out backtest.json]
"""

from __future__ import annotations

import argparse
import bisect
import json
import logging
import math
import random
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from statistics import median
from typing import Any, Callable, Iterable

from . import event_analysis, gifter_analysis, gifter_status, heat, room_analysis
from .gifter_analysis import _add
from .research_store import MONTH_SQL, Store
from .status import JST

log = logging.getLogger(__name__)

VERSION = 2  # 採点の線を変えたら上げる (画面は版の違う結果を「前の線」と書く)
FILE = "backtest.json"  # state のフォルダに置く結果
DAY = 86400
NEAR = 30 * DAY  # 「次の勝負」「次のイベント」を見る日数
ROOM_DAYS = room_analysis.MOMENTUM_DAYS * DAY  # ルームの勢いの、次の期間
RATIO = 2.0  # 量の予測は、実際がこの倍の内 (半分〜2 倍) なら当たり
MIN_N = 20  # これより少ない件数の採点は、画面で「件数が少ない」と書く

# 各予測の名前と、画面で何を言っているか (結果の JSON の label・claim になる)
PREDICTIONS: dict[str, tuple[str, str]] = {
    "status_fire": ("ステータスの火力", "次の 30 日に、1 イベント × 1 ルームで出す量 (半分〜2 倍なら当たり)"),
    "status_overall": ("ステータスの総合力", "次の 30 日の勝負の回で、1 人で出す量 (半分〜2 倍なら当たり)"),
    "status_order": ("総合力の並び", "総合力の多い人ほど、次の 30 日に多く出すか (2 人を選んだ時に、多い方が当たる割合。でたらめなら 50%)"),
    "status_state": ("ステータスの状態", "上り調子・安定・温存中・枯渇・離脱が、次の 3 か月に言ったとおりになるか"),
    "power_forecast": ("戦力の今月の予想", "その月の量 (半分〜2 倍なら当たり)"),
    "power_upper": ("戦力の「大きな勝負なら」", "その月の 1 イベントの量が、これを超えないか"),
    "psyche_range": ("心のカルテの見込みの幅", "次の 90 日の推しのルームの 1 イベントの量が、低い〜高いの間に入るか"),
    "psyche_appear": ("心のカルテの出てくる見込み", "高い・ほぼ無い と言った人が、次の 90 日に推しのルームに出したか"),
    "psyche_phase": ("心のカルテの時期", "熱中・冷却中・潜伏の疑い などが、次の 3 か月に言ったとおりになるか"),
    "room_power": ("ルームの地力", "同じイベントの 2 ルームで、地力の大きい方が多く取るか (でたらめなら 50%)"),
    "room_power_size": ("地力の大きさ", "次の 30 日のいちばん大きいイベントのポイントが、地力の半分〜2 倍か"),
    "room_momentum": ("ルームの勢い", "上り調子・下り気味・変わらない が、次の 60 日も続くか"),
    "event_strength": ("イベントの見込みの順位", "同じイベントの 2 ルームで、強さの大きい方が多く取るか (でたらめなら 50%)"),
    "event_projected": ("イベントの最後の見込み", "まだ採点していない (期間の途中の時点ごとに戻す必要がある)"),
}


# ------------------------------------------------------------------ 時点 T までに見えていたものだけにする
def month_start(month: str) -> float:
    return datetime.strptime(month + "-01", "%Y-%m-%d").replace(tzinfo=JST).timestamp()


def hide_after(s: Store, at: float) -> None:
    """この接続の中だけ、at より後に終わったイベントの記録を隠す (一時のビュー・表は main の同じ名前より先に読まれる)。

    写しのファイルは書き換えない (一時のものは接続を閉じると消える)。ステータスの作り直しで書く表 (event_rounds など) も、
    一時の表に書かせる。"""
    ym = datetime.fromtimestamp(at, JST).strftime("%Y%m")
    seen = f"SELECT event_id FROM main.events WHERE end IS NOT NULL AND end <= {float(at)!r}"
    c = s.conn
    c.execute(f"CREATE TEMP VIEW contributions AS SELECT * FROM main.contributions WHERE event_id IN ({seen})")
    c.execute(f"CREATE TEMP VIEW room_events AS SELECT * FROM main.room_events WHERE event_id IN ({seen})")
    c.execute(f"CREATE TEMP VIEW active_fans AS SELECT * FROM main.active_fans WHERE ym < '{ym}'")
    c.execute(f"CREATE TEMP VIEW room_fans AS SELECT * FROM main.room_fans WHERE ym < '{ym}'")
    c.execute("CREATE TEMP TABLE user_month (user_id INTEGER, month TEXT, points INTEGER, PRIMARY KEY (user_id, month)) WITHOUT ROWID")
    c.execute(f"""INSERT INTO temp.user_month SELECT o.user_id, {MONTH_SQL} AS m, SUM(o.point)
                  FROM main.contributions o JOIN main.events e USING(event_id) WHERE e.end IS NOT NULL AND e.end <= ?
                  GROUP BY o.user_id, m""", (at,))
    c.execute("CREATE INDEX temp.user_month_points ON user_month(month, points)")
    for name in ("event_rounds", "event_rounds_done", "gifter_status"):
        sql = c.execute("SELECT sql FROM main.sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
        c.execute(sql[0].replace("CREATE TABLE", "CREATE TEMP TABLE", 1))
    for (sql,) in c.execute("SELECT sql FROM main.sqlite_master WHERE type='index' AND tbl_name IN ('event_rounds', 'gifter_status') AND sql IS NOT NULL").fetchall():
        c.execute(re.sub(r"^CREATE (UNIQUE )?INDEX (IF NOT EXISTS )?", lambda m: f"CREATE {m[1] or ''}INDEX {m[2] or ''}temp.", sql))
    gifter_analysis._cuts.clear()  # 月の区切りの覚えは、隠す前の値なので使わない


# ------------------------------------------------------------------ 数え方
class Score:
    """1 つの予測の採点 (当たり・外れの数と、種類ごとの内訳)。"""

    def __init__(self) -> None:
        self.n = 0
        self.hit = 0
        self.ratios: list[float] = []
        self.by: dict[str, list[int]] = {}  # 種類 → [件数, 当たり]
        self.base: dict[str, list[int]] = {}  # 種類 → [同じ線を全員に当てはめた時に当たった数, 全員の数] (くらべる基準)
        self.naive = [0, 0]  # 何も考えない予測の [当たり, 件数]

    def add_naive(self, ok: bool) -> None:
        self.naive[0] += bool(ok)
        self.naive[1] += 1

    def add(self, ok: bool, kind: str | None = None, ratio: float | None = None) -> None:
        self.n += 1
        self.hit += bool(ok)
        if ratio is not None and ratio > 0:
            self.ratios.append(ratio)
        if kind is not None:
            b = self.by.setdefault(kind, [0, 0])
            b[0] += 1
            b[1] += bool(ok)

    def result(self) -> dict[str, Any]:
        out: dict[str, Any] = {"n": self.n, "hit": self.hit, "rate": round(self.hit / self.n, 3) if self.n else None}
        if self.naive[1]:
            out["naive"] = {"n": self.naive[1], "hit": self.naive[0], "rate": round(self.naive[0] / self.naive[1], 3)}
        if self.ratios:
            out["ratio_median"] = round(median(self.ratios), 3)  # 実際 ÷ 予測 の真ん中 (1 より大きければ、予測は少なめ)
        if self.by:
            out["by"] = {k: {"n": v[0], "hit": v[1], "rate": round(v[1] / v[0], 3) if v[0] else None,
                             **({"base": round(self.base[k][0] / self.base[k][1], 3)} if self.base.get(k, [0, 0])[1] else {})}
                         for k, v in sorted(self.by.items())}
        return out


def within(actual: float, predicted: float, ratio: float = RATIO) -> bool:
    return predicted > 0 and predicted / ratio <= actual <= predicted * ratio


def pairs_agree(pred: dict[int, float], actual: dict[int, float]) -> tuple[int, int]:
    """2 つずつくらべて、予測の大きい方が実際も大きかった数と、くらべた数 (どちらかが同じ値の組は数えない)。"""
    ids = [i for i in pred if i in actual]
    agree = total = 0
    for k, a in enumerate(ids):
        for b in ids[k + 1:]:
            if pred[a] == pred[b] or actual[a] == actual[b]:
                continue
            total += 1
            agree += (pred[a] > pred[b]) == (actual[a] > actual[b])
    return agree, total


# 何も考えない予測 (くらべる相手。式がこれより当たらなければ、式を入れている意味が無い)
NAIVE: dict[str, str] = {
    "status_fire": "直前の 1 回 (1 イベント × 1 ルーム) と同じ量",
    "status_overall": "直前の 1 回と同じ量",
    "status_order": "先月の量の多い順",
    "power_forecast": "先月と同じ量",
    "psyche_range": "推しのルームへの直前の 1 回の半分〜2 倍",
    "psyche_appear": "直前の 90 日に推しのルームに出していれば、また出す",
    "room_power": "直前のイベントのポイントの多い順",
    "room_power_size": "直前のイベントのポイントと同じ",
    "event_strength": "直前のイベントのポイントの多い順",
}


# 状態・時期が言っていること (次の 3 か月の量 next3 と、言った時の直近 3 か月 recent3・ふだんの月 usual から)
STATE_CLAIMS: dict[str, Callable[[float, float, float], bool]] = {
    "上り調子": lambda nxt, recent, usual: recent > 0 and nxt >= recent,
    "安定": lambda nxt, recent, usual: recent > 0 and recent / RATIO <= nxt <= recent * RATIO,
    "温存中": lambda nxt, recent, usual: usual > 0 and nxt >= usual,  # 3 か月のうちに、ふだんの 1 か月分は出す
    "枯渇": lambda nxt, recent, usual: nxt <= 0.1 * usual,
    "離脱": lambda nxt, recent, usual: nxt <= 0.1 * usual,
}
PHASE_CLAIMS: dict[str, Callable[[float, float, float], bool]] = {
    "熱中": STATE_CLAIMS["上り調子"],
    "冷却中": lambda nxt, recent, usual: recent > 0 and nxt < recent,
    "安定": STATE_CLAIMS["安定"],
    "離脱": STATE_CLAIMS["離脱"],
    "休止": lambda nxt, recent, usual: nxt < usual,
    "燃え尽きのあと": lambda nxt, recent, usual: nxt < usual,
    "潜伏の疑い": lambda nxt, recent, usual: usual > 0 and nxt >= 2 * usual,  # 貯めた分をまとめて出す
    "休止 (お金の事情か潜伏)": lambda nxt, recent, usual: usual > 0 and nxt >= usual,
    "復帰": lambda nxt, recent, usual: usual > 0 and nxt >= usual,
}
# ルームの勢い (nxt = 次の 60 日 ÷ 言った時の前の 60 日)
MOMENTUM_CLAIMS: dict[str, Callable[[float, float, float], bool]] = {
    "上り調子": lambda nxt, _r, _u: nxt > 1,
    "下り気味": lambda nxt, _r, _u: nxt < 1,
    "変わらない": lambda nxt, _r, _u: 1 / room_analysis.MOMENTUM_STEP <= nxt <= room_analysis.MOMENTUM_STEP,
}
APPEAR_CLAIMS = {"高い": True, "ほぼ無い": False, "低い": False}


# ------------------------------------------------------------------ 見込み量 (総合力) の式の候補 (2026-10-26)
# 利用者の「1 イベントだけ爆投げしたギフターが 1 位になっている」から。今の総合力は「出てきたら」の量で、出てくるかを入れていない。
# 「出てくる割合 × 出た時の量」の候補を、今の式・何も考えない予測と、同じ人・同じ時点でくらべる (画面の式は変えない。勝った時だけ入れる)。
# どれも時点 T までに見えていたものだけで作る。出た時の量は、ステータスの火力 (1 回だけの人はその 1 回) をそのまま使う
CAND_MONTHS = 6  # 出てくる割合を見る月数 (集め終わっていない月は数えない)
CAND_RECENT_DAYS = 60  # 最後に見えてからの日数で、出てくる割合を減らす目安 (この日数で約 37% に)
CANDIDATES: dict[str, str] = {
    "current": "今の総合力 (火力と残弾の小さい方 × 状態 × 腕)",
    "naive_month": "何も考えない予測: 先月の量",
    "naive_90": "何も考えない予測: 最近 90 日の量",
    "freq": "候補: 直近 6 か月で送った月の割合 × 火力",
    "freq_recent": "候補: 送った月の割合 × 最後に見えてからの日数で減らす × 火力",
    "freq_recent_budget": "候補: 送った月の割合 × 日数で減らす × 火力と月の予算の小さい方",
}


def candidates(st: dict[str, Any], last_month_pt: float, recent90: float, share: float | None, days_since: float | None) -> dict[str, float]:
    """1 人の候補の値。share は送った月の割合 (分からなければ None)、days_since は最後に見えてからの日数。"""
    fire = float(st.get("fire") or 0)
    hp = float(st.get("hp") or 0)
    p = share or 0.0
    fade = math.exp(-days_since / CAND_RECENT_DAYS) if days_since is not None else 0.0
    return {"current": float(st.get("overall") or 0), "naive_month": float(last_month_pt), "naive_90": float(recent90),
            "freq": p * fire, "freq_recent": p * fade * fire, "freq_recent_budget": p * fade * min(fire, hp or fire)}


def auc_counts(pred: dict[int, float], came: dict[int, bool]) -> tuple[float, int]:
    """出てきた人と出てこなかった人を 1 人ずつ組にして、出てきた人の値の方が大きかった数 (同じ値は半分) と、組の数。"""
    pos = sorted(pred[u] for u in pred if came.get(u))
    neg = sorted(pred[u] for u in pred if u in came and not came[u])
    agree = 0.0
    for v in pos:
        lo, hi = bisect.bisect_left(neg, v), bisect.bisect_right(neg, v)
        agree += lo + (hi - lo) / 2
    return agree, len(pos) * len(neg)


def top_overlap(pred: dict[int, float], actual: dict[int, float], k: int) -> int:
    """予測の上から k 人のうち、実際も上から k 人に入った人の数 (同じ値は番号の小さい方を上にする)。"""
    top_p = {u for u, _ in sorted(pred.items(), key=lambda x: (-x[1], x[0]))[:k]}
    top_a = {u for u, v in sorted(actual.items(), key=lambda x: (-x[1], x[0]))[:k] if v > 0}
    return len(top_p & top_a)


class Cands:
    """候補ごとの採点 (時点をまたいで足す)。"""

    def __init__(self) -> None:
        self.c = {k: {"order": [0, 0], "came_order": [0, 0], "appear": [0.0, 0], "top": [0, 0], "top_once": [0, 0]} for k in CANDIDATES}

    def add(self, rows: list[tuple[int, dict[str, float], float, int | None]], seed: int) -> None:
        """rows: (人, 候補の値, 次の 30 日の 1 イベント × 1 ルームの最大, 量を見た回の数)。"""
        actual = {u: a for u, _, a, _ in rows}
        came = {u: a > 0 for u, _, a, _ in rows}
        came_actual = {u: a for u, a in actual.items() if a > 0}
        rounds = {u: r for u, _, _, r in rows}
        k = min(len(rows), max(10, len(rows) // 40))
        for name, sc in self.c.items():
            pred = {u: v[name] for u, v, _, _ in rows}
            for key, act in (("order", actual), ("came_order", came_actual)):
                pp = {u: pred[u] for u in act}
                agree, total = pairs_agree(pp, act) if len(pp) <= 5000 else _pairs_sampled(pp, act, seed)
                sc[key][0] += agree
                sc[key][1] += total
            agree, total = auc_counts(pred, came)
            sc["appear"][0] += agree
            sc["appear"][1] += total
            sc["top"][0] += top_overlap(pred, actual, k)
            sc["top"][1] += k
            top_p = [u for u, _ in sorted(pred.items(), key=lambda x: (-x[1], x[0]))[:k]]
            sc["top_once"][0] += sum(1 for u in top_p if (rounds.get(u) or 0) <= 1)
            sc["top_once"][1] += len(top_p)

    def result(self) -> dict[str, Any]:
        def rate(x: list[Any]) -> float | None:
            return round(x[0] / x[1], 3) if x[1] else None
        return {name: {"label": CANDIDATES[name], **{k: rate(v) for k, v in sc.items()}, "n_pairs": sc["order"][1]}
                for name, sc in self.c.items()}


# ------------------------------------------------------------------ 1 つの時点を採点する
def _future(full: Store, uid: int, t0: float, t1: float) -> list[Any]:
    return full.all("""SELECT c.event_id, c.room_id, c.point, e.end FROM contributions c JOIN events e USING(event_id)
                       WHERE c.user_id=? AND e.end > ? AND e.end <= ? AND c.point > 0""", (uid, t0, t1))


def score_at(state_dir: Path, at: float, sample: int | None = None, seed: int = 1,
             scores: dict[str, Score] | None = None, rooms_limit: int | None = None, cands: Cands | None = None) -> dict[str, Score]:
    """時点 at (月の初め) で予測を作り、そのあとの実際とくらべて scores に足す。"""
    scores = {k: Score() for k in PREDICTIONS} if scores is None else scores
    month = gifter_analysis.last_month(at)  # at の時に集め終わっていた月
    cur = _add(month, 1)
    end3 = month_start(_add(cur, 3))
    with Store(state_dir) as past, Store(state_dir) as full:
        hide_after(past, at)
        while gifter_status.build_rounds(past, at, limit=10_000):
            pass
        people = gifter_status.population(past, at)
        if sample and len(people) > sample:
            people = sorted(random.Random(seed).sample(people, sample))
        battles = {(r[0], r[1]) for r in full.all("""SELECT r.event_id, r.room_id FROM event_rounds r WHERE r.battle=1 AND r.end > ? AND r.end <= ?""",
                                                    (at, at + NEAR))}
        overall_pred: dict[int, float] = {}
        overall_real: dict[int, float] = {}
        last_month_pt: dict[int, float] = {}
        state_rows: list[tuple[str, float, float, float]] = []
        phase_rows: list[tuple[str, float, float, float]] = []
        edges: dict[int, int] = {}
        cache: dict[Any, Any] = {}
        cand_rows: list[tuple[int, dict[str, float], float, int | None]] = []
        unk = gifter_status._unknown(past, cache)
        cand_months = [m for m in (_add(month, -k) for k in range(CAND_MONTHS)) if m not in unk]
        for uid in people:
            st = gifter_status.compute(past, uid, at, edges, cache)
            if st is None:
                continue
            with past.tx() as c:
                c.execute("INSERT OR REPLACE INTO temp.gifter_status(user_id, version, at, overall, firepower, budget, left_pt, state, top_room, "
                          "top_share, data) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                          (uid, gifter_status.VERSION, at, st["overall"], st["fire"], st["hp"], st["mp"], st["state"], st["_top_room"],
                           st["_top_share"], json.dumps({k: v for k, v in st.items() if not k.startswith("_")}, ensure_ascii=False)))
            fut = _future(full, uid, at, end3)
            near = [r for r in fut if r["end"] <= at + NEAR]
            near_max = max((r["point"] for r in near), default=0)
            battle_max = max((r["point"] for r in near if (r["event_id"], r["room_id"]) in battles), default=None)
            months = {m: past.scalar("SELECT points FROM user_month WHERE user_id=? AND month=?", (uid, m), 0) or 0
                      for m in (month, _add(month, -1), _add(month, -2))}
            recent3 = float(sum(months.values()))
            before = past.all("""SELECT c.room_id, c.point, e.end FROM contributions c JOIN events e USING(event_id)
                                 WHERE c.user_id=? AND c.point > 0 AND e.end > ? ORDER BY e.end DESC""", (uid, at - 120 * DAY))
            last = float(before[0]["point"]) if before else 0.0
            next3 = float(sum(r["point"] for r in fut))
            usual = float(st["hp"] or 0)
            if st["fire"] and near_max:
                scores["status_fire"].add(within(near_max, st["fire"]), st.get("scale"), near_max / st["fire"])
                scores["status_fire"].add_naive(within(near_max, last))
            if st["overall"] and battle_max:
                scores["status_overall"].add(within(battle_max, st["overall"]), st["state"], battle_max / st["overall"])
                scores["status_overall"].add_naive(within(battle_max, last))
            overall_pred[uid], overall_real[uid] = float(st["overall"] or 0), float(near_max)
            last_month_pt[uid] = float(months[month])
            if cands is not None:
                sent = {r["month"] for r in past.all("SELECT month FROM user_month WHERE user_id=? AND month>=? AND points>0",
                                                     (uid, _add(month, -(CAND_MONTHS - 1))))}
                share = sum(1 for m in cand_months if m in sent) / len(cand_months) if cand_months else None
                seen_end = past.scalar("""SELECT MAX(e.end) FROM contributions c JOIN events e USING(event_id)
                                          WHERE c.user_id=? AND c.point > 0 AND e.end <= ?""", (uid, at))
                days = max(0.0, (at - seen_end) / DAY) if seen_end else None
                r90 = float(sum(r["point"] for r in before if r["end"] > at - 90 * DAY))
                cand_rows.append((uid, candidates(st, months[month], r90, share, days), float(near_max), st.get("max_out_rounds")))
            state_rows.append((st["state"], next3, recent3, usual))
            # 前からある「戦力」(ギフターのページの別のタイル)
            pw = gifter_analysis.power(past, uid, month, at)
            if pw:
                real_month = float(sum(r["point"] for r in fut if r["end"] < month_start(_add(cur, 1))))
                big = max((r["point"] for r in fut if r["end"] < month_start(_add(cur, 1))), default=0)
                if pw["forecast"]:
                    scores["power_forecast"].add(within(real_month, pw["forecast"]), pw.get("state"),
                                                 real_month / pw["forecast"] if real_month else None)
                    scores["power_forecast"].add_naive(within(real_month, float(months[month])))
                if pw["upper"]:
                    scores["power_upper"].add(big <= pw["upper"], pw.get("state"))
            mind = st.get("psyche")
            if mind:
                o = mind.get("outlook") or {}
                if o.get("room_id") is not None:
                    real = max((r["point"] for r in fut if r["room_id"] == o["room_id"]
                                and r["end"] <= at + 90 * DAY), default=0)
                    scores["psyche_range"].add((o.get("low") or 0) <= real <= (o.get("high") or 0), mind.get("phase"))
                    mine = [r for r in before if r["room_id"] == o["room_id"]]
                    last_main = float(mine[0]["point"]) if mine else 0.0
                    scores["psyche_range"].add_naive(within(real, last_main) if last_main else real == 0)
                    if o.get("appear") in APPEAR_CLAIMS:
                        scores["psyche_appear"].add((real > 0) == APPEAR_CLAIMS[o["appear"]], o["appear"])
                        scores["psyche_appear"].add_naive((real > 0) == any(r["end"] > at - 90 * DAY for r in mine))
                phase_rows.append((mind.get("phase"), next3, recent3, float(mind.get("usual") or 0)))
        _claims(scores["status_state"], state_rows, STATE_CLAIMS)
        _claims(scores["psyche_phase"], phase_rows, PHASE_CLAIMS)
        agree, total = pairs_agree(overall_pred, overall_real) if len(overall_pred) <= 5000 else _pairs_sampled(overall_pred, overall_real, seed)
        scores["status_order"].n += total
        scores["status_order"].hit += agree
        agree, total = pairs_agree(last_month_pt, overall_real) if len(last_month_pt) <= 5000 else _pairs_sampled(last_month_pt, overall_real, seed)
        scores["status_order"].naive[0] += agree
        scores["status_order"].naive[1] += total
        if cands is not None and cand_rows:
            cands.add(cand_rows, seed)
        _score_rooms(past, full, at, scores, rooms_limit)
    gifter_analysis._cuts.clear()
    return scores


def _pairs_sampled(pred: dict[int, float], actual: dict[int, float], seed: int, n: int = 200_000) -> tuple[int, int]:
    """人が多い時は、2 人の組を n 組だけ選んでくらべる。"""
    ids = list(pred)
    rnd = random.Random(seed)
    agree = total = 0
    for _ in range(n):
        a, b = rnd.sample(ids, 2)
        if pred[a] == pred[b] or actual[a] == actual[b]:
            continue
        total += 1
        agree += (pred[a] > pred[b]) == (actual[a] > actual[b])
    return agree, total


def _claims(score: Score, rows: list[tuple[Any, float, float, float]], claims: dict[str, Callable[[float, float, float], bool]]) -> None:
    """種類ごとに当たりを数え、くらべる基準 (同じ線を、全員に当てはめた時に当たる割合) も入れる。
    基準より高くなければ、その種類を言う意味が無い (だれにでも当たる)。"""
    for kind, nxt, recent, usual in rows:
        test = claims.get(kind)
        if test is None:
            continue
        score.add(test(nxt, recent, usual), kind)
    for kind in {r[0] for r in rows}:
        test = claims.get(kind)
        if test is None:
            continue
        b = score.base.setdefault(kind, [0, 0])
        b[0] += sum(1 for _, nxt, recent, usual in rows if test(nxt, recent, usual))
        b[1] += len(rows)


def _score_rooms(past: Store, full: Store, at: float, scores: dict[str, Score], limit: int | None) -> None:
    # 地力 (作ったステータスから、ページと同じ足し方で)
    run = heat._PowerRun(None)
    while not run.add(past, heat.CHUNK):
        pass
    power = {rid: float(t[0]) for rid, t in run.totals.items() if t[0] > 0}
    events = full.all("""SELECT event_id, start, end FROM events WHERE start >= ? AND end IS NOT NULL AND end <= ?""", (at, at + NEAR))
    strength: dict[int, Any] = {}
    best: dict[int, float] = {}
    lasts: dict[int, float | None] = {}

    def last_of(rid: int) -> float | None:  # 直前のイベントのポイント (何も考えない予測)
        if rid not in lasts:
            r = past.one("""SELECT re.point FROM room_events re JOIN events e USING(event_id)
                            WHERE re.room_id=? AND re.point > 0 AND e.end < ? ORDER BY e.end DESC LIMIT 1""", (rid, at))
            lasts[rid] = float(r[0]) if r is not None else None
        return lasts[rid]

    for e in events:
        rows = full.all("SELECT room_id, point, rank FROM room_events WHERE event_id=? AND point > 0", (e["event_id"],))
        real = {r["room_id"]: float(r["point"]) for r in rows}
        for rid, pt in real.items():
            best[rid] = max(best.get(rid, 0.0), pt)
        a, t = pairs_agree({r: power[r] for r in real if r in power}, real)
        scores["room_power"].n += t
        scores["room_power"].hit += a
        naive = {r: v for r in real for v in [last_of(r)] if v}
        a, t = pairs_agree({r: v for r, v in naive.items() if r in power}, real)  # 地力をくらべたのと同じルームで
        scores["room_power"].naive[0] += a
        scores["room_power"].naive[1] += t
        st = {rid: x["per_day"] for rid in real for x in [event_analysis.room_strength(past, rid, at, strength)] if x}
        a, t = pairs_agree(st, real)
        scores["event_strength"].n += t
        scores["event_strength"].hit += a
        a, t = pairs_agree({r: v for r, v in naive.items() if r in st}, real)
        scores["event_strength"].naive[0] += a
        scores["event_strength"].naive[1] += t
    for rid, pt in best.items():
        if rid in power:
            scores["room_power_size"].add(within(pt, power[rid]), None, pt / power[rid])
            if last_of(rid):
                scores["room_power_size"].add_naive(within(pt, last_of(rid) or 0))
    # 勢い: at の時の判定と、次の 60 日 ÷ at の前の 60 日
    rids = [r[0] for r in past.all("""SELECT DISTINCT re.room_id FROM room_events re JOIN events e USING(event_id)
                                       WHERE e.end > ? AND e.end <= ? AND re.point > 0""", (at - ROOM_DAYS, at))]
    if limit:
        rids = rids[:limit]
    rows = []
    for rid in rids:
        mo = room_analysis.momentum(past, rid, at)
        if not mo.get("label"):
            continue
        nxt = room_analysis.momentum(full, rid, at + ROOM_DAYS).get("value")
        if nxt is not None:
            rows.append((mo["label"], float(nxt), 0.0, 0.0))
    _claims(scores["room_momentum"], rows, MOMENTUM_CLAIMS)


# ------------------------------------------------------------------ まとめて動かす・読む
def default_months(now: float, count: int = 3) -> list[str]:
    """結果が 3 か月分そろっている、いちばん新しい月の初めから count 個 (古い順)。"""
    m = _add(datetime.fromtimestamp(now, JST).strftime("%Y-%m"), -4)
    return [_add(m, -k) for k in range(count - 1, -1, -1)]


def run(state_dir: Path, months: Iterable[str], sample: int | None = None, rooms_limit: int | None = None) -> dict[str, Any]:
    started = time.time()
    scores: dict[str, Score] = {k: Score() for k in PREDICTIONS}
    cands = Cands()
    months = list(months)
    for m in months:
        log.info("採点: %s の初めの時点", m)
        score_at(Path(state_dir), month_start(m), sample, scores=scores, rooms_limit=rooms_limit, cands=cands)
    out = {"version": VERSION, "at": time.time(), "seconds": round(time.time() - started), "months": months, "sample": sample,
           "min_n": MIN_N, "predictions": {}}
    for k, (label, claim) in PREDICTIONS.items():
        r = scores[k].result()
        out["predictions"][k] = {"label": label, "claim": claim, **r, "checked": bool(r["n"]),
                                 **({"naive_label": NAIVE[k]} if k in NAIVE else {})}
    # 見込み量の式の候補 (画面には出さない。order = 2 人の並び、came_order = 出てきた人どうしの並び、appear = 出てくるかの当たり (AUC)、
    # top = 予測の上から 2.5% (最低 10 人) が実際も上から 2.5% に入った割合、top_once = その上の人のうち、量を見た回が 1 回以下の人の割合)
    out["candidates"] = cands.result()
    return out


def read(state_dir: Path) -> dict[str, Any]:
    """ページに出す採点の結果 (置いていなければ、どれも未検証)。"""
    try:
        data = json.loads((Path(state_dir) / FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = None
    if not isinstance(data, dict):
        return {"found": False, "predictions": {k: {"label": v[0], "claim": v[1], "checked": False} for k, v in PREDICTIONS.items()}}
    return {"found": True, **data, "current": data.get("version") == VERSION}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="予測の答え合わせ (research.db の写しで動かす。サーバーでは動かさない)")
    p.add_argument("state_dir", help="research.db の写しがあるフォルダ")
    p.add_argument("--at", action="append", help="予測を作る月 (例 2026-06。その月の初めの時点)。何回でも")
    p.add_argument("--sample", type=int, default=None, help="ギフターを何人まで選ぶか (省くと全員。地力は選んだ人だけで足す)")
    p.add_argument("--rooms", type=int, default=None, help="勢いを採点するルームの数の上限")
    p.add_argument("--out", default=None, help="結果の JSON (省くと、写しのフォルダの backtest.json)")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    months = a.at or default_months(time.time())
    out = run(Path(a.state_dir), months, a.sample, a.rooms)
    path = Path(a.out) if a.out else Path(a.state_dir) / FILE
    path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    for k, v in out["predictions"].items():
        rate = "未検証" if not v["checked"] else f"{v['rate'] * 100:.0f}% ({v['hit']}/{v['n']})"
        if v.get("naive"):
            rate += f" ・ 何も考えない予測 {v['naive']['rate'] * 100:.0f}%"
        print(f"{v['label']}: {rate}")
    print("見込み量の式の候補 (並び / 出てきた人どうしの並び / 出てくるか / 上の人の重なり / 上の人のうち 1 回だけの人):")
    for k, v in out["candidates"].items():
        cells = " / ".join("-" if v[x] is None else f"{v[x] * 100:.0f}%" for x in ("order", "came_order", "appear", "top", "top_once"))
        print(f"  {v['label']}: {cells}")
    print(f"結果: {path} ({out['seconds']} 秒)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
