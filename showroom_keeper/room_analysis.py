"""ルームの分析: タイプ (ギフトの集まり方)・勢い・戦術 (イベントの走り方)。

research.db を読んで作るだけで、SHOWROOM には問い合わせない。分かるのは集めた範囲だけ
(貢献ランキングは上位 100 人、ポイントの推移は集め始めてからのイベント) なので、どれも「傾向」として出す。

- タイプ: 直近 PROFILE_EVENTS 件の終わったイベントの貢献ランキングから、1 位の人の割合・上位 10 人のポイント・
  貢献した人数などを出し、同じくらいの規模のイベントに出ているルームの中で比べる (規模で数字が大きく変わるため)
- 勢い: イベントの順位 (参加ルームの中の位置) の、直近 MOMENTUM_DAYS 日とその前の MOMENTUM_DAYS 日の違い
- 戦術: ポイントの推移を残したイベントで、序盤・中盤・終盤の位置と最終日の伸びから、逃げ・先行・差し・追い込みに分ける。
  決まった走り方が無いルームは「自在」

全部のルームの分は、全ルームの収集 (sweep) の合間に 1 日 1 回、少しずつ作り直す (room_profile)。
ルームのページでは、そのルームの分をその場で作る (くらべる基準 = 区切りの値は、1 日 1 回の分を使う)。
"""

from __future__ import annotations

import json
from datetime import datetime
import math
import time
from typing import Any

from .status import JST
from .research_store import PROGRESS_THIN_AFTER, Store, median

PROFILE_EVENTS = 10  # タイプ分けに使う、直近の終わったイベントの数
MIN_EVENTS = 3  # これより少なければ「判定できない」
CAPPED = 100  # 貢献ランキングに載る人数の上限 (これに届いていれば、実際はもっと多い)
SIZE_EDGES = (50, 300)  # 参加ルームの数 (真ん中の値) で、小さい・中くらい・大きいイベントに分ける
SIZE_NAMES = ("小さいイベント (50 ルーム未満)", "中くらいのイベント (50〜299 ルーム)", "大きいイベント (300 ルーム以上)")
MOMENTUM_DAYS = 60
MOMENTUM_MIN = 2  # 勢いの数字を出すのに、それぞれの期間に要るイベントの数
MOMENTUM_LABEL_MIN = 5  # 「上り調子」「下り気味」と言うのに、それぞれの期間に要るイベントの数 (2 件ずつでは、偶然の差でも 7 割ほど付く)
MOMENTUM_STEP = 0.1  # 位置がこれだけ変わったら「上り調子」「下り気味」
MOMENTUM_MIN_ROOMS = 5  # これより参加ルームの少ないイベントは勢いに使わない (2 ルームなら位置が 0 か 1 しかなく、ぶれが大きい)
STYLE_SNAPSHOTS = 6  # 走り方を出すのに要る、推移を残した回数
STYLE_START = 0.3  # 推移を、イベントの期間のこの割合までに残し始めていること
STYLE_EVENTS = 3  # ルームの戦術を出すのに要る、走り方の分かったイベントの数
STYLE_RECENT = 10  # ルームの戦術に使う、直近のイベントの数
STYLE_NEAR = 2 * 3600  # 序盤・中盤・終盤・最終日の前の回は、狙った時刻からこれより離れていたら「分からない」(秒)
STYLE_MIN_ROOMS = 5  # これより参加ルームの少ないイベントでは走り方を出さない (2〜4 ルームでは逃げ・先行がほぼ必ず付く)
STYLE_LATE_BOOST = 2.5  # 追い込み: 最後の 24 時間の伸びが、ならした伸び (24 時間 ÷ 期間) のこの倍以上
STYLE_EARLY_ACTIVE = 0.1  # 序盤の回で、ポイントのあるルームがこの割合より少なければ、序盤の位置は分からない扱い
STYLE_WAIT = 3 * 86400  # 終わった時の最終結果が読めない時に、走り方を作るまで待つ時間 (秒)
PROFILE_EVERY = 86400  # 全部のルームの分を作り直す間隔 (秒)
PROFILE_CHUNK = 300  # 1 回に作るルームの数
CUTS_META = "room_type_cuts"

TYPES = {
    "上位集中型": "1 位の人のポイントが大きな割合を占める (1 人に頼っている)",
    "高額の常連型": "上位 10 人のポイントが大きく、上位 3 人に偏っていない (高額のギフターが何人もいる)",
    "少額多人数型": "1 人あたりは小さいが、貢献する人が多い",
    "少数精鋭型": "貢献する人は少ないが、ルームのポイントは大きい",
    "バランス型": "目立った偏りがない",
}
STYLES = {
    "逃げ": "序盤から 1 位に立ち、そのまま走る",
    "先行": "序盤から上位 (上から 4 分の 1 以内) にいて、そのまま終える",
    "差し": "序盤は上位でなく、中盤から終盤に上げて上位で終える",
    "追い込み": "終盤まで下の方にいて、最終日に大きく伸ばす",
    "自在": "いくつもの走り方をする (イベントごとに走り方が違う)",
    "型なし": "目立った走り方をしたイベントが少ない (中ほどで終えることが多いなど)",
}
CAUTION = ("集めた範囲から出した「傾向」です。貢献ランキングは上位 100 人までしか分からず、"
           "推移は集め始めてからのイベントだけです。外に出さず、中だけで使ってください。")


# ------------------------------------------------------------------ タイプ
def _event_rows(s: Store, room_id: int, limit: int = PROFILE_EVENTS) -> list[Any]:
    return s.all("""SELECT re.event_id, re.point, re.rank, re.c_count, re.c_sum, re.c_top1, re.c_top3, re.c_top10, re.c_p100,
                           e.end, e.room_count, e.median_point
                    FROM room_events re JOIN events e USING(event_id)
                    WHERE re.room_id=? AND re.final=1 AND e.show_ranking=1 AND e.end IS NOT NULL
                      AND re.c_count > 0 AND e.median_point > 0
                      AND e.rooms_at >= e.end  -- 終わったあとに読み直した最終のポイント (途中のポイントで比べない)
                    ORDER BY e.end DESC LIMIT ?""", (int(room_id), limit))


def _size_class(room_count: Any) -> int:
    if room_count is None:
        return 1
    return sum(room_count >= edge for edge in SIZE_EDGES)


def metrics(rows: list[Any]) -> dict[str, Any] | None:
    """直近のイベントの貢献ランキングから、タイプ分けの数字 (イベントごとの値の真ん中) を出す。"""
    if len(rows) < MIN_EVENTS:
        return None
    per = []
    for r in rows:
        denom = max(r["point"] or 0, r["c_sum"] or 0)
        if denom <= 0:
            continue
        per.append({"top1": r["c_top1"] / denom, "top3": r["c_top3"] / denom, "contributors": r["c_count"],
                    "capped": r["c_count"] >= CAPPED, "rel_top10": r["c_top10"] / r["median_point"],
                    "rel_per_contrib": r["c_sum"] / r["c_count"] / r["median_point"],
                    "rel_point": (r["point"] or 0) / r["median_point"], "room_count": r["room_count"],
                    "depth_pt": r["c_p100"], "depth_rel": None if r["c_p100"] is None else r["c_p100"] / r["median_point"],
                    # 100 位より下の人たちの分: ルームのポイント - 貢献ランキング (上位 100 人) の合計。
                    # 100 人に届かないイベントは、101 人目がいないので出さない (差があっても、ギフト以外のポイント)
                    "below_pt": max(0, (r["point"] or 0) - r["c_sum"]) if r["c_count"] >= CAPPED else None,
                    "below_share": max(0, (r["point"] or 0) - r["c_sum"]) / r["point"]
                    if r["c_count"] >= CAPPED and (r["point"] or 0) > 0 else None})
    if len(per) < MIN_EVENTS:
        return None
    out = {k: median(p[k] for p in per) for k in ("top1", "top3", "contributors", "rel_top10", "rel_per_contrib", "rel_point")}
    out["capped"] = sum(p["capped"] for p in per) * 2 > len(per)  # 半分より多くのイベントで上限に届いた
    # 層の厚さ: 貢献ランキング 100 位の人のポイント (100 人に届かないイベントは 0)。上の人だけでなく、
    # 100 人目までが大きく送っているほど、ファンの層が厚い
    out["depth_pt"] = median(p["depth_pt"] for p in per)
    out["depth_rel"] = median(p["depth_rel"] for p in per)
    # 100 位より下の分 (100 人に届いたイベントだけの真ん中の値。届いたイベントが無ければ None)
    out["below_pt"] = median(p["below_pt"] for p in per)
    out["below_share"] = median(p["below_share"] for p in per)
    out["below_events"] = sum(p["below_pt"] is not None for p in per)
    out["size_class"] = _size_class(median(p["room_count"] for p in per))
    out["events"] = len(per)
    return out


