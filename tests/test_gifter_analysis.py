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
    assert sep["month"] == "2026-09" and sep["tier"] == "頂点"  # 9 月に貢献ランキングに出た 200 人のうち、いちばん多い (少ない月でも、いちばん上の 1 人は頂点)
    assert sep["people"] == 200


def test_room_state_rules():
    per = {"2026-06": 100, "2026-07": 100, "2026-08": 100}
    assert ga._room_state({**per, "2026-09": 200}, "2026-09", True) == "増加"
    assert ga._room_state({**per, "2026-09": 30}, "2026-09", True) == "減少"
    assert ga._room_state(per, "2026-09", True) == "休眠"
    assert ga._room_state(per, "2026-09", False) == "ルームが休み"  # イベントに出ていなければ休眠にしない
    assert ga._room_state({"2026-01": 5, "2026-09": 5}, "2026-09", True) == "復帰"


def test_peak_tier_visibility_wall_and_pillar(tmp_path):
    """頂点 (上位 0.1%、少ない月は 10 人)・見えた回数・壁ぎわ率・ルームの柱。"""
    with Store(tmp_path) as s:
        s.set_meta("user_month_upto", True)
        # 9 月: 1000 人が出た大きなイベント (ルーム 10・11 は 100 人に届く)。2 は 100 位のすぐ上 (壁ぎわ) を 3 回
        for k, rid in enumerate((10, 11, 12)):
            users = {1000 + k * 300 + i: 1000 - i for i in range(200)}
            users[2] = 805  # 100 位 (= 901) より少ない? → 下の 200 人の中で、100 位の 1.2 倍まで
            _event(s, 10 + k, "2026-09", {rid: users})
        # 1 はルーム 50 (小さい) の柱: 3 回とも 1 位で、上位の合計の大半
        for k in range(3):
            _event(s, 20 + k, "2026-08", {50: {1: 9000, 7: 500, 8: 300}})
            s.set_contributions(20 + k, 50, [{"user_id": 1, "point": 9000, "rank": 1}, {"user_id": 7, "point": 500, "rank": 2},
                                             {"user_id": 8, "point": 300, "rank": 3}], NOW, final=True)
        st1 = ga.gifter_states(s, 1, NOW)
        st2 = ga.gifter_states(s, 2, NOW)
        cuts = ga.month_cuts(s, "2026-09")
    # 0.1% は 1 人未満なので、いちばん上の 1 人。2 はどのルームでも 100 位のすぐ上だが、3 ルームの合計ではいちばん多い
    assert cuts["people"] >= 600 and cuts["peak"] == 805 * 3
    assert ga.tier(805 * 3, cuts) == "頂点" and ga.tier(1000, cuts) == "大口" and st2["tiers"][-1]["tier"] == "頂点"
    v2 = st2["visibility"]
    assert v2["obs"] == 3 and v2["full"] == 3 and v2["wall"] == 3 and v2["wall_low"] and not v2["unclassified"]
    p1 = {p["room_id"]: p for p in st1["pillars"]}
    assert p1[50]["pillar"] and p1[50]["first"] == 3 and p1[50]["share"] > 0.9
    assert st1["visibility"]["full"] == 0 and st1["visibility"]["wall_rate"] is None


def test_focus_types_and_wandering(tmp_path):
    """集中のしかた: 一本推し・本命と掛け持ち・箱推し・分散と、送り先が 3 か月ごとに替わる渡り鳥。"""
    with Store(tmp_path) as s:
        s.set_meta("user_month_upto", True)
        eid = 0
        for m in ("2026-07", "2026-08", "2026-09"):
            eid += 1
            _event(s, eid, m, {10: {1: 950, 2: 600, 3: 100}, 11: {1: 50, 2: 200, 3: 100}, 12: {2: 200, 3: 100},
                               13: {3: 100}, 14: {3: 100}})
        # 4 は四半期ごとに送り先が替わる (20 → 21 → 22 → 23)
        for k, (m, rid) in enumerate((("2025-12", 20), ("2026-03", 21), ("2026-06", 22), ("2026-09", 23))):
            for j in range(2):
                eid += 1
                _event(s, eid, m, {rid: {4: 500}})
        s.conn.execute("INSERT INTO active_fans(room_id, ym, user_id, level) VALUES (99, '202609', 1, 12)")
        f = {u: ga.gifter_states(s, u, NOW)["focus"] for u in (1, 2, 3, 4)}
    assert f[1]["type"] == "一本推し" and f[1]["fan_only_rooms"] == 1 and f[1]["top_share"] == 0.95
    assert f[2]["type"] == "本命と掛け持ち"
    assert f[3]["type"] == "箱推し・分散" and round(f[3]["effective"]) == 5
    assert f[4]["bird"] is True and f[4]["moves"] >= 2 and f[1]["bird"] is None  # 1 は 1 四半期だけなので決めない
    assert [q["top"] for q in f[4]["quarters"] if q["top"]] == [20, 21, 22, 23]
