import json

import pytest

from showroom_keeper.comments import MAX_LENGTH, CommentDispatcher, enqueue, validate, wait_result


class Target:
    def __init__(self, key, ok=True):
        self.key, self.ok, self.posted = key, ok, []

    async def post_comment(self, text):
        self.posted.append(text)
        return self.ok, "投稿しました" if self.ok else "失敗"


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def make(tmp_path, targets, clock):
    slept = []

    async def sleep(s):
        slept.append(s)
        clock.t += s

    return CommentDispatcher(tmp_path, targets, clock=clock, sleep=sleep), slept


def result(tmp_path, req_id):
    return json.loads((tmp_path / f"{req_id}.result.json").read_text(encoding="utf-8"))


def test_validate():
    assert validate("  こんにちは \n 世界 ") == "こんにちは 世界"
    with pytest.raises(ValueError):
        validate("   ")
    with pytest.raises(ValueError):
        validate("あ" * (MAX_LENGTH + 1))


async def test_posts_to_single_room_and_spaces_out_posts(tmp_path):
    clock = Clock()
    t = Target("room1")
    d, slept = make(tmp_path, [t], clock)
    a = enqueue(tmp_path, "一つ目", now=clock.t)
    b = enqueue(tmp_path, "二つ目", now=clock.t)
    await d.process_pending()
    assert t.posted == ["一つ目", "二つ目"]
    assert slept == [3.0]  # 2 件目は最小間隔まで待つ
    assert result(tmp_path, a)["ok"] and result(tmp_path, b)["ok"]
    assert not list(tmp_path.glob("*.req.json"))


async def test_requires_room_when_multiple(tmp_path):
    clock = Clock()
    t1, t2 = Target("r1"), Target("r2")
    d, _ = make(tmp_path, [t1, t2], clock)
    a = enqueue(tmp_path, "hi", now=clock.t)
    b = enqueue(tmp_path, "hi", room="r2", now=clock.t)
    await d.process_pending()
    assert not result(tmp_path, a)["ok"]
    assert result(tmp_path, b)["ok"] and t2.posted == ["hi"] and t1.posted == []


async def test_stale_request_is_not_posted(tmp_path):
    clock = Clock()
    t = Target("r1")
    d, _ = make(tmp_path, [t], clock)
    a = enqueue(tmp_path, "old", now=clock.t - 120)
    await d.process_pending()
    assert t.posted == [] and not result(tmp_path, a)["ok"]


async def test_wait_result_timeout_cancels_request(tmp_path):
    req_id = enqueue(tmp_path, "hi")
    assert await wait_result(tmp_path, req_id, timeout=0.1) is None
    assert not (tmp_path / f"{req_id}.req.json").exists()
