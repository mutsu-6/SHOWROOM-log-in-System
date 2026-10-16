import json
import time

import pytest

from showroom_keeper import listing, research
from showroom_keeper.config import Config, apply_settings, save_settings
from showroom_keeper.gifts import record_gift

NOW = time.time()


class FakeShowroom:
    """SHOWROOM の代わり。support (応援ルーム, room_id 1) と other (room_id 2) の 2 ルーム。"""

    def __init__(self):
        self.calls = []

    def __call__(self, path):
        self.calls.append(path)
        if path == "/api/room/status?room_url_key=support":
            return {"room_id": 1, "room_name": "応援ルーム"}
        if path == "/api/room/status?room_url_key=other":
            return {"room_id": 2, "room_name": "ほかのルーム"}
        if path.startswith("/api/live/summary_ranking?room_id=1"):
            return {"ranking": [{"user_id": 10, "name": "ファン", "point": 5000, "visit_count": 30, "rank": 1}]}
        if path.startswith("/api/live/summary_ranking?room_id=2"):
            return {"ranking": [{"user_id": 10, "name": "ファン", "point": 800, "visit_count": 3, "rank": 2},
                                {"user_id": 20, "name": "大口", "point": 90000, "visit_count": 50, "rank": 1}]}
        if path == "/api/rooms/1/past_events?page=1":
            return {"events": [{"event_id": 100, "event_name": "秋のイベント", "started_at": NOW - 9 * 86400,
                                "ended_at": NOW - 7 * 86400, "event_type": "quest", "show_ranking": 1}], "next_page": None}
        if path == "/api/rooms/2/past_events?page=1":
            return {"events": [{"event_id": 100, "event_name": "秋のイベント", "started_at": NOW - 9 * 86400,
                                "ended_at": NOW - 7 * 86400, "event_type": "quest", "show_ranking": 1},
                               {"event_id": 101, "event_name": "順位なし", "show_ranking": 0, "ended_at": NOW - 86400}],
                    "next_page": None}
        if path.startswith("/api/room/event_and_support?room_id=1"):
            return {"event": {"event_id": 200, "event_name": "開催中", "event_url": "https://www.showroom-live.com/event/now_ev",
                              "started_at": NOW - 86400, "ended_at": NOW + 86400, "show_ranking": 1}}
        if path.startswith("/api/room/event_and_support?room_id=2"):
            return {"event": None}
        if path == "/api/event/contribution_ranking?event_id=100&room_id=1":
            return {"ranking": [{"user_id": 10, "name": "ファン", "point": 3000, "rank": 1}]}
        if path == "/api/event/contribution_ranking?event_id=100&room_id=2":
            return {"ranking": [{"user_id": 20, "name": "大口", "point": 40000, "rank": 1},
                                {"user_id": 30, "name": "イベントだけ", "point": 700, "rank": 2}]}
        if path == "/api/event/contribution_ranking?event_id=200&room_id=1":
            return {"ranking": [{"user_id": 10, "name": "ファン", "point": 100, "rank": 1}]}
        raise AssertionError(f"想定していない問い合わせ: {path}")


@pytest.fixture
def collected(tmp_path, monkeypatch):
    monkeypatch.setattr(listing, "_names_cache", None)
    monkeypatch.setattr(listing, "_file_cache", {})
    monkeypatch.setattr(listing, "gift_catalog", lambda state_dir, rooms=None: {5: {"gift_name": "花", "point": 100, "free": False}})
    get = FakeShowroom()
    waits = []
    c = research.Collector(tmp_path, get=get, sleep=waits.append, gap=1.0)
    result = c.collect(["support", "other"])
    return tmp_path, get, waits, result


def test_collect_rooms_with_gaps_between_requests(collected):
    tmp_path, get, waits, result = collected
    assert result == {"rooms": 2, "events": 3, "requests": len(get.calls), "failures": 0}
    assert waits == [1.0] * (len(get.calls) - 1)  # 問い合わせの間は必ず空ける
    assert not any("event_id=101" in c for c in get.calls)  # 順位を公開していないイベントは取らない
    rooms = research.load_rooms(tmp_path)
    assert rooms["support"]["events"][0] == {"event_id": 200, "name": "開催中", "key": "now_ev", "start": NOW - 86400,
                                             "end": NOW + 86400, "type": None, "show_ranking": True, "current": True}
    assert rooms["other"]["cumulative"][1]["visits"] == 50


