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


def test_throttle_pauses_longer_each_time_and_recovers(tmp_path):
    th, t = throttle()

    def busy(path):
        raise http_error(403)

    def pause():
        for _ in range(research.PAUSE_AFTER):
            with pytest.raises(urllib.error.HTTPError):
                th.call(busy, "/x")
        return th.state()

    assert pause()["paused"] == pytest.approx(research.PAUSE, abs=1)
    st = pause()  # 再開してすぐにまた止められたら、倍の時間止める
    assert st["paused"] == pytest.approx(2 * research.PAUSE, abs=60) and st["pauses"] == 2
    assert [x["count"] for x in st["pause_log"]] == [2, 1] and st["pause_log"][0]["why"] == "HTTP 403"
    for _ in range(4):
        st = pause()
    assert st["paused"] <= research.MAX_PAUSE + 1 and st["next_pause"] == research.MAX_PAUSE  # 上限まで
    for _ in range(research.RECOVER_AFTER):  # 続けて成功したら、1 時間に戻す
        th.call(lambda p: {}, "/x")
    assert th.state()["pauses"] == 0 and th.state()["next_pause"] == research.PAUSE


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
    jan = lambda y: datetime(y, 1, 1, tzinfo=JST).timestamp()  # noqa: E731
    # ID 500 が 2020 年、ID 900 が 2022 年に初めて分かった (間の ID はその間に登録した)
    sg = research._Signup([[500, jan(2020)], [900, jan(2022)]])
    assert sg.at(700) == jan(2022) and sg.at(901) is None  # 遅くとも
    est, before, after = sg.guess(700)
    assert (before, after) == (jan(2020), jan(2022)) and est == jan(2020) + (jan(2022) - jan(2020)) / 2
    assert sg.guess(500)[0] == jan(2020) and sg.guess(901) is None
    assert sg.guess(1)[1] == research.SHOWROOM_START
    years = sg.years()
    assert years[0][0] == 2022 and years[-1][0] == 2013
    # 年ごとの ID の範囲はすき間なく続き、どの ID の推測もその年に入る
    assert years[-1][1] == 1 and years[0][2] == 900
    assert all(a[1] == b[2] + 1 for a, b in zip(years, years[1:]))
    for year, lo, hi, rough in years:
        assert rough  # 目印が 2 年おきなので大まか
        for uid in (lo, hi):
            assert datetime.fromtimestamp(sg.guess(uid)[0], JST).year == year


def test_analysis_lists_gifters_by_signup_year(collected):
    tmp_path, _, _, _ = collected
    d = research.analyze(tmp_path, [], max_age=0)
    every = {u["user_id"]: u for u in d["candidates"] + d["signup_candidates"]}
    assert all(u["signup_from"] <= u["signup_at"] <= u["signup_to"] for u in every.values())
    assert sum(y["count"] for y in d["signup_years"]) == d["candidate_count"]
    detail = research.gifter_detail(tmp_path, 20, [])
    assert detail["summary"]["signup_at"] == every[20]["signup_at"]


class FakeOldEvents:
    """登録時期の目印を集める時の SHOWROOM の代わり (番号 100〜250 の古いイベント)。"""

    def __init__(self):
        self.calls = []

    def __call__(self, path):
        self.calls.append(path)
        if path.startswith("/api/event/search?status=4&page=1"):
            return {"event_list": [{"event_id": 250, "event_name": "最近", "started_at": NOW - 86400 * 9, "ended_at": NOW - 86400 * 8}],
                    "next_page": None}
        if path.startswith("/api/event/search"):
            return {"event_list": [], "next_page": None}
        if path.startswith("/api/event/room_list?event_id=100&"):
            return {"list": [{"room_id": r, "room_url_key": f"r{r}", "point": 1000 - r, "rank": r} for r in (1, 2, 3, 4)] +
                            [{"room_id": 9, "room_url_key": "r9", "point": 0, "rank": 9}], "next_page": None}
        if path.startswith("/api/event/room_list"):
            return {"list": [], "next_page": None}
        if path.startswith("/api/event/contribution_ranking?event_id=100&"):
            rid = int(path.rsplit("=", 1)[1])
            return {"event": {"event_name": "昔のイベント", "started_at": 1_440_000_000, "ended_at": 1_450_000_000, "show_ranking": 0},
                    "ranking": [{"user_id": 1000 + rid, "name": f"u{rid}", "point": 10, "rank": 1}]}
        raise AssertionError(f"想定していない問い合わせ: {path}")


