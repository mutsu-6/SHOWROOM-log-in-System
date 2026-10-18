import json
from datetime import datetime
import time

from showroom_keeper import comment_counts, explore
from showroom_keeper import room_analysis as ra
from showroom_keeper.status import JST
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
    assert contribution_stats([5, 1, 10, 3]) == (4, 19, 10, 18, 19, 0)  # 100 人に届かないので 100 位は 0
    assert contribution_stats(range(1, 151))[5] == 51  # 150 人なら、100 位は 51 pt
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
        # ルーム 1 は最初から 1 位 (逃げ)、ルーム 4 は最終日に大きく伸ばす (追い込み)。終わる 24 時間前 (6/7) の回もある
        for i, f in enumerate((0.05, 0.25, 0.5, 0.75, 6 / 7, 1.0)):
            at = start + f * (end - start)
            s.add_progress(5, [{"room_id": 1, "point": 1000 * (i + 1)}, {"room_id": 2, "point": 500 * (i + 1)},
                               {"room_id": 3, "point": 400 * (i + 1)}, {"room_id": 4, "point": 10 if i < 4 else 100 if i == 4 else 4000},
                               {"room_id": 5, "point": 300 * (i + 1)}], at)
        rows = {r[1]: r for r in ra.event_styles(s, 5)}
        assert rows[1][2] == "逃げ" and rows[4][2] == "追い込み"
        assert rows[4][7] > 0.9  # 最終日のポイントの割合
        s.conn.execute("UPDATE events SET rooms_at=? WHERE event_id=5", (end + 60,))
        assert ra.step(s, NOW) is True  # 走り方を作る
        assert s.scalar("SELECT COUNT(*) FROM room_event_style WHERE event_id=5") == 5
        assert s.one("SELECT style_at FROM events WHERE event_id=5")[0] == NOW
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


def test_momentum(tmp_path):
    with Store(tmp_path) as s:
        # 前の期間は 10 ルーム中 8 位前後、直近は 2 位前後 (5 件ずつ)
        plan = [(e, 100 - e * 5, (8, 7, 8, 9, 8)[e - 1]) for e in range(1, 6)] + [(e, 55 - (e - 6) * 10, (2, 3, 2, 1, 2)[e - 6]) for e in range(6, 11)]
        for eid, days, rank in plan:
            _event(s, eid, NOW - days * DAY, {1: (100, rank, []), **{r: (10, r if r != rank else 1, []) for r in range(2, 11)}})
        m = ra.momentum(s, 1, NOW)
        assert m["label"] == "上り調子" and m["value"] == 0.67 and m["noise"] < 0.2  # (8-1)/9 - (2-1)/9
        assert ra.momentum(s, 1, NOW - 50 * DAY)["label"] is None  # 直近のイベントが足りない


def test_momentum_needs_five_events_and_a_clear_difference(tmp_path):
    with Store(tmp_path) as s:
        # 2 件ずつ: 数字は出すが、上り調子とは言わない (偶然の差でもよく出るため)
        for eid, days, rank in ((1, 100, 8), (2, 90, 8), (3, 20, 2), (4, 10, 2)):
            _event(s, eid, NOW - days * DAY, {1: (100, rank, []), **{r: (10, r, []) for r in range(2, 11)}})
        m = ra.momentum(s, 1, NOW)
        assert m["value"] == 0.67 and m["label"] is None and "5 件" in m["label_why"]
    with Store(tmp_path / "b") as s:
        # 5 件ずつでも、ばらつきが大きく差がはっきりしない時は「変わらない」
        plan = [(e, 100 - e * 5, (1, 10, 1, 10, 1)[e - 1]) for e in range(1, 6)] + [(e, 55 - (e - 6) * 10, (10, 1, 10, 1, 5)[e - 6]) for e in range(6, 11)]
        for eid, days, rank in plan:
            _event(s, eid, NOW - days * DAY, {1: (100, rank, []), **{r: (10, r if r != rank else 1, []) for r in range(2, 11)}})
        assert ra.momentum(s, 1, NOW)["label"] == "変わらない"


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


