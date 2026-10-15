import asyncio

import pytest

from showroom_keeper import keeper as kp
from showroom_keeper.config import Config, Credentials


def test_keeper_requires_some_target(tmp_path):
    with pytest.raises(ValueError):
        kp.Keeper(Config(web_port=0, state_file=tmp_path / "none.json"))


class FakeAPI:
    def __init__(self, states, live_room=None):
        self.states, self.live_room, self.checked, self.live_calls = states, live_room, [], 0

    async def is_logged_in(self, room_id):
        self.checked.append(room_id)
        return self.states.get(room_id)

    async def live_room_id(self):
        self.live_calls += 1
        return self.live_room


async def test_login_is_checked_with_one_room():
    # ログインはアカウント単位なので、判定できた 1 ルームで決めて残りは問い合わせない
    api = FakeAPI({1: True, 2: False, 3: False})
    assert await kp.probe_login(api, [1, 2, 3]) is True
    assert api.checked == [1]
    # 問い合わせに失敗したルームだけ次を試す
    api = FakeAPI({2: False})
    assert await kp.probe_login(api, [1, 2, 3]) is False
    assert api.checked == [1, 2]
    assert await kp.probe_login(FakeAPI({}), []) is None


async def test_without_watch_rooms_one_live_room_is_reused(tmp_path):
    keeper = kp.Keeper(Config(credentials=Credentials("id", "pw"), web_port=0,
                              state_file=tmp_path / "s.json"))
    api = FakeAPI({}, live_room=99)
    assert await keeper._login_rooms(api, []) == [99]
    assert await keeper._login_rooms(api, []) == [99]
    assert api.live_calls == 1
    assert await keeper._login_rooms(api, [5]) == [5]


async def test_failed_room_list_does_not_relogin(tmp_path, monkeypatch):
    keeper = kp.Keeper(Config(credentials=Credentials("id", "pw"), web_port=0,
                              state_file=tmp_path / "s.json"))

    async def no_login(*args):
        raise AssertionError("ログイン状態が分からないだけで再ログインしない")

    monkeypatch.setattr(kp, "ensure_login", no_login)
    api = FakeAPI({}, live_room=None)
    assert await keeper._check_login(None, api, [], True) is None
    assert api.checked == []


async def test_login_only_starts_browser_without_rooms(tmp_path, monkeypatch):
    started = asyncio.Event()
    keeper = kp.Keeper(Config(credentials=Credentials("id", "pw"), web_port=0,
                              state_file=tmp_path / "s.json"))

    async def fake_loop():
        started.set()
        await keeper._wake.wait()

    monkeypatch.setattr(keeper, "_run_loop", fake_loop)
    task = asyncio.ensure_future(keeper.run())
    await asyncio.wait_for(started.wait(), timeout=1)
    keeper.stop()
    await asyncio.wait_for(task, timeout=1)


async def test_record_only_runs_without_browser(monkeypatch, tmp_path):
    started = asyncio.Event()

    class FakeRecorder:
        def __init__(self, cfg):
            pass

        async def run(self):
            started.set()
            await asyncio.Event().wait()

    def no_browser():
        raise AssertionError("ギフト記録だけの時はブラウザを起動しない")

    monkeypatch.setattr("showroom_keeper.recorder.GiftRecorder", FakeRecorder)
    monkeypatch.setattr(kp, "async_playwright", no_browser)
    keeper = kp.Keeper(Config(record_rooms=["a"], web_port=0, state_file=tmp_path / "none.json"))
    task = asyncio.ensure_future(keeper.run())
    await asyncio.wait_for(started.wait(), timeout=1)
    keeper.stop()
    await asyncio.wait_for(task, timeout=1)


async def test_launch_browser_leaves_sigint_to_keeper():
    calls = []

    class FakeChromium:
        async def launch(self, **kwargs):
            calls.append(kwargs)
            return type("B", (), {"version": "1"})()

    pw = type("PW", (), {"chromium": FakeChromium()})()
    await kp.launch_browser(pw, Config(rooms=["a"]))
    # Ctrl+C でブラウザが先に終了するとセッションを保存できない
    assert calls[0]["handle_sigint"] is False


def test_keep_open_is_decided_by_memory():
    rooms = ["a", "b", "c", "d", "e"]
    # 既定 (自動): 5 ルームは約 3GB 必要。2GB のサーバーでは配信中だけ開く
    assert kp.resolve_keep_open(Config(rooms=rooms), mem=1.9) is False
    assert kp.resolve_keep_open(Config(rooms=rooms), mem=4.0) is True
    assert kp.resolve_keep_open(Config(rooms=["a", "b"]), mem=1.9) is True
    # 明示した設定はそのまま使う
    assert kp.resolve_keep_open(Config(rooms=rooms, keep_open_when_offline=True), mem=1.9) is True
    assert kp.resolve_keep_open(Config(rooms=rooms, keep_open_when_offline=False), mem=64) is False
