import time

import pytest

from showroom_keeper import explore
from showroom_keeper.research_store import Store

NOW = time.time()


@pytest.fixture
def db(tmp_path):
    with Store(tmp_path) as s:
        s.upsert_rooms([{"room_id": 1, "key": "aaa", "name": "ルームA", "followers": 900},
                        {"room_id": 2, "key": "bbb", "name": "ルームB", "followers": 50},
                        {"room_id": 3, "key": "ccc", "name": "ルームC", "followers": 10}], "live", NOW)
        s.upsert_events([{"event_id": 10, "name": "終わったイベント", "key": "old_ev", "start": NOW - 9 * 86400, "end": NOW - 2 * 86400},
                         {"event_id": 11, "name": "開催中イベント", "key": "now_ev", "start": NOW - 86400, "end": NOW + 3600},
                         {"event_id": 12, "name": "これからのイベント", "key": "next_ev", "start": NOW + 86400, "end": NOW + 9 * 86400}])
        s.set_event_rooms(10, [{"room_id": 1, "point": 5000, "rank": 1}, {"room_id": 2, "point": 3000, "rank": 2},
                               {"room_id": 3, "point": 0, "rank": 3}], NOW)
        s.set_event_rooms(11, [{"room_id": 1, "point": 100, "rank": 2}, {"room_id": 2, "point": 200, "rank": 1}], NOW)
        s.set_contributions(10, 1, [{"user_id": 7, "name": "大口", "point": 4000, "rank": 1},
                                    {"user_id": 8, "name": "中堅", "point": 1000, "rank": 2}], NOW, final=True)
        s.set_contributions(10, 2, [{"user_id": 7, "name": "大口", "point": 2500, "rank": 1},
                                    {"user_id": 9, "name": "退会済みユーザー", "point": 500, "rank": 2}], NOW, final=True)
        s.set_cumulative(1, [{"user_id": 7, "name": "大口", "point": 90000, "visits": 40, "rank": 1},
                             {"user_id": 8, "name": "中堅", "point": 2000, "visits": 5, "rank": 2}], NOW)
        s.add_progress(11, [{"room_id": 1, "point": 50, "rank": 1}, {"room_id": 2, "point": 40, "rank": 2}], NOW - 7200)
        s.add_progress(11, [{"room_id": 1, "point": 100, "rank": 2}, {"room_id": 2, "point": 200, "rank": 1}], NOW - 3600)
    return tmp_path


def test_search_finds_gifters_rooms_and_events(db):
    r = explore.search(db, "大口")
    assert [g["user_id"] for g in r["gifters"]] == [7] and r["gifters"][0]["points"] == 90000 + 2500
    assert [x["key"] for x in explore.search(db, "ルーム")["rooms"]] == ["aaa", "bbb", "ccc"]  # フォロワーの多い順
    assert explore.search(db, "bbb")["rooms"][0]["name"] == "ルームB"
    ev = explore.search(db, "イベント")["events"]
    assert {e["event_id"]: e["state"] for e in ev} == {10: "ended", 11: "ongoing", 12: "upcoming"}
    assert explore.search(db, "8")["gifters"][0]["user_id"] == 8  # ユーザー ID
    assert explore.search(db, "") == {"gifters": [], "rooms": [], "events": []}


def test_room_detail(db):
    d = explore.room_detail(db, "aaa")
    assert d["found"] and d["name"] == "ルームA" and d["room_id"] == 1
    assert [(c["rank"], c["name"], c["point"]) for c in d["cumulative"]] == [(1, "大口", 90000), (2, "中堅", 2000)]
    assert [(g["user_id"], g["points"], g["rooms"]) for g in d["gifters"]] == [(7, 90000, 2), (8, 2000, 1)]
    assert [(e["event_id"], e["rank"], e["point"]) for e in d["events"]] == [(12, None, None), (11, 2, 100), (10, 1, 5000)][1:]
    assert d["event_stats"]["wins"] == 1 and d["event_stats"]["best"] == 1
    assert [(o["key"], o["shared"]) for o in d["overlap"]] == [("bbb", 1)]
    assert explore.room_detail(db, "1")["key"] == "aaa"  # room_id でも開ける
    assert explore.room_detail(db, "zzz") == {"key": "zzz", "found": False}


