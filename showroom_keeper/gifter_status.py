"""ギフターのステータス (ゲームのキャラクターのステータスのように、戦力を項目ごとの数字にする)。

考え方は project の資料「ギフターのステータス設計」(2026-10-20) と `docs/分析の見方.md` の「ステータス」。ここで作るのは、
全ギフターについて貢献ランキング・月の量から出せる項目 (作る順番の 1)。記録しているルームのギフトからしか分からない項目
(戦術・速さ・隠密・誘発・1 手の大きさ) は、まだ作らない。

- 財布: HP (月の予算)・残弾 (今月まだ出せる量)・火力 (勝負の時に 1 イベント × 1 ルームに出せる量)・最大出力・持久力・回復力
- 腕: 知性 (届かない勝負に弾を捨てない)・命中 (出した回にルームが目標に届いたか)
- 心: 勢い・疲労
- 経歴: 実績・経験値とレベル・キャリア・掛け持ちの広さ
- 相性: 大会での強さ
- 状態異常: 上り調子・安定・温存中・枯渇・離脱・疲労・新顔
- 総合力 = min(火力, 残弾) × 状態の係数 × 腕の係数。全体の順位と S〜D のランク
- ルームとの関係 (いちばん多く送ったルームから ROOMS ルーム): 忠誠心・付き合いの種類・出動率・士気・友好度・実績・ルームでの戦力とクラス

軽くするための決まり:
- 作るのは、直近 12 か月のどこかの月で、その月の中堅の下限 (上から 10%) 以上を出した人だけ (パワーギフター)
- 1 日 1 回、サーバーが暇な時だけ (room_parts と同じ決まり)、1 分に STEP_USERS 人ずつ。人で引く索引で読み、表を全部は読まない
- 勝負になった回 (event_rounds) は、終わったイベントを 1 回ずつ作って残す (作り直さない)
- ページは作っておいた表を読むだけ。SHOWROOM への問い合わせはしない
どの線も仮の値で、データを見て決め直す。貢献ランキングは各ルームの上位 100 人までなので、どの量も「少なくとも」の量。
"""

from __future__ import annotations

import array
import bisect
import json
import logging
import threading
import time
from datetime import datetime
from pathlib import Path
from statistics import median
from typing import Any

from . import memory, psyche
from .gifter_analysis import _add, last_month, month_cuts
from .status import JST

log = logging.getLogger(__name__)

VERSION = 1  # 作る中身を変えたら上げる (前に作った分を消して作り直す。上げると、作り直し終えるまで一覧が空になる)
FORMULA = 2  # 式の版。上げても前の分は消さず、毎日の作り直しで 1 人ずつ差し替える (画面に「作り直し待ち」と出す)。
# 2 (2026-10-23): ファンの名簿が全員分でない時は離脱・枯渇にしない (不明)、火力などは終わった回だけでくらべる
EVERY = 86400  # 作り直す間隔 (秒)
TICK = 60  # 暇かどうかを見て、少しずつ作る間隔 (秒)
STEP_USERS = 200  # 1 回に作る人数
STEP_EVENTS = 300  # 1 回に勝負の回を作るイベントの数
MONTHS = 12  # 財布・腕・心を見る月数
LONG_MONTHS = 24  # 実績・経験値を見る月数 (勝負の回は、この月数より前のイベントは作らない)
SETTLE = 2 * 86400  # 終わってからこれだけたったイベントの順位で、勝負の回を作る (最後の順位が入るのを待つ)
ROUND_RANKS = 10  # 勝負の回を残す順位 (特典の順位がこれより下なら、その 1 つ下まで)
CLOSE = 0.10  # 特典の境目で、隣の順位との差がポイントのこれだけ以内なら「勝負になった回」
SORTIE = 0.5  # 出動 = その回に、火力のこれだけ以上を出した
LOW = 0.3  # 月の量が予算のこれだけ以下なら「使い切った月」(回復力)。直近 3 か月が多かった 3 か月のこれだけ以下なら温存中か枯渇
RISING = 1.5  # 直近 3 か月が前の 3 か月のこれだけ倍以上なら「上り調子」
FATIGUE_DAYS = 30  # 疲労を見る日数
MIN_ROUNDS = 3  # 知性・命中・出動率を決める、勝負の回の数の下限
NEW_MONTHS = 3  # 最初に見えてからこれだけの月のうちは「新顔」
TSUKIAI = 0.2  # 最推しのルームの 1 イベントの最大の、これより少なければ「付き合い投げ」(room_analysis と同じ線)
ROOMS = 5  # ルームとの関係を出すルームの数
LEVELS = (1, 3, 10, 30, 100, 300)  # 経験値 (勝負の回の数) のレベルの段。これだけ以上で 1 つ上がる
GRADES = ((0.01, "S"), (0.05, "A"), (0.20, "B"), (0.50, "C"))  # 総合力の上から何割までがどのランクか (それより下は D)
# 総合力に掛ける (ほかは 1.0)。不明 = ファンの名簿が全員分でないなどで、離脱か枯渇か温存中かを決められない (仮に間の値)
STATE_FACTOR = {"枯渇": 0.2, "離脱": 0.0, "上り調子": 1.1, "不明": 0.5}
FATIGUE_FACTOR = 0.6  # 疲労している時に総合力に掛ける
# 火力の大きさの目安 (1 手 300 万 = 10 万 SG × 10 連。`docs/戦術メモ.md` の 2)
SCALE = ((3_000_000, "300 万級"), (1_000_000, "100 万級"), (300_000, "30 万級"), (100_000, "10 万級"))
# 1〜100 の評価を出す項目 (多いほど上。low は少ないほど上)
SCORED = ("hp", "mp", "fire", "max_out", "stamina", "recovery", "intel", "hit", "momentum", "fatigue", "achievements",
          "exp", "career", "spread", "contest", "overall")
