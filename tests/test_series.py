import time

import pytest

from showroom_keeper import explore, series
from showroom_keeper.research_store import Store

NOW = time.time()
DAY = 86400


def _stage(s, eid, name, start, days, rooms, now=NOW):
    s.upsert_events([{"event_id": eid, "name": name, "start": start, "end": start + days * DAY}])
    s.set_event_rooms(eid, [{"room_id": r, "point": p, "rank": i + 1} for i, (r, p) in enumerate(rooms)], now)


@pytest.fixture
def db(tmp_path):
    """ミス・グランド・ジャパンのような 4 段階の大会と、ミスターオブミスターのような名前の変わる決勝と、関係ないイベント。"""
    t0 = NOW - 200 * DAY
    with Store(tmp_path) as s:
        s.upsert_rooms([{"room_id": r, "key": f"k{r}", "name": f"ルーム{r}"} for r in range(1, 40)], "archive", NOW)
        _stage(s, 101, "【スタートダッシュ】ミス・グランド・ジャパン2025 SHOWROOM配信審査", t0, 7,
               [(r, 10000 - r * 100) for r in range(1, 11)])
        _stage(s, 102, "【1stイベント】ミス・グランド・ジャパン2025 SHOWROOM配信審査", t0 + 10 * DAY, 7,
               [(r, 30000 - r * 1000) for r in range(1, 9)])
        _stage(s, 103, "【2ndイベント】ミス・グランド・ジャパン2025 SHOWROOM配信審査", t0 + 20 * DAY, 7,
               [(r, 60000 - r * 2000) for r in range(1, 6)])
        _stage(s, 201, "ミスターオブミスター2025-予選- SHOWROOM審査", t0, 10, [(r, 5000 + r) for r in range(20, 30)])
        _stage(s, 202, "ミスターオブミスター2025-準決勝- SHOWROOM審査", t0 + 15 * DAY, 10, [(r, 8000 + r) for r in range(20, 26)])
        _stage(s, 203, "【ミスターオブミスター】-決勝後半- SHOWROOM最終審査", t0 + 30 * DAY, 5, [(r, 20000 + r) for r in range(20, 24)])
        # 毎月ある同じ名前のイベント (段階の言葉が無い)。ルームが重なっても大会にしない
        _stage(s, 301, "まいにち配信チャレンジ 10月", t0, 20, [(r, 100) for r in range(1, 6)])
        _stage(s, 302, "まいにち配信チャレンジ 11月", t0 + 25 * DAY, 20, [(r, 100) for r in range(1, 6)])
        # ギフター: 7 は予選は少しだけ (無料の星くらい)、決勝で多く送る。8 は大口で最後まで残る
        s.set_contributions(101, 1, [{"user_id": 7, "name": "a", "point": 700, "rank": 2},
                                     {"user_id": 8, "name": "b", "point": 9000, "rank": 1}], NOW, final=True)
        s.set_contributions(103, 1, [{"user_id": 7, "name": "a", "point": 40000, "rank": 1},
                                     {"user_id": 8, "name": "b", "point": 20000, "rank": 2}], NOW, final=True)
    return tmp_path


def _run(d):
    with Store(d) as s:
        while series.step(s, NOW):
            pass


def test_core_name_and_stage_label():
    a = series.core_name("【スタートダッシュ】ミス・グランド・ジャパン2025 SHOWROOM配信審査")
    assert a == series.core_name("【Lastイベント】ミス・グランド・ジャパン2025 SHOWROOM配信審査")
    assert series.similar(series.core_name("ミスターオブミスター2025-予選- SHOWROOM審査"),
                          series.core_name("【ミスターオブミスター】-決勝後半- SHOWROOM最終審査")) >= series.SERIES_NAME
    assert series.stage_label("ミスターオブミスター2025-準々決勝- SHOWROOM審査") == "準々決勝"
    assert series.stage_label("【1stイベント】ミス・グランド") == "1st"
    assert series.stage_label("まいにち配信チャレンジ 10月") is None
    assert series.entrance("【SR枠】なにかの大会 予選") == "SR枠"


def test_stages_are_linked_into_series(db):
    _run(db)
    with Store(db) as s:
        rows = {r["event_id"]: (r["series_id"], r["stage_no"]) for r in s.all("SELECT * FROM event_series")}
    assert rows[101] == (101, 1) and rows[102] == (101, 2) and rows[103] == (101, 3)
    assert rows[201] == (201, 1) and rows[202] == (201, 2) and rows[203] == (201, 3)
    assert rows[301][0] is None and rows[302][0] is None  # 段階の言葉の無い、毎月のイベントはまとめない


