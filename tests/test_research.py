import json
import time
import urllib.error
from datetime import datetime

import pytest

from showroom_keeper import listing, research, sweep
from showroom_keeper.config import Config, apply_settings, save_settings
from showroom_keeper.gifts import record_gift
from showroom_keeper.research_store import Store
from showroom_keeper.status import JST

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
        if path == "/api/event/contribution_ranking?event_id=102&room_id=2":
            return {"ranking": [{"user_id": 20, "name": "大口", "point": 5000, "rank": 1}]}
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
    assert result == {"rooms": 2, "events": 4, "requests": len(get.calls), "failures": 0}
    assert waits == [1.0] * (len(get.calls) - 1)  # 問い合わせの間は必ず空ける
    assert not any("event_id=101" in c for c in get.calls)  # 順位を公開していないイベントは取らない
    assert any("event_id=102" in c for c in get.calls)  # 古いイベントも取る (ギフトの量の移り変わりを見るため)
    rooms = research.load_rooms(tmp_path, ["support", "other"])
    assert rooms["support"]["events"][0] == {"event_id": 200, "name": "開催中", "key": "now_ev", "start": NOW - 86400,
                                             "end": NOW + 86400, "type": None, "show_ranking": True, "current": True}
    assert {r["user_id"]: r["visits"] for r in rooms["other"]["cumulative"]} == {20: 50, 10: 3}
    st = research.status(tmp_path, ["support", "other", "unknown"])
    assert [(r["name"], r["cumulative"], r["events"], r["rankings"]) for r in st] == [
        ("応援ルーム", 1, 2, 2), ("ほかのルーム", 2, 3, 2), ("unknown", 0, 0, 0)]


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
    assert big["event_count"] == 2 and big["best_rank"] == 1 and big["habits"]["times"] == 1
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


def test_cumulative_history_and_monthly_timeline(collected):
    tmp_path, _, _, _ = collected
    later = NOW + 40 * 86400
    with Store(tmp_path) as s:
        s.set_cumulative(2, [{"user_id": 20, "name": "大口", "point": 93000, "visits": 52, "rank": 1},
                             {"user_id": 10, "name": "ファン", "point": 800, "visits": 3, "rank": 2}], later)
        s.set_cumulative(2, [{"user_id": 20, "name": "大口", "point": 95000, "visits": 53, "rank": 1}], later + 86400)
        # ルームごとに 25 日以上空けて残す (すぐ後の取り直しは残さない)
        assert [tuple(r) for r in s.all("SELECT user_id, point FROM cumulative_history WHERE room_id=2 ORDER BY user_id, at")] == [
            (10, 800), (10, 800), (20, 90000), (20, 93000)]
    record_gift(tmp_path, "other", 9, {"t": 2, "u": 20, "ac": "大口", "g": 5, "n": 2, "created_at": NOW - 3600})
    t = research.gifter_detail(tmp_path, 20, ["support"])["timeline"]
    months = {m["month"]: m for m in t["months"]}
    month = lambda at: research._month(at)  # noqa: E731
    assert months[month(NOW - 7 * 86400)]["events"] == {"total": 40000, "rooms": {"other": 40000}}
    assert months[month(NOW - 300 * 86400)]["events"]["total"] == 5000  # 古いイベントの分も
    assert months[month(NOW - 3600)]["recorded"]["total"] == 200
    assert months[month(later)]["cumulative"]["total"] == 3000  # 取り直した間に増えた分
    assert t["rooms"] == ["other"] and t["names"] == {"other": "ほかのルーム"}
    keys = [m["month"] for m in t["months"]]
    assert keys == sorted(keys) and len(keys) >= 11  # 送っていない月も並べる
    assert research.gifter_detail(tmp_path, 30, [])["timeline"]["months"][0]["events"]["total"] == 700