LOW_IS_GOOD = {"recovery", "fatigue"}
CUTS_META = "gifter_status_cuts"
RUN_META = "gifter_status_run"


# ------------------------------------------------------------------ 勝負の回
def build_rounds(s: Any, now: float, limit: int = STEP_EVENTS) -> int:
    """終わったイベントの、上の方の順位と勝負の回を作る (まだ作っていないイベントを limit 件まで)。作った件数。"""
    since = _month_start(_add(last_month(now), -(LONG_MONTHS - 1)))
    todo = [r[0] for r in s.all("""SELECT event_id FROM events WHERE end BETWEEN ? AND ?
                                   AND event_id NOT IN (SELECT event_id FROM event_rounds_done) ORDER BY end LIMIT ?""",
                                (since, now - SETTLE, limit))]
    out: list[tuple[Any, ...]] = []
    done = []
    for eid in todo:
        e = s.one("SELECT end FROM events WHERE event_id=?", (eid,))
        info = s.one("SELECT reward_rank FROM event_info WHERE event_id=?", (eid,))
        reward = int(info["reward_rank"]) if info is not None and info["reward_rank"] else None
        st = s.one("SELECT stage_no FROM event_series WHERE event_id=? AND series_id IS NOT NULL", (eid,))
        stage = bool(st is not None and (st["stage_no"] or 0) > 1)
        rows = s.all("SELECT room_id, rank, point FROM room_events WHERE event_id=? AND point > 0", (eid,))
        ranked = sorted(rows, key=lambda r: (r["rank"] is None, r["rank"] or 0, -(r["point"] or 0)))
        pts = [r["point"] for r in ranked]
        targets = {1} | ({reward} if reward else set())
        keep = max(ROUND_RANKS, (reward or 0) + 1)
        for i, r in enumerate(ranked[:keep]):
            n, p = i + 1, pts[i]
            up = pts[i - 1] - p if i > 0 else None
            down = p - pts[i + 1] if i + 1 < len(pts) else None
            gaps = [g for g in (up, down) if g is not None]
            gap = min(gaps) if gaps else None
            edge = any((n == t and down is not None and down <= CLOSE * p) or (n == t + 1 and up is not None and up <= CLOSE * p)
                       for t in targets)
            out.append((eid, r["room_id"], e["end"], n, p, gap, 1 if edge or stage else 0, 1 if n <= (reward or 1) else 0))
        done.append((eid, e["end"], now))
    if done:
        with s.tx() as c:
            c.executemany("INSERT OR REPLACE INTO event_rounds VALUES (?, ?, ?, ?, ?, ?, ?, ?)", out)
            c.executemany("INSERT OR REPLACE INTO event_rounds_done VALUES (?, ?, ?)", done)
            old = since - 31 * 86400
            c.execute("DELETE FROM event_rounds WHERE event_id IN (SELECT event_id FROM event_rounds_done WHERE end < ?)", (old,))
            c.execute("DELETE FROM event_rounds_done WHERE end < ?", (old,))
    return len(done)


