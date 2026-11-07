import json
import time

from showroom_keeper import news
from showroom_keeper.research_store import Store

NOW = time.time()
H, DAY = 3600, 86400


def setup(tmp_path):
    """2 日前に終わったイベント 50 (ルーム 1〜5)。ルーム 2 が 24 時間前の 4 位から 1 位に。1 位と 2 位は 1,000 pt 差。"""
    end = NOW - 2 * DAY
    with Store(tmp_path) as s:
        s.upsert_rooms([{"room_id": r, "key": f"k{r}", "name": f"ルーム{r}"} for r in range(1, 6)], "archive", NOW)
        s.upsert_events([{"event_id": 50, "name": "秋のイベント", "start": end - 7 * DAY, "end": end},
                         {"event_id": 60, "name": "前のイベント", "start": NOW - 60 * DAY, "end": NOW - 50 * DAY}])
        s.set_event_rooms(50, [{"room_id": 2, "point": 501_000, "rank": 1}, {"room_id": 1, "point": 500_000, "rank": 2},
                               *({"room_id": r, "point": 1000 * r, "rank": 6 - r + 2} for r in (3, 4, 5))], NOW)
        s.set_contributions(50, 2, [{"user_id": 7, "point": 300_000, "rank": 1}, {"user_id": 8, "point": 1000, "rank": 2}], NOW, final=True)
        s.set_contributions(60, 2, [{"user_id": 8, "point": 50, "rank": 1}], NOW, final=True)
        s.add_progress(50, [{"room_id": 2, "point": 100_000, "rank": 4}], end - DAY - H)
        s.add_final(50, [{"room_id": 2, "point": 400_000, "rank": 2}, {"room_id": 1, "point": 450_000, "rank": 1}], end - 40)
        s.add_final(50, [{"room_id": 2, "point": 501_000, "rank": 1}, {"room_id": 1, "point": 500_000, "rank": 2}], end - 2)
        s.conn.execute("INSERT INTO users(user_id, name) VALUES (7, '大きい人') ON CONFLICT(user_id) DO UPDATE SET name=excluded.name")


def topic(out, key):
    return next(t for t in out["topics"] if t["key"] == key)["items"]


def test_news_picks_flashy_topics(tmp_path):
    setup(tmp_path)
    out = news.build(tmp_path, 7, now=NOW)
    fan = topic(out, "top_fans")[0]
    assert (fan["user_id"], fan["name"], fan["point"], fan["room_name"], fan["event_name"]) == (7, "大きい人", 300_000, "ルーム2", "秋のイベント")
    assert topic(out, "big_rooms")[0]["room_id"] == 2
    race = topic(out, "close_races")[0]
    assert (race["gap"], race["first"]["room_id"], race["second"]["room_id"]) == (1000, 2, 1)
    back = topic(out, "comebacks")[0]
    assert (back["room_id"], back["before"]) == (2, 4)
    shot = topic(out, "last_shots")[0]
    assert (shot["room_id"], shot["jump"], shot["before"], shot["after"]) == (2, 101_000, 2, 1)
    with Store(tmp_path) as s:  # 月ごとの集計 (貢献ランキングを集め終わった月から作られる) を直に入れる
        s.conn.executemany("INSERT OR REPLACE INTO user_month(user_id, month, points) VALUES (?, ?, ?)",
                           [(7, news._month(NOW), 300_000), (8, news._month(NOW), 1000), (8, "2000-01", 50)])
    out = news.build(tmp_path, 7, now=NOW, use_cache=False)
    assert [x["user_id"] for x in topic(out, "new_stars")] == [7]  # 8 は前の月にも出ている
    assert topic(out, "top_month")[0]["user_id"] == 7


def test_last_shots_keep_older_events_and_a_zero_second_jump(tmp_path):
    """選んだ日数より前の一気投げも残し、終わる時刻以降の増えは 0 秒前になる。画面は新しい順に 30 件。"""
    old_end = NOW - 40 * DAY
    new_end = NOW - 2 * H
    with Store(tmp_path) as s:
        s.upsert_rooms([{"room_id": 1, "key": "k1", "name": "ルーム1"}], "archive", NOW)
        s.upsert_events([
            {"event_id": 1, "name": "古い回", "start": old_end - 7 * DAY, "end": old_end},
            {"event_id": 2, "name": "新しい回", "start": new_end - DAY, "end": new_end},
        ])
        s.add_final(1, [{"room_id": 1, "point": 100, "rank": 2}], old_end - 30)
        s.add_final(1, [{"room_id": 1, "point": 180, "rank": 1}], old_end + 10)  # 終了のあと 10 秒 → 0 秒前
        s.add_final(2, [{"room_id": 1, "point": 10, "rank": 1}], new_end - 20)
        s.add_final(2, [{"room_id": 1, "point": 15, "rank": 1}], new_end - 5)
    shots = topic(news.build(tmp_path, 1, now=NOW, use_cache=False), "last_shots")
    assert [x["event_id"] for x in shots] == [2, 1]
    assert shots[1]["left"] == 0 and shots[1]["jump"] == 80
    assert shots[0]["left"] == 5 and shots[0]["jump"] == 5
    with Store(tmp_path) as s:
        assert s.scalar("SELECT COUNT(*) FROM last_shots WHERE jump > 0") == 2


