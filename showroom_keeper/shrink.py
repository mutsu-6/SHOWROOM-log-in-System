"""research.db を、データを消さずに小さくする仕事 (裏の catchup が、少しずつ行う)。

2026-10-26 にディスクが 1 日 0.9GB ずつ減っていた (Cursor が測った)。そのうち、データを消さずに減らせる 2 つ:
- イベントのページの文 (event_pages、257MB): 縮めずに入れていた。新しい分は research_store.pack_text で縮めて入れ、前の分もここで縮め直す
- user_room (524MB): 前の形 (WITHOUT ROWID でない) のままで、表の本体・主キーの索引・ルームの索引・読む欄を全部入れた索引
  (user_room_scan) と、同じ中身を 3 重に持っていた。今の形 (WITHOUT ROWID) の表を横に作って少しずつ写し、入れ替える

どちらも、1 回の書き込みを短く (CHUNK 行ずつ) して、収集や視聴の書き込みを長く待たせない。視聴を先にする時 (holding) は次に回す。
空いたページはファイルの中に残り、そのあとに増える分に使われる (ファイルは小さくならないが、そのぶん大きくならない)。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable

from .research_store import Store, pack_text

log = logging.getLogger(__name__)

BUDGET = 20.0  # 1 回に使う秒数 (catchup は 10 分ごと・管理画面を開いた時に、60 秒あけて動く)
PAGE_CHUNK = 200  # 縮め直すイベントのページの数 (1 回の書き込み)
ROOM_CHUNK = 20000  # user_room を写す行数 (1 回の書き込み)
DROP_CHUNK = 50000  # 入れ替えたあと、前の表から消す行数 (1 回の書き込み)

COLS = "user_id, room_id, cumulative, cumulative_rank, visits, events"
NEW_TABLE = """CREATE TABLE IF NOT EXISTS user_room_new (
    user_id INTEGER, room_id INTEGER, cumulative INTEGER DEFAULT 0, cumulative_rank INTEGER,
    visits INTEGER DEFAULT 0, events INTEGER DEFAULT 0, PRIMARY KEY (user_id, room_id)) WITHOUT ROWID"""
# 写している間に user_room に書いた分を、新しい表にも書く (写した所も、まだの所も、user_room の今の値になる)
MIRROR = [
    f"""CREATE TRIGGER IF NOT EXISTS user_room_mirror_ins AFTER INSERT ON user_room BEGIN
        INSERT OR REPLACE INTO user_room_new({COLS})
        VALUES (NEW.user_id, NEW.room_id, NEW.cumulative, NEW.cumulative_rank, NEW.visits, NEW.events); END""",
    f"""CREATE TRIGGER IF NOT EXISTS user_room_mirror_upd AFTER UPDATE ON user_room BEGIN
        DELETE FROM user_room_new WHERE user_id=OLD.user_id AND room_id=OLD.room_id;
        INSERT OR REPLACE INTO user_room_new({COLS})
        VALUES (NEW.user_id, NEW.room_id, NEW.cumulative, NEW.cumulative_rank, NEW.visits, NEW.events); END""",
    """CREATE TRIGGER IF NOT EXISTS user_room_mirror_del AFTER DELETE ON user_room BEGIN
        DELETE FROM user_room_new WHERE user_id=OLD.user_id AND room_id=OLD.room_id; END""",
]
TRIGGERS = ("user_room_mirror_ins", "user_room_mirror_upd", "user_room_mirror_del")


def step(s: Store, holding: Callable[[], bool] = lambda: False, budget: float = BUDGET) -> bool:
    """BUDGET 秒まで、縮める仕事を進める。全部終わっていれば True (そのあとは、表を 1 つずつ見るだけ)。"""
    until = time.monotonic() + budget
    done = True
    for fn in (pack_pages, rebuild_user_room):
        while True:
            if holding() or time.monotonic() > until:
                return False
            more = fn(s)
            if not more:
                break
            done = False
    return done


def pack_pages(s: Store) -> bool:
    """縮めずに入れたイベントのページの文を、PAGE_CHUNK 件ずつ縮め直す。まだ残っていれば True。
    どこまで見たかを meta に覚えて、イベントの番号の順に 1 回だけ見る (縮めた行を読み直さない)。"""
    if s.meta("pages_packed") == "done":
        return False
    after = int(s.meta("pages_packed", -1))
    with s.tx() as c:
        rows = c.execute("SELECT event_id, text FROM event_pages WHERE event_id > ? ORDER BY event_id LIMIT ?",
                         (after, PAGE_CHUNK)).fetchall()
        c.executemany("UPDATE event_pages SET text=? WHERE event_id=?",
                      [(pack_text(r[1]), r[0]) for r in rows if isinstance(r[1], str)])
        last = "done" if len(rows) < PAGE_CHUNK else rows[-1][0]
        s.set_meta("pages_packed", last)
    return last != "done"


def _old_form(s: Store) -> bool:
    row = s.one("SELECT sql FROM sqlite_master WHERE type='table' AND name='user_room'")
    return row is not None and "WITHOUT ROWID" not in (row[0] or "").upper()


def rebuild_user_room(s: Store) -> bool:
    """前の形の user_room を、今の形 (WITHOUT ROWID) に少しずつ作り直す。まだ続きがあれば True。

    1. 横に user_room_new (とルームの索引) を作り、user_room に書いた分を写す仕掛け (MIRROR) を付ける
    2. 人・ルームの順に ROOM_CHUNK 行ずつ写す (どこまで写したかは meta の user_room_copied)
    3. 写し終えたら、行の数が同じかを確かめて、名前を入れ替える (前の表は user_room_old)
    4. user_room_old の索引を 1 つずつ消し、行を DROP_CHUNK 行ずつ消して、最後に表を消す"""
    if s.one("SELECT 1 FROM sqlite_master WHERE type='table' AND name='user_room_old'") is not None:
        return _drop_old(s)
    if not _old_form(s):
        return False
    if s.one("SELECT 1 FROM sqlite_master WHERE type='table' AND name='user_room_new'") is None:
        with s.tx() as c:
            c.execute(NEW_TABLE)
            c.execute("CREATE INDEX IF NOT EXISTS user_room_new_room ON user_room_new(room_id)")
            for sql in MIRROR:
                c.execute(sql)
            c.execute("DELETE FROM meta WHERE k='user_room_copied'")
        log.info("user_room を今の形に作り直し始めます")
        return True
    at = s.meta("user_room_copied")
    if at != "done":
        uid, rid = at or (-1, -1)
        with s.tx() as c:
            rows = c.execute(f"""SELECT {COLS} FROM user_room WHERE (user_id, room_id) > (?, ?)
                                 ORDER BY user_id, room_id LIMIT ?""", (uid, rid, ROOM_CHUNK)).fetchall()
            c.executemany(f"INSERT OR REPLACE INTO user_room_new({COLS}) VALUES (?, ?, ?, ?, ?, ?)", [tuple(r) for r in rows])
            s.set_meta("user_room_copied", "done" if len(rows) < ROOM_CHUNK else [rows[-1][0], rows[-1][1]])
        return True
    with s.tx() as c:
        old, new = (c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("user_room", "user_room_new"))
        if old != new:  # 起きないはず。写し直す (写した分は上書きになるだけ)
            log.warning("user_room の作り直しで行の数が合いません (%d と %d)。最初から写し直します", old, new)
            c.execute("DELETE FROM meta WHERE k='user_room_copied'")
            return True
        for t in TRIGGERS:
            c.execute(f"DROP TRIGGER IF EXISTS {t}")
        c.execute("ALTER TABLE user_room RENAME TO user_room_old")
        c.execute("ALTER TABLE user_room_new RENAME TO user_room")
        c.execute("DELETE FROM meta WHERE k='user_room_copied'")
    log.info("user_room を今の形に入れ替えました (%d 行)。前の表を少しずつ消します", new)
    return True


def _drop_old(s: Store) -> bool:
    """入れ替えた前の表 (user_room_old) を、短い書き込みに分けて消す。"""
    idx = s.one("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='user_room_old' AND sql IS NOT NULL")
    with s.tx() as c:
        if idx is not None:
            c.execute(f"DROP INDEX IF EXISTS {idx[0]}")
            return True
        n = c.execute("DELETE FROM user_room_old WHERE rowid IN (SELECT rowid FROM user_room_old ORDER BY rowid LIMIT ?)",
                      (DROP_CHUNK,)).rowcount
        if n < DROP_CHUNK:
            c.execute("DROP TABLE user_room_old")
            log.info("user_room の作り直しが終わりました")
            return False
    return True


def status(s: Store) -> dict[str, Any]:
    """どこまで進んだか (確かめる時に読む)。"""
    return {"pages_packed": s.meta("pages_packed"), "user_room_old_form": _old_form(s),
            "user_room_copied": s.meta("user_room_copied"),
            "user_room_old": s.one("SELECT 1 FROM sqlite_master WHERE name='user_room_old'") is not None}