def test_finished_events_are_not_fetched_again(collected):
    tmp_path, get, _, _ = collected
    again = FakeShowroom()
    research.Collector(tmp_path, get=again, sleep=lambda s: None).collect(["support", "other"], force=True)
    assert not any("event_id=100" in c for c in again.calls)  # 終わったイベントは取り直さない
    assert any("event_id=200" in c for c in again.calls)  # 開催中のイベントは取り直す
    quiet = FakeShowroom()
    research.Collector(tmp_path, get=quiet, sleep=lambda s: None).collect(["support", "other"])
    assert quiet.calls == []  # 取ったばかりなら問い合わせない


def test_analyze_candidates_fans_and_similar_rooms(collected):
    tmp_path, _, _, _ = collected
    record_gift(tmp_path, "other", 9, {"t": 2, "u": 20, "ac": "大口", "g": 5, "n": 3, "created_at": NOW - 3600})
    r = research.analyze(tmp_path, ["support"])
    assert [c["user_id"] for c in r["candidates"]] == [20, 30]  # 応援ルームに送っていない人を、多い順に
    big = r["candidates"][0]
    assert big["other_points"] == 90000 and big["top_rooms"][0]["name"] == "ほかのルーム"
    assert big["event_count"] == 1 and big["best_rank"] == 1 and big["habits"]["times"] == 1
    assert big["sources"] == ["cumulative", "events", "recorded"]
    assert [f["user_id"] for f in r["fans"]] == [10] and r["fans"][0]["support_points"] == 5000
    assert r["similar"] == [{"key": "other", "name": "ほかのルーム", "gifters": 3, "shared": 1,
                             "overlap": round(1 / 3, 3), "candidates": 2}]


def test_gifter_detail_and_habits(collected):
    tmp_path, _, _, _ = collected
    for hours_ago in (1, 2, 26):
        record_gift(tmp_path, "other", 9, {"t": 2, "u": 20, "ac": "大口", "g": 5, "n": 1, "created_at": NOW - hours_ago * 3600})
    d = research.gifter_detail(tmp_path, "20", ["support"])
    assert d["found"] and [r["key"] for r in d["rooms"]] == ["other"]
    assert d["rooms"][0]["cumulative"] == 90000 and d["rooms"][0]["recorded"] == 300
    assert d["events"][0]["room_name"] == "ほかのルーム" and d["habits"]["times"] == 3 and d["habits"]["avg_point"] == 100
    assert sum(d["habits"]["hour"]) == 300 and len(d["habits"]["weekday"]) == 7
    assert research.gifter_detail(tmp_path, "999", [])["found"] is False
    with pytest.raises(ValueError):
        research.gifter_detail(tmp_path, "abc", [])


def test_job_runs_one_at_a_time(tmp_path):
    job = research.ResearchJob()

    class Slow:
        def collect(self, keys, force=False, progress=None):
            assert job.run(tmp_path, keys, "重ねて") is None  # 実行中は重ねて動かない
            return {"rooms": len(keys), "events": 0, "requests": 0, "failures": 0}

    assert job.run(tmp_path, ["a"], "テスト", collector=Slow())["rooms"] == 1
    assert job.last["reason"] == "テスト" and not job.running


def test_support_and_research_rooms_from_env_and_settings(tmp_path):
    env = {"SHOWROOM_SUPPORT_ROOMS": "https://www.showroom-live.com/r/sup", "SHOWROOM_RESEARCH_ROOMS": "a,b",
           "SHOWROOM_STATE_FILE": str(tmp_path / "s.json")}
    cfg = Config.load(None, env)
    assert (cfg.support_rooms, cfg.research_rooms) == (["sup"], ["a", "b"])
    # 前からある settings.json (応援ルームの項目が無い) は、.env の指定を消さない
    (tmp_path / "settings.json").write_text(json.dumps({"rooms": ["x"], "record_rooms": [], "events": []}), encoding="utf-8")
    assert apply_settings(cfg) and cfg.rooms == ["x"] and cfg.support_rooms == ["sup"]
    save_settings(cfg, {"rooms": ["x"], "support_rooms": ["new_sup"]})
    assert apply_settings(cfg) and cfg.support_rooms == ["new_sup"]
