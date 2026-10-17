import json

from showroom_keeper.api import RoomStatus
from showroom_keeper.config import Config
from showroom_keeper.entrance import RoomEntrance, cookie_session

COOKIES = [{"name": "sr_id", "value": "abc", "domain": "www.showroom-live.com", "path": "/"},
           {"name": "other", "value": "x", "domain": "example.com", "path": "/"}]


class FakeShowroom:
    """入室ごとに問い合わせを記録する (is_login=False ならログインしていない応答、fail なら通信の失敗)。"""

    def __init__(self, is_login=True, fail=False, greeting='{"ok":1}'):
        self.is_login, self.fail, self.greeting = is_login, fail, greeting
        self.sessions = []

    def __call__(self, cookies):
        asked = []
        self.sessions.append({"cookies": cookies, "asked": asked})

        def get(path):
            asked.append(path)
            if self.fail:
                raise OSError("Connection reset")
            if path.startswith("/api/live/current_user"):
                return json.dumps({"is_login": self.is_login, "user_id": 9 if self.is_login else 0})
            if path.startswith("/api/room/greeting"):
                return self.greeting
            return "{}"

        return get


def make(statuses, state_dir=None, **kw):
    showroom = FakeShowroom(**kw)
    waits = []

    async def fetch(key):
        value = statuses[key]
        if isinstance(value, Exception):
            raise value
        return value

    async def cookies():
        return COOKIES

    async def sleep(s):
        waits.append(s)

    entrance = RoomEntrance(list(statuses), 60, fetch, cookies, state_dir=state_dir, sleep=sleep, session=showroom)
    return entrance, showroom, waits


def live(key, live_id, room_id=1):
    return RoomStatus(key, room_id, key, is_live=live_id is not None, live_id=live_id)


async def test_enters_once_per_live_like_the_room_page():
    statuses = {"a": live("a", 10, room_id=5), "b": live("b", None)}
    entrance, showroom, waits = make(statuses)
    await entrance.step()
    await entrance.step()
    # 配信中の a に 1 回だけ入室する。配信していない b には入室しない
    assert len(showroom.sessions) == 1 and entrance.entered == {"a": 10}
    # 配信ページと同じ順に問い合わせ、10 秒たってから greeting を呼ぶ (これでアバターが出る)
    assert showroom.sessions[0]["asked"] == ["/r/a", "/api/live/live_info?room_id=5", "/api/live/current_user?room_id=5",
                                             "/api/live/polling?room_id=5", "/api/room/greeting?room_id=5"]
    assert waits == [10.0]
    assert showroom.sessions[0]["cookies"] == COOKIES
    # 次の配信が始まったらもう一度入室する
    statuses["a"] = live("a", 11, room_id=5)
    await entrance.step()
    assert len(showroom.sessions) == 2 and entrance.entered == {"a": 11}


async def test_not_logged_in_is_not_counted_as_entered():
    entrance, showroom, waits = make({"a": live("a", 10)}, is_login=False)
    await entrance.step()
    # ログインしていなければ greeting を呼ばず、入室済みにもしない (次の確認で入室し直す)
    assert not any("greeting" in p for p in showroom.sessions[0]["asked"]) and entrance.entered == {} and waits == []


async def test_failed_entry_is_retried_and_other_rooms_continue():
    statuses = {"broken": RuntimeError("HTTP 500"), "a": live("a", 10)}
    entrance, showroom, _ = make(statuses, fail=True)
    await entrance.step()
    assert len(showroom.sessions) == 1 and entrance.entered == {}
    await entrance.step()
    assert len(showroom.sessions) == 2


async def test_gives_up_on_a_live_after_repeated_failures():
    statuses = {"a": live("a", 10)}
    entrance, showroom, _ = make(statuses, is_login=False)
    for _ in range(5):
        await entrance.step()
    # 同じ配信には 3 回まで (配信が終わるまで 1 分ごとに入り直し続けない)
    assert len(showroom.sessions) == 3
    # 次の配信ではまた試す
    statuses["a"] = live("a", 11)
    await entrance.step()
    assert len(showroom.sessions) == 4


async def test_greeting_refused_is_retried():
    entrance, showroom, _ = make({"a": live("a", 10)}, greeting='{"errors":[{"code":1}]}')
    await entrance.step()
    assert entrance.entered == {}


async def test_entered_lives_survive_a_restart(tmp_path):
    entrance, showroom, _ = make({"a": live("a", 10)}, state_dir=tmp_path)
    await entrance.step()
    assert len(showroom.sessions) == 1
    # ブラウザの再起動や設定の反映のあとも、同じ配信には入室し直さない
    again, showroom, _ = make({"a": live("a", 10)}, state_dir=tmp_path)
    await again.step()
    assert showroom.sessions == [] and again.entered == {"a": 10}