def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    v = sorted(values)
    return v[min(len(v) - 1, int(q * len(v)))]


def build_cuts(profiles: list[dict[str, Any]]) -> dict[str, Any]:
    """規模ごとの区切りの値 (同じ規模のルームの中の、上から何割目か)。"""
    cuts: dict[str, Any] = {}
    for size in range(len(SIZE_NAMES)):
        ps = [p for p in profiles if p.get("size_class") == size and p.get("top1") is not None]
        col = {k: [p[k] for p in ps] for k in ("top1", "top3", "contributors", "rel_top10", "rel_per_contrib", "rel_point")}
        cuts[str(size)] = {"n": len(ps), "top1_p80": _pct(col["top1"], 0.8), "top3_p50": _pct(col["top3"], 0.5),
                           "rel_top10_p75": _pct(col["rel_top10"], 0.75), "contributors_p75": _pct(col["contributors"], 0.75),
                           "contributors_p25": _pct(col["contributors"], 0.25),
                           "rel_per_contrib_p50": _pct(col["rel_per_contrib"], 0.5), "rel_point_p50": _pct(col["rel_point"], 0.5)}
        # 層の厚さの区切りは、100 人に届いたルームどうしでくらべる (届かないルームは 0 で、くらべるまでもなく薄い)
        deep = sorted(p["depth_rel"] for p in ps if p.get("depth_rel"))
        cuts[str(size)].update({"depth_n": len(deep), "depth_p25": _pct(deep, 0.25), "depth_p75": _pct(deep, 0.75)})
    return cuts


CUTS_MIN_ROOMS = 20  # 区切りを作るのに要る、同じ規模のルームの数


def classify(m: dict[str, Any] | None, cuts: dict[str, Any] | None) -> str | None:
    """タイプ (None は判定できない)。上から順に当てはまるか見る。"""
    if m is None or not cuts:
        return None
    c = cuts.get(str(m["size_class"]))
    if not c or c.get("n", 0) < CUTS_MIN_ROOMS:
        return None
    if m["top1"] >= max(c["top1_p80"], 0.4):
        return "上位集中型"
    # 区切りちょうどの値は多いほう・少ないほうに入れない (みんな同じくらいのルームが、どれかの型にならないように)
    if m["rel_top10"] > c["rel_top10_p75"] and m["top3"] <= c["top3_p50"]:
        return "高額の常連型"
    if (m["capped"] or m["contributors"] > c["contributors_p75"]) and m["rel_per_contrib"] <= c["rel_per_contrib_p50"]:
        return "少額多人数型"
    if m["contributors"] < c["contributors_p25"] and m["rel_point"] >= c["rel_point_p50"]:
        return "少数精鋭型"
    return "バランス型"


DEPTH_NOTES = {
    "厚い": "100 位の人も大きく送っている (同じ規模で 100 人に届くルームの、上から 4 分の 1)",
    "ふつう": "100 位の人のポイントが、同じ規模で 100 人に届くルームの真ん中あたり",
    "やや薄い": "100 人には届くが、100 位の人のポイントは小さい (同じ規模で下から 4 分の 1)",
    "薄い": "多くのイベントで、貢献ランキングが 100 人に届かない",
}


def depth_label(m: dict[str, Any] | None, cuts: dict[str, Any] | None) -> str | None:
    """層の厚さ (厚い・ふつう・やや薄い・薄い)。None は判定できない。"""
    if m is None or m.get("depth_pt") is None:
        return None
    if not m["depth_pt"]:
        return "薄い"
    c = (cuts or {}).get(str(m["size_class"])) or {}
    if c.get("depth_n", 0) < CUTS_MIN_ROOMS or m.get("depth_rel") is None:
        return None
    if m["depth_rel"] >= c["depth_p75"]:
        return "厚い"
    if m["depth_rel"] <= c["depth_p25"]:
        return "やや薄い"
    return "ふつう"


# ------------------------------------------------------------------ 勢い
def momentum(s: Store, room_id: int, now: float) -> dict[str, Any]:
    """順位の位置 ((順位 - 1) ÷ (参加ルーム数 - 1)。1 位は 0、最下位は 1) の、直近と前の期間の平均の違い。
    正の数なら上がっている。1 位をいつも 0 にするので、小さいイベントに出ても位置が悪く見えない。
    """
    span = MOMENTUM_DAYS * 86400
    rows = s.all("""SELECT e.end, MIN(1.0, CAST(re.rank - 1 AS REAL) / (e.room_count - 1)) AS place
                    FROM room_events re JOIN events e USING(event_id)
                    WHERE re.room_id=? AND re.rank > 0 AND e.room_count >= ? AND e.end > ? AND e.end <= ?
                      AND e.rooms_at >= e.end""",
                 (int(room_id), MOMENTUM_MIN_ROOMS, now - 2 * span, now))
    recent = [r["place"] for r in rows if r["end"] > now - span]
    prev = [r["place"] for r in rows if r["end"] <= now - span]
    out: dict[str, Any] = {"recent_events": len(recent), "prev_events": len(prev), "days": MOMENTUM_DAYS,
                           "recent_place": sum(recent) / len(recent) if recent else None,
                           "prev_place": sum(prev) / len(prev) if prev else None, "value": None, "label": None}
    if len(recent) >= MOMENTUM_MIN and len(prev) >= MOMENTUM_MIN:
        # 画面には 100 倍して整数で出すので、判定も同じ丸めた値で行う (「+10 なのに変わらない」と見えないように)
        out["value"] = round((out["prev_place"] - out["recent_place"]) * 100) / 100
        # 差が、イベントごとのばらつきから来る誤差 (標準誤差) の 2 倍より大きい時だけ、上り・下りと言う
        noise = math.sqrt(_var(recent) / len(recent) + _var(prev) / len(prev))
        out["noise"] = round(noise, 3)
        if len(recent) < MOMENTUM_LABEL_MIN or len(prev) < MOMENTUM_LABEL_MIN:
            out["label_why"] = f"イベントが {MOMENTUM_LABEL_MIN} 件ずつそろうまでは、数字だけ出します"
        elif abs(out["value"]) >= MOMENTUM_STEP and abs(out["value"]) > 2 * noise:
            out["label"] = "上り調子" if out["value"] > 0 else "下り気味"
        else:
            out["label"] = "変わらない"
    return out


def _var(xs: list[float]) -> float:
    if len(xs) < 2:
        return 0.0
    m = sum(xs) / len(xs)
    return sum((x - m) ** 2 for x in xs) / (len(xs) - 1)


