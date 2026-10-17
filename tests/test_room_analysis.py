import json
import time

from showroom_keeper import comment_counts, explore
from showroom_keeper import room_analysis as ra
from showroom_keeper.research_store import Store, contribution_stats, median

NOW = time.time()
DAY = 86400


def _event(s, eid, end, rooms, start=None):
    """rooms: {room_id: (point, rank, [貢献者のポイント])}"""
    s.upsert_events([{"event_id": eid, "name": f"イベント{eid}", "start": start or end - 7 * DAY, "end": end}])
    s.set_event_rooms(eid, [{"room_id": r, "point": p, "rank": k} for r, (p, k, _) in rooms.items()], NOW)
    for r, (_, _, contrib) in rooms.items():
        if contrib:
            s.set_contributions(eid, r, [{"user_id": r * 1000 + i, "point": c, "rank": i + 1} for i, c in enumerate(contrib)],
                                NOW, final=True)


def test_contribution_stats_and_event_median(tmp_path):
    assert contribution_stats([5, 1, 10, 3]) == (4, 19, 10, 18, 19)
    assert median([3, 1, 2]) == 2 and median([1, 2, 3, 4]) == 2.5 and median([]) is None
    with Store(tmp_path) as s:
        _event(s, 1, NOW - DAY, {1: (100, 1, [60, 30, 10]), 2: (50, 2, []), 3: (0, 3, [])})
        re = s.one("SELECT * FROM room_events WHERE room_id=1 AND event_id=1")
        assert (re["c_count"], re["c_sum"], re["c_top1"], re["c_top3"], re["c_top10"]) == (3, 100, 60, 100, 100)
        e = s.one("SELECT room_count, median_point FROM events WHERE event_id=1")
        assert (e["room_count"], e["median_point"]) == (3, 75)  # 0 ポイントのルームは真ん中の値に入れない
        # 前の版で集めた分 (数字が無い) は、build_room_stats が少しずつ作る
        s.conn.execute("UPDATE room_events SET c_count=NULL, c_sum=NULL, c_top1=NULL")
        s.conn.execute("UPDATE events SET room_count=NULL, median_point=NULL")
        assert s.build_room_stats(step=1) is True  # 1 件目だけ (まだ残りがあるかもしれない)
        assert s.build_room_stats(step=1) is False and s.meta("room_stats_upto") is True
        assert s.one("SELECT c_top1 FROM room_events WHERE room_id=1 AND event_id=1")[0] == 60
        assert s.one("SELECT median_point FROM events WHERE event_id=1")[0] == 75


def test_classify_types():
    cuts = {"1": {"n": 30, "top1_p80": 0.5, "top3_p50": 0.6, "rel_top10_p75": 3.0, "contributors_p75": 80,
                  "contributors_p25": 20, "rel_per_contrib_p50": 0.05, "rel_point_p50": 1.0}}
    base = {"size_class": 1, "top1": 0.2, "top3": 0.5, "contributors": 50, "capped": False, "rel_top10": 1.0,
            "rel_per_contrib": 0.1, "rel_point": 1.0}
    assert ra.classify({**base, "top1": 0.6}, cuts) == "上位集中型"
    assert ra.classify({**base, "top1": 0.45}, cuts) == "バランス型"  # 0.4 以上でも、区切り (上から 2 割) に届かない
    assert ra.classify({**base, "rel_top10": 4.0}, cuts) == "高額の常連型"
    assert ra.classify({**base, "capped": True, "rel_per_contrib": 0.01}, cuts) == "少額多人数型"
    assert ra.classify({**base, "contributors": 10, "rel_point": 2.0}, cuts) == "少数精鋭型"
    assert ra.classify(base, cuts) == "バランス型"
    assert ra.classify(None, cuts) is None
    assert ra.classify(base, {"1": {**cuts["1"], "n": 3}}) is None  # くらべるルームが少ない
    assert ra.classify(base, None) is None


def test_classify_run():
    assert ra.classify_run(0.1, 0.1, 0.1, 0.1, 0.8) == "逃げ"
    assert ra.classify_run(0.2, 0.2, 0.1, 0.1, 0.0) == "先行"
    assert ra.classify_run(0.9, 0.8, 0.3, 0.6, 0.0) == "追い込み"
    assert ra.classify_run(0.6, 0.3, 0.2, 0.1, 0.0) == "差し"
    assert ra.classify_run(0.6, 0.6, 0.6, 0.1, 0.0) is None


