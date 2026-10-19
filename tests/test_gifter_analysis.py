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


def test_life_ladder_veteran_and_meteor(tmp_path):
    """段階と寿命: ルームごとの段 (見えた月の数)・古参と、初めて見えた月に大きく出してすぐ消えた流星型。"""
    with Store(tmp_path) as s:
        s.set_meta("user_month_upto", True)
        eid = 0
        months = ["2025-06"] + [f"2026-0{i}" for i in range(1, 10)]
        for m in months:
            eid += 1
            rows = {1: 300}
            if m >= "2026-08":
                rows.update({20 + i: 100 for i in range(6)})  # 新しく来た人たち
            _event(s, eid, m, {10: rows})
        _event(s, 100, "2026-05", {30: {5: 9000, 6: 9000, 8: 9000}})
        _event(s, 101, "2026-06", {30: {6: 500}})
        _event(s, 102, "2026-08", {30: {7: 9000}})
        s.conn.execute("INSERT INTO active_fans(room_id, ym, user_id, level) VALUES (30, '202606', 8, 15)")
        life = {u: ga.gifter_states(s, u, NOW)["life"] for u in (1, 5, 6, 7, 8, 20)}
    r1 = life[1]["rooms"][0]
    assert r1["months_seen"] == 10 and r1["stage"] == "定着" and r1["veteran"] and r1["first"] == "2025-06"
    assert life[20]["rooms"][0]["stage"] == "2 回目" and not life[20]["rooms"][0]["veteran"]
    assert life[5]["meteor"]["type"] is True and life[5]["meteor"]["share"] == 1
    assert life[6]["meteor"]["type"] is False  # 次の月も出た
    assert life[7]["meteor"]["type"] is None  # まだ 3 か月たっていない
    assert life[8]["meteor"]["type"] is False  # アクティブファンには残っている (100 位より下で続けているかもしれない)
    assert ga.stage_of(1) == "初見" and ga.stage_of(3) == "定着しかけ" and ga.stage_of(7) == "定着"


def test_engage_event_dependency_contest_watch_and_cooling(tmp_path):
    """イベント依存度 (イベント型・普段型)・段階ごとの大会での勝負型・見守り型・冷めかけ。"""
    with Store(tmp_path) as s:
        s.set_meta("user_month_upto", True)
        _event(s, 1, "2026-08", {10: {1: 1900, 2: 400}})
        t1, t2 = _end("2026-07"), _end("2026-09")
        for uid, a, b in ((1, 1000, 3000), (2, 1000, 3000)):
            s.conn.execute("INSERT INTO cumulative_history(room_id, user_id, at, point, visits) VALUES (10, ?, ?, ?, 5)", (uid, t1, a))
            s.conn.execute("INSERT INTO cumulative_history(room_id, user_id, at, point, visits) VALUES (10, ?, ?, ?, 9)", (uid, t2, b))
        # 段階ごとの大会: 1 は 4 倍に伸ばした (ほかの人はほぼ同じ)
        _event(s, 201, "2026-06", {20: {1: 100, 3: 100, 4: 100, 5: 100}})
        _event(s, 202, "2026-07", {20: {1: 400, 3: 110, 4: 100, 5: 120}})
        for eid, no in ((201, 1), (202, 2)):
            s.conn.execute("INSERT INTO event_series(event_id, series_id, stage_no, how, at) VALUES (?, 201, ?, 'auto', 0)", (eid, no))
        # 6 はルーム 30 を見守っている (ファン Lv12 が 2 か月、ポイントは無い)
        for ym in ("202608", "202609"):
            s.conn.execute("INSERT INTO active_fans(room_id, ym, user_id, level) VALUES (30, ?, 6, 12)", (ym,))
        # 7 はルーム 40 に来続けているが、送る量が減った
        for m, pt in (("2026-04", 1000), ("2026-05", 1000), ("2026-06", 1000), ("2026-08", 500)):
            _event(s, 300 + int(m[-2:]), m, {40: {7: pt}})
        for ym in ("202604", "202605", "202606", "202607", "202608", "202609"):
            s.conn.execute("INSERT INTO active_fans(room_id, ym, user_id, level) VALUES (40, ?, 7, 20)", (ym,))
        e = {u: ga.gifter_states(s, u, NOW)["engage"] for u in (1, 2, 3, 6, 7)}
    assert e[1]["dependency"]["type"] == "イベント型" and e[2]["dependency"]["type"] == "普段型"
    assert e[1]["contest"] is True and e[3]["contest"] is False and e[1]["contests"][0]["growth"] == 4
    assert e[6]["rooms"][0]["watch"] and e[6]["dependency"]["ratio"] is None
    assert e[7]["rooms"][0]["cool"] and not e[7]["rooms"][0]["watch"]


def test_recorded_types_late_regular_star_and_hype():
    """記録したギフトからの型: 終盤勝負型・規則型・星だけの大貢献・盛り上がり型。"""
    day = 86400
    t0 = NOW - 30 * day
    ev = {"a": [(t0, t0 + 7 * day)]}
    # 終盤勝負型: イベント中の 12 件が、すべて最後の 24 時間
    late = [(t0 + 7 * day - 3600 * k, 100, "a") for k in range(12)]
    r = ga.recorded_types(1, late, {(1, "a"): 1200}, {}, {"a": [(at, 1) for at, _, _ in late]}, ev, {"a": []})
    assert r["late"]["type"] is True and r["late"]["share"] == 1 and round(r["late"]["ratio"], 6) == 7
    early = [(t0 + 3600 * k, 100, "a") for k in range(12)]
    assert ga.recorded_types(1, early, {(1, "a"): 1200}, {}, {"a": []}, ev, {"a": []})["late"]["type"] is False
    # 規則型: 毎日同じ時刻 (配信の始まりはばらばら)
    reg = [(t0 + d * day + 3 * 3600, 10, "a") for d in range(25)]
    starts = {"a": sorted(t0 + d * day + (d * 5 % 24) * 3600 for d in range(25))}
    r = ga.recorded_types(1, reg, {(1, "a"): 250}, {}, {"a": []}, {}, starts)
    assert r["regular"]["type"] is True and r["regular"]["r"] > 0.99 and r["regular"]["room_r"] < 0.3
    # 星だけの大貢献: 無料ばかりで、記録した 12 人の中で 1 位
    pts = {(1, "a"): 5000, **{(u, "a"): 100 * u for u in range(2, 13)}}
    others = {"a": [(t0 + u, u) for u in range(1, 13)]}
    r = ga.recorded_types(1, [(t0, 5000, "a")], pts, {(1, "a"): 4800}, others, {}, {"a": []})
    assert r["stars"][0]["star"] and r["stars"][0]["place"] == 1
    # 盛り上がり型: いつも他の人のギフトの 10 秒あと (他の人は 1 時間に 1 回)
    theirs = [t0 + h * 3600 for h in range(30)]
    mine = [(t + 10, 50, "a") for t in theirs[:25]]
    others = {"a": [(t, 2) for t in theirs] + [(at, 1) for at, _, _ in mine]}
    r = ga.recorded_types(1, mine, {(1, "a"): 1250, (2, "a"): 3000}, {}, others, {}, {"a": []})
    assert r["hype"]["type"] is True and r["hype"]["share"] == 1 and r["hype"]["ratio"] > 10
