import json
import random
from datetime import datetime
from types import SimpleNamespace

from showroom_keeper import mission
from showroom_keeper.status import JST


def jst(*a):
    return datetime(*a, tzinfo=JST)


def test_period_of_splits_day_and_night_at_3_and_15():
    assert mission.period_of(jst(2026, 10, 22, 3, 0))[:2] == ("2026-10-22-day", "day")
    assert mission.period_of(jst(2026, 10, 22, 14, 59))[:2] == ("2026-10-22-day", "day")
    assert mission.period_of(jst(2026, 10, 22, 15, 0))[:2] == ("2026-10-22-night", "night")
    assert mission.period_of(jst(2026, 10, 23, 2, 59))[:2] == ("2026-10-22-night", "night")


def test_start_at_finds_the_time_inside_the_period():
    times = mission.parse_times("12:00,21:00")
    assert mission.start_at(jst(2026, 10, 22, 4, 0), times) == jst(2026, 10, 22, 12, 0)
    assert mission.start_at(jst(2026, 10, 23, 1, 0), times) == jst(2026, 10, 22, 21, 0)
    assert mission.start_at(jst(2026, 10, 22, 4, 0), mission.parse_times("21:00")) is None
    assert mission.start_at(jst(2026, 10, 23, 1, 0), mission.parse_times("1:30")) == jst(2026, 10, 23, 1, 30)


def _daily(level=1, value=0, comment=0, remain=0):
    part = {"continuous_mission": [{"mission_id": 1, "title": "配信を視聴しよう（1/20）", "current_value": value,
                                    "target_value": 1, "current_level": level, "max_level": 20, "remain_reward": remain,
                                    "reward_name": "G", "reward_num": 1, "is_active": 1}],
            "single_mission": [{"mission_id": 2, "title": "配信を見てコメントしよう", "current_value": comment,
                                "target_value": 1, "max_level": 1, "current_level": 1, "remain_reward": comment,
                                "reward_name": "G", "reward_num": 10, "is_active": 1}]}
    return {"genre_list": [{"genre": "daily", "current_period": "night", "night": part, "day": {}},
                           {"genre": "limited", "all_days_mission": [
                               {"mission_id": 3, "title": "きっかけ配信を10ルーム視聴してコメントしよう", "current_value": 0,
                                "target_value": 10, "is_daily": 0, "is_active": 1, "remain_reward": 0}]}]}


def test_views_left_and_comment_wanted():
    ms = mission.flatten(_daily(level=5, value=0))
    assert mission.views_left(ms) == 16
    assert mission.views_left(mission.flatten(_daily(level=20, value=1))) == 0
    assert mission.comment_wanted(ms)
    assert not mission.comment_wanted(mission.flatten(_daily(comment=1)))


def _onlives():
    lives = [{"room_id": i, "live_id": 1000 + i, "room_url_key": f"r{i}", "live_type": 0} for i in range(1, 30)]
    lives[0]["live_type"] = 4  # きっかけ配信
    lives[1]["new_days_count_label"] = "New7day"
    lives[2]["premium_room_type"] = 1  # 有料
    lives[3]["live_type"] = 5  # 見られる人が限られる配信
    lives[4]["room_url_key"] = "watched"
    return {"onlives": [{"genre_id": 0, "lives": lives}, {"genre_id": 763, "lives": [lives[1]]}]}


def test_candidates_put_kikkake_and_new_first_and_skip_others():
    rooms = mission.candidates(_onlives(), {"watched"}, {1010}, random.Random(0))
    assert [r["kind"] for r in rooms[:2]] == ["kikkake", "new"]
    ids = {r["room_id"] for r in rooms}
    assert not ids & {3, 4, 5, 10}
    assert len(ids) == len(rooms) == 29 - 4


