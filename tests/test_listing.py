import json
import time

from showroom_keeper import listing
from showroom_keeper.gifts import record_gift


def _setup(tmp_path, monkeypatch):
    # 他のテストの読み込み結果を持ち越さない
    monkeypatch.setattr(listing, "_names_cache", None)
    monkeypatch.setattr(listing, "_file_cache", {})
    monkeypatch.setattr(listing, "_event_cache", None)
    monkeypatch.setattr(listing, "_live_cache", None)
    monkeypatch.setattr(listing, "_search_cache", {})
    monkeypatch.setattr(listing, "_user_cache", {})
    monkeypatch.setattr(listing, "_name_misses", {})
    monkeypatch.setattr(listing, "gift_catalog", lambda state_dir, rooms=None: {
        1: {"gift_name": "星", "point": 1, "free": True}, 100: {"gift_name": "花火", "point": 100, "free": False}})
    now = time.time()
    record_gift(tmp_path, "room_a", 1, {"t": 2, "u": 7, "ac": "たろう", "g": 100, "n": 2, "created_at": now - 3600})
    record_gift(tmp_path, "room_a", 1, {"t": 2, "u": 8, "ac": "はなこ", "g": 1, "n": 10, "created_at": now - 60})
    record_gift(tmp_path, "room_b", 2, {"t": 2, "u": 7, "ac": "タロウ改", "g": 1, "n": 5, "created_at": now - 10 * 86400})
    return now


def test_room_names_are_remembered_only_when_changed(tmp_path, monkeypatch):
    monkeypatch.setattr(listing, "_names_cache", None)
    listing.remember_rooms(tmp_path, {"room_a": ("Aの部屋", 11)})
    path = tmp_path / "gifts" / "_rooms.json"
    first = path.stat().st_mtime_ns
    listing.remember_rooms(tmp_path, {"room_a": ("Aの部屋", 11)})  # 同じなら書き込まない
    assert path.stat().st_mtime_ns == first
    listing.remember_rooms(tmp_path, {"room_a": (None, None), "room_b": ("Bの部屋", 22)})  # 分からない値で消さない
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "room_a": {"name": "Aの部屋", "room_id": 11}, "room_b": {"name": "Bの部屋", "room_id": 22}}


