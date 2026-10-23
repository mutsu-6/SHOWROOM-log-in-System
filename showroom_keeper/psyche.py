"""ギフターの心の読み方 (心のカルテ)。

考え方は project の資料「ギフターの心理分析の方法」(2026-10-22) と `docs/分析の見方.md` の「心のカルテ」。心は直接見えないので、
行動の線のずれ方で読む。ここで作るのは資料の作る順番 1・2:
- お金の線 (月の量・ルームごとの量・1 イベントの量) と、関心の線 (アクティブファンの月ごとのレベル) を、24 か月並べる
- なだらかな変化 (傾き) と 急な変化 (段差) を見つけ、型に当てはめる: 財布が細った・推しが移った・熱が冷めた・冷めかけ・
  深くハマる・急にハマった・喧嘩して離れた・急な出費・燃え尽き・貯めて一気 (潜伏)・楽しくないのに出す
  (流星 = 1 回だけ大きく出して消える人は、出した月が少ないので、今ある「ギフターの一生」の判定で見る)
- のめり込みのしるし (負けの取り返し・だんだん増える・予算を超える・やめて戻る・長く続けた)
- 今の時期・性質・次の大事なイベントでの見込み・相手の名簿として見る時の注意

間違えないための決まり (資料の 5):
- ルームがイベントに出ていない月は、ギフターの変化と数えない
- 貢献ランキングは上位 100 人まで。壁ぎわの人は「減った」が見えなくなっただけかもしれないので、確からしさを下げる
- SHOWROOM 全体の波 (月ごとの中堅の下限の動き) で割ってから見る
- 根拠の線が 2 本そろった時だけ「高」。型はどれも仮説で、本当の理由 (喧嘩・病気・転職) はデータからは分からない

judge() は数字の列だけで決める (表は読まない)。表から列を作るのは gifter_status.compute() の中で、その人の行をもう読んでいる時。
どの線も仮の値で、答え合わせ (M さんのメモ・過去で当てる) をして決め直す。
"""

from __future__ import annotations

import math
from datetime import datetime
from statistics import median
from typing import Any

from .gifter_analysis import _add, month_cuts
from .status import JST

MONTHS = 24  # 並べる月数
SLOPE = 0.05  # なだらかな変化: 1 か月に、ふだんの量のこれだけ以上の傾き
STEP = 3.0  # 急な変化: 前後の水準がこれだけ倍 (またはこれだけ分の 1) 以上違う
GONE = 0.1  # ルームの量が、前のこれだけ以下になったら「ほぼ 0」
QUIET = 0.3  # 月の量が、ふだんのこれだけ以下なら「静か」
BACK = 0.6  # 前の水準のこれだけ以上に戻れば「戻った」
OVER = 2.0  # 月の量が、ふだんのこれだけ倍を超えたら「予算を超えた月」
SAVED = 0.6  # 爆発した月の量が、(休んだ月数 + 1) × ふだん のこれだけ以上なら「貯めていた」
CHASE = 1.3  # 接戦で負けた次のイベントで、勝った次のこれだけ倍以上出せば「負けの取り返し」
WALL_RANK = 70  # 貢献ランキングの順位の中ほどがこれより下なら、上位 100 人の壁ぎわ
SHIFT = 0.3  # 推しの割合がこれだけ以上動いたら「行き先が変わった」