def test_sweep_reads_old_events_first_as_signup_anchors(tmp_path, monkeypatch):
    get = FakeOldEvents()
    monkeypatch.setattr(sweep.Sweeper, "_onlives", lambda self, now: self.store.set_meta("live_at", now) or True)
    sw = sweep.Sweeper(tmp_path, collector=research.Collector(tmp_path, get=get, throttle=quiet_throttle()), scan=False)
    for _ in range(6):  # 配信中のルーム・イベントの一覧のあと、目印を 4 つ
        sw.step()
    assert research.anchor_order(250) == [100, 200, 150, 250]
    assert [c.split("&")[0].rsplit("=", 1)[1] for c in get.calls if "room_list" in c] == ["100", "200", "150", "250"]
    # 上位・中ほど・下位の 3 ルームだけ取る (ポイントが 0 のルームは取らない)
    assert sorted(int(c.rsplit("=", 1)[1]) for c in get.calls if "contribution_ranking" in c) == [1, 3, 4]
    with Store(tmp_path) as s:
        e = s.one("SELECT name, end, show_ranking FROM events WHERE event_id=100")
        assert tuple(e) == ("昔のイベント", 1_450_000_000, 1)  # 番号で読んだ古いイベントの日付が分かる
        assert s.meta("anchor_done_at") is None
    sw.step()
    with Store(tmp_path) as s:
        assert s.meta("anchor_done_at")
    assert research.store_status(tmp_path)["anchor_total"] == 4
    with Store(tmp_path) as s:
        assert research._Signup.build(s, research._Recorded(tmp_path)).at(1004) == 1_450_000_000


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
            live = {"room_id": 3, "room_url_key": "live3", "main_name": "配信中", "follower_num": 10, "view_num": 25,
                    "started_at": NOW - 600}
            return {"onlives": [{"lives": [live]}, {"lives": [live]}]}  # まとめ枠にも同じルームが出る
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
        if path.startswith("/api/active_fan/users?room_id=3&"):
            return {"users": [{"user_id": 77, "level": 8, "title_id": 2, "user_name": "常連"}], "total_user_count": 1, "fan_power": 5}
        if path.startswith("/api/active_fan/users"):
            return {"users": [], "total_user_count": 0}
        if path == "/api/user/profile?user_id=77":
            return {"room_profile": {"room_id": 4, "room_url_key": "ev4", "room_name": "参加"}}  # 77 はルーム 4 の配信者
        raise AssertionError(f"想定していない問い合わせ: {path}")


def test_sweep_finds_rooms_and_collects_rankings(tmp_path, monkeypatch):
    monkeypatch.setattr(sweep, "SCAN_MARGIN", 2)
    monkeypatch.setattr(sweep, "FANS_FIRST", "209901")  # 前の月のアクティブファンは読み足さない (この試しでは何年分も続くため)
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
        daily = s.one("SELECT * FROM room_daily WHERE room_id=3")
        assert (daily["followers"], daily["lives"], daily["view_max"], daily["view_n"]) == (10, 1, 25, 1)
        assert "view_num" in s.meta("onlives_keys")
        assert s.meta("room_stats_upto") is True and s.meta("room_profile_at")  # ルームの分析も作った
        # 最近配信したルームのアクティブファン (ポイントに出ない常連も) と、配信者のアカウントとルームの結び付き
        assert [tuple(r) for r in s.all("SELECT room_id, user_id, level FROM active_fans")] == [(3, 77, 8)]
        assert s.one("SELECT fan_power, total FROM room_fans WHERE room_id=3")[:] == (5, 1)
        assert s.one("SELECT room_id FROM user_rooms WHERE user_id=77")[0] == 4
    with Store(tmp_path) as s:
        assert s.room(key="scan1")["found_via"] == "scan" and s.room(room_id=3)["found_via"] == "live"
    assert [c["user_id"] for c in research.analyze(tmp_path, [])["candidates"]] == [77]
    before = len(get.calls)
    assert sw.step() is False and len(get.calls) == before  # することが無ければ問い合わせない