def test_list_rooms_combines_settings_records_and_live(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    listing.remember_rooms(tmp_path, {"room_a": ("Aの部屋", 11)})
    rooms = {r["key"]: r for r in listing.list_rooms(
        tmp_path, {"rooms": ["watched"], "record_rooms": ["room_b"]}, live={"watched": "見てる部屋"})}
    assert set(rooms) == {"watched", "room_a", "room_b"}
    assert rooms["watched"]["is_live"] and rooms["watched"]["name"] == "見てる部屋" and rooms["watched"]["records"] == 0
    assert rooms["room_a"]["name"] == "Aの部屋" and rooms["room_a"]["records"] == 2
    assert rooms["room_b"]["tags"] == ["record", "has_records"]


def test_listeners_are_aggregated_across_rooms_with_period_and_room_filters(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    every = {u["user_id"]: u for u in listing.list_listeners(tmp_path)["listeners"]}
    taro = every[7]
    assert (taro["points"], taro["paid_points"], taro["free_points"], taro["times"], taro["room_count"]) == (205, 200, 5, 2, 2)
    assert taro["name"] == "たろう" and taro["names"] == ["たろう", "タロウ改"]  # 最近の名前が先
    week = {u["user_id"] for u in listing.list_listeners(tmp_path, days=7)["listeners"]}
    assert week == {7, 8}
    assert [u["points"] for u in listing.list_listeners(tmp_path, days=7)["listeners"]] == [200, 10]
    only_b = listing.list_listeners(tmp_path, room="room_b")["listeners"]
    assert [(u["user_id"], u["points"]) for u in only_b] == [(7, 5)]


def test_listener_detail_shows_every_room(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    d = listing.listener_detail(tmp_path, "7")
    assert d["found"] and d["total"]["points"] == 205
    assert [(r["key"], r["points"]) for r in d["rooms"]] == [("room_a", 200), ("room_b", 5)]
    assert d["history"][0]["gift"] == "花火" and len(d["history"]) == 2
    assert listing.listener_detail(tmp_path, "999")["found"] is False


def test_listener_detail_charts_show_when_where_and_how_much(tmp_path, monkeypatch):
    from datetime import datetime

    from showroom_keeper.status import JST

    _setup(tmp_path, monkeypatch)
    start = datetime(2026, 10, 12, 21, 0, tzinfo=JST).timestamp()  # 月曜 21 時
    record_gift(tmp_path, "room_c", 9, {"t": 2, "u": 99, "ac": "べつ", "g": 1, "n": 1, "created_at": start})
    record_gift(tmp_path, "room_c", 9, {"t": 2, "u": 5, "ac": "じろう", "g": 100, "n": 3, "created_at": start + 25 * 60})
    record_gift(tmp_path, "room_c", 9, {"t": 2, "u": 5, "ac": "じろう", "g": 1, "n": 10, "created_at": start + 5 * 3600})
    record_gift(tmp_path, "room_c", 10, {"t": 2, "u": 5, "ac": "じろう", "g": 1, "n": 2, "created_at": start + 2 * 86400})
    c = listing.listener_detail(tmp_path, 5)["charts"]
    assert [d["day"] for d in c["daily"]] == ["2026-10-12", "2026-10-13", "2026-10-14"]  # 送らなかった日も 0 で出す
    assert c["daily"][0] == {"day": "2026-10-12", "paid": 300, "free": 0, "rooms": {"room_c": 300}}
    assert c["daily"][1]["free"] == 10 and c["daily"][2]["free"] == 2
    assert c["heatmap"]["paid"][0][21] == 300 and c["heatmap"]["free"][1][2] == 10  # 26 時 = 火曜 2 時
    timing = {t["from"]: t for t in c["timing"]}
    assert timing[20]["paid"] == 1  # 他の人の最初のギフトから 25 分後
    assert timing[180]["free"] == 1  # 180 分以降はまとめる
    assert timing[0]["free"] == 1  # 別の配信では、その配信の最初のギフト
    sizes = {s["from"]: s for s in c["sizes"]}
    assert sizes[100]["paid"] == 1 and sizes[10]["free"] == 1 and sizes[1]["free"] == 1
    assert c["rooms"] == ["room_c"]


def test_events_list_marks_state_recording_and_cached_ranking(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    now = time.time()
    pages = {
        "/api/event/search?status=1&page=1": {"event_list": [
            {"event_url_key": "now", "event_id": 1, "event_name": "開催中", "started_at": now - 100, "ended_at": now + 100}],
            "next_page": 2},
        "/api/event/search?status=1&page=2": {"event_list": [], "next_page": None},
        "/api/event/search?status=3&page=1": {"event_list": [
            {"event_url_key": "soon", "event_id": 2, "event_name": "これから", "started_at": now + 100, "ended_at": now + 200}],
            "next_page": None},
        "/api/event/search?status=4&page=1": {"event_list": [
            {"event_url_key": "past", "event_id": 4, "event_name": "終わった", "started_at": now - 900, "ended_at": now - 800}],
            "next_page": None},
        "/api/event/mine/abstraction": {"event_id": 3, "title": "設定済み", "event_start_at": now - 300, "event_end_at": now - 200},
    }
    (tmp_path / "cache").mkdir()
    (tmp_path / "cache" / "contribution_1.json").write_text("{}", encoding="utf-8")
    events = {e["key"]: e for e in listing.list_events(tmp_path, ["mine"], get=pages.__getitem__)}
    assert (events["now"]["state"], events["soon"]["state"], events["mine"]["state"]) == ("ongoing", "upcoming", "ended")
    assert events["past"]["state"] == "ended" and events["past"]["title"] == "終わった"
    assert events["mine"]["recording"] and not events["now"]["recording"]
    assert events["now"]["official_cached"] and not events["soon"]["official_cached"]


def test_unknown_room_names_are_looked_up_once(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    calls = []

    def get(path):
        calls.append(path)
        if "offline_room" in path:
            return {"room_id": 33, "room_name": "配信していない部屋", "is_live": False}
        raise OSError("down")

    rooms = {r["key"]: r for r in listing.list_rooms(tmp_path, {"record_rooms": ["offline_room"]}, lookup=get)}
    assert rooms["offline_room"]["name"] == "配信していない部屋" and rooms["offline_room"]["room_id"] == 33
    assert rooms["room_a"]["name"] == "room_a"  # 取れなかったものは room_url_key のまま
    before = len(calls)
    listing.list_rooms(tmp_path, {"record_rooms": ["offline_room"]}, lookup=get)
    assert not any("offline_room" in c for c in calls[before:])  # 覚えた名前は問い合わせ直さない


def test_rooms_include_every_live_room_and_showroom_search(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    listing.remember_rooms(tmp_path, {"room_a": ("Aの部屋", 11)})
    calls = []

    def get(path):
        calls.append(path)
        if path == "/api/live/onlives":
            return {"onlives": [
                {"genre_id": 0, "genre_name": "人気", "lives": [
                    {"room_url_key": "live_x", "main_name": "配信中X", "room_id": 5, "view_num": 300, "follower_num": 50, "genre_id": 102}]},
                {"genre_id": 102, "genre_name": "アイドル", "lives": [
                    {"room_url_key": "live_x", "main_name": "配信中X", "room_id": 5, "view_num": 300, "follower_num": 50, "genre_id": 102},
                    {"room_url_key": "room_a", "main_name": "Aの部屋", "room_id": 11, "view_num": 10, "genre_id": 102}]}]}
        if path.startswith("/api/search/room?word="):
            return {"result": [{"room_url_key": "found", "name": "検索で見つかった部屋", "room_id": 9, "is_online": False,
                                "follower_num": 1000, "genre_name": "idol"}]}
        if path == "/api/room/status?room_url_key=typed_key":
            return {"room_id": 12, "room_name": "URLで入れた部屋"}
        raise OSError("down")

    plain = {r["key"] for r in listing.list_rooms(tmp_path, {}, get=get)}
    assert plain == {"room_a", "room_b"} and not calls  # showroom を付けない時は SHOWROOM に問い合わせない
    rooms = {r["key"]: r for r in listing.list_rooms(tmp_path, {}, showroom=True, get=get)}
    x = rooms["live_x"]
    assert x["is_live"] and (x["viewers"], x["followers"], x["genre"]) == (300, 50, "アイドル") and not x["registered"]
    assert rooms["room_a"]["is_live"] and rooms["room_a"]["records"] == 2 and rooms["room_a"]["name"] == "Aの部屋"
    found = {r["key"]: r for r in listing.list_rooms(tmp_path, {}, showroom=True, q="検索", get=get)}
    assert found["found"]["hit"] and found["found"]["followers"] == 1000 and not found["live_x"]["hit"]
    typed = {r["key"]: r for r in listing.list_rooms(tmp_path, {}, showroom=True, q="https://www.showroom-live.com/r/typed_key",
                                                     lookup=get, get=get)}
    assert typed["typed_key"]["hit"] and typed["typed_key"]["name"] == "URLで入れた部屋"


def test_rooms_still_listed_when_showroom_is_down(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)

    def down(path):
        raise OSError("down")

    rooms = {r["key"] for r in listing.list_rooms(tmp_path, {"rooms": ["w"]}, showroom=True, q="なにか", get=down)}
    assert rooms == {"w", "room_a", "room_b"}


def test_lookup_user_by_id(monkeypatch):
    import urllib.error

    monkeypatch.setattr(listing, "_user_cache", {})
    calls = []

    def get(path):
        calls.append(path)
        if path.endswith("=5"):
            return {"name": "みる", "avatar_url": "https://static.showroom-live.com/a.png", "fan_level": 30, "description": "こんにちは"}
        raise urllib.error.HTTPError(path, 404, "Not Found", {}, None)

    assert listing.lookup_user("5", get=get) == {"user_id": 5, "name": "みる", "avatar": "https://static.showroom-live.com/a.png",
                                                 "level": 30, "description": "こんにちは"}
    assert listing.lookup_user(404, get=get) is None
    listing.lookup_user(5, get=get)
    assert len(calls) == 2  # 調べた結果は使い回す


def test_gift_catalog_merges_every_room_and_reuses_fetched_rooms(tmp_path, monkeypatch):
    monkeypatch.setattr(listing, "_names_cache", None)
    listing.remember_rooms(tmp_path, {"room_a": ("A", 11), "room_b": ("B", 22)})
    calls = []
    lists = {11: {1: {"gift_name": "星", "point": 1, "free": True}},
             22: {1: {"gift_name": "星", "point": 1, "free": True}, 500: {"gift_name": "B だけ", "point": 500, "free": False}}}

    def fetch(room_id):
        calls.append(room_id)
        return lists[room_id]

    monkeypatch.setattr(listing, "fetch_catalog", fetch)
    assert set(listing.gift_catalog(tmp_path, ["room_a"])) == {1}
    assert set(listing.gift_catalog(tmp_path, ["room_a", "room_b"])) == {1, 500}  # ルームだけのギフトも数える
    assert calls == [11, 22]  # 取ったルームは取り直さない
    assert set(listing.gift_catalog(tmp_path, ["room_a"])) == {1, 500} and calls == [11, 22]


def test_gift_catalog_retries_failed_room_only_after_a_day(tmp_path, monkeypatch):
    monkeypatch.setattr(listing, "_names_cache", None)
    listing.remember_rooms(tmp_path, {"room_a": ("A", 11)})
    calls = []

    def down(room_id):
        calls.append(room_id)
        raise OSError("down")

    monkeypatch.setattr(listing, "fetch_catalog", down)
    assert listing.gift_catalog(tmp_path, ["room_a"]) == {}
    listing.gift_catalog(tmp_path, ["room_a"])
    assert calls == [11]


def test_missing_rooms_are_not_looked_up_every_time(tmp_path, monkeypatch):
    import urllib.error

    monkeypatch.setattr(listing, "_names_cache", None)
    monkeypatch.setattr(listing, "_name_misses", {})
    calls = []

    def get(path):
        calls.append(path)
        raise urllib.error.HTTPError(path, 404, "Not Found", {}, None)

    listing.fill_unknown_names(tmp_path, ["gone"], get=get)
    listing.fill_unknown_names(tmp_path, ["gone"], get=get)
    assert len(calls) == 1
    monkeypatch.setattr(listing, "_name_misses", {"gone": time.time() - 1})  # 時間が経てば問い合わせ直す
    listing.fill_unknown_names(tmp_path, ["gone"], get=get)
    assert len(calls) == 2


def test_room_names_written_by_another_process_are_kept(tmp_path, monkeypatch):
    import os

    monkeypatch.setattr(listing, "_names_cache", None)
    listing.remember_rooms(tmp_path, {"room_a": ("A", 11)})
    path = tmp_path / "gifts" / "_rooms.json"
    path.write_text(json.dumps({"room_a": {"name": "A", "room_id": 11}, "other": {"name": "別", "room_id": 5}}),
                    encoding="utf-8")
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))  # 別のプロセスが後から書いた
    listing.remember_rooms(tmp_path, {"room_b": ("B", 22)})
    assert set(json.loads(path.read_text(encoding="utf-8"))) == {"room_a", "other", "room_b"}