def test_event_styles_and_room_style(tmp_path):
    with Store(tmp_path) as s:
        start, end = NOW - 10 * DAY, NOW - 3 * DAY
        s.upsert_events([{"event_id": 5, "start": start, "end": end}])
        # ルーム 1 は最初から 1 位 (逃げ)、ルーム 4 は最終日に大きく伸ばす (追い込み)
        for i, f in enumerate((0.05, 0.25, 0.5, 0.75, 0.9, 1.0)):
            at = start + f * (end - start)
            s.add_progress(5, [{"room_id": 1, "point": 1000 * (i + 1)}, {"room_id": 2, "point": 500 * (i + 1)},
                               {"room_id": 3, "point": 400 * (i + 1)}, {"room_id": 4, "point": 10 if i < 4 else 100 if i == 4 else 4000}], at)
        rows = {r[1]: r for r in ra.event_styles(s, 5)}
        assert rows[1][2] == "逃げ" and rows[4][2] == "追い込み"
        assert rows[4][7] > 0.9  # 最終日のポイントの割合
        s.conn.execute("UPDATE events SET rooms_at=? WHERE event_id=5", (end + 60,))
        assert ra.step(s, NOW) is True and s.meta(ra.STYLE_VERSION_META) == ra.STYLE_VERSION  # 決め方の版を書く
        assert ra.step(s, NOW) is True  # 走り方を作る
        assert s.scalar("SELECT COUNT(*) FROM room_event_style WHERE event_id=5") == 4
        assert s.one("SELECT style_at FROM events WHERE event_id=5")[0] == NOW
        # 決め方の版が変わったら、作った分を作り直す
        s.set_meta(ra.STYLE_VERSION_META, 1)
        assert ra.step(s, NOW + 1) is True and s.one("SELECT style_at FROM events WHERE event_id=5")[0] is None
        assert ra.step(s, NOW + 1) is True and s.one("SELECT style_at FROM events WHERE event_id=5")[0] == NOW + 1
        assert ra.room_style(s, 1) == {"style": None, "events": 1, "counts": {"逃げ": 1}}  # 3 件より少ない
        # 推移が足りないイベントは走り方を出さない
        s.upsert_events([{"event_id": 6, "start": start, "end": end}])
        s.add_progress(6, [{"room_id": 1, "point": 10}], end)
        assert ra.event_styles(s, 6) is None


def test_room_style_majority(tmp_path):
    with Store(tmp_path) as s:
        for eid, style in enumerate(("差し", "差し", "先行", "差し"), 1):
            s.upsert_events([{"event_id": eid, "end": NOW - eid * DAY}])
            s.conn.execute("INSERT INTO room_event_style(event_id, room_id, style) VALUES (?, 1, ?)", (eid, style))
        assert ra.room_style(s, 1)["style"] == "差し"
        for eid in (8, 9):
            s.conn.execute("INSERT INTO room_event_style(event_id, room_id, style) VALUES (?, 1, '先行')", (eid,))
            s.upsert_events([{"event_id": eid, "end": NOW}])
        assert ra.room_style(s, 1)["style"] == "自在"  # 半分を超える型がない


def test_room_style_counts_events_without_a_style(tmp_path):
    with Store(tmp_path) as s:
        # 逃げ 3 件・型なし 7 件: 型なしも数に入れるので、半分を超える型はなく「自在」
        for eid in range(1, 11):
            s.upsert_events([{"event_id": eid, "end": NOW - eid * DAY}])
            s.conn.execute("INSERT INTO room_event_style(event_id, room_id, style) VALUES (?, 1, ?)",
                           (eid, "逃げ" if eid <= 3 else None))
        st = ra.room_style(s, 1)
        assert st["style"] == "自在" and st["events"] == 10 and st["counts"] == {"逃げ": 3, "型なし": 7}


def test_event_styles_ties_share_the_same_place(tmp_path):
    with Store(tmp_path) as s:
        start, end = NOW - 10 * DAY, NOW - 3 * DAY
        s.upsert_events([{"event_id": 7, "start": start, "end": end}])
        # 序盤 (4 分の 1 まで) はルーム 8 だけがポイントを持ち、ほかの 7 ルームは 0 で並ぶ
        for i, f in enumerate((0.0, 0.1, 0.25, 0.5, 0.75, 0.9, 1.0)):
            s.add_progress(7, [{"room_id": r, "point": (100 if r == 8 else 0) if f <= 0.25 else r * 100 * (i + 1)}
                               for r in range(1, 9)], start + f * (end - start))
        rows = {r[1]: r for r in ra.event_styles(s, 7)}
        # 0 ポイントで並んだルームは、ルーム番号にかかわらず同じ位置 (いちばん下)
        assert {rows[r][3] for r in range(1, 8)} == {1.0} and rows[8][3] == 1 / 8
        assert rows[7][2] == "差し" and rows[1][2] is None
        # 誰もポイントの無い回は、だれの 1 位にも数えない
        s.add_progress(7, [{"room_id": r, "point": 0} for r in range(1, 9)], start - 3600)
        assert all(r[9] < 0.5 for r in ra.event_styles(s, 7) if r[1] != 8)


