"""research.db を、データを消さずに小さくする仕事 (shrink.py) のテスト。"""

import sqlite3

from showroom_keeper import explore, research_store, shrink
from showroom_keeper.research_store import Store

OLD_USER_ROOM = """CREATE TABLE user_room (user_id INTEGER, room_id INTEGER, cumulative INTEGER DEFAULT 0, cumulative_rank INTEGER,
                   visits INTEGER DEFAULT 0, events INTEGER DEFAULT 0, PRIMARY KEY (user_id, room_id))"""


def _old_db(tmp_path, rows):
    """前の形 (WITHOUT ROWID でない) の user_room を持つ research.db を作る。"""
    path = research_store.db_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(path)) as c:
        c.execute(OLD_USER_ROOM)
        c.execute("CREATE INDEX user_room_room ON user_room(room_id)")
        c.executemany("INSERT INTO user_room(user_id, room_id, cumulative, cumulative_rank, visits, events) VALUES (?, ?, ?, ?, ?, ?)", rows)
    research_store._ready.discard(str(path))


def _all(s):
    return [tuple(r) for r in s.all(f"SELECT {shrink.COLS} FROM user_room ORDER BY user_id, room_id")]


def test_event_pages_are_stored_packed_and_old_ones_are_packed_later(tmp_path, monkeypatch):
    monkeypatch.setattr(shrink, "PAGE_CHUNK", 2)
    text = "ポイントの数え方\n有料ギフト 1.5 倍\n" * 50
    with Store(tmp_path) as s:
        s.set_event_page(1, 200, text, 100.0)
        assert isinstance(s.one("SELECT text FROM event_pages WHERE event_id=1")[0], bytes)
        s.set_event_page(1, 0, None, 200.0)  # 読めなかった回は、前の文を残す
        assert research_store.unpack_text(s.one("SELECT text FROM event_pages WHERE event_id=1")[0]) == text
        # 前に縮めずに入れた文
        s.conn.executemany("INSERT INTO event_pages(event_id, at, status, text) VALUES (?, 1, 200, ?)",
                           [(i, f"{i} の文" * 30) for i in range(2, 7)] + [(7, None)])
        while shrink.pack_pages(s):
            pass
        rows = s.all("SELECT event_id, text FROM event_pages ORDER BY event_id")
        assert all(isinstance(r[1], bytes) for r in rows if r[0] != 7) and rows[-1][1] is None
        assert [research_store.unpack_text(r[1]) for r in rows[1:6]] == [f"{i} の文" * 30 for i in range(2, 7)]
        assert not shrink.pack_pages(s)  # 終われば、見るだけ
    assert research_store.unpack_text("前の文") == "前の文"


def test_event_detail_shows_unpacked_page(tmp_path):
    with Store(tmp_path) as s:
        s.upsert_events([{"event_id": 8, "name": "イベント", "start": 0, "end": 100}])
        s.set_event_page(8, 200, "ルール", 50.0)
    assert explore.event_detail(tmp_path, 8)["details"]["page"]["text"] == "ルール"


def test_old_user_room_is_rebuilt_without_losing_writes(tmp_path, monkeypatch):
    """前の形の user_room を少しずつ今の形に写し、写している間の書き込み (足す・直す・消す) も落とさずに入れ替える。"""
    monkeypatch.setattr(shrink, "ROOM_CHUNK", 3)
    monkeypatch.setattr(shrink, "DROP_CHUNK", 2)
    _old_db(tmp_path, [(u, r, u * r, 1, 0, u) for u in range(1, 6) for r in (10, 11)])
    with Store(tmp_path) as s:
        assert s.scalar("SELECT COUNT(*) FROM sqlite_master WHERE name='user_room_scan'") == 1
        assert shrink.rebuild_user_room(s)  # 横に作る
        assert shrink.rebuild_user_room(s)  # 3 行写す
        # 写した所・まだの所への書き込み
        s.conn.execute("UPDATE user_room SET events = events + 100 WHERE user_id=1 AND room_id=10")
        s.conn.execute("UPDATE user_room SET events = events + 100 WHERE user_id=5 AND room_id=11")
        s.conn.execute("""INSERT INTO user_room(user_id, room_id, events) VALUES (3, 12, 7)
                          ON CONFLICT(user_id, room_id) DO UPDATE SET events=events+excluded.events""")
        s.conn.execute("""INSERT INTO user_room(user_id, room_id, events) VALUES (4, 10, 7)
                          ON CONFLICT(user_id, room_id) DO UPDATE SET events=events+excluded.events""")
        s.conn.execute("DELETE FROM user_room WHERE user_id=2 AND room_id=11")
        want = _all(s)
        steps = 0
        while shrink.rebuild_user_room(s):
            steps += 1
            assert _all(s) == want  # 途中で入れ替わっても、読む中身は同じ
        assert steps > 5
        sql = s.one("SELECT sql FROM sqlite_master WHERE name='user_room'")[0]
        assert "WITHOUT ROWID" in sql.upper()
        assert s.all("SELECT name FROM sqlite_master WHERE name LIKE 'user_room_old%' OR name LIKE 'user_room_mirror%'") == []
        assert research_store._has_index_on(s.conn, "user_room", "room_id")
        assert _all(s) == want
        assert not shrink.rebuild_user_room(s) and shrink.step(s)
    research_store._ready.discard(str(research_store.db_path(tmp_path)))
    with Store(tmp_path) as s:  # 開き直しても、ルームの索引を 2 つ作らない
        names = [r[0] for r in s.all("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='user_room' AND sql IS NOT NULL")]
        assert names == ["user_room_new_room"]


def test_new_db_gets_room_index_once(tmp_path):
    with Store(tmp_path) as s:
        names = [r[0] for r in s.all("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='user_room' AND sql IS NOT NULL")]
        assert names == ["user_room_room"]
        assert not shrink.rebuild_user_room(s)


def test_step_waits_while_viewing_comes_first(tmp_path):
    _old_db(tmp_path, [(1, 10, 1, 1, 0, 0)])
    with Store(tmp_path) as s:
        assert not shrink.step(s, holding=lambda: True)
        assert s.one("SELECT 1 FROM sqlite_master WHERE name='user_room_new'") is None
        assert not shrink.step(s)  # 作り直しを進めた
        assert shrink.step(s)  # 終わった
        assert _all(s) == [(1, 10, 1, 1, 0, 0)]


def test_copy_reads_by_the_primary_key_without_sorting(tmp_path):
    """写す 1 回分は、主キーの順に続きから読む (表を全部読んで並べ替えない)。"""
    _old_db(tmp_path, [(1, 10, 1, 1, 0, 0)])
    with Store(tmp_path) as s:
        plan = " ".join(r[3] for r in s.conn.execute(f"""EXPLAIN QUERY PLAN SELECT {shrink.COLS} FROM user_room
                                                         WHERE (user_id, room_id) > (?, ?) ORDER BY user_id, room_id LIMIT ?""",
                                                      (1, 1, 10)))
    assert "SEARCH" in plan and "TEMP B-TREE" not in plan, plan
