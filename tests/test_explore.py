import json
import time

import pytest

from showroom_keeper import explore, listing, search_index
from showroom_keeper.gifts import record_gift
from showroom_keeper.research_store import Store

NOW = time.time()


@pytest.fixture
def db(tmp_path):
    with Store(tmp_path) as s:
        s.upsert_rooms([{"room_id": 1, "key": "aaa", "name": "ルームA", "followers": 900},
                        {"room_id": 2, "key": "bbb", "name": "ルームB", "followers": 50},
                        {"room_id": 3, "key": "ccc", "name": "ルームC", "followers": 10}], "live", NOW)
        s.upsert_events([{"event_id": 10, "name": "終わったイベント", "key": "old_ev", "start": NOW - 9 * 86400, "end": NOW - 2 * 86400},
                         {"event_id": 11, "name": "開催中イベント", "key": "now_ev", "start": NOW - 86400, "end": NOW + 3600},
                         {"event_id": 12, "name": "これからのイベント", "key": "next_ev", "start": NOW + 86400, "end": NOW + 9 * 86400}])
        s.set_event_rooms(10, [{"room_id": 1, "point": 5000, "rank": 1}, {"room_id": 2, "point": 3000, "rank": 2},
                               {"room_id": 3, "point": 0, "rank": 3}], NOW)
        s.set_event_rooms(11, [{"room_id": 1, "point": 100, "rank": 2}, {"room_id": 2, "point": 200, "rank": 1}], NOW)
        s.set_contributions(10, 1, [{"user_id": 7, "name": "大口", "point": 4000, "rank": 1},
                                    {"user_id": 8, "name": "中堅", "point": 1000, "rank": 2}], NOW, final=True)
        s.set_contributions(10, 2, [{"user_id": 7, "name": "大口", "point": 2500, "rank": 1},
                                    {"user_id": 9, "name": "退会済みユーザー", "point": 500, "rank": 2}], NOW, final=True)
        s.set_cumulative(1, [{"user_id": 7, "name": "大口", "point": 90000, "visits": 40, "rank": 1},
                             {"user_id": 8, "name": "中堅", "point": 2000, "visits": 5, "rank": 2}], NOW)
        s.add_progress(11, [{"room_id": 1, "point": 50, "rank": 1}, {"room_id": 2, "point": 40, "rank": 2}], NOW - 7200)
        s.add_progress(11, [{"room_id": 1, "point": 100, "rank": 2}, {"room_id": 2, "point": 200, "rank": 1}], NOW - 3600)
    return tmp_path


@pytest.fixture(params=["index", "like"])
def search_mode(request, monkeypatch):
    """検索は、名前の索引 (search_index) と、索引の無い時の前のやり方 (LIKE) の両方で確かめる。"""
    if request.param == "like":
        monkeypatch.setattr(search_index, "ensure", lambda s: False)
    return request.param


def test_search_finds_gifters_rooms_and_events(db, search_mode):
    r = explore.search(db, "大口")
    assert [g["user_id"] for g in r["gifters"]] == [7] and r["gifters"][0]["points"] == 90000 + 2500
    assert [x["key"] for x in explore.search(db, "ルーム")["rooms"]] == ["aaa", "bbb", "ccc"]  # フォロワーの多い順
    assert explore.search(db, "bbb")["rooms"][0]["name"] == "ルームB"
    ev = explore.search(db, "イベント")["events"]
    assert {e["event_id"]: e["state"] for e in ev} == {10: "ended", 11: "ongoing", 12: "upcoming"}
    assert explore.search(db, "8")["gifters"][0]["user_id"] == 8  # ユーザー ID
    assert explore.search(db, "") == {"gifters": [], "rooms": [], "events": []}


def test_room_detail(db):
    d = explore.room_detail(db, "aaa")
    assert d["found"] and d["name"] == "ルームA" and d["room_id"] == 1
    assert [(c["rank"], c["name"], c["point"]) for c in d["cumulative"]] == [(1, "大口", 90000), (2, "中堅", 2000)]
    assert [(g["user_id"], g["points"], g["rooms"]) for g in d["gifters"]] == [(7, 90000, 2), (8, 2000, 1)]
    assert [(e["event_id"], e["rank"], e["point"]) for e in d["events"]] == [(12, None, None), (11, 2, 100), (10, 1, 5000)][1:]
    assert d["event_stats"]["wins"] == 1 and d["event_stats"]["best"] == 1
    assert [(o["key"], o["shared"]) for o in d["overlap"]] == [("bbb", 1)]
    assert explore.room_detail(db, "1")["key"] == "aaa"  # room_id でも開ける
    assert explore.room_detail(db, "zzz") == {"key": "zzz", "found": False}