# ------------------------------------------------------------------ 戦術 (イベントの走り方)
def _places(order: list[tuple[int, int]]) -> dict[int, float]:
    """ポイントの多い順のルームの、位置 (0 = 1 位、1 = 最下位)。同じポイントのルームは、同じ位置 (いちばん悪い位置) にする
    (序盤に 0 ポイントのルームがたくさん並んでも、ルーム番号の順で上位・下位が決まらないように)。
    """
    n = len(order)
    if n <= 1:
        return {rid: 0.0 for rid, _ in order}
    out, i = {}, 0
    while i < n:
        j = i
        while j + 1 < n and order[j + 1][1] == order[i][1]:
            j += 1
        for k in range(i, j + 1):
            out[order[k][0]] = j / (n - 1)
        i = j + 1
    return out


def _near(ats: list[float], t: float) -> float | None:
    """狙った時刻にいちばん近い回 (STYLE_NEAR より離れていれば None)。"""
    best = min(ats, key=lambda a: abs(a - t))
    return best if abs(best - t) <= STYLE_NEAR else None


def _at_or_before(ats: list[float], t: float) -> float:
    best = ats[0]
    for a in ats:
        if a <= t:
            best = a
    return best


def event_styles(s: Store, event_id: int) -> list[tuple[Any, ...]] | None:
    """終わったイベントの、ルームごとの走り方。推移が足りなければ None。"""
    e = s.one("SELECT start, end FROM events WHERE event_id=?", (int(event_id),))
    if e is None or not e["start"] or not e["end"] or e["end"] <= e["start"]:
        return None
    start, end = e["start"], e["end"]
    dur = end - start
    eid = int(event_id)
    ats = [r[0] for r in s.all("SELECT DISTINCT at FROM event_progress WHERE event_id=? ORDER BY at", (eid,))]
    if len(ats) < STYLE_SNAPSHOTS or ats[0] > start + STYLE_START * dur or ats[-1] < end - 3600:
        return None
    # 全部の回を一度に読むと、長いイベントでは数百万行になる (サーバーのメモリが 2GB なので)。
    # 位置を使う回 (序盤・中盤・終盤・最終日の前・最後) だけを読み、1 位は回ごとに 1 行だけ読む
    # 狙った時刻から離れた回で代わりに測ると、推移が途切れたイベントで位置や伸びがずれるので、「分からない」にする
    t_early, t_mid, t_late = (_near(ats, start + f * dur) for f in (0.25, 0.5, 0.75))
    t_day = _near(ats, end - 86400) if dur > 1.5 * 86400 else None  # 短いイベントは最終日の伸びを測らない
    final_at = ats[-1]
    snaps: dict[float, dict[int, int]] = {}
    places: dict[float, dict[int, float]] = {}
    for at in {t for t in (t_early, t_mid, t_late, t_day, final_at) if t is not None}:
        order = [(r[0], r[1] or 0) for r in s.all("""SELECT room_id, point FROM event_progress WHERE event_id=? AND at=?
                                                    ORDER BY point DESC, room_id""", (eid, at))]
        snaps[at] = dict(order)
        places[at] = _places(order)
    # 1 位の回: 1 位が 1 ルームだけで、ポイントがある回だけ数える (全員 0 や同点の回は、だれの 1 位にもしない)
    leads: dict[int, int] = {}
    scored = 0
    tops: list[int | None] = []  # 回ごとの 1 位 (同点や全員 0 は None)。1 位の入れ替わりを数えるため
    for at in ats:
        top = s.all("SELECT room_id, point FROM event_progress WHERE event_id=? AND at=? ORDER BY point DESC LIMIT 2", (eid, at))
        if top and (top[0][1] or 0) > 0:
            scored += 1  # 誰かにポイントのある回 (1 位の割合は、この回の数で割る)
            if len(top) == 1 or (top[1][1] or 0) < top[0][1]:
                leads[top[0][0]] = leads.get(top[0][0], 0) + 1
                tops.append(top[0][0])
            else:
                tops.append(None)
    # 序盤にほとんど誰もポイントを持っていない (まだ始まっていない) 時は、みな同じ「最下位」になり、
    # 上位で終えたルームがみな「差し」に見えてしまう。その時は序盤の位置を使わない (先行・差しを付けない)
    if sum(p > 0 for p in snaps[final_at].values()) < STYLE_MIN_ROOMS:
        return []  # 参加ルームが少ないイベント (走り方は出さないが、作り終えた印は付ける)
    early_snap = snaps.get(t_early) or {}
    early_known = bool(early_snap) and sum(p > 0 for p in early_snap.values()) >= STYLE_EARLY_ACTIVE * len(early_snap)
    expected_day = 86400 / dur  # ならした時の、最後の 24 時間の割合
    # 1 位の入れ替わり: 1 位が決まっている回どうしで、1 位が変わった回 (そのルームが 1 位になった・1 位から落ちた)
    leaders = [t for t in tops if t is not None]
    swaps: dict[int, int] = {}
    for a, b in zip(leaders, leaders[1:]):
        if a != b:
            swaps[a] = swaps.get(a, 0) + 1
            swaps[b] = swaps.get(b, 0) + 1
    # 逃げの引き離し: 序盤の 1 位の、2 位との差 (1 位のポイントに対する割合)。相手の失速: その 2 位の、序盤から終盤までの
    # 伸びの速さ ÷ 始まりから序盤までの速さ (1 より小さいほど、引き離されたあと伸びが鈍った)
    early_gap, rival_slow, early_leader = None, None, None
    if early_known and t_early is not None:
        order = sorted(early_snap.items(), key=lambda kv: (-kv[1], kv[0]))
        if len(order) >= 2 and order[0][1] > order[1][1] > 0:
            early_leader, rival = order[0][0], order[1][0]
            early_gap = (order[0][1] - order[1][1]) / order[0][1]
            if t_late is not None and t_late > t_early > start:
                before = order[1][1] / (t_early - start)
                after = (snaps[t_late].get(rival, order[1][1]) - order[1][1]) / (t_late - t_early)
                rival_slow = after / before if before > 0 else None
    out = []
    for rid, final_pt in snaps[final_at].items():
        if final_pt <= 0:
            continue
        early = places[t_early].get(rid, 1.0) if early_known else None
        mid = places[t_mid].get(rid, 1.0) if t_mid is not None else None
        late = places[t_late].get(rid, 1.0) if t_late is not None else None
        final = places[final_at][rid]
        day_pt = snaps[t_day].get(rid, 0) if t_day is not None else None
        last_day = (final_pt - day_pt) / final_pt if day_pt is not None else None
        lead = leads.get(rid, 0) / scored if scored else 0.0
        style = classify_run(early, late, final, last_day, lead, expected_day)
        out.append((eid, rid, style, early, mid, late, final, last_day, None if early is None else early - final, lead,
                    swaps.get(rid, 0), early_gap if rid == early_leader else None, rival_slow if rid == early_leader else None))
    return out


def classify_run(early: float | None, late: float | None, final: float, last_day: float | None, lead: float,
                 expected_day: float = 1 / 7) -> str | None:
    """early が None (序盤の位置が分からない) の時は、先行・差しを付けない。late・last_day が None なら追い込みを付けない。

    追い込みの「最終日の伸び」は、ならした伸び (expected_day = 24 時間 ÷ 期間) の STYLE_LATE_BOOST 倍以上
    (前は「4 割以上」と決めていたので、3 日のイベントでは毎日同じペースでも 33% になり、だれでも届きかけた)。
    """
    if lead >= 0.5:
        return "逃げ"
    if early is not None and early <= 0.25 and final <= 0.25:
        return "先行"
    if late is not None and late > 0.5 and final <= 0.5 and last_day is not None and last_day >= STYLE_LATE_BOOST * expected_day:
        return "追い込み"
    if early is not None and early > 0.25 and final <= 0.25 and final < early - 0.1:
        return "差し"
    return None


