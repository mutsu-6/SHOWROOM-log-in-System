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


def test_comments_left_takes_the_cumulative_forty_up_to_the_limited_cap():
    ms = [
        {"title": "新人ライバーの配信を視聴して3ルームでコメントしよう", "target_value": 3,
         "current_value": 0, "is_daily": 0, "is_active": 1},
        {"title": "新人ライバーの配信を視聴して通算10ルームでコメントしよう", "target_value": 10,
         "current_value": 0, "is_daily": 1, "is_active": 1},
        {"title": "新人ライバーの配信を視聴して通算40ルームでコメントしよう", "target_value": 40,
         "current_value": 0, "is_active": 1},
        {"title": "新人ライバーに文字ギフト「はじめまして」or「初見です」を2回贈ろう", "target_value": 2,
         "current_value": 0, "is_daily": 1, "is_active": 1},
        {"title": "新人ライバーを10ルームフォローしよう", "target_value": 10, "current_value": 0, "is_active": 1},
    ]
    assert mission.comments_left(ms, "new") == mission.LIMITED_MAX  # 期間限定の通算40を 1 回の上限まで。ギフト・フォローは入れない
    near = [ms[0], dict(ms[2], current_value=38)]
    assert mission.comments_left(near, "new") == 3  # 通算の残りが少なければ毎日の 3 ルーム
    done = [dict(ms[0], current_value=3), dict(ms[2], current_value=40), *ms[3:]]
    assert mission.comments_left(done, "new") == 0
    assert mission.comments_left([dict(ms[2], is_active=0)], "new") == 0  # 受付が止まっている企画はしない


def test_comments_left_caps_a_cumulative_mission_when_there_is_no_daily():
    ms = [{"title": "きっかけ配信を10ルーム視聴してコメントしよう", "target_value": 10, "current_value": 6,
           "is_daily": 0, "is_active": 1}]
    assert mission.comments_left(ms, "kikkake") == 4
    assert mission.comments_left([dict(ms[0], current_value=0)], "kikkake") == mission.LIMITED_MAX


def test_limited_views_left_counts_only_viewing_missions_of_that_kind():
    ms = [
        {"title": "きっかけ配信を通算30回視聴しよう", "target_value": 30, "current_value": 6, "is_daily": 0, "is_active": 1},
        {"title": "きっかけ配信を2回視聴しよう", "target_value": 2, "current_value": 0, "is_daily": 1, "is_active": 1},
        {"title": "きっかけ配信を10ルーム視聴してコメントしよう", "target_value": 10, "current_value": 0, "is_active": 1},
        {"title": "新人ライバーの配信を通算10回視聴しよう", "target_value": 10, "current_value": 7, "is_active": 1},
        {"title": "新人ライバーに文字ギフト「はじめまして」を視聴中に2回贈ろう", "target_value": 2, "current_value": 0,
         "is_active": 1},
        {"title": "配信を10回視聴しよう", "target_value": 10, "current_value": 0, "is_active": 1},
    ]
    assert mission.limited_views_left(ms, "kikkake") == mission.LIMITED_MAX  # 残り 24 回を 1 回の上限まで
    assert mission.limited_views_left(ms, "new") == 3  # ギフトの題は数えない
    assert mission.limited_views_left([dict(ms[0], is_active=0)], "kikkake") == 0
    assert mission.limited_views_left(ms[5:], "new") == 0  # どのルームでもよい視聴は毎日の 20 ルームで進む


def test_candidates_take_more_rooms_first_when_asked():
    lives = _onlives()
    for live in lives["onlives"][0]["lives"][5:15]:
        live["new_days_count_label"] = "New3day"
    rooms = mission.candidates(lives, {"watched"}, set(), random.Random(0), first={"new": 8})
    assert [r["kind"] for r in rooms[:9]] == ["kikkake"] + ["new"] * 8


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
            rid = int(path.split("=")[1])
            data = _daily(sv.level, sv.value, sv.comment, sv.remain)
            if rid in sv.new_rooms:  # 新人のルームの中にだけ出るミッション
                data["genre_list"].append({"genre": "limited", "name": "新人ライバー応援", "all_days_mission": [
                    {"mission_id": 10, "title": getattr(sv, "new_comment_title", "新人ライバーの配信3ルームでコメントしよう"),
                     "current_value": sv.new_comments, "target_value": getattr(sv, "new_comment_target", 3),
                     "is_daily": getattr(sv, "new_comment_daily", 1), "is_active": 1, "remain_reward": 0},
                    {"mission_id": 11, "title": "新人ライバーの配信を1回視聴しよう", "current_value": 1, "target_value": 1,
                     "is_daily": 1, "is_active": 1, "remain_reward": sv.new_remain, "reward_name": "G", "reward_num": 2},
                    *getattr(sv, "extra_new_missions", [])]})
            if rid == 1:
                data["genre_list"][1]["all_days_mission"][0]["current_value"] = sv.kik_comments
            return json.dumps(data)
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
            rid = form["live_id"] - 1000
            sv.comment = 1
            sv.new_comments += rid in sv.new_rooms
            sv.kik_comments += rid == 1
            return json.dumps({"ok": 1})
        if path == "/api/mission/receive":
            if form["mission_id"] == 1:
                sv.remain = 0
            if form["mission_id"] == 11:
                sv.new_remain = 0
            return json.dumps({"mission": {}})
        raise AssertionError(path)


