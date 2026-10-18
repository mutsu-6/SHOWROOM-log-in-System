"""大会 (ミスコン・オーディションなど、段階ごとに別のイベントになっている大きな大会) のまとまり。

段階ごとのイベントはそのまま残し、event_series を足す。大会は 1 本道とは限らないので、木の形にする:
大会 → 道すじ (【aw本選権#1】・【SR枠】・ラストチャンス など) → 段階。各イベントに「前の段階のイベント」(parents) を持ち、
道すじが合流する段階 (本選権#1〜#3 の決勝から【aw枠】本選 1st へ、など) は前の段階が複数になる。
SHOWROOM の「親のイベント」(parent_event_id) は、ほとんど入っていないので使わない。まとめる手がかり:
- 名前: 【】の印から取った大会の名前 (フレキャン2025・ミスサークル など) が同じか、段階の言葉を除いた名前がよく似ている。
  年・第何回・何月がちがえば別の大会
- 期間: 前の段階が終わってから SERIES_GAP 日のうちに、次の段階が始まる (大きな大会は半年ほど続く)
- ルーム: 次の段階のルームの多く (SERIES_OVERLAP 以上) が、同じ大会のそれより前の段階にもいる (room_events でくらべる)
同じ時期に、同じ大会の名前で、ルームの重ならないイベント (【SR枠】・枠 A/B/C など) は、同じ段階の別の道すじとしてまとめる。
印だけでは、ほかのオーディションの中の枠か大会の本体かを分けられないので、自動でまちがえた時は、
管理画面で手でつないだり外したりでき (event_series_manual)、手で決めたものは自動で変えない。SHOWROOM には問い合わせない。
"""

from __future__ import annotations

import difflib
import re
import time
import unicodedata
from statistics import median
from typing import Any

from .research_store import Store

SERIES_VERSION = 3  # まとめ方を変えた時に上げる (自動でまとめた分を、作り直す)
SERIES_GAP = 200 * 86400  # 前の段階が終わってから、次の段階が始まるまでの日数の上限 (大きな大会は半年ほど続く)
SERIES_MAX = 365 * 86400  # 大会の最初の段階の始まりから、最後の段階の終わりまでの上限 (くり返し開かれるイベントをまとめないため)
FINAL_SHARE = 0.8  # 名前がちがっても、ファイナル審査などで、ルームのこれだけが同じ大会の前の段階にいればつなぐ
FINAL_MIN_ROOMS = 5  # その時の参加ルームの下限
ENTRANCE_DAYS = 7  # 年・第何回の同じ、別の入口 (【SR枠】など) は、大会の期間からこの日数のずれまで入れる
SERIES_OVERLAP = 0.5  # 次の段階のルームのうち、同じ大会の前の段階にもいたルームの割合の下限
SERIES_NAME = 0.55  # 段階の言葉を除いた名前の似ている度合い (0〜1) の下限
SERIES_MIN_ROOMS = 3  # これより参加ルームの少ないイベントは、ルームの重なりでまとめない
PARENT_SHARE = 0.1  # 前の段階とみる、ほかの道すじのイベントから来たルームの割合の下限 (2 ルーム以上)
PARALLEL_DAYS = 2  # 同じ名前で、始まりがこの日数のうちのイベントは、同じ段階 (入口のちがう枠) とみる
SERIES_STEP = 100  # 1 回に調べるイベントの数
STAR_PER_DAY = 320  # 無料の星だけで 1 日に送れるポイントの目安 (キラキラ星 200 + 広告の抽選の星・種 120。2.5 倍にならない)

_STAGE = (r"予選|準々決勝|準決勝|決勝|セミファイナル|ファイナル|本選(?!権)|本戦|最終審査|最終|[一二三四五六七八九十]+次(?:審査)?|"
          r"敗者復活|ラストチャンス|スタートダッシュ|スタダ|アピールタイム|1st|2nd|3rd|[0-9]+th|last|final|"
          r"前半|後半|day ?[0-9]+|第[0-9]+(?:ステージ|ラウンド|段階)|round ?[0-9]+|イベント|審査")
