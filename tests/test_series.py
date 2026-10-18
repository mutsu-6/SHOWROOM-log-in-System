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


def test_core_name_routes_and_brand():
    assert series.route_label("【aw本選権＃2】ハワイアンウェディング2026 準決勝") == "本選権#2"
    assert series.route_label("【SR枠】ハワイアンウェディング2026") == "SR枠"
    assert series.route_label("【aw枠】KIMONO girl 第3回 ラストチャンス") == "ラストチャンス"
    assert series.brand("【フレキャン2025 枠A】CanCam賞") == series.brand("【フレキャン2025】モデルプレス賞") == "フレキャン2025"
    assert series.brand("【ミスサークル枠】超十代 2025") is None  # 枠の印は、年か第何回が無ければ大会の名前に使わない
    assert series.brand("【FRESH CAMPUS CONTEST 2025】ファイナル審査") == series.brand("【フレキャン2025】CanCam賞")  # 言いかえ
    assert series.conflict("【男性枠】カラオケ歌合戦 vol.9", "【女性枠】カラオケ歌合戦 vol.10")
    assert series.stage_label("【スタートダッシュ】なにかのイベント 審査") is None  # どこにでも出る言葉は段階にしない
    assert series.route_label("【aw本選権】KIMONO girl 第3回 アピールタイム") == "本選権 (共通)"
    assert series.brand("【SR枠】ハワイアンウェディング2026") is None
    assert series.stage_label("【aw本選権#1】ハワイアンウェディング2026 予選") == "予選"
    assert series.same_name("【フレキャン2025 枠A】CanCam賞", "【フレキャン2025】モデルプレス賞")
    assert not series.same_name("【フレキャン2026】CanCam賞", "【フレキャン2025】モデルプレス賞")  # 年がちがう
    assert not series.same_name("【aw枠】KIMONO girl 第3回 本選1st", "【aw枠】KIMONO girl 第2回 本選ファイナル")


def test_routes_merge_into_one_tree(tmp_path):
    """ハワイアンウェディングのような形: 本選権#1・#2 ごとに 予選 → 準決勝 → 決勝、別の入口の【SR枠】、
    そのあと【aw枠】本選 1st → 2nd に合流する。半年ほど続く。"""
    t0 = NOW - 300 * DAY
    hw = "ハワイアンウェディング2026"
    with Store(tmp_path) as s:
        s.upsert_rooms([{"room_id": r, "key": f"k{r}", "name": f"ルーム{r}"} for r in range(1, 60)], "archive", NOW)
        for k, base, off in ((1, 0, 0), (2, 20, 60)):
            _stage(s, k * 10 + 1, f"【aw本選権＃{k}】{hw} 予選", t0 + off * DAY, 7, [(base + r, 1000 - r) for r in range(1, 13)])
            _stage(s, k * 10 + 2, f"【aw本選権＃{k}】{hw} 準決勝", t0 + (off + 10) * DAY, 7, [(base + r, 3000 - r) for r in range(1, 9)])
            _stage(s, k * 10 + 3, f"【aw本選権＃{k}】{hw} 決勝", t0 + (off + 20) * DAY, 7, [(base + r, 9000 - r) for r in range(1, 6)])
        _stage(s, 40, f"【SR枠】{hw}", t0 + DAY, 14, [(r, 500) for r in range(41, 47)])
        _stage(s, 50, f"【aw枠】{hw} 本選1st", t0 + 100 * DAY, 10,
               [(r, 20000) for r in (1, 2, 3, 4, 5, 21, 22, 23, 24, 25, 41, 42, 43)])
        _stage(s, 51, f"【aw枠】{hw} 本選2nd", t0 + 120 * DAY, 10, [(r, 40000) for r in (1, 2, 3, 21, 22, 23)])
        _stage(s, 52, f"【aw枠】{hw} ラストチャンス", t0 + 135 * DAY, 7, [(r, 30000) for r in (4, 5, 24)])
        while series.step(s, NOW):
            pass
        rows = {r["event_id"]: dict(r) for r in s.all("SELECT * FROM event_series")}
        sid = rows[11]["series_id"]
        assert sid == 11 and all(rows[e]["series_id"] == sid for e in (12, 13, 21, 22, 23, 40, 50, 51, 52))
        assert rows[13]["parents"] == "12" and rows[13]["route"] == "本選権#1" and rows[13]["stage_no"] == 3
        assert rows[40]["route"] == "SR枠" and rows[40]["stage_no"] == 1  # 別の入口 (同じころに始まった)
        assert rows[50]["parents"] == "13,23,40" and rows[50]["route"] == "aw枠" and rows[50]["stage_no"] == 4  # 合流
        assert rows[51]["parents"] == "50" and rows[51]["stage_no"] == 5  # 合流のあとは、前の決勝にはつながない
        assert rows[52]["route"] == "ラストチャンス"
        d = series.series_detail(s, sid)
    line = {x["event_id"]: x for x in d["lines"]}
    assert line[13]["advanced"] == 5 and line[11]["advanced"] == 8 and line[11]["line_rank"] == 8
    assert line[40]["advanced"] == 3 and line[40]["best_out"] == 500
    assert {r["route"] for r in d["routes"]} == {"本選権#1", "本選権#2", "SR枠", "aw枠", "ラストチャンス"}
    assert any(g["from"] == 23 and g["to"] == 50 for g in d["growth"])


