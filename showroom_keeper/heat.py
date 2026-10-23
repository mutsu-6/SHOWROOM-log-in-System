"""ヒートマップ (株のヒートマップのように、四角の大きさと色で、どこが伸びてどこが落ちたかを一度に見る)。

SHOWROOM には問い合わせない (集めた情報を読むだけ)。
- ルーム: 毎日 3:00 / 15:00 (日本時間) で区切った 12 時間 (月曜 3:00 を週の始まりにする) ごとに、イベントで増えたポイント。
  イベントの終わりの日でまとめると動きが少ないので、1 時間ごとの推移 (event_progress) の差から、区切りが終わったあと
  1 回だけ数えて room_point_12h に残す (全ルームの収集の小さな仕事として、1 回に 1 区切りずつ)。
  四角の大きさはその期間のポイント、色は前の期間 (既定は先週の同じ区切り) からの増え減り。配信ジャンル (アイドル・モデル・芸人など。room_lives.genre) ごとにまとめる (タイプにも切り替えられる)。
- ギフター: 月ごとのイベントのポイント (user_month) を、最推しのルーム (gifter_status.top_room) ごとにまとめる。色は前の月からの増え減り。
- 地力と残弾 (`docs/戦術メモ.md`・調査の「戦力クラスの考え方」): ルームの四角の大きさを「地力」(主砲・主力・支えの人が、勝負の時に
  このルームに出せる量の合計)、色を「残弾」(そのうち今月まだ撃てる割合) にもできる。箱はリーグ (同じイベントに出られる相手) にもできる。
  ギフターのステータス (gifter_status) を作り終えたあと、サーバーが暇な時に 1 回だけ足し上げて room_power に残す。ページはそれを読むだけ。
"""

from __future__ import annotations

import heapq
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .event_analysis import LEAGUES
from .research_store import Store

log = logging.getLogger(__name__)

JST = timezone(timedelta(hours=9))
SLOT = 12 * 3600  # 区切りの長さ (秒)
ANCHOR = 324000  # 区切りの起点: 1970-01-05 (月) 3:00 日本時間。1 週間 = 14 区切りで、月曜 3:00〜15:00 が 0 番目
WEEK_SLOTS = 7 * 86400 // SLOT
READY_AFTER = 2 * 3600  # 区切りが終わって、これだけたってから数える (終わったイベントの最後の値が推移に入るのを待つ。秒)
START_NEAR = 2 * 3600  # 区切りの始まりの値が無い時、始まってこれだけのうちのイベントは 0 から数える (推移は始まって 1 時間以内に読む)
BACKFILL_DAYS = 28  # 初めて作る時に、さかのぼって作る日数 (先週との比べに 2 週間要る。それより前は推移が間引かれている)
ROOM_LIMIT = 300  # ヒートマップに出すルームの数 (今の期間の上位と、前の期間の上位)
GIFTER_LIMIT = 300  # ヒートマップに出すギフターの数 (今の月の上位と、前の月の上位)
GROUP_LIMIT = 40  # まとめる箱の数の上限 (残りは「そのほか」)
POWER_VERSION = 2  # 地力の作り方を変えたら上げる (作り直す)
CHUNK = 2000  # 1 回に足すギフターの数 (手元で 5 万人を 2.6 秒。サーバーで 9,273 人を 5 回・15 秒。2026-10-21 に Cursor が測った)
CHUNK_BUSY = 300  # 管理画面が使われている間に 1 回に足す数 (1 回を 0.5 秒ほどにして、ページを開くのと重なっても待たせない)
PEOPLE = 60  # ルームごとに残す、出せる量の多い人の数 (ルームのページの中のギフターのマップ)
SORTIE_UNKNOWN = 0.5  # 出動率が分からない人 (勝負の回が少ないルーム) の出動率 (gifter_status の power と同じ)
FIGHTERS = ("主砲", "主力", "支え")  # 地力に数えるクラス (このルームでの本気の上限を、決め手の量とくらべて分ける)
STATES = ("上り調子", "安定", "温存中", "枯渇", "離脱", "不明")


