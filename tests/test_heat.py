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
        s.add_room_lives([{"room_id": 1, "started_at": int(NOW - 9 * H), "genre": "モデル"},
                          {"room_id": 1, "started_at": int(NOW - 3 * H), "genre": "アイドル"},  # 最後の配信のジャンル
                          {"room_id": 2, "started_at": int(NOW - 5 * H), "genre": "芸人"}], NOW)
        s.conn.executemany("INSERT INTO room_point_12h(slot, room_id, point, events) VALUES (?, ?, ?, 1)", [
            (i, 1, 200), (i - heat.WEEK_SLOTS, 1, 100),  # 先週の同じ区切りの 2 倍
            (i - heat.WEEK_SLOTS, 2, 50),  # 今は 0 (落ちた。大きさは前の値)
            (i, 3, 10), (i - 1, 3, 5)])  # 先週は無い (新しく)
    out = heat.rooms(tmp_path)
    assert out["slot"] == i and out["compare"] == "week" and out["group"] == "genre"
    groups = {g["name"]: {x["room_id"]: (x["size"], x["change"]) for x in g["items"]} for g in out["groups"]}
    assert groups == {"アイドル": {1: (200, 1.0)}, "芸人": {2: (50, -1.0)}, "ジャンル未判定": {3: (10, None)}}
    by_type = {g["name"]: sorted(x["room_id"] for x in g["items"]) for g in heat.rooms(tmp_path, group="type")["groups"]}
    assert by_type == {"バランス型": [1, 2], "タイプ未判定": [3]}
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


def _status(s, uid, state, fire, hp, mp, rooms):
    import json

    from showroom_keeper import gifter_status as gs

    s.conn.execute("INSERT INTO gifter_status(user_id, version, state, data) VALUES (?, ?, ?, ?)",
                   (uid, gs.VERSION, state, json.dumps({"state": state, "fire": fire, "hp": hp, "mp": mp, "rooms": rooms})))


def test_room_power_adds_up_fighters_and_their_ammo(tmp_path):
    import json

    from showroom_keeper import gifter_status as gs

    with Store(tmp_path) as s:
        s.upsert_rooms([{"room_id": r, "key": f"k{r}", "name": f"ルーム{r}"} for r in (1, 2)], "archive", NOW)
        s.conn.execute("INSERT INTO users(user_id, name) VALUES (10, '主砲の人')")
        _status(s, 10, "安定", 1000, 100, 25, [{"room_id": 1, "share": 0.8, "sortie": 1.0, "class": "主砲"},
                                              {"room_id": 2, "share": 0.2, "sortie": None, "class": "付き合い"}])  # 付き合いは数えない
        _status(s, 11, "枯渇", 1000, 100, 100, [{"room_id": 1, "share": 1.0, "sortie": None, "class": "支え"}])  # 枯渇 0.2・出動率 0.5
        _status(s, 12, "離脱", 9999, 100, 100, [{"room_id": 1, "share": 1.0, "sortie": 1.0, "class": "主力"}])  # 離脱は 0
        assert not heat.power_due(s)  # ステータスをまだ作り終えていない
        s.set_meta(gs.RUN_META, json.dumps({"done": NOW}))
        assert heat.power_due(s)
        assert heat.build_power(s, NOW) == 1
        assert not heat.power_due(s)
        r = s.one("SELECT * FROM room_power WHERE room_id=1")
        assert (r["power"], r["left_pt"], r["people"], r["guns"], r["mains"]) == (800 + 100, 200 + 100, 2, 1, 0)
    out = heat.room_power(tmp_path, "k1")
    assert out["found"] and out["rank"] == 1
    assert [(g["user_id"], g["power"], g["left"], g["class"], g["state"]) for g in out["gifters"]] == [
        (10, 800, 0.25, "主砲", "安定"), (11, 100, 1.0, "支え", "枯渇")]
    assert out["gifters"][0]["name"] == "主砲の人"
    assert not heat.room_power(tmp_path, "k2")["found"]


def test_room_heatmap_by_power_and_league(tmp_path):
    i = heat.slot_of(NOW) - 2
    with Store(tmp_path) as s:
        s.upsert_rooms([{"room_id": r, "key": f"k{r}", "name": f"ルーム{r}"} for r in (1, 2, 3)], "archive", NOW)
        s.conn.executemany("INSERT INTO room_point_12h(slot, room_id, point, events) VALUES (?, ?, ?, 1)", [(i, 1, 200), (i, 3, 10)])
        s.conn.executemany("INSERT INTO room_power(room_id, at, power, left_pt, people, guns, mains, data) VALUES (?, ?, ?, ?, 1, 1, 0, '[]')",
                           [(1, NOW, 1000, 250), (2, NOW, 500, 500)])
        s.conn.executemany("INSERT INTO room_info(room_id, league_id) VALUES (?, ?)", [(1, 9), (2, 30)])
    out = heat.rooms(tmp_path, size="power", group="league")
    assert out["size"] == "power" and out["group"] == "league"
    got = {g["name"]: {x["room_id"]: (x["size"], x["now"], x["left_pt"]) for x in g["items"]} for g in out["groups"]}
    assert got == {"SS": {1: (1000, 200, 250)}, "B": {2: (500, 0, 500)}}  # 地力の無いルーム 3 は出ない
    assert {g["name"]: (g["power"], g["left_pt"]) for g in out["groups"]} == {"SS": (1000, 250), "B": (500, 500)}
    # ポイントの大きさのままでも、地力と残弾が付く (色を残弾にする時)
    pt = {x["room_id"]: (x["size"], x["power"]) for g in heat.rooms(tmp_path)["groups"] for x in g["items"]}
    assert pt == {1: (200, 1000), 3: (10, 0)}


def test_power_step_builds_a_little_at_a_time_when_idle(tmp_path, monkeypatch):
    import json

    from showroom_keeper import gifter_status as gs, room_parts

    monkeypatch.setattr(heat, "CHUNK", 2)
    with Store(tmp_path) as s:
        for u in range(5):
            _status(s, u + 1, "安定", 100, 10, 10, [{"room_id": 1, "share": 1.0, "sortie": 1.0, "class": "支え"}])
        s.set_meta(gs.RUN_META, json.dumps({"done": NOW}))
        monkeypatch.setattr(room_parts, "idle", lambda now=None: False)
        assert not heat.power_step(s, NOW)  # 暇でなければ何もしない
        monkeypatch.setattr(room_parts, "idle", lambda now=None: True)
        steps = 0
        while heat.power_step(s, NOW):
            steps += 1
        assert steps == 3  # 2 人・2 人・1 人 (足し終えて保存)
        assert s.scalar("SELECT people FROM room_power WHERE room_id=1") == 5
