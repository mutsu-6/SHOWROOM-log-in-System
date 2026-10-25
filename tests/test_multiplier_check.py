import json

from showroom_keeper import multiplier_check as mc
from showroom_keeper.research_store import Store

DAY = 86400
T = 1_760_000_000  # 2025 年 (2.5 倍のあと)
OLD = 1_500_000_000  # 2017 年 (2.5 倍の前)


def _fill(tmp_path):
    (tmp_path / "cache").mkdir()
    (tmp_path / "cache" / "gift_catalog.json").write_text(json.dumps({"gifts": {
        "1": {"name": "有料", "point": 100, "free": False}, "2": {"name": "星", "point": 1, "free": True}}}), encoding="utf-8")
    with Store(tmp_path) as s:
        s.upsert_rooms([{"room_id": 1, "key": "k1", "name": "ルーム1"}], "live", T)
        for eid, start, rate in ((10, T, 2.5), (11, T + 10 * DAY, 1.0), (12, OLD, 1.0)):
            s.upsert_events([{"event_id": eid, "name": f"イベント{eid}", "start": start, "end": start + 5 * DAY}])
            s.set_event_rooms(eid, [{"room_id": 1, "point": 10_000, "rank": 1}], start + 6 * DAY)
            s.set_contributions(eid, 1, [{"user_id": u, "name": f"u{u}", "point": int(1000 * rate), "rank": i + 1}
                                         for i, u in enumerate(range(100, 106))], start + 6 * DAY, final=True)
            with s.tx() as c:
                c.executemany("INSERT INTO gift_events VALUES (?, ?, ?, ?, ?)",
                              [(u, start + DAY, "k1", 1, 10) for u in range(100, 106)])  # 1 人 1,000 pt (元 pt)
                c.execute("INSERT INTO gift_events VALUES (?, ?, ?, ?, ?)", (105, start + DAY, "k1", 2, 5))  # 無料も送った人
                c.execute("INSERT INTO gift_events VALUES (?, ?, ?, ?, ?)", (104, start + DAY, "k1", 99, 1))  # カタログに無い
        with s.tx() as c:
            c.execute("INSERT INTO gift_event_pos(room_key, pos, size, at) VALUES ('k1', 0, 0, ?)", (T,))


def test_events_are_sorted_by_the_multiplier_they_seem_to_have(tmp_path):
    _fill(tmp_path)
    before = (tmp_path / "research.db").read_bytes()
    out = mc.run(tmp_path)
    assert (tmp_path / "research.db").read_bytes() == before  # 写しは書き換えない
    ev = {e["event_id"]: e for e in out["events"]}
    assert ev[10]["class"] == "unknown" and ev[10]["users"] == 4  # 無料・カタログに無い人を除くと 4 人 (5 人に満たない)
    assert ev[10]["median_ratio"] == 2.5 and ev[10]["dropped"] == {"free_gift": 1, "unknown_gift": 1}
    assert ev[11]["median_ratio"] == 1.0 and ev[12]["before_2_5"] and not ev[10]["before_2_5"]
    assert out["summary"]["since_2_5"]["events"] == 2 and out["summary"]["before_2_5"]["events"] == 1
    assert "user_id" not in json.dumps(out) and "u100" not in json.dumps(out)  # 名前・ID を出さない


def test_class_needs_enough_people():
    assert mc._class(2.6, mc.MIN_USERS) == "with_2_5" and mc._class(1.1, mc.MIN_USERS) == "without"
    assert mc._class(2.6, mc.MIN_USERS - 1) == "unknown" and mc._class(5.0, 50) == "unknown"