def test_event_list(db):
    on = explore.event_list(db, "ongoing")
    assert [e["event_id"] for e in on["events"]] == [11]
    assert on["events"][0]["rooms"] == 2 and on["events"][0]["snapshots"] == 2 and on["events"][0]["leader"] == "ルームB"
    assert [e["event_id"] for e in explore.event_list(db, "upcoming")["events"]] == [12]
    ended = explore.event_list(db, "ended")["events"]
    assert [e["event_id"] for e in ended] == [10] and ended[0]["rankings"] == 2
    with pytest.raises(ValueError):
        explore.event_list(db, "bad")


def test_event_detail(db):
    d = explore.event_detail(db, 10)
    assert d["found"] and d["state"] == "ended" and d["room_total"] == 3
    assert [(r["name"], r["rank"], r["contributors"]) for r in d["rooms"]] == [("ルームA", 1, 2), ("ルームB", 2, 2), ("ルームC", 3, 0)]
    # 退会したユーザーも出す (印つき)。複数のルームに送った人は合計する
    assert [(c["user_id"], c["point"], c["rooms"], c["top_room"], c["left"]) for c in d["contributors"]] == [
        (7, 6500, 2, "ルームA", False), (8, 1000, 1, "ルームA", False), (9, 500, 1, "ルームB", True)]
    assert d["rankings"] == 2 and d["rankings_pending"] == 0  # ポイント 0 のルームは取らない
    p = explore.event_detail(db, "now_ev")["progress"]
    assert len(p["times"]) == 2 and [r["name"] for r in p["rooms"]] == ["ルームB", "ルームA"]
    assert p["rooms"][0]["points"] == [40, 200] and p["rooms"][0]["ranks"] == [2, 1]
    assert explore.event_detail(db, 999) == {"event_id": "999", "found": False}


