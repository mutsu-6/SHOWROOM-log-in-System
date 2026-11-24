"""イベントのページのチャート (出来高・順位のローソク足・上下との差・勢い・値上がり率)。"""

import time

import pytest

from showroom_keeper import event_chart as ec, once
from showroom_keeper.research_store import Store

H = 3600
NOW = time.time()
# 日本時間の 0 時ちょうどから始める (日足の区切りを確かめやすく)
T0 = (int(NOW + ec.JST) // 86400 - 3) * 86400 - ec.JST


@pytest.fixture
def db(tmp_path):
    once._done.clear()
    with Store(tmp_path) as s:
        s.upsert_rooms([{"room_id": r, "key": f"k{r}", "name": f"ルーム{r}"} for r in (1, 2, 3, 4)], "live", NOW)
        s.upsert_events([{"event_id": 5, "name": "開催中", "start": T0, "end": NOW + 10 * H}])
        # 1 時間ごと。ただし 10〜13 時間目は記録が無い (観測なし)。ルーム 3 は途中から
        for k in range(0, int((NOW - T0) // H)):
            if 10 < k < 14:
                continue
            rows = [{"room_id": 1, "point": 100 * k, "rank": None}, {"room_id": 2, "point": 150 * k}, {"room_id": 4, "point": 10 * k}]
            if k >= 30:
                rows.append({"room_id": 3, "point": 1000 + 500 * (k - 30)})
            rows.sort(key=lambda r: -r["point"])
            for i, r in enumerate(rows):
                r["rank"] = i + 1
            s.add_progress(5, rows, T0 + k * H)
    return tmp_path


def test_steps_mark_gaps_and_do_not_treat_them_as_zero():
    series = [(0, 0, 1), (H, 100, 1), (2 * H, 250, 1), (6 * H, 650, 2)]
    st = ec.steps(series)
    assert [x["gain"] for x in st] == [100, 150, 400]
    assert [x["gap"] for x in st] == [False, False, True]
    assert st[2]["rate"] == pytest.approx(100)  # 4 時間で 400 = 1 時間あたり 100 (0 とも 400 ともしない)
    assert st[1]["ma24"] == pytest.approx(125)


def test_drop_is_kept_as_is():
    st = ec.steps([(0, 500, 1), (H, 300, 1)])
    assert st[0]["gain"] == -200 and st[0]["drop"]


def test_buckets_flag_partial_hours():
    st = ec.steps([(T0 + k * H, 10 * k, 1) for k in range(0, 10)])  # 0 時〜9 時 (日本時間)
    six = ec.buckets(st, 6 * H)
    assert six[0]["gain"] == 60 and six[0]["partial"] is False  # 0〜6 時の足 6 本
    assert six[1]["gain"] == 30 and six[1]["partial"] is True  # 6〜12 時は 9 時までしか見ていない
    assert six[1]["hours"] == pytest.approx(3)


def test_rank_candles_use_best_and_worst():
    c = ec.candles([(T0 + H, 0, 5), (T0 + 2 * H, 0, 2), (T0 + 3 * H, 0, 7), (T0 + 4 * H, 0, 4), (T0 + 86400 + H, 0, 3)])
    assert c[0] == {"day": c[0]["day"], "open": 5, "close": 4, "high": 2, "low": 7, "n": 4}
    assert c[1]["open"] == c[1]["close"] == 3


def test_event_chart_picks_neighbours_momentum_and_projection(db):
    d = ec.event_chart(db, 5, room="1")
    assert d["found"] and d["room"]["room_id"] == 1
    roles = {l["role"]: l["room_id"] for l in d["lines"]}
    assert roles == {"target": 1, "above": 2, "below": 4}  # 今のポイントの順は 3, 2, 1, 4
    st = d["stats"]
    assert st["rate"] == pytest.approx(100)  # 1 時間に 100 ずつ
    assert st["ratio"] == pytest.approx(1.0)
    assert st["projected"] == pytest.approx(st["point"] + 100 * st["hours_left"])
    assert st["above_gap"] == 50 * (int((NOW - T0) // H) - 1)
    assert st["need_rate"] == pytest.approx(150 + st["above_gap"] / st["hours_left"])
    gaps = [b for b in d["bars"]["1h"] if b["gap"]]
    assert len(gaps) == 1 and gaps[0]["hours"] == pytest.approx(4)
    # 重ねたルーム 3 は途中からなので、それより前の差は None (0 にしない)
    d = ec.event_chart(db, 5, room="1", compare=["3"])
    three = next(l for l in d["lines"] if l["room_id"] == 3)
    assert three["role"] == "compare" and three["diff"][0] is None and three["points"][0] is None
    assert three["diff"][-1] == d["stats"]["point"] - three["points"][-1]


def test_movers_leave_missing_history_empty(db):
    d = ec.event_chart(db, 5)
    by = {m["room_id"]: m for m in d["movers"]}
    assert by[3]["rate"] == pytest.approx(500)
    assert by[2]["rate"] == pytest.approx(150)
    assert by[4]["ratio"] == pytest.approx(1.0)


def test_default_room_is_the_support_room(db):
    assert ec.event_chart(db, 5, support=["k4"])["room"]["room_id"] == 4
    assert ec.event_chart(db, 5, room="k2")["room"]["room_id"] == 2


def test_compare_rooms_are_added(db):
    d = ec.event_chart(db, 5, room="4", compare=["1", "3"])  # 1 はもう「1 つ上」なので重ねない
    assert [(l["room_id"], l["role"]) for l in d["lines"]] == [(4, "target"), (1, "above"), (3, "compare")]  # 4 は最下位なので下は無い


def test_event_without_progress(tmp_path):
    once._done.clear()
    with Store(tmp_path) as s:
        s.upsert_events([{"event_id": 9, "name": "x", "start": NOW - H, "end": NOW + H}])
    d = ec.event_chart(tmp_path, 9)
    assert d["found"] and d["room"] is None and d["movers"] == []
    assert ec.event_chart(tmp_path, 99) == {"event_id": 99, "found": False}


def test_momentum_compares_with_the_same_hours_of_the_day_before():
    # 直近 6 時間は 1 時間 50、前の日の同じ 6 時間も 1 時間 50 (その前の 24 時間の平均は 1 時間 200 でも、1 倍と出す)
    m = ec._momentum((10300, 30 * H), (10000, 24 * H), (4000, 0), (4300, 6 * H))
    assert m["rate"] == pytest.approx(50) and m["base_rate"] == pytest.approx(50) and m["ratio"] == pytest.approx(1)
    assert ec._momentum((10300, 30 * H), None, (4000, 0), (4300, 6 * H))["ratio"] is None  # 直近の記録が無い
    assert ec._momentum((10300, 30 * H), (10000, 24 * H), None, None)["ratio"] is None  # 前の日の記録が無い
    assert ec._momentum((10300, 30 * H), (10000, 24 * H), (4000, 0), (4000, 6 * H))["ratio"] is None  # 前の日に 0 (割れない)
