import json

from showroom_keeper import backtest, backtest_pair as bp
from showroom_keeper.research_store import Store

DAY = 86400
T0 = backtest.month_start("2026-04")


def _event(s, eid, start, rooms, read=2 * DAY, days=7):
    """rooms: {room_id: [(user_id, point), ...]}。read = 終わってから貢献ランキングを読むまで (負なら開催中に読んだ)。"""
    end = start + days * DAY
    s.upsert_events([{"event_id": eid, "name": f"イベント{eid}", "start": start, "end": end}])
    s.set_event_rooms(eid, [{"room_id": r, "point": sum(p for _, p in us) or 1, "rank": i + 1} for i, (r, us) in enumerate(rooms.items())],
                      end + 2 * DAY)
    for r, us in rooms.items():
        s.set_contributions(eid, r, [{"user_id": u, "name": f"u{u}", "point": p, "rank": k + 1} for k, (u, p) in enumerate(us)],
                            end + read, final=True)


def _fill(s):
    s.upsert_rooms([{"room_id": r, "key": f"k{r}", "name": f"ルーム{r}"} for r in (1, 2, 3)], "event", 0)
    # ルーム 1: T より前に 4 回 (2 週間ごと)。7 は毎回、8 は最初の回だけ。3 回目は開催中にしか読めていない (使えない回)
    for k in range(4):
        users = [(7, 1000 * (k + 1))] + ([(8, 5000)] if k == 0 else [])
        _event(s, 10 + k, T0 - (70 - k * 14) * DAY, {1: users}, read=-3600 if k == 2 else 2 * DAY)
    # 4 月に始まった回 (目当て): 7 は 3500、8 は来ない (載った人が 100 人に届かないので 0)、9 は新顔
    _event(s, 20, T0 + 5 * DAY, {1: [(7, 3500), (9, 100)]})
    # ルーム 2: 目当ての回を開催中にしか読めていない (使わない)
    _event(s, 30, T0 - 30 * DAY, {2: [(7, 10)]})
    _event(s, 31, T0 + 6 * DAY, {2: [(7, 20)]}, read=-3600)
    # ルーム 3: 100 人の一覧。前からの 50 は目当ての回に載っていない (100 位の外なので量は分からない)
    _event(s, 40, T0 - 30 * DAY, {3: [(50, 900)] + [(1000 + j, 800) for j in range(99)]})
    _event(s, 41, T0 + 7 * DAY, {3: [(2000 + j, 1000) for j in range(100)]})


def test_pairs_are_scored_by_room_and_event(tmp_path):
    with Store(tmp_path) as s:
        _fill(s)
    before = (tmp_path / "research.db").read_bytes()
    out = bp.run(tmp_path, ["2026-04"], ["2026-05"])
    assert (tmp_path / "research.db").read_bytes() == before  # 写しは書き換えない
    d = out["periods"]["dev"]["all"]
    assert d["label_dropped"] == {"not_read_after_end": 1}  # ルーム 2 の目当ての回
    assert d["room_events"] == 2  # ルーム 1・3
    # ルーム 1 の前からの人は 7・8、ルーム 3 は 100 人 (どれも推測でない行)
    assert d["cases"] == 102 and d["listed"] == 1 and d["true_zero"] == 1 and d["outside_top100"] == 100
    assert d["newcomers"] == 101 and d["listed_all"] == 102  # 9 と、ルーム 3 の 100 人
    e = d["expected"]
    # 7: 直前の使える回 (4 回目) は 4000 → 実際 3500。8: 直前の回に載っていないので 0 → 実際 0
    assert e["naive_last"]["n"] == 2 and e["naive_last"]["mae"] == 250 and e["naive_last"]["bias"] == 250
    c = d["came"]
    # 7 の載った回 (新しい順): 4000・2000・1000 (3 回目は使えない回なので入らない)。中ほど 2000
    assert c["med3"]["mae"] == 1500 and c["naive_last_seen"]["mae"] == 500 and c["second"]["mae"] == 1500
    # 出てくるか: 載った 7 と、載らなかった 8 + ルーム 3 の 100 人
    assert d["appear"]["naive_last"]["pairs"] == 101 and d["appear"]["naive_last"]["auc"] > 0.5
    assert d["band"]["min_max6"] == {"n": 1, "inside": 1.0, "below": 0.0, "above": 0.0}  # 1000〜4000 に 3500
    assert out["periods"]["check"]["all"]["cases"] == 0 and "遡及" in out["kind"] and "予定既知" in out["kind"]
    assert out["periods"]["dev"]["months"]["2026-04"]["cases"] == 102
    json.dumps(out)


def test_rate_denominator_skips_rounds_that_were_not_read(tmp_path):
    with Store(tmp_path) as s:
        _fill(s)
    with bp.backtest_event.connect(tmp_path) as conn:
        rounds = bp.load_room(conn, 1, T0 - 400 * DAY, T0 + 60 * DAY)
    assert [r.why for r in rounds] == ["usable", "usable", "not_read_after_end", "usable", "usable"]
    t = bp.Tally()
    seen = []

    class Spy(bp.Tally):
        def add(self, f, listed, amount, confirmed):
            seen.append((f, listed, amount))

    bp.score_room(rounds, [20], [t, Spy()])
    f7 = next(f for f, listed, _ in seen if listed)
    # 7 は使える 3 回すべてに載った (取れなかった回を分母に入れない) が、ルームが出た 4 回で割ると 3/4 になる
    assert f7["appear"]["rate180"] == 1.0 and f7["appear"]["rate180_old"] == 0.75
    assert f7["expected"]["med3_rate180"] == 2000 and f7["rounds"] == 3