# ------------------------------------------------------------------ 1 人のステータス
def _month_start(month: str) -> float:
    return datetime.strptime(month + "-01", "%Y-%m-%d").replace(tzinfo=JST).timestamp()


def _month_of(t: float) -> str:
    return datetime.fromtimestamp(t, JST).strftime("%Y-%m")


def _second(values: list[int]) -> int:
    v = sorted(values, reverse=True)
    return v[1] if len(v) > 1 else (v[0] if v else 0)


def _ratio(a: float, b: float) -> float | None:
    return round(a / b, 3) if b else None


def compute(s: Any, uid: int, now: float, edges: dict[int, int] | None = None,
            cache: dict[Any, Any] | None = None) -> dict[str, Any] | None:
    """1 人のステータス (表に入れる形)。貢献ランキングに出ていなければ None。edges はルームの決め手の量、
    cache は心のカルテのルームごとの行を、同じ回の人どうしで使い回す入れ物。"""
    uid = int(uid)
    month = last_month(now)  # 集め終わったいちばん新しい月
    cur = _add(month, 1)
    first12 = _add(month, -(MONTHS - 1))
    since12, since24 = _month_start(first12), _month_start(_add(month, -(LONG_MONTHS - 1)))
    rows = s.all("""SELECT c.event_id, c.room_id, c.point, c.rank, e.end, r.battle, r.reached, d.event_id IS NOT NULL AS judged,
                           es.stage_no, re.final
                    FROM contributions c JOIN events e USING(event_id)
                    LEFT JOIN room_events re ON re.event_id=c.event_id AND re.room_id=c.room_id
                    LEFT JOIN event_rounds r ON r.event_id=c.event_id AND r.room_id=c.room_id
                    LEFT JOIN event_rounds_done d ON d.event_id=c.event_id
                    LEFT JOIN event_series es ON es.event_id=c.event_id AND es.series_id IS NOT NULL
                    WHERE c.user_id=? AND c.point > 0""", (uid,))
    if not rows:
        return None
    first24 = _add(month, -(psyche.MONTHS - 1))
    totals = {r["month"]: r["points"] for r in s.all("SELECT month, points FROM user_month WHERE user_id=? AND month>=?",
                                                       (uid, min(first12, first24)))}
    months = [totals.get(_add(month, -k), 0) for k in range(MONTHS - 1, -1, -1)]  # 古い順、month まで
    active = [v for v in months if v]
    budget = int(median(active)) if active else 0
    spent = totals.get(cur, 0)
    left = max(0, budget - spent)
    r12 = [r for r in rows if (r["end"] or 0) >= since12]
    r24 = [r for r in rows if (r["end"] or 0) >= since24]
    # 火力・最大出力・持久力・ルームの上限 (cap) は、終わった回だけでくらべる (終わって SETTLE たち、終わったあとの貢献ランキングを
    # 取り終えた回)。開催中の途中の値が入ると、小さい値で火力が下がるため。残弾・疲労・送り先の割合は、開催中の分も入れたまま
    d12 = [r for r in r12 if r["final"] and (r["end"] or 0) <= now - SETTLE]
    battle12 = [r for r in d12 if r["battle"]]
    fire_basis = "battle" if len(battle12) >= 2 else "all"
    fire_pool = battle12 if fire_basis == "battle" else d12
    fire = _second([r["point"] for r in fire_pool])
    max_out = max((r["point"] for r in d12), default=0)

    # 持久力: 1 か月に、火力の半分以上を出したイベントの数 (送った月の中ほど)
    per_month: dict[str, int] = {}
    for r in d12:
        if fire and r["point"] >= SORTIE * fire:
            m = _month_of(r["end"])
            per_month[m] = per_month.get(m, 0) + 1
    stamina = median([per_month.get(_add(month, -k), 0) for k in range(MONTHS) if months[MONTHS - 1 - k]]) if active else None

    # 回復力: 使い切った月 (予算の 3 割以下) のあと、予算に戻るまでの月数 (中ほど)
    waits = []
    for i in range(1, len(months)):
        if budget and months[i] <= LOW * budget and months[i - 1] > LOW * budget:
            for j in range(i + 1, len(months)):
                if months[j] >= budget:
                    waits.append(j - i)
                    break
    recovery = median(waits) if waits else None

    # 知性 (届かない勝負に弾を捨てない) と 命中 (出した回にルームが目標に届いたか)。勝負を作ったイベントの分だけ
    b_judged = [r for r in battle12 if r["judged"]]
    intel = (round(1 - sum(r["point"] for r in b_judged if not r["reached"]) / sum(r["point"] for r in b_judged), 3)
             if len(b_judged) >= MIN_ROUNDS and sum(r["point"] for r in b_judged) else None)
    shots = [r for r in d12 if r["judged"] and fire and r["point"] >= SORTIE * fire]
    hit = round(sum(1 for r in shots if r["reached"]) / len(shots), 3) if len(shots) >= MIN_ROUNDS else None

    # 勢い・疲労
    recent, before = sum(months[-3:]), sum(months[-6:-3])
    peak3 = max((sum(months[i:i + 3]) for i in range(len(months) - 2)), default=0)
    momentum = _ratio(recent, before)
    new_high = bool(d12) and max((r["point"] for r in d12 if r["end"] >= _month_start(_add(month, -2))), default=0) >= max_out > 0
    tired_pt = sum(r["point"] for r in rows if (r["end"] or 0) >= now - FATIGUE_DAYS * 86400)
    fatigue = _ratio(tired_pt, budget)

    # 経歴
    first_seen = min(r["end"] for r in rows if r["end"])
    career = max(0, (now - first_seen) / (30.4 * 86400))
    battle24 = [r for r in r24 if r["battle"]]
    exp = len(battle24)
    level = sum(1 for t in LEVELS if exp >= t)
    achievements = sum(1 for r in r24 if r["reached"] and (r["rank"] or 999) <= 3)
    tops = sum(1 for r in r24 if r["reached"] and r["rank"] == 1)

    # 掛け持ちの広さ・相性 (大会での強さ)
    by_room: dict[int, list[Any]] = {}
    for r in r12:
        by_room.setdefault(r["room_id"], []).append(r)
    room_sum = {rid: sum(x["point"] for x in xs) for rid, xs in by_room.items()}
    total12 = sum(room_sum.values())
    spread = round(1 / sum((v / total12) ** 2 for v in room_sum.values()), 2) if total12 else None
    done_by_room: dict[int, list[Any]] = {}
    for r in d12:
        done_by_room.setdefault(r["room_id"], []).append(r)
    main_rooms = sum(1 for xs in done_by_room.values() if fire and max(x["point"] for x in xs) >= SORTIE * fire)
    stage_pt = [r["point"] for r in r12 if (r["stage_no"] or 0) > 1]
    plain_pt = [r["point"] for r in r12 if not (r["stage_no"] or 0) > 1]
    contest = round(median(stage_pt) / median(plain_pt), 2) if len(stage_pt) >= 2 and len(plain_pt) >= 2 else None

    fan_rows = s.all("SELECT room_id, ym, level FROM active_fans WHERE user_id=? AND ym >= ?",
                     (uid, min(first12, first24).replace("-", "")))
    # 心のカルテの列 (ファンの名簿に居ないのが「0」と分かるかも、ここで分かる)
    try:
        lines = psyche.gather(s, month, rows, totals, fan_rows, fire, career, cache)
    except Exception:  # noqa: BLE001
        log.exception("心のカルテの列を作れませんでした (ユーザー %s)", uid)
        lines = None
    # 最後の 3 か月、どのルームのファンの名簿にも居ないと分かっているか。名簿が全員分でない・読んでいない月があれば分からない
    fan_gone = lines is not None and psyche.fan_gone(lines["fans"], len(lines["months"]))

    # 状態異常
    fan = s.one("SELECT 1 FROM active_fans WHERE user_id=? AND ym >= ? LIMIT 1", (uid, _add(month, -2).replace("-", ""))) is not None
    last3 = any((r["end"] or 0) >= _month_start(_add(month, -2)) for r in rows) or spent > 0
    if not last3 and not fan:
        state = "離脱" if fan_gone else "不明"
    elif before and recent >= RISING * before or (not before and recent):
        state = "上り調子"
    elif peak3 and recent <= LOW * peak3:
        state = "温存中" if fan else ("枯渇" if fan_gone else "不明")
    else:
        state = "安定"
    marks = [state]
    if fatigue is not None and fatigue > 1:
        marks.append("疲労")
    if now - first_seen < NEW_MONTHS * 30.4 * 86400:
        marks.append("新顔")

    # 総合力 = 次の勝負で、一人で動かせる量の見込み
    ready = min(fire, left) if left else 0
    factor = STATE_FACTOR.get(state, 1.0) * (FATIGUE_FACTOR if "疲労" in marks else 1.0)
    skill = 0.8 + 0.4 * intel if intel is not None else 1.0
    overall = round(ready * factor * skill)

    # ルームとの関係
    by_room24: dict[int, list[Any]] = {}
    for r in r24:
        by_room24.setdefault(r["room_id"], []).append(r)
    main = max(room_sum, key=lambda k: room_sum[k]) if room_sum else None
    main_max = max((x["point"] for x in by_room24.get(main, [])), default=0) if main is not None else 0
    fans = {}
    for f in fan_rows:
        if f["ym"] < first12.replace("-", ""):
            continue
        a = fans.setdefault(f["room_id"], {"level": 0, "months": 0})
        a["level"], a["months"] = max(a["level"], f["level"] or 0), a["months"] + 1
    rooms = []
    for rid in sorted(room_sum, key=lambda k: -room_sum[k])[:ROOMS]:
        xs = by_room[rid]
        cap = _second([x["point"] for x in done_by_room.get(rid, [])])
        mine = {x["event_id"]: x["point"] for x in by_room24.get(rid, [])}
        fights = [r[0] for r in s.all("SELECT event_id FROM event_rounds WHERE room_id=? AND end >= ? AND battle=1", (rid, since12))]
        sortie = (round(sum(1 for e in fights if cap and mine.get(e, 0) >= SORTIE * cap) / len(fights), 3)
                  if len(fights) >= MIN_ROUNDS else None)
        kind = "推し" if rid == main else ("付き合い" if max(x["point"] for x in by_room24.get(rid, xs)) < TSUKIAI * main_max else "掛け持ち")
        rm = [sum(x["point"] for x in xs if _month_of(x["end"]) == _add(month, -k)) for k in range(5, -1, -1)]
        morale = _ratio(sum(rm[-3:]), sum(rm[:3]))
        cum = s.one("SELECT visits, point FROM cumulative WHERE room_id=? AND user_id=?", (rid, uid))
        share = round(room_sum[rid] / total12, 3) if total12 else 0
        power = round(overall * share * (sortie if sortie is not None else 0.5))
        edge = room_edge(s, rid, since12, edges)
        if kind == "付き合い":
            cls = "付き合い"
        elif edge and power >= edge:
            cls = "主砲"
        elif edge and power >= edge / 3:
            cls = "主力"
        elif sortie is not None and sortie >= 0.5:
            cls = "支え"
        else:
            cls = "付き合い"
        rooms.append({"room_id": rid, "share": share, "kind": kind, "cap": cap, "points": room_sum[rid], "sortie": sortie,
                      "fights": len(fights), "morale": morale, "fan_level": fans.get(rid, {}).get("level"),
                      "fan_months": fans.get(rid, {}).get("months", 0), "visits": cum["visits"] if cum is not None else None,
                      "wins": sum(1 for x in by_room24.get(rid, []) if x["reached"] and (x["rank"] or 999) <= 3),
                      "power": power, "edge": edge, "class": cls})
    top = rooms[0] if rooms else None
    try:  # 心のカルテが失敗しても、ステータスは作る
        mind = psyche.judge(lines) if lines is not None else None
    except Exception:  # noqa: BLE001
        log.exception("心のカルテを作れませんでした (ユーザー %s)", uid)
        mind = None
    return {"user_id": uid, "at": now, "month": month,
            "hp": budget, "mp": left, "spent": spent, "fire": fire, "fire_basis": fire_basis, "max_out": max_out,
            "stamina": stamina, "recovery": recovery, "intel": intel, "intel_rounds": len(b_judged), "hit": hit, "hit_rounds": len(shots),
            "momentum": momentum, "new_high": new_high, "fatigue": fatigue, "achievements": achievements, "tops": tops,
            "exp": exp, "level": level, "career": round(career, 1), "first_seen": first_seen, "spread": spread, "main_rooms": main_rooms,
            "contest": contest, "state": state, "formula": FORMULA, "fire_rounds": len(fire_pool), "marks": marks, "fan": fan, "overall": overall, "ready": ready,
            "factor": round(factor * skill, 3), "scale": scale(fire), "rooms": rooms, "psyche": mind,
            "_top_room": top["room_id"] if top else None, "_top_share": top["share"] if top else None}