def test_sweep_puts_off_rooms_and_events_that_keep_failing(tmp_path, monkeypatch):
    monkeypatch.setattr(sweep, "FANS_FIRST", "209901")  # 前の月のアクティブファンは読み足さない (この試しでは何年分も続くため)
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
    for _ in range(14):
        sw.step()
    kinds = [c.split("?")[0].rsplit("/", 1)[-1] for c in get.calls]
    # 配信中 → イベント一覧のあとは、ルーム → 参加ルーム → ルーム番号 … と順番に回す (間に貢献ランキングを挟む)
    assert "profile" in kinds and "summary_ranking" in kinds and "room_list" in kinds


def test_sweep_gives_rankings_every_other_turn(tmp_path):
    sw = sweep.Sweeper(tmp_path, collector=research.Collector(tmp_path, get=lambda p: {}, throttle=quiet_throttle()))
    done = []
    for name in ("_next_progress", "_next_ranking", "_next_room", "_next_event_rooms", "_next_fans", "_next_event_history",
                 "_next_owner", "_next_scan"):
        setattr(sw, name, (lambda n: lambda now: done.append(n) or True)(name))
    with Store(tmp_path) as s:
        s.set_meta("live_at", NOW)
        s.set_meta("events_at", NOW)
        s.set_meta("anchor_done_at", NOW)
    sw._clock = lambda: NOW
    for _ in range(10):
        sw.step()
    assert done.count("_next_ranking") == 5 and done[1::2] == ["_next_ranking"] * 5  # ほかの仕事 1 つごとに 1 回


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
    # 終わったあと、参加ルームを読み直した時の最終結果も、終わった時刻の値として推移に入れる
    clock[0] = NOW + 86400 * 3
    get.point = 900
    for _ in range(20):
        if not sw.step():
            break
    with Store(tmp_path) as s:
        last = s.one("SELECT at, point FROM event_progress WHERE event_id=8 AND room_id=4 ORDER BY at DESC LIMIT 1")
    assert last["point"] == 900 and last["at"] == pytest.approx(NOW + 86400)


def test_watch_list_with_memo(collected):
    tmp_path, _, _, _ = collected
    assert research.watch_list(tmp_path, []) == []
    research.set_watch(tmp_path, "20", True, "月末に大きく送る")
    research.set_watch(tmp_path, 10, True)
    w = {x["user_id"]: x for x in research.watch_list(tmp_path, ["support"])}
    assert w[20]["memo"] == "月末に大きく送る" and w[20]["other_points"] == 90000 and w[20]["name"] == "大口"
    assert w[10]["memo"] == "" and w[10]["support_points"] == 5000
    research.set_watch(tmp_path, 20, True)  # メモを渡さない時はメモを変えない
    assert research.gifter_detail(tmp_path, 20, [])["watch"]["memo"] == "月末に大きく送る"
    research.set_watch(tmp_path, 20, True, "")
    assert research.gifter_detail(tmp_path, 20, [])["watch"]["memo"] == ""
    research.set_watch(tmp_path, 20, False)
    assert research.gifter_detail(tmp_path, 20, [])["watch"] is None
    assert [x["user_id"] for x in research.watch_list(tmp_path, [])] == [10]
    with pytest.raises(ValueError):
        research.set_watch(tmp_path, "abc", True)
    with pytest.raises(ValueError):
        research.set_watch(tmp_path, 1, True, "あ" * 501)


def _monthly_events(s, months_back, points, now, pending_months=()):
    """months_back か月前から先月まで、月に 1 つずつ終わったイベントと貢献ランキングを入れる。"""
    s.upsert_rooms([{"room_id": 1, "key": "r1", "name": "ルーム"}], "settings", now)
    s.conn.execute("UPDATE rooms SET collected_at=?", (now,))
    s.set_meta("history_done_at", now)
    this = datetime.fromtimestamp(now, JST).replace(day=15, hour=12, minute=0, second=0, microsecond=0)
    for back in range(months_back, 0, -1):
        y, m = this.year, this.month - back
        while m <= 0:
            y, m = y - 1, m + 12
        end = this.replace(year=y, month=m).timestamp()
        eid = 1000 + back
        s.upsert_events([{"event_id": eid, "name": f"e{back}", "end": end, "show_ranking": True}])
        s.link([(1, eid)])
        if back in pending_months:
            continue  # まだ取っていない貢献ランキング
        s.set_contributions(eid, 1, [{"user_id": u, "point": f(back), "name": f"u{u}"} for u, f in points.items() if f(back)],
                            now, final=True)


