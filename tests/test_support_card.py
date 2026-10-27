from showroom_keeper import research, support_card
from showroom_keeper.research_store import Store

DAY = 86400
NOW = 1_790_000_000.0


def _event(s, eid, end, rooms, read=2 * DAY):
    """rooms: {room_id: [(user_id, point), ...]}。read = 終わってから貢献ランキングを読むまで (負なら開催中に読んだ)。"""
    s.upsert_events([{"event_id": eid, "name": f"イベント{eid}", "start": end - 7 * DAY, "end": end}])
    s.set_event_rooms(eid, [{"room_id": r, "point": sum(p for _, p in us) or 1, "rank": i + 1} for i, (r, us) in enumerate(rooms.items())],
                      end + 2 * DAY)
    for r, us in rooms.items():
        s.set_contributions(eid, r, [{"user_id": u, "name": f"u{u}", "point": p, "rank": k + 1} for k, (u, p) in enumerate(us)],
                            end + read, final=True)


def _fill(s):
    s.upsert_rooms([{"room_id": r, "key": f"k{r}", "name": f"ルーム{r}"} for r in (1, 2)], "event", 0)
    # ルーム 1: 7 は 5 回載った (1000〜5000)。開催中にしか読めていない回 (9000) は使わない。2 年前の 20000 は最大に入る
    _event(s, 1, NOW - 700 * DAY, {1: [(7, 20_000)]})
    for k in range(5):
        _event(s, 10 + k, NOW - (200 - k * 40) * DAY, {1: [(7, 1000 * (k + 1))]})
    _event(s, 20, NOW - 20 * DAY, {1: [(7, 9000)]}, read=-3600)
    # 7 が載っていない回: 100 人の一覧 (100 位の外) と、2 人の一覧 (0)
    _event(s, 21, NOW - 50 * DAY, {1: [(1000 + j, 10_000) for j in range(100)]})
    _event(s, 22, NOW - 60 * DAY, {1: [(8, 10), (9, 5)]})
    # ルーム 2: 1 回だけ
    _event(s, 30, NOW - 30 * DAY, {2: [(7, 300)]})


def test_support_record_by_room(tmp_path):
    with Store(tmp_path) as s:
        _fill(s)
        out = support_card.by_room(s, 7, NOW)
    r1, r2 = out["rooms"]
    assert out["room_total"] == 2 and r1["room_id"] == 1 and r2["room_id"] == 2
    assert r1["max"]["point"] == 20_000 and r1["max"]["event_id"] == 1 and r1["max"]["rounds"] == 6  # 開催中にしか読めていない回は入らない
    # 365 日に載った回: 1000〜5000 の 5 回 (200 日前から 40 日ごと)。中ほど 3000、4 分の 1〜4 分の 3 は 2000〜4000
    assert r1["normal"] == {"point": 3000, "rounds": 5, "low": 2000, "high": 4000}
    assert r1["recent"] == {"point": 4500, "rounds": 2, "ratio": 1.5}  # 80 日前の 4000 と 40 日前の 5000
    y = r1["year"]
    # ルームの回 (365 日): 載った 5・100 位の外 1・0 が 1・読めていない 1
    assert y == {"ran": 8, "usable": 7, "listed": 5, "outside_top100": 1, "zero": 1, "unknown": 1}
    # 180 日: 使える回は 10 (200 日前) を除く 12〜14 と 11 (160 日前)・21・22。読めていない回は分母に入れない
    assert r1["share180"] == {"listed": 4, "usable": 6}
    assert r1["reliability"] == "中" and r2["reliability"] == "参考"
    assert r2["normal"]["low"] is None and r2["year"]["listed"] == 1


def test_gifter_page_has_support_record(tmp_path):
    with Store(tmp_path) as s:
        _fill(s)
    d = research.gifter_detail(tmp_path, 7, [])
    assert [r["key"] for r in d["support"]["rooms"]] == ["k1", "k2"]