def test_brand_marks_link_stages_with_different_names(tmp_path):
    """フレキャンのような形: 段階ごとの名前がまったくちがい、共通は頭の【】の印と年だけ。同じ時期に枠 A・B に分かれる。"""
    t0 = NOW - 200 * DAY
    with Store(tmp_path) as s:
        _stage(s, 1, "【フレキャン2025 枠A】モデルプレス賞 SHOWROOM審査", t0, 7, [(r, 100) for r in range(1, 11)])
        _stage(s, 2, "【フレキャン2025 枠B】超十代賞 SHOWROOM審査", t0, 7, [(r, 100) for r in range(11, 21)])
        _stage(s, 3, "【フレキャン2025】CanCam賞", t0 + 30 * DAY, 7, [(r, 300) for r in (1, 2, 3, 11, 12, 13)])
        _stage(s, 4, "【フレキャン2025】ファイナル審査", t0 + 150 * DAY, 3, [(r, 900) for r in (1, 11, 12)])
        _stage(s, 5, "【フレキャン2024】ファイナル審査", t0 - 200 * DAY, 3, [(r, 900) for r in (1, 11, 12)])
        while series.step(s, NOW):
            pass
        rows = {r["event_id"]: dict(r) for r in s.all("SELECT * FROM event_series")}
    assert rows[1]["series_id"] == rows[2]["series_id"] == rows[3]["series_id"] == rows[4]["series_id"] is not None
    assert rows[2]["route"] == "枠B" and rows[2]["stage_no"] == 1
    assert rows[3]["parents"] == "1,2" and rows[4]["parents"] == "3" and rows[4]["stage_no"] == 3
    assert rows[5]["series_id"] is None  # 前の年の大会とはつなげない


def _run_all(s, now=NOW):
    while series.step(s, now):
        pass
    return {r["event_id"]: dict(r) for r in s.all("SELECT * FROM event_series")}


def test_repeated_events_are_not_series(tmp_path):
    """くり返し開かれるふつうのイベント (vol. の付くもの・段階の言葉の無いもの) は、ルームが重なってもまとめない。1 年は超えない。"""
    t0 = NOW - 800 * DAY
    with Store(tmp_path) as s:
        for i in range(20):
            _stage(s, 100 + i, f"【エンジョイカラオケ】vol.{i + 1}", t0 + i * 30 * DAY, 7, [(r, 100) for r in range(1, 9)])
            _stage(s, 200 + i, "【まったり3日間】イベント", t0 + i * 30 * DAY + 10 * DAY, 3, [(r, 100) for r in range(1, 9)])
            _stage(s, 300 + i, f"【男性枠】カラオケ歌合戦 vol.{i + 1} 予選", t0 + i * 30 * DAY + 15 * DAY, 3, [(r, 100) for r in range(11, 19)])
            _stage(s, 400 + i, f"【女性枠】カラオケ歌合戦 vol.{i + 1} 決勝", t0 + i * 30 * DAY + 20 * DAY, 3, [(r, 100) for r in range(11, 15)])
        rows = _run_all(s)
        spans = s.all("""SELECT es.series_id, MAX(e.end) - MIN(e.start) AS span, COUNT(*) AS n FROM event_series es JOIN events e USING(event_id)
                         WHERE es.series_id IS NOT NULL GROUP BY es.series_id""")
    assert all(rows[100 + i]["series_id"] is None and rows[200 + i]["series_id"] is None for i in range(20))
    assert all(r["span"] <= series.SERIES_MAX and r["n"] == 2 for r in spans)  # vol. ごとの 予選 → 決勝 だけ


