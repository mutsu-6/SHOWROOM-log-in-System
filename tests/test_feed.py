import json
import time

from showroom_keeper import feed
from showroom_keeper.research_store import Store

NOW = time.time()
H = 3600


def midnight():
    return feed._midnights(NOW - 24 * H, NOW)[0]


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
        s.upsert_rooms([{"room_id": 3, "key": "k3", "name": "ルーム3"}, {"room_id": 4, "key": "k4", "name": "ルーム4"}], "archive", NOW)
        # 直近 24 時間のうちの 24:00 (日本時間) の 10 分前の推移: 4 ルームのうち上位 3 つを報告する
        s.upsert_events([{"event_id": 51, "name": "冬のイベント", "start": NOW - 5 * 86400, "end": NOW + 10 * H}])
        s.set_event_rooms(51, [{"room_id": r, "point": 1, "rank": r} for r in (1, 2, 3, 4)], NOW)
        s.add_progress(51, [{"room_id": r, "point": p, "rank": i + 1}
                            for i, (r, p) in enumerate([(3, 900), (1, 800), (4, 700), (2, 600)])], midnight() - 600)
        s.add_progress(51, [{"room_id": r, "point": p} for r, p in [(3, 400), (1, 800), (4, 100)]], midnight() - 86400 + 300)  # 前の日の 24:00
        s.conn.execute("INSERT INTO notices(notice_id, label, title, created_at) VALUES (1, 'お知らせ', '新しいギフト', ?)", (NOW - H,))
        s.conn.execute("INSERT INTO live_visits(user_id, at, room_key, n) VALUES (8, ?, 'k1', 3)", (int(NOW - H),))


def test_feed_lists_recent_happenings(tmp_path):
    setup(tmp_path)
    out = feed.build(tmp_path, ["k2"], now=NOW)
    kinds = [x["kind"] for x in out["items"]]
    assert out["items"] == sorted(out["items"], key=lambda x: -x["at"])
    big = [x for x in out["items"] if x["kind"] == "big_gift"]
    assert len(big) == 1  # 1 分後の続きは 1 つにまとめ、24 時間より前の分は出さない。無料の星は大きなギフトにしない
    assert (big[0]["point"], big[0]["times"], big[0]["name"], big[0]["room_name"]) == (30000, 2, "送る人", "ルーム1")
    assert big[0]["gifts"] == [{"name": "大きいギフト", "num": 3}]
    watch = next(x for x in out["items"] if x["kind"] == "watch_gift")
    assert (watch["user_id"], watch["points"], watch["paid_points"], watch["memo"], watch["level"]) == (8, 10, 0, "見ておく", "normal")
    assert any(x["kind"] == "watch_visit" and x["n"] == 3 for x in out["items"])
    assert not {"live_start", "live_end", "event_rank", "event_ending", "event_end", "showroom_notice"} & set(kinds)  # 要らないとされたもの
    daily = [x for x in out["items"] if x["kind"] == "event_daily" and x["event_id"] == 51]
    assert len(daily) == 1 and daily[0]["at"] == midnight() and daily[0]["snapshot_at"] == midnight() - 600
    assert [(r["room_name"], r["point"], r["rank"], r["gain"]) for r in daily[0]["top"]] == [
        ("ルーム3", 900, 1, 500), ("ルーム1", 800, 2, 0), ("ルーム4", 700, 3, 600)]
    assert (daily[0]["score"], daily[0]["rooms"]) == (500 + 600 + 600, 4)  # ルーム2 は前の日の値が無い (0 から)
    assert daily[0]["mine"] == [{"room_key": "k2", "room_name": "ルーム2", "point": 600, "rank": 4}]


def test_feed_daily_report_needs_a_snapshot_near_midnight(tmp_path):
    with Store(tmp_path) as s:
        s.upsert_rooms([{"room_id": 1, "key": "k1", "name": "ルーム1"}], "archive", NOW)
        s.upsert_events([{"event_id": 50, "name": "秋", "start": NOW - 5 * 86400, "end": NOW + 10 * H}])
        s.set_event_rooms(50, [{"room_id": 1, "point": 300, "rank": 1}], NOW)
        s.add_progress(50, [{"room_id": 1, "point": 100, "rank": 1}], midnight() - 3 * H)  # 24:00 から遠い推移しか無い
    assert feed.build(tmp_path, ["k1"], now=NOW)["items"] == []


def test_feed_daily_report_ranks_events_by_points_sent_that_day(tmp_path):
    """注目度 (その日に送られたポイント) の多い順に、1 日 DAILY_EVENTS 件。ポイントの合計が多くても、その日に増えていなければ出さない。"""
    m = midnight()
    with Store(tmp_path) as s:
        s.upsert_rooms([{"room_id": 1, "key": "k1", "name": "ルーム1"}], "archive", NOW)
        for eid in range(1, 9):
            s.upsert_events([{"event_id": eid, "name": f"イベント{eid}", "start": NOW - 5 * 86400, "end": NOW + 10 * H}])
            s.add_progress(eid, [{"room_id": 1, "point": 10**6}, {"room_id": 2, "point": 10}], m - 86400)
            s.add_progress(eid, [{"room_id": 1, "point": 10**6 + eid * 100}, {"room_id": 2, "point": 10 + eid}], m)
        s.add_progress(8, [{"room_id": 1, "point": 10**6}, {"room_id": 2, "point": 10}], m)  # 合計は多いが、その日は増えていない
        # その日に始まったイベント: 前の日の値が無くても、0 から数える
        s.upsert_events([{"event_id": 20, "name": "新しいイベント", "start": m - 10 * H, "end": NOW + 10 * H}])
        s.add_progress(20, [{"room_id": 3, "point": 5000}], m)
        # 前の日の 24:00 の値が無く、前から開いているイベント: その日のいちばん早い値から数える
        s.upsert_events([{"event_id": 21, "name": "途中から", "start": NOW - 5 * 86400, "end": NOW + 10 * H}])
        s.add_progress(21, [{"room_id": 3, "point": 10**7}], m - 5 * H)
        s.add_progress(21, [{"room_id": 3, "point": 10**7 + 650}], m)
    items = [x for x in feed.build(tmp_path, ["k1"], now=NOW)["items"] if x["kind"] == "event_daily"]
    assert [(x["event_id"], x["place"], x["score"]) for x in items] == [(20, 1, 5000), (7, 2, 707), (21, 3, 650), (6, 4, 606), (5, 5, 505)]
    assert items[1]["mine"] == [{"room_key": "k1", "room_name": "ルーム1", "point": 10**6 + 700, "rank": None}]


def test_feed_threshold_and_period(tmp_path):
    setup(tmp_path)
    assert not [x for x in feed.build(tmp_path, [], min_point=50000, now=NOW)["items"] if x["kind"] == "big_gift"]
    week = feed.build(tmp_path, [], hours=48, now=NOW)["items"]
    assert len([x for x in week if x["kind"] == "big_gift"]) == 2
    assert feed.build(tmp_path, [], hours=10**6, now=NOW)["hours"] == feed.MAX_HOURS


def test_feed_without_data(tmp_path):
    assert feed.build(tmp_path, ["none"], now=NOW)["items"] == []
