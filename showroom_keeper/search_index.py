"""上の検索欄の索引 (SQLite の FTS5 の trigram。名前を 3 文字ずつに区切った索引)。

前は検索のたびに、ギフター (users・user_names)・ルーム・イベントの名前を全部、LIKE でくらべていた
(サーバーで 1 回目 11 秒・2 回目 4 秒。ひらがな・カタカナのまざった名前のために、ルームの名前を 10 分ごとに全部そろえ直すのに 5 秒。
2026-10-19 に Cursor が測った)。ここでは、そろえた形 (explore._fold: 全角半角・大文字小文字・空白・ひらがなカタカナ) の名前を
索引に入れておき、3 文字以上ならその索引で引く。2 文字は、2 文字ずつに区切った索引 (search2_*) で引く。
1 文字は索引の中身を全部くらべる (それでも、そろえた形が入っているので前より軽い)。

- 名前が入った・変わった時は、research.db の仕掛け (トリガー) が search_dirty に印を付け、refresh がその分だけ入れ直す
- 初めて (と VERSION を変えた時) は、表の最初から少しずつ入れる (fill)。入れ終わるまでは、検索は前のやり方で探す
- この SQLite に trigram が無ければ、索引は作らず、前のやり方のまま
"""

from __future__ import annotations

import sqlite3
import threading
import time
from typing import Any

from .research_store import Store

VERSION = 1  # 索引の作り方を変えたら上げる (最初から入れ直す)
FILL_STEP = 20000  # 最初から入れる時に、1 回に読む行の数
DIRTY_STEP = 5000  # 印の付いた名前を、1 回に入れ直す数
SEARCH_BUDGET = 1.0  # 検索の時に、索引を足すのに使ってよい秒数 (残りは収集の合間に)
SWEEP_BUDGET = 20.0  # 収集の合間に、索引を足すのに使ってよい秒数

# 3 文字ずつの索引 (trigram) と、2 文字の検索のための 2 文字ずつの索引 (名前を「みく くち ちゃ」のように 2 文字ずつ区切って入れ、
# ascii の区切り方で引く。trigram は 3 文字より短いと使えず、2 文字の検索が索引の中を全部くらべて遅かった。2026-10-19 に Cursor が測った)
TABLES = (
    "CREATE VIRTUAL TABLE IF NOT EXISTS search_users USING fts5(folded, tokenize='trigram')",  # rowid = user_id
    # 前の名前 (1 人に何個もある)。rowid は自動
    "CREATE VIRTUAL TABLE IF NOT EXISTS search_former USING fts5(folded, user_id UNINDEXED, name UNINDEXED, tokenize='trigram')",
    "CREATE VIRTUAL TABLE IF NOT EXISTS search_rooms USING fts5(name, key, tokenize='trigram')",  # rowid = room_id
    "CREATE VIRTUAL TABLE IF NOT EXISTS search_events USING fts5(name, key, tokenize='trigram')",  # rowid = event_id
    "CREATE VIRTUAL TABLE IF NOT EXISTS search2_users USING fts5(folded, tokenize='ascii')",
    "CREATE VIRTUAL TABLE IF NOT EXISTS search2_former USING fts5(folded, user_id UNINDEXED, name UNINDEXED, tokenize='ascii')",
    "CREATE VIRTUAL TABLE IF NOT EXISTS search2_rooms USING fts5(name, key, tokenize='ascii')",
    "CREATE VIRTUAL TABLE IF NOT EXISTS search2_events USING fts5(name, key, tokenize='ascii')",
)
KINDS = ("u", "r", "e", "f")  # 3 文字ずつの索引。2 文字ずつの索引は、後ろに 2 を付ける (u2 など。後から足したので、別に入れる)
PARTS = KINDS + tuple(k + "2" for k in KINDS)
_SOURCE = {"u": ("users", "user_id"), "r": ("rooms", "room_id"), "e": ("events", "event_id")}
_TARGET = {"u": "users", "r": "rooms", "e": "events", "f": "former"}
_ok: dict[str, bool] = {}
_lock = threading.Lock()


def _fold(text: Any) -> str:
    from .explore import _fold as fold

    return fold(text)


