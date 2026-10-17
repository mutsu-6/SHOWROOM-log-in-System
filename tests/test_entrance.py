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
