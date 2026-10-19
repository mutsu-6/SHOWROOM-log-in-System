import json
import time

from showroom_keeper import feed
from showroom_keeper.research_store import Store

NOW = time.time()
H = 3600


def setup(tmp_path):
    """記録しているルーム k1 (room_id 1) と応援ルーム k2 (room_id 2)。ギフトの一覧は 1 = 有料 10,000 pt、2 = 無料の星 1 pt。"""
    (tmp_path / "cache").mkdir()
    (tmp_path / "cache" / "gift_catalog.json").write_text(json.dumps({"gifts": {
        "1": {"gift_name": "大きいギフト", "point": 10000, "free": False},
        "2": {"gift_name": "星", "point": 1, "free": True}}}), encoding="utf-8")
    (tmp_path / "gifts").mkdir()
    lines = [{"at": NOW - 2 * H, "user_id": 7, "name": "送る人", "gift_id": 1, "num": 1},
             {"at": NOW - 2 * H + 60, "user_id": 7, "name": "送る人", "gift_id": 1, "num": 2},  # 1 分後の続き: まとめる
             {"at": NOW - 1 * H, "user_id": 8, "name": "星の人", "gift_id": 2, "num": 10},
             {"at": NOW - 30 * H, "user_id": 7, "name": "送る人", "gift_id": 1, "num": 1}]  # 24 時間より前
    (tmp_path / "gifts" / "k1.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8")
    with Store(tmp_path) as s:
        s.upsert_rooms([{"room_id": 1, "key": "k1", "name": "ルーム1"}, {"room_id": 2, "key": "k2", "name": "ルーム2"}], "archive", NOW)
        s.set_watch(8, True, "見ておく", NOW)
        s.add_room_lives([{"room_id": 2, "started_at": int(NOW - 5 * H), "followers": 10}], NOW - 3 * H)  # 3 時間前に終わった
        s.upsert_events([{"event_id": 50, "name": "秋のイベント", "start": NOW - 5 * 86400, "end": NOW + 10 * H}])
        s.set_event_rooms(50, [{"room_id": 2, "point": 300, "rank": 2}], NOW)
        s.add_progress(50, [{"room_id": 2, "point": 100, "rank": 4}], NOW - 4 * H)
        s.add_progress(50, [{"room_id": 2, "point": 300, "rank": 2}], NOW - 3 * H)
        s.conn.execute("INSERT INTO notices(notice_id, label, title, created_at) VALUES (1, 'お知らせ', '新しいギフト', ?)", (NOW - H,))
        s.conn.execute("INSERT INTO live_visits(user_id, at, room_key, n) VALUES (8, ?, 'k1', 3)", (int(NOW - H),))


def test_feed_lists_recent_happenings(tmp_path):
    setup(tmp_path)
    out = feed.build(tmp_path, ["k2"], ["k1"], now=NOW)
    kinds = [x["kind"] for x in out["items"]]
    assert out["items"] == sorted(out["items"], key=lambda x: -x["at"])
    big = [x for x in out["items"] if x["kind"] == "big_gift"]
    assert len(big) == 1  # 1 分後の続きは 1 つにまとめ、24 時間より前の分は出さない。無料の星は大きなギフトにしない
    assert (big[0]["point"], big[0]["times"], big[0]["name"], big[0]["room_name"]) == (30000, 2, "送る人", "ルーム1")
    assert big[0]["gifts"] == [{"name": "大きいギフト", "num": 3}]
    watch = next(x for x in out["items"] if x["kind"] == "watch_gift")
    assert (watch["user_id"], watch["points"], watch["paid_points"], watch["memo"], watch["level"]) == (8, 10, 0, "見ておく", "normal")
    assert any(x["kind"] == "watch_visit" and x["n"] == 3 for x in out["items"])
    assert {"live_start", "live_end", "event_rank", "event_ending", "showroom_notice"} <= set(kinds)
    rank = next(x for x in out["items"] if x["kind"] == "event_rank")
    assert (rank["before"], rank["after"], rank["level"]) == (4, 2, "high")


def test_feed_threshold_and_period(tmp_path):
    setup(tmp_path)
    assert not [x for x in feed.build(tmp_path, [], [], min_point=50000, now=NOW)["items"] if x["kind"] == "big_gift"]
    week = feed.build(tmp_path, [], [], hours=48, now=NOW)["items"]
    assert len([x for x in week if x["kind"] == "big_gift"]) == 2
    assert feed.build(tmp_path, [], [], hours=10**6, now=NOW)["hours"] == feed.MAX_HOURS


def test_feed_without_data(tmp_path):
    assert feed.build(tmp_path, ["none"], ["none"], now=NOW)["items"] == []
