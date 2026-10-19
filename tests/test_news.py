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
