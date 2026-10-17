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
    keeper = kp.Keeper(Config(enter_rooms=["x"], web_port=0, state_file=tmp_path / "s.json"))
    api = FakeAPI({}, live_room=99)
    assert await keeper._login_rooms(api, []) == [99]
    assert await keeper._login_rooms(api, []) == [99]
    assert api.live_calls == 1
    assert await keeper._login_rooms(api, [5]) == [5]


async def test_failed_room_list_does_not_relogin(tmp_path, monkeypatch):
    keeper = kp.Keeper(Config(enter_rooms=["x"], web_port=0, state_file=tmp_path / "s.json"))

    async def no_login(*args):
        raise AssertionError("ログイン状態が分からないだけで再ログインしない")

    monkeypatch.setattr(kp, "ensure_login", no_login)
    api = FakeAPI({}, live_room=None)
    assert await keeper._check_login(None, api, [], True) is None
    assert api.checked == []


async def test_enter_rooms_alone_start_the_browser(tmp_path, monkeypatch):
    started = asyncio.Event()
    keeper = kp.Keeper(Config(enter_rooms=["x"], web_port=0, state_file=tmp_path / "s.json"))

    async def fake_loop():
        started.set()
        await keeper._wake.wait()

    monkeypatch.setattr(keeper, "_run_loop", fake_loop)
    task = asyncio.ensure_future(keeper.run())
    await asyncio.wait_for(started.wait(), timeout=1)
    keeper.stop()
    await asyncio.wait_for(task, timeout=1)


async def test_enter_all_alone_starts_the_browser(tmp_path, monkeypatch):
    started = asyncio.Event()
    keeper = kp.Keeper(Config(enter_all=True, web_port=0, state_file=tmp_path / "s.json"))

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
    # 保存済みセッションとログイン情報があっても、メモリを使うブラウザは起動しない
    (tmp_path / "s.json").write_text("{}")
    keeper = kp.Keeper(Config(record_rooms=["a"], credentials=Credentials("id", "pw"), web_port=0,
                              state_file=tmp_path / "s.json"))
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


def test_narrow_streaming_urls_keeps_only_requested_quality():
    from showroom_keeper.keeper import VIDEO_QUALITY, narrow_streaming_urls

    data = {"streaming_url_list": [
        {"id": 200, "is_default": True, "quality": 0, "type": "webrtc", "url": "webrtc://x/main"},
        {"id": 203, "is_default": False, "quality": 100, "type": "webrtc", "url": "webrtc://x/main_ll"},
        {"id": 2, "is_default": False, "quality": 1000, "type": "hls", "url": "https://x/main_ss.m3u8"},
        {"id": 4, "is_default": False, "quality": 100, "type": "hls", "url": "https://x/main_ll.m3u8"},
    ]}
    low = narrow_streaming_urls(data, VIDEO_QUALITY["low"])["streaming_url_list"]
    assert [e["id"] for e in low] == [203, 4] and [e["is_default"] for e in low] == [True, False]
    assert len(data["streaming_url_list"]) == 4  # 元の応答は書き換えない
    assert narrow_streaming_urls(data, VIDEO_QUALITY["medium"]) is data  # その画質が無ければそのまま
    assert narrow_streaming_urls({"errors": []}, 100) == {"errors": []}


async def test_page_factory_keeps_one_blank_page_in_front():
    from showroom_keeper.config import Config
    from showroom_keeper.keeper import page_factory

    class FakePage:
        def __init__(self, ctx):
            self.ctx, self.closed = ctx, False

        def is_closed(self):
            return self.closed

        async def bring_to_front(self):
            self.ctx.front = self

    class FakeContext:
        def __init__(self):
            self.pages, self.front = [], None

        async def new_page(self):
            page = FakePage(self)
            self.pages.append(page)
            self.front = page  # 新しいページは前面に出る
            return page

    ctx = FakeContext()
    new_page = page_factory(ctx, Config())
    a = await new_page()
    b = await new_page()
    assert len(ctx.pages) == 3 and ctx.front not in (a, b)  # 空白のページ 1 つが前面
    off = FakeContext()
    page = await page_factory(off, Config(background_tabs=False))()
    assert off.pages == [page] and off.front is page