def room_style(s: Store, room_id: int) -> dict[str, Any]:
    """直近 STYLE_RECENT 件の、走り方を見たイベント (「型なし」も数に入れる) から、ルームの戦術を決める。

    - 1 つの走り方が半分を超える → その走り方
    - 型の付いたイベントが半分以上あり、2 種類以上の走り方をしている → 自在
    - それ以外 (型の付いたイベントが少ない) → 型なし (例: 逃げ 3 件・型なし 7 件。「自在」とは言えない)
    """
    rows = s.all("""SELECT st.style FROM room_event_style st JOIN events e USING(event_id)
                    WHERE st.room_id=? ORDER BY e.end DESC LIMIT ?""", (int(room_id), STYLE_RECENT))
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["style"] or "型なし"] = counts.get(r["style"] or "型なし", 0) + 1
    if len(rows) < STYLE_EVENTS:
        return {"style": None, "events": len(rows), "counts": counts}
    typed = {k: v for k, v in counts.items() if k != "型なし"}
    top, n = max(typed.items(), key=lambda kv: kv[1], default=(None, 0))
    if top and n * 2 > len(rows):
        style = top
    elif sum(typed.values()) * 2 >= len(rows) and len(typed) >= 2:
        style = "自在"
    else:
        style = "型なし"
    return {"style": style, "events": len(rows), "counts": counts}


# ------------------------------------------------------------------ 戦い方の目印 (docs/戦術メモ.md)
WIN_EVENTS = 20  # 節約の目印に使う、直近の 1 位のイベントの数


def tactics(s: Store, room_id: int) -> dict[str, Any]:
    """戦い方の目印: 節約して勝てたか・逆転の多さ・逃げの引き離しと相手の失速 (人数の割合などは出さない)。

    - 節約: 1 位になったイベントで、2 位との差 ÷ 自分のポイント。小さいほど、必要な分だけで勝った (真ん中の値)
    - 逆転: 走り方を見たイベントで、そのルームが 1 位になった・1 位から落ちた回数の平均
    - 逃げ: 序盤に 1 位だったイベントで、2 位との差の割合と、2 位の伸びの速さの変わり方 (1 より小さいほど鈍った)
    """
    rid = int(room_id)
    wins = s.all("""SELECT re.event_id, re.point, e.name, e.end,
                           (SELECT MAX(o.point) FROM room_events o WHERE o.event_id = re.event_id AND o.room_id != re.room_id) AS second
                    FROM room_events re JOIN events e USING(event_id)
                    WHERE re.room_id=? AND re.rank=1 AND re.point > 0 AND e.rooms_at >= e.end AND e.room_count >= ?
                    ORDER BY e.end DESC LIMIT ?""", (rid, STYLE_MIN_ROOMS, WIN_EVENTS))
    margins = [(w["point"] - (w["second"] or 0)) / w["point"] for w in wins if w["second"]]
    rows = s.all("""SELECT st.swaps, st.early_gap, st.rival_slow FROM room_event_style st JOIN events e USING(event_id)
                    WHERE st.room_id=? ORDER BY e.end DESC LIMIT ?""", (rid, STYLE_RECENT))
    swaps = [r["swaps"] for r in rows if r["swaps"] is not None]
    gaps = [r["early_gap"] for r in rows if r["early_gap"] is not None]
    slows = [r["rival_slow"] for r in rows if r["rival_slow"] is not None]
    return {"wins": len(wins), "win_margin": median(margins), "close_wins": sum(m < 0.05 for m in margins),
            "swaps": sum(swaps) / len(swaps) if swaps else None, "swap_events": len(swaps),
            "early_leads": len(gaps), "early_gap": median(gaps), "rival_slow": median(slows)}


# ------------------------------------------------------------------ リス活 (挨拶回り) と、推し・付き合い投げ
ROUND_EVENTS = 5  # リス活を見る、直近の終わったイベントの数
ROUND_CORE = 5  # そのイベントの貢献上位の何人を「コアな人」とみるか
ROUND_FANS = 10  # アクティブファンのレベルの高い何人を「コアな人」に足すか (ポイントに出ない常連も入れるため)
TSUKIAI = 0.2  # 最推しのルームの 1 イベントで送った最大の量に対して、これより少なければ「付き合い投げ」


def room_owner(s: Store, room_id: int) -> int | None:
    """ルームの配信者のユーザー ID。分からない時は None (推測はしない)。

    配信者は自分のルームのファンにはならないので、ほかのルームのアクティブファンのプロフィールから見つける
    (プロフィールでルームを公開している人だけ)。次の両方を満たす時だけ「本人」とみる:
    - ルームの URL の名前が、SHOWROOM が最初に付ける形 (ユーザー ID で終わる) で、その人の ID と合う
    - そのルームに結び付いた人が 1 人だけ
    """
    rows = s.all("SELECT user_id, room_key FROM user_rooms WHERE room_id=?", (int(room_id),))
    sure = [r[0] for r in rows if r[1] and str(r[1]).endswith(str(r[0]))]
    return sure[0] if len(sure) == 1 else None


def regulars(s: Store, room_id: int) -> dict[str, Any] | None:
    """アクティブファン (いちばん新しい月): 人数・ファンパワーと、そのうち貢献ランキングに一度も出ていない人の数。"""
    rid = int(room_id)
    f = s.one("SELECT * FROM room_fans WHERE room_id=? ORDER BY ym DESC LIMIT 1", (rid,))
    if f is None:
        return None
    fans = s.all("SELECT user_id, level FROM active_fans WHERE room_id=? AND ym=? ORDER BY level DESC", (rid, f["ym"]))
    givers = {r[0] for r in s.all("""SELECT DISTINCT c.user_id FROM room_events re
                                     JOIN contributions c ON c.event_id = re.event_id AND c.room_id = re.room_id WHERE re.room_id=?""",
                                  (rid,))}
    silent = [r for r in fans if r["user_id"] not in givers]
    return {"ym": f["ym"], "total": f["total"], "fan_power": f["fan_power"], "listed": len(fans), "silent": len(silent),
            "silent_top_level": silent[0]["level"] if silent else None, "top_level": fans[0]["level"] if fans else None}


def _oshi(s: Store, uid: int, room_id: int, amount: int, cache: dict[int, tuple[Any, int]]) -> tuple[str, Any]:
    """その人にとって、このルームへの送り方が「推し」「掛け持ち」「付き合い投げ」のどれか。

    最推しのルーム = 貢献ランキングでいちばん多く送ったルーム。そのルームの 1 イベントで送った最大の量 (ガチイベの量) とくらべて、
    TSUKIAI より少なければ付き合い投げ (付き合いの古さは問わない)。
    """
    if uid not in cache:
        main = s.one("""SELECT room_id, SUM(point) AS total, MAX(point) AS best FROM contributions WHERE user_id=?
                        GROUP BY room_id ORDER BY total DESC LIMIT 1""", (uid,))
        cache[uid] = (main["room_id"], main["best"] or 0) if main is not None else (None, 0)
    main, best = cache[uid]
    if main == room_id:
        return "推し", main
    return ("付き合い" if best and amount < TSUKIAI * best else "掛け持ち"), main