def test_places_ties_get_the_same_worst_place():
    assert ra._places([(5, 100), (1, 0), (2, 0), (3, 0)]) == {5: 0.0, 1: 1.0, 2: 1.0, 3: 1.0}
    assert ra._places([(1, 9), (2, 5), (3, 5), (4, 1)]) == {1: 0.0, 2: 2 / 3, 3: 2 / 3, 4: 1.0}
    assert ra._places([(1, 0)]) == {1: 0.0}


def test_event_styles_ignore_room_numbers_when_tied(tmp_path):
    with Store(tmp_path) as s:
        start, end = NOW - 10 * DAY, NOW - 3 * DAY
        s.upsert_events([{"event_id": 5, "start": start, "end": end}])
        for i, f in enumerate((0.05, 0.25, 0.5, 0.75, 0.9, 1.0)):
            at = start + f * (end - start)
            # 序盤 (2 回目まで) は全員 0 ポイント。そのあとも全員同じポイント
            s.add_progress(5, [{"room_id": r, "point": 0 if i < 2 else 100 * i} for r in range(1, 9)], at)
        rows = {r[1]: r for r in ra.event_styles(s, 5)}
        assert {r[3] for r in rows.values()} == {None}  # 序盤は誰もポイントが無いので、序盤の位置は分からない扱い
        assert {r[2] for r in rows.values()} == {None}  # 差し・逃げなどにならない
        assert {r[9] for r in rows.values()} == {0.0}  # 0 ポイントや同点の回は、だれの 1 位にもしない


def test_room_style_counts_events_without_style(tmp_path):
    with Store(tmp_path) as s:
        for eid in range(1, 11):
            s.upsert_events([{"event_id": eid, "end": NOW - eid * DAY}])
            s.conn.execute("INSERT INTO room_event_style(event_id, room_id, style) VALUES (?, 1, ?)", (eid, "逃げ" if eid <= 3 else None))
        st = ra.room_style(s, 1)
        # 逃げ 3 件・型なし 7 件は、いくつもの走り方をしているわけではないので「自在」ではなく「型なし」
        assert st["style"] == "型なし" and st["events"] == 10 and st["counts"] == {"逃げ": 3, "型なし": 7}
        # 逃げ 3・差し 3・型なし 4: 型の付いたイベントが半分以上で 2 種類 → 自在
        for eid in (4, 5, 6):
            s.conn.execute("UPDATE room_event_style SET style='差し' WHERE event_id=?", (eid,))
        assert ra.room_style(s, 1)["style"] == "自在"


def test_momentum_is_not_dragged_by_small_events(tmp_path):
    with Store(tmp_path) as s:
        # 前は 500 ルームのイベントで 1 位、直近は 5 ルームのイベントで 1 位: 同じ成績なので「変わらない」
        for eid, days, n in [(e, 100 - e * 5, 500) for e in range(1, 6)] + [(e, 55 - (e - 6) * 10, 5) for e in range(6, 11)]:
            _event(s, eid, NOW - days * DAY, {1: (1000, 1, []), **{r: (10, r, []) for r in range(2, n + 1)}})
        m = ra.momentum(s, 1, NOW)
        assert m["value"] == 0 and m["label"] == "変わらない" and m["recent_place"] == 0