def test_search_finds_former_names_recorded_gifters_and_known_rooms(db, search_mode):
    from showroom_keeper.gifts import record_gift
    from showroom_keeper.listing import remember_rooms

    with Store(db) as s:
        s.set_cumulative(1, [{"user_id": 7, "name": "大口の新しい名前", "point": 90000, "visits": 40, "rank": 1}], NOW + 1)
    # 前の名前でも見つかる
    g = explore.search(db, "大口")["gifters"]
    assert g[0]["user_id"] == 7 and g[0]["match"] is None  # 今の名前にも「大口」が入っている
    g = {x["user_id"]: x for x in explore.search(db, "中堅")["gifters"]}
    assert g[8]["match"] is None
    with Store(db) as s:
        s.set_cumulative(1, [{"user_id": 8, "name": "まったく別", "point": 2000, "visits": 5, "rank": 2}], NOW + 2)
    g = {x["user_id"]: x for x in explore.search(db, "中堅")["gifters"]}
    assert g[8]["match"] == {"kind": "former", "name": "中堅"} and g[8]["name"] == "まったく別"
    # 記録したギフトにしか出ていない人も、名前で見つかる
    # (送り主の表は、収集の合間に、記録のファイルの新しい分だけ足す。検索はファイルを読まない)
    record_gift(db, "rec_room", 1, {"u": 55, "ac": "少額さん", "g": 1, "n": 1, "created_at": NOW})
    with Store(db) as s:
        # ギフトの一覧がまだ無ければ足さない (0 pt で足すと戻せない)。SHOWROOM には問い合わせない
        listing.gift_catalog, real = (lambda *a, **k: {}), listing.gift_catalog
        try:
            explore.update_gift_senders(db, s, "rec_room")
        finally:
            listing.gift_catalog = real
        assert s.scalar("SELECT COUNT(*) FROM gift_sender_pos") == 0
    (db / "cache").mkdir(exist_ok=True)
    (db / "cache" / "gift_catalog.json").write_text(json.dumps({"gifts": {"1": {"point": 1, "free": True}}}))
    with Store(db) as s:
        explore.update_gift_senders(db, s, "rec_room")
        assert tuple(s.one("SELECT points, free_points, unknown FROM gift_senders WHERE user_id=55")) == (1, 1, 0)
    assert explore.search(db, "少額")["gifters"][0]["times"] == 1
    record_gift(db, "rec_room", 1, {"u": 55, "ac": "少額さん改", "g": 1, "n": 1, "created_at": NOW + 5})
    with Store(db) as s:
        explore.update_gift_senders(db, s, "rec_room")  # 新しい 1 行だけ足す
        explore.update_gift_senders(db, s, "rec_room")  # 増えていなければ何もしない
    g = explore.search(db, "少額")["gifters"]
    assert g[0]["user_id"] == 55 and g[0]["match"]["kind"] == "recorded" and g[0]["times"] == 2 and g[0]["name"] == "少額さん改"
    assert explore.search(db, "55")["gifters"][0]["user_id"] == 55  # ユーザー ID でも
    # ファイルが小さくなったら (まとめ直し)、そのルームの分を作り直す
    (db / "gifts" / "rec_room.jsonl").write_text((db / "gifts" / "rec_room.jsonl").read_text().splitlines()[0] + "\n")
    with Store(db) as s:
        explore.update_gift_senders(db, s, "rec_room")
    assert explore.search(db, "少額")["gifters"][0]["times"] == 1
    # 設定・記録だけにあるルームも見つかる
    remember_rooms(db, {"rec_room": ("記録だけのルーム", 999)})
    rooms = explore.search(db, "記録だけ")["rooms"]
    assert [(r["key"], r["source"]) for r in rooms] == [("rec_room", "known")]
    # 名前の分からないイベントは番号で見つかり、名前が無いと分かる
    with Store(db) as s:
        s.set_event_rooms(77, [{"room_id": 1, "point": 1, "rank": 1}], NOW)
    ev = explore.search(db, "77")["events"]
    assert ev[0]["event_id"] == 77 and ev[0]["named"] is False and ev[0]["name"] == "イベント 77"


def test_search_ignores_spaces_width_and_kana(tmp_path, monkeypatch, search_mode):
    from showroom_keeper import listing

    with Store(tmp_path) as s:
        s.upsert_rooms([{"room_id": 5, "key": "wnb", "name": "渡辺 みく (WNB39)", "followers": 826},
                        {"room_id": 6, "key": "kana", "name": "ミクのヘヤ", "followers": 10}], "live", NOW)
        s.set_cumulative(5, [{"user_id": 70, "name": "★渡辺みく推し", "point": 100, "visits": 1, "rank": 1}], NOW)
    r = explore.search(tmp_path, "渡辺みく")
    assert [x["key"] for x in r["rooms"]] == ["wnb"] and r["gifters"][0]["user_id"] == 70
    assert [x["key"] for x in explore.search(tmp_path, "みくのへや")["rooms"]] == ["kana"]  # ひらがなでカタカナの名前
    assert [x["key"] for x in explore.search(tmp_path, "ＷＮＢ３９")["rooms"]] == ["wnb"]  # 全角
    # 少ししか見つからない時は SHOWROOM のルーム検索でも探し、見つけたルームは集める対象に足す
    monkeypatch.setattr(listing, "search_showroom_rooms",
                        lambda q: [{"key": "new1", "name": "渡邉みく", "room_id": 99, "followers": 77, "is_live": True},
                                   {"key": "wnb", "name": "渡辺 みく (WNB39)", "room_id": 5}])
    r = explore.search(tmp_path, "渡辺みく", showroom=True)
    assert [x["key"] for x in r["rooms"]] == ["wnb", "new1"] and r["rooms"][1]["source"] == "showroom"
    with Store(tmp_path) as s:
        assert s.one("SELECT found_via FROM rooms WHERE room_id=99")[0] == "search"


