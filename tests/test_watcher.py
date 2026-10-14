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


async def test_opens_page_when_live(env):
    w, live, pages, clock = env
    delay = await w.step()
    assert len(pages) == 1 and pages[0].gotos == ["https://www.showroom-live.com/r/k"]
    assert delay == 30


async def test_waits_when_offline_and_closes_page_after_live_ends(env):
    w, live, pages, clock = env
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

    async def click(sel, **kw):
        actions.append(("click", sel))

    checks = iter([False, True])

    async def evaluate(js, arg=None):
        return next(checks)

    async def no_sleep(s):
        pass

    page.fill, page.click, page.evaluate = fill, click, evaluate
    w._sleep = no_sleep
    ok, msg = await w.post_comment("こんにちは")
    assert ok and actions == [("fill", "こんにちは"), ("click", "button.st-comment__button")]


async def test_post_comment_requires_open_live_page(env):
    w, live, pages, clock = env
    live["v"] = False
    await w.step()
    ok, msg = await w.post_comment("hi")
    assert not ok
