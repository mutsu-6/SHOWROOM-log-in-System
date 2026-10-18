from datetime import datetime

from showroom_keeper import gifter_analysis as ga
from showroom_keeper.research_store import Store
from showroom_keeper.status import JST

NOW = datetime(2026, 10, 18, 12, tzinfo=JST).timestamp()


def _end(month: str) -> float:
    y, m = map(int, month.split("-"))
    return datetime(y, m, 20, 12, tzinfo=JST).timestamp()


def _event(s, eid, month, rows):
    """rows = {room_id: {user_id: point}}"""
    s.upsert_events([{"event_id": eid, "start": _end(month) - 7 * 86400, "end": _end(month)}])
    s.set_event_rooms(eid, [{"room_id": r, "point": 1, "rank": 1} for r in rows], NOW)
    for r, users in rows.items():
        s.set_contributions(eid, r, [{"user_id": u, "point": p} for u, p in users.items()], NOW, final=True)


def test_room_states_switch_and_tiers(tmp_path):
    with Store(tmp_path) as s:
        s.set_meta("user_month_upto", True)
        eid = 0
        # 1 はルーム 10 を 3 月から 6 月まで推し (月 1000)、7 月からルーム 20 に乗り換えた
        for m in ("2026-03", "2026-04", "2026-05", "2026-06"):
            eid += 1
            _event(s, eid, m, {10: {1: 1000}, 30: {1: 100}})
        for m in ("2026-07", "2026-08", "2026-09"):
            eid += 1
            _event(s, eid, m, {10: {1: 50}, 20: {1: 2000}})
        # ルーム 40 は 9 月に初めて
        eid += 1
        _event(s, eid, "2026-09", {40: {1: 10, **{100 + i: i for i in range(1, 200)}}})
        st = ga.gifter_states(s, 1, NOW)
    assert st["month"] == "2026-09"
    by = {r["room_id"]: r["state"] for r in st["rooms"]}
    assert by[20] == "安定" and by[40] == "新しく出た"
    assert by[10] == "減少"  # 6 月の 1000 が前の 3 か月の平均に入る
    assert by[30] == "ルームが休み"  # 6 月まで送っていたが、9 月にルーム 30 はイベントに出ていない (休眠にしない)
    assert st["switch"]["from"] == 10 and st["switch"]["to"] == 20
    sep = st["tiers"][-1]
    assert sep["month"] == "2026-09" and sep["tier"] == "大口"  # 9 月に貢献ランキングに出た 200 人のうち、いちばん多い
    assert sep["people"] == 200


def test_room_state_rules():
    per = {"2026-06": 100, "2026-07": 100, "2026-08": 100}
    assert ga._room_state({**per, "2026-09": 200}, "2026-09", True) == "増加"
    assert ga._room_state({**per, "2026-09": 30}, "2026-09", True) == "減少"
    assert ga._room_state(per, "2026-09", True) == "休眠"
    assert ga._room_state(per, "2026-09", False) == "ルームが休み"  # イベントに出ていなければ休眠にしない
    assert ga._room_state({"2026-01": 5, "2026-09": 5}, "2026-09", True) == "復帰"