# ------------------------------------------------------------------ 小さな道具
def slope(vals: list[float | None]) -> float | None:
    """外れた月に引っぱられない傾き (2 点ずつの傾きの中ほど)。分からない月 (None) は飛ばす。"""
    pts = [(i, v) for i, v in enumerate(vals) if v is not None]
    if len(pts) < 4:
        return None
    s = [(b[1] - a[1]) / (b[0] - a[0]) for k, a in enumerate(pts) for b in pts[k + 1:]]
    return median(s)


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def steps(vals: list[float | None], base: float) -> list[dict[str, Any]]:
    """急な変化 (段差) を見つける。前の 3〜6 か月と、後の 2〜3 か月の水準の比が STEP 倍以上違う所 (いちばん新しい月だけは
    後が 1 か月で、final = まだ続くか分からない)。近いもの (3 か月以内) は、比の大きい方だけ残す。"""
    eps = 0.05 * base if base else 1.0
    cand = []
    n = len(vals)
    for k in range(3, n):
        before = [v for v in vals[max(0, k - 6):k] if v is not None]
        if len(before) < 3:
            continue
        mb = _mean(before)
        best = None
        for size in ((1,) if k == n - 1 else (2, 3)):
            after = [v for v in vals[k:k + size] if v is not None]
            if len(after) < size:
                continue
            ma = _mean(after)
            r = (ma + eps) / (mb + eps)
            if best is None or abs(math.log(r)) > abs(math.log(best[0])):
                best = (r, ma)
        # 前が落ち着いていた時だけ (前の窓に、もう段差のあとの月が入っていれば、同じ変化を 2 度数えない)
        steady = best and (min(before) >= mb / STEP if best[0] < 1 else max(before) <= max(mb, eps) * STEP)
        if steady and (best[0] >= STEP or best[0] <= 1 / STEP):
            cand.append({"at": k, "ratio": best[0], "before": mb, "after": best[1], "final": k == n - 1})
    out: list[dict[str, Any]] = []
    for c in sorted(cand, key=lambda c: -abs(math.log(c["ratio"]))):
        if all(abs(c["at"] - o["at"]) > 3 for o in out):
            out.append(c)
    return sorted(out, key=lambda c: c["at"])


def _known(xs: list[Any]) -> list[Any]:
    return [x for x in xs if x is not None]