def test_last_shots_show_the_newest_thirty_and_keep_the_rest(tmp_path):
    with Store(tmp_path) as s:
        s.upsert_rooms([{"room_id": 1, "key": "k1", "name": "ルーム1"}], "archive", NOW)
        for i in range(1, 32):
            end = NOW - (40 - i) * H  # 全部、1 日より前に終わっている
            s.upsert_events([{"event_id": i, "name": f"回{i}", "start": end - DAY, "end": end}])
            s.add_final(i, [{"room_id": 1, "point": 1, "rank": 1}], end - 10)
            s.add_final(i, [{"room_id": 1, "point": 1 + i, "rank": 1}], end - 2)
    news.build(tmp_path, 1, now=NOW, use_cache=False)  # 開いた時に一部を残す
    with Store(tmp_path) as s:  # 残りは収集が少しずつ残す
        while news.remember_pending(s, NOW):
            pass
    shots = topic(news.build(tmp_path, 1, now=NOW, use_cache=False), "last_shots")
    assert len(shots) == 30 and shots[0]["event_id"] == 31 and shots[-1]["event_id"] == 2
    with Store(tmp_path) as s:
        assert s.scalar("SELECT COUNT(*) FROM last_shots WHERE jump > 0") == 31


def test_sweep_step_saves_a_closed_last_shot(tmp_path):
    from showroom_keeper import research, sweep

    end = NOW - 2000
    with Store(tmp_path) as s:
        s.upsert_rooms([{"room_id": 1, "key": "k1", "name": "ルーム1"}], "archive", NOW)
        s.upsert_events([{"event_id": 3, "name": "終わった", "start": end - DAY, "end": end}])
        s.add_final(3, [{"room_id": 1, "point": 10, "rank": 1}], end - 20)
        s.add_final(3, [{"room_id": 1, "point": 50, "rank": 1}], end)
        s.set_meta("live_at", NOW)
        s.set_meta("events_at", NOW)
    sw = sweep.Sweeper(tmp_path, collector=research.Collector(
        tmp_path, get=lambda path: {}, throttle=research.Throttle(0.0, sleep=lambda x: None)), scan=False, clock=lambda: NOW)
    sw.step()
    with Store(tmp_path) as s:
        row = s.one("SELECT jump, left_sec FROM last_shots WHERE event_id=3")
    assert row["jump"] == 40 and row["left_sec"] == 0


def test_news_period_and_empty(tmp_path):
    setup(tmp_path)
    assert topic(news.build(tmp_path, 1, now=NOW), "top_fans") == []  # 2 日前に終わったので、1 日には入らない
    assert news.build(tmp_path, 999, now=NOW)["days"] == 31
    empty = news.build(tmp_path / "none", 7, now=NOW)
    assert all(t["items"] == [] for t in empty["topics"])


def test_news_big_gifts(tmp_path):
    (tmp_path / "cache").mkdir()
    (tmp_path / "cache" / "gift_catalog.json").write_text(json.dumps({"gifts": {"1": {"gift_name": "大", "point": 10000, "free": False}}}))
    (tmp_path / "gifts").mkdir()
    (tmp_path / "gifts" / "k1.jsonl").write_text(json.dumps({"at": NOW - H, "user_id": 7, "name": "大きい人", "gift_id": 1, "num": 5}) + "\n")
    big = topic(news.build(tmp_path, 7, now=NOW), "big_gifts")
    assert (big[0]["point"], big[0]["user_id"]) == (50000, 7)


def test_close_races_skip_rank_and_point_that_disagree(tmp_path):
    setup(tmp_path)
    with Store(tmp_path) as s:  # 前に読んだ時の 1 位の行が残り、今の 2 位の方が pt が多い (別の時に読んだ値が並んだ)
        s.upsert_events([{"event_id": 70, "name": "おかしい回", "start": NOW - 9 * DAY, "end": NOW - 3 * DAY}])
        s.set_event_rooms(70, [{"room_id": 3, "point": 154_007, "rank": 1}, {"room_id": 4, "point": 333_968, "rank": 2}], NOW)
        s.upsert_events([{"event_id": 80, "name": "1 位が 2 つ", "start": NOW - 9 * DAY, "end": NOW - 3 * DAY}])
        s.set_event_rooms(80, [{"room_id": 3, "point": 300_000, "rank": 1}, {"room_id": 4, "point": 290_000, "rank": 1},
                               {"room_id": 5, "point": 280_000, "rank": 2}], NOW)
        s.upsert_events([{"event_id": 90, "name": "2 位の pt が無い", "start": NOW - 9 * DAY, "end": NOW - 3 * DAY}])
        s.set_event_rooms(90, [{"room_id": 3, "point": 300_000, "rank": 1}, {"room_id": 4, "point": None, "rank": 2}], NOW)
    out = news.build(tmp_path, 7, now=NOW)
    races = next(t for t in out["topics"] if t["key"] == "close_races")
    assert [x["event_id"] for x in races["items"]] == [50]  # 合わない回は載せない
    assert races["skipped"] == 3  # 数だけ出す
    with Store(tmp_path) as s:  # 行は消さず、直しもしない
        assert s.scalar("SELECT point FROM room_events WHERE event_id=70 AND room_id=3") == 154_007


def test_close_races_report_the_skipped_count_even_when_nothing_is_shown(tmp_path):
    """載せる接戦が 1 つも無くても、載せなかった数は出す (画面は skipped があれば話題を見せる)。"""
    with Store(tmp_path) as s:
        s.upsert_rooms([{"room_id": r, "key": f"k{r}", "name": f"ルーム{r}"} for r in (3, 4)], "archive", NOW)
        s.upsert_events([{"event_id": 70, "name": "おかしい回", "start": NOW - 9 * DAY, "end": NOW - 3 * DAY}])
        s.set_event_rooms(70, [{"room_id": 3, "point": 154_007, "rank": 1}, {"room_id": 4, "point": 333_968, "rank": 2}], NOW)
    out = news.build(tmp_path, 7, now=NOW, use_cache=False)
    races = next(t for t in out["topics"] if t["key"] == "close_races")
    assert races["items"] == [] and races["skipped"] == 1