def slot_of(t: float) -> int:
    return int((t - ANCHOR) // SLOT)


def slot_start(i: int) -> float:
    return float(ANCHOR + int(i) * SLOT)


def _gains(s: Store, e: Any, t0: float, t1: float) -> dict[int, int] | None:
    """イベント e で、t0〜t1 にルームごとに増えたポイント (推移が無くて分からない時は None)。

    区切りの終わり (イベントが先に終わったら終わった時刻) までの最後の値から、始まりまでの最後の値を引く。
    イベントの始まり (イベントの詳細の started_at) が区切りの中なら、始まりは 0 から数える。減った分 (不正の取り消しなど) は数えない。
    """
    eid = e["event_id"]
    end_at = min(t1, e["end"]) if e["end"] else t1
    at1 = s.scalar("SELECT MAX(at) FROM event_progress WHERE event_id=? AND at<=?", (eid, end_at), None)
    if at1 is None or at1 <= t0:
        return None
    base: dict[int, int] = {}
    if not (e["start"] and e["start"] >= t0):
        at0 = s.scalar("SELECT MAX(at) FROM event_progress WHERE event_id=? AND at<=?", (eid, t0), None)
        if at0 is None:
            if not (e["start"] and e["start"] >= t0 - START_NEAR):
                return None  # 途中から集め始めたイベント: 区切りの始まりの値が分からない
        else:
            base = {r[0]: r[1] or 0 for r in s.all("SELECT room_id, point FROM event_progress WHERE event_id=? AND at=?", (eid, at0))}
    return {r[0]: max(0, (r[1] or 0) - base.get(r[0], 0))
            for r in s.all("SELECT room_id, point FROM event_progress WHERE event_id=? AND at=?", (eid, at1))}


def build_slot(s: Store, i: int) -> int:
    """区切り i の、ルームごとに増えたポイントを room_point_12h に入れ直す (戻り値はルームの数)。"""
    t0, t1 = slot_start(i), slot_start(i + 1)
    total: dict[int, list[int]] = {}
    for e in s.all("SELECT event_id, start, end FROM events WHERE progress_at IS NOT NULL AND start < ? AND end > ?", (t1, t0)):
        for rid, pt in (_gains(s, e, t0, t1) or {}).items():
            x = total.setdefault(rid, [0, 0])
            x[0] += pt
            x[1] += 1
    rows = [(i, rid, pt, n) for rid, (pt, n) in total.items() if pt > 0]
    with s.tx() as c:
        c.execute("DELETE FROM room_point_12h WHERE slot=?", (i,))
        c.executemany("INSERT INTO room_point_12h(slot, room_id, point, events) VALUES (?, ?, ?, ?)", rows)
    return len(rows)


def step(s: Store, now: float | None = None) -> bool:
    """終わって READY_AFTER たった区切りのうち、まだ作っていないものを 1 つ作る。することが無ければ False。"""
    now = time.time() if now is None else now
    last = slot_of(now - READY_AFTER) - 1
    nxt = s.meta("heat_next")
    nxt = slot_of(now - BACKFILL_DAYS * 86400) if nxt is None else max(int(nxt), last - BACKFILL_DAYS * 2)
    if nxt > last:
        return False
    build_slot(s, nxt)
    s.set_meta("heat_next", nxt + 1)
    return True


def _grouped(items: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    """items を key の値ごとの箱にまとめる (大きい順。GROUP_LIMIT を超えた箱は「そのほか」へ)。"""
    groups: dict[Any, dict[str, Any]] = {}
    for x in items:
        g = groups.setdefault(x[key], {"name": x[key], "items": []})
        g["items"].append(x)
    out = sorted(groups.values(), key=lambda g: -sum(x["size"] for x in g["items"]))
    if len(out) > GROUP_LIMIT:
        rest = {"name": "そのほか", "items": [x for g in out[GROUP_LIMIT - 1:] for x in g["items"]]}
        out = out[:GROUP_LIMIT - 1] + [rest]
    for g in out:
        g["items"].sort(key=lambda x: -x["size"])
        g["size"] = sum(x["size"] for x in g["items"])
        g["now"] = sum(x["now"] for x in g["items"])
        g["before"] = sum(x["before"] for x in g["items"])
        if "power" in g["items"][0]:
            g["power"] = sum(x["power"] for x in g["items"])
            g["left_pt"] = sum(x["left_pt"] for x in g["items"])
    return out


def _item(now_pt: int, before_pt: int) -> dict[str, Any]:
    """大きさ (今か前の多い方。送らなくなった人・ルームも見えるように) と、増え減りの割合 (前が 0 なら None = 新しく)。"""
    return {"now": now_pt, "before": before_pt, "size": max(now_pt, before_pt),
            "change": (now_pt - before_pt) / before_pt if before_pt else None}


def _by_room(s: Store, sql: str, ids: list[int]) -> dict[int, Any]:
    """room_id ごとの 1 行 (500 ずつ読む)。"""
    out: dict[int, Any] = {}
    for k in range(0, len(ids), 500):
        part = ids[k:k + 500]
        out.update({r[0]: r for r in s.all(sql.format(",".join("?" * len(part))), part)})
    return out


def rooms(state_dir: Path, slot: int | None = None, span: str = "12h", compare: str = "week", group: str = "genre",
          size: str = "point") -> dict[str, Any]:
    """ルームのヒートマップ。slot は見る区切り (期間の最後の区切り。既定は作り終えた最新)、span は 12h か week、
    compare は week (先週の同じ期間と比べる) か prev (すぐ前の期間と比べる。week の時は同じ)、
    group は箱の分け方: genre (配信ジャンル。株のヒートマップの業種のように)・type (ルームのタイプ)・league (リーグ)、
    size は四角の大きさ: point (その期間のポイント) か power (地力。地力の多い上位 ROOM_LIMIT ルーム)。
    どのルームにも地力・残弾 (room_power) を付ける (色を残弾にする時に使う)。"""
    span = "week" if span == "week" else "12h"
    group = group if group in ("type", "league") else "genre"
    size = "power" if size == "power" else "point"
    with Store(state_dir) as s:
        latest = s.scalar("SELECT MAX(slot) FROM room_point_12h", (), None)
        if latest is None:
            return {"slot": None, "items": [], "groups": [], "slots": []}
        slot = latest if slot is None else min(int(slot), int(latest))
        n = WEEK_SLOTS if span == "week" else 1
        back = WEEK_SLOTS if (span == "week" or compare != "prev") else 1
        cur = (slot - n + 1, slot)
        base = (cur[0] - back, cur[1] - back)

        def top(lo: int, hi: int) -> dict[int, int]:
            return {r[0]: r[1] for r in s.all("""SELECT room_id, SUM(point) FROM room_point_12h WHERE slot BETWEEN ? AND ?
                                                 GROUP BY room_id ORDER BY 2 DESC LIMIT ?""", (lo, hi, ROOM_LIMIT))}

        if size == "power":  # 地力の多い順 (地力の索引を上から読む)。ポイントは期間ごとに 1 回ずつ足し上げる
            powered = [r[0] for r in s.all("SELECT room_id FROM room_power INDEXED BY room_power_power ORDER BY power DESC LIMIT ?",
                                           (ROOM_LIMIT,))]
            ids = powered

            def total(lo: int, hi: int) -> dict[int, int]:
                return {r[0]: r[1] for r in s.all("SELECT room_id, SUM(point) FROM room_point_12h WHERE slot BETWEEN ? AND ? GROUP BY room_id",
                                                  (lo, hi))}

            now_pt, before_pt = total(*cur), total(*base)
        else:
            now_top, before_top = top(*cur), top(*base)
            ids = list(dict.fromkeys([*now_top, *before_top]))

            def points(lo: int, hi: int, known: dict[int, int]) -> dict[int, int]:
                out = dict(known)
                for rid in ids:
                    if rid not in out:
                        out[rid] = s.scalar("SELECT SUM(point) FROM room_point_12h WHERE slot BETWEEN ? AND ? AND room_id=?",
                                            (lo, hi, rid), 0) or 0
                return out

            now_pt, before_pt = points(*cur, now_top), points(*base, before_top)
        power = _by_room(s, "SELECT room_id, power, left_pt, guns, mains FROM room_power WHERE room_id IN ({})", ids)
        league = _by_room(s, "SELECT room_id, league_id FROM room_info WHERE room_id IN ({})", ids) if group == "league" else {}
        items = []
        for rid in ids:
            r = s.one("""SELECT r.key, r.name, p.type FROM rooms r LEFT JOIN room_profile p USING(room_id)
                         WHERE r.room_id=?""", (rid,))
            if group == "type":
                name = (r["type"] if r else None) or "タイプ未判定"
            elif group == "league":
                lg = league.get(rid)
                name = LEAGUES.get(lg["league_id"], "リーグ不明") if lg is not None and lg["league_id"] is not None else "リーグ不明"
            else:  # 最後の配信のジャンル (配信中のルームの一覧で、配信ごとに残している)
                name = s.scalar("""SELECT genre FROM room_lives WHERE room_id=? AND genre IS NOT NULL
                                   ORDER BY started_at DESC LIMIT 1""", (rid,), None) or "ジャンル未判定"
            pw = power.get(rid)
            item = {"room_id": rid, "key": r["key"] if r else None, "name": (r["name"] if r else None) or str(rid),
                    "group": name, **_item(now_pt.get(rid, 0), before_pt.get(rid, 0)),
                    "power": pw["power"] if pw else 0, "left_pt": pw["left_pt"] if pw else 0,
                    "guns": pw["guns"] if pw else 0, "mains": pw["mains"] if pw else 0}
            if size == "power":
                item["size"] = item["power"]
            items.append(item)
        items = [x for x in items if x["size"] > 0]
        slots = [r[0] for r in s.all("SELECT DISTINCT slot FROM room_point_12h WHERE slot > ? ORDER BY slot DESC",
                                     (latest - 4 * WEEK_SLOTS,))]
        return {"slot": slot, "span": span, "compare": "week" if back == WEEK_SLOTS else "prev", "group": group, "size": size,
                "power_at": s.scalar("SELECT MAX(at) FROM room_power", (), None),
                "from": slot_start(cur[0]), "to": slot_start(cur[1] + 1),
                "base_from": slot_start(base[0]), "base_to": slot_start(base[1] + 1),
                "groups": _grouped(items, "group"),
                "slots": [{"slot": i, "start": slot_start(i)} for i in slots]}


def _month_before(month: str) -> str:
    y, m = map(int, month.split("-"))
    return f"{y - 1}-12" if m == 1 else f"{y}-{m - 1:02d}"


def gifters(state_dir: Path, month: str | None = None) -> dict[str, Any]:
    """ギフターのヒートマップ。month (YYYY-MM。既定は前の月 = そろった最新の月) の、イベントの貢献ポイントを、
    最推しのルームごとにまとめる。色は前の月からの増え減り。"""
    with Store(state_dir) as s:
        this_month = datetime.now(JST).strftime("%Y-%m")
        months = [r[0] for r in s.all("SELECT DISTINCT month FROM user_month WHERE month <= ? ORDER BY month DESC LIMIT 24",
                                      (this_month,))]
        if not months:
            return {"month": None, "groups": [], "months": []}
        if month not in months:
            month = next((m for m in months if m < this_month), months[0])
        prev = _month_before(month)

        def top(m: str) -> dict[int, int]:
            return {r[0]: r[1] for r in s.all("""SELECT user_id, points FROM user_month INDEXED BY user_month_points
                                                 WHERE month=? ORDER BY points DESC LIMIT ?""", (m, GIFTER_LIMIT))}

        now_top, before_top = top(month), top(prev)
        ids = list(dict.fromkeys([*now_top, *before_top]))

        def pt(uid: int, m: str, known: dict[int, int]) -> int:
            if uid in known:
                return known[uid]
            return s.scalar("SELECT points FROM user_month WHERE user_id=? AND month=?", (uid, m), 0) or 0

        status, names = {}, {}
        for k in range(0, len(ids), 500):
            part = ids[k:k + 500]
            marks = ",".join("?" * len(part))
            status.update({r[0]: r for r in s.all(f"SELECT user_id, top_room, state FROM gifter_status WHERE user_id IN ({marks})", part)})
            names.update({r[0]: r[1] for r in s.all(f"SELECT user_id, name FROM users WHERE user_id IN ({marks})", part)})
        rooms_of = {}
        for rid in {r["top_room"] for r in status.values() if r["top_room"]}:
            r = s.one("SELECT key, name FROM rooms WHERE room_id=?", (rid,))
            rooms_of[rid] = ((r["key"] if r else None), (r["name"] if r else None) or str(rid))
        items = []
        for uid in ids:
            st = status.get(uid)
            room = rooms_of.get(st["top_room"]) if st is not None and st["top_room"] else None
            items.append({"user_id": uid, "name": names.get(uid) or "",
                          "state": st["state"] if st is not None else None,
                          "group": room[1] if room else "最推しのルームが分からない", "room_key": room[0] if room else None,
                          **_item(pt(uid, month, now_top), pt(uid, prev, before_top))})
        items = [x for x in items if x["size"] > 0]
        groups = _grouped(items, "group")
        keys = {x["group"]: x["room_key"] for x in items}
        for g in groups:
            g["room_key"] = keys.get(g["name"])
        return {"month": month, "prev": prev, "partial": month == this_month, "groups": groups, "months": months}


# ------------------------------------------------------------------ 地力と残弾
def _power_src(s: Store) -> float | None:
    """ギフターのステータスを作り終えた時刻 (作り終えていなければ None)。"""
    from . import gifter_status

    try:
        return json.loads(s.meta(gifter_status.RUN_META) or "{}").get("done")
    except (TypeError, ValueError):
        return None


def power_due(s: Store) -> bool:
    """作り直す時か (ギフターのステータスが新しく作り終わった・作り方を変えた)。"""
    src = _power_src(s)
    return src is not None and s.meta("heat_power_src") != [POWER_VERSION, src]


class _PowerRun:
    """作り直しの途中 (ルームごとの合計と、出せる量の多い人)。1 回に CHUNK 人ずつ足していく。"""

    def __init__(self, src: float | None) -> None:
        self.src = src
        self.after = -1  # ここまでの user_id を足した
        self.totals: dict[int, list[int]] = {}  # room_id → [地力, 残弾, 人数, 主砲, 主力]
        self.tops: dict[int, list[int]] = {}  # room_id → 出せる量の多い人 (小さい順のヒープ。下の _pack)

    def add(self, s: Store, limit: int) -> bool:
        """次の limit 人を足す (user_id の順に、主キーで読む)。全員を足し終えたら True。"""
        from . import gifter_status

        n = 0
        for uid, data in s.all("SELECT user_id, data FROM gifter_status WHERE user_id > ? AND version=? ORDER BY user_id LIMIT ?",
                               (self.after, gifter_status.VERSION, limit)):
            n += 1
            self.after = uid
            d = json.loads(data)
            state = d.get("state")
            factor = gifter_status.STATE_FACTOR.get(state, 1.0)
            hp = d.get("hp") or 0
            ratio = min(1.0, max(0.0, (d.get("mp") or 0) / hp)) if hp else 0.0
            for r in d.get("rooms") or []:
                if r.get("kind") == "付き合い":  # 最推しのルームとくらべてごく少ない (付き合い投げ)
                    continue
                sortie = r.get("sortie")
                power = int((r.get("cap") or 0) * (SORTIE_UNKNOWN if sortie is None else sortie) * factor)
                if power <= 0:
                    continue
                edge = r.get("edge") or 0
                cls = "主砲" if edge and power >= edge else "主力" if edge and power >= edge / 3 else "支え"
                rid = int(r["room_id"])
                t = self.totals.setdefault(rid, [0, 0, 0, 0, 0])
                t[0] += power
                t[1] += int(power * ratio)
                t[2] += 1
                t[3] += cls == "主砲"
                t[4] += cls == "主力"
                h = self.tops.setdefault(rid, [])
                item = _pack(power, int(uid), ratio, FIGHTERS.index(cls), STATES.index(state) if state in STATES else len(STATES))
                if len(h) < PEOPLE:
                    heapq.heappush(h, item)
                elif item > h[0]:
                    heapq.heapreplace(h, item)
        return n < limit

    def save(self, s: Store, now: float) -> int:
        rows = []
        for rid, t in self.totals.items():
            people = [_unpack(x) for x in sorted(self.tops.get(rid, []), reverse=True)]
            rows.append((rid, now, t[0], t[1], t[2], t[3], t[4], json.dumps(people, separators=(",", ":"))))
        with s.tx() as c:
            c.execute("DELETE FROM room_power")
            c.executemany("INSERT INTO room_power(room_id, at, power, left_pt, people, guns, mains, data) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                          rows)
        s.set_meta("heat_power_src", [POWER_VERSION, self.src])
        log.info("ルームの地力と残弾を作りました (%d ルーム)", len(rows))
        return len(rows)


_runs: dict[str, _PowerRun] = {}  # 作り直しの途中 (state_dir ごと。起動し直したら、初めからやり直す)


def build_power(s: Store, now: float | None = None) -> int:
    """ギフターのステータスから、ルームごとの地力・残弾と、出せる量の多い人を一度に作り直す (戻り値はルームの数。テスト用)。

    1 人のこのルームへの地力 = このルームでの本気の上限 (直近 12 か月の、このルームの 1 イベントの量の上から 2 番目。gifter_status の cap)
    × このルームの出動率 (分からなければ 0.5) × 状態の係数 (離脱 0・枯渇 0.2。gifter_status と同じ)。付き合い投げの先は数えない。
    クラスは、この地力をルームの決め手の量 (edge) とくらべて分ける (以上なら主砲・3 分の 1 以上なら主力・それより下は支え)。

    はじめ (POWER_VERSION 1) は、火力 (どのルームでもよい上限) × 向ける割合 にして、gifter_status のクラス (残弾をかけた総合力で分ける)
    で付き合いを除いていた。サーバーで、大きなルームほど地力が少なく (イベントの多い 20 ルーム中 19 で、いちばん大きいイベントの
    0.6 倍以下)、よその火力が残る小さなルームで 66 倍になった (2026-10-21 に Cursor が測った)。残弾の少ない時期は総合力が小さく、
    決め手の量の大きいルームの人ほど「付き合い」に落ちていたため。地力は残弾をかけない量なので、クラスもここで分け直す。
    残弾をかけないので、月の途中でも大きさは動かない (色の残弾で見る)。
    ステータスは少しずつ読み、全部をメモリに持たない (ルームごとに出せる量の多い PEOPLE 人だけ、数を 1 つの整数につめて持つ)。
    """
    run = _PowerRun(_power_src(s))
    while not run.add(s, CHUNK):
        pass
    return run.save(s, time.time() if now is None else now)


def _pack(power: int, uid: int, ratio: float, cls: int, state: int) -> int:
    """1 人分を 1 つの整数につめる (大きいほど出せる量が多い。たくさんの人を持っても、メモリを使いすぎないように)。"""
    return (power << 48) | (uid << 16) | (round(ratio * 1000) << 6) | (cls << 3) | state


def _unpack(x: int) -> list[Any]:
    """[user_id, 出せる量, 残弾の割合, クラス, 状態]"""
    state = x & 7
    return [(x >> 16) & (2**32 - 1), x >> 48, ((x >> 6) & 1023) / 1000, FIGHTERS[(x >> 3) & 7],
            STATES[state] if state < len(STATES) else None]


def quiet() -> bool:
    """ディスクが空いているか (自己点検が後回しにしておらず、ディスク待ちが room_parts.IDLE_IOWAIT より少ない)。

    room_parts.idle と違い、管理画面が使われているかは見ない。管理画面は開いている間 15 秒ごとに読み直すので、
    それを見ると、開いたままの間はいつまでも作られなかった (2026-10-21 に Cursor が測った)。
    代わりに、使われている間は 1 回に足す人数を CHUNK_BUSY に減らす。"""
    from . import memory, room_parts, selfcheck

    if memory.holding() > 0:
        return False
    iowait = selfcheck.CHECKER.load.get("iowait") if selfcheck.CHECKER is not None else None
    return iowait is None or iowait < room_parts.IDLE_IOWAIT


def power_step(s: Store, now: float | None = None) -> bool:
    """作り直す時で、ディスクが空いていれば、CHUNK 人ずつ (管理画面が使われている間は CHUNK_BUSY 人ずつ) 足していき、
    全員を足し終えたら保存する (収集の 1 回の仕事を長く止めない。ほかの重い集計とは重ならないようにする)。何かしたら True。"""
    from . import memory, room_parts

    key = str(s.state_dir)
    if not power_due(s):
        _runs.pop(key, None)
        return False
    if not quiet():
        return False
    with memory.heavy(wait=False) as got:
        if not got:
            return False
        src = _power_src(s)
        run = _runs.get(key)
        if run is None or run.src != src:  # ステータスが作り直されたら、初めから
            run = _runs[key] = _PowerRun(src)
        if run.add(s, CHUNK if room_parts.idle(now) else CHUNK_BUSY):
            run.save(s, time.time() if now is None else now)
            del _runs[key]
            memory.release()
    return True


def room_power(state_dir: Path, room: str) -> dict[str, Any]:
    """ルームのページの、中のギフターのマップ (出せる量の多い人。大きさ = このルームに出せる量、色 = 残弾)。room は room_id か key。"""
    with Store(state_dir) as s:
        rid = int(room) if str(room).isdigit() else s.scalar("SELECT room_id FROM rooms WHERE key=?", (room,), None)
        r = s.one("SELECT * FROM room_power WHERE room_id=?", (rid,)) if rid is not None else None
        built = s.meta("heat_power_src")
        if r is None:
            return {"found": False, "built": built is not None}
        people = json.loads(r["data"])
        ids = [p[0] for p in people]
        names = {x[0]: x[1] for x in s.all(f"SELECT user_id, name FROM users WHERE user_id IN ({','.join('?' * len(ids))})", ids)} if ids else {}
        rank = s.scalar("SELECT COUNT(*) FROM room_power WHERE power > ?", (r["power"],)) + 1
        return {"found": True, "at": r["at"], "power": r["power"], "left_pt": r["left_pt"], "people": r["people"], "guns": r["guns"],
                "mains": r["mains"], "rank": rank, "rooms": s.scalar("SELECT COUNT(*) FROM room_power"),
                "gifters": [{"user_id": p[0], "name": names.get(p[0]) or "", "power": p[1], "left": p[2], "class": p[3], "state": p[4]}
                            for p in people]}
