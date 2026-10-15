import pytest

from showroom_keeper.api import RoomStatus
from showroom_keeper.config import Config
from showroom_keeper.watcher import RoomWatcher


class FakePage:
    def __init__(self):
        self.closed = False
        self.gotos, self.reloads = [], 0
        self.video_times = []

    async def goto(self, url, **kw):
        self.gotos.append(url)

    async def reload(self, **kw):
        self.reloads += 1

    async def evaluate(self, js, arg=None):
        t = self.video_times.pop(0) if self.video_times else None
        return None if t is None else {"time": t, "paused": False, "ready": 4}

    async def close(self):
        self.closed = True

    def is_closed(self):
        return self.closed


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


@pytest.fixture
def env():
    cfg = Config(rooms=["k"], status_poll_interval=60, health_check_interval=30, stall_threshold=2, periodic_reload_minutes=10)
    live = {"v": True}
    pages = []
    clock = Clock()

    async def fetch(key):
        return RoomStatus(key, 1, "Room", live["v"])

    async def new_page():
        pages.append(FakePage())
        return pages[-1]

    w = RoomWatcher("k", cfg, fetch, new_page, clock=clock)
    return w, live, pages, clock


async def test_keeps_every_room_open_while_offline_and_reloads_when_live_starts(env):
    w, live, pages, clock = env
    live["v"] = False
    assert await w.step() == 60
    assert len(pages) == 1 and not pages[0].closed
    for i in range(1, 4):
        clock.t = i * 60
        await w.step()
    assert pages[0].reloads == 0 and w.page is pages[0]
    live["v"] = True
    clock.t = 240
    await w.step()
    assert pages[0].reloads == 1 and not pages[0].closed


async def test_stays_open_and_reloads_once_when_live_ends(env):
    w, live, pages, clock = env
    await w.step()
    live["v"] = False
    clock.t = 60
    await w.step()
    assert pages[0].reloads == 1 and not pages[0].closed and w.page is pages[0]
    clock.t = 120
    await w.step()
    assert pages[0].reloads == 1


async def test_offline_page_reloads_on_the_usual_interval(env):
    w, live, pages, clock = env
    live["v"] = False
    await w.step()
    clock.t = 601
    await w.step()
    assert pages[0].reloads == 1 and not pages[0].closed


async def test_activity_restarts_when_the_next_live_begins(env):
    w, live, pages, clock = env
    live_id = {"v": 11}

    async def fetch(key):
        return RoomStatus(key, 1, "Room", live["v"], live_id=live_id["v"] if live["v"] else None)

    w._fetch_status = fetch
    await w.step()
    w.activity.feed('MSG\tk\t{"t": 1, "u": 9}', 1000.0)
    assert w.activity.count(1000.0) == 1
    live["v"] = False
    clock.t = 60
    await w.step()
    live["v"] = True
    live_id["v"] = 22
    clock.t = 120
    await w.step()
    assert w._activity_live_id == 22
    assert w.activity.count(1000.0) == 0


async def test_reloads_open_room_after_relogin(env):
    w, live, pages, clock = env
    await w.step()
    w.reload_reason = "再ログイン"
    clock.t = 30
    await w.step()
    assert pages[0].reloads == 1 and not pages[0].closed


async def test_opens_page_when_live(env):
    w, live, pages, clock = env
    delay = await w.step()
    assert len(pages) == 1 and pages[0].gotos == ["https://www.showroom-live.com/r/k"]
    assert delay == 30


async def test_waits_when_offline_and_closes_page_after_live_ends(env):
    w, live, pages, clock = env
    w.cfg.keep_open_when_offline = False
    live["v"] = False
    assert await w.step() == 60 and not pages
    live["v"] = True
    clock.t = 60
    await w.step()
    assert len(pages) == 1
    live["v"] = False
    clock.t = 120
    await w.step()
    assert pages[0].closed and w.page is None


async def test_reload_on_stall_and_watch_time(env):
    w, live, pages, clock = env
    await w.step()
    page = pages[0]
    page.video_times = [10.0, 40.0, 40.0, 40.0]
    for i in range(1, 5):
        clock.t = i * 30
        await w.step()
    assert w.watched_seconds == pytest.approx(30.0)
    assert page.reloads == 1


async def test_reload_when_no_video(env):
    w, live, pages, clock = env
    await w.step()
    for i in range(1, 3):
        clock.t = i * 5
        await w.step()
    assert pages[0].reloads == 1


async def test_periodic_reload(env):
    w, live, pages, clock = env
    await w.step()
    pages[0].video_times = [float(i) for i in range(100)]
    clock.t = 601
    await w.step()
    assert pages[0].reloads == 1


async def test_reopens_crashed_page(env):
    w, live, pages, clock = env
    await w.step()
    pages[0].closed = True
    clock.t = 30
    await w.step()
    assert len(pages) == 2


