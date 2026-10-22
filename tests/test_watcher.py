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
        if "current_user" in js and "comment_log" not in js:  # 自分のユーザー ID (視聴中に先に取得する)
            return {"user_id": 1}
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
    w.comment_echo_wait = 0.01  # リアルタイム通信では届かず、コメント履歴で確かめる場合
    ok, msg = await w.post_comment("こんにちは")
    assert ok and actions == [("fill", "こんにちは"), ("press", "Enter")]


def _comment_frame(text, user):
    import json as _json
    return "MSG\tk\t" + _json.dumps({"t": 1, "cm": text, "u": user, "ac": "名前"}, ensure_ascii=False)


async def test_post_comment_is_confirmed_by_the_live_feed_right_away(env):
    import asyncio
    import time as _time

    w, live, pages, clock = env
    await w.step()
    page = pages[0]
    history_checks = []

    async def fill(sel, value, **kw):
        pass

    async def press(sel, key, **kw):
        # 送信すると、配信ページのリアルタイム通信に自分のコメントが届く (他の人の同じ文面も混ざる)
        loop = asyncio.get_running_loop()
        loop.call_soon(w._on_frame, _comment_frame("こんにちは", 99))
        loop.call_soon(w._on_frame, _comment_frame("こんにちは", 42))

    async def evaluate(js, arg=None):
        if "current_user" in js and "comment_log" not in js:
            return {"user_id": 42}
        history_checks.append(arg)
        return False

    page.fill, page.press, page.evaluate = fill, press, evaluate
    started = _time.monotonic()
    ok, msg = await w.post_comment("こんにちは")
    assert ok and msg == "投稿しました"
    assert _time.monotonic() - started < 1  # 遅れて載るコメント履歴を待たない
    assert history_checks == [] and w._comment_waiters == []


async def test_someone_elses_same_comment_does_not_count(env):
    import asyncio

    w, live, pages, clock = env
    await w.step()
    page = pages[0]

    async def fill(sel, value, **kw):
        pass

    async def press(sel, key, **kw):
        asyncio.get_running_loop().call_soon(w._on_frame, _comment_frame("こんにちは", 99))

    async def evaluate(js, arg=None):
        if "current_user" in js and "comment_log" not in js:
            return {"user_id": 42}
        return False

    async def no_sleep(s):
        pass

    page.fill, page.press, page.evaluate = fill, press, evaluate
    w._sleep = no_sleep
    w.comment_echo_wait = 0.05
    ok, msg = await w.post_comment("こんにちは")
    assert not ok and "確認できませんでした" in msg


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
        if "current_user" in js and "comment_log" not in js:
            return {"user_id": 1}
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


async def test_status_errors_do_not_close_a_working_page(env):
    w, live, pages, clock = env
    await w.step()
    assert len(pages) == 1

    async def down(key):
        raise TimeoutError("APIRequestContext.get: Timeout 15000ms exceeded.")

    w._fetch_status = down
    clock.t = 60
    await w.step()
    # 配信状況が分からないだけなので、ページは閉じずに前回の状況のまま視聴を続ける
    assert not pages[0].closed and w.page is pages[0] and w.status.is_live
    assert w._status_failures == 1


async def test_comment_reports_a_slow_page_clearly(env):
    w, live, pages, clock = env
    await w.step()

    async def slow_fill(*a, **k):
        raise TimeoutError("Page.fill: Timeout 30000ms exceeded.")

    pages[0].fill = slow_fill
    ok, message = await w.post_comment("こんにちは")
    assert ok is False and "メモリや CPU" in message


async def test_waits_without_opening_when_memory_is_short(env):
    from showroom_keeper.keeper import page_opener

    w, live, pages, clock = env
    other = type("Other", (), {"_page_alive": lambda self: True})()
    free = {"gb": 0.5}
    w.may_open = page_opener([other, w], available=lambda: free["gb"])
    assert await w.step() == 60
    assert pages == [] and w.waiting_memory  # ほかのページが開いていて空きが 0.5GB なので開かない
    free["gb"] = 1.0
    clock.t = 60
    await w.step()
    assert len(pages) == 1 and not w.waiting_memory


