import asyncio
import json
import urllib.error
from types import SimpleNamespace

import pytest

from showroom_keeper import gift_plan as gp

NOW = 1_800_000_000.0


def cfg(**kw):
    base = dict(gift_send=False, gift_free_path="/free", gift_paid_path="/paid", gift_paid_max=0, gift_paid_daily_max=0,
                gift_late_limit=2.0)
    base.update(kw)
    return SimpleNamespace(**base)


def fake_get(path):
    if path.startswith("/api/room/status"):
        return {"room_id": 42, "room_name": "テストのルーム", "is_live": True, "live_id": 999}
    if path.startswith("/api/live/gift_list"):
        return {"normal": [{"gift_id": 1, "gift_name": "星", "point": 1, "free": True},
                           {"gift_id": 2, "gift_name": "100G", "point": 100, "free": False}]}
    raise AssertionError(path)


def make(tmp_path, c, **data):
    body = {"room": "test_room", "gift_id": 1, "num": 10, "at": NOW + 60, "confirm_sg": 0}
    body.update(data)
    return gp.create(tmp_path, c, body, now=NOW, get=fake_get)


def test_free_gift_can_be_reserved_and_cancelled(tmp_path):
    p = make(tmp_path, cfg())
    assert p["state"] == gp.WAITING and p["sg"] == 0 and p["room_id"] == 42
    gp.cancel(tmp_path, p["id"])
    assert gp.load(tmp_path)[0]["state"] == gp.CANCELLED
    with pytest.raises(ValueError):
        gp.cancel(tmp_path, p["id"])  # 済んだものは取り消せない


def test_paid_is_refused_while_the_limit_is_zero(tmp_path):
    with pytest.raises(ValueError, match="0 なので"):
        make(tmp_path, cfg(), gift_id=2, num=1, confirm_sg=100)


def test_paid_needs_the_same_confirmed_amount_and_keeps_the_limits(tmp_path):
    c = cfg(gift_paid_max=500, gift_paid_daily_max=700)
    with pytest.raises(ValueError, match="確認の金額"):
        make(tmp_path, c, gift_id=2, num=5, confirm_sg=100)
    with pytest.raises(ValueError, match="1 回の有料の上限"):
        make(tmp_path, c, gift_id=2, num=10, confirm_sg=1000)
    make(tmp_path, c, gift_id=2, num=5, confirm_sg=500)
    with pytest.raises(ValueError, match="その日の有料の上限"):
        make(tmp_path, c, gift_id=2, num=5, confirm_sg=500)
    make(tmp_path, c, gift_id=2, num=1, confirm_sg=100)


def test_bad_counts_and_times_are_refused(tmp_path):
    with pytest.raises(ValueError, match="個数"):
        make(tmp_path, cfg(), num=5)  # 無料は 1 か 10
    with pytest.raises(ValueError, match="時刻"):
        make(tmp_path, cfg(), at=NOW + 5)


def test_recover_turns_sending_into_unknown_and_never_resends(tmp_path):
    p = make(tmp_path, cfg())
    gp.update(tmp_path, p["id"], state=gp.SENDING)
    assert gp.recover(tmp_path) == 1
    assert gp.load(tmp_path)[0]["state"] == gp.UNKNOWN


@pytest.mark.parametrize("status,body,state", [
    (200, '{"ok": true}', gp.SENT),
    (200, '{"errors": [{"message": "x"}]}', gp.FAILED),
    (400, "{}", gp.FAILED),
    (503, "", gp.UNKNOWN),
    (None, "", gp.UNKNOWN),
])
def test_judge(status, body, state):
    assert gp.judge(status, body)[0] == state


class FakeSession:
    def __init__(self, *a, **kw):
        self.posts = []
        self.fail = None

    def get(self, path):
        if path.startswith("/api/room/status"):
            return json.dumps({"room_id": 42, "is_live": True, "live_id": 999})
        if path == "/api/csrf_token":
            return json.dumps({"csrf_token": "t"})
        raise AssertionError(path)

    def post_with(self, path, form, token, timeout=10):
        self.posts.append((path, form, token))
        if self.fail:
            raise self.fail
        return 200, '{"ok": true}'


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s

    async def asleep(self, s):
        self.t += s


def runner(tmp_path, c, clock, session):
    async def cookies():
        return []
    return gp.GiftRunner(c, cookies, tmp_path, session=lambda _c: session, sleep=clock.asleep, clock=clock,
                         thread_sleep=clock.sleep)


def test_runner_sends_once_at_the_time(tmp_path):
    p = make(tmp_path, cfg(gift_send=True))
    clock, s = Clock(NOW + 50), FakeSession()
    r = runner(tmp_path, cfg(gift_send=True), clock, s)
    asyncio.run(r.step())
    assert len(s.posts) == 1
    path, form, token = s.posts[0]
    assert path == "/free" and form == {"gift_id": 1, "live_id": 999, "num": 10, "is_delay": 0} and token == "t"
    done = gp.load(tmp_path)[0]
    assert done["state"] == gp.SENT and done["started"] >= p["at"] and done["late"] < 0.1
    asyncio.run(r.step())
    assert len(s.posts) == 1  # 2 度は送らない


def test_runner_practice_does_not_post(tmp_path):
    make(tmp_path, cfg())
    clock, s = Clock(NOW + 50), FakeSession()
    asyncio.run(runner(tmp_path, cfg(), clock, s).step())
    assert s.posts == [] and gp.load(tmp_path)[0]["state"] == gp.PRACTICE


def test_runner_skips_late_plans_and_timeouts_become_unknown(tmp_path):
    make(tmp_path, cfg())
    clock, s = Clock(NOW + 70), FakeSession()
    asyncio.run(runner(tmp_path, cfg(gift_send=True), clock, s).step())
    assert s.posts == [] and gp.load(tmp_path)[0]["state"] == gp.SKIPPED

    make(tmp_path, cfg(), at=NOW + 100)
    s.fail = TimeoutError("timed out")
    clock.t = NOW + 90
    asyncio.run(runner(tmp_path, cfg(gift_send=True), clock, s).step())
    assert len(s.posts) == 1 and gp.load(tmp_path)[-1]["state"] == gp.UNKNOWN


def test_runner_rechecks_the_paid_limit_before_sending(tmp_path):
    make(tmp_path, cfg(gift_paid_max=500, gift_paid_daily_max=500), gift_id=2, num=5, confirm_sg=500)
    clock, s = Clock(NOW + 50), FakeSession()
    asyncio.run(runner(tmp_path, cfg(gift_send=True), clock, s).step())  # 上限を 0 に下げた
    assert s.posts == [] and gp.load(tmp_path)[0]["state"] == gp.SKIPPED


def test_http_error_is_failed(tmp_path):
    s = FakeSession()
    s.fail = urllib.error.HTTPError("u", 403, "no", {}, None)
    clock = Clock(NOW)
    out = gp.send_at(s, "/free", {}, "t", NOW, 2.0, clock, clock.sleep, True)
    assert out["state"] == gp.FAILED