def test_different_editions_are_not_mixed(tmp_path):
    """第 3 回と第 4 回は、あいだに番号の無い段階があっても混ぜない。"""
    t0 = NOW - 400 * DAY
    with Store(tmp_path) as s:
        _stage(s, 1, "Popteen専属モデルオーディション 第3回 予選", t0, 7, [(r, 100) for r in range(1, 11)])
        _stage(s, 2, "Popteen専属モデルオーディション 二次審査", t0 + 10 * DAY, 7, [(r, 300) for r in range(1, 7)])
        _stage(s, 3, "Popteen専属モデルオーディション 第4回 予選", t0 + 150 * DAY, 7, [(r, 100) for r in range(1, 9)])
        _stage(s, 4, "Popteen専属モデルオーディション 第4回 決勝", t0 + 160 * DAY, 7, [(r, 100) for r in range(1, 5)])
        rows = _run_all(s)
    assert rows[2]["series_id"] == rows[1]["series_id"] == 1
    assert rows[3]["series_id"] != 1 and rows[4]["series_id"] == rows[3]["series_id"] == 3


def test_final_with_other_name_and_late_entrances(tmp_path):
    """名前のちがうファイナル審査 (ルームが全部、前の段階にいた) と、1 週間ずれた別の入口の【SR枠】を入れる。
    番号の無い本選権のアピールタイムは、決勝の前の段階にしない。"""
    t0 = NOW - 200 * DAY
    kg = "KIMONO girl 第3回"
    with Store(tmp_path) as s:
        _stage(s, 1, "【ミスサー二次審査】超十代賞", t0, 7, [(r, 100) for r in range(1, 21)])
        _stage(s, 2, "【ミスサー二次審査】CanCam賞", t0 + 10 * DAY, 7, [(r, 300) for r in range(1, 13)])
        _stage(s, 3, "【MISS CIRCLE CONTEST 2025】ファイナル審査", t0 + 30 * DAY, 3, [(r, 900) for r in range(1, 13)])
        _stage(s, 11, f"【aw本選権＃1】{kg} 予選", t0, 7, [(r, 100) for r in range(31, 41)])
        _stage(s, 12, f"【aw本選権＃1】{kg} 準決勝", t0 + 10 * DAY, 7, [(r, 100) for r in range(31, 37)])
        _stage(s, 13, f"【aw本選権】{kg} アピールタイム", t0 + 18 * DAY, 3, [(r, 100) for r in range(31, 37)])
        _stage(s, 14, f"【aw本選権＃1】{kg} 決勝", t0 + 22 * DAY, 7, [(r, 100) for r in range(31, 35)])
        _stage(s, 15, f"【aw枠】{kg} 本選ファイナル", t0 + 40 * DAY, 7, [(r, 100) for r in range(31, 34)])
        _stage(s, 16, f"【SR枠】{kg}", t0 + 47 * DAY, 7, [(r, 100) for r in range(51, 56)])
        _stage(s, 17, f"【既存aw枠】{kg}", t0 + 47 * DAY, 7, [(r, 100) for r in range(61, 66)])
        _stage(s, 18, f"【SR枠】KIMONO girl 第2回", t0 + 47 * DAY, 7, [(r, 100) for r in range(71, 76)])
        rows = _run_all(s)
    assert rows[2]["series_id"] == rows[1]["series_id"] == 1 and rows[3]["series_id"] == 1 and rows[3]["parents"] == "2"
    assert all(rows[e]["series_id"] == 11 for e in (12, 13, 14, 15, 16, 17))
    assert rows[13]["route"] == "本選権 (共通)" and rows[14]["parents"] == "12"
    assert rows[16]["route"] == "SR枠" and rows[17]["route"] == "既存aw枠"
    assert rows[18]["series_id"] is None  # 第 2 回は別の大会


