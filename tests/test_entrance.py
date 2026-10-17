import json
import urllib.error

from showroom_keeper.api import RoomStatus
from showroom_keeper.config import Config
from showroom_keeper.entrance import RoomEntrance, Session

COOKIES = [{"name": "sr_id", "value": "abc", "domain": "www.showroom-live.com", "path": "/"},
           {"name": "other", "value": "x", "domain": "example.com", "path": "/"}]


class FakeShowroom:
    """入室ごとに問い合わせを記録する。配信ページを開くと uuid をもらえ、greeting は uuid が無いと ok を返さない。

    is_login=False ならログインしていない応答 (401)、fail なら通信の失敗、stale_uuid は使い回した uuid を断る。
    """

    def __init__(self, is_login=True, fail=False, greeting='{"ok":1}', stale_uuid=False):
        self.is_login, self.fail, self.greeting, self.stale_uuid = is_login, fail, greeting, stale_uuid
        self.sessions = []
        self.issued = 0

    def __call__(self, cookies, uuid=None):
        fake = self
        asked = []
        self.sessions.append({"cookies": cookies, "uuid": uuid, "asked": asked})

        class S:
            def __init__(self):
                self.uuid = uuid

            def get(self, path):
                asked.append(path)
                if fake.fail:
                    raise OSError("Connection reset")
                if path.startswith("/r/"):
                    fake.issued += 1
                    self.uuid = f"u{fake.issued}"
                    return "<html>"
                if path.startswith("/api/room/greeting"):
                    if not fake.is_login:
                        raise urllib.error.HTTPError(path, 401, "Unauthorized", {}, None)
                    if self.uuid is None or (fake.stale_uuid and self.uuid == uuid):
                        return '{"errors":[{"code":1}]}'
                    return fake.greeting
                return "{}"

        return S()


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


async def test_enters_once_per_live_with_a_single_greeting():
    statuses = {"a": live("a", 10, room_id=5), "b": live("b", None), "c": live("c", 30, room_id=7)}
    entrance, showroom, waits = make(statuses)
    await entrance.step()
    await entrance.step()
    # 配信中の a と c に 1 回ずつ入室する。配信していない b には入室しない
    assert entrance.entered == {"a": 10, "c": 30} and len(showroom.sessions) == 2
    # 最初の入室で配信ページを 1 回開いて uuid をもらい、あとは使い回して greeting だけ (待たない)
    assert showroom.sessions[0]["asked"] == ["/r/a", "/api/room/greeting?room_id=5"]
    assert showroom.sessions[1]["asked"] == ["/api/room/greeting?room_id=7"] and showroom.sessions[1]["uuid"] == "u1"
    assert waits == [] and showroom.sessions[0]["cookies"] == COOKIES
    # 次の配信が始まったらもう一度入室する
    statuses["a"] = live("a", 11, room_id=5)
    await entrance.step()
    assert len(showroom.sessions) == 3 and entrance.entered == {"a": 11, "c": 30}


async def test_stale_uuid_is_renewed_once():
    entrance, showroom, _ = make({"a": live("a", 10), "c": live("c", 30)}, stale_uuid=True)
    await entrance.step()
    # 2 件目は使い回した uuid を断られたので、配信ページで取り直して入室する
    assert entrance.entered == {"a": 10, "c": 30}
    assert [s["asked"][0] for s in showroom.sessions] == ["/r/a", "/api/room/greeting?room_id=1", "/r/c"]


async def test_uuid_is_renewed_after_a_while():
    entrance, showroom, _ = make({"a": live("a", 10)})
    now = [0.0]
    entrance._clock = lambda: now[0]
    await entrance.step()
    now[0] = 7 * 3600
    entrance.entered = {}
    await entrance.step()
    assert [s["asked"][0] for s in showroom.sessions] == ["/r/a", "/r/a"]


async def test_not_logged_in_is_not_counted_as_entered():
    entrance, showroom, waits = make({"a": live("a", 10)}, is_login=False)
    await entrance.step()
    # ログインしていなければ (greeting が 401) 入室済みにしない (次の確認で入室し直す)。uuid は取り直さない
    assert entrance.entered == {} and len(showroom.sessions) == 1


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


def test_session_keeps_only_showroom_cookies_and_the_reused_uuid(monkeypatch):
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
    s = Session(COOKIES, uuid="u1")
    assert s.uuid == "u1" and Session(COOKIES).uuid is None
    assert s.get("/api/room/greeting?room_id=1") == '{"ok":1}'
    assert sent == [("https://www.showroom-live.com/api/room/greeting?room_id=1", ["sr_id", "uuid"])]


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

    waits = []

    async def sleep(s):
        waits.append(s)

    now = clock or [1000.0]
    entrance = AllRoomsEntrance(60, fetch, cookies, skip=["watched"], state_dir=state_dir, sleep=sleep,
                                session=showroom, clock=lambda: now[0])
    entrance.waits = waits
    return entrance, showroom, now


async def test_all_rooms_enters_every_new_live_once():
    lives = [[live("a", 10, 1), live("b", 20, 2), live("c", 30, 3), live("watched", 40, 4)]]
    entrance, showroom, _ = make_all(lives)
    await entrance.step()
    # 視聴しているルームは視聴ページで入室済みなので入らない
    greeted = sorted(s["asked"][-1] for s in showroom.sessions)
    assert greeted == [f"/api/room/greeting?room_id={i}" for i in (1, 2, 3)] and entrance.entered == {"a": 10, "b": 20, "c": 30}
    # 入室と入室の間は 1 秒ずつ空ける (起動直後に数百件をまとめて送らない)
    assert entrance.waits == [1.0, 1.0, 1.0]
    await entrance.step()
    assert len(showroom.sessions) == 3
    # 新しく始まった配信と、次の配信にだけ入る
    lives[0] = [live("a", 11, 1), live("b", 20, 2), live("d", 50, 5)]
    await entrance.step()
    assert sorted(s["asked"][-1] for s in showroom.sessions[3:]) == ["/api/room/greeting?room_id=1", "/api/room/greeting?room_id=5"]


async def test_all_rooms_forgets_rooms_that_stopped_and_survives_restart(tmp_path):
    lives = [[live("a", 10, 1)]]
    entrance, showroom, now = make_all(lives, state_dir=tmp_path)
    await entrance.step()
    # 起動し直しても、同じ配信には入り直さない
    again, showroom2, now2 = make_all(lives, state_dir=tmp_path)
    await again.step()
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