def test_trend_compares_only_collected_months(tmp_path):
    with Store(tmp_path) as s:
        _monthly_events(s, 26, {
            1: lambda b: 20000 if b > 12 else 1000,  # 前の 12 か月は多く、最近は少ない → 離れつつある
            2: lambda b: 8000 if b <= 12 else 0,  # 最近から送り始めた → 伸びている
            3: lambda b: 5000,  # 変わらない
        }, NOW)
    cov = research.coverage(tmp_path, max_age=0)
    assert cov["complete_from"] is not None and all(m["complete"] for m in cov["months"][:-1])
    d = research.analyze(tmp_path, [], max_age=0)
    t = d["trend"]
    assert t["period"] == 12
    assert [u["user_id"] for u in t["falling"]] == [1] and t["falling"][0]["before"] == 240000
    assert [u["user_id"] for u in t["rising"]] == [2] and t["rising"][0]["recent"] == 96000
    assert t["falling"][0]["name"] == "u1"
    assert research.gifter_detail(tmp_path, 1, [])["coverage"]["complete_from"] == cov["complete_from"]


def test_trend_waits_for_older_months(tmp_path):
    # 古い月の貢献ランキングをまだ取っていない時は、そこまで比べない (取れている短い期間で比べる)
    with Store(tmp_path) as s:
        _monthly_events(s, 26, {1: lambda b: 20000}, NOW, pending_months=(9, 15))
    cov = research.coverage(tmp_path, max_age=0)
    assert not next(m for m in cov["months"] if m["pending"])["complete"]
    t = research.analyze(tmp_path, [], max_age=0)["trend"]
    assert t["period"] == 3 and t["falling"] == [] and t["rising"] == []
    # ルーム番号の調べで見つけた、取らないルーム (フォロワー 0 人・配信の記録なし) は数えない
    with Store(tmp_path) as s:
        s.upsert_rooms([{"room_id": n, "followers": 0} for n in range(100, 150)], "scan", NOW)
    assert research.coverage(tmp_path, max_age=0)["rooms"] == {"total": 1, "collected": 1, "ok": True}
    # ルームをまだ集め終わっていない時は、どの月も集め終わっていない扱い
    with Store(tmp_path) as s:
        s.upsert_rooms([{"room_id": n} for n in range(2, 10)], "live", NOW)
    assert research.coverage(tmp_path, max_age=0)["complete_from"] is None
    assert research.analyze(tmp_path, [], max_age=0)["trend"]["period"] is None


def test_user_month_is_built_from_earlier_rankings(tmp_path):
    with Store(tmp_path) as s:
        _monthly_events(s, 3, {1: lambda b: 100 * b}, NOW)
        s.conn.execute("DELETE FROM user_month")
        s.conn.execute("DELETE FROM meta WHERE k='user_month_upto'")  # user_month が無かったころの research.db
        assert s.build_user_month(step=2) is True
        assert s.build_user_month(step=2) is False
        assert s.scalar("SELECT SUM(points) FROM user_month WHERE user_id=1") == 600
        # 取り直したときは、前の分を引いてから足す
        s.set_contributions(1001, 1, [{"user_id": 1, "point": 150}], NOW, final=True)
        assert s.scalar("SELECT SUM(points) FROM user_month WHERE user_id=1") == 650


