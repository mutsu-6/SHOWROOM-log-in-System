import pytest

from showroom_keeper import live_contrib
from tests.test_explore import NOW, db  # noqa: F401 - 同じ記録を使う


@pytest.fixture(autouse=True)
def fresh():
    live_contrib.forget()
    yield
    live_contrib.forget()


def ranking(rows):
    return {"ranking": [{"user_id": u, "name": n, "point": p, "rank": i + 1} for i, (u, n, p) in enumerate(rows)]}


def fake(calls):
    data = {1: ranking([(7, "大口", 60), (8, "中堅", 40)]), 2: ranking([(7, "大口", 150), (9, "少額", 50)])}

    def get(path):
        calls.append(path)
        return data[int(path.rsplit("=", 1)[1])]
    return get


def test_reads_ongoing_event_and_sums_rooms(db):  # noqa: F811
    calls = []
    d = live_contrib.event_live(db, "11", get=fake(calls), now=NOW, paused=lambda: False)
    assert d["live"] and d["failed"] == 0 and d["room_total"] == 2
    assert [r["room_id"] for r in d["rooms"]] == [2, 1]  # ポイントの多い順に読む
    assert [(c["user_id"], c["point"], c["rooms"], c["top_room"]) for c in d["contributors"]] == \
        [(7, 210, 2, "ルームB"), (9, 50, 1, "ルームB"), (8, 40, 1, "ルームA")]
    assert [x["point"] for x in d["by_room"]["1"]] == [60, 40]
    # 使い回す間は問い合わせない
    live_contrib.event_live(db, "11", get=fake(calls), now=NOW + 60, paused=lambda: False)
    assert len(calls) == 2
    live_contrib.event_live(db, "11", get=fake(calls), now=NOW + live_contrib.LIVE_TTL + 1, paused=lambda: False)
    assert len(calls) == 4


def test_does_not_read_ended_or_while_paused(db):  # noqa: F811
    calls = []
    assert not live_contrib.event_live(db, "10", get=fake(calls), now=NOW, paused=lambda: False)["live"]
    assert not live_contrib.event_live(db, "12", get=fake(calls), now=NOW, paused=lambda: False)["live"]
    assert not live_contrib.event_live(db, "11", get=fake(calls), now=NOW, paused=lambda: True)["live"]
    assert calls == []


def test_one_room_failing_keeps_the_rest(db):  # noqa: F811
    def get(path):
        if path.endswith("room_id=2"):
            raise OSError("timeout")
        return ranking([(8, "中堅", 40)])
    d = live_contrib.event_live(db, "11", get=get, now=NOW, paused=lambda: False)
    assert d["failed"] == 1 and [c["user_id"] for c in d["contributors"]] == [8]