def test_version_change_rebuilds_auto_but_keeps_manual(db):
    _run(db)
    explore.series_link(db, "301", "101")
    with Store(db) as s:
        s.set_meta("series_version", 1)
        series.step(s, NOW)
        assert s.one("SELECT how FROM event_series WHERE event_id=301")["how"] == "manual"
        _run_all(s)
        assert series.series_of(s, 103)["series_id"] == 101


def test_years_of_other_auditions_inside_names_do_not_split():
    """名前の中の「超十代 2026」「お台場冒険王 2024」は、段階になっている別のオーディションの年で、大会の年ではない。"""
    assert not series.conflict("【ミスサー枠】超十代 2026", "【MISS CIRCLE CONTEST 2025】ファイナル審査")
    assert not series.conflict("【ミスサー二次審査】お台場冒険王 2024", "【ミスサー二次審査】TGCしずおか 2025")
    assert series.conflict("【フレキャン2025】CanCam賞", "【フレキャン2026】CanCam賞")  # 【】の中の年はくらべる
    assert series.conflict("ミスターオブミスター2025-予選-", "ミスターオブミスター2026-決勝-")  # 印が無ければ名前の中の年
    assert series.conflict("【aw本選権＃1】ハワイアンウェディング2026 予選", "【aw枠】ハワイアンウェディング2025 本選1st")


def test_other_entry_number_is_not_the_previous_stage(tmp_path):
    """本選権#1 で負けたルームが #2 に出直しても、#2 の予選の前の段階を #1 の段階にしない。"""
    t0 = NOW - 200 * DAY
    hw = "ハワイアンウェディング2026"
    with Store(tmp_path) as s:
        _stage(s, 1, f"【aw本選権＃1】{hw} 予選", t0, 7, [(r, 100) for r in range(1, 13)])
        _stage(s, 2, f"【aw本選権＃1】{hw} 準決勝", t0 + 10 * DAY, 7, [(r, 100) for r in range(1, 9)])
        _stage(s, 3, f"【aw本選権＃2】{hw} 予選", t0 + 20 * DAY, 7, [(r, 100) for r in range(3, 13)])  # ほとんどが #1 に出ていた
        _stage(s, 4, f"【aw本選権＃2】{hw} 準決勝", t0 + 30 * DAY, 7, [(r, 100) for r in range(3, 9)])
        rows = _run_all(s)
    assert rows[3]["series_id"] == 1 and rows[3]["parents"] is None and rows[3]["stage_no"] == 1 and rows[3]["route"] == "本選権#2"
    assert rows[4]["parents"] == "3" and rows[4]["stage_no"] == 2


def test_numberless_stage_does_not_join_two_editions(tmp_path):
    """番号の無い段階が、第 3 回と第 4 回の両方とルームが重なっても、2 つの大会を 1 つにしない。"""
    t0 = NOW - 300 * DAY
    with Store(tmp_path) as s:
        _stage(s, 1, "Popteen専属モデルオーディション 第3回 予選", t0, 7, [(r, 100) for r in range(1, 11)])
        _stage(s, 2, "Popteen専属モデルオーディション 第3回 決勝", t0 + 10 * DAY, 7, [(r, 100) for r in range(1, 6)])
        _stage(s, 3, "Popteen専属モデルオーディション 第4回 予選", t0 + 30 * DAY, 7, [(r, 100) for r in range(4, 14)])
        _stage(s, 4, "Popteen専属モデルオーディション 第4回 決勝", t0 + 40 * DAY, 7, [(r, 100) for r in range(4, 9)])
        _stage(s, 5, "Popteen専属モデルオーディション 敗者復活 決勝", t0 + 50 * DAY, 3, [(r, 100) for r in (1, 2, 4, 5, 6, 7)])
        rows = _run_all(s)
    assert rows[2]["series_id"] == 1 and rows[4]["series_id"] == 3 and rows[3]["series_id"] == 3
