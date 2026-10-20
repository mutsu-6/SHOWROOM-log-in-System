import time

import pytest

from showroom_keeper import explore, memory, room_parts, selfcheck
from showroom_keeper.research_store import Store

NOW = time.time()


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(room_parts, "SAVE", True)
    monkeypatch.setattr(room_parts, "WAIT", True)
    room_parts._opened.clear()
    with Store(tmp_path) as s:
        s.upsert_rooms([{"room_id": 1, "key": "aaa", "name": "ルームA"}, {"room_id": 2, "key": "bbb", "name": "ルームB"}], "live", NOW)
        s.upsert_events([{"event_id": 10, "name": "終わったイベント", "key": "ev", "start": NOW - 9 * 86400, "end": NOW - 2 * 86400}])
        s.set_event_rooms(10, [{"room_id": 1, "point": 5000, "rank": 1}, {"room_id": 2, "point": 3000, "rank": 2}], NOW)
        s.set_contributions(10, 1, [{"user_id": 7, "name": "a", "point": 4000, "rank": 1},
                                    {"user_id": 8, "name": "b", "point": 1000, "rank": 2}], NOW - 100, final=True)
        s.set_contributions(10, 2, [{"user_id": 7, "name": "a", "point": 2500, "rank": 1}], NOW - 100, final=True)
    return tmp_path


def stored(d):
    with Store(d) as s:
        return [dict(r) for r in s.all("SELECT room_id, at, src FROM room_parts ORDER BY room_id")]


def test_page_reads_what_was_built(db):
    first = explore.room_detail(db, "aaa")
    assert [(g["user_id"], g["rooms"]) for g in first["gifters"]] == [(7, 2), (8, 1)]
    assert [(o["key"], o["shared"]) for o in first["overlap"]] == [("bbb", 1)]
    assert [r["room_id"] for r in stored(db)] == [1]  # 開いた時に作って保存した
    with Store(db) as s:  # 作ったあとに増えた分は、作り直すまでページに出ない (ページは作っておいた分を読むだけ)
        s.set_contributions(10, 2, [{"user_id": 7, "name": "a", "point": 2500, "rank": 1},
                                    {"user_id": 8, "name": "b", "point": 10, "rank": 2}], NOW - 50, final=True)
    again = explore.room_detail(db, "aaa")
    assert again["overlap"] == first["overlap"] and again["analysis"]["rounds"] == first["analysis"]["rounds"]
    assert again["parts_at"] == first["parts_at"]
    assert [(g["user_id"], g["rooms"]) for g in again["gifters"]] == [(7, 2), (8, 1)]


def test_gifters_that_joined_the_top_later_are_counted_on_the_page(db):
    explore.room_detail(db, "aaa")
    with Store(db) as s:
        s.set_contributions(10, 1, [{"user_id": 7, "name": "a", "point": 4000, "rank": 1},
                                    {"user_id": 8, "name": "b", "point": 1000, "rank": 2},
                                    {"user_id": 9, "name": "c", "point": 500, "rank": 3}], NOW - 50, final=True)
    assert {g["user_id"]: g["rooms"] for g in explore.room_detail(db, "aaa")["gifters"]}[9] == 1


def test_step_rebuilds_when_a_ranking_comes_in_or_a_day_passes(db):
    explore.room_detail(db, "aaa")
    with Store(db) as s:
        assert room_parts.step(s) is False  # 新しいまま
        s.set_contributions(10, 2, [{"user_id": 7, "name": "a", "point": 2500, "rank": 1},
                                    {"user_id": 8, "name": "b", "point": 10, "rank": 2}], NOW, final=True)
        s.conn.execute("UPDATE room_events SET ranking_at=? WHERE room_id=1", (NOW + 10,))  # このルームの貢献ランキングが新しく入った
        assert room_parts.step(s) is True and room_parts.step(s) is False
    assert stored(db)[0]["src"] == NOW + 10
    assert [(o["key"], o["shared"]) for o in explore.room_detail(db, "aaa")["overlap"]] == [("bbb", 2)]
    with Store(db) as s:
        assert room_parts.step(s, NOW + room_parts.EVERY + 60) is True  # 1 日たった
        s.conn.execute("UPDATE room_parts SET version=0")
        assert room_parts.step(s, NOW + room_parts.EVERY + 120) is True  # 作り方を変えた


def test_step_forgets_rooms_nobody_opens(db):
    explore.room_detail(db, "aaa")
    room_parts._opened.clear()
    with Store(db) as s:
        assert room_parts.step(s, NOW + room_parts.KEEP + 60) is False
    assert stored(db) == []


def test_built_only_when_the_server_is_idle(monkeypatch):
    monkeypatch.setattr(room_parts, "_seen", 0.0)
    monkeypatch.setattr(selfcheck, "CHECKER", None)
    assert room_parts.idle()
    room_parts.seen()
    assert not room_parts.idle()  # 管理画面が使われている
    assert room_parts.idle(time.time() + room_parts.IDLE_ADMIN + 1)

    class Checker:
        load = {"iowait": 0.3}

    monkeypatch.setattr(selfcheck, "CHECKER", Checker())
    assert not room_parts.idle(time.time() + room_parts.IDLE_ADMIN + 1)  # ディスク待ちが多い
    Checker.load = {"iowait": 0.05}
    assert room_parts.idle(time.time() + room_parts.IDLE_ADMIN + 1)
    memory.hold_off(60)
    try:
        assert not room_parts.idle(time.time() + room_parts.IDLE_ADMIN + 1)  # 自己点検が後回しにしている
    finally:
        monkeypatch.setattr(memory, "_hold_until", 0.0)