_STAGE_RE = re.compile(_STAGE, re.IGNORECASE)
# 道すじ (入口・枠) の言葉。大会の名前を取り出す時に除く
_ROUTE_RE = re.compile(r"本選権\s*#?\s*[0-9]+|既存|枠\s*[a-z0-9]?(?![a-z0-9])|(?<![a-z])(?:aw|sr|tp)(?![a-z])",
                       re.IGNORECASE)
_BRACKET = re.compile(r"[【\[（(]([^】\])）]*)[】\])）]")
_PUNCT = re.compile(r"[\s\W_‐-]+")


# 大会の名前の言いかえ (英語の正式名 → 名前の頭の【】の短い名前)
_ALIASES = ((re.compile(r"miss\s*circle\s*contest|ミスサークル", re.IGNORECASE), "ミスサー"),
            (re.compile(r"fresh\s*campus\s*contest|フレッシュキャンパス(?:コンテスト)?", re.IGNORECASE), "フレキャン"))


def _fold(name: str | None) -> str:
    s = unicodedata.normalize("NFKC", name or "").lower()
    for rx, to in _ALIASES:
        s = rx.sub(to, s)
    return s


def _bare(text: str) -> str:
    """道すじ・段階の言葉と記号を除いたもの。"""
    return _PUNCT.sub("", _STAGE_RE.sub("", _ROUTE_RE.sub("", text)))


def brand(name: str | None) -> str | None:
    """【】の印から取った大会の名前 (【フレキャン2025 枠A】→ フレキャン2025、【FRESH CAMPUS CONTEST 2025】→ フレキャン2025)。
    道すじ・段階の言葉だけの印 (【SR枠】【aw本選権#1】【予選】) なら None。枠の印 (【TGCスクール枠】【ミスサー枠】) は、
    ほかのオーディションの中の枠のことが多いので、年か第何回が入っている時だけ使う。"""
    for m in _BRACKET.finditer(_fold(name)):
        if "枠" in m.group(1) and not _has_num(m.group(1)):
            continue
        b = _bare(m.group(1))
        if len(b) >= 2 and not (b.isascii() and len(b) <= 3):
            return b
    return None


def core_name(name: str | None) -> str:
    """道すじ・段階の言葉と、それだけのかっこを除いた名前 (くらべるため)。年の数字などは残す。"""
    s = _BRACKET.sub(lambda m: _bare(m.group(1)), _fold(name))
    return _bare(s)


def route_label(name: str | None) -> str | None:
    """名前の中の道すじ (本選権#1・ラストチャンス・SR枠・枠A など)。無ければ None。"""
    s = unicodedata.normalize("NFKC", name or "")
    m = re.search(r"本選権\s*#?\s*([0-9]+)", s)
    if m:
        return f"本選権#{m.group(1)}"
    if "本選権" in s:
        return "本選権 (共通)"  # 番号の無い本選権 (アピールタイムなど、本選権の道すじに共通)
    m = re.search(r"ラストチャンス|敗者復活", s)
    if m:
        return m.group(0)
    m = re.search(r"([^\s【】\[\]()（）]{0,8}?枠\s*[A-Za-z0-9]?)(?![A-Za-z0-9])", s)
    return m.group(1).strip() if m else None


def entrance(name: str | None) -> str | None:
    """入口の枠 (【SR枠】【男性枠】など)。無ければ None。"""
    m = re.search(r"【([^】]*枠[^】]*)】", unicodedata.normalize("NFKC", name or ""))
    return m.group(1) if m else None


def stage_label(name: str | None) -> str | None:
    """名前の中の段階の言葉 (表に出す。これがあると、大会の段階かもしれないとみる)。
    「イベント」「審査」「スタートダッシュ」のような、どこにでも出る言葉は入れない。"""
    s = unicodedata.normalize("NFKC", name or "")
    words = [w for w in re.findall(r"予選|準々決勝|準決勝|決勝[・ ]?(?:前半|後半)?|セミファイナル|ファイナル(?:審査)?|本選(?!権)|本戦|最終審査|"
                                    r"[一二三四五六七八九十]+次審査|敗者復活|ラストチャンス|アピールタイム|"
                                    r"1st|2nd|3rd|[0-9]+th|Last|Final", s, re.IGNORECASE)]
    return "・".join(dict.fromkeys(words)) or None