def test_sweep_stops_when_next_page_does_not_advance(tmp_path):
    calls = []

    def get(path):
        calls.append(path)
        if "room_list" in path:  # 次のページの番号が進まない (いつも 1 を返す) イベント
            return {"list": [{"room_id": 1, "point": 5, "rank": 1}], "next_page": 1}
        if "event/search" in path:
            return {"event_list": [], "next_page": 1}
        return {}

    sw = sweep.Sweeper(tmp_path, collector=research.Collector(tmp_path, get=get, throttle=quiet_throttle()), scan=False)
    assert len(sw._read_event_rooms(7, NOW)) == 1 and len(calls) == 1  # 同じページを読み続けない
    sw._event_list(NOW)
    assert sum("event/search" in c for c in calls) == 3  # 状態ごとに 1 ページだけ

    calls.clear()
    pages = {1: [1, 2], 2: [3], 3: [1, 2]}  # 最後のページより先を頼むと 1 ページ目が返ってくる

    def get2(path):
        calls.append(path)
        p = int(path.rsplit("=", 1)[1])
        return {"list": [{"room_id": r, "point": r} for r in pages.get(p, pages[1])], "next_page": p + 1}

    sw2 = sweep.Sweeper(tmp_path, collector=research.Collector(tmp_path, get=get2, throttle=quiet_throttle()), scan=False)
    assert [r["room_id"] for r in sw2._read_event_rooms(8, NOW)] == [1, 2, 3] and len(calls) == 3


def test_sweep_logs_where_it_is_stuck(tmp_path, monkeypatch, caplog):
    import threading

    monkeypatch.setattr(sweep, "STALL", 0)
    sw = sweep.Sweeper(tmp_path, collector=research.Collector(tmp_path, get=lambda p: {}, throttle=quiet_throttle()), scan=False)
    gate = threading.Event()
    monkeypatch.setattr(sw, "step", lambda: gate.wait(10) and False)  # 仕事の途中で止まる
    waits = []
    real_wait = sw._stop.wait
    monkeypatch.setattr(sw._stop, "wait", lambda t=None: waits.append(t) or real_wait(0.05))
    t = threading.Thread(target=sw.run_forever, daemon=True)
    with caplog.at_level("WARNING", logger="research"):
        t.start()
        for _ in range(100):
            if any("続いています" in r.message for r in caplog.records):
                break
            time.sleep(0.05)
        assert sw.state()["busy_seconds"] >= 0 and sw.step_started_at
        gate.set()
        sw.stop()
        t.join(5)
    msg = next(r.message for r in caplog.records if "続いています" in r.message)
    assert "gate.wait" in msg or "wait" in msg  # どこで止まっているかが分かる
    assert sw.last_step_at and sw.step_started_at is None


def test_sweep_retries_live_progress_soon_after_a_failure(tmp_path):
    clock = [NOW]
    fail = [True]

    def get(path):
        if "room_list?event_id=8" in path:
            if fail[0]:
                raise urllib.error.HTTPError(path, 500, "busy", {}, None)
            return {"list": [{"room_id": 4, "point": 100, "rank": 1}], "next_page": None}
        return {}

    with Store(tmp_path) as s:
        s.upsert_events([{"event_id": 8, "name": "開催中", "start": NOW - 86400, "end": NOW + 86400}])
    sw = sweep.Sweeper(tmp_path, collector=research.Collector(tmp_path, get=get, throttle=quiet_throttle()), scan=False,
                       clock=lambda: clock[0])
    assert sw._next_progress(NOW)  # 1 回失敗
    fail[0] = False
    assert not sw._next_progress(NOW + 60)  # すぐには読み直さない
    clock[0] = NOW + sweep.PROGRESS_RETRY + 1
    assert sw._next_progress(clock[0])  # 1 日ではなく、10 分ほどで読み直す
    with Store(tmp_path) as s:
        assert s.scalar("SELECT COUNT(*) FROM event_progress WHERE event_id=8") == 1


def test_sweep_stops_when_disk_is_almost_full(tmp_path, monkeypatch):
    class Usage:
        free = 100

    monkeypatch.setattr(sweep.shutil, "disk_usage", lambda path: Usage)
    calls = []
    sw = sweep.Sweeper(tmp_path, collector=research.Collector(tmp_path, get=lambda p: calls.append(p) or {}, throttle=quiet_throttle()),
                       scan=False)
    assert sw.step() is False and calls == [] and sw.state()["disk_low"] == 100
    Usage.free = 10 << 30
    sw._disk_checked = 0
    assert sw.step() is True and sw.state()["disk_low"] is None  # 空きが戻ったら再開する


