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