def fans_complete(ym: str, total: Any, at: Any, got: int) -> bool:
    """そのルームのその月 (ym = "202610") のファンの名簿で、居ない人を「レベル 0」と言えるか。
    名簿が全員分 (読めた人数が SHOWROOM の言う総数以上) で、月が終わってから読んだものだけ (月の終わりの数日前に読んだ名簿でも、
    そのあとファンになった人は居ない。Codex の確認 #127。月の途中で読んだ月は、sweep._next_fans_closed が月が終わってから読み直す)。
    そうでない名簿 (上位 1,000 人・さかのぼった月は 200 人までしか読まない・月の途中で読んだ) に居ない人は、分からない。"""
    if total is None or at is None or got < int(total):
        return False
    y, m = int(ym[:4]), int(ym[4:])
    end = datetime(y + m // 12, m % 12 + 1, 1, tzinfo=JST).timestamp()
    return float(at) >= end


def fan_line_of(fans: dict[int, list[int | None]], n: int) -> list[int | None]:
    """関心の線: 月ごとの、どのルームのでもいちばん高いファンレベル。レベルが見えたらその値。
    見えず、分からないルームが 1 つでもある月は None (0 にしない)。全部のルームで 0 と分かった月だけ 0。"""
    out: list[int | None] = []
    for i in range(n):
        vals = [f[i] for f in fans.values()]
        known = [v for v in vals if v is not None]
        if known and max(known) > 0:
            out.append(max(known))
        elif known and len(known) == len(vals):
            out.append(0)
        else:
            out.append(None)
    return out


def fan_gone(fans: dict[int, list[int | None]], n: int, last: int = 3) -> bool:
    """最後の last か月、どのルームのファンの名簿にも居ないと分かっているか (分からない月があれば False)。"""
    line = fan_line_of(fans, n)[-last:]
    return bool(line) and all(v == 0 for v in line)


# ------------------------------------------------------------------ 表から列を作る
def gather(s: Any, month: str, rows: list[Any], totals: dict[str, int], fan_rows: list[Any], fire: int, career: float,
           cache: dict[Any, Any] | None = None) -> dict[str, Any]:
    """その人の、もう読んである行 (貢献ランキング rows・月の量 totals・ファンレベル fan_rows) から列を作る。
    足して読むのは、ルームの決まった小さな行 (イベントに出た月・ファンレベルを集めた月) と月の区切りだけ (ルームで引く索引)。
    cache は、同じ回の人どうしで使い回す入れ物。"""
    cache = {} if cache is None else cache
    months = [_add(month, -k) for k in range(MONTHS - 1, -1, -1)]
    pos = {m: i for i, m in enumerate(months)}
    n = len(months)
    total = [int(totals.get(m, 0) or 0) for m in months]
    rooms: dict[int, list[int]] = {}
    events = []
    for r in sorted(rows, key=lambda r: r["end"] or 0):
        i = pos.get(datetime.fromtimestamp(r["end"] or 0, JST).strftime("%Y-%m"))
        if i is None:
            continue
        rooms.setdefault(r["room_id"], [0] * n)[i] += r["point"]
        events.append({"room_id": r["room_id"], "point": r["point"], "rank": r["rank"], "i": i, "battle": r["battle"],
                       "reached": r["reached"], "judged": r["judged"]})
    top = sorted(rooms, key=lambda r: -sum(rooms[r]))[:6]
    fan_by: dict[int, dict[str, int]] = {}
    for f in fan_rows:
        fan_by.setdefault(f["room_id"], {})[f"{f['ym'][:4]}-{f['ym'][4:]}"] = f["level"] or 0
    fan_rooms = list(dict.fromkeys(top + sorted(fan_by, key=lambda r: -max(fan_by[r].values()))[:4]))
    first = months[0]
    fans: dict[int, list[int | None]] = {}
    active: dict[int, list[bool]] = {}
    for rid in fan_rooms:
        key = ("fans", rid)
        if key not in cache:
            cache[key] = {f"{r[0][:4]}-{r[0][4:]}" for r in s.all(
                """SELECT r.ym, r.total, r.at, (SELECT COUNT(*) FROM active_fans a WHERE a.room_id=r.room_id AND a.ym=r.ym)
                   FROM room_fans r WHERE r.room_id=? AND r.ym >= ?""", (rid, first.replace("-", "")))
                if fans_complete(r[0], r[1], r[2], r[3])}
        mine = fan_by.get(rid, {})
        # 名簿に居ればそのレベル。居なくても、名簿が全員分で、月の終わりごろに読んだものなら 0。そうでなければ分からない (None)
        fans[rid] = [mine[m] if m in mine else (0 if m in cache[key] else None) for m in months]
    since = datetime.strptime(first + "-01", "%Y-%m-%d").replace(tzinfo=JST).timestamp()
    for rid in top:
        key = ("events", rid)
        if key not in cache:
            cache[key] = {datetime.fromtimestamp(r[0], JST).strftime("%Y-%m") for r in s.all(
                "SELECT e.end FROM room_events re JOIN events e USING(event_id) WHERE re.room_id=? AND e.end >= ?", (rid, since))
                if r[0]}
        active[rid] = [m in cache[key] for m in months]
    mids = []
    for m in months:
        key = ("mid", m)
        if key not in cache:
            cache[key] = month_cuts(s, m).get("mid")
        mids.append(cache[key])
    base = median([x for x in mids if x]) if any(mids) else None
    season = [min(2.0, max(0.5, x / base)) if x and base else 1.0 for x in mids]
    return {"months": months, "total": total, "rooms": rooms, "fans": fans, "active": active, "season": season,
            "events": events, "fire": fire, "career": career}


# ------------------------------------------------------------------ 決める
def judge(lines: dict[str, Any]) -> dict[str, Any] | None:
    """lines (列はどれも古い順で MONTHS 個、最後が集め終わったいちばん新しい月):
    months: 月の名前 / total: 全部のルームの月の量 / rooms: {ルーム: 月の量} / fans: {ルーム: 月のファンレベル (分からない月は None)}
    active: {ルーム: その月にイベントに出ていたか} / season: SHOWROOM 全体の波 (ふつうの月 = 1)
    events: [{room_id, point, rank, i (月の番号), battle, reached, judged}] (古い順) / fire: 火力 / career: 続いた月数"""
    months, total, season = lines["months"], lines["total"], lines["season"]
    n = len(months)
    active_vals = [v for v in total if v]
    if len(active_vals) < 3:
        return None
    usual = median(active_vals)
    events = lines["events"]
    rooms, fans, act = lines["rooms"], lines["fans"], lines["active"]
    room_sum = {r: sum(v) for r, v in rooms.items()}
    mains = [r for r in sorted(room_sum, key=lambda r: -room_sum[r]) if room_sum[r] >= 0.2 * sum(room_sum.values())][:4]

    # SHOWROOM 全体の波で割り、ふだんの量でそろえる。推しのルームがどれもイベントに出ていない月は分からない (None)
    norm: list[float | None] = []
    for i in range(n):
        off = mains and not total[i] and not any(act.get(r, [True] * n)[i] for r in mains)
        norm.append(None if off else total[i] / (season[i] or 1) / usual)
    # 関心の線: どのルームのでも、いちばん高いファンレベル (見えず、分からないルームがある月は None)
    fan_line: list[float | None] = list(fan_line_of(fans, n))
    fan_base = median([v for v in fan_line if v]) if any(fan_line) else 0

    def fan_kept(a: int, b: int, rid: int | None = None) -> bool | None:
        """a〜b 月 (b を含まない) に、関心が保たれていたか (前の水準の半分以上のレベルが出ていたか)。分からなければ None。"""
        line = fans.get(rid, [None] * n) if rid is not None else fan_line
        before = _known(line[max(0, a - 6):a])
        after = _known(line[a:b])
        if not after or not before:
            return None
        lv = max(before)
        return lv > 0 and max(after) >= lv / 2

    def room_line(rid: int) -> list[int]:
        return rooms.get(rid, [0] * n)

    def main_in(a: int, b: int, exclude: int | None = None) -> int | None:
        cand = {r: sum(v[a:b]) for r, v in rooms.items() if r != exclude}
        cand = {r: v for r, v in cand.items() if v > 0}
        return max(cand, key=lambda r: cand[r]) if cand else None

    ranks = [e["rank"] for e in events if e.get("rank") and e["i"] >= n - 12]
    wall = bool(ranks) and median(ranks) >= WALL_RANK
    history = len(active_vals)
    timeline: list[dict[str, Any]] = []

    def add(i: int, kind: str, evidence: list[str], lines_ok: int, room: int | None = None, to: int | None = None) -> None:
        conf = "高" if lines_ok >= 2 else "中" if lines_ok == 1 else "低"
        if wall or history < 6:
            conf = {"高": "中", "中": "低"}.get(conf, "低")
        timeline.append({"month": months[i], "i": i, "type": kind, "room_id": room, "to_room": to, "evidence": evidence, "conf": conf})

    def pct(x: float) -> str:
        return f"{x:.1f} 倍" if x >= 1 else f"{x * 100:.0f}%"

    # ---- 貯めて一気 (潜伏): 静かな月 (どこにも出していない) のあとに、休んだ分をまとめて出す
    bursts = []
    i = 1
    while i < n:
        if norm[i] is not None and norm[i] <= QUIET and (norm[i - 1] or 0) >= 0.5:
            j = i
            while j < n and norm[j] is not None and norm[j] <= QUIET:
                j += 1
            if j < n and norm[j] is not None:
                gap = j - i
                if norm[j] >= max(OVER, SAVED * (gap + 1)):
                    bursts.append(j)
                    add(j, "貯めて一気", [f"{gap} か月どこにも出さず (ふだんの {QUIET * 100:.0f}% 以下)",
                                         f"そのあと 1 か月に、ふだんの {norm[j]:.1f} 倍 (休んだ分を合わせた量に近い)"],
                        1 + (fan_kept(i, j) is True), main_in(j, j + 1))
            i = max(j, i + 1)
        else:
            i += 1
    quiet_now = 0
    for v in reversed(norm):
        if v is None or v > QUIET:
            break
        quiet_now += 1
    if quiet_now and quiet_now < n and (norm[n - quiet_now - 1] or 0) < 0.5:
        quiet_now = 0  # 少しずつ下がって静かになった (急に止めたのではない) なら、なだらかな変化で見る

    # ---- 急な変化
    for st in steps(norm, 1.0):
        k = st["at"]
        if st["ratio"] <= 1 / STEP and not st["final"]:
            if any(k < b <= k + 4 for b in bursts):
                continue  # 静かにしていたあと、まとめて出した (貯めて一気で見る)
            prev_main = main_in(max(0, k - 6), k)
            before_total, after_total = st["before"], st["after"]
            r_line = room_line(prev_main) if prev_main is not None else [0] * n
            rb, ra = _mean(r_line[max(0, k - 6):k]), _mean(r_line[k:k + 3])
            # 燃え尽き: 直前 2 か月に、予算を超えて出していた
            peak_before = max((norm[x] or 0) for x in range(max(0, k - 2), k))
            back = next((x for x in range(k + 1, min(n, k + 4)) if norm[x] is not None and norm[x] >= BACK * before_total), None)
            kept = fan_kept(k, min(n, k + 3))
            if peak_before >= OVER:
                add(k, "燃え尽き", [f"直前の月に、ふだんの {peak_before:.1f} 倍を出した",
                                   f"その次の月から、全部のルームで前の {pct(after_total / before_total)} に"] +
                    ([f"関心も下がった"] if kept is False else []), 1 + (kept is False))
            elif back is not None:
                add(k, "急な出費", [f"全部のルームで、同じ月に前の {pct(after_total / before_total)} に",
                                   f"{back - k} か月で前の水準に戻った (戻った時も、前と同じくらい)"] +
                    (["関心は保たれていた (ファンレベルが残る)"] if kept else []), 1 + bool(kept))
            elif k >= n - 3 and kept:
                add(k, "急な出費の疑い", [f"全部のルームで、同じ月に前の {pct(after_total / before_total)} に",
                                         "関心は保たれている (ファンレベルが残る)", "戻るかは、まだ分からない"], 1)
            else:
                add(k, "熱が冷めた (急)" if kept is False else "お金が止まった", [
                    f"全部のルームで、同じ月に前の {pct(after_total / before_total)} に", "戻っていない"] +
                    (["関心も同じころに下がった"] if kept is False else ["関心は保たれている"] if kept else []),
                    1 + (kept is not None))
        elif st["ratio"] >= STEP:
            if k in bursts:
                continue
            # どのルームで増えたか
            gain = {r: _mean(v[k:k + 3]) - _mean(v[max(0, k - 6):k]) for r, v in rooms.items()}
            rid = max(gain, key=lambda r: gain[r]) if gain else None
            if rid is None or gain[rid] <= 0:
                continue
            lasted = sum(1 for v in room_line(rid)[k:k + 3] if v) >= 2
            fl = fans.get(rid, [None] * n)
            fan_up = any((fl[x] or 0) > max(_known(fl[max(0, k - 6):max(0, k - 1)]) or [0]) for x in range(max(0, k - 1), min(n, k + 2)))
            if lasted or k >= n - 1:
                add(k, "急にハマった" if lasted else "急にハマった疑い",
                    [f"このルームで、前の {pct(st['ratio'])} に", "2 か月以上続いている" if lasted else "続くかはまだ分からない"] +
                    (["同じころ、ファンレベルも上がった"] if fan_up else []), 1 + fan_up, rid)

    # ---- 喧嘩して離れた・推しが移った (1 つのルームだけで止まり、お金がほかのルームに移る)
    for rid in mains:
        line = room_line(rid)
        rs = steps([float(v) for v in line], max(1.0, median([v for v in line if v] or [1])))
        for st in rs:
            k = st["at"]
            if st["ratio"] > 1 / STEP or st["final"] or st["after"] > GONE * st["before"]:
                continue
            ra = act.get(rid, [True] * n)
            if not any(ra[k:min(n, k + 3)]):
                continue  # ルームがイベントに出ていない (ルームの側の事情)
            if len(line[k:k + 3]) < 2 or max(line[k:k + 3]) > GONE * st["before"]:
                continue  # すぐ戻った (離れていない)
            tb, ta = _mean([v for v in total[max(0, k - 6):k]]), _mean([v for v in total[k:k + 2]])
            to = main_in(k, min(n, k + 3), exclude=rid)
            if not tb or ta < 0.5 * tb or to is None:
                continue  # 全部で減った (上の急な変化で見る)
            kept = fan_kept(k, min(n, k + 3), rid)
            declining = (slope([v / (st["before"] or 1) for v in line[max(0, k - 6):k]]) or 0) <= -SLOPE
            kind = "推しが移った" if declining else "喧嘩して離れた"
            add(k, kind, [("前から少しずつ減っていたあと" if declining else "前は落ち着いていたのに、同じ月に") + "このルームの量がほぼ 0 に",
                          f"全部の合計はあまり減らず ({pct(ta / tb)})、ほかのルームに移った"] +
                (["このルームのファンレベルも消えた"] if kept is False else []), 1 + (kept is False), rid, to)

    # ---- なだらかな変化 (直近 12 か月の傾き)。急な変化が近くにある時は、そちらで説明できるので出さない
    recent_steps = [t for t in timeline if t["i"] >= n - 12]
    s12, s6 = slope(norm[-12:]), slope(norm[-6:])
    f12 = slope([v / fan_base if fan_base and v is not None else None for v in fan_line[-12:]]) if fan_base else None
    old_main, new_main = main_in(n - 12, n - 6), main_in(n - 6, n)
    share = {}
    for a, b in ((n - 12, n - 6), (n - 6, n)):
        tot = sum(sum(v[a:b]) for v in rooms.values())
        share[a] = {r: sum(v[a:b]) / tot for r, v in rooms.items()} if tot else {}
    moved = (old_main is not None and new_main is not None and old_main != new_main
             and share[n - 12].get(old_main, 0) - share[n - 6].get(old_main, 0) >= SHIFT)
    if moved and not any(t["type"] in ("推しが移った", "喧嘩して離れた") for t in timeline):
        a, b = share[n - 12].get(old_main, 0), share[n - 6].get(old_main, 0)
        add(n - 6, "推しが移った", [f"推しのルームの割合が、半年で {a * 100:.0f}% から {b * 100:.0f}% に下がった",
                                  f"同じころ、別のルームの割合が {share[n - 12].get(new_main, 0) * 100:.0f}% から "
                                  f"{share[n - 6].get(new_main, 0) * 100:.0f}% に上がった"], 2, old_main, new_main)
    elif not recent_steps and s12 is not None and s6 is not None:
        if s12 <= -SLOPE and s6 <= -SLOPE / 2:
            ev = [f"直近 12 か月、1 か月にふだんの {abs(s12) * 100:.0f}% ずつ下がっている"]
            if f12 is not None and f12 <= -SLOPE:
                add(n - 12, "熱が冷めた", ev + ["関心 (ファンレベル) も同じように下がっている", "どのルームでも下がっている"], 2)
            elif f12 is not None:
                add(n - 12, "財布が細った", ev + ["関心 (ファンレベル) は保たれている", "推しのルームの割合は変わらない"], 2)
            else:
                add(n - 12, "下がり続けている", ev + ["関心が分からないので、財布の事情か熱が冷めたかは見分けられない"], 1)
        elif s12 >= SLOPE and s6 >= SLOPE / 2:
            ev = [f"直近 12 か月、1 か月にふだんの {s12 * 100:.0f}% ずつ上がっている"]
            if f12 is not None and f12 <= -SLOPE:
                add(n - 12, "楽しくないのに出す", ev + ["関心 (ファンレベル) は下がっている (出す量と心がずれている)"], 2)
            else:
                add(n - 12, "深くハマる", ev + (["関心 (ファンレベル) も上がっている"] if f12 is not None and f12 >= SLOPE else []),
                    1 + (f12 is not None and f12 >= SLOPE))
    # 冷めかけ: 推しのルームに来ている (ファンレベルが残る) のに、このルームのお金だけ半分以下 (2〜3 か月)。
    # 出すのを止めた (0 が続く) のではなく、出してはいるが減った人
    if old_main is not None and not any(t["type"] in ("推しが移った", "喧嘩して離れた") and t["room_id"] == old_main for t in timeline):
        line = room_line(old_main)
        before, now3 = _mean(line[n - 9:n - 3]), _mean(line[n - 3:])
        others = [total[i] - line[i] for i in range(n)]
        o_before, o_now = _mean(others[n - 9:n - 3]), _mean(others[n - 3:])
        fl = fans.get(old_main, [None] * n)
        if (before and now3 <= 0.5 * before and sum(1 for v in line[n - 3:] if v) >= 2 and any(fl[n - 3:]) and all(act.get(old_main, [True] * n)[n - 3:])
                and o_before and o_now >= 0.8 * o_before):  # ほかのルームには前と同じくらい出している (財布の事情ではない)
            add(n - 3, "冷めかけ", ["このルームに来ている (ファンレベルが残る)", f"このルームのお金だけ、前の {pct(now3 / before)} に"], 2,
                old_main)

    # ---- のめり込みのしるし
    signs = []
    over = [i for i in range(n) if norm[i] is not None and norm[i] > OVER]
    rebound = sum(1 for i in over if any(norm[x] is not None and norm[x] <= QUIET for x in range(i + 1, min(n, i + 3))))
    if over:
        signs.append({"sign": "予算を超える", "value": len(over), "note": f"月の量が、ふだんの {OVER:.0f} 倍を超えた月が {len(over)} 回"
                      + (f" (そのうち {rebound} 回は、そのあと急に落ちた = 無理をした反動)" if rebound else ""),
                      "score": 20 if len(over) >= 2 else 10})
    # 負けの取り返し: 推しのルームが接戦で負けた次のイベントで、勝った次より多く出す
    main = mains[0] if mains else None
    mine = [e for e in events if e["room_id"] == main]
    loss, win = [], []
    for a, b in zip(mine, mine[1:]):
        if a.get("battle") and a.get("judged"):
            (win if a.get("reached") else loss).append(b["point"])
    if len(loss) >= 2 and len(win) >= 2 and median(win):
        r = median(loss) / median(win)
        if r >= CHASE:
            signs.append({"sign": "負けの取り返し", "value": round(r, 2),
                          "note": f"接戦で負けた次のイベントで、勝った次の {r:.1f} 倍を出す", "score": 30 if r >= 1.6 else 25})
    # だんだん増える: 1 イベントの上限の更新の回数と、直近 12 か月の上限 ÷ その前の 12 か月の上限
    pts = [e["point"] for e in events]
    records = sum(1 for k in range(3, len(pts)) if pts[k] > max(pts[:k]))
    old_max = max((e["point"] for e in events if e["i"] < n - 12), default=0)
    new_max = max((e["point"] for e in events if e["i"] >= n - 12), default=0)
    if records >= 3 and old_max and new_max >= 1.3 * old_max:
        signs.append({"sign": "だんだん増える", "value": records,
                      "note": f"1 イベントの上限を {records} 回更新し、直近 12 か月の上限はその前の {new_max / old_max:.1f} 倍", "score": 20})
    # やめて戻る: 2 か月以上どこにも出さない → 戻る
    returns, bigger, i = 0, 0, 1
    while i < n:
        if total[i] == 0 and total[i - 1]:
            j = i
            while j < n and total[j] == 0:
                j += 1
            if j < n and j - i >= 2 and all(norm[x] is not None for x in range(i, j)):
                returns += 1
                bigger += total[j] >= total[i - 1]
            i = j
        i += 1
    if returns:
        signs.append({"sign": "やめて戻る", "value": returns, "note": f"2 か月以上休んで戻ったことが {returns} 回"
                      + (f" (そのうち {bigger} 回は、戻った時に前より多く出した)" if bigger else ""), "score": 15 if returns >= 2 else 8})
    if (lines.get("career") or 0) >= 24 and main is not None and room_sum.get(main, 0) >= 0.5 * sum(room_sum.values()):
        signs.append({"sign": "長く続けた", "value": round(lines["career"]), "note": f"{lines['career']:.0f} か月続け、半分以上を 1 つのルームに (やめるのが惜しくなる)",
                      "score": 5})
    if any(t["type"] == "楽しくないのに出す" for t in timeline):
        signs.append({"sign": "楽しくないのに出す", "value": 1, "note": "出す量は増えているのに、関心は下がっている", "score": 10})
    addiction = min(100, sum(x["score"] for x in signs))

    # ---- 今の時期
    last3 = total[-3:]
    gap_then_back = (total[-1] or (n >= 2 and total[-2])) and n >= 5 and any(
        all(v == 0 for v in total[k:k + 2]) for k in range(n - 5, n - 2))
    fan_now = any(_known(fan_line[-3:]))
    if not any(last3) and not fan_now and fan_gone(fans, n):
        phase, why = "離脱", "3 か月、どこにも出さず、ファンレベルも無い"
    elif not any(last3) and not fan_now:
        phase, why = "分からない", "3 か月、どこにも出していない。ファンの名簿が全員分でない (または読んでいない) 月があり、離れたかは分からない"
    elif quiet_now and bursts:
        phase, why = "潜伏の疑い", f"{quiet_now} か月静かだが、前にも休んでからまとめて出したことが {len(bursts)} 回ある"
    elif quiet_now and any(t["type"] == "燃え尽き" and t["i"] >= n - quiet_now - 1 for t in timeline):
        phase, why = "燃え尽きのあと", f"大勝負で出し切ったあと、{quiet_now} か月静か"
    elif quiet_now and fan_now:
        phase, why = "休止 (お金の事情か潜伏)", f"{quiet_now} か月静かだが、ファンレベルは残っている"
    elif gap_then_back:
        phase, why = "復帰", "休んでいたあと、戻ってきた"
    elif quiet_now:
        phase, why = "休止", f"{quiet_now} か月静か"
    elif any(t["type"].startswith("急にハマった") and t["i"] >= n - 3 for t in timeline) or (s6 or 0) >= 2 * SLOPE:
        phase, why = "熱中", "直近で量が上がっている"
    elif any(t["type"] in ("熱が冷めた", "財布が細った", "下がり続けている", "冷めかけ") for t in timeline) or (s6 or 0) <= -2 * SLOPE:
        phase, why = "冷却中", "直近で量が下がり続けている"
    else:
        phase, why = "安定", "大きな変化は見えない"

    # ---- 性質
    saver = len(bursts)
    burnout = sum(1 for t in timeline if t["type"] == "燃え尽き")
    waits = []
    for t in timeline:
        if t["type"] == "急な出費":
            k = t["i"]
            back = next((x for x in range(k + 1, n) if norm[x] is not None and norm[x] >= BACK), None)
            if back is not None:
                waits.append(back - k)
    traits = {"saver": saver, "addiction": addiction, "signs": signs, "burnout": burnout,
              "recovery": median(waits) if waits else None}

    # ---- 次の大事なイベントでの見込み (推しのルーム)
    fire = lines.get("fire") or 0
    recent_ev = [e["point"] for e in events if e["room_id"] == main and e["i"] >= n - 6]
    low = int(median(recent_ev)) if recent_ev else 0
    high = max(fire, low)
    stored = int(quiet_now * usual) if quiet_now else 0
    appear = {"離脱": "ほぼ無い", "分からない": "分からない", "休止": "低い", "燃え尽きのあと": "低い", "冷却中": "中くらい", "休止 (お金の事情か潜伏)": "中くらい"}.get(phase, "高い")
    if phase == "潜伏の疑い":
        high = max(high, stored + usual)
        low = 0
    elif phase in ("休止", "離脱", "燃え尽きのあと", "分からない"):
        low = 0
    elif phase == "冷却中":
        high = max(low, int(high * 0.7))
    outlook = {"room_id": main, "appear": appear, "low": low, "high": int(high),
               "timing": "最後にまとめて出す型" if saver >= 2 else None}

    # ---- 相手の名簿として見る時の注意
    notes = []
    if saver >= 2 or phase == "潜伏の疑い":
        notes.append(f"今は静かでも、休んだ分を貯めて最後に出してくることがある (休んだ分の見込み: およそ {stored:,} pt)"
                     if stored else "休んでから、貯めた分をまとめて出す過去がある")
    if addiction >= 40:
        notes.append("のめり込みが強い。負けそうになると、予算を超えてでも出してくる見込み (残弾を多めに見る)")
    if phase == "冷却中":
        notes.append("熱が下がっている。大きな勝負でも、前ほどは出さない見込み")
    if burnout:
        notes.append("大勝負で出し切ったあと、しばらく出さなくなることがある")
    warnings = []
    if wall:
        warnings.append("貢献ランキングの順位が上位 100 人の壁ぎわ。「減った」は、100 位より下に下がって見えなくなっただけかもしれない")
    if history < 6:
        warnings.append(f"出した月が {history} か月しかない。型の確からしさは低い")
    if fan_base == 0:
        warnings.append("ファンレベルの記録が無いので、関心の線と重ねられない (お金の事情か、熱が冷めたかを見分けにくい)")

    for t in timeline:
        t.pop("i", None)
    timeline.sort(key=lambda t: t["month"])
    return {"phase": phase, "phase_why": why, "timeline": timeline, "traits": traits, "outlook": outlook, "notes": notes,
            "warnings": warnings, "months": months, "money": list(total),
            "fan": fan_line, "season": [round(x, 2) for x in season], "usual": int(usual)}