def _runner(tmp_path, now, **cfg):
    sv = SimpleNamespace(level=1, value=0, comment=0, remain=0, login=True, targeted=False, polls={}, calls=[], now=now,
                         new_rooms={2}, new_comments=0, new_remain=1, kik_comments=0)
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


async def test_comments_in_new_and_kikkake_rooms_and_receives_their_limited_rewards(tmp_path):
    r, sv = _runner(tmp_path, jst(2026, 10, 22, 21, 5).timestamp())
    sv.targeted = True
    sv.new_rooms = {2, 7, 8, 9}
    lives = _onlives()

    async def more():
        for live in lives["onlives"][0]["lives"]:
            if live["room_id"] in sv.new_rooms:
                live["new_days_count_label"] = "New3day"
        return lives

    r._fetch_onlives = more
    await r.step(mission.parse_times("21:00"))
    posts = [c for c in sv.calls if c[0] == "POST"]
    commented = [p[2]["live_id"] - 1000 for p in posts if p[1] == "/api/live/post_live_comment"]
    assert sorted(commented) == [1, *sorted(set(commented) & {2, 7, 8, 9})]
    assert len(set(commented) & {2, 7, 8, 9}) == 3  # 毎日の 3 ルームで足りる
    assert ("POST", "/api/mission/receive", {"mission_id": 11}) in posts  # 新人のルームの中で受け取る
    assert r.state["kikkake"] == 1 and r.state["new"] >= 3  # 新人は毎日の 3 つを先に見る


async def test_limited_cumulative_forty_comments_up_to_the_cap_without_gifts_or_follows(tmp_path):
    r, sv = _runner(tmp_path, jst(2026, 11, 2, 21, 5).timestamp())
    sv.targeted = True
    sv.new_rooms = {2, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18}
    sv.new_comment_title = "新人ライバーの配信を視聴して3ルームでコメントしよう"
    sv.new_comment_target = 3
    sv.new_comment_daily = 0  # 画面の「毎日」は題の外
    sv.extra_new_missions = [
        {"mission_id": 12, "title": "新人ライバーの配信を視聴して通算10ルームでコメントしよう",
         "current_value": 0, "target_value": 10, "is_daily": 0, "is_active": 1, "remain_reward": 0},
        {"mission_id": 13, "title": "新人ライバーの配信を視聴して通算40ルームでコメントしよう",
         "current_value": 0, "target_value": 40, "is_daily": 0, "is_active": 1, "remain_reward": 0},
        {"mission_id": 14, "title": "新人ライバーに文字ギフト「はじめまして」or「初見です」を2回贈ろう",
         "current_value": 0, "target_value": 2, "is_daily": 0, "is_active": 1, "remain_reward": 0},
        {"mission_id": 15, "title": "新人ライバーを10ルームフォローしよう",
         "current_value": 0, "target_value": 10, "is_daily": 0, "is_active": 1, "remain_reward": 0},
    ]
    lives = _onlives()

    async def more():
        for live in lives["onlives"][0]["lives"]:
            if live["room_id"] in sv.new_rooms:
                live["new_days_count_label"] = "New7day"
        return lives

    r._fetch_onlives = more
    await r.step(mission.parse_times("21:00"))
    posts = [c for c in sv.calls if c[0] == "POST"]
    commented = {p[2]["live_id"] - 1000 for p in posts if p[1] == "/api/live/post_live_comment"}
    assert r.state["new"] >= mission.LIMITED_MAX
    assert len(commented & sv.new_rooms) == mission.LIMITED_MAX  # 期間限定の通算40を 1 回の上限まで先に
    assert 1 in commented  # きっかけ配信も (対象のルームは 1 つ)
    assert not any("gift" in p[1] or "follow" in p[1] for p in posts)
    assert {p[1] for p in posts} <= {"/api/live/post_live_comment", "/api/mission/receive"}


async def test_keeps_viewing_limited_rooms_after_the_daily_twenty(tmp_path):
    r, sv = _runner(tmp_path, jst(2026, 11, 13, 21, 5).timestamp(), mission_comment="off")
    sv.level, sv.value = 20, 1  # 毎日の 20 ルームは済んでいる
    sv.new_rooms = set(range(6, 20))
    sv.extra_new_missions = [{"mission_id": 16, "title": "新人ライバーの配信を通算10回視聴しよう", "current_value": 4,
                              "target_value": 10, "is_daily": 0, "is_active": 1, "remain_reward": 0}]
    lives = _onlives()

    async def more():
        for live in lives["onlives"][0]["lives"]:
            if live["room_id"] in sv.new_rooms:
                live["new_days_count_label"] = "New7day"
        return lives

    r._fetch_onlives = more
    await r.step(mission.parse_times("21:00"))
    assert r.state["views"] == r.state["new"] == 6  # 残りの 6 回を新人のルームだけで
    assert r.state["done"]


async def test_does_nothing_more_when_daily_is_done_and_no_limited_left(tmp_path):
    r, sv = _runner(tmp_path, jst(2026, 11, 13, 21, 5).timestamp())
    sv.level, sv.value, sv.comment = 20, 1, 1
    sv.new_comments = 3
    await r.step(mission.parse_times("21:00"))
    assert r.state["views"] == 0 and r.state["done"]


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