def test_momentum(tmp_path):
    with Store(tmp_path) as s:
        # 前の期間は 10 ルーム中 8 位、直近は 2 位
        for eid, days, rank in ((1, 100, 8), (2, 90, 8), (3, 20, 2), (4, 10, 2)):
            _event(s, eid, NOW - days * DAY, {1: (100, rank, []), **{r: (10, r, []) for r in range(2, 11)}})
        m = ra.momentum(s, 1, NOW)
        # 位置は (順位 - 1) ÷ (参加ルーム数 - 1): 8 位は 7/9、2 位は 1/9
        assert m["label"] == "上り調子" and abs(m["value"] - 6 / 9) < 1e-9
        assert ra.momentum(s, 2, NOW)["label"] == "変わらない"
        assert ra.momentum(s, 1, NOW - 50 * DAY)["label"] is None  # 直近のイベントが足りない
        # 1 位は、出たイベントの規模にかかわらず位置 0 (小さいイベントに移っただけで下り気味にしない)
        for eid, days, n in ((5, 100, 500), (6, 90, 500), (7, 20, 5), (8, 10, 5)):
            _event(s, eid, NOW - days * DAY, {50: (1000, 1, []), **{r: (10, r - 99, []) for r in range(101, 100 + n)}})
        m = ra.momentum(s, 50, NOW)
        assert m["recent_place"] == 0 and m["prev_place"] == 0 and m["label"] == "変わらない"


def test_profile_rebuild_and_list(tmp_path):
    with Store(tmp_path) as s:
        # 25 ルーム × 3 イベント。ルーム 1 は 1 人に頼っている、ほかは 10 人で均等
        for eid in (1, 2, 3):
            rooms = {r: (1000, r, [900] + [10] * 9 if r == 1 else [100] * 10) for r in range(1, 26)}
            _event(s, eid, NOW - eid * 10 * DAY, rooms)
        s.set_meta("room_stats_upto", True)
        while ra.step(s, NOW):
            pass
        assert s.meta("room_profile_at") == NOW
        assert s.one("SELECT type FROM room_profile WHERE room_id=1")[0] == "上位集中型"
        assert s.one("SELECT type FROM room_profile WHERE room_id=5")[0] == "バランス型"
        assert ra.step(s, NOW + 60) is False  # 1 日 1 回
    lst = explore.room_profiles(tmp_path, rtype="上位集中型")
    assert [r["room_id"] for r in lst["rooms"]] == [1] and lst["type_counts"]["上位集中型"] == 1
    d = explore.room_detail(tmp_path, "1")["analysis"]
    assert d["type"] == "上位集中型" and d["metrics"]["events"] == 3 and abs(d["metrics"]["top1"] - 0.9) < 1e-9
    assert d["new_share"]["value"] == 0  # 毎回同じ人
    assert "name" not in json.dumps(d["metrics"])  # トップの人の名前は出さない


def test_room_detail_without_data(tmp_path):
    with Store(tmp_path) as s:
        s.upsert_rooms([{"room_id": 1, "key": "aaa"}], "live", NOW)
    d = explore.room_detail(tmp_path, "aaa")
    assert d["analysis"]["type"] is None and "少ない" in d["analysis"]["type_why"]
    assert d["analysis"]["momentum"]["label"] is None and d["analysis"]["style"]["style"] is None
    assert d["balance"] is None


def test_room_daily(tmp_path):
    with Store(tmp_path) as s:
        s.add_room_daily([{"room_id": 1, "followers": 100, "viewers": 50, "started_at": 111}], NOW - 10 * DAY)
        s.add_room_daily([{"room_id": 1, "followers": 100, "viewers": 80, "started_at": 111}], NOW - 10 * DAY + 300)
        s.add_room_daily([{"room_id": 1, "followers": 101, "viewers": 20, "started_at": 222}], NOW - 10 * DAY + 600)
        s.add_room_daily([{"room_id": 1, "followers": 130, "viewers": None, "started_at": 333}], NOW)
        rows = s.all("SELECT * FROM room_daily ORDER BY day")
        assert (rows[0]["lives"], rows[0]["view_max"], rows[0]["view_sum"], rows[0]["view_n"], rows[0]["followers"]) == (2, 80, 150, 3, 101)
        assert rows[1]["view_n"] == 0
        g = ra.followers_growth(s, 1, NOW)
        assert g["change"] == 29
        s.add_room_daily([{"room_id": 2, "followers": 1}], NOW + 500 * DAY)  # 古い日の分は消える
        assert s.scalar("SELECT COUNT(*) FROM room_daily WHERE room_id=1") == 0


