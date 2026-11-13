import json
import time

from showroom_keeper import backtest, gifter_analysis, gifter_status as gs
from showroom_keeper.research_store import Store

DAY = 86400
NOW = time.time()
MONTHS = backtest.default_months(NOW, 1)
AT = backtest.month_start(MONTHS[0])  # 4 か月前の月の初め


def _event(s, eid, end, rooms):
    """rooms: [(room_id, room_point, [(user_id, point), ...]), ...]。"""
    s.upsert_events([{"event_id": eid, "name": f"イベント{eid}", "start": end - 7 * DAY, "end": end}])
    s.set_event_rooms(eid, [{"room_id": r, "point": p, "rank": i + 1} for i, (r, p, _) in enumerate(rooms)], NOW)
    with s.tx() as c:
        c.execute("UPDATE events SET rooms_at=? WHERE event_id=?", (end + DAY, eid))
    for r, _, users in rooms:
        s.set_contributions(eid, r, [{"user_id": u, "name": f"u{u}", "point": p, "rank": k + 1} for k, (u, p) in enumerate(users)],
                            NOW, final=True)


def _fill(s, after=True):
    s.set_meta("user_month_upto", True)
    s.upsert_rooms([{"room_id": r, "key": f"k{r}", "name": f"ルーム{r}"} for r in range(1, 4)], "live", NOW)
    eid = 100
    for k in range(10, 0, -1):  # T より前: 2 週間ごと。7 はルーム1 の推し、8 はルーム2 (ルーム3 の少額の 30 人で、上から 1 割が 3 人になる)
        end = AT - k * 14 * DAY
        _event(s, eid, end, [(1, 100_000, [(7, 60_000 + k * 1000), (9, 300)]), (2, 95_000, [(8, 50_000), (9, 200)]),
                             (3, 20_000, [(9, 500)] + [(1000 + j, 10) for j in range(30)])])
        eid += 1
    if after:  # T より後: 7 は前と同じくらい、8 は止めた
        for k in range(1, 7):
            end = AT + k * 14 * DAY
            _event(s, eid, end, [(1, 120_000, [(7, 70_000), (9, 300)]), (2, 10_000, [(9, 200)]), (3, 20_000, [(9, 500)])])
            eid += 1


def _plain(st):
    return {k: v for k, v in st.items() if k != "at"}


def test_past_view_sees_only_what_was_known(tmp_path):
    full, pre = tmp_path / "full", tmp_path / "pre"
    with Store(full) as s:
        _fill(s)
    with Store(pre) as s:
        _fill(s, after=False)
    gifter_analysis._cuts.clear()
    with Store(full) as s:
        n_before = s.scalar("SELECT COUNT(*) FROM contributions")
        backtest.hide_after(s, AT)
        while gs.build_rounds(s, AT, limit=1000):
            pass
        hidden = {u: _plain(gs.compute(s, u, AT)) for u in (7, 8, 9)}
        assert s.scalar("SELECT COUNT(*) FROM contributions") < n_before
    gifter_analysis._cuts.clear()
    with Store(pre) as s:
        while gs.build_rounds(s, AT, limit=1000):
            pass
        alone = {u: _plain(gs.compute(s, u, AT)) for u in (7, 8, 9)}
    gifter_analysis._cuts.clear()
    assert hidden == alone  # 後の記録を隠した写しと、はじめから前の分しか無い DB で、同じ予測になる
    with Store(full) as s:  # 写しのファイルは書き換えていない (隠したのは接続の中だけ)
        assert s.scalar("SELECT COUNT(*) FROM contributions") == n_before
        assert s.scalar("SELECT COUNT(*) FROM gifter_status") == 0


def test_scores_compare_with_what_happened(tmp_path):
    with Store(tmp_path) as s:
        _fill(s)
    out = backtest.run(tmp_path, MONTHS)
    p = out["predictions"]
    assert p["status_fire"]["n"] >= 1 and p["status_fire"]["rate"] == 1.0  # 7 は前と同じくらい出した
    assert p["status_state"]["n"] >= 1 and "by" in p["status_state"]
    assert p["room_power"]["n"] >= 1  # 同じイベントのルームを 2 つずつくらべた
    assert p["event_strength"]["n"] >= 1
    assert p["event_projected"]["checked"] is False
    assert p["status_fire"]["naive"]["n"] >= 1 and p["status_order"]["naive"]["n"] >= 1 and p["status_fire"]["naive_label"]
    with Store(tmp_path) as s:
        assert s.scalar("SELECT COUNT(*) FROM gifter_status") == 0 and s.scalar("SELECT COUNT(*) FROM event_rounds") == 0


def test_page_reads_the_file_or_says_unchecked(tmp_path):
    got = backtest.read(tmp_path)
    assert got["found"] is False and all(not v["checked"] for v in got["predictions"].values())
    (tmp_path / backtest.FILE).write_text(json.dumps({"version": backtest.VERSION, "predictions": {"status_fire": {"n": 3}}}), encoding="utf-8")
    got = backtest.read(tmp_path)
    assert got["found"] and got["current"] and got["predictions"]["status_fire"]["n"] == 3