def test_no_sashi_when_nobody_had_points_early(tmp_path):
    with Store(tmp_path) as s:
        start, end = NOW - 10 * DAY, NOW - 3 * DAY
        s.upsert_events([{"event_id": 5, "start": start, "end": end}])
        # 序盤 (4 分の 1 の時) まで全員 0 ポイント、そのあと差が付く: 上位で終えたルームを「差し」にしない
        for i, f in enumerate((0.05, 0.25, 0.5, 0.75, 0.9, 1.0)):
            s.add_progress(5, [{"room_id": r, "point": 0 if i < 2 else r * 100 * i} for r in range(1, 9)], start + f * (end - start))
        rows = {r[1]: r for r in ra.event_styles(s, 5)}
        assert rows[7][2] is None and rows[7][3] is None and rows[7][8] is None
        # ルーム 8 は、ポイントの付いた回 (4 回) はずっと 1 位: 誰もポイントの無い回は数えないので「逃げ」
        assert rows[8][2] == "逃げ" and rows[8][9] == 1.0
        # 1 ルームでもポイントがあれば (8 ルームの 1 割以上)、遅れて来たルームは序盤が後ろだったと分かる
        s.add_progress(5, [{"room_id": 1, "point": 50}], start + 0.25 * (end - start))
        rows = {r[1]: r for r in ra.event_styles(s, 5)}
        assert rows[7][2] == "差し"


def test_new_share_ignores_regulars_seen_in_older_events(tmp_path):
    with Store(tmp_path) as s:
        # 常連 1〜3 は毎回。常連 4 は 1 回目と 3 回目だけ (2 回目は載らなかった)。5 は 3 回目に初めて来た
        people = {1: [1, 2, 3, 4], 2: [1, 2, 3], 3: [1, 2, 3, 4, 5]}
        for eid, users in people.items():
            s.upsert_events([{"event_id": eid, "start": NOW - (40 - eid * 10) * DAY - DAY, "end": NOW - (40 - eid * 10) * DAY}])
            s.set_event_rooms(eid, [{"room_id": 1, "point": 1000, "rank": 1}, {"room_id": 2, "point": 10, "rank": 2}], NOW)
            s.set_contributions(eid, 1, [{"user_id": u, "point": 100 - u, "rank": u} for u in users], NOW, final=True)
        ns = ra.new_share(s, 1)
        # 2 回目: 新しい人 0、3 回目: 5 だけが新しい (4 は前に見ている) → 0 と 1/5 の真ん中
        assert ns["events"] == 2 and abs(ns["value"] - 0.1) < 1e-9


def test_momentum_ignores_tiny_events(tmp_path):
    with Store(tmp_path) as s:
        # 2 ルームのイベントでは、1 位か最下位しかないので勢いに使わない
        for eid, days, rank in ((1, 100, 2), (2, 90, 2), (3, 20, 1), (4, 10, 1)):
            _event(s, eid, NOW - days * DAY, {1: (100, rank, []), 9: (50, 3 - rank, [])})
        m = ra.momentum(s, 1, NOW)
        assert m["label"] is None and m["recent_events"] == 0


def test_depth_from_100th_contributor(tmp_path):
    with Store(tmp_path) as s:
        # 25 ルーム × 3 イベント (同じ規模)。100 位のポイントがルームごとに違う。ルーム 25 は 50 人しかいない
        for eid in (1, 2, 3):
            rooms = {r: (100000, r, [1000] * 99 + [r * 10] if r < 25 else [1000] * 50) for r in range(1, 26)}
            _event(s, eid, NOW - eid * 10 * DAY, rooms)
        assert s.one("SELECT c_p100 FROM room_events WHERE room_id=7 AND event_id=1")[0] == 70
        s.set_meta("room_stats_upto", True)
        while ra.step(s, NOW):
            pass
        depth = {r["room_id"]: r["depth"] for r in s.all("SELECT room_id, depth FROM room_profile")}
        assert depth[24] == "厚い" and depth[1] == "やや薄い" and depth[12] == "ふつう" and depth[25] == "薄い"
        assert s.one("SELECT depth_pt FROM room_profile WHERE room_id=24")[0] == 240
    lst = explore.room_profiles(tmp_path, depth="薄い")
    assert [r["room_id"] for r in lst["rooms"]] == [25] and lst["depth_counts"]["厚い"] >= 1
    assert explore.room_profiles(tmp_path, order="depth")["rooms"][0]["room_id"] == 24
    d = explore.room_detail(tmp_path, "24")["analysis"]
    assert d["depth"] == "厚い" and d["metrics"]["depth_pt"] == 240
    # 100 位より下の分: ルームのポイント 100000 - 上位 100 人の合計 (99000 + 240) = 760
    assert d["metrics"]["below_pt"] == 760 and abs(d["metrics"]["below_share"] - 0.0076) < 1e-9
    assert {e["below"] for e in explore.room_detail(tmp_path, "24")["events"]} == {760}
    m25 = explore.room_detail(tmp_path, "25")
    assert m25["analysis"]["metrics"]["below_pt"] is None and {e["below"] for e in m25["events"]} == {None}  # 100 人に届かない
    assert explore.room_profiles(tmp_path, order="below")["rooms"][0]["room_id"] == 1  # 100 位が 10 pt で、下の分がいちばん多い