def test_event_list(db):
    on = explore.event_list(db, "ongoing")
    assert [e["event_id"] for e in on["events"]] == [11]
    assert on["events"][0]["rooms"] == 2 and on["events"][0]["snapshots"] == 2 and on["events"][0]["leader"] == "ルームB"
    assert [e["event_id"] for e in explore.event_list(db, "upcoming")["events"]] == [12]
    ended = explore.event_list(db, "ended")["events"]
    assert [e["event_id"] for e in ended] == [10] and ended[0]["rankings"] == 2
    with pytest.raises(ValueError):
        explore.event_list(db, "bad")


def test_event_detail(db):
    d = explore.event_detail(db, 10)
    assert d["found"] and d["state"] == "ended" and d["room_total"] == 3
    assert [(r["name"], r["rank"], r["contributors"]) for r in d["rooms"]] == [("ルームA", 1, 2), ("ルームB", 2, 2), ("ルームC", 3, 0)]
    # 退会したユーザーも出す (印つき)。複数のルームに送った人は合計する
    assert [(c["user_id"], c["point"], c["rooms"], c["top_room"], c["left"]) for c in d["contributors"]] == [
        (7, 6500, 2, "ルームA", False), (8, 1000, 1, "ルームA", False), (9, 500, 1, "ルームB", True)]
    assert d["rankings"] == 2 and d["rankings_pending"] == 0  # ポイント 0 のルームは取らない
    p = explore.event_detail(db, "now_ev")["progress"]
    assert len(p["times"]) == 2 and [r["name"] for r in p["rooms"]] == ["ルームB", "ルームA"]
    assert p["rooms"][0]["points"] == [40, 200] and p["rooms"][0]["ranks"] == [2, 1]
    assert explore.event_detail(db, 999) == {"event_id": "999", "found": False}


def test_search_finds_former_names_recorded_gifters_and_known_rooms(db):
    from showroom_keeper.gifts import record_gift
    from showroom_keeper.listing import remember_rooms

    with Store(db) as s:
        s.set_cumulative(1, [{"user_id": 7, "name": "大口の新しい名前", "point": 90000, "visits": 40, "rank": 1}], NOW + 1)
    # 前の名前でも見つかる
    g = explore.search(db, "大口")["gifters"]
    assert g[0]["user_id"] == 7 and g[0]["match"] is None  # 今の名前にも「大口」が入っている
    g = {x["user_id"]: x for x in explore.search(db, "中堅")["gifters"]}
    assert g[8]["match"] is None
    with Store(db) as s:
        s.set_cumulative(1, [{"user_id": 8, "name": "まったく別", "point": 2000, "visits": 5, "rank": 2}], NOW + 2)
    g = {x["user_id"]: x for x in explore.search(db, "中堅")["gifters"]}
    assert g[8]["match"] == {"kind": "former", "name": "中堅"} and g[8]["name"] == "まったく別"
    # 記録したギフトにしか出ていない人も、名前で見つかる
    explore._recorded_names.clear()
    record_gift(db, "rec_room", 1, {"u": 55, "ac": "少額さん", "g": 1, "n": 1, "created_at": NOW})
    record_gift(db, "rec_room", 1, {"u": 55, "ac": "少額さん", "g": 1, "n": 1, "created_at": NOW + 5})
    g = explore.search(db, "少額")["gifters"]
    assert g[0]["user_id"] == 55 and g[0]["match"]["kind"] == "recorded" and g[0]["times"] == 2
    # 設定・記録だけにあるルームも見つかる
    remember_rooms(db, {"rec_room": ("記録だけのルーム", 999)})
    rooms = explore.search(db, "記録だけ")["rooms"]
    assert [(r["key"], r["source"]) for r in rooms] == [("rec_room", "known")]
    # 名前の分からないイベントは番号で見つかり、名前が無いと分かる
    with Store(db) as s:
        s.set_event_rooms(77, [{"room_id": 1, "point": 1, "rank": 1}], NOW)
    ev = explore.search(db, "77")["events"]
    assert ev[0]["event_id"] == 77 and ev[0]["named"] is False and ev[0]["name"] == "イベント 77"
