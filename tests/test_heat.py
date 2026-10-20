import time
from datetime import datetime

from showroom_keeper import heat
from showroom_keeper.research_store import Store
from showroom_keeper.status import JST

H = 3600
NOW = time.time()


def test_slots_start_on_monday_3am_every_12_hours():
    t = heat.slot_start(heat.slot_of(NOW))
    assert t <= NOW < t + heat.SLOT
    d = datetime.fromtimestamp(heat.slot_start(heat.WEEK_SLOTS * 3000), JST)
    assert (d.weekday(), d.hour, d.minute) == (0, 3, 0)  # 週の 0 番目は月曜 3:00
    assert datetime.fromtimestamp(heat.slot_start(heat.WEEK_SLOTS * 3000 + 1), JST).hour == 15


def _event(s, eid, start, end):
    s.upsert_events([{"event_id": eid, "name": f"イベント{eid}", "start": start, "end": end}])
    s.conn.execute("UPDATE events SET progress_at=? WHERE event_id=?", (NOW, eid))


def test_slot_points_are_the_gain_inside_the_12_hours(tmp_path):
    i = heat.slot_of(NOW) - 3
    t0, t1 = heat.slot_start(i), heat.slot_start(i + 1)
    with Store(tmp_path) as s:
        _event(s, 1, t0 - 86400, t1 + 86400)  # 前から開いている: 区切りの始まりまでの最後の値から数える
        s.add_progress(1, [{"room_id": 1, "point": 100}, {"room_id": 2, "point": 50}], t0 - 1800)
        s.add_progress(1, [{"room_id": 1, "point": 400}, {"room_id": 2, "point": 40}], t1 - 600)  # 2 は減った (0 にする)
        s.add_progress(1, [{"room_id": 1, "point": 9999}], t1 + 600)  # 区切りの後: 数えない
        _event(s, 2, t0 + 2 * H, t1 + 86400)  # 区切りの中で始まった: 0 から
        s.add_progress(2, [{"room_id": 1, "point": 30}], t0 + 3 * H)
        _event(s, 3, t0 - 86400, t0 + 5 * H)  # 区切りの中で終わった: 終わった時の値まで
        s.add_progress(3, [{"room_id": 3, "point": 10}], t0 - 600)
        s.add_progress(3, [{"room_id": 3, "point": 70}], t0 + 5 * H)
        _event(s, 4, t0 - 86400, t1 + 86400)  # 途中から集め始めた: 始まりの値が分からないので数えない
        s.add_progress(4, [{"room_id": 4, "point": 10**6}], t0 + H)
        assert heat.build_slot(s, i) == 2
        got = {r[0]: (r[1], r[2]) for r in s.all("SELECT room_id, point, events FROM room_point_12h WHERE slot=?", (i,))}
    assert got == {1: (300 + 30, 2), 3: (60, 1)}


def test_step_builds_finished_slots_once(tmp_path):
    with Store(tmp_path) as s:
        built = 0
        while heat.step(s, NOW):
            built += 1
        assert built == heat.BACKFILL_DAYS * 2 - 1 or built == heat.BACKFILL_DAYS * 2
        assert not heat.step(s, NOW)
        last = s.meta("heat_next") - 1
        assert heat.slot_start(last + 1) + heat.READY_AFTER <= NOW


def test_room_heatmap_compares_with_the_same_slot_last_week(tmp_path):
    i = heat.slot_of(NOW) - 2
    with Store(tmp_path) as s:
        s.upsert_rooms([{"room_id": r, "key": f"k{r}", "name": f"ルーム{r}"} for r in (1, 2, 3)], "archive", NOW)
        s.conn.execute("INSERT INTO room_profile(room_id, type) VALUES (1, 'バランス型'), (2, 'バランス型')")
        s.conn.executemany("INSERT INTO room_point_12h(slot, room_id, point, events) VALUES (?, ?, ?, 1)", [
            (i, 1, 200), (i - heat.WEEK_SLOTS, 1, 100),  # 先週の同じ区切りの 2 倍
            (i - heat.WEEK_SLOTS, 2, 50),  # 今は 0 (落ちた。大きさは前の値)
            (i, 3, 10), (i - 1, 3, 5)])  # 先週は無い (新しく)
    out = heat.rooms(tmp_path)
    assert out["slot"] == i and out["compare"] == "week"
    groups = {g["name"]: {x["room_id"]: (x["size"], x["change"]) for x in g["items"]} for g in out["groups"]}
    assert groups == {"バランス型": {1: (200, 1.0), 2: (50, -1.0)}, "タイプ未判定": {3: (10, None)}}
    prev = heat.rooms(tmp_path, compare="prev")
    assert {x["room_id"]: x["change"] for g in prev["groups"] for x in g["items"]}[3] == 1.0  # すぐ前の 12 時間と比べる
    week = heat.rooms(tmp_path, span="week")
    assert {x["room_id"]: x["now"] for g in week["groups"] for x in g["items"]}[3] == 15


def test_gifter_heatmap_groups_by_the_favorite_room(tmp_path):
    with Store(tmp_path) as s:
        s.upsert_rooms([{"room_id": 1, "key": "k1", "name": "推しのルーム"}], "archive", NOW)
        s.conn.executemany("INSERT INTO user_month(user_id, month, points) VALUES (?, ?, ?)", [
            (10, "2026-08", 1000), (10, "2026-07", 500), (11, "2026-07", 300), (12, "2026-08", 50)])
        s.conn.execute("INSERT INTO gifter_status(user_id, top_room, state) VALUES (10, 1, '上り調子'), (11, 1, '離脱')")
    out = heat.gifters(tmp_path, "2026-08")
    assert (out["month"], out["prev"]) == ("2026-08", "2026-07")
    groups = {g["name"]: {x["user_id"]: (x["size"], x["change"]) for x in g["items"]} for g in out["groups"]}
    assert groups == {"推しのルーム": {10: (1000, 1.0), 11: (300, -1.0)}, "最推しのルームが分からない": {12: (50, None)}}
    assert out["groups"][0]["room_key"] == "k1"
