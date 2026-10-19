from showroom_keeper.api import ShowroomAPI, parse_login_state, parse_room_status


def test_parse_room_status_is_live():
    s = parse_room_status("k", {"room_id": "123", "room_name": "Room", "is_live": True})
    assert s.room_id == 123 and s.is_live and s.room_name == "Room"
    assert s.url == "https://www.showroom-live.com/r/k"


def test_parse_room_status_fallback_fields():
    assert parse_room_status("k", {"live_status": 2}).is_live
    assert not parse_room_status("k", {"live_status": 1}).is_live
    assert parse_room_status("k", {}).room_name == "k"


def test_parse_login_state():
    assert parse_login_state({"is_login": 1}) is True
    assert parse_login_state({"is_login": False}) is False
    assert parse_login_state({"user_id": 0}) is False
    assert parse_login_state({"user_id": 42}) is True
    assert parse_login_state({}) is None
    assert parse_login_state([]) is None


class FakeResp:
    def __init__(self, data, status=200):
        self._data, self.status, self.ok = data, status, 200 <= status < 300

    async def json(self):
        return self._data


class FakeRequest:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    async def get(self, url, **kw):
        self.calls.append(("GET", url, kw))
        return self.routes[url]

    async def post(self, url, **kw):
        self.calls.append(("POST", url, kw))
        return self.routes[url]


B = "https://www.showroom-live.com"


async def test_login_success():
    req = FakeRequest({f"{B}/api/csrf_token": FakeResp({"csrf_token": "tok"}), f"{B}/user/login": FakeResp({"ok": 1})})
    assert await ShowroomAPI(req).login("id", "pw")
    assert req.calls[-1][2]["form"] == {"account_id": "id", "password": "pw", "csrf_token": "tok"}


async def test_login_captcha_fails():
    req = FakeRequest({f"{B}/api/csrf_token": FakeResp({"csrf_token": "tok"}), f"{B}/user/login": FakeResp({"captcha_url": "x"})})
    assert not await ShowroomAPI(req).login("id", "pw")


async def test_is_logged_in_unknown_on_error():
    req = FakeRequest({f"{B}/api/live/current_user": FakeResp({}, status=500)})
    assert await ShowroomAPI(req).is_logged_in(1) is None


async def test_live_room_id_picks_one_live_room():
    onlives = {"onlives": [{"lives": []}, {"lives": [{"room_id": "7"}, {"room_id": 8}]}]}
    assert await ShowroomAPI(FakeRequest({f"{B}/api/live/onlives": FakeResp(onlives)})).live_room_id() == 7
    assert await ShowroomAPI(FakeRequest({f"{B}/api/live/onlives": FakeResp({}, status=503)})).live_room_id() is None


async def test_live_room_id_skips_rooms_not_to_visit():
    onlives = {"onlives": [{"lives": [{"room_id": 7}, {"room_id": 8}]}]}
    assert await ShowroomAPI(FakeRequest({f"{B}/api/live/onlives": FakeResp(onlives)})).live_room_id(skip={7}) == 8


async def test_public_lookups_skip_the_browser_and_share_the_live_list(monkeypatch):
    """配信状況と配信中の一覧はブラウザを通さず、配信中の一覧は入室・ギフトの記録・収集で 1 分のあいだ使い回す。"""
    import asyncio

    from showroom_keeper import api, onlives, recorder

    calls = []

    def fake_get_json(path, cookie=None):
        calls.append(path)
        if path == onlives.PATH:
            return {"onlives": [{"lives": [{"room_id": 7, "room_url_key": "k7", "live_id": 70}]}]}
        return {"room_id": 9, "room_name": "R", "is_live": True}

    monkeypatch.setattr(recorder, "get_json", fake_get_json)
    monkeypatch.setattr(api, "_public_pool", None)
    req = FakeRequest({})  # ブラウザには何も問い合わせない
    a = ShowroomAPI(req, public=api.public_get)
    status = await a.room_status("k 9")
    assert status.room_id == 9 and status.is_live and calls == ["/api/room/status?room_url_key=k+9"]

    calls.clear()
    lives, room_id = await asyncio.gather(a.onlives(), a.live_room_id())
    assert [s.room_id for s in lives] == [7] and room_id == 7
    r = recorder.GiftRecorder.__new__(recorder.GiftRecorder)
    r._glitter, r._glitter_saved, r.state_dir = {}, 1e18, None
    seen: dict = {}
    await r._add_glitter(None, {"k7"}, seen, 0)
    assert r._seen_watched == {"k7": (7, 70)}
    assert onlives.get(lambda: (_ for _ in ()).throw(AssertionError("取り直した")))["onlives"]
    assert calls == [onlives.PATH] and req.calls == []  # 1 回だけ取った

    onlives.remember(onlives.fresh(), now=0)  # 1 分より古くなれば取り直す
    await a.onlives()
    assert calls == [onlives.PATH, onlives.PATH]