def test_later_rooms_recheck_next_stage(tmp_path):
    """次の段階を先に調べて、前の段階の参加ルームがあとから分かった時も、つなぎ直す。"""
    t0 = NOW - 100 * DAY
    with Store(tmp_path) as s:
        s.upsert_events([{"event_id": 1, "name": "なにか大会 予選", "start": t0, "end": t0 + 7 * DAY}])
        _stage(s, 2, "なにか大会 決勝", t0 + 10 * DAY, 5, [(r, 100) for r in range(1, 5)])
        while series.step(s, NOW):
            pass
        assert series.series_of(s, 2) is None
        s.set_event_rooms(1, [{"room_id": r, "point": 50, "rank": r} for r in range(1, 9)], NOW + 10)
        while series.step(s, NOW + 20):
            pass
        assert series.series_of(s, 2)["series_id"] == 1 and series.series_of(s, 2)["stage_no"] == 2


def test_parallel_entrances_are_one_stage(tmp_path):
    t0 = NOW - 100 * DAY
    with Store(tmp_path) as s:
        _stage(s, 1, "【SR枠】なにか大会 予選", t0, 7, [(r, 100 * r) for r in range(1, 7)])
        _stage(s, 2, "【TP枠】なにか大会 予選", t0, 7, [(r, 100 * r) for r in range(11, 17)])
        _stage(s, 3, "なにか大会 決勝", t0 + 10 * DAY, 5, [(r, 1000) for r in (5, 6, 15, 16)])
        while series.step(s, NOW):
            pass
        st = {r["event_id"]: (r["series_id"], r["stage_no"]) for r in s.all("SELECT * FROM event_series")}
        assert st[3][1] == 2 and st[3][0] in (1, 2)
        d = series.series_detail(s, st[3][0])
    # 決勝は、片方の枠だけでは半分に届くので、その枠の大会になる。もう片方は同じ名前・同じ時期なので同じ段階
    assert {(x["event_id"], x["stage_no"]) for x in d["events"]} >= {(3, 2)}


def test_series_detail(db):
    _run(db)
    d = explore.series_detail(db, "101")
    assert d["found"] and d["stages"] == 3 and d["rooms"] == 10
    line1 = next(x for x in d["lines"] if x["event_id"] == 101)
    assert line1["advanced"] == 8 and line1["line_rank"] == 8 and line1["line_point"] == 10000 - 800
    assert line1["best_out"] == 10000 - 900
    last = next(x for x in d["lines"] if x["event_id"] == 103)
    assert last["advanced"] is None
    assert d["growth"][0]["rooms"] == 8 and d["growth"][0]["ratio"] > 1
    assert [x["top"] for x in d["stay"]] == [2, 0, 2]
    assert d["started_free"] == 1 and d["free_to_paid"] == 1  # 7 は予選 100 pt/日、決勝で 1 日 5000 pt 以上
    assert [g["user_id"] for g in d["top_gifters"]] == [7, 8]
    assert d["top_rooms"][0]["reached"] == 3
    assert explore.series_list(db)["series"][0]["series_id"] in (101, 201)
    ev = explore.event_detail(db, "102")
    assert ev["taikai"]["series"]["series_id"] == 101 and [e["event_id"] for e in ev["taikai"]["events"]] == [101, 102, 103]


def test_manual_link_and_detach(db):
    _run(db)
    out = explore.series_link(db, "203")  # 外す
    assert out["series_id"] is None and out["taikai"]["manual"]
    _run(db)
    with Store(db) as s:
        s.set_event_rooms(203, [{"room_id": 20, "point": 1, "rank": 1}, {"room_id": 21, "point": 1, "rank": 2},
                                {"room_id": 22, "point": 1, "rank": 3}], NOW + 100)
        while series.step(s, NOW + 200):
            pass
        assert series.series_of(s, 203) is None  # 手で外したものは、読み直しても自動でつながない
    out = explore.series_link(db, "301", "101")  # 手でつなぐ
    assert out["series_id"] == 101
    with Store(db) as s:
        assert series.series_of(s, 301)["stage_no"] == 1  # 始まりの順に段階を付け直す (101 と同じ時期)
        assert series.series_of(s, 103)["stage_no"] == 3
    with pytest.raises(ValueError):
        explore.series_link(db, "999", "101")
