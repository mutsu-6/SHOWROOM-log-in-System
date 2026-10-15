from showroom_keeper.api import ShowroomAPI, parse_login_state, parse_room_status, summarize_login


def test_parse_room_status_is_live():
    s = parse_room_status("k", {"room_id": "123", "room_name": "Room", "is_live": True})
    assert s.room_id == 123 and s.is_live and s.room_name == "Room"
    assert s.url == "https://www.showroom-live.com/r/k"


def test_parse_room_status_fallback_fields():
    assert parse_room_status("k", {"live_status": 2}).is_live
    assert not parse_room_status("k", {"live_status": 1}).is_live
    assert parse_room_status("k", {}).room_name == "k"


def test_summarize_login_requires_every_room():
    assert summarize_login([]) is None
    assert summarize_login([None, None]) is None
    assert summarize_login([True, None]) is True
    assert summarize_login([True, True]) is True
    assert summarize_login([True, False]) is False
    assert summarize_login([False]) is False


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