def room_edge(s: Any, rid: int, since: float, cache: dict[int, int] | None = None) -> int:
    """ルームの決め手の量 = このルームが上の方で終えたイベントで、隣の順位と分かれた差の中ほど。
    上の方に入った回が少ないルームは、イベントのポイントの中ほどの 1 割。"""
    if cache is not None and rid in cache:
        return cache[rid]
    gaps = [r[0] for r in s.all("SELECT gap FROM event_rounds WHERE room_id=? AND end >= ? AND gap IS NOT NULL", (rid, since))]
    if len(gaps) >= MIN_ROUNDS:
        edge = int(median(gaps))
    else:
        pts = [r[0] for r in s.all("SELECT point FROM room_events WHERE room_id=? AND point > 0", (rid,))]
        edge = int(CLOSE * median(pts)) if pts else 0
    if cache is not None:
        cache[rid] = edge
    return edge


def scale(fire: int) -> str:
    for at, label in SCALE:
        if fire >= at:
            return label
    return "10 万未満"


def save(s: Any, st: dict[str, Any]) -> None:
    with s.tx() as c:
        c.execute("""INSERT OR REPLACE INTO gifter_status(user_id, version, at, overall, firepower, budget, left_pt, state, top_room,
                                                          top_share, data) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                  (st["user_id"], VERSION, st["at"], st["overall"], st["fire"], st["hp"], st["mp"], st["state"], st["_top_room"],
                   st["_top_share"], json.dumps({k: v for k, v in st.items() if not k.startswith("_")}, ensure_ascii=False)))


# ------------------------------------------------------------------ 1 日 1 回の回し方
def population(s: Any, now: float) -> list[int]:
    """パワーギフター: 直近 12 か月のどこかの月で、その月の中堅の下限 (上から 10%) 以上を出した人。"""
    month = last_month(now)
    out: set[int] = set()
    for k in range(MONTHS):
        m = _add(month, -k)
        cut = month_cuts(s, m).get("mid")
        if cut:
            out.update(r[0] for r in s.all("SELECT user_id FROM user_month WHERE month=? AND points >= ?", (m, cut)))
    return sorted(out)


def _run(s: Any) -> dict[str, Any]:
    try:
        return json.loads(s.meta(RUN_META) or "{}")
    except (TypeError, ValueError):
        return {}


def step(s: Any, now: float | None = None, users: int = STEP_USERS) -> bool:
    """少しだけ進める (勝負の回を作る → 人を作る → 回し終えたら評価の区切りを作る)。何かしたら True。"""
    now = time.time() if now is None else now
    if build_rounds(s, now):
        return True
    run = _run(s)
    queued = s.scalar("SELECT COUNT(*) FROM gifter_status_queue")
    if not queued:
        if run.get("started") and not run.get("done"):
            finish(s, run, now)
            return True
        if run.get("version") == VERSION and (run.get("done") or 0) > now - EVERY:
            return False
        ids = population(s, now)
        with s.tx() as c:
            c.executemany("INSERT OR IGNORE INTO gifter_status_queue VALUES (?)", [(u,) for u in ids])
        s.set_meta(RUN_META, json.dumps({"started": now, "version": VERSION, "people": len(ids)}))
        return True
    ids = [r[0] for r in s.all("SELECT user_id FROM gifter_status_queue ORDER BY user_id LIMIT ?", (users,))]
    edges: dict[int, int] = {}
    cache: dict[Any, Any] = {}
    for uid in ids:
        st = compute(s, uid, now, edges, cache)
        if st is not None:
            save(s, st)
    with s.tx() as c:
        c.executemany("DELETE FROM gifter_status_queue WHERE user_id=?", [(u,) for u in ids])
    return True


def finish(s: Any, run: dict[str, Any], now: float) -> None:
    """回し終えた: 今回作らなかった人 (パワーギフターでなくなった人) を消し、項目ごとの評価の区切り (1〜100) を作る。"""
    with s.tx() as c:
        c.execute("DELETE FROM gifter_status WHERE at < ? OR version != ?", (run["started"], VERSION))
    vals: dict[str, array.array] = {k: array.array("d") for k in SCORED}
    for (data,) in s.conn.execute("SELECT data FROM gifter_status"):
        d = json.loads(data)
        for k in SCORED:
            if d.get(k) is not None:
                vals[k].append(float(d[k]))
    cuts = {}
    for k, v in vals.items():
        xs = sorted(v)
        cuts[k] = [xs[min(len(xs) - 1, int(len(xs) * q / 100))] for q in range(1, 100)] if xs else []
    s.set_meta(CUTS_META, json.dumps({"at": now, "people": len(vals["overall"]), "cuts": cuts}))
    s.set_meta(RUN_META, json.dumps({**run, "done": now}))
    log.info("ギフターのステータスを作り終えました (%d 人、%.0f 秒)", len(vals["overall"]), now - run["started"])


# ------------------------------------------------------------------ ページで読む
_cuts_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def _cuts(s: Any) -> dict[str, Any]:
    key = str(s.state_dir)
    hit = _cuts_cache.get(key)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    try:
        out = json.loads(s.meta(CUTS_META) or "{}")
    except (TypeError, ValueError):
        out = {}
    _cuts_cache[key] = (time.time(), out)
    return out


def score(cuts: dict[str, Any], k: str, v: Any) -> int | None:
    """1〜100 の評価 (パワーギフターの中で、上から何番目くらいか。100 がいちばん上)。"""
    xs = (cuts.get("cuts") or {}).get(k) or []
    if v is None or not xs:
        return None
    p = bisect.bisect_right(xs, float(v)) + 1  # 1〜100
    return 101 - p if k in LOW_IS_GOOD else p


def grade(cuts: dict[str, Any], overall: Any) -> str | None:
    sc = score(cuts, "overall", overall)
    return None if sc is None else _grade_of_score(sc)  # 上から何割かでランク


def read(s: Any, uid: int) -> dict[str, Any] | None:
    """ギフターのページに出すステータス (作っていなければ、何を待っているか)。"""
    run, cuts = _run(s), _cuts(s)
    info = {"run": {"started": run.get("started"), "done": run.get("done"), "people": cuts.get("people") or run.get("people"),
                    "queued": s.scalar("SELECT COUNT(*) FROM gifter_status_queue")}}
    r = s.one("SELECT data, overall FROM gifter_status WHERE user_id=? AND version=?", (int(uid), VERSION))
    if r is None:
        return {**info, "found": False}
    d = json.loads(r["data"])
    rank = s.scalar("SELECT COUNT(*) FROM gifter_status WHERE overall > ?", (r["overall"],)) + 1
    mind = d.get("psyche") or {}
    extra = [t.get(k) for t in mind.get("timeline", []) for k in ("room_id", "to_room")] + [(mind.get("outlook") or {}).get("room_id")]
    names = _room_names(s, [x["room_id"] for x in d["rooms"]] + [i for i in extra if i is not None])
    for x in d["rooms"]:
        x["key"], x["name"] = names.get(x["room_id"], (None, str(x["room_id"])))
    for t in mind.get("timeline", []) + ([mind["outlook"]] if mind.get("outlook") else []):
        for k, out in (("room_id", "room_name"), ("to_room", "to_name")):
            if t.get(k) is not None:
                t[out] = names.get(t[k], (None, str(t[k])))[1]
    return {**info, "found": True, **d, "scores": {k: score(cuts, k, d.get(k)) for k in SCORED}, "grade": grade(cuts, d["overall"]),
            "rank": rank, "people": cuts.get("people")}


def _room_names(s: Any, ids: list[int]) -> dict[int, tuple[str | None, str]]:
    ids = [int(i) for i in ids if i is not None]
    if not ids:
        return {}
    return {r[0]: (r[1], r[2] or r[1] or str(r[0]))
            for r in s.all(f"SELECT room_id, key, name FROM rooms WHERE room_id IN ({','.join('?' * len(ids))})", ids)}


def grade_bounds(cuts: dict[str, Any], g: str) -> tuple[float | None, float | None] | None:
    """ランク g になる総合力の範囲 (下限以上・上限未満。None は端なし)。区切りがまだ無ければ None。
    score() は「区切りのいくつを超えたか」なので、同じランクの人は区切りの値の間にまとまる。"""
    xs = (cuts.get("cuts") or {}).get("overall") or []
    if not xs:
        return None
    ks = [k for k in range(1, len(xs) + 2) if _grade_of_score(k) == g]  # score() の値 (1〜len+1) のうち、ランク g になるもの
    if not ks:
        return None
    lo = xs[min(ks) - 2] if min(ks) >= 2 else None
    hi = xs[max(ks) - 1] if max(ks) - 1 < len(xs) else None
    return lo, hi


def _grade_of_score(sc: int) -> str:
    top = (100 - sc) / 100
    for frac, g in GRADES:
        if top < frac:
            return g
    return "D"


def ranking(s: Any, limit: int = 500, grade_of: str | None = None) -> dict[str, Any]:
    """SHOWROOM 全体の戦力ランキング (総合力の多い順。総合力の索引を上から読む)。順位は同点なら同じ順位 (詳しいページと同じ)。
    grade_of を付けると、そのランクの人を、ランクの境目の総合力で絞ってから上から読む (上位 limit 人の中から絞るのではない)。"""
    cuts = _cuts(s)
    where, args = "g.overall IS NOT NULL", []
    if grade_of:
        b = grade_bounds(cuts, grade_of)
        if b is None:
            return {"rows": [], "people": cuts.get("people"), "run": _run(s), "at": cuts.get("at"), "grade": grade_of}
        if b[0] is not None:
            where, args = where + " AND g.overall >= ?", args + [b[0]]
        if b[1] is not None:
            where, args = where + " AND g.overall < ?", args + [b[1]]
    rows = [dict(r) for r in s.all(f"""SELECT g.user_id, g.overall, g.firepower, g.budget, g.left_pt, g.state, g.top_room, g.top_share,
                                              u.name FROM gifter_status g INDEXED BY gifter_status_overall
                                       LEFT JOIN users u USING(user_id) WHERE {where}
                                       ORDER BY g.overall DESC LIMIT ?""", (*args, int(limit)))]
    names = _room_names(s, [r["top_room"] for r in rows])
    # 読んだ行は総合力の並びで切れ目なく続くので、最初の行より多い人の数から数えれば、全体での順位になる
    base = s.scalar("SELECT COUNT(*) FROM gifter_status WHERE overall > ?", (rows[0]["overall"],)) if rows else 0
    first = 0
    for i, r in enumerate(rows):
        if i and r["overall"] != rows[i - 1]["overall"]:
            first = i
        r["rank"], r["grade"], r["scale"] = base + first + 1, grade(cuts, r["overall"]), scale(r["firepower"] or 0)
        r["top_key"], r["top_name"] = names.get(r["top_room"], (None, None))
    if grade_of:
        rows = [r for r in rows if r["grade"] == grade_of]  # 念のため (境目の値ちょうどの扱いは score() と同じ)
    out = {"rows": rows, "people": cuts.get("people"), "run": _run(s), "at": cuts.get("at")}
    if grade_of:
        out["grade"] = grade_of
    return out


def lookup(s: Any, ids: list[int]) -> dict[int, dict[str, Any]]:
    """一覧に出す、人ごとの総合力・ランク・火力 (作ってある人だけ)。"""
    cuts, out = _cuts(s), {}
    ids = [int(i) for i in ids]
    for k in range(0, len(ids), 500):
        part = ids[k:k + 500]
        for r in s.all(f"""SELECT user_id, overall, firepower, state FROM gifter_status
                           WHERE user_id IN ({','.join('?' * len(part))})""", part):
            out[r["user_id"]] = {"overall": r["overall"], "grade": grade(cuts, r["overall"]), "fire": r["firepower"],
                                 "scale": scale(r["firepower"] or 0), "state": r["state"]}
    return out


def run_forever(state_dir: Path, stop: threading.Event) -> None:
    from . import room_parts
    from .research_store import Store

    while not stop.wait(TICK):
        if not room_parts.idle():
            continue
        try:
            with memory.heavy(wait=False) as got:
                if not got:
                    continue  # ほかの重い集計が動いている: 次の機会に
                with Store(state_dir) as s:
                    step(s)
            memory.release()
        except Exception as e:  # noqa: BLE001 - 1 回の失敗で止めない
            log.warning("ギフターのステータスを作れませんでした: %s", e)


_stop: threading.Event | None = None


def start(state_dir: Path) -> None:
    """裏で作る仕事を始める (何回呼んでも 1 本だけ)。"""
    global _stop
    if _stop is not None and not _stop.is_set():
        return
    _stop = threading.Event()
    threading.Thread(target=run_forever, args=(Path(state_dir), _stop), name="gifter-status", daemon=True).start()


def stop() -> None:
    if _stop is not None:
        _stop.set()