def similar(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return difflib.SequenceMatcher(None, a, b).ratio()


# くり返しの回 (年・第何回・何月・vol.・第何弾)。どちらにも書いてあってちがえば、別の回
_NUMS = (re.compile(r"(20[0-9]{2})"), re.compile(r"第\s*([0-9]+)\s*回"), re.compile(r"([0-9]{1,2})\s*月"),
         re.compile(r"vol\s*\.?\s*([0-9]+)", re.IGNORECASE), re.compile(r"第\s*([0-9]+)\s*弾"))
_FINAL = re.compile(r"ファイナル|最終審査|最終選考|決勝|グランプリ|final", re.IGNORECASE)


def _has_num(name: str | None) -> bool:
    """年か第何回が書いてある (大会らしい名前)。"""
    f = _fold(name)
    return bool(_NUMS[0].search(f) or _NUMS[1].search(f))


def _final(name: str | None) -> bool:
    return bool(_FINAL.search(_fold(name)))


def conflict(a: str | None, b: str | None) -> bool:
    """年・第何回・何月・vol.・第何弾が、どちらにも書いてあって、ちがう (別の回)。"""
    fa, fb = _fold(a), _fold(b)
    for rx in _NUMS:
        x, y = set(rx.findall(fa)), set(rx.findall(fb))
        if x and y and not x & y:
            return True
    return False


def same_name(e_name: str | None, p_name: str | None) -> bool:
    """同じ大会の名前か (【】の大会の名前が同じか、段階・道すじの言葉があって、それを除いた名前がよく似ている)。"""
    if conflict(e_name, p_name):
        return False
    b = brand(e_name)
    if b and b == brand(p_name) and ((_has_num(e_name) and _has_num(p_name)) or stage_label(e_name)):
        # 【】の大会の名前が同じで、年 (第何回) か段階の言葉もある (段階ごとの名前がまったくちがう大会のため。
        # 年も段階の言葉も無い、くり返しのイベントは除く)
        return True
    if not (stage_label(e_name) or route_label(e_name)):
        return False  # 段階の言葉の無い、毎月ある同じ名前のイベントを、大会とまちがえないため
    return similar(core_name(e_name), core_name(p_name)) >= SERIES_NAME


def _eligible(name: str | None) -> bool:
    """大会の段階かもしれない名前か (段階・道すじの言葉か、【】の大会の名前がある)。"""
    return bool(stage_label(name) or route_label(name) or brand(name))


def _rooms(s: Store, eid: int) -> set[int]:
    return {r[0] for r in s.all("SELECT room_id FROM room_events WHERE event_id=?", (eid,))}


def _parents(text: str | None) -> list[int]:
    return [int(x) for x in (text or "").split(",") if x.strip().isdigit()]


def _place(s: Store, eid: int, name: str | None, series_id: int | None, stage_no: int | None, parents: list[int],
           route: str | None, how: str, now: float) -> None:
    s.conn.execute("""INSERT INTO event_series(event_id, series_id, stage_no, stage, how, at, parents, route, brand)
                      VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                      ON CONFLICT(event_id) DO UPDATE SET series_id=excluded.series_id, stage_no=excluded.stage_no,
                        stage=excluded.stage, how=excluded.how, at=excluded.at, parents=excluded.parents,
                        route=excluded.route, brand=excluded.brand""",
                   (eid, series_id, stage_no, stage_label(name), how, now, ",".join(map(str, parents)) or None, route, brand(name)))


def _series_span(s: Store, sid: Any) -> tuple[float, float, list[str]]:
    """大会の始まり・終わりと、入っているイベントの名前 (第何回などがちがうものを混ぜないため)。"""
    rows = s.all("""SELECT e.start, e.end, e.name FROM event_series es JOIN events e USING(event_id)
                    WHERE es.series_id=? AND es.series_id IS NOT NULL""", (sid,))
    if not rows:
        return 0.0, 0.0, []
    return (min(r["start"] or 0 for r in rows), max(r["end"] or 0 for r in rows), [r["name"] for r in rows])


def link_event(s: Store, e: Any, now: float) -> int | None:
    """1 つのイベントを、前の段階のイベントの大会につなぐ (つないだら大会の番号)。e は events の行。"""
    eid, start, end, name = int(e["event_id"]), e["start"], e["end"] or e["start"], e["name"]
    manual = s.one("SELECT series_id FROM event_series_manual WHERE event_id=?", (eid,))
    if manual is not None:
        return manual["series_id"]  # 手で決めたもの (外したものは None) は自動で変えない
    route = route_label(name)
    final = _final(name)
    rooms = _rooms(s, eid) if (_eligible(name) or final) else set()
    members: list[tuple[dict[str, Any], set[int]]] = []
    sid: int | None = None
    if len(rooms) >= SERIES_MIN_ROOMS and start:
        # 前の段階の候補: このイベントのルームが出た、少し前に終わったイベント (ルームごとに主キーで引く)
        shared: dict[int, set[int]] = {}
        for rid in rooms:
            for r in s.all("""SELECT re.event_id FROM room_events re JOIN events o USING(event_id)
                              WHERE re.room_id=? AND re.event_id != ? AND o.end IS NOT NULL AND o.end <= ? + 86400 AND o.end >= ?""",
                           (rid, eid, start, start - SERIES_GAP)):
                shared.setdefault(r[0], set()).add(rid)
        # 同じ大会の前の段階から来たルームを、大会ごと (まだ大会でなければイベントごと) にまとめて数える。
        # ファイナル審査などは、名前がちがっても、もう大会になっているイベントから来たルームを別に数える (loose)
        groups: dict[Any, dict[str, Any]] = {}
        for pid, got in shared.items():
            p = s.one("""SELECT e.event_id, e.name, e.start, e.end, es.series_id, es.stage_no, es.route FROM events e
                         LEFT JOIN event_series es USING(event_id) WHERE e.event_id=?""", (pid,))
            if p is None or conflict(name, p["name"]):
                continue
            ok = same_name(name, p["name"])
            if not ok and not (final and p["series_id"] is not None):
                continue
            key = p["series_id"] if p["series_id"] is not None else ("event", pid)
            g = groups.setdefault(key, {"key": key, "rooms": set(), "loose": set(), "members": []})
            (g["rooms"] if ok else g["loose"]).update(got)
            g["members"].append((dict(p), got))
        # 大会の期間 (1 年まで) と、第何回などが、ほかの段階と合うものだけ
        for key, g in list(groups.items()):
            if isinstance(key, tuple):
                first, names = g["members"][0][0]["start"] or start, [g["members"][0][0]["name"]]
            else:
                first, _, names = _series_span(s, key)
            g["first"] = first
            if end - first > SERIES_MAX or any(conflict(name, n) for n in names):
                del groups[key]
        need = max(2, PARENT_SHARE * len(rooms))
        use = [g for g in groups.values() if len(g["rooms"]) >= need]
        came = set().union(*(g["rooms"] for g in use)) if use else set()
        if not (use and len(came) / len(rooms) >= SERIES_OVERLAP) and final and len(rooms) >= FINAL_MIN_ROOMS:
            # 名前のちがうファイナル審査: ルームのほとんどが、1 つの大会の前の段階にいた
            loose = [g for g in groups.values() if not isinstance(g["key"], tuple)
                     and len(g["rooms"] | g["loose"]) >= FINAL_SHARE * len(rooms)]
            use = sorted(loose, key=lambda g: -len(g["rooms"] | g["loose"]))[:1]
            came = set(rooms) if use else set()
        # 道すじが合流する段階 (本選権#1〜#3 の決勝から本選 1st へ) は、道すじごとの大会を合わせて数える
        if use and len(came) / len(rooms) >= SERIES_OVERLAP:
            def order(g: dict[str, Any]) -> tuple[int, float]:
                if isinstance(g["key"], tuple):
                    return (2, g["first"])
                manual = s.one("SELECT 1 FROM event_series WHERE series_id=? AND how='manual' LIMIT 1", (g["key"],))
                return (0 if manual else 1, g["first"])
            use.sort(key=order)
            target = use[0]
            sid = int(target["members"][0][0]["event_id"]) if isinstance(target["key"], tuple) else int(target["key"])
            for g in use:
                if g is not target and (end - min(g["first"], target["first"]) > SERIES_MAX):
                    continue  # 合わせると 1 年を超える
                if isinstance(g["key"], tuple):
                    p, got = g["members"][0]
                    _place(s, int(p["event_id"]), p["name"], sid, 1, [], route_label(p["name"]), "auto", now)
                    members.append(({**p, "series_id": sid, "stage_no": 1, "route": route_label(p["name"])}, got))
                elif g is target:
                    members += g["members"]
                elif order(g)[0] == 1:  # ほかの道すじの大会を、この大会に合わせる (手で直した大会は変えない)
                    s.conn.execute("UPDATE event_series SET series_id=? WHERE series_id=?", (sid, g["key"]))
                    members += [({**p, "series_id": sid}, got) for p, got in g["members"]]
    if members:
        # 前の段階: 道すじごとに、いちばんあとに終わったイベント (ほかの道すじから少しだけ来たものは除く)
        last: dict[Any, tuple[dict[str, Any], set[int]]] = {}
        for p, got in members:
            k = p["route"]
            if k not in last or (p["end"] or 0) > (last[k][0]["end"] or 0):
                last[k] = (p, last[k][1] if k in last else set())
            last[k][1].update(got)
        need = max(2, PARENT_SHARE * len(rooms))
        cands = [(p, got) for p, got in last.values() if len(got) >= need] or [max(last.values(), key=lambda x: len(x[1]))]
        same = [(p, got) for p, got in cands if route and p["route"] == route]
        if same:  # 同じ道すじの前の段階があれば、そこにつなぐ (共通のアピールタイムなどを、前の段階にしない)
            parents = [same[0][0]]
        else:
            # ルームの多くが、もっとあとに終わった候補にも出ていれば、そちらが前の段階 (合流したあとの段階につなぐ)
            parents = [p for p, got in cands if not any((q["end"] or 0) > (p["end"] or 0) and len(got & qgot) * 2 >= len(got)
                                                       for q, qgot in cands)]
        no = max(p["stage_no"] or 1 for p in parents) + 1
        own = route or (parents[0]["route"] if len({p["route"] for p in parents}) == 1 else None)
        _place(s, eid, name, sid, no, sorted(int(p["event_id"]) for p in parents), own, "auto", now)
        return sid
    if start and _eligible(name):
        # 同じ時期・同じ大会の名前の、入口のちがう枠 (ルームは重ならない): 同じ大会の同じ段階。
        # 年か第何回が書いてあれば、大会の期間から ENTRANCE_DAYS 日ずれていても入れる (段階の言葉の無い【SR枠】など)
        wide = _has_num(name) and route is not None  # 広く探すのは、入口 (枠) の名前のあるものだけ (調べる量を増やしすぎないため)
        days = ENTRANCE_DAYS if wide else PARALLEL_DAYS
        lo = start - (SERIES_MAX if wide else PARALLEL_DAYS * 86400)
        for r in s.all("""SELECT o.event_id, o.name, o.start, es.series_id, es.stage_no, es.parents FROM events o JOIN event_series es USING(event_id)
                          WHERE o.start BETWEEN ? AND ? AND o.event_id != ? AND es.series_id IS NOT NULL ORDER BY ABS(o.start - ?)""",
                       (lo, start + days * 86400, eid, start)):
            if not same_name(name, r["name"]) or (wide and not _has_num(r["name"])):
                continue
            first, last_end, names = _series_span(s, r["series_id"])
            if any(conflict(name, n) for n in names) or end - first > SERIES_MAX:
                continue
            if abs((r["start"] or 0) - start) > PARALLEL_DAYS * 86400 and start > last_end + days * 86400:
                continue  # 大会の期間の外
            _place(s, eid, name, r["series_id"], r["stage_no"], _parents(r["parents"]), route, "auto", now)
            return r["series_id"]
    old = s.one("SELECT series_id, stage_no, parents, route FROM event_series WHERE event_id=?", (eid,))
    if old is not None and old["series_id"] is not None:
        # 前に大会に入れたもの (大会の最初の段階など) は、前の段階が見つからなくても外さない
        _place(s, eid, name, old["series_id"], old["stage_no"], _parents(old["parents"]), old["route"] or route, "auto", now)
        return old["series_id"]
    _place(s, eid, name, None, None, [], route, "auto", now)  # 大会ではない (調べたしるし)
    return None


def step(s: Store, now: float | None = None) -> bool:
    """まだ調べていないイベント (参加ルームを読み直したものも) を、始まりの順に SERIES_STEP 件まとめる。することが無ければ False。"""
    now = time.time() if now is None else now
    if s.meta("series_version") != SERIES_VERSION:  # まとめ方を変えた: 自動でまとめた分を作り直す (手で直した分は残す)
        with s.tx():
            s.conn.execute("DELETE FROM event_series WHERE how='auto' OR how IS NULL")
        s.set_meta("series_version", SERIES_VERSION)
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
    """前の段階の参加ルームがあとから分かった時のため、同じルームの出た、少しあとに始まった、まだ大会に入っていない
    イベントを、もう一度調べるしるしを付ける (ルームごとに主キーで引く)。"""
    if not e["end"] or not _eligible(e["name"]):
        return
    todo: set[int] = set()
    for rid in _rooms(s, int(e["event_id"])):
        for r in s.all("""SELECT o.event_id, o.name FROM room_events re JOIN events o USING(event_id) JOIN event_series es USING(event_id)
                          WHERE re.room_id=? AND o.start > ? AND o.start <= ? AND es.series_id IS NULL AND es.how='auto' AND es.at > 0""",
                       (rid, e["start"], e["end"] + SERIES_GAP)):
            if r["event_id"] not in todo and same_name(r["name"], e["name"]):
                todo.add(r["event_id"])
    for eid in todo:
        s.conn.execute("UPDATE event_series SET at=0 WHERE event_id=?", (eid,))


# ------------------------------------------------------------------ 手で直す
def set_manual(s: Store, event_id: int, to_event: int | None, now: float | None = None) -> dict[str, Any]:
    """手で直す: to_event のイベントと同じ大会につなぐ (to_event が None なら、大会から外す)。
    to_event がこのイベントより前に終わっていれば、その次の段階に、同じころなら同じ段階 (別の道すじ) にする。"""
    now = time.time() if now is None else now
    eid = int(event_id)
    me = s.one("SELECT name, start FROM events WHERE event_id=?", (eid,))
    if me is None:
        raise ValueError("イベントが見つかりません")
    if to_event is None:
        with s.tx():
            s.conn.execute("INSERT OR REPLACE INTO event_series_manual(event_id, series_id, at) VALUES (?, NULL, ?)", (eid, now))
            _place(s, eid, me["name"], None, None, [], route_label(me["name"]), "manual", now)
        return {"event_id": eid, "series_id": None}
    to = int(to_event)
    other = s.one("SELECT name, start, end FROM events WHERE event_id=?", (to,))
    if other is None or to == eid:
        raise ValueError("つなぐ先のイベントが見つかりません")
    target = s.one("SELECT series_id, stage_no, parents, route FROM event_series WHERE event_id=?", (to,))
    with s.tx():
        if target is None or target["series_id"] is None:  # つなぐ先が大会でなければ、つなぐ先から大会を始める
            s.conn.execute("INSERT OR REPLACE INTO event_series_manual(event_id, series_id, at) VALUES (?, ?, ?)", (to, to, now))
            _place(s, to, other["name"], to, 1, [], route_label(other["name"]), "manual", now)
            target = {"series_id": to, "stage_no": 1, "parents": None, "route": route_label(other["name"])}
        sid = target["series_id"]
        s.conn.execute("INSERT OR REPLACE INTO event_series_manual(event_id, series_id, at) VALUES (?, ?, ?)", (eid, sid, now))
        if other["end"] and me["start"] and other["end"] <= me["start"] + 86400:  # つなぐ先の次の段階
            _place(s, eid, me["name"], sid, (target["stage_no"] or 1) + 1, [to], route_label(me["name"]) or target["route"], "manual", now)
        else:  # 同じ段階の、別の道すじ
            _place(s, eid, me["name"], sid, target["stage_no"] or 1, _parents(target["parents"]), route_label(me["name"]), "manual", now)
    return {"event_id": eid, "series_id": sid}


# ------------------------------------------------------------------ 大会の分析
_TITLE_STAGE = re.compile(r"[-‐－・\s]*(?:" + _STAGE.replace("|イベント|審査", "") + r")[-‐－・\s]*", re.IGNORECASE)


def title(name: str | None) -> str | None:
    """大会の名前として出すもの (最初の段階の名前から、道すじ・段階の言葉だけのかっこと、段階の言葉を除く)。"""
    if not name:
        return name
    s = unicodedata.normalize("NFKC", name)
    s = _BRACKET.sub(lambda m: "" if not _bare(_fold(m.group(1))) else m.group(0), s)
    s = _TITLE_STAGE.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip() or name


def _root_name(s: Store, series_id: int) -> str | None:
    root = s.one("SELECT name FROM events WHERE event_id=?", (series_id,))
    return title(root["name"]) if root is not None else None


def series_of(s: Store, event_id: int) -> dict[str, Any] | None:
    r = s.one("SELECT series_id, stage_no, stage, route, parents FROM event_series WHERE event_id=?", (int(event_id),))
    if r is None or r["series_id"] is None:
        return None
    n = s.scalar("SELECT MAX(stage_no) FROM event_series WHERE series_id=?", (r["series_id"],))
    return {"series_id": r["series_id"], "stage_no": r["stage_no"], "stages": n, "stage": r["stage"], "route": r["route"],
            "parents": _parents(r["parents"]), "name": _root_name(s, r["series_id"])}


def series_list(s: Store, limit: int = 200) -> list[dict[str, Any]]:
    rows = s.all("""SELECT es.series_id, COUNT(*) AS events, MAX(es.stage_no) AS stages, COUNT(DISTINCT es.route) AS routes,
                           MIN(e.start) AS start, MAX(e.end) AS end
                    FROM event_series es JOIN events e USING(event_id) WHERE es.series_id IS NOT NULL
                    GROUP BY es.series_id ORDER BY MAX(e.end) DESC LIMIT ?""", (limit,))
    return [{**dict(r), "name": _root_name(s, r["series_id"]) or str(r["series_id"])} for r in rows]


def series_detail(s: Store, series_id: int) -> dict[str, Any] | None:
    """大会のページ: 道すじごとの段階・勝ち上がりの線・ポイントの増え方・大口のギフターが残るか・無料の星から有料に変わった人・合計。"""
    sid = int(series_id)
    evs = [dict(r) for r in s.all("""SELECT e.event_id, e.name, e.key, e.start, e.end, e.type, es.stage_no, es.stage, es.how,
                                            es.parents, es.route
                                     FROM event_series es JOIN events e USING(event_id) WHERE es.series_id=?
                                     ORDER BY es.stage_no, e.start""", (sid,))]
    if not evs:
        return None
    for e in evs:
        e["parents"] = _parents(e["parents"])
        e["stage_no"] = e["stage_no"] or 1
    ids = {e["event_id"] for e in evs}
    by_id = {e["event_id"]: e for e in evs}
    children: dict[int, list[int]] = {e["event_id"]: [] for e in evs}
    for e in evs:
        for p in e["parents"]:
            if p in children:
                children[p].append(e["event_id"])
    pts: dict[int, dict[int, tuple[int, int | None]]] = {}  # event_id -> {room_id: (point, rank)}
    for e in evs:
        pts[e["event_id"]] = {r["room_id"]: (r["point"] or 0, r["rank"]) for r in
                              s.all("SELECT room_id, point, rank FROM room_events WHERE event_id=?", (e["event_id"],))}
    days_of = {e["event_id"]: max(1.0, ((e["end"] or 0) - (e["start"] or 0)) / 86400) for e in evs}
    lines = []
    for e in evs:
        rows = pts[e["event_id"]]
        kids = children[e["event_id"]]
        nxt = {rid for c in kids for rid in pts[c]}
        adv = [(p, rk) for rid, (p, rk) in rows.items() if rid in nxt]
        out = [(p, rk) for rid, (p, rk) in rows.items() if rid not in nxt]
        lines.append({"event_id": e["event_id"], "name": e["name"], "stage_no": e["stage_no"], "stage": e["stage"],
                      "route": e["route"], "entrance": entrance(e["name"]), "parents": [p for p in e["parents"] if p in ids],
                      "children": kids, "start": e["start"], "end": e["end"], "rooms": len(rows),
                      "advanced": len(adv) if kids else None,
                      "line_point": min(p for p, _ in adv) if adv else None,
                      "line_rank": max((rk for _, rk in adv if rk), default=None) if adv else None,
                      "best_out": max((p for p, _ in out), default=None) if kids else None,
                      "median": median([p for p, _ in rows.values()]) if rows else None, "days": days_of[e["event_id"]],
                      "total": sum(p for p, _ in rows.values())})
    # ポイントの増え方: 前の段階 → 次の段階 (木の枝ごと) で続けて出たルームの、1 日あたりのポイントの倍率 (真ん中の値)
    growth = []
    for e in evs:
        for p in e["parents"]:
            if p not in by_id:
                continue
            ra, rb = pts[p], pts[e["event_id"]]
            ratios = [(rb[r][0] / days_of[e["event_id"]]) / (ra[r][0] / days_of[p]) for r in ra.keys() & rb.keys() if ra[r][0] > 0]
            growth.append({"from": p, "from_name": by_id[p]["name"], "to": e["event_id"], "to_name": e["name"],
                           "from_stage": by_id[p]["stage_no"], "to_stage": e["stage_no"],
                           "rooms": len(ratios), "ratio": median(ratios) if ratios else None})
    # 道すじ: 道すじごとのイベント (始まりの順)
    routes: dict[Any, list[int]] = {}
    for e in sorted(evs, key=lambda x: (x["start"] or 0)):
        routes.setdefault(e["route"], []).append(e["event_id"])
    route_list = [{"route": k, "events": v, "rooms": len({rid for x in v for rid in pts[x]})} for k, v in routes.items()]
    # ギフター: 大会全体の合計と、段階ごとに残っているか
    stages = sorted({e["stage_no"] for e in evs})
    per: dict[int, dict[int, int]] = {}
    for e in evs:
        for r in s.all("SELECT user_id, point FROM contributions WHERE event_id=?", (e["event_id"],)):
            u = per.setdefault(r["user_id"], {})
            u[e["event_id"]] = u.get(e["event_id"], 0) + (r["point"] or 0)
    totals = sorted(((uid, sum(v.values())) for uid, v in per.items()), key=lambda x: -x[1])
    top = [uid for uid, _ in totals[:20]]
    stay = []
    for no in stages:
        st_ids = {e["event_id"] for e in evs if e["stage_no"] == no}
        stay.append({"stage_no": no, "top": sum(1 for uid in top if st_ids & per[uid].keys()), "of": len(top),
                     "gifters": sum(1 for v in per.values() if st_ids & v.keys())})
    # 無料の星だけ (1 日あたり STAR_PER_DAY 以下) で始めて、あとの段階で 1 日あたりそれより多く送った人 (目安)
    first_ids = {e["event_id"] for e in evs if e["stage_no"] == stages[0]}
    later_ids = ids - first_ids
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
    names = {}
    for uid, _ in totals[:30]:
        r = s.one("SELECT name FROM users WHERE user_id=?", (uid,))
        if r is not None:
            names[uid] = r["name"]
    room_total: dict[int, int] = {}
    room_stages: dict[int, int] = {}
    room_routes: dict[int, set[Any]] = {}
    for e in evs:
        for rid, (p, _) in pts[e["event_id"]].items():
            room_total[rid] = room_total.get(rid, 0) + p
            room_stages[rid] = max(room_stages.get(rid, 0), e["stage_no"])
            room_routes.setdefault(rid, set()).add(e["route"])
    top_rooms = []
    for rid, p in sorted(room_total.items(), key=lambda x: -x[1])[:30]:
        r = s.one("SELECT key, name FROM rooms WHERE room_id=?", (rid,))
        top_rooms.append({"room_id": rid, "key": (r["key"] if r else None) or str(rid), "name": (r["name"] if r else None) or str(rid),
                          "points": p, "reached": room_stages[rid], "routes": sorted(x for x in room_routes[rid] if x)})
    return {"series_id": sid, "name": _root_name(s, sid) or evs[0]["name"], "stages": len(stages), "events": evs, "lines": lines,
            "routes": route_list, "growth": growth, "stay": stay, "free_to_paid": free_to_paid, "started_free": started_free,
            "star_per_day": STAR_PER_DAY, "total_points": sum(room_total.values()), "rooms": len(room_total), "gifters": len(per),
            "top_gifters": [{"user_id": uid, "name": names.get(uid), "points": p,
                             "stages": len({by_id[x]["stage_no"] for x in per[uid]})} for uid, p in totals[:30]],
            "top_rooms": top_rooms}
