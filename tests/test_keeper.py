import asyncio

import pytest

from showroom_keeper import keeper as kp
from showroom_keeper.config import Config


def test_keeper_requires_some_target():
    with pytest.raises(ValueError):
        kp.Keeper(Config(web_port=0))


async def test_record_only_runs_without_browser(monkeypatch):
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
    keeper = kp.Keeper(Config(record_rooms=["a"], web_port=0))
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


def test_login_is_checked_with_a_single_room_preferring_a_live_one():
    from showroom_keeper.api import RoomStatus

    def w(room_id, live):
        return type("W", (), {"status": RoomStatus(f"k{room_id}", room_id, "", live)})()

    none = type("W", (), {"status": None})()
    assert kp.login_room_ids([w(1, False), w(2, True), w(3, True), none]) == [2]
    assert kp.login_room_ids([none, w(5, False)]) == [5]
    assert kp.login_room_ids([none]) == []


async def test_login_check_runs_on_its_own_interval_while_state_is_saved_often(monkeypatch, tmp_path):
    from showroom_keeper.api import RoomStatus

    clock = {"t": 0.0}
    monkeypatch.setattr(kp.time, "monotonic", lambda: clock["t"])

    async def fake_sleep(seconds):
        clock["t"] += seconds
        if clock["t"] > 4 * 3600 + 1:
            raise asyncio.CancelledError

    monkeypatch.setattr(kp.asyncio, "sleep", fake_sleep)
    saves, checks = [], []

    async def fake_save(ctx, cfg):
        saves.append(clock["t"])

    monkeypatch.setattr(kp, "save_state", fake_save)

    class API:
        async def is_logged_in(self, room_id):
            checks.append((clock["t"], room_id))
            return True

    cfg = Config(rooms=["a", "b"], state_file=tmp_path / "s.json")  # 既定: 確認 2 時間 / 保存 10 分
    keeper = kp.Keeper(cfg)
    watchers = [type("W", (), {"status": RoomStatus(k, i, "", True)})() for i, k in ((11, "a"), (12, "b"))]
    with pytest.raises(asyncio.CancelledError):
        await keeper._periodic_save(None, API(), watchers)
    assert [t for t, _ in checks] == [7200, 14400]  # 2 時間ごと
    assert {room for _, room in checks} == {11}  # 問い合わせるのは 1 ルームだけ
    assert len(saves) == 24 and saves[0] == 600  # セッションの保存は 10 分ごとのまま


def test_status_warns_only_when_checks_stop_for_twice_the_interval():
    from showroom_keeper.status import format_login

    stat = {"logged_in": True, "checked_at": 1000.0, "check_interval": 7200}
    assert "途絶え" not in format_login(stat, now=1000 + 3 * 3600)
    assert "途絶え" in format_login(stat, now=1000 + 5 * 3600)
    assert "途絶え" in format_login({"logged_in": True, "checked_at": 1000.0}, now=1000 + 700)  # 以前の記録 (3 分間隔)