def test_signup_is_estimated_from_user_id_order():
    jan = lambda y: datetime(y, 1, 15, tzinfo=JST).timestamp()  # noqa: E731
    # ID 500 が 2020 年、ID 900 が 2022 年に分かった。ID 700 が 2023 年に分かっても、900 より前に登録している
    sg = research._Signup([[500, jan(2020)], [900, jan(2022)]])
    assert sg.at(1) == sg.at(500) == jan(2020) and sg.at(501) == sg.at(700) == jan(2022) and sg.at(901) is None
    assert sg.years() == [(2022, 501, 900), (2020, 1, 500)]


def test_analysis_lists_gifters_by_signup_year(collected):
    tmp_path, _, _, _ = collected
    d = research.analyze(tmp_path, [], max_age=0)
    every = {u["user_id"]: u for u in d["candidates"] + d["signup_candidates"]}
    assert all(u["signup_at"] for u in every.values())
    years = {y["year"]: y for y in d["signup_years"]}
    assert sum(y["count"] for y in years.values()) == d["candidate_count"]
    assert [y["earliest"] for y in d["signup_years"]][-1] is True
    detail = research.gifter_detail(tmp_path, 20, [])
    assert detail["summary"]["signup_at"] == every[20]["signup_at"]


def test_older_research_db_gets_new_columns(tmp_path):
    import sqlite3

    c = sqlite3.connect(tmp_path / "research.db")
    c.executescript("""CREATE TABLE rooms (room_id INTEGER PRIMARY KEY, key TEXT, name TEXT, followers INTEGER,
                         found_via TEXT, found_at REAL, live_at REAL, collected_at REAL);
                       CREATE TABLE events (event_id INTEGER PRIMARY KEY, key TEXT, name TEXT, start REAL, end REAL, type TEXT,
                         show_ranking INTEGER DEFAULT 1, rooms_at REAL);
                       CREATE TABLE room_events (room_id INTEGER, event_id INTEGER, ranking_at REAL, final INTEGER DEFAULT 0,
                         failures INTEGER DEFAULT 0, PRIMARY KEY (room_id, event_id));""")
    c.close()
    with Store(tmp_path) as s:
        s.set_event_rooms(7, [{"room_id": 1, "key": "a", "point": 10, "rank": 1}], NOW)
        assert tuple(s.one("SELECT point, rank FROM room_events")) == (10, 1)
        assert s.room(room_id=1)["retry_at"] is None


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
            return {"event_list": [{"event_id": 3, "event_name": "終わった", "event_url_key": "done_ev",
                                    "started_at": NOW - 5 * 86400, "ended_at": NOW - 2 * 86400, "show_ranking": True}],
                    "next_page": None}
        if path.startswith("/api/event/search"):
            return {"event_list": [], "next_page": None}
        if path == "/api/event/room_list?event_id=3&p=1":
            return {"list": [{"room_id": 4, "room_url_key": "ev4", "room_name": "参加", "point": 5000, "rank": 1,
                              "last_lived_at": NOW - 86400}], "next_page": 2}
        if path == "/api/event/room_list?event_id=3&p=2":
            return {"list": [{"room_id": 3, "room_url_key": "live3", "room_name": "配信中", "point": 10, "rank": 2},
                             {"room_id": 5, "room_url_key": "zero5", "room_name": "誰も送っていない", "point": 0}],
                    "next_page": None}
        if path == "/api/event/room_list?event_id=2&p=1":  # 過去のイベント (一覧には出てこない)
            return {"list": [{"room_id": 6, "room_url_key": "old6", "room_name": "昔のルーム", "point": 300, "rank": 1}],
                    "next_page": None}
        if path.startswith("/api/event/room_list"):
            return {"list": [], "next_page": None}
        if path.startswith("/api/event/contribution_ranking?event_id=3"):
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
    assert (st["rooms"], st["events"], st["rankings"], st["rankings_waiting"]) == (5, 2, 2, 0)
    assert st["scan_done_at"] and st["scan_next"] == 9  # 最大の番号 6 + 2 まで調べた
    assert st["collected"] == 5  # 配信中・イベント参加・番号で見つけたルームの累計ランキングなど
    # 参加した 2 ルームの分を 1 回ずつ (ポイントが 0 のルームは誰も送っていないので取らない)
    assert sum("contribution_ranking" in c for c in get.calls) == 2
    assert sum("room_list?event_id=1&" in c for c in get.calls) == 1  # 過去のイベントを番号で 1 までさかのぼる
    with Store(tmp_path) as s:
        assert tuple(s.one("SELECT point, rank FROM room_events WHERE room_id=4 AND event_id=3")) == (5000, 1)
        assert s.one("SELECT point FROM room_events WHERE room_id=6 AND event_id=2")[0] == 300  # 昔のイベントの成績
        assert s.room(room_id=4)["live_at"] == NOW - 86400  # 最後に配信した日時
        assert s.meta("history_done_at")
    with Store(tmp_path) as s:
        assert s.room(key="scan1")["found_via"] == "scan" and s.room(room_id=3)["found_via"] == "live"
    assert [c["user_id"] for c in research.analyze(tmp_path, [])["candidates"]] == [77]
    before = len(get.calls)
    assert sw.step() is False and len(get.calls) == before  # することが無ければ問い合わせない


