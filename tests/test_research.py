import json
import time
import urllib.error

import pytest

from showroom_keeper import listing, research, sweep
from showroom_keeper.config import Config, apply_settings, save_settings
from showroom_keeper.gifts import record_gift
from showroom_keeper.research_store import Store

NOW = time.time()


def http_error(code):
    return urllib.error.HTTPError("https://www.showroom-live.com/api", code, "error", {}, None)


class FakeTime:
    """sleep すると進む時計 (問い合わせの間隔を確かめる)。"""

    def __init__(self):
        self.now = 0.0
        self.waits = []

    def sleep(self, s):
        self.waits.append(s)
        self.now += s

    def clock(self):
        return self.now


def throttle(gap=1.0):
    t = FakeTime()
    return research.Throttle(gap, sleep=t.sleep, clock=t.clock), t


class FakeShowroom:
    """SHOWROOM の代わり。support (応援ルーム, room_id 1) と other (room_id 2) の 2 ルーム。"""

    def __init__(self, fail=()):
        self.calls = []
        self.fail = set(fail)

    def __call__(self, path):
        self.calls.append(path)
        if path in self.fail:
            raise http_error(503)
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
                               {"event_id": 101, "event_name": "順位なし", "show_ranking": 0, "ended_at": NOW - 86400},
                               {"event_id": 102, "event_name": "去年", "show_ranking": 1, "ended_at": NOW - 300 * 86400}],
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
                                {"user_id": 30, "name": "イベントだけ", "point": 700, "rank": 2},
                                {"user_id": 40, "name": "退会済みユーザー", "point": 500, "rank": 3}]}
        if path == "/api/event/contribution_ranking?event_id=200&room_id=1":
            return {"ranking": [{"user_id": 10, "name": "ファン", "point": 100, "rank": 1}]}
        raise AssertionError(f"想定していない問い合わせ: {path}")


@pytest.fixture(autouse=True)
def fresh_caches(monkeypatch):
    monkeypatch.setattr(listing, "_names_cache", None)
    monkeypatch.setattr(listing, "_file_cache", {})
    monkeypatch.setattr(listing, "gift_catalog", lambda state_dir, rooms=None: {5: {"gift_name": "花", "point": 100, "free": False}})
    research.forget_analysis()


@pytest.fixture
def collected(tmp_path):
    get = FakeShowroom()
    th, t = throttle()
    result = research.Collector(tmp_path, get=get, throttle=th).collect(["support", "other"])
    return tmp_path, get, t.waits, result


def quiet_throttle():
    return research.Throttle(0, sleep=lambda s: None)


def test_collect_rooms_with_gaps_between_requests(collected):
    tmp_path, get, waits, result = collected
    assert result == {"rooms": 2, "events": 3, "requests": len(get.calls), "failures": 0}
    assert waits == [1.0] * (len(get.calls) - 1)  # 問い合わせの間は必ず空ける
    assert not any("event_id=101" in c for c in get.calls)  # 順位を公開していないイベントは取らない
    assert not any("event_id=102" in c for c in get.calls)  # 約 9 か月より前のイベントは SHOWROOM が返さないので取らない
    rooms = research.load_rooms(tmp_path, ["support", "other"])
    assert rooms["support"]["events"][0] == {"event_id": 200, "name": "開催中", "key": "now_ev", "start": NOW - 86400,
                                             "end": NOW + 86400, "type": None, "show_ranking": True, "current": True}
    assert {r["user_id"]: r["visits"] for r in rooms["other"]["cumulative"]} == {20: 50, 10: 3}
    st = research.status(tmp_path, ["support", "other", "unknown"])
    assert [(r["name"], r["cumulative"], r["events"], r["rankings"]) for r in st] == [
        ("応援ルーム", 1, 2, 2), ("ほかのルーム", 2, 3, 1), ("unknown", 0, 0, 0)]


def test_finished_events_are_not_fetched_again(collected):
    tmp_path, get, _, _ = collected
    again = FakeShowroom()
    research.Collector(tmp_path, get=again, throttle=quiet_throttle()).collect(["support", "other"], force=True)
    assert not any("event_id=100" in c for c in again.calls)  # 終わったイベントは取り直さない
    assert any("event_id=200" in c for c in again.calls)  # 開催中のイベントは取り直す
    assert not any("room/status" in c for c in again.calls)  # room_id は覚えている
    quiet = FakeShowroom()
    research.Collector(tmp_path, get=quiet, throttle=quiet_throttle()).collect(["support", "other"])
    assert quiet.calls == []  # 取ったばかりなら問い合わせない


def test_refetching_a_ranking_replaces_the_points(collected):
    tmp_path, _, _, _ = collected
    with Store(tmp_path) as s:
        s.set_contributions(200, 1, [{"user_id": 10, "name": "ファン", "point": 250, "rank": 1}], NOW, False)
        assert s.scalar("SELECT events FROM user_room WHERE user_id=10 AND room_id=1") == 3000 + 250  # 100 は引いて 250 を足す