def test_falling_ignores_people_who_only_dropped_below_rank_100(tmp_path):
    with Store(tmp_path) as s:
        _monthly_events(s, 26, {
            1: lambda b: 20000 if b > 12 else 1000,  # 本当に減った
            2: lambda b: 20000 if b > 12 else 0,  # 最近は載っていない (101 位より下かもしれない)
        }, NOW)
        # 最近の期間のイベントは 100 人に届いていて、100 位は 12000 pt だった → 2 は 12000 pt 近く送っていたかもしれない
        s.conn.execute("""UPDATE room_events SET c_count=100, c_p100=12000 WHERE event_id IN
                          (SELECT event_id FROM events WHERE event_id < 1013)""")
    t = research.analyze(tmp_path, [], max_age=0)["trend"]
    assert [u["user_id"] for u in t["falling"]] == [1]
    assert [u["user_id"] for u in t["hidden"]] == [2] and t["hidden"][0]["recent_upper"] == 12 * 12000
    assert t["falling"][0]["reason"] == "本人が減った" and t["falling"][0]["name"] == "u1"


def test_falling_reason_when_main_room_stopped(tmp_path):
    with Store(tmp_path) as s:
        _monthly_events(s, 26, {1: lambda b: 20000 if b > 12 else 0}, NOW)
        # 最近の期間は、いちばん送っていたルームがイベントに出ていない (推しのルームが休んだ)
        s.conn.execute("DELETE FROM room_events WHERE event_id < 1013")
    t = research.analyze(tmp_path, [], max_age=0)["trend"]
    assert [u["user_id"] for u in t["falling"]] == [1] and t["falling"][0]["reason"] == "推しのルームが休んだ・消えた"
    assert t["falling_reasons"] == {"推しのルームが休んだ・消えた": 1}


def test_sweep_records_the_last_hours_finely(tmp_path):
    clock = [NOW]
    calls = []

    def get(path):
        calls.append(path)
        if "room_list?event_id=8&p=1" in path:
            return {"list": [{"room_id": 4, "point": int(clock[0] - NOW), "rank": 1}], "next_page": 2}
        return {}

    with Store(tmp_path) as s:
        s.upsert_events([{"event_id": 8, "name": "もうすぐ終わる", "start": NOW - 86400, "end": NOW + 3600}])
        s.conn.execute("UPDATE events SET progress_at=? WHERE event_id=8", (NOW,))
    sw = sweep.Sweeper(tmp_path, collector=research.Collector(tmp_path, get=get, throttle=quiet_throttle()), scan=False,
                       clock=lambda: clock[0])
    assert sw._next_final(NOW) and not sw._next_final(NOW + 30)  # 1 分ごと
    assert sw._next_final(NOW + 61)
    assert sw._next_final(NOW + 3600 - 500) and sw._next_final(NOW + 3600 - 484)  # 終わる 10 分前からは 15 秒ごと
    assert not sw._next_final(NOW + 3600 - 480)
    assert sw._next_final(NOW + 3600 + 100)  # 終わったあとも少し読む
    assert not sw._next_final(NOW + 3600 + 2000)  # 15 分たったらやめる
    with Store(tmp_path) as s:
        rows = s.all("SELECT at, point FROM event_final WHERE event_id=8 ORDER BY at")
    assert len(rows) == 5 and all("p=1" in c for c in calls)  # 1 ページ目 (上位 30 ルーム) だけ


def test_sweep_reads_every_two_seconds_in_the_last_minute(tmp_path):
    def get(path):
        return {"list": [{"room_id": 4, "point": 1, "rank": 1}]}

    with Store(tmp_path) as s:
        s.upsert_events([{"event_id": 8, "name": "まもなく終了", "start": NOW - 86400, "end": NOW + 30}])
        s.conn.execute("UPDATE events SET progress_at=? WHERE event_id=8", (NOW,))
    sw = sweep.Sweeper(tmp_path, collector=research.Collector(tmp_path, get=get, throttle=quiet_throttle()), scan=False)
    assert sw._next_final(NOW) and not sw._next_final(NOW + 1) and sw._next_final(NOW + 2)  # 最後の 1 分は 2 秒ごと


