"""上の検索欄の索引 (SQLite の FTS5 の trigram。名前を 3 文字ずつに区切った索引)。

前は検索のたびに、ギフター (users・user_names)・ルーム・イベントの名前を全部、LIKE でくらべていた
(サーバーで 1 回目 11 秒・2 回目 4 秒。ひらがな・カタカナのまざった名前のために、ルームの名前を 10 分ごとに全部そろえ直すのに 5 秒。
2026-10-19 に Cursor が測った)。ここでは、そろえた形 (explore._fold: 全角半角・大文字小文字・空白・ひらがなカタカナ) の名前を
索引に入れておき、3 文字以上ならその索引で引く。1〜2 文字は索引の中を全部くらべる (それでも、そろえた形が入っているので前より軽い)。

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

TABLES = (
    "CREATE VIRTUAL TABLE IF NOT EXISTS search_users USING fts5(folded, tokenize='trigram')",  # rowid = user_id
    # 前の名前 (1 人に何個もある)。rowid は自動
    "CREATE VIRTUAL TABLE IF NOT EXISTS search_former USING fts5(folded, user_id UNINDEXED, name UNINDEXED, tokenize='trigram')",
    "CREATE VIRTUAL TABLE IF NOT EXISTS search_rooms USING fts5(name, key, tokenize='trigram')",  # rowid = room_id
    "CREATE VIRTUAL TABLE IF NOT EXISTS search_events USING fts5(name, key, tokenize='trigram')",  # rowid = event_id
)
KINDS = ("u", "r", "e", "f")
_ok: dict[str, bool] = {}
_lock = threading.Lock()


def _fold(text: Any) -> str:
    from .explore import _fold as fold

    return fold(text)


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
            for t in ("search_users", "search_former", "search_rooms", "search_events"):
                c.execute(f"DELETE FROM {t}")
            c.execute("DELETE FROM search_dirty")  # 最初から入れるので、印も要らない
        st = {"v": VERSION, "u": 0, "r": 0, "e": 0, "f": [0, ""], "done": []}
        s.set_meta("search_index", st)
    return st


def _fill(s: Store, st: dict[str, Any], kind: str) -> None:
    """表の最初から FILL_STEP 行ずつ入れる。"""
    with s.tx() as c:
        if kind == "u":
            rows = c.execute("SELECT user_id, name FROM users WHERE user_id > ? ORDER BY user_id LIMIT ?", (st["u"], FILL_STEP)).fetchall()
            c.executemany("INSERT INTO search_users(rowid, folded) VALUES (?, ?)", [(r[0], _fold(r[1])) for r in rows if r[1]])
            last = rows[-1][0] if rows else None
        elif kind == "f":
            rows = c.execute("SELECT user_id, name FROM user_names WHERE (user_id, name) > (?, ?) ORDER BY user_id, name LIMIT ?",
                             (*st["f"], FILL_STEP)).fetchall()
            c.executemany("INSERT INTO search_former(folded, user_id, name) VALUES (?, ?, ?)",
                          [(_fold(r[1]), r[0], r[1]) for r in rows if r[1]])
            last = [rows[-1][0], rows[-1][1]] if rows else None
        else:
            table, col = ("rooms", "room_id") if kind == "r" else ("events", "event_id")
            rows = c.execute(f"SELECT {col}, name, key FROM {table} WHERE {col} > ? ORDER BY {col} LIMIT ?", (st[kind], FILL_STEP)).fetchall()
            c.executemany(f"INSERT INTO search_{table}(rowid, name, key) VALUES (?, ?, ?)",
                          [(r[0], _fold(r[1]), _fold(r[2])) for r in rows if r[1] or r[2]])
            last = rows[-1][0] if rows else None
        if last is not None:
            # いま入れた範囲に付いていた印は要らない (入れ直すと、前の名前が 2 つ入る)
            if kind == "f":
                c.execute("DELETE FROM search_dirty WHERE kind='f' AND (ref, name) > (?, ?) AND (ref, name) <= (?, ?)", (*st["f"], *last))
            else:
                c.execute("DELETE FROM search_dirty WHERE kind=? AND ref > ? AND ref <= ?", (kind, st[kind], last))
            st[kind] = last
        if len(rows) < FILL_STEP:
            st["done"].append(kind)
        c.execute("INSERT INTO meta(k, v) VALUES ('search_index', ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (_json(st),))


def _json(v: Any) -> str:
    import json

    return json.dumps(v)


def _dirty(s: Store, st: dict[str, Any]) -> int:
    """印の付いた名前を、DIRTY_STEP 個まで入れ直す。まだ最初から入れている途中の所より先は、入れる時に入るので飛ばす。"""
    rows = s.all("SELECT kind, ref, name FROM search_dirty LIMIT ?", (DIRTY_STEP,))
    if not rows:
        return 0
    with s.tx() as c:
        for kind, ref, name in rows:
            ahead = kind not in st["done"] and ((ref, name) > tuple(st["f"]) if kind == "f" else ref > st[kind])
            if not ahead:
                if kind == "u":
                    c.execute("DELETE FROM search_users WHERE rowid=?", (ref,))
                    r = c.execute("SELECT name FROM users WHERE user_id=?", (ref,)).fetchone()
                    if r is not None and r[0]:
                        c.execute("INSERT INTO search_users(rowid, folded) VALUES (?, ?)", (ref, _fold(r[0])))
                elif kind == "f":
                    c.execute("INSERT INTO search_former(folded, user_id, name) VALUES (?, ?, ?)", (_fold(name), ref, name))
                else:
                    table, col = ("rooms", "room_id") if kind == "r" else ("events", "event_id")
                    c.execute(f"DELETE FROM search_{table} WHERE rowid=?", (ref,))
                    r = c.execute(f"SELECT name, key FROM {table} WHERE {col}=?", (ref,)).fetchone()
                    if r is not None and (r[0] or r[1]):
                        c.execute(f"INSERT INTO search_{table}(rowid, name, key) VALUES (?, ?, ?)", (ref, _fold(r[0]), _fold(r[1])))
            c.execute("DELETE FROM search_dirty WHERE kind=? AND ref=? AND name=?", (kind, ref, name))
    return len(rows)


def refresh(s: Store, budget: float = SEARCH_BUDGET) -> bool:
    """索引を、budget 秒まで足す (印の付いた名前と、最初から入れている途中の分)。使える (入れ終わった) なら True。"""
    if not ensure(s):
        return False
    with _lock:  # 検索と収集の合間が、同じ分を 2 回入れないように
        st = _state(s)
        until = time.monotonic() + budget
        while time.monotonic() < until:
            todo = [k for k in KINDS if k not in st["done"]]
            if todo:
                _fill(s, st, todo[0])
            elif not _dirty(s, st):
                break
        return len(st["done"]) == len(KINDS)


def where(table: str, cols: tuple[str, ...], folded: str) -> tuple[str, list[Any]]:
    """そろえた形 folded を含む行を選ぶ条件 (と、その値)。3 文字以上は索引で、それより短ければ索引の中を全部くらべる。"""
    if len(folded) >= 3:
        return f"{table} MATCH ?", ['"' + folded.replace('"', '""') + '"']
    return "(" + " OR ".join(f"instr({c}, ?) > 0" for c in cols) + ")", [folded] * len(cols)