def grams(folded: str) -> str:
    """2 文字ずつに区切った形 (「みくちゃ」→「みく くち ちゃ」)。1 文字の名前は、その 1 文字。"""
    if len(folded) < 2:
        return folded
    return " ".join(folded[i:i + 2] for i in range(len(folded) - 1))


def ensure(s: Store) -> bool:
    """索引の表を作る。この SQLite で作れなければ False (印も溜めない)。"""
    key = str(s.state_dir)
    if key not in _ok:
        try:
            for sql in TABLES:
                s.conn.execute(sql)
            _ok[key] = True
        except sqlite3.OperationalError:  # FTS5 か trigram が無い SQLite
            _ok[key] = False
            s.conn.execute("DELETE FROM search_dirty")
    return _ok[key]


def _state(s: Store) -> dict[str, Any]:
    st = s.meta("search_index")
    if not st or st.get("v") != VERSION:
        with s.tx() as c:
            for k in KINDS:
                c.execute(f"DELETE FROM search_{_TARGET[k]}")
                c.execute(f"DELETE FROM search2_{_TARGET[k]}")
            c.execute("DELETE FROM search_dirty")  # 最初から入れるので、印も要らない
        st = {"v": VERSION, "done": []}
    if any(part not in st for part in PARTS):
        for part in PARTS:  # 後から足した索引 (2 文字ずつ) は、前の索引を残したまま、最初から入れる
            st.setdefault(part, [0, ""] if part[0] == "f" else 0)
        s.set_meta("search_index", st)
    return st


def _put(c: sqlite3.Connection, part: str, ref: int, values: tuple[Any, ...], check: bool = False) -> None:
    """1 つの名前を、その索引に入れる (values はそろえる前の名前。ギフター・前の名前は (名前,)、ルーム・イベントは (名前, URL キー))。

    check: 前の名前が、もう入っていれば入れない (前の名前は 1 人に何個もあり、rowid で入れ替えられないので)。
    """
    kind, two = part[0], part.endswith("2")
    table = f"search{'2' if two else ''}_{_TARGET[kind]}"
    form = (lambda v: grams(_fold(v))) if two else _fold
    if kind == "f":
        folded = form(values[0])
        if check:
            short = len(_fold(values[0])) < (2 if two else 3)  # 索引で引けない短い名前は、中身を全部くらべる (少ない)
            hit = c.execute(f"SELECT 1 FROM {table} WHERE {'folded = ?' if short else f'{table} MATCH ?'} AND user_id=? AND name=? LIMIT 1",
                            (folded if short else '"' + folded.replace('"', '""') + '"', ref, values[0])).fetchone()
            if hit:
                return
        c.execute(f"INSERT INTO {table}(folded, user_id, name) VALUES (?, ?, ?)", (folded, ref, values[0]))
    elif kind == "u":
        c.execute(f"INSERT INTO {table}(rowid, folded) VALUES (?, ?)", (ref, form(values[0])))
    else:
        c.execute(f"INSERT INTO {table}(rowid, name, key) VALUES (?, ?, ?)", (ref, form(values[0]), form(values[1])))