def test_event_details_pages_and_notices(tmp_path, monkeypatch):
    import json as _json
    import urllib.error

    from showroom_keeper import recorder

    DAY = 86400
    lst = {"event_list": [{"event_id": 7, "event_name": "大会", "event_url_key": "cup", "started_at": NOW - DAY, "ended_at": NOW + DAY,
                           "event_description": "説明", "parent_event_id": 3, "reward_rank": 5, "league_ids": [10, 20],
                           "event_block_label": "All Livers", "is_box_event": False, "image_m": "x.png"}]}
    sw = sweep.Sweeper(tmp_path, collector=research.Collector(tmp_path, get=lambda p: lst if "status=1" in p else {},
                                                              throttle=quiet_throttle()), scan=False)
    sw._event_list(NOW)
    pages = {"/event/cup": "<html><script>x=1</script><h1>大会</h1><p>ポイントの数え方<br>有料ギフト 1.5 倍</p></html>"}

    def page(p):
        if p not in pages:
            raise urllib.error.HTTPError(p, 404, "Not Found", {}, None)
        return pages[p]

    sw.c._get_page = page
    assert sw._next_event_page(NOW)
    assert not sw._next_event_page(NOW)  # 1 回読んだら、終わるまで読まない
    sw._pages.clear()
    assert sw._next_event_page(NOW + 3 * DAY)  # 終わったあとに、もう 1 回読む
    sw._pages.clear()
    assert not sw._next_event_page(NOW + 4 * DAY)
    with Store(tmp_path) as s:
        info = s.one("SELECT * FROM event_info WHERE event_id=7")
        page_row = s.one("SELECT * FROM event_pages WHERE event_id=7")
    assert info["parent_id"] == 3 and info["reward_rank"] == 5 and info["block"] == "All Livers"
    assert _json.loads(info["leagues"]) == [10, 20] and "image_m" not in _json.loads(info["data"])
    assert _json.loads(info["data"])["event_description"] == "説明"
    assert page_row["status"] == 200 and page_row["text"] == "大会\nポイントの数え方\n有料ギフト 1.5 倍"

    # お知らせ: ログイン状態が無ければ読まない
    assert sw._next_notices(NOW)
    with Store(tmp_path) as s:
        assert s.meta("notices_error") == "ログイン状態がありません"
    (tmp_path / "storage_state.json").write_text(_json.dumps({"cookies": [{"name": "sr", "value": "1", "domain": ".showroom-live.com"}]}))
    calls = []

    def get_json(path, cookie=None):
        calls.append(path)
        assert cookie == "sr=1"
        if path.startswith("/api/notices?"):
            return {"list": [{"notice_id": 2, "title": "メンテナンス", "label": "maintenance", "created_at": NOW},
                             {"notice_id": 1, "title": "新しいギフト", "created_at": NOW - DAY}], "current_page": 1, "last_page": 1}
        return {"notice_id": int(path.rsplit("/", 1)[1]), "contents": "<p>中身</p>"}

    monkeypatch.setattr(recorder, "get_json", get_json)
    with Store(tmp_path) as s:
        s.set_meta("notices_at", 0)
    assert sw._next_notices(NOW)
    with Store(tmp_path) as s:
        rows = s.all("SELECT notice_id, title, contents FROM notices ORDER BY notice_id")
        assert [tuple(r) for r in rows] == [(1, "新しいギフト", "<p>中身</p>"), (2, "メンテナンス", "<p>中身</p>")]
        assert s.meta("notices_error") is None
        s.set_meta("notices_at", 0)
    calls.clear()
    assert sw._next_notices(NOW)
    assert calls == ["/api/notices?count=50&page=1"]  # 前に読んだものばかりなら、中身は読まない


