import json

from showroom_keeper.activity import ActivityTracker, parse_event


def msg(t, u):
    return "MSG\t444c:123\t" + json.dumps({"t": t, "u": u, "created_at": 0})


def test_parse_event():
    assert parse_event(msg(1, 42)) == (1, 42)
    assert parse_event(msg(18, 7).encode()) == (18, 7)
    assert parse_event("ACK\tshowroom") is None
    assert parse_event("MSG\tkey\t{broken") is None
    assert parse_event("MSG\tkey\t" + json.dumps({"t": 8, "telop": "x"})) is None  # ユーザーの無いイベント


def test_counts_unique_active_users_in_window():
    tr = ActivityTracker(window=600)
    tr.reset(0)
    tr.feed(msg(1, 1), 10)     # コメント
    tr.feed(msg(2, 1), 20)     # 同じ人のギフト
    tr.feed(msg(18, 2), 30)    # 入室
    tr.feed(msg(8, 3), 40)     # テロップ等は数えない
    tr.feed("MSG\tk\t" + json.dumps({"t": 5, "u": 9}), 50)  # 対象外の種類
    assert tr.count(100) == 2
    assert tr.count(615) == 2  # 1 番の最後は 20 秒 (595 秒前)、2 番は 30 秒 (585 秒前)
    assert tr.count(625) == 1  # 1 番は 600 秒を過ぎたので外れる
    assert tr.observed_seconds(625) == 625
