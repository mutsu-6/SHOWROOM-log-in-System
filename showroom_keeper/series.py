"""大会 (ミスコン・オーディションなど、段階ごとに別のイベントになっている大きな大会) のまとまり。

段階ごとのイベントはそのまま残し、event_series (イベント → 大会・段階の順番・段階の名前) を足す。
SHOWROOM の「親のイベント」(parent_event_id) は、ほとんど入っていないので使わない。まとめる手がかりは 3 つ:
- 名前から段階の言葉 (【予選】・準々決勝・準決勝・決勝前半/後半・1st/2nd/Last・スタートダッシュ・本選 など) を除くと、同じか、よく似ている
- 前の段階が終わってから SERIES_GAP 日のうちに、次の段階が始まる
- 次の段階のルームの多く (SERIES_OVERLAP 以上) が、前の段階にもいる (room_events でくらべる)
同じ時期に、同じ名前で、ルームの重ならないイベント (【SR枠】【TP枠】など、入口が分かれたもの) は、同じ段階としてまとめる。
自動でまちがえた時は、管理画面で手でつないだり外したりでき (event_series_manual)、手で決めたものは自動で変えない。
SHOWROOM には問い合わせない。
"""

from __future__ import annotations

import difflib
import re
import time
import unicodedata
from statistics import median
from typing import Any

from .research_store import Store

SERIES_GAP = 60 * 86400  # 前の段階が終わってから、次の段階が始まるまでの日数の上限
SERIES_OVERLAP = 0.5  # 次の段階のルームのうち、前の段階にもいたルームの割合の下限
SERIES_NAME = 0.55  # 段階の言葉を除いた名前の似ている度合い (0〜1) の下限
SERIES_MIN_ROOMS = 3  # これより参加ルームの少ないイベントは、ルームの重なりでまとめない
PARALLEL_DAYS = 2  # 同じ名前で、始まりがこの日数のうちのイベントは、同じ段階 (入口のちがう枠) とみる
SERIES_STEP = 100  # 1 回に調べるイベントの数
STAR_PER_DAY = 320  # 無料の星だけで 1 日に送れるポイントの目安 (キラキラ星 200 + 広告の抽選の星・種 120。2.5 倍にならない)

_STAGE = (r"予選|準々決勝|準決勝|決勝|セミファイナル|ファイナル|本選|本戦|最終審査|最終|[一二三四五六七八九十]+次(?:審査)?|"
          r"敗者復活|ラストチャンス|スタートダッシュ|スタダ|1st|2nd|3rd|[0-9]+th|last|final|"
          r"前半|後半|day ?[0-9]+|第[0-9]+(?:ステージ|ラウンド|段階)|round ?[0-9]+|イベント|審査")
_STAGE_RE = re.compile(_STAGE, re.I)
_BRACKET = re.compile(r"[【\[（(][^】\])）]*[】\])）]")
_PUNCT = re.compile(r"[\s\W_ー‐-]+")


def _fold(name: str) -> str:
    return unicodedata.normalize("NFKC", name or "").lower()


def core_name(name: str | None) -> str:
    """段階の言葉と、段階の言葉の入ったかっこを除いた名前 (くらべるため)。年の数字などは残す。"""
    s = _fold(name or "")
    s = _BRACKET.sub(lambda m: "" if _STAGE_RE.search(m.group(0)) else m.group(0), s)
    s = _STAGE_RE.sub("", s)
    return _PUNCT.sub("", s)


def entrance(name: str | None) -> str | None:
    """入口の枠 (【SR枠】【男性枠】など)。無ければ None。"""
    m = re.search(r"【([^】]*枠[^】]*)】", unicodedata.normalize("NFKC", name or ""))
    return m.group(1) if m else None


def stage_label(name: str | None) -> str | None:
    """名前の中の段階の言葉 (表に出す)。"""
    s = unicodedata.normalize("NFKC", name or "")
    words = [w for w in re.findall(r"予選|準々決勝|準決勝|決勝[・ ]?(?:前半|後半)?|セミファイナル|ファイナル|本選|本戦|最終審査|"
                                    r"[一二三四五六七八九十]+次審査|敗者復活|ラストチャンス|スタートダッシュ|スタダ|"
                                    r"1st|2nd|3rd|[0-9]+th|Last|Final", s, re.I)]
    return "・".join(dict.fromkeys(words)) or None