def test_sweep_puts_off_rooms_and_events_that_keep_failing(tmp_path):
    class Broken(FakeAll):
        def __call__(self, path):
            if path.startswith("/api/live/summary_ranking?room_id=4") or path.startswith("/api/event/room_list?event_id=3&"):
                self.calls.append(path)
                raise http_error(500)
            return super().__call__(path)

    get = Broken()
    sw = sweep.Sweeper(tmp_path, collector=research.Collector(tmp_path, get=get, throttle=quiet_throttle()), scan=False)
    with Store(tmp_path) as s:
        s.upsert_rooms([{"room_id": 4, "key": "ev4"}], "event", NOW)
    steps = 0
    while sw.step():
        steps += 1
        assert steps < 20  # 失敗し続けるものを何度も問い合わせない
    assert sum("summary_ranking?room_id=4" in c for c in get.calls) == 1
    assert sum("room_list?event_id=3&" in c for c in get.calls) == 1
    assert "500" in sw.last_error
    with Store(tmp_path) as s:
        assert s.room(room_id=4)["retry_at"] > NOW and s.room(room_id=4)["collected_at"] is None


def test_events_keep_names_and_hidden_rankings(tmp_path):
    with Store(tmp_path) as s:
        s.upsert_events([research._event_info({"event_id": 5, "event_name": "名前", "show_ranking": 0})])
        s.upsert_events([research._event_info({"event_id": 5, "ended_at": NOW})])  # 名前などの無い取り方
        e = s.one("SELECT * FROM events WHERE event_id=5")
        assert (e["name"], e["show_ranking"], e["end"]) == ("名前", 0, NOW)
        s.upsert_events([research._event_info({"event_id": 6})])
        assert s.one("SELECT show_ranking FROM events WHERE event_id=6")[0] == 1


def test_forbidden_counts_as_busy():
    th, _ = throttle()
    with pytest.raises(urllib.error.HTTPError):
        th.call(lambda p: (_ for _ in ()).throw(http_error(403)), "/x")
    assert th.errors == 1 and th.gap == 2


def test_sweep_takes_turns_between_jobs(tmp_path):
    get = FakeAll()
    sw = sweep.Sweeper(tmp_path, collector=research.Collector(tmp_path, get=get, throttle=quiet_throttle()))
    for _ in range(6):
        sw.step()
    kinds = [c.split("?")[0].rsplit("/", 1)[-1] for c in get.calls]
    # 配信中 → イベント一覧のあとは、ルーム → 参加ルーム → ルーム番号 … と順番に回す
    assert "profile" in kinds and "summary_ranking" in kinds and "room_list" in kinds


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