class FakeSession:
    """視聴すると 30 秒ごとの polling が 2 回目で 1 段進む SHOWROOM の代わり。"""

    def __init__(self, server):
        self.server = server

    def get(self, path):
        sv = self.server
        sv.calls.append(("GET", path))
        if path.startswith("/api/mission"):
            return json.dumps(_daily(sv.level, sv.value, sv.comment, sv.remain))
        if path.startswith("/api/live/current_user"):
            rid = int(path.split("=")[1])
            return json.dumps({"is_login": sv.login, "is_targeted_discovery_live": rid != 1 or sv.targeted})
        if path.startswith("/api/live/polling"):
            rid = int(path.split("=")[1])
            sv.polls[rid] = sv.polls.get(rid, 0) + 1
            if sv.polls[rid] == 2 and sv.level < 20:
                sv.level += 1
                sv.remain += 1
            elif sv.polls[rid] == 2:
                sv.value = 1
            return "{}"
        raise AssertionError(path)

    def post(self, path, form):
        sv = self.server
        sv.calls.append(("POST", path, form))
        if path == "/api/live/post_live_comment":
            sv.comment = 1
            return json.dumps({"ok": 1})
        if path == "/api/mission/receive":
            if form["mission_id"] == 1:
                sv.remain = 0
            return json.dumps({"mission": {}})
        raise AssertionError(path)


def _runner(tmp_path, now, **cfg):
    sv = SimpleNamespace(level=1, value=0, comment=0, remain=0, login=True, targeted=False, polls={}, calls=[], now=now)
    base = dict(state_file=tmp_path / "storage_state.json", rooms=["watched"], no_visit_rooms=[], mission_rooms=20,
                mission_comment="1", mission_times="12:00,21:00", mission_view_seconds=60.0)
    base.update(cfg)

    async def sleep(sec):
        sv.now += sec

    async def cookies():
        return []

    async def lives():
        return _onlives()

    r = mission.MissionRunner(SimpleNamespace(**base), cookies, lives, session=lambda c: FakeSession(sv),
                              sleep=sleep, clock=lambda: sv.now, rng=random.Random(1))
    return r, sv


async def test_run_views_until_the_20_steps_are_done_comments_once_and_receives(tmp_path):
    now = jst(2026, 10, 22, 21, 5).timestamp()
    r, sv = _runner(tmp_path, now)
    await r.step(mission.parse_times("12:00,21:00"))
    assert r.state["done"] and r.state["views"] == 20
    assert r.state["kikkake"] == 0  # きっかけ配信の対象でなければ見ない
    posts = [c for c in sv.calls if c[0] == "POST"]
    assert [p[1] for p in posts].count("/api/live/post_live_comment") == 1
    assert posts[0][2]["comment"] == "1"
    assert ("POST", "/api/mission/receive", {"mission_id": 1}) in posts
    assert r.state["rewards"]["G"] >= 10
    saved = json.loads((tmp_path / "mission.json").read_text())
    assert len(saved["live_ids"]) == 20
    calls = len(sv.calls)
    await r.step(mission.parse_times("12:00,21:00"))  # 同じ区切りでは 2 回しない
    assert len(sv.calls) == calls


async def test_waits_for_the_start_time_and_skips_comment_when_off(tmp_path):
    r, sv = _runner(tmp_path, jst(2026, 10, 22, 20, 0).timestamp(), mission_comment="off")
    await r.step(mission.parse_times("12:00,21:00"))
    assert sv.calls == []
    sv.now = jst(2026, 10, 22, 21, 0).timestamp()
    await r.step(mission.parse_times("12:00,21:00"))
    assert r.state["done"]
    assert not any(c[1] == "/api/live/post_live_comment" for c in sv.calls if c[0] == "POST")


async def test_not_logged_in_stops_and_retries_later(tmp_path):
    r, sv = _runner(tmp_path, jst(2026, 10, 22, 21, 5).timestamp())
    sv.login = False
    try:
        await r.step(mission.parse_times("21:00"))
    except mission.NotLoggedIn:
        pass
    else:
        raise AssertionError("ログインしていないのに続けた")
    assert not r.state["done"] and r.state["views"] == 0


async def test_stops_when_views_are_not_counted(tmp_path):
    r, sv = _runner(tmp_path, jst(2026, 10, 22, 21, 5).timestamp())
    sv.polls = {i: 99 for i in range(100)}  # polling を送っても段が進まない
    lives = _onlives()["onlives"][0]["lives"]
    lives += [{"room_id": i, "live_id": 1000 + i, "room_url_key": f"r{i}", "live_type": 0} for i in range(30, 80)]

    async def more():
        return {"onlives": [{"lives": lives}]}

    r._fetch_onlives = more
    for _ in range(5):
        r.state["retry_at"] = 0
        await r.step(mission.parse_times("21:00"))
    assert r.state["done"]
    assert r.state["views"] == 20 + 2 * mission.EXTRA_ROOMS
