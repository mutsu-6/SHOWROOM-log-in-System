import json
import time

from showroom_keeper.config import Config
from showroom_keeper.gifts import load_recorded
from showroom_keeper import recorder as rec


async def test_targets_skip_watched_ended_and_offline(monkeypatch, tmp_path):
    now = time.time()
    responses = {
        "/api/room/status?room_url_key=solo": {"is_live": True, "room_id": 1},
        "/api/event/ev/abstraction": {"title": "大会", "event_start_at": now - 10, "event_end_at": now + 1000},
        "/api/event/ev/rooms?page=1": {"rooms": [
            {"room_url_key": "a", "room_id": 2, "is_on_live": True},
            {"room_url_key": "b", "room_id": 3, "is_on_live": False},
            {"room_url_key": "watched", "room_id": 4, "is_on_live": True},
        ], "next_page": None},
        "/api/event/old/abstraction": {"title": "終了済", "event_start_at": now - 9999, "event_end_at": now - 5000},
    }

    async def fake_aget(path, cookie=None):
        return responses[path]

    monkeypatch.setattr(rec, "aget", fake_aget)
    cfg = Config(rooms=["watched"], record_rooms=["solo"], events=["ev", "old"], state_file=tmp_path / "s.json")
    targets = await rec.GiftRecorder(cfg)._targets()
    assert set(targets) == {"solo", "a"}  # 配信していない b、視聴中の watched、終了イベントは対象外


def test_session_cookie_uses_showroom_cookies_only(tmp_path):
    path = tmp_path / "s.json"
    path.write_text(json.dumps({"cookies": [
        {"name": "sr", "value": "1", "domain": ".showroom-live.com"},
        {"name": "ok", "value": "2", "domain": "www.showroom-live.com", "expires": -1},
        {"name": "other", "value": "2", "domain": "example.com"},
        {"name": "old", "value": "3", "domain": ".showroom-live.com", "expires": 1},
        {"name": "bad", "value": "a\nb", "domain": ".showroom-live.com"},
        {"name": "foreign", "value": "4", "domain": "notshowroom-live.com"},
    ]}), encoding="utf-8")
    assert rec.session_cookie(path, now=1_000) == "sr=1; ok=2"
    assert rec.session_cookie(tmp_path / "missing.json") is None
    assert rec.next_page_number(1, "2") == 2
    assert rec.next_page_number(2, 2) is None
    assert rec.next_page_number(2, "nope") is None


async def test_broken_room_or_event_does_not_drop_the_others(monkeypatch, tmp_path):
    now = time.time()
    responses = {
        "/api/room/status?room_url_key=good": {"is_live": True, "room_id": 1},
        "/api/room/status?room_url_key=bad": {"is_live": True},
        "/api/room/status?room_url_key=weird": ["nope"],
        "/api/event/ok/abstraction": {"title": "開催中", "event_start_at": now - 10, "event_end_at": now + 1000},
        "/api/event/ok/rooms?page=1": {"rooms": [{"room_url_key": "live", "room_id": 8, "is_on_live": True}], "next_page": "2"},
        "/api/event/ok/rooms?page=2": {"rooms": [{"room_url_key": "more", "room_id": 9, "is_on_live": True}], "next_page": 2},
        "/api/event/bad/abstraction": RuntimeError("down"),
    }

    async def fake_aget(path, cookie=None):
        data = responses[path]
        if isinstance(data, Exception):
            raise data
        return data

    monkeypatch.setattr(rec, "aget", fake_aget)
    cfg = Config(record_rooms=["good", "bad", "weird"], events=["bad", "ok"], state_file=tmp_path / "s.json")
    targets = await rec.GiftRecorder(cfg)._targets()
    assert set(targets) == {"good", "live", "more"}


async def test_refresh_restarts_when_the_live_changes(tmp_path):
    import asyncio

    started = []

    async def hang(key, room_id):
        started.append(room_id)
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            started.append(("cancel", room_id))
            raise

    recorder = rec.GiftRecorder(Config(state_file=tmp_path / "s.json"))
    box = {"a": {"room_id": 1, "source": "ルーム指定"}}

    async def targets():
        return dict(box)

    recorder._targets = targets
    recorder._record_room = hang
    await recorder._refresh()
    await asyncio.sleep(0)
    await recorder._refresh()
    assert started == [1]
    box["a"] = {"room_id": 2, "source": "ルーム指定"}
    await recorder._refresh()
    await asyncio.sleep(0)
    assert started[0] == 1 and started.count(1) == 1
    assert ("cancel", 1) in started and 2 in started
    for _, task in recorder._tasks.values():
        task.cancel()
    await asyncio.sleep(0)


async def test_event_rooms_follow_every_page(monkeypatch):
    pages = {
        "/api/event/ev/rooms?page=1": {"rooms": [{"room_url_key": "a"}], "next_page": 2},
        "/api/event/ev/rooms?page=2": {"rooms": [{"room_url_key": "b"}], "next_page": None},
    }

    async def fake_aget(path, cookie=None):
        assert cookie == "sr=1"
        return pages[path]

    monkeypatch.setattr(rec, "aget", fake_aget)
    rooms = await rec.event_rooms("ev", "sr=1")
    assert [r["room_url_key"] for r in rooms] == ["a", "b"]


async def test_record_room_saves_gifts_until_live_end(monkeypatch, tmp_path):
    async def fake_aget(path, cookie=None):
        return {"live_status": 2, "bcsvr_key": "k:1", "live_id": 55, "room_name": "部屋"}

    sent = []

    class FakeWS:
        def __init__(self):
            self.msgs = iter([
                "ACK\tshowroom",
                "MSG\tk:1\t" + json.dumps({"t": 2, "u": 7, "ac": "A", "g": 1, "n": 5, "created_at": 1}),
                "MSG\tk:1\t" + json.dumps({"t": 1, "u": 8, "cm": "hi"}),
                "MSG\tk:1\t" + json.dumps({"t": 101}),
                "MSG\tk:1\t" + json.dumps({"t": 2, "u": 9, "ac": "B", "g": 1, "n": 1, "created_at": 2}),  # 終了後
            ])

        async def send(self, m):
            sent.append(m)

        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                return next(self.msgs)
            except StopIteration:
                raise StopAsyncIteration

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    import websockets

    monkeypatch.setattr(rec, "aget", fake_aget)
    monkeypatch.setattr(websockets, "connect", lambda *a, **k: FakeWS())
    r = rec.GiftRecorder(Config(state_file=tmp_path / "s.json"))
    await r._record_room("room", 1)
    got = load_recorded(tmp_path, "room")
    assert [(g["user_id"], g["num"], g["live_id"]) for g in got] == [(7, 5, 55)]
    assert sent == ["SUB\tk:1"]