def listener_rounds(s: Store, room_id: int) -> dict[str, Any]:
    """リス活: イベント中に、コアな人 (貢献上位・ファンレベルの高い常連) が、競争相手 (同じイベントのルーム)
    以外のルームにギフトを送って回ったか。そのイベントの貢献者を「推し・掛け持ち・付き合い投げ」に分け、
    回った先のルームが最推しの人 (お返しに来た人の目安) を数える。

    配信者本人は、アカウントが分かるルームと分からないルームがあるので、コアな人には入れず、別の数字にする
    (入れると、分かったルームだけ数字が大きくなり、ルームどうしでくらべられなくなるため)。
    """
    rid = int(room_id)
    owner = room_owner(s, rid)
    events = s.all("""SELECT re.event_id, e.name, e.start, e.end FROM room_events re JOIN events e USING(event_id)
                      WHERE re.room_id=? AND re.final=1 AND e.start IS NOT NULL AND e.end IS NOT NULL AND e.rooms_at >= e.end
                      ORDER BY e.end DESC LIMIT ?""", (rid, ROUND_EVENTS))
    cache: dict[int, tuple[Any, int]] = {}
    out = []
    for ev in events:
        eid = ev["event_id"]
        contrib = s.all("SELECT user_id, point FROM contributions WHERE event_id=? AND room_id=? ORDER BY point DESC", (eid, rid))
        ym = datetime.fromtimestamp(ev["end"], JST).strftime("%Y%m")
        fans = [r[0] for r in s.all("SELECT user_id FROM active_fans WHERE room_id=? AND ym<=? ORDER BY ym DESC, level DESC LIMIT ?",
                                    (rid, ym, ROUND_FANS))]
        core = [u for u in dict.fromkeys([r["user_id"] for r in contrib[:ROUND_CORE]] + fans) if u != owner]
        rivals = {r[0] for r in s.all("SELECT room_id FROM room_events WHERE event_id=?", (eid,))}

        def rounds(uid: int) -> list[Any]:
            rows = s.all("""SELECT c.room_id, SUM(c.point) AS points FROM contributions c JOIN events e USING(event_id)
                            WHERE c.user_id=? AND c.event_id != ? AND e.end > ? AND e.start < ? GROUP BY c.room_id""",
                         (uid, eid, ev["start"], ev["end"]))
            return [r for r in rows if r["room_id"] not in rivals and r["room_id"] != rid]

        visited: dict[int, int] = {}
        visitors = 0
        for uid in core:
            rooms = rounds(uid)
            visitors += bool(rooms)
            for r in rooms:
                visited[r["room_id"]] = visited.get(r["room_id"], 0) + r["points"]
        by_owner = {r["room_id"]: r["points"] for r in rounds(owner)} if owner else {}
        kinds = {"推し": 0, "掛け持ち": 0, "付き合い": 0}
        returned = owner_returned = 0
        for r in contrib:
            kind, main = _oshi(s, r["user_id"], rid, r["point"], cache)
            kinds[kind] += 1
            if kind != "推し" and main in visited:
                returned += 1
            elif kind != "推し" and main in by_owner:
                owner_returned += 1  # コアな人は回っていないが、配信者が回った先から来た
        out.append({"event_id": eid, "name": ev["name"], "end": ev["end"], "core": len(core), "visitors": visitors,
                    "rooms": len(visited), "points": sum(visited.values()), "returned": returned, "kinds": kinds,
                    "owner_rooms": len(by_owner) if owner else None, "owner_points": sum(by_owner.values()) if owner else None,
                    "owner_returned": owner_returned if owner else None})
    done = [e for e in out if e["core"]]
    return {"owner": bool(owner), "events": out,
            "avg_rooms": sum(e["rooms"] for e in done) / len(done) if done else None,
            "avg_returned": sum(e["returned"] for e in done) / len(done) if done else None,
            "avg_owner_rooms": sum(e["owner_rooms"] for e in out) / len(out) if owner and out else None,
            "kinds": {k: sum(e["kinds"][k] for e in out) for k in ("推し", "掛け持ち", "付き合い")}}


# ------------------------------------------------------------------ ルームのページ
def new_share(s: Store, room_id: int) -> dict[str, Any]:
    """直近のイベントで、このルームの前のどのイベントの貢献ランキングにも載っていなかった人の割合 (真ん中の値)。

    1 つ前のイベントだけとくらべると、前回たまたま 101 位より下だった常連も「新しく来た人」に入り、
    大きいルームほど高く出る。前のイベントを全部見れば、そういう常連はほとんど外れる。人数の割合だけを出す。
    """
    rid = int(room_id)
    events = [(r["event_id"], r["end"]) for r in _event_rows(s, rid, PROFILE_EVENTS + 1)][::-1]
    if len(events) < 2:
        return {"value": None, "events": 0}
    # このルームの前のイベントを room_events (主キーが room_id から) で引き、貢献ランキングは主キーで読む
    # (contributions を room_id で探すと、表を全部読むことになるため)
    seen = {r[0] for r in s.all("""SELECT DISTINCT c.user_id FROM room_events re JOIN events e USING(event_id)
                                   JOIN contributions c ON c.event_id = re.event_id AND c.room_id = re.room_id
                                   WHERE re.room_id=? AND e.end < ?""", (rid, events[0][1]))}
    shares = []
    for i, (eid, _) in enumerate(events):
        users = {r[0] for r in s.all("SELECT user_id FROM contributions WHERE event_id=? AND room_id=?", (eid, rid))}
        if i > 0 and users:
            shares.append(len(users - seen) / len(users))
        seen |= users
    return {"value": median(shares), "events": len(shares)}


def followers_growth(s: Store, room_id: int, now: float, days: int = 30) -> dict[str, Any] | None:
    rows = s.all("""SELECT day, followers FROM room_daily WHERE room_id=? AND followers IS NOT NULL
                    AND day >= strftime('%Y-%m-%d', ? + 9 * 3600, 'unixepoch') ORDER BY day""", (int(room_id), now - days * 86400))
    if len(rows) < 2:
        return None
    return {"from_day": rows[0]["day"], "to_day": rows[-1]["day"], "from": rows[0]["followers"], "to": rows[-1]["followers"],
            "change": rows[-1]["followers"] - rows[0]["followers"]}


def room_analysis(s: Store, room_id: int, now: float | None = None) -> dict[str, Any]:
    """ルームのページに出す分析 (その場で作る)。トップの人の名前は入れない。"""
    now = time.time() if now is None else now
    m = metrics(_event_rows(s, room_id))
    cuts = s.meta(CUTS_META)
    rtype = classify(m, cuts)
    why = None
    if m is None:
        why = f"貢献ランキングの分かった終わったイベントが {MIN_EVENTS} 件より少ない"
    elif rtype is None:
        why = "くらべる基準 (同じ規模のルームの数字) がまだできていない"
    styles = [dict(r) for r in s.all(
        """SELECT st.*, e.name, e.end FROM room_event_style st JOIN events e USING(event_id)
           WHERE st.room_id=? ORDER BY e.end DESC LIMIT 30""", (int(room_id),))]
    daily = [dict(r) for r in s.all("""SELECT day, followers, lives, view_max, CASE WHEN view_n > 0 THEN view_sum / view_n END AS view_avg
                                       FROM room_daily WHERE room_id=? ORDER BY day DESC LIMIT 60""", (int(room_id),))]
    depth = depth_label(m, cuts)
    return {"type": rtype, "type_note": TYPES.get(rtype or ""), "type_why": why, "metrics": m,
            "depth": depth, "depth_note": DEPTH_NOTES.get(depth or ""), "depths": DEPTH_NOTES,
            "size_name": SIZE_NAMES[m["size_class"]] if m else None,
            "momentum": momentum(s, room_id, now), "style": room_style(s, room_id), "event_styles": styles,
            "tactics": tactics(s, room_id), "rounds": listener_rounds(s, room_id), "regulars": regulars(s, room_id),
            "new_share": new_share(s, room_id), "followers": followers_growth(s, room_id, now), "daily": daily[::-1],
            "types": TYPES, "styles": STYLES, "caution": CAUTION}


# ------------------------------------------------------------------ 1 日 1 回の作り直し
STYLE_VERSION = 5  # 5: 1 位の入れ替わり・序盤の引き離し・相手の失速も残す。 4: 狙った時刻から離れた回を使わない・参加ルームの少ないイベントを除く・追い込みを期間の長さでくらべる。 走り方の出し方を変えたら上げる (前に作った分を作り直す。3: 序盤にポイントの無い回を使わない)


