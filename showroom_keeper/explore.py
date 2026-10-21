"""管理画面のルーム・イベントのページと、上の検索欄に出す情報 (research.db を読むだけ)。

SHOWROOM には問い合わせない。分析 (research.analyze) のように全体を数え直さず、
1 つのルーム・イベントの分だけを、索引のある列から読む。
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

from . import event_analysis, room_analysis, room_parts, search_index, series as series_mod
from .research import LEFT_NAMES, _user_names, PENDING_RANKING, RANKING_RETRIES
from .research_store import Store

SEARCH_LIMIT = 20  # 検索で種類ごとに返す数
ROOM_GIFTERS = 200  # ルームのページに出すギフターの数
ROOM_EVENTS = 300  # ルームのページに出すイベントの数
OVERLAP_GIFTERS = 300  # 重なるルームを調べる、このルームのギフターの数 (多い順)
OVERLAP_ROOMS = 20  # 重なるルームとして出す数
EVENT_ROOMS = 500  # イベントのページに出す参加ルームの数
EVENT_LIST = 200  # イベントの一覧に出す数 (終わったもの)
PROGRESS_ROOMS = 10  # 推移のグラフに出すルームの数 (今のポイントの多い順)
CONTRIBUTORS = 100  # イベントのページに出す貢献者の数


def _event_state(e: Any, now: float) -> str:
    if e["start"] and e["start"] > now:
        return "upcoming"
    if e["end"] and e["end"] <= now:
        return "ended"
    return "ongoing" if e["start"] else "unknown"


def _event_row(e: Any, now: float) -> dict[str, Any]:
    return {"event_id": e["event_id"], "key": e["key"], "name": e["name"] or f"イベント {e['event_id']}",
            "start": e["start"], "end": e["end"], "state": _event_state(e, now)}


# ------------------------------------------------------------------ 検索
_sender_lock = threading.Lock()


def update_gift_senders(state_dir: Path, s: Store, room_key: str, files: dict[str, Path] | None = None) -> None:
    """記録したギフトのファイルの、前に読んだところから先だけを、送り主の表 (gift_senders・gift_sender_names) に足す。

    少額でランキングに出ない人は、ランキングから作る users 表に入らないので、上の検索欄では記録からも探す。
    前は検索のたびにファイル全部を読み直していた (789 ルーム・66MB で 40 秒・メモリ 420MB。2026-10-19 に Cursor が測った)。
    ファイルが前より小さくなっていたら (作り直し・まとめ直し)、そのルームの分を最初から作り直す。
    """
    from . import listing

    files = files if files is not None else listing._record_files(state_dir)  # files: 何ルームも続けて足す時に、一度だけ探したもの
    update_gift_events(state_dir, s, room_key, files)  # ギフト 1 件ずつの表も、同じ時に足す
    path = files.get(room_key)
    if path is None:
        return
    with _sender_lock:
        try:
            size = path.stat().st_size
        except OSError:
            return
        row = s.one("SELECT pos, size FROM gift_sender_pos WHERE room_key=?", (room_key,))
        pos = row["pos"] if row is not None else 0
        if row is not None and size < row["size"]:  # ファイルが小さくなった: 最初から
            with s.tx() as c:
                c.execute("DELETE FROM gift_senders WHERE room_key=?", (room_key,))
                c.execute("DELETE FROM gift_room_totals WHERE room_key=?", (room_key,))
            pos = 0
        if size <= pos:
            return
        with path.open("rb") as f:
            f.seek(pos)
            chunk = f.read(size - pos)
        end = chunk.rfind(b"\n") + 1  # 書きかけの最後の行は、次に読む
        if end <= 0:
            return
        catalog = listing.gift_catalog(state_dir, [room_key])
        if not catalog:  # ギフトの一覧がまだ取れていない: 全部 0 pt で足すと戻せないので、次に回す
            return
        senders: dict[int, list[Any]] = {}  # user_id -> [回数, 最後の時刻, 最後の名前, pt, そのうち無料, 最初の時刻, 一覧に無いギフトの回数]
        names: dict[tuple[int, str], float] = {}
        for line in chunk[:end].splitlines():
            try:
                g = json.loads(line)
            except ValueError:
                continue
            if not isinstance(g, dict) or g.get("user_id") is None or "at" not in g:
                continue
            uid, name, at = int(g["user_id"]), g.get("name") or "", float(g["at"])
            x = senders.setdefault(uid, [0, 0.0, "", 0, 0, at, 0])
            x[0] += 1
            pt, free = listing._points(catalog, g)
            x[3] += pt
            x[4] += pt if free else 0
            x[5] = min(x[5], at)
            x[6] += g.get("gift_id") not in catalog
            if at >= x[1]:
                x[1] = at
                x[2] = name or x[2]
            if name:
                names[(uid, name)] = max(at, names.get((uid, name), 0.0))
        with s.tx() as c:
            c.executemany("""INSERT INTO gift_senders(room_key, user_id, times, last_at, name, points, free_points, first_at, unknown)
                             VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                             ON CONFLICT(room_key, user_id) DO UPDATE SET times=times+excluded.times,
                               name=CASE WHEN excluded.last_at >= last_at AND excluded.name != '' THEN excluded.name ELSE name END,
                               last_at=MAX(last_at, excluded.last_at), points=points+excluded.points,
                               free_points=free_points+excluded.free_points, first_at=MIN(COALESCE(first_at, excluded.first_at), excluded.first_at),
                               unknown=COALESCE(unknown, 0)+excluded.unknown""",
                          [(room_key, uid, *x) for uid, x in senders.items()])
            c.executemany("""INSERT INTO gift_sender_names(user_id, name, folded, at) VALUES (?, ?, ?, ?)
                             ON CONFLICT(user_id, name) DO UPDATE SET at=MAX(at, excluded.at)""",
                          [(uid, name, _fold(name), at) for (uid, name), at in names.items()])
            if senders:
                c.execute("""INSERT INTO gift_room_totals(room_key, records, last_at) VALUES (?, ?, ?)
                             ON CONFLICT(room_key) DO UPDATE SET records=records+excluded.records,
                               last_at=MAX(COALESCE(last_at, 0), excluded.last_at)""",
                          (room_key, sum(x[0] for x in senders.values()), max(x[1] for x in senders.values())))
            c.execute("""INSERT INTO gift_sender_pos(room_key, pos, size, at) VALUES (?, ?, ?, ?)
                         ON CONFLICT(room_key) DO UPDATE SET pos=excluded.pos, size=excluded.size, at=excluded.at""",
                      (room_key, pos + end, pos + end, time.time()))


def ensure_room_totals(s: Store) -> None:
    """ルームごとの件数の表 (gift_room_totals) を、前に足した送り主の表から 1 回だけ作る (この表を足す前の research.db のため)。"""
    if s.meta("gift_room_totals"):
        return
    with _sender_lock, s.tx() as c:  # 送り主の表に足すのと重ならないように
        c.execute("""INSERT OR REPLACE INTO gift_room_totals(room_key, records, last_at)
                     SELECT room_key, SUM(times), MAX(last_at) FROM gift_senders GROUP BY room_key""")
        c.execute("INSERT INTO meta(k, v) VALUES ('gift_room_totals', '1') ON CONFLICT(k) DO UPDATE SET v=excluded.v")


_event_lock = threading.Lock()


def update_gift_events(state_dir: Path, s: Store, room_key: str, files: dict[str, Path] | None = None) -> None:
    """記録したギフトのファイルの、前に読んだところから先だけを、ギフト 1 件ずつの表 (gift_events) に足す。
    ファイルが前より小さくなっていたら、そのルームの分を最初から作り直す。ポイントは読む時にギフトの一覧で数える (ここでは残さない)。"""
    from . import listing

    path = (files if files is not None else listing._record_files(state_dir)).get(room_key)
    if path is None:
        return
    with _event_lock:
        try:
            size = path.stat().st_size
        except OSError:
            return
        row = s.one("SELECT pos, size FROM gift_event_pos WHERE room_key=?", (room_key,))
        pos = row["pos"] if row is not None else 0
        if row is not None and size < row["size"]:  # ファイルが小さくなった: 最初から
            with s.tx() as c:
                c.execute("DELETE FROM gift_events WHERE room_key=?", (room_key,))
            pos = 0
        if size <= pos:
            return
        with path.open("rb") as f:
            f.seek(pos)
            chunk = f.read(size - pos)
        end = chunk.rfind(b"\n") + 1  # 書きかけの最後の行は、次に読む
        if end <= 0:
            return
        rows = []
        for line in chunk[:end].splitlines():
            try:
                g = json.loads(line)
                if isinstance(g, dict) and g.get("user_id") is not None and "at" in g:
                    rows.append((int(g["user_id"]), float(g["at"]), room_key, g.get("gift_id"), int(g.get("num") or 1)))
            except (ValueError, TypeError):
                continue
        with s.tx() as c:
            c.executemany("INSERT INTO gift_events(user_id, at, room_key, gift_id, num) VALUES (?, ?, ?, ?, ?)", rows)
            c.execute("""INSERT INTO gift_event_pos(room_key, pos, size, at) VALUES (?, ?, ?, ?)
                         ON CONFLICT(room_key) DO UPDATE SET pos=excluded.pos, size=excluded.size, at=excluded.at""",
                      (room_key, pos + end, pos + end, time.time()))


_visit_lock = threading.Lock()


def update_live_visits(state_dir: Path, s: Store, room_key: str) -> None:
    """来場の知らせのファイル (live_events/<ルーム>.jsonl) の、前に読んだところから先の visit だけを live_visits に足す。
    ファイルが前より小さくなっていたら、そのルームの分を最初から作り直す。"""
    from . import live_events

    path = live_events.event_dir(Path(state_dir)) / f"{room_key}.jsonl"
    with _visit_lock:
        try:
            size = path.stat().st_size
        except OSError:
            return
        row = s.one("SELECT pos, size FROM live_visit_pos WHERE room_key=?", (room_key,))
        pos = row["pos"] if row is not None else 0
        if row is not None and size < row["size"]:
            with s.tx() as c:
                c.execute("DELETE FROM live_visits WHERE room_key=?", (room_key,))
            pos = 0
        if size <= pos:
            return
        with path.open("rb") as f:
            f.seek(pos)
            chunk = f.read(size - pos)
        end = chunk.rfind(b"\n") + 1
        if end <= 0:
            return
        rows = []
        for line in chunk[:end].splitlines():
            if b'"visit"' not in line:  # 来場の知らせだけ (ほかの知らせは読まない)
                continue
            try:
                e = json.loads(line)
                if e.get("kind") == "visit" and e.get("user_id") is not None:
                    rows.append((int(e["user_id"]), int(e["at"]), room_key, int(e.get("n") or 0)))
            except (ValueError, TypeError):
                continue
        with s.tx() as c:
            c.executemany("INSERT OR IGNORE INTO live_visits(user_id, at, room_key, n) VALUES (?, ?, ?, ?)", rows)
            c.execute("""INSERT INTO live_visit_pos(room_key, pos, size, at) VALUES (?, ?, ?, ?)
                         ON CONFLICT(room_key) DO UPDATE SET pos=excluded.pos, size=excluded.size, at=excluded.at""",
                      (room_key, pos + end, pos + end, time.time()))


def update_all_live_visits(state_dir: Path, s: Store) -> None:
    """来場の知らせのある全ルームの、新しい分だけを足す (新しい分が無ければ、ファイルの大きさを見るだけ)。"""
    from . import live_events

    folder = live_events.event_dir(Path(state_dir))
    for path in folder.glob("*.jsonl") if folder.is_dir() else []:
        try:
            update_live_visits(state_dir, s, path.stem)
        except Exception:  # noqa: BLE001
            continue


def update_all_gift_senders(state_dir: Path, s: Store) -> None:
    """記録している全ルームの、新しい分だけを送り主の表に足す (新しい分が無ければ、ファイルの大きさを見るだけ)。"""
    from . import listing

    files = listing._record_files(state_dir)
    for key in files:
        try:
            update_gift_senders(state_dir, s, key, files)
        except Exception:  # noqa: BLE001 - 1 つのルームの失敗で、ほかを止めない
            continue


def _recorded_senders(s: Store, folded: str, num: int | None, skip: set[int], limit: int) -> list[dict[str, Any]]:
    """記録したギフトにしか出ていない人を、送り主の表から探す (名前のそろえた形に folded を含む人と、ユーザー ID が num の人)。"""
    ids: dict[int, str] = {}
    if num is not None:
        for r in s.all("SELECT name FROM gift_sender_names WHERE user_id=? ORDER BY at DESC LIMIT 1", (num,)):
            ids[num] = r["name"]
        if num not in ids and s.one("SELECT 1 FROM gift_senders WHERE user_id=? LIMIT 1", (num,)):
            ids[num] = ""
    if folded:
        for r in s.all("SELECT user_id, name FROM gift_sender_names WHERE instr(folded, ?) > 0 ORDER BY at DESC LIMIT ?",
                       (folded, limit * 10)):
            ids.setdefault(r["user_id"], r["name"])
    out = []
    for uid, hit in ids.items():
        if uid in skip:
            continue
        row = s.one("SELECT SUM(times) AS times FROM gift_senders WHERE user_id=?", (uid,))
        last = s.one("SELECT name FROM gift_senders WHERE user_id=? AND name != '' ORDER BY last_at DESC LIMIT 1", (uid,))
        out.append({"user_id": uid, "name": (last["name"] if last else None) or hit, "hit": hit or (last["name"] if last else ""),
                    "times": (row["times"] if row else 0) or 0})
    out.sort(key=lambda x: (x["user_id"] != num, -x["times"]))
    return out[:limit]


def _fold(text: Any) -> str:
    """探すためにそろえた形: 全角・半角をそろえ (NFKC)、小文字にし、空白を除き、カタカナをひらがなにする。"""
    import unicodedata

    t = unicodedata.normalize("NFKC", str(text or "")).lower()
    t = "".join(t.split())
    return "".join(chr(ord(c) - 0x60) if "ァ" <= c <= "ヶ" else c for c in t)


def _kana_variants(q: str) -> list[str]:
    """SQL の LIKE で探す形 (空白を除いた形と、ひらがな・カタカナの両方)。"""
    import unicodedata

    t = "".join(unicodedata.normalize("NFKC", q).split())
    hira = "".join(chr(ord(c) - 0x60) if "ァ" <= c <= "ヶ" else c for c in t)
    kata = "".join(chr(ord(c) + 0x60) if "ぁ" <= c <= "ゖ" else c for c in t)
    return list(dict.fromkeys(x.replace("%", "").replace("_", "") for x in (t, hira, kata) if x))


FOLDED_TTL = 600  # ルームの名前をそろえた形を使い回す時間 (秒)
_folded_rooms: dict[str, tuple[float, list[tuple[str, int]]]] = {}


def _rooms_by_folded(s: Store, folded: str, limit: int) -> list[dict[str, Any]]:
    """ルームの名前を、そろえた形 (ひらがな・カタカナがまざった名前も) でくらべて探す。名前の一覧は FOLDED_TTL 使い回す。"""
    key = str(s.state_dir)
    hit = _folded_rooms.get(key)
    if hit is None or time.time() - hit[0] > FOLDED_TTL:
        # メモリを使いすぎないように、そろえた名前と room_id だけを持つ (フォロワーの多い順)
        rows = [(_fold(r[1]), r[0]) for r in s.all("SELECT room_id, name FROM rooms WHERE name IS NOT NULL ORDER BY COALESCE(followers, 0) DESC")]
        hit = _folded_rooms[key] = (time.time(), rows)
    ids = [rid for name, rid in hit[1] if folded in name][:limit]
    return [dict(r) for rid in ids for r in [s.one("SELECT room_id, key, name, followers, live_at, collected_at FROM rooms WHERE room_id=?",
                                                   (rid,))] if r]


# 名前から空白 (半角・全角) を除いた形 (「渡辺 みく」を「渡辺みく」で見つけるため)
_NOSPACE = "REPLACE(REPLACE(LOWER({col}), ' ', ''), '　', '')"


def search(state_dir: Path, q: str, showroom: bool = False) -> dict[str, Any]:
    """上の検索欄: ギフター・ルーム・イベントを探す。

    名前は、空白・全角半角・大文字小文字・ひらがなカタカナのちがいを気にせずに探す。
    - ギフター: ランキングに出た人 (users) の名前・ユーザー ID、前の名前 (user_names)、記録したギフトの送り主の名前
    - ルーム: 見つけたルーム (rooms) の名前・URL キー・room_id と、視聴・記録の設定や記録したギフトにあるルーム。
      showroom なら、見つかったルームが少ない時に SHOWROOM のルーム検索でも探し、見つけたルームは集める対象に足す
    - イベント: 見つけたイベントの名前・URL キー・番号 (名前のまだ分からないイベントは番号で)
    """
    from .listing import room_names

    q = str(q or "").strip()[:100]
    if not q:
        return {"gifters": [], "rooms": [], "events": []}
    variants = _kana_variants(q)
    likes = [f"%{v.lower()}%" for v in variants]
    folded = _fold(q)
    digits = q.isdigit()
    num = int(q) if digits else -1
    now = time.time()

    def name_match(col: str) -> str:
        return "(" + " OR ".join(f"{_NOSPACE.format(col=col)} LIKE ?" for _ in likes) + ")"

    with Store(state_dir) as s:
        # 名前の索引 (search_index)。入れ終わるまで・作れない SQLite では、前のやり方 (名前を全部 LIKE でくらべる)
        # 索引を足すのは裏の仕事に頼み、ここでは足し終えた分で引く (書き込みの順番を待たない)
        ready = search_index.ready(s)
        from . import catchup

        catchup.soon(state_dir)
        two = ready["two"]
        indexed = ready["three"]  # 2 文字ずつの索引がまだ無い間の 2 文字は、3 文字ずつの索引の中を全部くらべる

        def ids(table: str, cols: tuple[str, ...]) -> tuple[str, list[Any]]:
            src, cond, args = search_index.where(table, cols, folded, two)
            return f"SELECT rowid FROM {src} WHERE {cond}", args

        points = "(SELECT SUM(MAX(ur.cumulative, ur.events)) FROM user_room ur WHERE ur.user_id=u.user_id)"
        if indexed:
            sub, args = ids("search_users", ("folded",))
            users_sql, users_args = f"u.user_id IN ({sub})", args
            src, cond, args = search_index.where("search_former", ("folded",), folded, two)
            former_sql, former_args = f"(SELECT user_id, name FROM {src} WHERE {cond}) n", args
        else:
            users_sql, users_args = name_match("u.name"), likes
            former_sql, former_args = f"user_names n WHERE {name_match('n.name')}", likes
        gifters = [{**dict(r), "match": None} for r in s.all(
            f"""SELECT u.user_id, u.name, {points} AS points FROM users u WHERE u.user_id = ? OR {users_sql}
                ORDER BY u.user_id = ? DESC, points DESC LIMIT ?""", (num, *users_args, num, SEARCH_LIMIT))]
        seen = {g["user_id"] for g in gifters}
        # 前の名前で見つかった人 (今の名前と、どの名前で見つかったかを出す)
        former_from, _, former_where = former_sql.partition(" WHERE ") if not indexed else (former_sql, "", "")
        for r in s.all(f"""SELECT n.user_id, n.name AS old, u.name, {points} AS points FROM {former_from}
                           LEFT JOIN users u USING(user_id) {"WHERE " + former_where if former_where else ""} ORDER BY points DESC LIMIT ?""",
                       (*former_args, SEARCH_LIMIT)):
            if r["user_id"] not in seen:
                seen.add(r["user_id"])
                gifters.append({"user_id": r["user_id"], "name": r["name"] or r["old"], "points": r["points"],
                                "match": {"kind": "former", "name": r["old"]}})
        # 記録したギフトにしか出ていない人 (少額でランキングに出ない人など)
        # (送り主の表は収集の合間に 10 分ごとに足すので、新しい送り主は少し遅れて出る)
        for u in _recorded_senders(s, folded, num if digits else None, seen, SEARCH_LIMIT):
            gifters.append({"user_id": u["user_id"], "name": u["name"], "points": None, "times": u["times"],
                            "match": {"kind": "recorded", "name": u["hit"]}})

        if indexed:
            sub, args = ids("search_rooms", ("name", "key"))
            rooms_sql, rooms_args = f"room_id IN ({sub})", args
        else:
            rooms_sql, rooms_args = f"{name_match('name')} OR {name_match('key')}", [*likes, *likes]
        rooms = [{**dict(r), "source": "research"} for r in s.all(
            f"""SELECT room_id, key, name, followers, live_at, collected_at FROM rooms
                WHERE room_id = ? OR key = ? OR {rooms_sql}
                ORDER BY room_id = ? DESC, key = ? DESC, COALESCE(followers, 0) DESC LIMIT ?""",
            (num, q, *rooms_args, num, q, SEARCH_LIMIT))]
        known = {r["key"] for r in rooms if r["key"]} | {str(r["room_id"]) for r in rooms}
        if not indexed and len(rooms) < SEARCH_LIMIT and not digits:  # ひらがな・カタカナがまざった名前 (SQL ではそろえられない。索引ではそろえてある)
            for r in _rooms_by_folded(s, folded, SEARCH_LIMIT):
                if r["key"] not in known and str(r["room_id"]) not in known:
                    known.add(r["key"])
                    rooms.append({**r, "source": "research"})
        # 視聴・記録の設定や記録したギフトにあるルーム (全ルームの収集でまだ見つかっていないものも)
        for key, info in room_names(state_dir).items():
            name = (info or {}).get("name") or key
            rid = (info or {}).get("room_id")
            if key in known or (rid is not None and str(rid) in known):
                continue
            if folded in _fold(key) or folded in _fold(name) or (digits and rid is not None and int(rid) == num):
                known.add(key)
                rooms.append({"room_id": rid, "key": key, "name": name, "followers": None, "live_at": None, "source": "known"})
                if len(rooms) >= SEARCH_LIMIT * 2:
                    break
        # SHOWROOM のルーム検索 (集めた情報で少ししか見つからない時。見つけたルームは集める対象に足す)
        if showroom and len(rooms) < SEARCH_LIMIT and len(q) >= 2 and not digits:
            from .listing import search_showroom_rooms

            try:
                hits = search_showroom_rooms(q)
            except Exception:  # noqa: BLE001 - SHOWROOM に聞けなくても、集めた情報の分は返す
                hits = []
            new = [h for h in hits if h.get("key") and h["key"] not in known and h.get("room_id")]
            if new:
                s.upsert_rooms([{"room_id": h["room_id"], "key": h["key"], "name": h["name"], "followers": h.get("followers")}
                                for h in new], "search", now)
            for h in new[:SEARCH_LIMIT * 2 - len(rooms)]:
                rooms.append({"room_id": h["room_id"], "key": h["key"], "name": h["name"], "followers": h.get("followers"),
                              "live_at": now if h.get("is_live") else None, "source": "showroom", "is_live": h.get("is_live")})
        # 名前がそのまま合うものを先に (そのあとはフォロワーの多い順のまま)
        rooms.sort(key=lambda r: _fold(r["name"]) != folded)

        if indexed:
            sub, args = ids("search_events", ("name", "key"))
            events_sql, events_args = f"event_id IN ({sub})", args
        else:
            events_sql, events_args = f"{name_match('name')} OR {name_match('key')}", [*likes, *likes]
        events = [{**_event_row(r, now), "named": bool(r["name"])} for r in s.all(
            f"""SELECT * FROM events WHERE event_id = ? OR key = ? OR {events_sql}
                ORDER BY event_id = ? DESC, COALESCE(end, 0) DESC LIMIT ?""",
            (num, q, *events_args, num, SEARCH_LIMIT))]
    return {"gifters": gifters, "rooms": rooms, "events": events}


# ------------------------------------------------------------------ ルーム
def _find_room(s: Store, key: Any) -> Any:
    key = str(key or "").strip()
    if not key:
        raise ValueError("ルームを指定してください")
    row = s.room(key=key)
    if row is None and key.isdigit():
        row = s.room(room_id=int(key))
    return row


def _room_congestion(s: Store, rid: int, now: float) -> dict[str, Any] | None:
    """時間帯の混み具合 (全ルームと、このルームと同じリーグ)。"""
    c = room_analysis.congestion(s, now)
    if c is None:
        return None
    info = s.one("SELECT league_id FROM room_info WHERE room_id=?", (rid,))
    lg = info["league_id"] if info is not None else None
    return {"days": c["days"], "all": c["all"], "league": c["leagues"].get(str(lg)) if lg is not None else None,
            "league_name": event_analysis.LEAGUES.get(lg) if lg is not None else None}


METRIC_DAYS = 400  # 日ごとの数字を出す日数の上限
_gift_daily_lock = threading.Lock()


_EPOCH_DAYS: dict[int, str] = {}  # 1970-01-01 からの日数 → "YYYY-MM-DD" (作った分を使い回す)


def _jst_day(t: float) -> str:
    """日本時間の日 (YYYY-MM-DD)。日本時間は +9 時間で決まっているので、1 件ずつ datetime を作らずに出す (ギフトは数が多いため)。"""
    n = (int(t) + 32400) // 86400
    text = _EPOCH_DAYS.get(n)
    if text is None:
        from datetime import date, timedelta

        text = _EPOCH_DAYS[n] = (date(1970, 1, 1) + timedelta(days=n)).isoformat()
    return text


def _jst_noon(t: float) -> float:
    """その日 (日本時間) の正午。今が正午より前なら、前の日の正午 (先の時刻にならないように)。"""
    n = (int(t) + 32400) // 86400
    noon = n * 86400 - 32400 + 43200
    return noon if noon <= t else noon - 86400


def update_gift_daily(state_dir: Path, s: Store, room_key: str, files: dict[str, Path] | None = None) -> None:
    """記録したギフトのファイルの、前に足したところから先だけを、ルーム × 日の合計 (room_gift_daily) に足す。

    ファイルが前より小さくなっていたら (作り直し・まとめ直し)、そのルームの分を最初から作り直す。
    ポイントは足した時のギフトの一覧で数える (一覧に無いギフトは 0 pt)。
    """
    from . import listing

    path = (files if files is not None else listing._record_files(state_dir)).get(room_key)  # files: 何ルームも続けて足す時に、一度だけ探したもの
    if path is None:
        return
    with _gift_daily_lock:
        try:
            size = path.stat().st_size
        except OSError:
            return
        row = s.one("SELECT pos, size FROM room_gift_pos WHERE room_key=?", (room_key,))
        pos = row["pos"] if row is not None else 0
        if row is not None and size < row["size"]:  # ファイルが小さくなった: 最初から
            with s.tx() as c:
                c.execute("DELETE FROM room_gift_daily WHERE room_key=?", (room_key,))
                c.execute("DELETE FROM room_gift_day_users WHERE room_key=?", (room_key,))
            pos = 0
        if size <= pos:
            return
        with path.open("rb") as f:
            f.seek(pos)
            chunk = f.read(size - pos)
        end = chunk.rfind(b"\n") + 1  # 書きかけの最後の行は、次に読む
        if end <= 0:
            return
        catalog = listing.gift_catalog(state_dir, [room_key])
        sums: dict[str, list[int]] = {}
        users: set[tuple[str, int]] = set()
        for line in chunk[:end].splitlines():
            try:
                g = json.loads(line)
            except ValueError:
                continue
            if not isinstance(g, dict) or "at" not in g:
                continue
            d = _jst_day(g["at"])
            pt, free = listing._points(catalog, g)
            acc = sums.setdefault(d, [0, 0, 0])
            acc[1 if free else 0] += pt
            acc[2] += int(g.get("num") or 1)
            if not free and g.get("user_id") is not None:
                users.add((d, int(g["user_id"])))
        with s.tx() as c:
            c.executemany("""INSERT INTO room_gift_daily(room_key, day, paid_points, free_points, gifts) VALUES (?, ?, ?, ?, ?)
                             ON CONFLICT(room_key, day) DO UPDATE SET paid_points=paid_points+excluded.paid_points,
                               free_points=free_points+excluded.free_points, gifts=gifts+excluded.gifts""",
                          [(room_key, d, v[0], v[1], v[2]) for d, v in sums.items()])
            c.executemany("INSERT OR IGNORE INTO room_gift_day_users(room_key, day, user_id) VALUES (?, ?, ?)",
                          [(room_key, d, u) for d, u in users])
            c.execute("""INSERT INTO room_gift_pos(room_key, pos, size, at) VALUES (?, ?, ?, ?)
                         ON CONFLICT(room_key) DO UPDATE SET pos=excluded.pos, size=excluded.size, at=excluded.at""",
                      (room_key, pos + end, pos + end, time.time()))


def room_daily_metrics(state_dir: Path, key: Any, days: int = METRIC_DAYS) -> dict[str, Any]:
    """ルームの日ごとの数字 (日本時間の日): 配信の時間・回数・盛り上がり度・フォロワー・ギフト・コメント・来場。

    ギフト・コメント・来場は、記録しているルームだけ (記録が無い日は 0 ではなく「無し」)。
    """
    from . import comment_counts, listing, live_events

    now = time.time()
    since = now - days * 86400

    day_of = _jst_day

    out: dict[str, dict[str, float]] = {}

    def put(d: str, k: str, v: float, add: bool = True) -> None:
        row = out.setdefault(d, {})
        row[k] = row.get(k, 0) + v if add else v

    with Store(state_dir) as s:
        row = _find_room(s, key)
        if row is None:
            return {"found": False}
        rid, rkey = int(row["room_id"]), row["key"]
        for r in s.all("SELECT day, followers, view_max FROM room_daily WHERE room_id=? AND day >= ? ORDER BY day", (rid, day_of(since))):
            if r["followers"] is not None:
                put(r["day"], "followers", r["followers"], add=False)
            if r["view_max"]:
                put(r["day"], "view_max", r["view_max"], add=False)
        for r in s.all("SELECT started_at, last_seen FROM room_lives WHERE room_id=? AND started_at >= ?", (rid, since)):
            d = day_of(r["started_at"])
            put(d, "lives", 1)
            put(d, "hours", max(0.0, r["last_seen"] - r["started_at"] + room_analysis.LIVE_SEEN_GAP / 2) / 3600)
    recorded = False
    if rkey:
        if rkey in listing._record_files(state_dir):
            recorded = True
            from . import catchup

            catchup.soon(state_dir)  # 新しいギフトの分は裏で足す (ここでは前に足した分で出し、書き込みの順番を待たない)
            with Store(state_dir) as s:
                start = day_of(since)
                for r in s.all("SELECT day, paid_points, free_points, gifts FROM room_gift_daily WHERE room_key=? AND day >= ?", (rkey, start)):
                    put(r["day"], "paid_points", r["paid_points"])
                    put(r["day"], "free_points", r["free_points"])
                    put(r["day"], "gifts", r["gifts"])
                for r in s.all("""SELECT day, COUNT(*) AS n FROM room_gift_day_users WHERE room_key=? AND day >= ? GROUP BY day""",
                               (rkey, start)):
                    put(r["day"], "paid_gifters", r["n"], add=False)
        for live in comment_counts.load(state_dir, rkey):
            if live.get("first") and live["first"] >= since:
                d = day_of(live["first"])
                recorded = True
                put(d, "comments", live.get("comments") or 0)
                put(d, "commenters", live.get("users") or 0)
        for e in live_events.load(state_dir, rkey, since):
            if e.get("kind") == "visit":
                recorded = True
                d = day_of(e["at"])
                put(d, "visits", 1)
                if e.get("n") == 1:
                    put(d, "first_visits", 1)
            elif e.get("kind") == "fan_level":
                put(day_of(e["at"]), "level_ups", 1)
    # フォロワーの増減 (前に記録した日から)
    prev = None
    for d in sorted(out):
        f = out[d].get("followers")
        if f is not None:
            if prev is not None:
                out[d]["follower_gain"] = f - prev
            prev = f
    rec_keys = ("paid_points", "free_points", "gifts", "comments", "visits")
    rec_days = [d for d in sorted(out) if any(k in out[d] for k in rec_keys)]
    return {"found": True, "key": rkey, "recorded": recorded, "rec_from": rec_days[0] if rec_days else None,
            "days": [{"day": d, **out[d]} for d in sorted(out)]}


def room_detail(state_dir: Path, key: Any) -> dict[str, Any]:
    """1 つのルームの、集めた情報 (累計ランキング・ギフター・イベントの成績・重なるルーム)。同じページを同時に開いた時は、1 回だけ数える (once)。"""
    from . import once

    with Store(state_dir) as s:  # URL の名前と番号で同じルームを開いた時も、1 回にまとめる (2026-10-20 に Codex が見つけた)
        row = _find_room(s, key)
    return once.shared(("room", str(state_dir), row["room_id"] if row is not None else str(key)), lambda: _room_detail(state_dir, key))


def _room_detail(state_dir: Path, key: Any) -> dict[str, Any]:
    now = time.time()
    with Store(state_dir) as s:
        row = _find_room(s, key)
        if row is None:
            return {"key": str(key), "found": False}
        rid = int(row["room_id"])
        cumulative = [dict(r) for r in s.all(
            """SELECT c.rank, c.user_id, COALESCE(u.name, '') AS name, c.point, c.visits FROM cumulative c
               LEFT JOIN users u USING(user_id) WHERE c.room_id=? ORDER BY COALESCE(c.rank, 999), c.point DESC""", (rid,))]
        gifter_total = s.scalar("SELECT COUNT(*) FROM user_room WHERE room_id=? AND MAX(cumulative, events) > 0", (rid,))
        # リス活・重なるルーム・ギフターごとのルーム数は、作っておいた分を読む (無ければここで作る。room_parts)
        parts = room_parts.for_page(s, rid)
        gifters = [dict(r) for r in s.all(
            """SELECT ur.user_id, COALESCE(u.name, '') AS name, MAX(ur.cumulative, ur.events) AS points, ur.cumulative,
                      ur.cumulative_rank, ur.events, ur.visits
               FROM user_room ur LEFT JOIN users u USING(user_id)
               WHERE ur.room_id=? AND MAX(ur.cumulative, ur.events) > 0 ORDER BY points DESC, ur.user_id LIMIT ?""",
            (rid, ROOM_GIFTERS))]
        missing = [g["user_id"] for g in gifters if g["user_id"] not in parts["rooms"]]  # 作ったあとに上位に入った人だけ数える
        counts = dict(parts["rooms"])
        for i in range(0, len(missing), 500):
            chunk = missing[i:i + 500]
            counts.update({r[0]: r[1] for r in s.all(
                f"""SELECT user_id, COUNT(*) FROM user_room WHERE user_id IN ({",".join("?" * len(chunk))})
                    AND MAX(cumulative, events) > 0 GROUP BY user_id""", chunk)})
        for g in gifters:
            g["rooms"] = counts.get(g["user_id"], 0)
        event_total = s.scalar("SELECT COUNT(*) FROM room_events WHERE room_id=?", (rid,))
        events = []
        for r in s.all("""SELECT e.*, re.point, re.rank, re.ranking_at, re.final, re.c_count, re.c_sum,
                                 (SELECT COUNT(*) FROM room_events o WHERE o.event_id=e.event_id) AS rooms,
                                 (SELECT COUNT(*) FROM contributions c WHERE c.event_id=e.event_id AND c.room_id=re.room_id) AS contributors
                          FROM room_events re JOIN events e USING(event_id) WHERE re.room_id=?
                          ORDER BY COALESCE(e.end, e.start, 0) DESC, e.event_id DESC LIMIT ?""", (rid, ROOM_EVENTS)):
            events.append({**_event_row(r, now), "point": r["point"], "rank": r["rank"], "rooms": r["rooms"],
                           "contributors": r["contributors"], "ranking": bool(r["final"]),
                           # 100 位より下の人たちの分 (100 人に届いたイベントだけ。ポイント - 上位 100 人の合計)
                           "below": max(0, (r["point"] or 0) - r["c_sum"]) if r["final"] and (r["c_count"] or 0) >= 100
                           and r["c_sum"] is not None and r["end"] and (r["rooms_at"] or 0) >= r["end"] else None})
        overlap = _overlap(s, parts["overlap"])
        info = s.one("SELECT * FROM room_info WHERE room_id=?", (rid,))
        profile = None
        if info is not None:
            profile = {k: info[k] for k in ("at", "show_rank", "prev_show_rank", "grade", "continuous_days", "prev_score", "style",
                                            "group_type", "official")}
            profile.update({k: event_analysis.LEAGUES.get(info[k]) if info[k] is not None else None
                            for k in ("league_id", "next_league_id", "prev_league_id")})
            profile["ranks"] = [dict(r) for r in s.all("SELECT ym, show_rank, grade FROM room_rank_month WHERE room_id=? ORDER BY ym DESC LIMIT 12",
                                                       (rid,))]
        return {"key": row["key"], "room_id": rid, "found": True, "profile": profile, "name": row["name"] or row["key"] or str(rid),
                "followers": row["followers"], "live_at": row["live_at"], "collected_at": row["collected_at"],
                "found_at": row["found_at"], "cumulative": cumulative, "gifters": gifters, "gifter_total": gifter_total,
                "events": events, "event_total": event_total, "overlap": overlap,
                "parts_at": parts["at"],
                "event_stats": _room_event_stats(s, rid), "analysis": room_analysis.room_analysis(s, rid, now, rounds=parts["rounds"]),
                "balance": room_analysis.balance(state_dir, row["key"], now) if row["key"] else None,
                "lure": room_analysis.lure_throws(state_dir, row["key"], now, store=s, room_id=rid) if row["key"] else None,
                "stage": room_analysis.stage_regulars(state_dir, row["key"], now, store=s) if row["key"] else None,
                "live_habits": room_analysis.live_habits(s, rid, now),
                "followers_daily": [[r[0], r[1]] for r in s.all(
                    "SELECT day, followers FROM room_daily WHERE room_id=? AND followers IS NOT NULL ORDER BY day", (rid,))],
                "congestion": _room_congestion(s, rid, now), "effort": room_analysis.event_effort(s, rid, now)}


def _room_event_stats(s: Store, rid: int) -> dict[str, Any]:
    r = s.one("""SELECT COUNT(*) AS n, SUM(rank = 1) AS wins, SUM(rank <= 3) AS top3, MIN(rank) AS best,
                        SUM(point) AS points FROM room_events WHERE room_id=? AND rank > 0""", (rid,))
    return {k: (r[k] or 0) if k != "best" else r[k] for k in ("n", "wins", "top3", "best", "points")}


def overlap_rows(s: Store, rid: int) -> list[list[Any]]:
    """このルームのギフター (多い順に OVERLAP_GIFTERS 人) が、ほかに送っているルーム (共通のギフターの多い順)。[[room_id, 共通の人数, ポイント]]。
    重いので、ルームのページは作っておいた分 (room_parts) を読む。"""
    rows = s.all(f"""SELECT o.room_id, COUNT(*) AS shared, SUM(MAX(o.cumulative, o.events)) AS points
                     FROM (SELECT user_id FROM user_room WHERE room_id=? AND MAX(cumulative, events) > 0
                           ORDER BY MAX(cumulative, events) DESC LIMIT {OVERLAP_GIFTERS}) g
                     JOIN user_room o USING(user_id)
                     WHERE o.room_id != ? AND MAX(o.cumulative, o.events) > 0
                     GROUP BY o.room_id ORDER BY shared DESC, points DESC LIMIT ?""", (rid, rid, OVERLAP_ROOMS))
    return [[r["room_id"], r["shared"], r["points"]] for r in rows]


def _overlap(s: Store, rows: list[list[Any]]) -> list[dict[str, Any]]:
    """重なるルームに、今のルームの名前を付ける。"""
    out = []
    for room_id, shared, points in rows:
        info = s.room(room_id=room_id)
        out.append({"room_id": room_id, "key": info["key"] if info is not None else None,
                    "name": (info["name"] if info is not None else None) or str(room_id),
                    "shared": shared, "points": points})
    return out


def room_profiles(state_dir: Path, rtype: str | None = None, style: str | None = None, order: str = "momentum",
                  depth: str | None = None) -> dict[str, Any]:
    """ルームの一覧の、タイプ・勢い・戦術・層の厚さ (1 日 1 回作り直した分)。"""
    with Store(state_dir) as s:
        return room_analysis.profile_list(s, rtype or None, style or None, order or "momentum", depth=depth or None)


# ------------------------------------------------------------------ イベント
def event_list(state_dir: Path, state: str = "ongoing", limit: int = EVENT_LIST) -> dict[str, Any]:
    """イベントの一覧 (ongoing: 開催中・終わりが近い順 / upcoming: これから / ended: 終わった・新しい順)。"""
    now = time.time()
    where, order = {
        "ongoing": ("start <= ? AND end > ?", "end"),
        "upcoming": ("start > ? AND ? > 0", "start"),
        "ended": ("end <= ? AND ? > 0", "end DESC"),
    }.get(state, (None, None))
    if where is None:
        raise ValueError("state は ongoing・upcoming・ended のどれかです")
    with Store(state_dir) as s:
        total = s.scalar(f"SELECT COUNT(*) FROM events WHERE {where}", (now, now))
        rows = s.all(f"""SELECT e.*, (SELECT COUNT(*) FROM room_events re WHERE re.event_id=e.event_id) AS rooms,
                                (SELECT COUNT(*) FROM room_events re WHERE re.event_id=e.event_id AND re.final=1) AS rankings,
                                (SELECT COUNT(DISTINCT at) FROM event_progress p WHERE p.event_id=e.event_id) AS snapshots,
                                (SELECT r.name FROM room_events re JOIN rooms r USING(room_id)
                                  WHERE re.event_id=e.event_id AND re.rank=1 LIMIT 1) AS leader
                         FROM events e WHERE {where} ORDER BY {order} LIMIT ?""", (now, now, int(limit)))
        return {"state": state, "total": total, "now": now,
                "api_lag": event_analysis.api_lag(state_dir, now) if state == "ongoing" else None,
                "events": [{**_event_row(r, now), "rooms": r["rooms"], "rankings": r["rankings"], "snapshots": r["snapshots"],
                            "leader": r["leader"], "type": r["type"]} for r in rows]}


def _room_ids(s: Store, keys: list[str] | None) -> list[int]:
    out = []
    for k in keys or []:
        r = s.one("SELECT room_id FROM rooms WHERE key=?", (k,))
        if r is not None:
            out.append(int(r[0]))
    return out


def _room_labels(s: Store, rows: list[dict[str, Any]]) -> None:
    for r in rows:
        x = s.one("SELECT key, name FROM rooms WHERE room_id=?", (r["room_id"],))
        r["key"] = (x["key"] if x is not None else None) or str(r["room_id"])
        r["name"] = (x["name"] if x is not None else None) or r["key"]


def event_choice(state_dir: Path, support: list[str]) -> dict[str, Any]:
    """応援ルームに合うイベント: 開催中・これからのイベントで、応援ルームが出たら何位くらいか。"""
    with Store(state_dir) as s:
        ids = _room_ids(s, support)
        out = dict(event_analysis.event_choice(s, ids))
        out["events"] = [dict(e, focus=[dict(f) for f in e["focus"]]) for e in out["events"]]
        for e in out["events"]:
            _room_labels(s, e["focus"])
        out["support"] = len(ids)
        return out


def event_detail(state_dir: Path, event_id: Any, support: list[str] | None = None) -> dict[str, Any]:
    """1 つのイベントの参加ルームの順位・ポイントの推移・貢献者。同じページを同時に開いた時は、1 回だけ数える (once)。"""
    from . import once

    return once.shared(("event", str(state_dir), str(event_id), tuple(support or ())),
                       lambda: _event_detail(state_dir, event_id, support))


def _event_detail(state_dir: Path, event_id: Any, support: list[str] | None = None) -> dict[str, Any]:
    ref = str(event_id or "").strip()
    if not ref:
        raise ValueError("イベント番号か URL キーを指定してください")
    now = time.time()
    with Store(state_dir) as s:
        e = s.one("SELECT * FROM events WHERE event_id=?", (int(ref),)) if ref.isdigit() else \
            s.one("SELECT * FROM events WHERE key=? ORDER BY event_id DESC LIMIT 1", (ref,))
        if e is None:
            return {"event_id": ref, "found": False}
        eid = int(e["event_id"])
        rooms = [dict(r) for r in s.all(
            """SELECT re.room_id, r.key, COALESCE(r.name, r.key, re.room_id) AS name, re.point, re.rank, re.final,
                      (SELECT COUNT(*) FROM contributions c WHERE c.event_id=re.event_id AND c.room_id=re.room_id) AS contributors
               FROM room_events re LEFT JOIN rooms r USING(room_id) WHERE re.event_id=?
               ORDER BY re.rank IS NULL, re.rank, re.point DESC LIMIT ?""", (eid, EVENT_ROOMS))]
        # 開催中は順位が入っていないことがあるので、ポイントの多い順の位置を使う (place)
        points = sorted((r["point"] or 0 for r in rooms), reverse=True)
        styles = {r["room_id"]: r for r in s.all("SELECT room_id, style, early, final, last_day_share FROM room_event_style WHERE event_id=?",
                                                  (eid,))}
        for r in rooms:
            r["place"] = r["rank"] or (points.index(r["point"] or 0) + 1 if r["point"] else None)
            st = styles.get(r["room_id"])
            r["style"] = st["style"] if st is not None else None
        room_total = s.scalar("SELECT COUNT(*) FROM room_events WHERE event_id=?", (eid,))
        pending = s.scalar(f"""SELECT COUNT(*) FROM room_events re JOIN events e USING(event_id)
                               WHERE re.event_id=? AND {PENDING_RANKING}""", (eid, now - 3600, RANKING_RETRIES))
        # 退会したユーザーも出す (イベントの実際の顔ぶれなので)。前の名前が分かれば、それも出す
        contributors = [dict(r) for r in s.all(
            """SELECT c.user_id, COALESCE(u.name, '') AS name, SUM(c.point) AS point, COUNT(*) AS rooms, MIN(c.rank) AS best_rank,
                      MAX(c.guessed IS NOT NULL) AS guessed,
                      (SELECT COALESCE(r.name, r.key) FROM contributions c2 LEFT JOIN rooms r USING(room_id)
                        WHERE c2.event_id=c.event_id AND c2.user_id=c.user_id ORDER BY c2.point DESC LIMIT 1) AS top_room
               FROM contributions c LEFT JOIN users u USING(user_id) WHERE c.event_id=?
               GROUP BY c.user_id ORDER BY point DESC LIMIT ?""", (eid, CONTRIBUTORS))]
        shown = _user_names(s, None, [c["user_id"] for c in contributors if c["name"] in LEFT_NAMES])
        for c in contributors:
            c["left"] = c["name"] in LEFT_NAMES
            c["name"] = shown.get(c["user_id"], c["name"])
        contributor_total = s.scalar("SELECT COUNT(DISTINCT user_id) FROM contributions WHERE event_id=?", (eid,))
        info = s.one("SELECT * FROM event_info WHERE event_id=?", (eid,))
        data = json.loads(info["data"]) if info is not None and info["data"] else {}
        pg = s.one("SELECT at, status, text FROM event_pages WHERE event_id=?", (eid,))
        series = [dict(r) for r in s.all("""SELECT e.event_id, e.name, e.end FROM event_info i JOIN events e USING(event_id)
                                            WHERE i.parent_id=? AND i.parent_id > 0 AND i.event_id != ? ORDER BY e.end DESC LIMIT 20""",
                                         (info["parent_id"], eid))] if info is not None and info["parent_id"] else []
        details = {"description": data.get("event_description"), "reward_rank": data.get("reward_rank"),
                   "block": data.get("event_block_label"), "leagues": data.get("league_ids"), "parent_id": data.get("parent_event_id") or None,
                   "required_level": data.get("required_level"), "required_level_max": data.get("required_level_max"),
                   "box": data.get("is_box_event"), "official": data.get("is_official"), "type_name": data.get("type_name"),
                   "series": series, "page": dict(pg) if pg is not None else None}
        return {**_event_row(e, now), "found": True, "type": e["type"], "show_ranking": bool(e["show_ranking"]),
                "rooms": rooms, "room_total": room_total, "rankings": sum(1 for r in rooms if r["final"]),
                "rankings_pending": pending, "contributors": contributors, "contributor_total": contributor_total,
                "progress": _progress(s, eid, rooms), "styled": bool(e["style_at"]) and bool(styles), "details": details,
                "needed": event_analysis.needed_points(s, eid, now), "strength": _strength(s, eid, now, support),
                "taikai": _taikai(s, eid)}


def _taikai(s: Store, eid: int) -> dict[str, Any]:
    """イベントの入っている大会 (段階ごとのイベントのまとまり)。入っていなくても、手で直したかどうかを返す。"""
    of = series_mod.series_of(s, eid)
    manual = s.one("SELECT series_id FROM event_series_manual WHERE event_id=?", (eid,))
    out: dict[str, Any] = {"series": of, "manual": manual is not None, "checked": s.one(
        "SELECT 1 FROM event_series WHERE event_id=?", (eid,)) is not None}
    if of is not None:
        out["events"] = [dict(r) for r in s.all("""SELECT e.event_id, e.name, e.start, e.end, es.stage_no, es.stage, es.how, es.route
                                                   FROM event_series es JOIN events e USING(event_id)
                                                   WHERE es.series_id=? ORDER BY es.stage_no, e.start""", (of["series_id"],))]
    return out


def series_list(state_dir: Path) -> dict[str, Any]:
    """大会の一覧 (終わりの新しい順)。"""
    with Store(state_dir) as s:
        return {"series": series_mod.series_list(s), "now": time.time()}


def series_detail(state_dir: Path, series_id: Any) -> dict[str, Any]:
    """大会のページ (段階ごとの勝ち上がりの線・ポイントの増え方・大口のギフターが残るか・合計)。"""
    ref = str(series_id or "").strip()
    if not ref.isdigit():
        raise ValueError("大会の番号を指定してください")
    with Store(state_dir) as s:
        d = series_mod.series_detail(s, int(ref))
        if d is None:
            return {"series_id": int(ref), "found": False}
        shown = _user_names(s, None, [g["user_id"] for g in d["top_gifters"] if (g["name"] or "") in LEFT_NAMES])
        for g in d["top_gifters"]:
            g["left"] = (g["name"] or "") in LEFT_NAMES
            g["name"] = shown.get(g["user_id"], g["name"])
        now = time.time()
        for e in d["events"]:
            e["state"] = _event_state(e, now)
        return {**d, "found": True, "now": now}


def series_link(state_dir: Path, event_id: Any, to_event: Any = None) -> dict[str, Any]:
    """手で直す: event_id を to_event と同じ大会につなぐ (to_event が空なら、大会から外す)。"""
    ref = str(event_id or "").strip()
    to = str(to_event or "").strip()
    if not ref.isdigit() or (to and not to.isdigit()):
        raise ValueError("イベント番号を指定してください")
    with Store(state_dir) as s:
        out = series_mod.set_manual(s, int(ref), int(to) if to else None)
        out["taikai"] = _taikai(s, int(ref))
        return out


def _strength(s: Store, eid: int, now: float, support: list[str] | None) -> dict[str, Any]:
    st = event_analysis.event_strength(s, eid, now, _room_ids(s, support))
    st["rooms"] = st["rooms"][:EVENT_ROOMS]
    _room_labels(s, st["rooms"])
    _room_labels(s, st["focus"])
    return st


def _progress(s: Store, eid: int, rooms: list[dict[str, Any]]) -> dict[str, Any]:
    """ポイントの推移 (今のポイントの多い PROGRESS_ROOMS ルーム)。"""
    ats = [r[0] for r in s.all("SELECT DISTINCT at FROM event_progress WHERE event_id=? ORDER BY at", (eid,))]
    if not ats:
        return {"times": [], "rooms": []}
    last = s.all("""SELECT room_id, point FROM event_progress WHERE event_id=? AND at=(SELECT MAX(at) FROM event_progress WHERE event_id=?)
                    ORDER BY point DESC LIMIT ?""", (eid, eid, PROGRESS_ROOMS))
    names = {r["room_id"]: (r["key"], r["name"]) for r in rooms}
    out = []
    for r in last:
        pts = {p["at"]: (p["point"], p["rank"]) for p in s.all(
            "SELECT at, point, rank FROM event_progress WHERE event_id=? AND room_id=?", (eid, r["room_id"]))}
        key, name = names.get(r["room_id"]) or (None, None)
        if name is None:
            info = s.room(room_id=r["room_id"])
            key, name = (info["key"], info["name"]) if info is not None else (None, str(r["room_id"]))
        out.append({"room_id": r["room_id"], "key": key, "name": name or key or str(r["room_id"]),
                    "points": [pts.get(at, (None, None))[0] for at in ats], "ranks": [pts.get(at, (None, None))[1] for at in ats]})
    return {"times": ats, "rooms": out}
