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


def _recorder_with_fake_tasks(tmp_path, box, statuses=None):
    import asyncio

    started = []

    async def hang(key, room_id):
        started.append((key, box[key]["live_id"] if key in box else None))
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            started.append(("cancel", key))
            raise

    recorder = rec.GiftRecorder(Config(state_file=tmp_path / "s.json"))

    async def targets():
        recorder._incomplete = box.get("_incomplete", False)
        return {k: dict(v) for k, v in box.items() if not k.startswith("_")}

    async def room_status(key, cookie):
        return (statuses or {}).get(key)

    recorder._targets = targets
    recorder._record_room = hang
    recorder._room_status = room_status
    return recorder, started


async def _cleanup(recorder):
    import asyncio

    for task in recorder._tasks.values():
        task.cancel()
    await asyncio.sleep(0)


async def test_refresh_reconnects_when_the_next_live_starts(tmp_path):
    import asyncio

    # 同じルームでも配信が変われば live_id が変わる (room_id は変わらない)
    box = {"a": {"room_id": 1, "live_id": 100, "source": "ルーム指定"}}
    recorder, started = _recorder_with_fake_tasks(tmp_path, box)
    await recorder._refresh()
    await asyncio.sleep(0)
    await recorder._refresh()
    assert started == [("a", 100)]
    box["a"]["live_id"] = 200
    await recorder._refresh()
    await asyncio.sleep(0)
    assert started == [("a", 100), ("cancel", "a"), ("a", 200)]
    await _cleanup(recorder)


async def test_event_room_live_id_is_checked_while_recording(tmp_path):
    import asyncio

    box = {"e": {"room_id": 5, "live_id": None, "source": "大会"}}
    statuses = {"e": {"live_id": 7}}
    recorder, started = _recorder_with_fake_tasks(tmp_path, box, statuses)
    await recorder._refresh()
    await asyncio.sleep(0)
    recorder._live_ids["e"] = 7  # 記録開始時に live_info から分かった値
    await recorder._refresh()
    assert started == [("e", None)]
    statuses["e"] = {"live_id": 8}
    await recorder._refresh()
    await asyncio.sleep(0)
    assert ("cancel", "e") in started and started.count(("e", None)) == 2
    await _cleanup(recorder)


async def test_refresh_stops_rooms_confirmed_offline_but_not_on_fetch_errors(tmp_path):
    import asyncio

    box = {"a": {"room_id": 1, "live_id": 100, "source": "ルーム指定"}}
    recorder, started = _recorder_with_fake_tasks(tmp_path, box)
    await recorder._refresh()
    await asyncio.sleep(0)
    # 取得に失敗して対象から外れただけの時は記録を続ける
    box.clear()
    box["_incomplete"] = True
    await recorder._refresh()
    await asyncio.sleep(0)
    assert ("cancel", "a") not in started and "a" in recorder._tasks
    # 配信していないと確認できたら止める (配信終了のお知らせを受け損ねた場合)
    box["_incomplete"] = False
    await recorder._refresh()
    await asyncio.sleep(0)
    assert ("cancel", "a") in started and "a" not in recorder._tasks
    await _cleanup(recorder)


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


async def test_glitter_rooms_are_recorded_for_a_week(monkeypatch, tmp_path):
    now = time.time()
    badge = [{"type": "show_grade", "id": 1}, {"type": "glitter", "id": 2}]
    lives = [{"room_url_key": "d", "room_id": 1, "live_id": 11, "badge_list": badge},
             {"room_url_key": "plain", "room_id": 2, "live_id": 12, "badge_list": [{"type": "show_grade"}]},
             {"room_url_key": "before", "room_id": 3, "live_id": 13, "badge_list": []},
             {"room_url_key": "watched", "room_id": 4, "live_id": 14, "badge_list": badge}]

    async def fake_aget(path, cookie=None):
        assert path == "/api/live/onlives"
        return {"onlives": [{"lives": lives}]}

    monkeypatch.setattr(rec, "aget", fake_aget)
    # 「before」は 3 日前にグリッターを取った (今日はまだバッジが無くても、配信の最初から記録する)。「gone」は 8 日前
    (tmp_path / rec.GLITTER_FILE).write_text(json.dumps({"before": now - 3 * 86400, "gone": now - 8 * 86400}))
    cfg = Config(rooms=["watched"], state_file=tmp_path / "s.json")
    r = rec.GiftRecorder(cfg)
    targets = await r._targets()
    assert set(targets) == {"d", "before"} and targets["d"]["live_id"] == 11 and targets["d"]["source"] == "グリッター"
    saved = json.loads((tmp_path / rec.GLITTER_FILE).read_text())
    assert set(saved) == {"d", "before", "watched"}  # 8 日前の分は忘れる
    cfg.record_glitter = False
    assert await rec.GiftRecorder(cfg)._targets() == {}


async def test_save_stage_ranks(monkeypatch, tmp_path):
    from showroom_keeper import stage_ranks

    async def fake_aget(path, cookie=None):
        assert path == "/api/live/stage_user_list?room_id=5"
        return {"stage_user_list": [{"rank": 1, "order_no": 1, "user": {"user_id": 9, "name": "x"}}, {"rank": 2, "user": {}}]}

    monkeypatch.setattr(rec, "aget", fake_aget)
    r = rec.GiftRecorder(Config(state_file=tmp_path / "s.json"))
    await r._save_stage("room", 5, 77)
    assert stage_ranks.last_per_live(tmp_path, "room")[77]["ranks"] == [[1, 9]]