def test_depth_needs_enough_rooms_to_compare():
    m = {"size_class": 1, "depth_pt": 50, "depth_rel": 0.5}
    assert ra.depth_label(m, {"1": {"depth_n": 3, "depth_p25": 0.1, "depth_p75": 1}}) is None
    assert ra.depth_label({**m, "depth_pt": 0, "depth_rel": 0}, None) == "薄い"  # 100 人に届かないのは、くらべなくても薄い
    assert ra.depth_label(None, None) is None


def test_styles_need_enough_rooms_and_nearby_snapshots(tmp_path):
    with Store(tmp_path) as s:
        start, end = NOW - 10 * DAY, NOW - 7 * DAY  # 3 日のイベント
        s.upsert_events([{"event_id": 1, "start": start, "end": end}, {"event_id": 2, "start": start, "end": end}])
        hours = [start + h * 3600 for h in range(0, 73, 3)]
        for at in hours:
            k = (at - start) / (end - start)
            # 毎日同じペースで伸びるルームたち (最後の 24 時間で 33% になる): 追い込みではない
            s.add_progress(1, [{"room_id": r, "point": int(1000 * r * k) + 1} for r in range(1, 7)], at)
            s.add_progress(2, [{"room_id": r, "point": int(1000 * r * k) + 1} for r in range(1, 4)], at)  # 3 ルームだけ
        rows = ra.event_styles(s, 1)
        assert rows and not any(r[2] == "追い込み" for r in rows)
        assert all(abs(r[7] - 1 / 3) < 0.05 for r in rows if r[1] > 1)  # 最後の 24 時間の割合は約 3 分の 1
        assert ra.event_styles(s, 2) == []  # 参加ルームが少ないイベントは、走り方を出さない
        # 中盤の回が無い (推移が途切れた) 時は、離れた回で代わりに測らない
        s.conn.execute("DELETE FROM event_progress WHERE event_id=1 AND at BETWEEN ? AND ?", (start + 0.6 * 86400, start + 2.4 * 86400))
        assert all(r[4] is None and r[5] is None for r in ra.event_styles(s, 1))


