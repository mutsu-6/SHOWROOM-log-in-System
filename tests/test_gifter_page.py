"""ギフターのページを、すぐ出す分 (main) とあとから読む重い分析 (more) に分けたこと (2026-11-14)。"""
from showroom_keeper import once, research, support_card
from showroom_keeper.research_store import Store

DAY = 86400
NOW = 1_790_000_000.0
HEAVY = ("states", "recorded_types", "involvement", "visits", "overlap")


def _fill(s):
    s.upsert_rooms([{"room_id": r, "key": f"k{r}", "name": f"ルーム{r}"} for r in (1, 2)], "event", 0)
    for eid, room, pt in ((1, 1, 500), (2, 2, 300)):
        end = NOW - (10 + eid) * DAY
        s.upsert_events([{"event_id": eid, "name": f"イベント{eid}", "start": end - 7 * DAY, "end": end}])
        s.set_event_rooms(eid, [{"room_id": room, "point": pt, "rank": 1}], end + DAY)
        s.set_contributions(eid, room, [{"user_id": 7, "name": "u7", "point": pt, "rank": 1}], end + DAY, final=True)
    s.conn.execute("UPDATE contributions SET guessed=1 WHERE event_id=2")  # イベント 2 は名前で推測した分


def test_main_comes_without_the_heavy_parts_and_more_has_them(tmp_path):
    with Store(tmp_path) as s:
        _fill(s)
    d = research.gifter_detail(tmp_path, 7, [])
    assert d["found"] and d["more"] and not any(k in d for k in HEAVY)
    assert d["summary"] and d["rooms"] and d["support"] is not None and "timeline" in d and "habits" in d
    # 名前で推測した印は、前と同じに出る
    assert {e["event_id"]: bool(e["guessed"]) for e in d["events"]} == {1: False, 2: True}
    assert d["guessed"] == {"events": 1, "points": 300}
    m = research.gifter_detail(tmp_path, 7, [], "more")
    assert m["found"] and all(k in m for k in HEAVY)
    assert research.gifter_detail(tmp_path, 7, [], "more") is m  # 使い回す (GIFTER_MORE_TTL 秒)
    once.forget(lambda k: k[0] == "gifter-more")


def test_support_card_leaves_out_guessed_rows(tmp_path):
    with Store(tmp_path) as s:
        _fill(s)
        out = support_card.by_room(s, 7, NOW)
    assert [r["room_id"] for r in out["rooms"]] == [1]  # 推測した回 (ルーム 2) は入れない