def test_cookie_session_keeps_only_showroom_cookies(monkeypatch):
    import urllib.request

    sent = []

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"ok":1}'

    def fake_open(self, req, timeout=None):
        jar = next(h for h in self.handlers if isinstance(h, urllib.request.HTTPCookieProcessor)).cookiejar
        sent.append((req if isinstance(req, str) else req.full_url, sorted(c.name for c in jar)))
        return Resp()

    monkeypatch.setattr(urllib.request.OpenerDirector, "open", fake_open)
    get = cookie_session(COOKIES)
    assert get("/api/room/greeting?room_id=1") == '{"ok":1}'
    assert sent == [("https://www.showroom-live.com/api/room/greeting?room_id=1", ["sr_id"])]


def test_entry_needs_no_page_in_the_memory_estimate():
    import pytest

    kp = pytest.importorskip("showroom_keeper.keeper")
    rooms = ["a", "b", "c"]
    # 入室はブラウザのページを開かないので、入室するルームがあってもメモリの見積もりは変わらない
    assert kp.resolve_keep_open(Config(rooms=rooms), mem=2.0) is True
    assert kp.resolve_keep_open(Config(rooms=rooms, enter_rooms=["x"]), mem=2.0) is True


def make_all(lives, state_dir=None, clock=None, **kw):
    from showroom_keeper.entrance import AllRoomsEntrance

    showroom = FakeShowroom(**kw)

    async def fetch():
        if isinstance(lives[0], Exception):
            raise lives[0]
        return list(lives[0])

    async def cookies():
        return COOKIES

    async def sleep(s):
        pass

    now = clock or [1000.0]
    entrance = AllRoomsEntrance(60, fetch, cookies, skip=["watched"], state_dir=state_dir, sleep=sleep,
                                session=showroom, clock=lambda: now[0], parallel=2)
    return entrance, showroom, now


async def test_all_rooms_enters_every_new_live_once():
    lives = [[live("a", 10, 1), live("b", 20, 2), live("c", 30, 3), live("watched", 40, 4)]]
    entrance, showroom, _ = make_all(lives)
    await entrance.step()
    await entrance.wait_idle()
    # 視聴しているルームは視聴ページで入室済みなので入らない
    entered = sorted(s["asked"][0] for s in showroom.sessions)
    assert entered == ["/r/a", "/r/b", "/r/c"] and entrance.entered == {"a": 10, "b": 20, "c": 30}
    await entrance.step()
    await entrance.wait_idle()
    assert len(showroom.sessions) == 3
    # 新しく始まった配信と、次の配信にだけ入る
    lives[0] = [live("a", 11, 1), live("b", 20, 2), live("d", 50, 5)]
    await entrance.step()
    await entrance.wait_idle()
    assert sorted(s["asked"][0] for s in showroom.sessions[3:]) == ["/r/a", "/r/d"]


async def test_all_rooms_limits_parallel_entries():
    import asyncio

    from showroom_keeper.entrance import AllRoomsEntrance

    running, peak = [0], [0]

    async def slow_enter(self, status):
        running[0] += 1
        peak[0] = max(peak[0], running[0])
        await asyncio.sleep(0.01)
        running[0] -= 1
        return True

    lives = [[live(k, i, i) for i, k in enumerate("abcdef", 1)]]
    entrance, _, _ = make_all(lives)
    entrance._enter = slow_enter.__get__(entrance, AllRoomsEntrance)
    await entrance.step()
    # 一覧を取ったらすぐ戻る (入室は裏で同時に 2 件まで)
    assert entrance.entered == {}
    await entrance.wait_idle()
    assert peak[0] == 2 and len(entrance.entered) == 6


async def test_all_rooms_forgets_rooms_that_stopped_and_survives_restart(tmp_path):
    lives = [[live("a", 10, 1)]]
    entrance, showroom, now = make_all(lives, state_dir=tmp_path)
    await entrance.step()
    await entrance.wait_idle()
    # 起動し直しても、同じ配信には入り直さない
    again, showroom2, now2 = make_all(lives, state_dir=tmp_path)
    await again.step()
    await again.wait_idle()
    assert showroom2.sessions == [] and again.entered == {"a": 10}
    # 1 時間以上配信中の一覧に出てこないルームは、入室済みの記録から消す (記録を小さく保つ)
    lives[0] = []
    now2[0] += 3601
    await again.step()
    assert again.entered == {} and json.loads((tmp_path / "entered.json").read_text()) == {}


async def test_all_rooms_keeps_going_when_the_list_fails():
    lives = [RuntimeError("HTTP 503")]
    entrance, showroom, _ = make_all(lives)
    await entrance.step()
    assert showroom.sessions == []


def test_parse_onlives_dedupes_and_skips_paid_lives():
    from showroom_keeper.api import parse_onlives

    data = {"onlives": [
        {"lives": [{"room_url_key": "a", "room_id": 1, "main_name": "A", "live_id": 10},
                   {"room_url_key": "paid", "room_id": 2, "live_id": 20, "premium_room_type": 1}]},
        {"lives": [{"room_url_key": "a", "room_id": 1, "main_name": "A", "live_id": 10}, {"room_id": 3}]},
    ]}
    got = parse_onlives(data)
    assert [(s.room_url_key, s.room_id, s.room_name, s.live_id, s.is_live) for s in got] == [("a", 1, "A", 10, True)]