def test_search_index_fills_in_steps_and_follows_changes(tmp_path, monkeypatch):
    """索引は表の最初から少しずつ入れ、入れ終わるまでは前のやり方で探す。名前が変わったら、印の付いた分だけ入れ直す。"""
    monkeypatch.setattr(search_index, "FILL_STEP", 2)
    with Store(tmp_path) as s:
        s.set_cumulative(1, [{"user_id": u, "name": f"ながいなまえ{u}", "point": 10 * u, "visits": 1, "rank": u} for u in range(1, 6)], NOW)
        assert search_index.refresh(s, budget=0) == {"three": False, "two": False}  # まだ入れていない
        assert s.meta("search_index")["done"] == []
        # 入れている途中で名前が変わる (入れ終わった所と、まだの所)
        st = search_index._state(s)
        while "u" not in st["done"] and st["u"] < 2:
            search_index._fill(s, st, "u")
        s.set_cumulative(1, [{"user_id": 1, "name": "かわったなまえ", "point": 10, "visits": 1, "rank": 1},
                             {"user_id": 5, "name": "べつのなまえ", "point": 50, "visits": 1, "rank": 5}], NOW + 1)
        assert search_index.refresh(s, budget=5) == {"three": True, "two": True}
        assert s.scalar("SELECT COUNT(*) FROM search_dirty") == 0
        # 前の名前は 1 つずつ (入れている途中に付いた印で、2 つ入らない)
        for t in ("search_former", "search2_former"):
            assert sorted(r[0] for r in s.all(f"SELECT name FROM {t}")) == ["ながいなまえ1", "ながいなまえ5"]
        # 入れ終わったあとの名前の変わりも、両方の索引に 1 つずつ
        s.set_cumulative(1, [{"user_id": 2, "name": "みじかい", "point": 20, "visits": 1, "rank": 2}], NOW + 2)
        search_index.refresh(s, budget=5)
        search_index.refresh(s, budget=5)
        for t in ("search_former", "search2_former"):
            assert s.scalar(f"SELECT COUNT(*) FROM {t} WHERE name='ながいなまえ2'") == 1
        assert search_index.grams("みくちゃ") == "みく くち ちゃ" and search_index.grams("み") == "み"
    g = {x["user_id"]: x for x in explore.search(tmp_path, "なまえ")["gifters"]}
    assert set(g) == {1, 2, 3, 4, 5} and g[1]["name"] == "かわったなまえ"
    assert [x["user_id"] for x in explore.search(tmp_path, "かわった")["gifters"]] == [1]
    g = explore.search(tmp_path, "ながいなまえ1")["gifters"]
    assert [(x["user_id"], x["match"]) for x in g] == [(1, {"kind": "former", "name": "ながいなまえ1"})]
    # 2 文字は 2 文字ずつの索引で、1 文字は索引の中身を全部くらべる
    assert {x["user_id"] for x in explore.search(tmp_path, "ナマ")["gifters"]} == {1, 2, 3, 4, 5}
    assert {x["user_id"] for x in explore.search(tmp_path, "か")["gifters"]} == {1, 2}
    assert [x["user_id"] for x in explore.search(tmp_path, "みじ")["gifters"]] == [2]
    assert [x["match"] for x in explore.search(tmp_path, "え2")["gifters"]] == [{"kind": "former", "name": "ながいなまえ2"}]


def test_room_daily_metrics(tmp_path):
    import json as _json

    from showroom_keeper import live_events

    with Store(tmp_path) as s:
        s.upsert_rooms([{"room_id": 1, "key": "aaa", "name": "A"}], "live", NOW)
        # 日本時間の日の境目をまたがないように、その日の正午を使う
        noon = explore._jst_noon(NOW)
        s.add_room_daily([{"room_id": 1, "followers": 10, "viewers": 50}], noon - 86400)
        s.add_room_daily([{"room_id": 1, "followers": 13, "viewers": 70}], noon)
        s.add_room_lives([{"room_id": 1, "started_at": int(noon - 3600)}], noon - 150)
    (tmp_path / "gifts").mkdir()
    (tmp_path / "cache").mkdir()
    (tmp_path / "cache" / "gift_catalog.json").write_text(_json.dumps({"gifts": {"1": {"point": 100, "free": False}}}))
    (tmp_path / "gifts" / "aaa.jsonl").write_text(_json.dumps({"at": int(noon - 600), "live_id": 1, "user_id": 7, "gift_id": 1, "num": 2}) + "\n")
    live_events.append(tmp_path, "aaa", {"at": int(noon - 500), "live_id": 1, "user_id": 8, "kind": "visit", "n": 1})
    m = explore.room_daily_metrics(tmp_path, "aaa")
    last = m["days"][-1]
    assert m["recorded"] and round(last["hours"], 2) == 1.0 and last["lives"] == 1 and last["follower_gain"] == 3
    assert last["paid_points"] == 200 and last["paid_gifters"] == 1 and last["visits"] == 1 and last["first_visits"] == 1
    assert explore.room_daily_metrics(tmp_path, "zzz") == {"found": False}