async def test_errors_back_off_and_recover(env):
    w, live, pages, clock = env
    calls = {"n": 0}

    async def flaky(key):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise RuntimeError("network down")
        return RoomStatus(key, 1, "Room", True)

    w._fetch_status = flaky
    assert await w.step() == 5
    assert await w.step() == 10
    assert await w.step() == 30
    assert len(pages) == 1


async def test_blocked_room_does_not_reload(env):
    w, live, pages, clock = env
    await w.step()

    async def blocked(js, arg=None):
        return {"blocked": True}

    pages[0].evaluate = blocked
    for i in range(1, 6):
        clock.t = i * 30
        await w.step()
    assert w.blocked and pages[0].reloads == 0


async def test_post_comment_fills_submits_and_confirms(env):
    w, live, pages, clock = env
    await w.step()
    page = pages[0]
    actions = []

    async def fill(sel, value, **kw):
        actions.append(("fill", value))

    async def press(sel, key, **kw):
        actions.append(("press", key))

    checks = iter([False, True])

    async def evaluate(js, arg=None):
        return next(checks)

    async def no_sleep(s):
        pass

    page.fill, page.press, page.evaluate = fill, press, evaluate
    w._sleep = no_sleep
    ok, msg = await w.post_comment("こんにちは")
    assert ok and actions == [("fill", "こんにちは"), ("press", "Enter")]


async def test_post_comment_requires_open_live_page(env):
    w, live, pages, clock = env
    live["v"] = False
    await w.step()
    ok, msg = await w.post_comment("hi")
    assert not ok


async def test_viewer_count_is_fetched_and_saved(tmp_path):
    from showroom_keeper.status import read_live_stats

    cfg = Config(rooms=["k"], state_file=tmp_path / "s.json", viewer_count_interval=60)
    clock = Clock()
    responses = iter([
        {"status": 200, "data": {"viewer_count": 123}}, None,  # 30 秒: 視聴者数, 再生確認
        None,  # 60 秒: 再生確認のみ
        {"status": 401, "data": {"error_user_msg": "ログインが必要です。"}}, None,  # 95 秒
    ])

    async def fetch(key):
        return RoomStatus(key, 1, "Room", True, live_id=99)

    page = FakePage()

    async def evaluate(js, arg=None):
        return next(responses)

    page.evaluate = evaluate

    async def new_page():
        return page

    w = RoomWatcher("k", cfg, fetch, new_page, clock=clock)
    await w.step()  # ページを開く
    clock.t = 30
    await w.step()  # 視聴者数 (200) + 再生確認
    assert w.viewer_count == 123
    assert read_live_stats(tmp_path)["k"]["viewer_count"] == 123
    clock.t = 60
    await w.step()  # 60 秒未満なので視聴者数は取りに行かない (再生確認のみ)
    clock.t = 95
    await w.step()  # 401
    stat = read_live_stats(tmp_path)["k"]
    assert stat["viewer_count"] is None and "ログイン" in stat["viewer_count_error"]


async def test_classic_room_does_not_send_presence(tmp_path):
    from showroom_keeper.status import read_live_stats

    cfg = Config(rooms=["k"], state_file=tmp_path / "s.json")
    clock = Clock()
    calls = []

    async def fetch(key):
        return RoomStatus(key, 1, "Room", True, live_id=99)

    page = FakePage()

    async def evaluate(js, arg=None):
        calls.append(js)
        return {"style": "classic"} if "viewer_presence" in js else None

    page.evaluate = evaluate

    async def new_page():
        return page

    w = RoomWatcher("k", cfg, fetch, new_page, clock=clock)
    await w.step()
    clock.t = 30
    await w.step()
    stat = read_live_stats(tmp_path)["k"]
    assert stat["broadcast_style"] == "classic" and stat["viewer_count"] is None
    assert "active_users" in stat


async def test_websocket_gifts_are_recorded(tmp_path):
    import json as _json

    from showroom_keeper.gifts import load_recorded

    cfg = Config(rooms=["k"], state_file=tmp_path / "s.json")

    async def fetch(key):
        return RoomStatus(key, 1, "Room", True, live_id=99)

    async def new_page():
        return FakePage()

    w = RoomWatcher("k", cfg, fetch, new_page)
    await w.step()
    w._on_frame("MSG\tk\t" + _json.dumps({"t": 2, "u": 7, "ac": "A", "g": 1, "n": 3, "created_at": 5}))
    w._on_frame("MSG\tk\t" + _json.dumps({"t": 1, "u": 8, "cm": "hi"}))  # コメントは記録しない
    got = load_recorded(tmp_path, "k")
    assert len(got) == 1 and got[0]["num"] == 3 and got[0]["live_id"] == 99
    assert w.activity.count(10) == 2  # 推定アクティブ人数には両方入る