def test_thin_progress_keeps_what_the_analysis_needs(tmp_path):
    with Store(tmp_path) as s:
        start, end = NOW - 20 * DAY, NOW - 10 * DAY  # 10 日間のイベントを 1 時間ごとに残した
        s.upsert_events([{"event_id": 9, "start": start, "end": end}])
        hours = int((end - start) // 3600)
        for h in range(hours + 1):
            s.add_progress(9, [{"room_id": r, "point": r * h} for r in range(1, 4)], start + h * 3600)
        s.conn.execute("UPDATE events SET style_at=? WHERE event_id=9", (NOW,))
        s.set_meta("style_version", ra.STYLE_VERSION)
        before = s.scalar("SELECT COUNT(DISTINCT at) FROM event_progress WHERE event_id=9")
        assert ra.step(s, NOW) is True
        ats = [r[0] for r in s.all("SELECT DISTINCT at FROM event_progress WHERE event_id=9 ORDER BY at")]
        assert len(ats) < before / 3  # 241 回 → 60 回ほど
        assert ats[0] == start and ats[-1] == end
        assert all(start + h * 3600 in ats for h in range(hours - 24, hours + 1))  # 最後の 24 時間は全部残す
        for f in (0.25, 0.5, 0.75):  # 走り方の作り直しに使う回 (狙った時刻の前後) は残す
            assert start + f * (end - start) in ats
        assert ra.event_styles(s, 9) is not None
        assert s.one("SELECT thinned_at FROM events WHERE event_id=9")[0] == NOW
        # 終わって間もないイベントは、まだ間引かない
        s.upsert_events([{"event_id": 10, "start": NOW - 3 * DAY, "end": NOW - DAY}])
        s.add_progress(10, [{"room_id": 1, "point": 1}], NOW - 2 * DAY)
        s.conn.execute("UPDATE events SET style_at=? WHERE event_id=10", (NOW,))
        ra.step(s, NOW)
        assert s.one("SELECT thinned_at FROM events WHERE event_id=10")[0] is None


def test_tactics_marks(tmp_path):
    with Store(tmp_path) as s:
        start, end = NOW - 10 * DAY, NOW - 3 * DAY
        s.upsert_events([{"event_id": 9, "start": start, "end": end}])
        # ルーム 1 は序盤に大きく引き離す。2 位のルーム 2 は、引き離されたあと伸びが鈍る。ルーム 3 と 4 は 1 位を取り合う回がある
        plan = {0.05: (500, 100, 50, 40, 30), 0.25: (2000, 500, 400, 300, 200), 0.5: (3000, 600, 3500, 400, 300),
                0.75: (4000, 700, 3800, 4500, 400), 6 / 7: (5000, 800, 4000, 4600, 500), 1.0: (9000, 900, 4100, 4700, 600)}
        for f, pts in plan.items():
            s.add_progress(9, [{"room_id": r + 1, "point": p} for r, p in enumerate(pts)], start + f * (end - start))
        rows = {r[1]: r for r in ra.event_styles(s, 9)}
        assert abs(rows[1][11] - 0.75) < 1e-9  # 序盤の 2 位との差 (2000 - 500) / 2000
        assert rows[1][12] < 0.5  # 2 位の伸びが鈍った
        assert rows[2][11] is None  # 序盤に 1 位でなかったルームには付けない
        assert rows[1][10] == 2 and rows[3][10] == 2 and rows[4][10] == 2 and rows[2][10] == 0  # 1 位が 1→3→4→1 と入れ替わった
        s.conn.executemany("""INSERT INTO room_event_style(event_id, room_id, style, swaps, early_gap, rival_slow)
                              VALUES (?, ?, ?, ?, ?, ?)""", [(9, r[1], r[2], r[10], r[11], r[12]) for r in rows.values()])
        # 節約: 1 位になったイベントで、2 位との差の割合
        for eid, (p1, p2) in enumerate(((1000, 980), (1000, 500)), 20):
            _event(s, eid, NOW - eid * DAY, {1: (p1, 1, []), 2: (p2, 2, []), **{r: (10, r, []) for r in range(3, 7)}})
        t = ra.tactics(s, 1)
        assert t["wins"] == 2 and t["close_wins"] == 1 and abs(t["win_margin"] - (0.02 + 0.5) / 2) < 1e-9
        assert t["early_leads"] == 1 and t["early_gap"] == 0.75 and t["swaps"] == 2


def test_listener_rounds(tmp_path):
    with Store(tmp_path) as s:
        # イベント 1: ルーム 1 と 2 が競争。同じ時期のイベント 2 にはルーム 3 (競争相手ではない) が出ている
        for eid in (1, 2):
            s.upsert_events([{"event_id": eid, "start": NOW - 10 * DAY, "end": NOW - 3 * DAY}])
        s.upsert_events([{"event_id": 0, "start": NOW - 40 * DAY, "end": NOW - 30 * DAY}])  # 前のイベント
        s.set_event_rooms(0, [{"room_id": 3, "point": 100, "rank": 1}], NOW)
        s.set_contributions(0, 3, [{"user_id": 50, "point": 500}], NOW, final=True)  # 50 はルーム 3 が最推し (ガチイベで 500)
        s.set_event_rooms(1, [{"room_id": 1, "point": 1000, "rank": 1}, {"room_id": 2, "point": 900, "rank": 2}], NOW)
        s.set_event_rooms(2, [{"room_id": 3, "point": 500, "rank": 1}, {"room_id": 2, "point": 1, "rank": 2}], NOW)
        s.set_contributions(1, 1, [{"user_id": 10, "point": 800}, {"user_id": 50, "point": 30}], NOW, final=True)
        s.set_contributions(1, 2, [{"user_id": 20, "point": 900}], NOW, final=True)
        # ルーム 1 のコアな人 (10) が、ルーム 3 (競争相手でない) とルーム 2 (競争相手) に送った
        s.set_contributions(2, 3, [{"user_id": 10, "point": 50}], NOW, final=True)
        s.set_contributions(2, 2, [{"user_id": 10, "point": 5}], NOW, final=True)
        r = ra.listener_rounds(s, 1)
    e = next(x for x in r["events"] if x["event_id"] == 1)
    assert e["rooms"] == 1 and e["points"] == 50 and e["visitors"] == 1  # 競争相手のルーム 2 は数えない
    assert e["returned"] == 1  # ルーム 3 が最推しの 50 が、お返しにルーム 1 に来た (30 は最推しの 500 の 2 割より少ない → 付き合い投げ)
    assert e["kinds"] == {"推し": 1, "掛け持ち": 0, "付き合い": 1}  # 10 はルーム 1 が最推し


def test_listener_rounds_counts_fans_and_the_streamer(tmp_path):
    with Store(tmp_path) as s:
        for eid in (1, 2):
            s.upsert_events([{"event_id": eid, "start": NOW - 10 * DAY, "end": NOW - 3 * DAY}])
        s.set_event_rooms(1, [{"room_id": 1, "point": 1000, "rank": 1}], NOW)
        s.set_event_rooms(2, [{"room_id": 3, "point": 500, "rank": 1}, {"room_id": 4, "point": 400, "rank": 2}], NOW)
        s.set_contributions(1, 1, [{"user_id": 10, "point": 800}], NOW, final=True)
        ym = datetime.fromtimestamp(NOW - 3 * DAY, JST).strftime("%Y%m")
        s.set_active_fans(1, ym, 30, 2, [{"user_id": 60, "level": 9, "user_name": "常連"}], NOW)  # ポイントに出ない常連
        s.set_user_room(70, 1, NOW, "abc0070")  # 70 はルーム 1 の配信者 (URL の名前が ID で終わる)
        s.set_contributions(2, 3, [{"user_id": 60, "point": 20}], NOW, final=True)  # 常連がルーム 3 に挨拶回り
        s.set_contributions(2, 4, [{"user_id": 70, "point": 15}], NOW, final=True)  # 配信者がルーム 4 に挨拶回り
        r = ra.listener_rounds(s, 1)
        reg = ra.regulars(s, 1)
    e = r["events"][0]
    # 配信者はコアな人に入れず、別に数える (配信者が分からないルームとくらべられるように)
    assert r["owner"] and e["rooms"] == 1 and e["visitors"] == 1 and e["owner_rooms"] == 1 and e["owner_points"] == 15
    assert reg["total"] == 2 and reg["silent"] == 1  # 貢献ランキングに出ていない常連が 1 人


def test_room_owner_only_when_sure(tmp_path):
    with Store(tmp_path) as s:
        s.set_user_room(70, 1, NOW, "abc0070")
        assert ra.room_owner(s, 1) == 70
        s.set_user_room(71, 2, NOW, "my_room")  # URL の名前が ID と合わない → 本人と決めない
        assert ra.room_owner(s, 2) is None
        s.set_user_room(80, 1, NOW, "def0080")  # 1 つのルームに 2 人 → 分からない
        assert ra.room_owner(s, 1) is None
        s.set_user_room(90, 3, NOW)  # 前の版で残した (URL の名前が無い)
        assert ra.room_owner(s, 3) is None


def test_lure_throws(tmp_path):
    (tmp_path / "gifts").mkdir()
    (tmp_path / "cache").mkdir()
    (tmp_path / "cache" / "gift_catalog.json").write_text(json.dumps({"gifts": {"1": {"point": 100, "free": False},
                                                                                  "2": {"point": 10000, "free": False},
                                                                                  "3": {"point": 1, "free": True}}}))
    lines = []
    t0 = int(NOW) - 7200
    lines.append({"at": t0, "live_id": 1, "user_id": 1, "gift_id": 2, "num": 10})  # 常連 1 の大きいギフト
    lines.append({"at": t0 + 1, "live_id": 1, "user_id": 2, "gift_id": 2, "num": 5})  # 常連 2 の大きいギフト
    for k in range(4):  # 常連が 10 分おきに少額 (100 円) を送る (誘い投げ)。前の 3 分は 1 人、後の 3 分は 3 人がギフトを送る
        t = t0 + 600 + k * 600
        lines.append({"at": t, "live_id": 1, "user_id": 1 if k % 2 == 0 else 2, "gift_id": 1, "num": 1})
        lines.append({"at": t - 60, "live_id": 1, "user_id": 100 + k, "gift_id": 3, "num": 10})
        lines += [{"at": t + 30 + i, "live_id": 1, "user_id": 200 + k * 10 + i, "gift_id": 1 if i == 0 else 3, "num": 1} for i in range(3)]
    (tmp_path / "gifts" / "aaa.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines))
    ra.LURE_CORE, old = 2, ra.LURE_CORE
    try:
        r = ra.lure_throws(tmp_path, "aaa", NOW)
        assert r["core"] == 2 and r["lures"] == 4 and r["gap"] == 600
        assert r["people_before"] == 4 and r["people_after"] == 12 and r["people_ratio"] == 3.0  # 誘い投げのあと、送る人が 3 倍
        assert r["points_ratio"] is None  # 前の 3 分には有料ギフトが無い (くらべられない)
        assert r["per_hour"] > 1
        # 3 人が 20 秒おきに順に送る (常連でない人も入る)・5 分あけて 1 人が続けて送る (リレーではない)
        quick = [{"at": t0 + k * 20, "live_id": 2, "user_id": (1, 2, 300)[k % 3], "gift_id": 1, "num": 1} for k in range(6)]
        quick += [{"at": t0 + 400 + k * 10, "live_id": 2, "user_id": 301, "gift_id": 1, "num": 1} for k in range(4)]
        quick.append({"at": t0 + 1000, "live_id": 2, "user_id": 302, "gift_id": 2, "num": 1})  # 大きなギフトに
        quick.append({"at": t0 + 1010, "live_id": 2, "user_id": 303, "gift_id": 1, "num": 1})  # 10 秒後にほかの人が応えた
        (tmp_path / "gifts" / "bbb.jsonl").write_text("".join(json.dumps(x) + "\n" for x in quick))
        q = ra.lure_throws(tmp_path, "bbb", NOW)
        assert q["relays"] == 1 and q["relay_people_max"] == 3 and q["relay_longest"] == 6
        assert q["relay_share"] == 600 / (1100 + 10000) and q["relay_core_start"] == 0.0  # コアな常連 (多く送った 2 人) は始めていない
        assert q["respond"]["大きなギフト"] == {"n": 1, "rate": 1.0}
        assert q["respond_base"] is not None and q["respond_base"] < 1
    finally:
        ra.LURE_CORE = old
    assert ra.lure_throws(tmp_path, "zzz", NOW) is None