def test_gift_daily_is_added_incrementally(tmp_path):
    import json as _json
    from datetime import datetime

    from showroom_keeper.status import JST

    with Store(tmp_path) as s:
        s.upsert_rooms([{"room_id": 1, "key": "aaa", "name": "A"}], "live", NOW)
    (tmp_path / "gifts").mkdir()
    (tmp_path / "cache").mkdir()
    (tmp_path / "cache" / "gift_catalog.json").write_text(_json.dumps({"gifts": {"1": {"point": 100, "free": False},
                                                                                  "2": {"point": 1, "free": True}}}))
    f = tmp_path / "gifts" / "aaa.jsonl"
    t1 = datetime(2026, 10, 17, 23, 59, tzinfo=JST).timestamp()  # 日本時間の日の境目
    t2 = datetime(2026, 10, 18, 0, 1, tzinfo=JST).timestamp()
    f.write_text(_json.dumps({"at": t1, "user_id": 7, "gift_id": 1, "num": 2}) + "\n")
    with Store(tmp_path) as s:
        explore.update_gift_daily(tmp_path, s, "aaa")
        with f.open("a") as w:  # 新しいギフトと、書きかけの行
            w.write(_json.dumps({"at": t2, "user_id": 7, "gift_id": 1, "num": 1}) + "\n" + _json.dumps({"at": t2, "user_id": 8, "gift_id": 2}) + "\n")
            w.write('{"at": ')
        explore.update_gift_daily(tmp_path, s, "aaa")
        explore.update_gift_daily(tmp_path, s, "aaa")  # 2 回目は何も足さない
        rows = {r["day"]: tuple(r)[2:] for r in s.all("SELECT * FROM room_gift_daily WHERE room_key='aaa'")}
        assert rows == {"2026-10-17": (200, 0, 2), "2026-10-18": (100, 1, 2)}
        assert s.scalar("SELECT COUNT(*) FROM room_gift_day_users WHERE room_key='aaa' AND day='2026-10-18'") == 1
        f.write_text(_json.dumps({"at": t2, "user_id": 9, "gift_id": 1, "num": 1}) + "\n")  # 小さくなった: 作り直す
        explore.update_gift_daily(tmp_path, s, "aaa")
        rows = {r["day"]: tuple(r)[2:] for r in s.all("SELECT * FROM room_gift_daily WHERE room_key='aaa'")}
        assert rows == {"2026-10-18": (100, 0, 1)}
    assert explore._jst_day(t1) == "2026-10-17" and explore._jst_day(t2) == "2026-10-18"


def test_adding_gifts_from_two_processes_at_once_does_not_double(tmp_path, monkeypatch):
    """別のプロセスが同じ所から同時に足しても、同じギフトを 2 回足さない (2026-10-21 に Cursor の計測で二重になった)。
    ロックは同じプロセスの中しか守らないので、ここではロックを外し、読んだあと・書く前に、もう 1 つの Store から足す。"""
    import threading
    from types import SimpleNamespace

    db = tmp_path
    (db / "cache").mkdir()
    (db / "cache" / "gift_catalog.json").write_text(json.dumps({"gifts": {"1": {"point": 1, "free": True}}}))
    for k in range(3):
        record_gift(db, "rec_room", 1, {"u": 55 + k, "ac": "名前", "g": 1, "n": 1, "created_at": NOW + k})
    monkeypatch.setattr(explore, "_sender_lock", threading.RLock())
    monkeypatch.setattr(explore, "_event_lock", threading.RLock())
    busy = []

    def loads(line):  # 読んだあと・書く前に、もう 1 つのプロセスが足し終える
        if not busy:
            busy.append(1)
            with Store(db) as other:
                explore.update_gift_senders(db, other, "rec_room")
        return json.loads(line)

    monkeypatch.setattr(explore, "json", SimpleNamespace(loads=loads, dumps=json.dumps))
    with Store(db) as s:
        explore.update_gift_senders(db, s, "rec_room")
        explore.update_gift_senders(db, s, "rec_room")  # 先に足された分は飛ばし、続きが無いので何もしない
        assert s.scalar("SELECT COUNT(*) FROM gift_events") == 3
        assert s.scalar("SELECT SUM(times) FROM gift_senders") == 3
        assert s.scalar("SELECT records FROM gift_room_totals") == 3


