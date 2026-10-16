import asyncio

from showroom_keeper.api import RoomStatus
from showroom_keeper.config import Config
from showroom_keeper.entrance import RoomEntrance, is_join_frame


class FakeWS:
    def __init__(self):
        self.handlers = {}

    def on(self, event, cb):
        self.handlers[event] = cb


class FakePage:
    """goto のあと、少し遅れて配信ページのリアルタイム通信が SUB を送る (send_sub=False なら送らない)。"""

    def __init__(self, send_sub=True, fail=False):
        self.send_sub, self.fail = send_sub, fail
        self.visited, self.closed, self.closed_before_sub = [], False, None
        self._on_ws = None

    def on(self, event, cb):
        if event == "websocket":
            self._on_ws = cb

    async def goto(self, url, **kw):
        self.visited.append((url, kw.get("wait_until")))
        if self.fail:
            raise RuntimeError("net::ERR_CONNECTION_RESET")
        if self.send_sub:
            asyncio.get_running_loop().call_later(0.02, self._send_sub)

    def _send_sub(self):
        self.closed_before_sub = self.closed
        ws = FakeWS()
        self._on_ws(ws)
        ws.handlers["framesent"]("SUB\tabc123")

    async def close(self):
        self.closed = True


def make(statuses, state_dir=None, **page_kw):
    pages = []

    async def fetch(key):
        value = statuses[key]
        if isinstance(value, Exception):
            raise value
        return value

    async def new_page():
        pages.append(FakePage(**page_kw))
        return pages[-1]

    entrance = RoomEntrance(list(statuses), 60, fetch, new_page, state_dir=state_dir, enter_timeout=0.2, stay=0.01)
    return entrance, pages


def live(key, live_id):
    return RoomStatus(key, 1, key, is_live=live_id is not None, live_id=live_id)


def test_join_frame():
    assert is_join_frame("SUB\tabc") and is_join_frame(b"SUB\tabc")
    assert not is_join_frame("PING\tshowroom") and not is_join_frame(None)


async def test_enters_once_per_live_after_joining_and_closes_the_page():
    statuses = {"a": live("a", 10), "b": live("b", None)}
    entrance, pages = make(statuses)
    await entrance.step()
    await entrance.step()
    # 配信中の a に 1 回だけ入室する。配信に参加 (SUB) する前には閉じない。配信していない b は開かない
    assert len(pages) == 1 and pages[0].closed and pages[0].closed_before_sub is False
    assert pages[0].visited == [("https://www.showroom-live.com/r/a", "commit")]
    # 次の配信が始まったらもう一度入室する
    statuses["a"] = live("a", 11)
    await entrance.step()
    assert len(pages) == 2 and entrance.entered == {"a": 11}


async def test_gives_up_when_the_page_never_joins():
    entrance, pages = make({"a": live("a", 10)}, send_sub=False)
    await entrance.step()
    # 上限で打ち切って閉じ、入室済みにはしない (次の確認で入室し直す)
    assert len(pages) == 1 and pages[0].closed and entrance.entered == {}


async def test_failed_entry_is_retried_and_other_rooms_continue():
    statuses = {"broken": RuntimeError("HTTP 500"), "a": live("a", 10)}
    entrance, pages = make(statuses, fail=True)
    await entrance.step()
    assert len(pages) == 1 and pages[0].closed and entrance.entered == {}
    await entrance.step()
    assert len(pages) == 2


async def test_entered_lives_survive_a_restart(tmp_path):
    entrance, pages = make({"a": live("a", 10)}, state_dir=tmp_path)
    await entrance.step()
    assert len(pages) == 1
    # ブラウザの再起動や設定の反映のあとも、同じ配信には入室し直さない
    again, pages = make({"a": live("a", 10)}, state_dir=tmp_path)
    await again.step()
    assert pages == [] and again.entered == {"a": 10}


def test_entry_page_is_counted_in_the_memory_estimate():
    import pytest

    kp = pytest.importorskip("showroom_keeper.keeper")
    rooms = ["a", "b", "c"]
    # 3 ルームは約 2GB。入室用のページ 1 つ分を足すと 2.0GB では足りない
    assert kp.resolve_keep_open(Config(rooms=rooms), mem=2.0) is True
    assert kp.resolve_keep_open(Config(rooms=rooms, enter_rooms=["x"]), mem=2.0) is False
    # 視聴するルームと重なる分は入室しないので数えない
    assert kp.resolve_keep_open(Config(rooms=rooms, enter_rooms=["a"]), mem=2.0) is True
