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
