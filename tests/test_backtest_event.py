import json

from showroom_keeper import backtest, backtest_event as be
from showroom_keeper.research_store import Store

DAY = 86400


def _event(s, eid, start, days, rooms, read_after=True, count=None):
    """rooms: {room_id: point}。read_after = 正式な結果のあとに参加ルームを読み直したか。"""
    end = start + days * DAY
    s.upsert_events([{"event_id": eid, "name": f"イベント{eid}", "start": start, "end": end, "type": "quest"}])
    s.set_event_rooms(eid, [{"room_id": r, "point": p, "rank": i + 1} for i, (r, p) in enumerate(rooms.items())],
                      end + (2 * DAY if read_after else 600))
    if count is not None:
        with s.tx() as c:
            c.execute("UPDATE events SET room_count=? WHERE event_id=?", (count, eid))


def _fill(s):
    s.upsert_rooms([{"room_id": r, "key": f"k{r}", "name": f"ルーム{r}"} for r in (1, 2, 3)], "event", 0)
    t = backtest.month_start("2026-04")
    for k in range(4):  # 3 月までに 4 回。ルーム 1 は 1000・2000・3000・4000、ルーム 2 は 1 回だけ
        _event(s, 10 + k, t - (40 - k * 9) * DAY, 7, {1: 1000 * (k + 1), **({2: 500} if k == 3 else {})})
    _event(s, 20, t + 5 * DAY, 7, {1: 5000, 2: 600, 3: 100})  # 4 月に始まった回
    _event(s, 21, t + 15 * DAY, 3, {1: 9000}, read_after=False)  # 正式な結果のあとに読んでいない (使わない)
    _event(s, 22, t + 20 * DAY, 3, {1: 1, 2: 1}, count=be.ROOM_CAP)  # 参加ルームが上限で切れているかもしれない
    with s.tx() as c:
        c.execute("UPDATE room_events SET point=NULL WHERE event_id=20 AND room_id=3")


def test_room_points_are_compared_with_simple_history(tmp_path):
    with Store(tmp_path) as s:
        _fill(s)
    before = (tmp_path / "research.db").read_bytes()
    out = be.run(tmp_path, ["2026-04"], ["2026-05"])
    assert (tmp_path / "research.db").read_bytes() == before  # 写しは書き換えない
    d = out["periods"]["dev"]["all"]
    assert d["label_ok"] == 2  # イベント 20 のルーム 1・2
    assert d["label_dropped"] == {"point_missing": 1, "not_read_after_result": 1, "room_list_capped": 2}
    assert d["coverage"] == {"b0a": 1.0, "b0b": 0.5}  # ルーム 2 は履歴が 1 回だけ (0 にしない)
    assert d["common"] == 1
    m = d["methods"]
    assert m["b0a"]["mae"] == 1000 and m["b0a"]["bias"] == -1000  # 直前 4000、実際 5000
    assert m["b0b"]["mae"] == 2000  # 直近 3 回 (2000・3000・4000) の中央値 3000
    assert out["periods"]["check"]["all"]["label_ok"] == 0 and "遡及" in out["kind"]
    json.dumps(out)
