from showroom_keeper.api import RoomStatus
from showroom_keeper.entrance import RoomEntrance


class FakePage:
    def __init__(self, fail=False):
        self.fail, self.visited, self.closed = fail, [], False

    async def goto(self, url, **kw):
        self.visited.append((url, kw.get("wait_until")))
        if self.fail:
            raise RuntimeError("Timeout 60000ms exceeded")

    async def close(self):
        self.closed = True


def make(statuses, fail=False):
    pages = []

    async def fetch(key):
        value = statuses[key]
        if isinstance(value, Exception):
            raise value
        return value

    async def new_page():
        pages.append(FakePage(fail))
        return pages[-1]

    return RoomEntrance(list(statuses), 60, fetch, new_page), pages


def live(key, live_id):
    return RoomStatus(key, 1, key, is_live=live_id is not None, live_id=live_id)


async def test_enters_once_per_live_and_closes_the_page():
    statuses = {"a": live("a", 10), "b": live("b", None)}
    entrance, pages = make(statuses)
    await entrance.step()
    await entrance.step()
    # 配信中の a に 1 回だけ入室し、読み込みが終わったら閉じる。配信していない b は開かない
    assert len(pages) == 1 and pages[0].closed
    assert pages[0].visited == [("https://www.showroom-live.com/r/a", "load")]
    # 次の配信が始まったらもう一度入室する
    statuses["a"] = live("a", 11)
    await entrance.step()
    assert len(pages) == 2 and entrance.entered == {"a": 11}


async def test_failed_entry_is_retried_and_other_rooms_continue():
    statuses = {"broken": RuntimeError("HTTP 500"), "a": live("a", 10)}
    entrance, pages = make(statuses, fail=True)
    await entrance.step()
    assert len(pages) == 1 and pages[0].closed and entrance.entered == {}
    await entrance.step()
    assert len(pages) == 2
