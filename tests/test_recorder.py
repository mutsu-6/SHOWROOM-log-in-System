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

    async def fake_aget(path):
        return responses[path]

    monkeypatch.setattr(rec, "aget", fake_aget)
    cfg = Config(rooms=["watched"], record_rooms=["solo"], events=["ev", "old"], state_file=tmp_path / "s.json")
    targets = await rec.GiftRecorder(cfg)._targets()
    assert set(targets) == {"solo", "a"}  # 配信していない b、視聴中の watched、終了イベントは対象外


async def test_record_room_saves_gifts_until_live_end(monkeypatch, tmp_path):
    async def fake_aget(path):
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