def test_one_failed_ranking_does_not_stop_the_room(tmp_path):
    get = FakeShowroom(fail={"/api/event/contribution_ranking?event_id=100&room_id=1"})
    result = research.Collector(tmp_path, get=get, throttle=quiet_throttle()).collect(["support"])
    assert result["rooms"] == 1 and result["failures"] == 1
    assert any("event_id=200" in c for c in get.calls)  # 失敗したイベントの次も取る
    retry = FakeShowroom()
    research.Collector(tmp_path, get=retry, throttle=quiet_throttle()).collect(["support"])
    assert retry.calls == ["/api/event/contribution_ranking?event_id=100&room_id=1"]  # 失敗した分だけ取り直す


def test_throttle_backs_off_and_pauses():
    th, t = throttle()

    def busy(path):
        raise http_error(429)

    for _ in range(research.PAUSE_AFTER - 1):
        with pytest.raises(urllib.error.HTTPError):
            th.call(busy, "/x")
    assert th.gap == 16 and t.waits == [2, 4, 8]  # 失敗するたびに間隔を倍に広げる
    with pytest.raises(urllib.error.HTTPError):
        th.call(busy, "/x")
    assert th.state()["paused"] == pytest.approx(research.PAUSE, abs=1)  # 続けて失敗したら止める
    t.waits.clear()
    assert th.call(lambda p: {"ok": 1}, "/x") == {"ok": 1}
    assert t.waits == [pytest.approx(research.PAUSE)]  # 止めている間は待つ
    before = th.gap
    th.call(lambda p: {}, "/x")
    assert th.gap < before and th.errors == 0  # 成功すると間隔を少しずつ戻す
    with pytest.raises(urllib.error.HTTPError):
        th.call(lambda p: (_ for _ in ()).throw(http_error(404)), "/x")
    assert th.errors == 0  # 無いもの (404) は失敗に数えない


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
    assert r["similar"] == [{"key": "other", "name": "ほかのルーム", "gifters": 4, "shared": 1,
                             "overlap": 0.25, "candidates": 3}]  # 退会したユーザーも重なりの数には入れる
    assert r["support"] == [{"key": "support", "name": "応援ルーム", "gifters": 1}] and r["gifter_count"] == 4
    # 結果はしばらく使い回す (全ルーム分は重いので、そのたびに数え直さない)
    record_gift(tmp_path, "other", 9, {"t": 2, "u": 50, "ac": "新しい人", "g": 5, "n": 1, "created_at": NOW - 60})
    assert research.analyze(tmp_path, ["support"]) is r
    assert 50 in [c["user_id"] for c in research.analyze(tmp_path, ["support"], max_age=0)["candidates"]]


def test_recorded_only_rooms_are_counted(tmp_path):
    record_gift(tmp_path, "nowhere", 9, {"t": 2, "u": 70, "ac": "記録だけ", "g": 5, "n": 2, "created_at": NOW - 60})
    r = research.analyze(tmp_path, [])
    assert [(c["user_id"], c["other_points"], c["top_rooms"][0]["key"]) for c in r["candidates"]] == [(70, 200, "nowhere")]


def test_gifter_detail_and_habits(collected):
    tmp_path, _, _, _ = collected
    for hours_ago in (1, 2, 26):
        record_gift(tmp_path, "other", 9, {"t": 2, "u": 20, "ac": "大口", "g": 5, "n": 1, "created_at": NOW - hours_ago * 3600})
    d = research.gifter_detail(tmp_path, "20", ["support"])
    assert d["found"] and [r["key"] for r in d["rooms"]] == ["other"]
    assert d["rooms"][0]["cumulative"] == 90000 and d["rooms"][0]["recorded"] == 300
    assert d["events"][0]["room_name"] == "ほかのルーム" and d["events"][0]["event"] == "秋のイベント"
    assert d["habits"]["times"] == 3 and d["habits"]["avg_point"] == 100
    assert sum(d["habits"]["hour"]) == 300 and len(d["habits"]["weekday"]) == 7
    assert research.gifter_detail(tmp_path, "999", [])["found"] is False
    with pytest.raises(ValueError):
        research.gifter_detail(tmp_path, "abc", [])


def test_old_json_files_are_imported(tmp_path):
    folder = tmp_path / "research"
    (folder / "rooms").mkdir(parents=True)
    (folder / "events").mkdir()
    (folder / "rooms" / "old.json").write_text(json.dumps({
        "key": "old", "room_id": 5, "name": "前のルーム", "at": NOW - 60,
        "cumulative": [{"user_id": 1, "name": "A", "point": 300, "visits": 2, "rank": 1}],
        "events": [{"event_id": 9, "name": "前のイベント", "key": None, "start": NOW - 99, "end": NOW - 50,
                    "type": None, "show_ranking": True, "current": False}]}), encoding="utf-8")
    (folder / "events" / "9_5.json").write_text(json.dumps({
        "event_id": 9, "room_id": 5, "room": "old", "name": "前のイベント", "at": NOW - 60, "final": True,
        "rows": [{"user_id": 1, "name": "A", "point": 400, "rank": 1}]}), encoding="utf-8")
    st = research.status(tmp_path, ["old"])
    assert st == [{"key": "old", "name": "前のルーム", "at": NOW - 60, "cumulative": 1, "events": 1, "rankings": 1}]
    assert research.gifter_detail(tmp_path, 1, [])["rooms"][0]["points"] == 400