def test_first_page_always_opens_even_when_memory_is_short():
    from showroom_keeper.keeper import page_opener

    me = type("W", (), {"_page_alive": lambda self: False})()
    closed = type("W", (), {"_page_alive": lambda self: False})()
    assert page_opener([me, closed], available=lambda: 0.1)(me) is True
    assert page_opener([me, closed], available=lambda: None)(me) is True


async def test_avatar_on_stage_is_checked_and_reentered(tmp_path):
    """配信中に 10 分ごとに、観客席に自分のアバターがいるかを確かめる。いなければ (current_user で入り直したので) 少しあとに確かめ直す。
    ページがログインしていなければ、読み込み直す。"""
    from showroom_keeper import watcher as wmod
    from showroom_keeper.status import read_live_stats

    cfg = Config(rooms=["k"], state_file=tmp_path / "s.json", health_check_interval=30, periodic_reload_minutes=0,
                 viewer_count=False, block_media=True)
    clock = Clock()
    answers = [{"checked": True, "login": True, "user_id": 1, "stage": 12, "rank": None, "on_stage": False},
               {"checked": True, "login": True, "user_id": 1, "stage": 13, "rank": 13, "on_stage": True},
               {"checked": True, "login": False, "user_id": None, "stage": 13, "rank": None, "on_stage": None}]
    asked = []
    page = FakePage()

    async def evaluate(js, arg=None):
        if "stage_user_list" in js:
            asked.append(clock.t)
            return answers.pop(0)
        if "current_user" in js:
            return {"user_id": 1}
        return None

    page.evaluate = evaluate

    async def fetch(key):
        return RoomStatus(key, 7, "Room", True, live_id=99)

    async def new_page():
        return page

    w = RoomWatcher("k", cfg, fetch, new_page, clock=clock)
    await w.step()  # ページを開く
    clock.t = 30
    await w.step()  # 1 回目: いない → 入り直し
    assert asked == [30] and read_live_stats(tmp_path)["k"]["avatar"]["on_stage"] is False
    clock.t = 60
    await w.step()  # まだ確かめない
    clock.t = 30 + wmod.AVATAR_RECHECK
    await w.step()  # 少しあとに確かめ直す: いる
    assert asked == [30, 30 + wmod.AVATAR_RECHECK] and w.avatar["on_stage"] and w.avatar["rank"] == 13
    clock.t = 30 + wmod.AVATAR_RECHECK + wmod.AVATAR_CHECK_EVERY
    await w.step()  # ログインしていない → 次の回に読み込み直す
    assert w.avatar["login"] is False and w.reload_reason
    clock.t += 30
    await w.step()
    assert page.reloads == 1


async def test_periodic_reload_waits_while_seated(env):
    """配信中に観客席にいる間は、定期リロードを延ばす (席を外れないように)。延ばすのは間隔の SEATED_RELOAD_MAX 倍まで。"""
    import time

    from showroom_keeper import watcher as wmod

    w, live, pages, clock = env
    await w.step()
    pages[0].video_times = [float(i) for i in range(100)]
    w.avatar = {"at": time.time(), "login": True, "on_stage": True, "live_id": None}
    w._avatar_at = clock.t = 601
    await w.step()
    assert pages[0].reloads == 0
    w.avatar["on_stage"] = False  # 席を外れたら、いつもどおり読み込み直す
    clock.t = 631
    await w.step()
    assert pages[0].reloads == 1
    w.avatar = {"at": time.time(), "login": True, "on_stage": True, "live_id": None}
    clock.t = 631 + 600 * wmod.SEATED_RELOAD_MAX
    w._avatar_at = clock.t
    await w.step()
    assert pages[0].reloads == 2