def test_claims_have_a_baseline():
    sc = backtest.Score()
    rows = [("上り調子", 10, 5, 5), ("上り調子", 1, 5, 5), ("安定", 5, 5, 5), ("安定", 20, 5, 5)]
    backtest._claims(sc, rows, backtest.STATE_CLAIMS)
    r = sc.result()["by"]
    assert r["上り調子"]["rate"] == 0.5 and r["上り調子"]["base"] == 0.75  # 全員に「上り調子」と言えば 4 人中 3 人当たる (言う意味が無い)
    assert r["安定"]["rate"] == 0.5
    assert backtest.pairs_agree({1: 3, 2: 2, 3: 1}, {1: 30, 2: 10, 3: 20}) == (2, 3)


def test_candidates_put_one_big_round_below_steady_givers():
    once = {"overall": 1_000_000, "fire": 1_000_000, "hp": 1_000_000}  # 1 回だけ大きく出した人 (半年に 1 か月、最後は 150 日前)
    steady = {"overall": 200_000, "fire": 200_000, "hp": 300_000}  # 毎月出している人
    a = backtest.candidates(once, 0, 0, 1 / 6, 150)
    b = backtest.candidates(steady, 250_000, 600_000, 1.0, 5)
    assert a["current"] > b["current"]  # 今の式では、1 回だけの人が上
    assert a["freq"] < b["freq"] and a["freq_recent"] < b["freq_recent"] and a["freq_recent_budget"] < b["freq_recent_budget"]
    assert backtest.candidates(once, 0, 0, None, None)["freq_recent"] == 0


def test_candidate_scores():
    assert backtest.auc_counts({1: 3, 2: 2, 3: 1, 4: 1}, {1: True, 2: False, 3: True, 4: False}) == (2.5, 4)
    assert backtest.top_overlap({1: 3, 2: 2, 3: 1}, {1: 0, 2: 5, 3: 9}, 2) == 1
    c = backtest.Cands()
    c.add([(1, {k: 3.0 for k in backtest.CANDIDATES}, 30.0, 5), (2, {k: 1.0 for k in backtest.CANDIDATES}, 0.0, 1),
           (3, {k: 2.0 for k in backtest.CANDIDATES}, 10.0, 1)], seed=1)
    r = c.result()["current"]
    assert r["order"] == 1.0 and r["appear"] == 1.0 and r["came_order"] == 1.0 and r["month_order"] is None  # 月の合計を渡していない
    c.add([(1, {k: 3.0 for k in backtest.CANDIDATES}, 30.0, 5, 50.0), (2, {k: 1.0 for k in backtest.CANDIDATES}, 0.0, 1, 0.0),
           (3, {k: 2.0 for k in backtest.CANDIDATES}, 10.0, 1, 60.0)], seed=1)
    r = c.result()["current"]
    assert r["month_order"] == 0.667 and r["month_came"] == 0.0  # 3 の方が 1 より月の合計は多い


def test_run_reports_candidates(tmp_path):
    with Store(tmp_path) as s:
        _fill(s)
    out = backtest.run(tmp_path, MONTHS)
    assert set(out["candidates"]) == set(backtest.CANDIDATES) | {"decisive_past", "_decisive"}
    assert out["candidates"]["freq"]["n_pairs"] >= 1 and "candidates_split" not in out
    # その月の合計の並び: T の月に 7 は 2 回 7 万ずつ、9 は少額、8 は出さない。先月の量の順 (7 > 8 > 9) は 7 と 9・7 と 8 で当たる
    assert out["candidates"]["naive_month"]["month_order"] is not None and out["candidates"]["naive_month"]["month_came"] is not None
    d = out["candidates"]["_decisive"]
    # 7 はルーム 1 (1 位、2 位との差 5,000) に毎回 6 万ほど: 決め手。8 はルーム 2 (2 位、3 位との差 75,000) に 5 万: 決め手でない
    assert d["people"] >= 2 and d["past_rounds"] >= 10 and d["past_block"] == 0
    split = backtest.run(tmp_path, MONTHS, dev=MONTHS)["candidates_split"]
    assert split["dev_months"] == MONTHS and split["check_months"] == [] and split["dev"]["decay_h2"]["n_pairs"] >= 1


def test_decay_puts_recent_months_first_and_skips_unknown_months():
    assert backtest.decay_sum({0: 100, 2: 100}, 2) == 150  # 2 か月前は重み半分
    assert backtest.decay_sum({0: 0, 12: 1000}, 1) == 0  # 12 か月より前は入れない
    once = {"overall": 1_000_000, "fire": 1_000_000, "hp": 1_000_000}
    steady = {"overall": 200_000, "fire": 200_000, "hp": 300_000}
    a = backtest.candidates(once, 0, 0, 1 / 6, 150, {5: 1_000_000}, 1_000_000)  # 5 か月前に 1 回だけ 100 万
    b = backtest.candidates(steady, 250_000, 600_000, 1.0, 5, {k: 250_000 for k in range(12)}, 60_000)
    assert a["decay_h1"] < b["decay_h1"] and a["decay_h2"] < b["decay_h2"]
    assert a["appear_med3"] > b["appear_med3"]  # 出てくる割合 × 直近 3 回は、1 回の大きさにまだ引っぱられる (採点で見る)


def test_decisive_counts_rounds_where_the_gift_exceeded_the_gap_below(tmp_path):
    with Store(tmp_path) as s:
        _fill(s, after=False)
        while gs.build_rounds(s, NOW, limit=1000):
            pass
        assert backtest.decisive(s, 7, 0, NOW) == (10, 10, 0)
        assert backtest.decisive(s, 8, 0, NOW) == (0, 10, 0)
        assert backtest.decisive(s, 9, 0, NOW)[0] == 0  # 少額 (下のルームとの差に届かない)