def step(s: Store, now: float) -> bool:
    """作り直しの小さな仕事を 1 つ行う (SHOWROOM には問い合わせない)。することが無ければ False。"""
    if s.meta("style_version", 1) != STYLE_VERSION:
        with s.tx() as c:
            c.execute("UPDATE events SET style_at=NULL WHERE style_at IS NOT NULL")
            c.execute("DELETE FROM meta WHERE k IN ('room_profile_at', 'room_profile_run')")
        s.set_meta("style_version", STYLE_VERSION)
    # 終わったイベントの走り方 (推移を残したもので、まだ作っていないもの)
    # (終わった時の最終結果を読んでから。読めないままなら STYLE_WAIT 秒で、あるだけで作る)
    e = s.one("""SELECT event_id FROM events WHERE progress_at IS NOT NULL AND end IS NOT NULL AND end < ? AND style_at IS NULL
                   AND (rooms_at >= end OR end + ? < ?) ORDER BY end LIMIT 1""", (now, STYLE_WAIT, now))
    if e is not None:
        rows = event_styles(s, e["event_id"]) or []
        with s.tx() as c:
            c.execute("DELETE FROM room_event_style WHERE event_id=?", (e["event_id"],))
            c.executemany("""INSERT INTO room_event_style(event_id, room_id, style, early, mid, late, final, last_day_share, climb,
                             lead_share, swaps, early_gap, rival_slow) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", rows)
            c.execute("UPDATE events SET style_at=? WHERE event_id=?", (now, e["event_id"]))
        return True
    # 走り方を作り終えて日がたったイベントの推移を間引く (残すのは、作り直しと終盤の分析に使う回)
    e = s.one("""SELECT event_id FROM events WHERE style_at IS NOT NULL AND thinned_at IS NULL AND end < ?
                 ORDER BY end LIMIT 1""", (now - PROGRESS_THIN_AFTER,))
    if e is not None:
        s.thin_progress(e["event_id"], now)
        return True
    if s.meta("room_stats_upto", 0) is not True:
        return False  # 貢献ランキングの数字を作り終えてから
    state = s.meta("room_profile_run") or {}
    if not state:
        if now - (s.meta("room_profile_at") or 0) < PROFILE_EVERY:
            return False
        state = {"next": 0, "started": now}
    rooms = [r[0] for r in s.all("""SELECT DISTINCT room_id FROM room_events WHERE room_id > ? AND final=1
                                    ORDER BY room_id LIMIT ?""", (state["next"], PROFILE_CHUNK))]
    cuts = s.meta(CUTS_META)
    out = []
    for rid in rooms:
        m = metrics(_event_rows(s, rid))
        mo = momentum(s, rid, now)
        st = room_style(s, rid)
        if m is None and mo["label"] is None and st["style"] is None:
            continue
        m = m or {}
        out.append((rid, now, m.get("events"), m.get("size_class"), m.get("top1"), m.get("top3"), m.get("contributors"),
                    int(bool(m.get("capped"))), m.get("rel_top10"), m.get("rel_per_contrib"), m.get("rel_point"),
                    classify(m or None, cuts), mo["value"], mo["recent_place"], mo["prev_place"], st["style"], st["events"],
                    json.dumps(st["counts"], ensure_ascii=False), m.get("depth_pt"), m.get("depth_rel"), depth_label(m or None, cuts),
                    m.get("below_pt"), m.get("below_share"), mo["label"]))
    with s.tx() as c:
        c.executemany("""INSERT OR REPLACE INTO room_profile(room_id, at, events, size_class, top1, top3, contributors, capped,
                           rel_top10, rel_per_contrib, rel_point, type, momentum, recent_place, prev_place, style, style_events,
                           style_counts, depth_pt, depth_rel, depth, below_pt, below_share, momentum_label)
                         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", out)
    if len(rooms) == PROFILE_CHUNK:
        s.set_meta("room_profile_run", {**state, "next": rooms[-1]})
        return True
    # 全部のルームを見終えた: 区切りの値を作り直し、タイプを付け直す。古い (今回出てこなかった) ルームは消す
    profiles = [dict(r) for r in s.all("SELECT * FROM room_profile WHERE at >= ?", (state["started"],))]
    cuts = build_cuts(profiles)
    with s.tx() as c:
        c.execute("DELETE FROM room_profile WHERE at < ?", (state["started"],))
        c.executemany("UPDATE room_profile SET type=?, depth=? WHERE room_id=?",
                      [(classify(p if p["top1"] is not None else None, cuts),
                        depth_label(p if p["top1"] is not None else None, cuts), p["room_id"]) for p in profiles])
        c.execute("INSERT INTO meta(k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (CUTS_META, json.dumps(cuts)))
        c.execute("DELETE FROM meta WHERE k='room_profile_run'")
        c.execute("INSERT INTO meta(k, v) VALUES ('room_profile_at', ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (json.dumps(now),))
    return True


def profile_list(s: Store, rtype: str | None = None, style: str | None = None, order: str = "momentum",
                 limit: int = 300, depth: str | None = None) -> dict[str, Any]:
    """ルームの一覧に出す、タイプ・勢い・戦術 (1 日 1 回の分)。"""
    where, args = ["1=1"], []
    if rtype:
        where.append("p.type=?")
        args.append(rtype)
    if style:
        where.append("p.style=?")
        args.append(style)
    if depth:
        where.append("p.depth=?")
        args.append(depth)
    by = {"momentum": "p.momentum IS NULL, p.momentum DESC", "place": "p.recent_place IS NULL, p.recent_place",
          "depth": "p.depth_rel IS NULL, p.depth_rel DESC", "below": "p.below_share IS NULL, p.below_share DESC"}.get(order)
    if by is None:
        raise ValueError("order は momentum・place・depth・below のどれかです")
    rows = s.all(f"""SELECT p.*, r.key, COALESCE(r.name, r.key, p.room_id) AS name, r.followers FROM room_profile p
                     LEFT JOIN rooms r USING(room_id) WHERE {' AND '.join(where)} ORDER BY {by}, p.room_id LIMIT ?""",
                 (*args, int(limit)))
    total = s.scalar(f"SELECT COUNT(*) FROM room_profile p WHERE {' AND '.join(where)}", args)
    counts = {r[0]: r[1] for r in s.all("SELECT type, COUNT(*) FROM room_profile WHERE type IS NOT NULL GROUP BY type")}
    style_counts = {r[0]: r[1] for r in s.all("SELECT style, COUNT(*) FROM room_profile WHERE style IS NOT NULL GROUP BY style")}
    depth_counts = {r[0]: r[1] for r in s.all("SELECT depth, COUNT(*) FROM room_profile WHERE depth IS NOT NULL GROUP BY depth")}
    return {"at": s.meta("room_profile_at"), "total": total, "type_counts": counts, "style_counts": style_counts,
            "depth_counts": depth_counts, "types": TYPES, "styles": STYLES, "depths": DEPTH_NOTES, "caution": CAUTION,
            "rooms": [{k: r[k] for k in ("room_id", "key", "name", "followers", "type", "style", "style_events", "momentum",
                                         "recent_place", "prev_place", "events", "top1", "contributors", "depth",
                                         "depth_pt", "depth_rel", "below_pt", "below_share", "momentum_label")} for r in rows]}


# ------------------------------------------------------------------ コメントとギフトの釣り合い (記録しているルームだけ)
BALANCE_DAYS = 30
BALANCE_LIVES = 3  # これより少ない配信では「判定できない」
GIFT_HEAVY = 0.3  # 有料ギフトを送った人 ÷ コメントした人 がこれ以上なら「ギフトが多め」
COMMENT_HEAVY = 0.05  # これ以下なら「コメントが多め」
BALANCE_NOTES = {"ギフト寄り": "コメントは少ないが、有料ギフトを送る人が多い", "コメント寄り": "コメントは多いが、有料ギフトを送る人は少ない",
                 "釣り合っている": "コメントと有料ギフトの人数の割合が、ふつうくらい"}


def balance(state_dir: Any, room_key: str, now: float | None = None, days: int = BALANCE_DAYS) -> dict[str, Any] | None:
    """記録した配信ごとの、コメントの数とギフトの数。記録していないルームは None。"""
    from . import comment_counts, listing

    now = time.time() if now is None else now
    counts = [x for x in comment_counts.load(state_dir, room_key) if (x.get("last") or 0) >= now - days * 86400]
    if not counts:
        return None
    catalog = (listing._read_catalog(state_dir) or ({}, {}))[0]
    files = listing._record_files(state_dir)
    gifts: dict[Any, dict[str, Any]] = {}
    for g in listing._load_file(files[room_key]) if room_key in files else []:
        if g["at"] < now - (days + 1) * 86400:
            continue
        b = gifts.setdefault(g.get("live_id"), {"gifts": 0, "paid": 0, "paid_points": 0, "gifters": set(), "paid_gifters": set()})
        pt, free = listing._points(catalog, g)
        b["gifts"] += 1
        b["gifters"].add(g.get("user_id"))
        if not free:
            b["paid"] += 1
            b["paid_points"] += pt
            b["paid_gifters"].add(g.get("user_id"))
    lives = []
    for c in counts:
        b = gifts.get(c.get("live_id")) or {"gifts": 0, "paid": 0, "paid_points": 0, "gifters": set(), "paid_gifters": set()}
        lives.append({"live_id": c.get("live_id"), "first": c.get("first"), "last": c.get("last"), "comments": c.get("comments", 0),
                      "numeric": c.get("numeric", 0), "commenters": c.get("users", 0), "partial": bool(c.get("partial")),
                      "gifts": b["gifts"], "paid": b["paid"], "paid_points": b["paid_points"], "gifters": len(b["gifters"]),
                      "paid_gifters": len(b["paid_gifters"])})
    commenters = sum(x["commenters"] for x in lives)
    paid_gifters = sum(x["paid_gifters"] for x in lives)
    ratio = paid_gifters / commenters if commenters else None
    label = None
    if len(lives) >= BALANCE_LIVES and ratio is not None:
        label = "ギフト寄り" if ratio >= GIFT_HEAVY else "コメント寄り" if ratio <= COMMENT_HEAVY else "釣り合っている"
    talk = sum(x["comments"] - x["numeric"] for x in lives)
    return {"days": days, "lives": lives[::-1], "label": label, "ratio": ratio, "label_note": BALANCE_NOTES.get(label or ""),
            "per_live": {"comments": talk / len(lives), "numeric": sum(x["numeric"] for x in lives) / len(lives),
                         "commenters": commenters / len(lives), "paid_gifters": paid_gifters / len(lives),
                         "paid_points": sum(x["paid_points"] for x in lives) / len(lives)},
            "partial": sum(x["partial"] for x in lives)}



# ------------------------------------------------------------------ 誘い投げ (記録しているルームだけ)
LURE_CORE = 5  # 有料ギフトの多い順に、何人を「コアな常連」とみるか (アクティブファンのレベルの高い人も足す)
LURE_FANS = 10  # アクティブファンのレベルの高い何人を、コアな常連に足すか
LURE_SMALL = 1000  # 少額ギフト: 1 回に送った有料ギフトがこれ以下 (1 SG = 1 円なので 1000 円以下)
LURE_WINDOW = 180  # 誘い投げの前後をくらべる時間 (秒)
RELAY_GAP = 60  # リレー: 有料ギフトがこの秒数より短い間隔で、ちがう人へと続いたら、ひと続きとみる
RELAY_HANDS = 2  # リレー: ひと続きの中で、送る人が何回以上替わったら「リレー」とみるか (A→B→A・A→B→C から)
RESPOND_WINDOW = 30  # 呼応: ある人の有料ギフトのあと、この秒数のうちに、ほかの人が有料ギフトを送ったら「呼応した」とみる
RESPOND_BIG = 1000  # 呼応: 1 回にこのポイント以上の有料ギフトを「大きなギフト」とみる
MIRROR_BALL = 800003  # ミラーボール (500 pt)。リレーの定番のギフト


def lure_throws(state_dir: Any, room_key: str, now: float | None = None, days: int = BALANCE_DAYS,
                store: Store | None = None, room_id: int | None = None) -> dict[str, Any] | None:
    """誘い投げ: コアな常連が、ギフトが飛ぶ空気を作るために、少額の有料ギフトをこまめに・交互に送り、盛り上げたか。

    記録した 1 回ずつのギフトとコメントの数から出す (名前と ID は出さない)。
    - 誘い投げ = コアな常連の少額の有料ギフト (1 回 LURE_SMALL 円以下)
    - 盛り上がり = 同じ配信の中で、誘い投げの「後の 3 分」と「前の 3 分」をくらべる (配信の流れで自然に増え減りする分を除くため)。
      ほかの人のギフト (無料も含む) を送った人数・有料のポイント・コメントの数
    - こまめさ = 配信 1 時間あたりの誘い投げの回数と、間隔の真ん中の値
    - リレー・呼応 = relay_and_respond (常連に限らず、すべての人の有料ギフトで見る)
    """
    from . import comment_counts, listing

    now = time.time() if now is None else now
    files = listing._record_files(state_dir)
    if room_key not in files:
        return None
    catalog = (listing._read_catalog(state_dir) or ({}, {}))[0]
    gifts = []
    for g in listing._load_file(files[room_key]):
        if g["at"] < now - days * 86400 or g.get("user_id") is None:
            continue
        pt, free = listing._points(catalog, g)
        gifts.append((g["at"], g.get("user_id"), pt, free, g.get("live_id"), g.get("gift_id")))
    if not gifts:
        return None
    gifts.sort()
    totals: dict[Any, int] = {}
    for _, uid, pt, free, *_ in gifts:
        if not free:
            totals[uid] = totals.get(uid, 0) + pt
    core = set(sorted(totals, key=lambda u: -totals[u])[:LURE_CORE])
    if store is not None and room_id is not None:  # ポイントに出ない常連 (ファンレベルの高い人) も入れる
        core |= {r[0] for r in store.all("""SELECT user_id FROM active_fans WHERE room_id=? AND ym=(SELECT MAX(ym) FROM active_fans
                                            WHERE room_id=?) ORDER BY level DESC LIMIT ?""", (int(room_id), int(room_id), LURE_FANS))}
    lures = [g for g in gifts if g[1] in core and not g[3] and 0 < g[2] <= LURE_SMALL]
    others = [g for g in gifts if g[1] not in core]
    minutes: dict[Any, dict[int, int]] = {x.get("live_id"): {int(k): v for k, v in (x.get("minutes") or {}).items()}
                                          for x in comment_counts.load(state_dir, room_key)}

    def window(live: Any, a: float, b: float) -> tuple[int, int, int]:
        got = [g for g in others if g[4] == live and a <= g[0] < b]
        cm = sum(v for m, v in minutes.get(live, {}).items() if a <= m < b)
        return len({g[1] for g in got}), sum(g[2] for g in got if not g[3]), cm

    before = [0, 0, 0]
    after = [0, 0, 0]
    for t, _, _, _, live, _ in lures:
        for acc, w in ((before, window(live, t - LURE_WINDOW, t)), (after, window(live, t + 1, t + 1 + LURE_WINDOW))):
            for i in range(3):
                acc[i] += w[i]
    ratio = [(a / b) if b else None for a, b in zip(after, before)]
    # こまめさ: 配信ごとの長さ (最初と最後のギフトのあいだ) あたりの回数と、同じ配信の中の誘い投げの間隔
    spans: dict[Any, list[float]] = {}
    for t, _, _, _, live, _ in gifts:
        spans.setdefault(live, []).append(t)
    hours = sum((max(v) - min(v)) / 3600 for v in spans.values())
    gaps = [b[0] - a[0] for a, b in zip(lures, lures[1:]) if a[4] == b[4]]
    return {"days": days, "core": len(core), "lures": len(lures), "per_hour": len(lures) / hours if hours else None,
            "gap": median(gaps), "window": LURE_WINDOW, **relay_and_respond(gifts, core),
            "visits": _visits(state_dir, room_key, now - days * 86400, lures),
            "people_ratio": ratio[0], "points_ratio": ratio[1], "comments_ratio": ratio[2],
            "people_before": before[0], "people_after": after[0], "has_comments": any(minutes.values())}


def _visits(state_dir: Any, room_key: str, since: float, lures: list[tuple[Any, ...]]) -> dict[str, Any] | None:
    """来場・ファンレベルの知らせ (live_events): 1 配信あたりの来場 (初めての人)・ファンレベルが上がった人と、
    誘い投げの前後 LURE_WINDOW 秒の来場の数。知らせを残し始める前の配信は数えない。"""
    from . import live_events

    evs = [e for e in live_events.load(state_dir, room_key, since) if e.get("kind") in ("visit", "fan_level")]
    if not evs:
        return None
    lives = {e.get("live_id") for e in evs}
    visits = [e for e in evs if e["kind"] == "visit"]
    levels = [e for e in evs if e["kind"] == "fan_level"]
    first = sum(1 for e in visits if e.get("n") == 1)
    before = after = 0
    for t, *_rest in lures:
        live = _rest[3]
        before += sum(1 for e in visits if e.get("live_id") == live and t - LURE_WINDOW <= e["at"] < t)
        after += sum(1 for e in visits if e.get("live_id") == live and t < e["at"] <= t + LURE_WINDOW)
    n = len(lives)
    return {"lives": n, "visits": len(visits) / n, "first": first / n, "levels": len(levels) / n,
            "regular": sum(1 for e in visits if (e.get("n") or 0) >= 10) / n,  # 10 回目より多く来ている人
            "streak": sum(1 for e in levels if (e.get("streak") or 0) >= 3) / n,  # 3 か月以上続けてレベルを上げている人
            "before": before, "after": after, "ratio": after / before if before else None}


def relay_and_respond(gifts: list[tuple[Any, ...]], core: set[Any]) -> dict[str, Any]:
    """有料ギフトの流れ: リレー (何人かで続けて送る) と、呼応 (だれかのギフトに、ほかの人が応える)。

    gifts は (時刻, ユーザー, ポイント, 無料か, 配信, ギフトの番号) を時刻の順に並べたもの。常連かどうか・人数は問わない。
    - リレー: 同じ配信で、有料ギフトが RELAY_GAP 秒より短い間隔で続き、送る人が RELAY_HANDS 回以上替わったひと続き。
      何人で回したか・最初に送ったのがコアな常連か・すべての有料ポイントのうちリレーの中で送られた割合
    - 呼応: 有料ギフトのあと RESPOND_WINDOW 秒のうちに、ほかの人が有料ギフトを送った割合。送ったギフトの種類
      (コアな常連の少額 = 誘い投げ・大きなギフト・そのほか) ごとに出し、「いつもの割合」(配信中の 10 秒ごとの時刻から、
      RESPOND_WINDOW 秒のうちにだれかが有料ギフトを送った割合) とくらべる
    - 歓迎: コアな常連でない人 (応援に来た人) の有料ギフトに、RESPOND_WINDOW 秒のうちにコアな常連が有料ギフトで応えた割合。
      いつもの割合 (どの時刻でも RESPOND_WINDOW 秒のうちにコアな常連が送る割合) とくらべる
    - ミラーボール: 送られた数と、そのうちリレーの中で送られた割合、ミラーボールが 2 回以上入ったリレーの数
    """
    paid = [g for g in gifts if not g[3] and g[2] > 0]
    relays: list[list[tuple[Any, ...]]] = []
    run: list[tuple[Any, ...]] = []
    for g in paid + [None]:  # type: ignore[list-item]
        if g is not None and run and g[4] == run[-1][4] and g[0] - run[-1][0] < RELAY_GAP:
            run.append(g)
            continue
        if sum(1 for a, b in zip(run, run[1:]) if a[1] != b[1]) >= RELAY_HANDS:
            relays.append(run)
        run = [g] if g is not None else []
    people = [len({g[1] for g in r}) for r in relays]
    balls = [g for g in paid if g[5] == MIRROR_BALL]
    in_relay = {id(g) for r in relays for g in r}
    total = sum(g[2] for g in paid)

    kinds: dict[str, list[int]] = {"誘い投げ": [0, 0], "大きなギフト": [0, 0], "そのほか": [0, 0]}
    welcome = [0, 0]
    lives: dict[Any, list[tuple[Any, ...]]] = {}
    for g in paid:
        lives.setdefault(g[4], []).append(g)
    spans: dict[Any, list[float]] = {}
    for x in gifts:
        span = spans.setdefault(x[4], [x[0], x[0]])
        span[0], span[1] = min(span[0], x[0]), max(span[1], x[0])
    base = [0, 0]
    core_base = 0
    for live, rows in lives.items():
        core_times = [g[0] for g in rows if g[1] in core]
        times = [g[0] for g in rows]
        for i, g in enumerate(rows):
            kind = "誘い投げ" if g[1] in core and g[2] <= LURE_SMALL else "大きなギフト" if g[2] >= RESPOND_BIG else "そのほか"
            answers = [h[1] for h in rows[i + 1:] if g[0] < h[0] <= g[0] + RESPOND_WINDOW and h[1] != g[1]]
            kinds[kind][0] += 1
            kinds[kind][1] += bool(answers)
            if g[1] not in core:
                welcome[0] += 1
                welcome[1] += any(u in core for u in answers)
        # いつもの割合: 配信の中 (ほかのギフトも含めた最初と最後のあいだ) を 10 秒ごとに
        t, j, k = spans[live][0], 0, 0
        while t <= spans[live][1]:
            while j < len(times) and times[j] <= t:
                j += 1
            while k < len(core_times) and core_times[k] <= t:
                k += 1
            base[0] += 1
            base[1] += j < len(times) and times[j] - t <= RESPOND_WINDOW
            core_base += k < len(core_times) and core_times[k] - t <= RESPOND_WINDOW
            t += 10
    return {"relays": len(relays), "relay_people_avg": sum(people) / len(people) if people else None,
            "relay_people_max": max(people, default=0), "relay_longest": max((len(r) for r in relays), default=0),
            "relay_share": sum(g[2] for r in relays for g in r) / total if total else None,
            "relay_core_start": sum(1 for r in relays if r[0][1] in core) / len(relays) if relays else None,
            "relay_gap": RELAY_GAP, "respond_window": RESPOND_WINDOW, "respond_big": RESPOND_BIG,
            "respond": {k: {"n": v[0], "rate": v[1] / v[0] if v[0] else None} for k, v in kinds.items()},
            "respond_base": base[1] / base[0] if base[0] else None,
            "welcome": {"n": welcome[0], "rate": welcome[1] / welcome[0] if welcome[0] else None,
                        "base": core_base / base[0] if base[0] else None},
            "mirror": {"n": len(balls), "in_relay": sum(1 for g in balls if id(g) in in_relay) / len(balls) if balls else None,
                       "relays": sum(1 for r in relays if sum(1 for g in r if g[5] == MIRROR_BALL) >= 2)}}