def similar(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def _rooms(s: Store, eid: int) -> set[int]:
    return {r[0] for r in s.all("SELECT room_id FROM room_events WHERE event_id=?", (eid,))}


def _place(s: Store, eid: int, series_id: int, stage_no: int, label: str | None, how: str, now: float) -> None:
    s.conn.execute("""INSERT INTO event_series(event_id, series_id, stage_no, stage, how, at) VALUES (?, ?, ?, ?, ?, ?)
                      ON CONFLICT(event_id) DO UPDATE SET series_id=excluded.series_id, stage_no=excluded.stage_no,
                        stage=excluded.stage, how=excluded.how, at=excluded.at""", (eid, series_id, stage_no, label, how, now))


def link_event(s: Store, e: Any, now: float) -> int | None:
    """1 つのイベントを、前の段階のイベントの大会につなぐ (つないだら大会の番号)。e は events の行。
    名前に段階の言葉 (予選・決勝・2nd など) が無いイベントは、前の段階を探さない (毎月ある同じ名前のイベントを、大会とまちがえないため)。"""
    eid, start = int(e["event_id"]), e["start"]
    manual = s.one("SELECT series_id FROM event_series_manual WHERE event_id=?", (eid,))
    if manual is not None:
        return manual["series_id"]  # 手で決めたもの (外したものは None) は自動で変えない
    core = core_name(e["name"])
    label = stage_label(e["name"])
    rooms = _rooms(s, eid) if label else set()
    best: tuple[tuple[float, float], dict[str, Any]] | None = None
    if len(rooms) >= SERIES_MIN_ROOMS and start:
        # 前の段階の候補: このイベントのルームが出た、少し前に終わったイベント (ルームごとに主キーで引く)
        shared: dict[int, set[int]] = {}
        for rid in rooms:
            for r in s.all("""SELECT re.event_id FROM room_events re JOIN events o USING(event_id)
                              WHERE re.room_id=? AND re.event_id != ? AND o.end IS NOT NULL AND o.end <= ? + 86400 AND o.end >= ?""",
                           (rid, eid, start, start - SERIES_GAP)):
                shared.setdefault(r[0], set()).add(rid)
        # 入口の分かれた枠 (同じ大会の同じ段階) は、まとめてルームの重なりを数える
        groups: dict[Any, dict[str, Any]] = {}
        for pid, got in shared.items():
            p = s.one("""SELECT e.event_id, e.name, e.end, es.series_id, es.stage_no FROM events e
                         LEFT JOIN event_series es USING(event_id) WHERE e.event_id=?""", (pid,))
            if p is None:
                continue
            sim = similar(core, core_name(p["name"]))
            if sim < SERIES_NAME:
                continue
            key = (p["series_id"], p["stage_no"]) if p["series_id"] is not None else ("event", pid)
            g = groups.setdefault(key, {"rooms": set(), "sim": 0.0, "event": p, "end": 0})
            g["rooms"] |= got
            g["sim"] = max(g["sim"], sim)
            g["end"] = max(g["end"], p["end"] or 0)
        # 前の段階の中でも、いちばん近い (あとに終わった) 段階につなぐ (勝ち上がったルームは、それより前の段階にもいるので)
        for g in groups.values():
            share = len(g["rooms"]) / len(rooms)
            score = (g["end"], g["sim"] + share)
            if share >= SERIES_OVERLAP and (best is None or score > best[0]):
                best = (score, g)
    if best is not None:
        p = best[1]["event"]
        if p["series_id"] is None:  # 前の段階は、まだ大会になっていなかった: 前の段階から大会を始める
            _place(s, int(p["event_id"]), int(p["event_id"]), 1, stage_label(p["name"]), "auto", now)
            sid, no = int(p["event_id"]), 1
        else:
            sid, no = p["series_id"], p["stage_no"] or 1
        _place(s, eid, sid, no + 1, label, "auto", now)
        return sid
    # 同じ時期・同じ名前の、入口のちがう枠 (ルームは重ならない): 同じ大会の同じ段階
    if start and core:
        for r in s.all("""SELECT es.series_id, es.stage_no, o.name FROM events o JOIN event_series es USING(event_id)
                          WHERE o.start BETWEEN ? AND ? AND o.event_id != ? AND es.series_id IS NOT NULL""",
                       (start - PARALLEL_DAYS * 86400, start + PARALLEL_DAYS * 86400, eid)):
            if core_name(r["name"]) == core:
                _place(s, eid, r["series_id"], r["stage_no"], label, "auto", now)
                return r["series_id"]
    old = s.one("SELECT series_id, stage_no, stage FROM event_series WHERE event_id=?", (eid,))
    if old is not None and old["series_id"] is not None:
        # 前に大会に入れたもの (大会の最初の段階など) は、前の段階が見つからなくても外さない
        _place(s, eid, old["series_id"], old["stage_no"], old["stage"] or label, "auto", now)
        return old["series_id"]
    _place(s, eid, None, None, label, "auto", now)  # 大会ではない (調べたしるし)
    return None


def step(s: Store, now: float | None = None) -> bool:
    """まだ調べていないイベント (参加ルームを読み直したものも) を、始まりの順に SERIES_STEP 件まとめる。することが無ければ False。"""
    now = time.time() if now is None else now
    rows = s.all("""SELECT e.event_id, e.name, e.start, e.end, e.rooms_at FROM events e LEFT JOIN event_series es USING(event_id)
                    WHERE e.start IS NOT NULL AND e.rooms_at IS NOT NULL AND (es.event_id IS NULL OR es.at < e.rooms_at)
                    ORDER BY e.start LIMIT ?""", (SERIES_STEP,))
    if not rows:
        return False
    with s.tx():
        for e in rows:
            link_event(s, e, now)
            # 調べたしるし (手で決めたものも。参加ルームの読み直しの時刻より前にしない: 同じイベントを何度も調べないため)
            s.conn.execute("""INSERT INTO event_series(event_id, at) VALUES (?, ?)
                              ON CONFLICT(event_id) DO UPDATE SET at=MAX(COALESCE(at, 0), excluded.at)""",
                           (e["event_id"], max(now, e["rooms_at"] or 0)))
            _recheck_later(s, e)
    return True


def _recheck_later(s: Store, e: Any) -> None:
    """前の段階の参加ルームがあとから分かった時のため、名前の似た、少しあとに始まった、まだ大会に入っていない
    段階のイベントを、もう一度調べるしるしを付ける。"""
    if not e["end"]:
        return
    core = core_name(e["name"])
    for r in s.all("""SELECT o.event_id, o.name FROM events o JOIN event_series es USING(event_id)
                      WHERE es.series_id IS NULL AND es.stage IS NOT NULL AND es.how='auto' AND es.at > 0
                        AND o.start > ? AND o.start <= ?""", (e["start"], e["end"] + SERIES_GAP)):
        if similar(core, core_name(r["name"])) >= SERIES_NAME:
            s.conn.execute("UPDATE event_series SET at=0 WHERE event_id=?", (r["event_id"],))


# ------------------------------------------------------------------ 手で直す
def set_manual(s: Store, event_id: int, to_event: int | None, now: float | None = None) -> dict[str, Any]:
    """手で直す: to_event のイベントと同じ大会につなぐ (to_event が None なら、大会から外す)。"""
    now = time.time() if now is None else now
    eid = int(event_id)
    me = s.one("SELECT name FROM events WHERE event_id=?", (eid,))
    if me is None:
        raise ValueError("イベントが見つかりません")
    if to_event is None:
        with s.tx():
            s.conn.execute("INSERT OR REPLACE INTO event_series_manual(event_id, series_id, at) VALUES (?, NULL, ?)", (eid, now))
            _place(s, eid, None, None, stage_label(me["name"]), "manual", now)
        return {"event_id": eid, "series_id": None}
    to = int(to_event)
    target = s.one("SELECT series_id, stage_no FROM event_series WHERE event_id=?", (to,))
    other = s.one("SELECT name FROM events WHERE event_id=?", (to,))
    if other is None or to == eid:
        raise ValueError("つなぐ先のイベントが見つかりません")
    with s.tx():
        if target is None or target["series_id"] is None:  # つなぐ先が大会でなければ、つなぐ先から大会を始める
            s.conn.execute("INSERT OR REPLACE INTO event_series_manual(event_id, series_id, at) VALUES (?, ?, ?)", (to, to, now))
            _place(s, to, to, 1, stage_label(other["name"]), "manual", now)
            target = {"series_id": to, "stage_no": 1}
        sid = target["series_id"]
        s.conn.execute("INSERT OR REPLACE INTO event_series_manual(event_id, series_id, at) VALUES (?, ?, ?)", (eid, sid, now))
        _place(s, eid, sid, 0, stage_label(me["name"]), "manual", now)
        renumber(s, sid)
    return {"event_id": eid, "series_id": sid}


def renumber(s: Store, series_id: int) -> None:
    """大会の段階の順番を、始まりの順に付け直す (同じ時期に始まった枠は、同じ順番)。"""
    rows = s.all("""SELECT es.event_id, e.start FROM event_series es JOIN events e USING(event_id)
                    WHERE es.series_id=? ORDER BY e.start""", (series_id,))
    no, last = 0, None
    for r in rows:
        if last is None or (r["start"] or 0) - last > PARALLEL_DAYS * 86400:
            no += 1
            last = r["start"] or 0
        s.conn.execute("UPDATE event_series SET stage_no=? WHERE event_id=?", (no, r["event_id"]))


# ------------------------------------------------------------------ 大会の分析
def series_of(s: Store, event_id: int) -> dict[str, Any] | None:
    r = s.one("SELECT series_id, stage_no, stage FROM event_series WHERE event_id=?", (int(event_id),))
    if r is None or r["series_id"] is None:
        return None
    n = s.scalar("SELECT MAX(stage_no) FROM event_series WHERE series_id=?", (r["series_id"],))
    root = s.one("SELECT name FROM events WHERE event_id=?", (r["series_id"],))
    return {"series_id": r["series_id"], "stage_no": r["stage_no"], "stages": n, "stage": r["stage"],
            "name": root["name"] if root is not None else None}


def series_list(s: Store, limit: int = 200) -> list[dict[str, Any]]:
    rows = s.all("""SELECT es.series_id, COUNT(*) AS events, MAX(es.stage_no) AS stages, MIN(e.start) AS start, MAX(e.end) AS end
                    FROM event_series es JOIN events e USING(event_id) WHERE es.series_id IS NOT NULL
                    GROUP BY es.series_id ORDER BY MAX(e.end) DESC LIMIT ?""", (limit,))
    out = []
    for r in rows:
        root = s.one("SELECT name FROM events WHERE event_id=?", (r["series_id"],))
        out.append({**dict(r), "name": root["name"] if root is not None else str(r["series_id"])})
    return out


def series_detail(s: Store, series_id: int) -> dict[str, Any] | None:
    """大会のページ: 段階ごとの勝ち上がりの線・ポイントの増え方・大口のギフターが残るか・無料の星から有料に変わった人・大会全体の合計。"""
    sid = int(series_id)
    evs = [dict(r) for r in s.all("""SELECT e.event_id, e.name, e.key, e.start, e.end, e.type, es.stage_no, es.stage, es.how
                                     FROM event_series es JOIN events e USING(event_id) WHERE es.series_id=?
                                     ORDER BY es.stage_no, e.start""", (sid,))]
    if not evs:
        return None
    pts: dict[int, dict[int, tuple[int, int | None]]] = {}  # event_id -> {room_id: (point, rank)}
    for e in evs:
        pts[e["event_id"]] = {r["room_id"]: (r["point"] or 0, r["rank"]) for r in
                              s.all("SELECT room_id, point, rank FROM room_events WHERE event_id=?", (e["event_id"],))}
    stages = sorted({e["stage_no"] for e in evs})
    by_stage = {no: [e for e in evs if e["stage_no"] == no] for no in stages}
    rooms_in = {no: {rid for e in by_stage[no] for rid in pts[e["event_id"]]} for no in stages}
    lines = []
    for i, no in enumerate(stages):
        later = set().union(*(rooms_in[x] for x in stages[i + 1:])) if i + 1 < len(stages) else set()
        for e in by_stage[no]:
            rows = pts[e["event_id"]]
            adv = [(p, rk) for rid, (p, rk) in rows.items() if rid in later]
            out = [(p, rk) for rid, (p, rk) in rows.items() if rid not in later]
            days = max(1.0, ((e["end"] or 0) - (e["start"] or 0)) / 86400)
            lines.append({"event_id": e["event_id"], "name": e["name"], "stage_no": no, "stage": e["stage"], "entrance": entrance(e["name"]),
                          "start": e["start"], "end": e["end"], "rooms": len(rows), "advanced": len(adv) if later else None,
                          "line_point": min(p for p, _ in adv) if adv else None,
                          "line_rank": max((rk for _, rk in adv if rk), default=None) if adv else None,
                          "best_out": max((p for p, _ in out), default=None) if later else None,
                          "median": median([p for p, _ in rows.values()]) if rows else None, "days": days,
                          "total": sum(p for p, _ in rows.values())})
    # ポイントの増え方: 続けて出たルームの、段階ごとの 1 日あたりのポイントの倍率 (真ん中の値)
    growth = []
    for a, b in zip(stages, stages[1:]):
        ra = {rid: p for e in by_stage[a] for rid, (p, _) in pts[e["event_id"]].items()}
        rb = {rid: p for e in by_stage[b] for rid, (p, _) in pts[e["event_id"]].items()}
        da = max(1.0, sum((e["end"] or 0) - (e["start"] or 0) for e in by_stage[a]) / len(by_stage[a]) / 86400)
        db = max(1.0, sum((e["end"] or 0) - (e["start"] or 0) for e in by_stage[b]) / len(by_stage[b]) / 86400)
        ratios = [(rb[r] / db) / (ra[r] / da) for r in ra.keys() & rb.keys() if ra[r] > 0]
        growth.append({"from": a, "to": b, "rooms": len(ratios), "ratio": median(ratios) if ratios else None})
    # ギフター: 大会全体の合計と、段階ごとに残っているか
    per: dict[int, dict[int, int]] = {}
    days_of = {e["event_id"]: max(1.0, ((e["end"] or 0) - (e["start"] or 0)) / 86400) for e in evs}
    stage_of = {e["event_id"]: e["stage_no"] for e in evs}
    for e in evs:
        for r in s.all("SELECT user_id, point FROM contributions WHERE event_id=?", (e["event_id"],)):
            u = per.setdefault(r["user_id"], {})
            u[e["event_id"]] = u.get(e["event_id"], 0) + (r["point"] or 0)
    totals = sorted(((uid, sum(v.values())) for uid, v in per.items()), key=lambda x: -x[1])
    top = [uid for uid, _ in totals[:20]]
    stay = []
    for no in stages:
        ids = {e["event_id"] for e in by_stage[no]}
        stay.append({"stage_no": no, "top": sum(1 for uid in top if ids & per[uid].keys()), "of": len(top),
                     "gifters": sum(1 for v in per.values() if ids & v.keys())})
    # 無料の星だけ (1 日あたり STAR_PER_DAY 以下) で始めて、あとの段階で 1 日あたりそれより多く送った人 (目安)
    first_ids = {e["event_id"] for e in by_stage[stages[0]]}
    later_ids = {e["event_id"] for e in evs if e["stage_no"] != stages[0]}
    free_to_paid = 0
    started_free = 0
    for v in per.values():
        f = [v[x] / days_of[x] for x in first_ids if x in v]
        if not f:
            continue
        if max(f) <= STAR_PER_DAY:
            started_free += 1
            if any(v[x] / days_of[x] > STAR_PER_DAY for x in later_ids if x in v):
                free_to_paid += 1
    names = {r[0]: r[1] for uid in [u for u, _ in totals[:30]] for r in [s.one("SELECT user_id, name FROM users WHERE user_id=?", (uid,))] if r}
    room_total: dict[int, int] = {}
    room_stages: dict[int, int] = {}
    for e in evs:
        for rid, (p, _) in pts[e["event_id"]].items():
            room_total[rid] = room_total.get(rid, 0) + p
            room_stages[rid] = max(room_stages.get(rid, 0), e["stage_no"])
    top_rooms = []
    for rid, p in sorted(room_total.items(), key=lambda x: -x[1])[:30]:
        r = s.one("SELECT key, name FROM rooms WHERE room_id=?", (rid,))
        top_rooms.append({"room_id": rid, "key": (r["key"] if r else None) or str(rid), "name": (r["name"] if r else None) or str(rid),
                          "points": p, "reached": room_stages[rid]})
    return {"series_id": sid, "name": evs[0]["name"], "stages": len(stages), "events": evs, "lines": lines, "growth": growth,
            "stay": stay, "free_to_paid": free_to_paid, "started_free": started_free, "star_per_day": STAR_PER_DAY,
            "total_points": sum(room_total.values()), "rooms": len(room_total), "gifters": len(per),
            "top_gifters": [{"user_id": uid, "name": names.get(uid), "points": p,
                             "stages": len({stage_of[x] for x in per[uid]})} for uid, p in totals[:30]],
            "top_rooms": top_rooms}