def test_rankings_wait_for_the_official_result(tmp_path):
    """正式な結果 (終わった次の日の正午) が出てから、貢献ランキングを取り、参加ルームを読み直す。"""
    end = datetime(2026, 10, 18, 21, 59, 59, tzinfo=JST).timestamp()
    with Store(tmp_path) as s:
        s.upsert_events([{"event_id": 1, "start": end - 7 * 86400, "end": end}])
        s.set_event_rooms(1, [{"room_id": 5, "point": 100, "rank": 1}], end + 7200)

        def pending(now):
            return s.scalar(f"SELECT COUNT(*) FROM room_events re JOIN events e USING(event_id) WHERE {research.PENDING_RANKING}",
                            (now - 3600, research.RANKING_RETRIES))

        assert pending(end + 7200) == 0
        assert pending(datetime(2026, 10, 19, 12, 30, tzinfo=JST).timestamp()) == 0  # 正午すぎ 1 時間は待つ
        assert pending(datetime(2026, 10, 19, 13, 1, tzinfo=JST).timestamp()) == 1
    sw = sweep.Sweeper(tmp_path, collector=research.Collector(tmp_path, get=lambda p: {}, throttle=quiet_throttle()), scan=False)
    read = []
    sw._read_event_rooms = lambda eid, now: read.append(eid) or []
    assert not sw._next_event_rooms(end + 7200 + 60)
    assert sw._next_event_rooms(datetime(2026, 10, 19, 13, 1, tzinfo=JST).timestamp()) and read == [1]


def test_fans_history_reads_past_months_backwards(tmp_path):
    now = datetime(2026, 10, 18, 12, tzinfo=JST).timestamp()
    asked = []

    def get(path):
        asked.append(path)
        ym = path.split("ym=")[1].split("&")[0]
        if ym >= "202607":
            return {"fan_power": 100, "total_user_count": 1, "users": [{"user_id": 5, "level": 9, "user_name": "常連"}]}
        return {"fan_power": 0, "total_user_count": 0, "users": []}  # 2026-06 より前はルームが無い

    sw = sweep.Sweeper(tmp_path, collector=research.Collector(tmp_path, get=get, throttle=quiet_throttle()), scan=False)
    with Store(tmp_path) as s:
        s.upsert_rooms([{"room_id": 1, "key": "a", "name": "A", "followers": 10, "live_at": now}], "live", now)
        s.set_active_fans(1, "202610", 100, 1, [{"user_id": 5, "level": 9}], now - 100)  # 今の月はふつうに読んである
    while sw._next_fans_history(now):
        sw._fan_history.clear()
    yms = [p.split("ym=")[1].split("&")[0] for p in asked]
    assert yms == ["202609", "202608", "202607", "202606", "202605", "202604"]  # 3 か月続けて 0 人でやめる
    with Store(tmp_path) as s:
        assert s.one("SELECT fans_back FROM rooms WHERE room_id=1")[0] == "done"
        assert s.scalar("SELECT COUNT(*) FROM active_fans WHERE room_id=1") == 4  # 10・9・8・7 月
        assert s.one("SELECT fans_at FROM rooms WHERE room_id=1")[0] == now - 100  # 前の月を読んでも、読み直す時期は変えない


def test_room_profile_is_read_weekly_and_each_month(tmp_path):
    now = datetime(2026, 10, 18, 12, tzinfo=JST).timestamp()
    prof = {"show_rank_subdivided": "B-5", "prev_show_rank_subdivided": "B-4", "league_id": 30, "next_league_id": 20,
            "room_grade_after_aggregation": "Gold", "live_continuous_days": 908, "broadcast_style": 1, "is_official": False,
            "follower_num": 1143, "image": "x.png", "banner_list": []}
    sw = sweep.Sweeper(tmp_path, collector=research.Collector(tmp_path, get=lambda p: prof, throttle=quiet_throttle()), scan=False)
    with Store(tmp_path) as s:
        s.upsert_rooms([{"room_id": 1, "key": "a", "name": "A", "followers": 10, "live_at": now}], "live", now)
    assert sw._next_room_profile(now)
    sw._profiles.clear()
    assert not sw._next_room_profile(now + 86400)  # 1 週間は読み直さない
    sw._profiles.clear()
    assert sw._next_room_profile(datetime(2026, 11, 1, 1, tzinfo=JST).timestamp())  # 月が替わったら読み直す
    with Store(tmp_path) as s:
        r = s.one("SELECT * FROM room_info WHERE room_id=1")
        assert r["show_rank"] == "B-5" and r["league_id"] == 30 and r["next_league_id"] == 20 and r["continuous_days"] == 908
        assert "image" not in json.loads(r["data"])
        assert [x[0] for x in s.all("SELECT ym FROM room_rank_month WHERE room_id=1 ORDER BY ym")] == ["202610", "202611"]
        assert s.one("SELECT followers FROM rooms WHERE room_id=1")[0] == 1143