def _fill(s: Store, st: dict[str, Any], part: str) -> None:
    """表の最初から FILL_STEP 行ずつ入れる。"""
    kind = part[0]
    with s.tx() as c:
        if kind == "f":
            rows = c.execute("SELECT user_id, name FROM user_names WHERE (user_id, name) > (?, ?) ORDER BY user_id, name LIMIT ?",
                             (*st[part], FILL_STEP)).fetchall()
            for r in rows:
                if r[1]:
                    _put(c, part, r[0], (r[1],))
            last = [rows[-1][0], rows[-1][1]] if rows else None
        else:
            table, col = _SOURCE[kind]
            cols = "name" if kind == "u" else "name, key"
            rows = c.execute(f"SELECT {col}, {cols} FROM {table} WHERE {col} > ? ORDER BY {col} LIMIT ?", (st[part], FILL_STEP)).fetchall()
            for r in rows:
                if any(r[1:]):
                    _put(c, part, r[0], tuple(r[1:]))
            last = rows[-1][0] if rows else None
        if last is not None:
            st[part] = last
        if len(rows) < FILL_STEP:
            st["done"].append(part)
        c.execute("INSERT INTO meta(k, v) VALUES ('search_index', ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (_json(st),))


def _json(v: Any) -> str:
    import json

    return json.dumps(v)


def _ahead(st: dict[str, Any], part: str, ref: int, name: str) -> bool:
    """その名前は、最初から入れている途中の所より先か (先なら、入れる時に入るので、印では入れない)。"""
    if part in st["done"]:
        return False
    return (ref, name) > tuple(st[part]) if part[0] == "f" else ref > st[part]


def _dirty(s: Store, st: dict[str, Any]) -> int:
    """印の付いた名前を、DIRTY_STEP 個まで入れ直す。それぞれの索引で、最初から入れている途中の所より先は、入れる時に入るので飛ばす。"""
    rows = s.all("SELECT kind, ref, name FROM search_dirty LIMIT ?", (DIRTY_STEP,))
    if not rows:
        return 0
    with s.tx() as c:
        for kind, ref, name in rows:
            for part in (kind, kind + "2"):
                if _ahead(st, part, ref, name):
                    continue
                if kind == "f":
                    _put(c, part, ref, (name,), check=True)  # 入れる時に入った名前かもしれない
                    continue
                table, col = _SOURCE[kind]
                c.execute(f"DELETE FROM search{'2' if part.endswith('2') else ''}_{_TARGET[kind]} WHERE rowid=?", (ref,))
                r = c.execute(f"SELECT {'name' if kind == 'u' else 'name, key'} FROM {table} WHERE {col}=?", (ref,)).fetchone()
                if r is not None and any(r):
                    _put(c, part, ref, tuple(r))
            c.execute("DELETE FROM search_dirty WHERE kind=? AND ref=? AND name=?", (kind, ref, name))
    return len(rows)


def refresh(s: Store, budget: float = SEARCH_BUDGET) -> dict[str, bool]:
    """索引を、budget 秒まで足す (印の付いた名前と、最初から入れている途中の分)。

    戻り値は、使える (入れ終わった) 索引 {"three": 3 文字ずつ, "two": 2 文字ずつ}。
    """
    if not ensure(s):
        return {"three": False, "two": False}
    with _lock:  # 検索と収集の合間が、同じ分を 2 回入れないように
        st = _state(s)
        until = time.monotonic() + budget
        while time.monotonic() < until:
            todo = [p for p in PARTS if p not in st["done"]]
            if todo:
                _fill(s, st, todo[0])
            elif not _dirty(s, st):
                break
        _dirty(s, st)  # 入れている途中でも、入れ終わった所の名前の変わりは入れる (時間が無くても 1 回)
        return {"three": all(k in st["done"] for k in KINDS), "two": all(k + "2" in st["done"] for k in KINDS)}


def where(table: str, cols: tuple[str, ...], folded: str, two: bool = False) -> tuple[str, str, list[Any]]:
    """そろえた形 folded を含む行を選ぶ (読む表, 条件, その値)。

    3 文字以上は 3 文字ずつの索引で、2 文字は 2 文字ずつの索引 (two) で引く。1 文字 (か 2 文字ずつの索引がまだ無い時) は、
    索引の中身の表 (<表>_content。c0, c1 … が列) を全部くらべる。table は 3 文字ずつの索引の名前 (search_users など)。
    """
    phrase = '"' + folded.replace('"', '""') + '"'
    if len(folded) >= 3:
        return table, f"{table} MATCH ?", [phrase]
    if len(folded) == 2 and two:
        t2 = table.replace("search_", "search2_", 1)
        return t2, f"{t2} MATCH ?", [phrase]
    # 索引 (仮想の表) を 1 行ずつ読むより、中身のふつうの表を読むほうが速い
    names = {"search_users": ("folded",), "search_former": ("folded", "user_id", "name")}.get(table, ("name", "key"))
    src = f"(SELECT id AS rowid, {', '.join(f'c{i} AS {n}' for i, n in enumerate(names))} FROM {table}_content)"
    return src, "(" + " OR ".join(f"instr({c}, ?) > 0" for c in cols) + ")", [folded] * len(cols)