class FakeAll:
    """全ルームの収集の SHOWROOM の代わり。"""

    def __init__(self):
        self.calls = []

    def __call__(self, path):
        self.calls.append(path)
        if path == "/api/live/onlives":
            return {"onlives": [{"lives": [{"room_id": 3, "room_url_key": "live3", "main_name": "配信中", "follower_num": 10}]}]}
        if path.startswith("/api/event/search?status=4&page=1"):
            return {"event_list": [{"event_id": 300, "event_name": "終わった", "event_url_key": "done_ev",
                                    "started_at": NOW - 5 * 86400, "ended_at": NOW - 2 * 86400, "show_ranking": True}],
                    "next_page": None}
        if path.startswith("/api/event/search"):
            return {"event_list": [], "next_page": None}
        if path == "/api/event/done_ev/rooms?page=1":
            return {"rooms": [{"room_id": 4, "room_url_key": "ev4", "room_name": "参加"}], "next_page": 2}
        if path == "/api/event/done_ev/rooms?page=2":
            return {"rooms": [{"room_id": 3, "room_url_key": "live3", "room_name": "配信中"}], "next_page": None}
        if path.startswith("/api/event/contribution_ranking?event_id=300"):
            return {"ranking": [{"user_id": 77, "name": "参加者のファン", "point": 1200, "rank": 1}]}
        if path.startswith("/api/live/summary_ranking"):
            return {"ranking": []}
        if path.startswith("/api/rooms/"):
            return {"events": [], "next_page": None}
        if path.startswith("/api/room/event_and_support"):
            return {"event": None}
        if path == "/api/room/profile?room_id=1":
            return {"room_url_key": "scan1", "room_name": "番号で見つけた", "follower_num": 5, "is_onlive": False}
        if path.startswith("/api/room/profile"):
            raise http_error(404)
        raise AssertionError(f"想定していない問い合わせ: {path}")


def test_sweep_finds_rooms_and_collects_rankings(tmp_path, monkeypatch):
    monkeypatch.setattr(sweep, "SCAN_MARGIN", 2)
    get = FakeAll()
    sw = sweep.Sweeper(tmp_path, collector=research.Collector(tmp_path, get=get, throttle=quiet_throttle()))
    steps = 0
    while sw.step():
        steps += 1
        assert steps < 50
    assert get.calls[0] == "/api/live/onlives" and get.calls[1].startswith("/api/event/search")
    st = research.store_status(tmp_path)
    assert (st["rooms"], st["events"], st["rankings"], st["rankings_waiting"]) == (3, 1, 2, 0)
    assert st["scan_done_at"] and st["scan_next"] == 7  # 最大の番号 4 + 2 まで調べた
    assert st["collected"] == 3  # 配信中・イベント参加・番号で見つけたルームの累計ランキングなど
    assert sum("contribution_ranking" in c for c in get.calls) == 2  # 参加した 2 ルームの分を 1 回ずつ
    with Store(tmp_path) as s:
        assert s.room(key="scan1")["found_via"] == "scan" and s.room(room_id=3)["found_via"] == "live"
    assert [c["user_id"] for c in research.analyze(tmp_path, [])["candidates"]] == [77]
    before = len(get.calls)
    assert sw.step() is False and len(get.calls) == before  # することが無ければ問い合わせない


def test_sweep_takes_turns_between_jobs(tmp_path):
    get = FakeAll()
    sw = sweep.Sweeper(tmp_path, collector=research.Collector(tmp_path, get=get, throttle=quiet_throttle()))
    for _ in range(6):
        sw.step()
    kinds = [c.split("?")[0].rsplit("/", 1)[-1] for c in get.calls]
    # 配信中 → イベント一覧のあとは、ルーム → 参加ルーム → ルーム番号 … と順番に回す
    assert "profile" in kinds and "summary_ranking" in kinds and "rooms" in kinds


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
           "SHOWROOM_STATE_FILE": str(tmp_path / "s.json"), "SHOWROOM_RESEARCH_ALL": "true",
           "SHOWROOM_RESEARCH_SCAN": "off", "SHOWROOM_RESEARCH_GAP": "1.5"}
    cfg = Config.load(None, env)
    assert (cfg.support_rooms, cfg.research_rooms) == (["sup"], ["a", "b"])
    assert (cfg.research_all, cfg.research_scan, cfg.research_gap) == (True, False, 1.5)
    assert Config.load(None, {}).research_all is False  # 全ルームの収集は .env で有効にしたときだけ
    # 前からある settings.json (応援ルームの項目が無い) は、.env の指定を消さない
    (tmp_path / "settings.json").write_text(json.dumps({"rooms": ["x"], "record_rooms": [], "events": []}), encoding="utf-8")
    assert apply_settings(cfg) and cfg.rooms == ["x"] and cfg.support_rooms == ["sup"]
    save_settings(cfg, {"rooms": ["x"], "support_rooms": ["new_sup"]})
    assert apply_settings(cfg) and cfg.support_rooms == ["new_sup"]