def test_comment_counter(tmp_path):
    t = [NOW]
    c = comment_counts.CommentCounter(tmp_path, clock=lambda: t[0])
    c.start("aaa", 9)
    for u, cm in ((1, "こんにちは"), (2, "50"), (2, "５０"), (1, "わこつ")):
        c.feed("aaa", 9, {"t": 1, "u": u, "cm": cm})
    c.feed("aaa", 9, {"t": 2, "u": 3})  # ギフトは数えない
    c.flush("aaa")
    raw = (tmp_path / "comment_counts" / "aaa.json").read_text(encoding="utf-8")
    assert "こんにちは" not in raw and "わこつ" not in raw  # 本文は残さない
    (live,) = comment_counts.load(tmp_path, "aaa")
    assert (live["comments"], live["numeric"], live["users"], live["partial"]) == (4, 2, 2, False)
    assert "user" not in raw.replace('"users"', "")  # ユーザー ID は残さない
    c.start("aaa", 9)  # つなぎ直した (途中が切れていた)
    c.flush("aaa")
    assert comment_counts.load(tmp_path, "aaa")[0]["partial"] is True
    # keeper を起動し直したあとも、前に数えた分の続きから数える
    c2 = comment_counts.CommentCounter(tmp_path, clock=lambda: t[0])
    c2.feed("aaa", 9, {"t": 1, "u": 5, "cm": "x"})
    c2.flush("aaa")
    assert comment_counts.load(tmp_path, "aaa")[0]["comments"] == 5
    t[0] += 200 * DAY  # 古い配信は消える
    c2.feed("aaa", 10, {"t": 1, "u": 5, "cm": "x"})
    c2.flush("aaa")
    assert [x["live_id"] for x in comment_counts.load(tmp_path, "aaa")] == [10]


def test_balance(tmp_path):
    c = comment_counts.CommentCounter(tmp_path, clock=lambda: NOW)
    for live in (1, 2, 3):
        for u in range(10):
            c.feed("aaa", live, {"t": 1, "u": u, "cm": "hi"})
    c.flush("aaa")
    (tmp_path / "gifts").mkdir()
    (tmp_path / "gifts" / "aaa.jsonl").write_text("".join(
        json.dumps({"at": int(NOW), "live_id": live, "user_id": u, "gift_id": 1, "num": 1}) + "\n"
        for live in (1, 2, 3) for u in range(5)), encoding="utf-8")
    (tmp_path / "cache").mkdir()
    (tmp_path / "cache" / "gift_catalog.json").write_text(json.dumps({"gifts": {"1": {"point": 100, "free": False}}}), encoding="utf-8")
    b = ra.balance(tmp_path, "aaa", NOW)
    assert len(b["lives"]) == 3 and b["lives"][0]["commenters"] == 10
    assert b["per_live"]["comments"] == 10 and b["per_live"]["paid_gifters"] == 5 and b["per_live"]["paid_points"] == 500
    assert b["ratio"] == 0.5 and b["label"] == "ギフト寄り"
    assert ra.balance(tmp_path, "zzz", NOW) is None


def test_comment_counter_flush_due():
    import tempfile
    from pathlib import Path as P

    d = P(tempfile.mkdtemp())
    t = [NOW]
    c = comment_counts.CommentCounter(d, clock=lambda: t[0])
    c.feed("aaa", 1, {"t": 1, "u": 1, "cm": "a"})  # 最初の 1 件は書く
    t[0] += 10
    c.feed("aaa", 1, {"t": 1, "u": 2, "cm": "b"})  # 1 分たっていないので、まだ書かない
    assert comment_counts.load(d, "aaa")[0]["comments"] == 1
    c.flush_due("aaa")
    assert comment_counts.load(d, "aaa")[0]["comments"] == 1
    t[0] += 60
    c.flush_due("aaa")  # 新しいコメントが来なくても、配信の最後の分を書く
    assert comment_counts.load(d, "aaa")[0]["comments"] == 2