def test_analyze_without_support_room(collected):
    tmp_path, _, _, _ = collected
    r = research.analyze(tmp_path, [])
    assert r["mode"] == "all" and r["fans"] == [] and r["similar"] == []
    assert [c["user_id"] for c in r["candidates"]] == [20, 10, 30]  # 分かったギフター全員を、ポイントの多い順に
    assert r["candidates"][1]["room_count"] == 2 and r["multi_count"] == 1
    assert r["pairs"] == []  # 共通のギフターが 1 人だけの組は出さない (PAIR_MIN_SHARED)
    rooms = {x["key"]: x for x in r["rooms"]}
    assert rooms["other"]["gifters"] == 4 and rooms["other"]["top"]["user_id"] == 20 and rooms["other"]["multi"] == 1
    assert rooms["support"]["events"] == 2 and rooms["support"]["researched"]


def test_room_pairs_share_gifters(tmp_path, monkeypatch):
    monkeypatch.setattr(listing, "_names_cache", None)
    monkeypatch.setattr(listing, "_file_cache", {})
    monkeypatch.setattr(listing, "gift_catalog", lambda state_dir, rooms=None: {5: {"gift_name": "花", "point": 10, "free": False}})
    for room in ("a", "b", "c"):
        users = {"a": (1, 2, 3), "b": (1, 2), "c": (3,)}[room]
        for u in users:
            record_gift(tmp_path, room, 1, {"t": 2, "u": u, "ac": f"u{u}", "g": 5, "n": 1, "created_at": NOW})
    r = research.analyze(tmp_path, [])
    assert [(p["a"], p["b"], p["shared"]) for p in r["pairs"]] == [("a", "b", 2)]
    assert r["pairs"][0]["overlap"] == round(2 / 3, 3)


def test_sweep_records_points_of_ongoing_events_every_hour(tmp_path):
    class Ongoing(FakeAll):
        point = 100

        def __call__(self, path):
            if path.startswith("/api/event/search?status=1&page=1"):
                self.calls.append(path)
                return {"event_list": [{"event_id": 8, "event_name": "開催中", "event_url_key": "now_ev",
                                        "started_at": NOW - 86400, "ended_at": NOW + 86400, "show_ranking": True}],
                        "next_page": None}
            if path == "/api/event/room_list?event_id=8&p=1":
                self.calls.append(path)
                return {"list": [{"room_id": 4, "room_url_key": "ev4", "room_name": "参加", "point": self.point, "rank": 1},
                                 {"room_id": 9, "room_url_key": "ev9", "room_name": "まだ", "point": None}], "next_page": None}
            return super().__call__(path)

    clock = [NOW]
    get = Ongoing()
    sw = sweep.Sweeper(tmp_path, collector=research.Collector(tmp_path, get=get, throttle=quiet_throttle()), scan=False,
                       clock=lambda: clock[0])
    for _ in range(40):
        if not sw.step():
            break
    first = sum("room_list?event_id=8&" in c for c in get.calls)
    with Store(tmp_path) as s:
        rows = s.all("SELECT room_id, point, rank FROM event_progress WHERE event_id=8")
    assert [tuple(r) for r in rows] == [(4, 100, 1)]  # ポイントの分からないルームは残さない
    for _ in range(5):
        sw.step()
    assert sum("room_list?event_id=8&" in c for c in get.calls) == first  # 1 時間たつまでは読み直さない
    clock[0] = NOW + 3601
    get.point = 250
    for _ in range(10):
        sw.step()
    with Store(tmp_path) as s:
        pts = [r[0] for r in s.all("SELECT point FROM event_progress WHERE event_id=8 AND room_id=4 ORDER BY at")]
    assert pts == [100, 250]
    st = research.store_status(tmp_path)
    assert st["progress_events"] == 1 and st["progress_rows"] == 2
