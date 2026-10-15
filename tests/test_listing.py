import json
import time

from showroom_keeper import listing
from showroom_keeper.gifts import record_gift


def _setup(tmp_path, monkeypatch):
    # 他のテストの読み込み結果を持ち越さない
    monkeypatch.setattr(listing, "_names_cache", None)
    monkeypatch.setattr(listing, "_file_cache", {})
    monkeypatch.setattr(listing, "_event_cache", None)
    monkeypatch.setattr(listing, "gift_catalog", lambda state_dir: {
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


def test_events_list_marks_state_recording_and_cached_ranking(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    now = time.time()
    pages = {
        "/api/event/search?page=1": {"event_list": [
            {"event_url_key": "now", "event_id": 1, "event_name": "開催中", "started_at": now - 100, "ended_at": now + 100}],
            "next_page": 2},
        "/api/event/search?page=2": {"event_list": [
            {"event_url_key": "soon", "event_id": 2, "event_name": "これから", "started_at": now + 100, "ended_at": now + 200}],
            "next_page": None},
        "/api/event/mine/abstraction": {"event_id": 3, "title": "設定済み", "event_start_at": now - 300, "event_end_at": now - 200},
    }
    (tmp_path / "cache").mkdir()
    (tmp_path / "cache" / "contribution_1.json").write_text("{}", encoding="utf-8")
    events = {e["key"]: e for e in listing.list_events(tmp_path, ["mine"], get=pages.__getitem__)}
    assert (events["now"]["state"], events["soon"]["state"], events["mine"]["state"]) == ("ongoing", "upcoming", "ended")
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