def test_gift_room_index_is_built_in_the_background_on_old_databases(tmp_path, monkeypatch):
    """前からある research.db (ギフト 1 件ずつの表に行があり、前の索引だけ) では、起動の時には作らず、裏の仕事で 1 回だけ作り替える。"""
    from showroom_keeper import research_store

    def indexes(s):
        return {r[0] for r in s.all("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='gift_events'")}

    new = {"gift_events_room_at", "gift_events_user_at"}
    with Store(tmp_path) as s:
        assert indexes(s) == new  # 空の表: 初めから作る
        with s.tx() as c:
            c.execute("DROP INDEX gift_events_room_at")
            c.execute("DROP INDEX gift_events_user_at")
            c.execute("CREATE INDEX gift_events_room ON gift_events(room_key, at)")
            c.execute("CREATE INDEX gift_events_user ON gift_events(user_id, at)")
            c.execute("INSERT INTO gift_events(user_id, at, room_key, gift_id, num) VALUES (1, 1, 'k', 1, 1)")
    monkeypatch.setattr(research_store, "_ready", set())
    with Store(tmp_path) as s:
        assert indexes(s) == {"gift_events_room", "gift_events_user"}  # 起動の時には作らない
        explore.ensure_gift_event_indexes(s, holding=lambda: True)  # 視聴を先にする時は作らない
        assert indexes(s) == {"gift_events_room", "gift_events_user"}
        explore.ensure_gift_event_indexes(s)
        assert indexes(s) == new
        explore.ensure_gift_event_indexes(s)  # 2 回目は何もしない


def test_gift_records_keep_the_looks_seen_on_the_stream(tmp_path):
    """ギフトの知らせのアバターの番号 (av)・Class (cl) を記録し、人ごとにいちばん新しい値を残す (配信の画面で同じ名前の人を見分けるため)。"""
    (tmp_path / "cache").mkdir()
    (tmp_path / "cache" / "gift_catalog.json").write_text(json.dumps({"gifts": {"1": {"point": 1, "free": True}}}))
    record_gift(tmp_path, "rec_room", 1, {"u": 55, "ac": "同じ名前", "g": 1, "n": 1, "av": 1001, "cl": 3, "h": 2, "created_at": NOW})
    record_gift(tmp_path, "rec_room", 1, {"u": 56, "ac": "同じ名前", "g": 1, "n": 1, "created_at": NOW})  # 見た目の無い知らせ
    line = json.loads((tmp_path / "gifts" / "rec_room.jsonl").read_text().splitlines()[0])
    assert line["av"] == 1001 and line["cl"] == 3 and line["h"] == 2
    with Store(tmp_path) as s:
        explore.update_gift_senders(tmp_path, s, "rec_room")
        assert s.looks([55, 56]) == {55: {"avatar_id": 1001, "class_level": 3, "at": int(NOW)}}
    record_gift(tmp_path, "rec_room", 1, {"u": 55, "ac": "同じ名前", "g": 1, "n": 1, "av": 1002, "cl": 4, "created_at": NOW + 9})
    record_gift(tmp_path, "rec_room", 1, {"u": 55, "ac": "同じ名前", "g": 1, "n": 1, "av": 999, "cl": 1, "created_at": NOW - 9})  # 古い知らせ
    with Store(tmp_path) as s:
        explore.update_gift_senders(tmp_path, s, "rec_room")
        assert s.looks([55])[55]["avatar_id"] == 1002 and s.looks([55])[55]["class_level"] == 4  # 新しい方が残る
