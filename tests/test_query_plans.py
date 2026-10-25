"""よく動く処理が、大きな表を全部読んでいないかを確かめる (問い合わせの読み方 = EXPLAIN QUERY PLAN)。

小さなテスト用のデータでは、表を全部読んでも速いので、ふつうのテストでは気づけない。
サーバーの research.db は数千万行になるので、全部読む処理が数秒ごとに動くと、視聴や収集が遅くなる。
ここでは、実際に流れた SQL をすべて拾い、大きな表を「SCAN」(全部読む) していたら失敗にする。
わざと全部読むもの (1 日 1 回だけ・使い回すもの) は ALLOWED に、理由と一緒に書く。
"""

import re
import sqlite3
import time

import pytest

from showroom_keeper import explore, heat, live_contrib, research, research_store, room_analysis as ra, series, sweep
from showroom_keeper.research_store import Store

NOW = time.time()
DAY = 86400
# 大きくなる表 (ルーム × イベント・ギフター × なにか で増えるもの)。room_profile は 1 ルーム 1 行で小さく、
# ルームの一覧で絞り込み・並べ替えのために全部読むので入れない
BIG = {"contributions", "event_progress", "user_room", "cumulative_history", "room_events", "user_month", "cumulative",
       "user_names", "users", "archive_contributions", "room_daily", "room_event_style", "active_fans", "user_rooms", "room_lives",
       "gift_senders", "gift_events", "event_rounds", "gifter_status", "gifter_status_next", "room_point_12h"}
# わざと全部読むもの: (読み方の一部, SQL の一部, 理由)
ALLOWED = [
    ("SCAN room_daily", "DELETE FROM room_daily WHERE day <", "古い日の分を消す。1 日 1 回だけ"),
    ("USING COVERING INDEX room_events_todo", "WHERE re.final=0", "取っていない分だけの索引を読む (取り終えた分・ポイント 0 の分は読まない)"),
    ("SCAN gift_senders", "first_at, last_at, name, unknown FROM gift_senders",
     "ギフターの一覧・分析で、記録したギフトの全員の合計を 1 回読む (サーバーで 8 万行・8MB。前は記録のファイル全部 66MB を読み直していた)"),
    ("SCAN gift_senders", "INSERT OR REPLACE INTO gift_room_totals",
     "ルームごとの件数の表を、前に足した送り主の表から作る。入れ替えた時に 1 回だけ"),
    ("SCAN af", "FROM active_fans af LEFT JOIN user_rooms", "プロフィールを読む人を 100 人ずつまとめて選ぶ (100 回に 1 回だけ)"),
    ("SCAN gifter_status_next", "SELECT data FROM gifter_status_next", "ステータスの評価の区切りを作る。1 日 1 回、回し終えた時だけ (パワーギフターの分だけ)"),
    ("SCAN gifter_status_next", "SELECT user_id, version", "作り直した全員を一覧に入れ替える。1 日 1 回、回し終えた時だけ"),
]


DML = re.compile(r"\s*(SELECT|UPDATE|DELETE|INSERT|WITH)", re.I)


def _plan(conn: sqlite3.Connection, seen: list, sql: str, params) -> None:
    """流れる直前に、同じ接続で読み方を確かめる (一時表を使う SQL も、その接続なら確かめられる)。"""
    if DML.match(sql):
        try:
            steps = [r[3] for r in sqlite3.Connection.execute(conn, "EXPLAIN QUERY PLAN " + sql, params)]
        except sqlite3.Error:
            return  # 確かめられない SQL は、流しても同じエラーになる
        seen.append((sql, steps))


@pytest.fixture
def traced(tmp_path, monkeypatch):
    """実際に流れた SQL と、その読み方を拾う。

    前は、流れた SQL を後から別の接続で確かめていたので、一時表 (TEMP TABLE) を使う SQL は「表が無い」で黙って飛ばされていた
    (2026-10-19 に Cursor が見つけた)。今は、流れる直前に同じ接続で確かめる。
    """
    seen: list = []
    connect = sqlite3.connect

    class Cursor(sqlite3.Cursor):
        def execute(self, sql, params=()):
            _plan(self.connection, seen, sql, params)
            return super().execute(sql, params)

    class Connection(sqlite3.Connection):
        def execute(self, sql, params=()):
            _plan(self, seen, sql, params)
            return super().execute(sql, params)

        def cursor(self, factory=Cursor):
            return super().cursor(factory)

    def tracing(*a, **k):
        return connect(*a, factory=Connection, **k)

    monkeypatch.setattr(research_store.sqlite3, "connect", tracing)
    with Store(tmp_path) as s:
        s.upsert_rooms([{"room_id": r, "key": f"k{r}", "name": f"ルーム{r}"} for r in range(1, 6)], "archive", NOW)
        for e in range(1, 4):
            s.upsert_events([{"event_id": e, "name": "イベント", "start": NOW - (e * 10 + 7) * DAY, "end": NOW - e * 10 * DAY}])
            s.set_event_rooms(e, [{"room_id": r, "point": 100 * r, "rank": 6 - r} for r in range(1, 6)], NOW)
            for r in range(1, 6):
                s.set_contributions(e, r, [{"user_id": r * 100 + i, "point": 10 - i, "rank": i + 1, "guessed": r == 5}
                                           for i in range(5)], NOW, final=True)
            for k in range(7):
                s.add_progress(e, [{"room_id": r, "point": r * (k + 1)} for r in range(1, 6)], NOW - (e * 10 + 7 - k) * DAY)
        s.add_room_daily([{"room_id": 1, "followers": 10}], NOW)
        s.add_room_lives([{"room_id": r, "started_at": int(NOW - 3600 * r), "followers": 5} for r in range(1, 6)], NOW)
    seen.clear()
    return tmp_path, seen


def full_scans(state_dir, seen: list) -> list[tuple[str, str]]:
    bad = []
    for q, plan in {q: plan for q, plan in seen}.items():
        for step in plan:
            m = re.match(r"SCAN (\w+)", step)
            if not m:
                continue
            name = m.group(1)
            table = name if name in BIG else next((t for t in BIG if re.search(rf"\b{t}\s+(AS\s+)?{name}\b", q)), None)
            if table and not any(a in step and b in q for a, b, _ in ALLOWED):
                bad.append((step, " ".join(q.split())[:200]))
    return bad


def check(traced, fn):
    state_dir, seen = traced
    seen.clear()
    fn(state_dir)
    assert full_scans(state_dir, seen) == []


def test_room_and_event_pages_do_not_read_whole_tables(traced):
    check(traced, lambda d: explore.room_detail(d, "k2"))
    with Store(traced[0]) as s:
        s.set_room_info(2, {"show_rank_subdivided": "B-1", "league_id": 30}, NOW, "202610")
    check(traced, lambda d: explore.room_detail(d, "k2"))
    check(traced, lambda d: explore.event_detail(d, "1", ["k2"]))
    check(traced, lambda d: explore.event_choice(d, ["k2"]))
    check(traced, lambda d: explore.room_profiles(d))
    live_contrib.forget()
    check(traced, lambda d: live_contrib.event_live(d, "1", get=lambda p: {}, now=NOW - 12 * DAY, paused=lambda: False))


def test_saving_does_not_read_whole_tables(traced):
    def save(d):
        with Store(d) as s:
            s.set_contributions(1, 2, [{"user_id": 9, "point": 5}], NOW, True)
            s.set_event_rooms(1, [{"room_id": 1, "point": 3, "rank": 1}], NOW)
            s.add_progress(1, [{"room_id": 1, "point": 3}], NOW)
            s.add_room_daily([{"room_id": 1, "followers": 11}], NOW + 2 * DAY)
    check(traced, save)


def test_changing_the_room_to_look_from_does_not_read_whole_tables(traced):
    """分析の見るルームを切り替えた時は、土台 (全部読む重い集計。1 時間に 1 回) に、そのルームのギフターを索引で当てはめるだけ。"""
    state_dir, seen = traced
    research.forget_analysis()
    research._analysis.clear()
    research.analyze(state_dir, [])  # 土台を作る (ここは全部読む)
    seen.clear()
    research.analyze(state_dir, ["k2"])
    assert seen and full_scans(state_dir, seen) == []


def test_room_analysis_does_not_read_whole_tables(traced):
    def analyze(d):
        with Store(d) as s:
            ra.room_analysis(s, 2, NOW)
            ra.event_styles(s, 1)
            s.set_meta("room_stats_upto", True)
            while ra.step(s, NOW):  # 1 日 1 回の作り直しも、ルームごと・イベントごとに読む
                pass
    check(traced, analyze)


def test_room_analysis_step_reads_only_the_todo_events(traced):
    """収集の仕事のたびに探す「走り方を作っていない・間引いていないイベント」は、まだのイベントだけの索引で探す
    (events は BIG に入れていないので、終わった全部のイベントを順に見ても上のテストでは気づけない。サーバーで 1 回 0.1〜0.15 秒)。"""
    state_dir, seen = traced
    with Store(state_dir) as s:
        s.set_meta("room_stats_upto", True)
        seen.clear()
        ra.step(s, NOW)
        while ra.step(s, NOW):
            pass
    plans = [" ".join(plan) for q, plan in seen if "FROM events WHERE" in q and ("style_at IS NULL" in q or "thinned_at IS NULL" in q)]
    assert plans and all("events_style_todo" in p or "events_thin_todo" in p for p in plans), plans


def test_sweep_jobs_do_not_read_whole_tables(traced):
    def run(d):
        c = research.Collector(d, get=lambda path: {}, throttle=research.Throttle(0.0, sleep=lambda x: None))
        c.refresh_room = lambda room_id: None
        sw = sweep.Sweeper(d, collector=c, scan=False)
        with Store(d) as s:
            s.set_meta("live_at", NOW)
            s.set_meta("events_at", NOW)
        sw._next_room(NOW)  # 名前で推測した分のあるルーム (5) を集め直す時も、表を全部読まない
        sw._next_ranking(NOW)
        sw._next_progress(NOW)
        sw._next_event_rooms(NOW)
        sw._next_fans(NOW)
        sw._next_owner(NOW)
        sw._next_event_page(NOW)
        sw._next_fans_history(NOW)
        sw._next_room_profile(NOW)
        sw._next_fans_closed(NOW)
        c.close()
    check(traced, run)


def test_falling_check_does_not_read_whole_tables(traced):
    def check_falling(d):
        with Store(d) as s:
            out = {"falling": [{"user_id": 101, "recent": 0, "before": 50}], "falling_count": 1}
            research._check_falling(s, out, (NOW - 60 * DAY, NOW - 15 * DAY), (NOW - 15 * DAY, NOW))
    check(traced, check_falling)


def test_gifter_page_does_not_read_whole_tables(traced):
    check(traced, lambda d: research.gifter_detail(d, 101, []))


def test_series_does_not_read_whole_tables(traced):
    with Store(traced[0]) as s:
        for e, name in ((3, "大会 予選"), (2, "大会 準決勝"), (1, "大会 決勝")):  # 3 → 2 → 1 の順に始まる
            s.upsert_events([{"event_id": e, "name": name}])

    def run(d):
        with Store(d) as s:
            while series.step(s, NOW):
                pass
        assert explore.series_detail(d, "3")["stages"] == 3
        explore.series_list(d)
        explore.event_detail(d, "2")
        explore.series_link(d, "1")
        explore.series_link(d, "1", "2")
    check(traced, run)


def test_recorded_types_do_not_read_whole_tables(traced):
    from types import SimpleNamespace

    def run(d):
        with Store(d) as s:
            rooms = research._Rooms(s, d)
            gifts = [(NOW - 3600, 10, "k1"), (NOW - 60, 20, "k2")]
            rec = SimpleNamespace(gifts=SimpleNamespace(room=lambda key: [(at, 1) for at, _, k in gifts if k == key]),
                                  points={(1, "k1"): 10, (1, "k2"): 20}, free={}, by_user={1: {"k1": 10, "k2": 20}})
            assert research._recorded_types(s, rooms, rec, 1, gifts)["gifts"] == 2
    check(traced, run)


def test_recorded_senders_use_the_index(traced):
    def run(d):
        with Store(d) as s:
            with s.tx() as c:
                c.execute("INSERT INTO gift_senders(room_key, user_id, times, last_at, name) VALUES ('k1', 55, 3, 1, '少額さん')")
                c.execute("INSERT INTO gift_sender_names(user_id, name, folded, at) VALUES (55, '少額さん', '少額さん', 1)")
            assert explore._recorded_senders(s, "少額", 55, set(), 20)[0]["times"] == 3
    check(traced, run)


def test_listeners_in_a_period_use_the_gift_events_index(traced):
    """期間を決めたギフターの一覧は、ギフト 1 件ずつの表をルームと時刻の索引で読む。"""
    from showroom_keeper import listing

    def run(d):
        with Store(d) as s:
            with s.tx() as c:
                c.execute("INSERT INTO gift_events(user_id, at, room_key, gift_id, num) VALUES (55, ?, 'k1', 1, 2)", (NOW,))
        listing._period_rows(d, NOW - 7 * DAY)
    check(traced, run)
    # 一時表を使う SQL は、full_scans の別の接続では読み方を確かめられない (表が無いので飛ばされる) ので、ここで確かめる
    with Store(traced[0]) as s:
        for sql in listing.PERIOD_TEMP:
            s.conn.execute(sql)
        plan = [r[3] for r in s.conn.execute("EXPLAIN QUERY PLAN " + listing.PERIOD_SQL, (NOW,))]
    assert not [p for p in plan if p.startswith("SCAN e")], plan
    assert any("COVERING INDEX gift_events_room_at" in p for p in plan), plan  # 表を引き直さない


def test_signup_anchor_does_not_read_whole_contributions(traced):
    def run(d):
        with Store(d) as s:
            s.conn.execute("DELETE FROM event_max_user")
            while s.build_event_max_user(2):
                pass
            assert s.scalar("SELECT COUNT(*) FROM event_max_user WHERE uid IS NOT NULL") == 3
            research._Signup.build(s, research._Recorded(d, s))
    check(traced, run)


def test_search_uses_the_name_index(traced):
    """上の検索欄: 3 文字以上は名前の索引 (search_index) で引き、ギフターの表を全部読まない (前は毎回全部 LIKE でくらべていた)。"""
    from showroom_keeper import search_index

    with Store(traced[0]) as s:
        assert search_index.refresh(s, budget=5) == {"three": True, "two": True}
    traced[1].clear()
    check(traced, lambda d: explore.search(d, "ルーム1"))
    check(traced, lambda d: explore.search(d, "ーム"))  # 2 文字は 2 文字ずつの索引で


def test_listener_lists_read_the_tables_as_intended(traced, monkeypatch):
    """ギフターの一覧 (すべての期間・期間を決めた時) と、ギフターのページの記録したギフト。一時表を使う SQL も確かめる。"""
    from showroom_keeper import listing
    from showroom_keeper.gifts import record_gift

    monkeypatch.setattr(listing, "gift_catalog", lambda state_dir, rooms=None: {1: {"point": 1, "free": True}, 100: {"point": 100, "free": False}})
    for k in range(3):
        record_gift(traced[0], f"k{k + 1}", 1, {"u": 100 + k, "ac": "a", "g": 100, "n": 1, "created_at": NOW - 60 * k})
    check(traced, lambda d: listing.list_listeners(d, days=7))
    check(traced, lambda d: listing.listener_detail(d, 100))
    assert any("FROM gift_events WHERE user_id" in " ".join(q.split()) for q, _ in traced[1])  # ギフターのページは 1 人分を索引で読んだ
    check(traced, lambda d: listing.list_listeners(d))
    assert any("sender_done" in q and "gift_senders" in q for q, _ in traced[1])  # 一時表を使う SQL も確かめた
    check(traced, lambda d: listing.list_listeners(d, room="k1"))  # 1 ルームの一覧は、そのルームの送り主だけを読む
    check(traced, lambda d: listing.list_rooms(d, {}))  # ルームの一覧は、ルームごとの件数の表を読む


def test_feed_does_not_read_whole_tables(traced):
    import json

    from showroom_keeper import feed

    d = traced[0]
    (d / "cache").mkdir(exist_ok=True)
    (d / "cache" / "gift_catalog.json").write_text(json.dumps({"gifts": {"1": {"gift_name": "大", "point": 10000, "free": False}}}))
    with Store(d) as s:
        s.conn.executemany("INSERT INTO gift_events(user_id, at, room_key, gift_id, num) VALUES (?, ?, ?, 1, 1)",
                           [(u, NOW - u * 60, f"k{u % 5 + 1}") for u in range(1, 50)])
        s.conn.executemany("INSERT INTO gift_event_pos(room_key, pos, size, at) VALUES (?, 0, 0, ?)", [(f"k{r}", NOW) for r in range(1, 6)])
        s.set_watch(101, True, "", NOW)
        s.add_progress(1, [{"room_id": 2, "point": 5}], NOW)
        # 開催中のイベント (12 時間ごとの上位 3 ルームの報告を作る)
        s.upsert_events([{"event_id": 9, "name": "開催中", "start": NOW - 5 * DAY, "end": NOW + DAY}])
        s.set_event_rooms(9, [{"room_id": r, "point": r, "rank": 6 - r} for r in range(1, 6)], NOW)
        m = feed._boundaries(NOW - DAY, NOW)[0]
        s.add_progress(9, [{"room_id": r, "point": r} for r in range(1, 6)], m - 60)
        s.add_progress(9, [{"room_id": r, "point": 0} for r in range(1, 6)], m - heat.SLOT)  # 前の区切り
        # 前の区切りの値が無いイベント (その 12 時間のいちばん早い値から数える)
        s.upsert_events([{"event_id": 10, "name": "途中から", "start": NOW - 5 * DAY, "end": NOW + DAY}])
        s.add_progress(10, [{"room_id": 2, "point": 1}], m - 5 * 3600)
        s.add_progress(10, [{"room_id": 2, "point": 3}], m)
        # 12 時間の中で終わったイベント (終わった時の値で数える)
        s.upsert_events([{"event_id": 11, "name": "終わった", "start": NOW - 5 * DAY, "end": m - 3600}])
        s.add_progress(11, [{"room_id": 3, "point": 9}], m - 3600)
    check(traced, lambda d: feed.build(d, ["k2"], now=NOW))
    assert any(x["kind"] == "event_daily" for x in feed.build(d, ["k2"], now=NOW, use_cache=False)["items"])


def test_heatmaps_do_not_read_whole_tables(traced):
    import json

    from showroom_keeper import gifter_status as gs

    """ヒートマップ: 12 時間の区切りを作る時は、その区切りに開いていたイベントの推移を索引で読み、画面は作った表と月の上位を読む。"""
    d = traced[0]
    i = heat.slot_of(NOW) - 2
    t0 = heat.slot_start(i)
    month = time.strftime("%Y-%m", time.gmtime(NOW + 9 * 3600 - 40 * DAY))
    with Store(d) as s:
        s.upsert_events([{"event_id": 9, "name": "開催中", "start": t0 - DAY, "end": NOW + DAY}])
        s.conn.execute("UPDATE events SET progress_at=? WHERE event_id=9", (NOW,))
        s.add_progress(9, [{"room_id": r, "point": r} for r in range(1, 6)], t0 - 600)
        s.add_progress(9, [{"room_id": r, "point": r * 10} for r in range(1, 6)], t0 + 6 * 3600)
        s.conn.execute("INSERT INTO user_month(user_id, month, points) VALUES (101, ?, 5)", (month,))
        s.conn.execute("INSERT INTO gifter_status(user_id, top_room, version, data) VALUES (101, 1, ?, ?)", (gs.VERSION, json.dumps(
            {"state": "安定", "fire": 1000, "hp": 100, "mp": 50, "rooms": [{"room_id": 1, "kind": "推し", "cap": 1000, "sortie": 1.0, "edge": 100}]})))
        s.conn.execute("INSERT INTO room_info(room_id, league_id) VALUES (1, 9)")
        s.set_meta(gs.RUN_META, json.dumps({"done": NOW}))

    def run(d):
        with Store(d) as s:
            assert heat.build_slot(s, i) == 5
            s.set_meta("heat_next", i + 1)
            heat.step(s, NOW)
        assert heat.rooms(d)["groups"]
        heat.rooms(d, span="week")
        assert heat.gifters(d, month)["groups"]
        # 地力・残弾・リーグ (作っておいた room_power を索引で上から読む)
        with Store(d) as s:
            heat.build_power(s, NOW)
        assert heat.rooms(d, size="power", group="league")["groups"]
        assert heat.room_power(d, "1")["found"]
    check(traced, run)


def test_news_does_not_read_whole_tables(traced):
    from showroom_keeper import news

    d = traced[0]
    with Store(d) as s:
        s.add_final(1, [{"room_id": 1, "point": 5, "rank": 1}], NOW - 10 * DAY - 30)
        s.conn.execute("INSERT INTO user_month(user_id, month, points) VALUES (101, ?, 5)", (news._month(NOW),))
    check(traced, lambda d: news.build(d, 31, now=NOW))


def test_room_page_rounds_and_event_series_use_indexes(traced):
    """ルームのページのリス活 (推し・付き合い投げ) と、イベントのページの続きもの探し。
    ディスクの取り合いで遅かった (2026-10-20 に Cursor が py-spy で見た) ので、人ごとの貢献を索引で読むことを確かめる。"""
    from showroom_keeper import event_analysis

    with Store(traced[0]) as s:
        s.conn.execute("UPDATE events SET rooms_at = end + 1")
        s.conn.execute("INSERT INTO active_fans(room_id, ym, user_id, level, title_id, at) VALUES (2, '202601', 201, 5, 1, 0)")
    ra._oshi_cache.clear()
    event_analysis._series.clear()

    def run(d):
        with Store(d) as s:
            assert ra.listener_rounds(s, 2)["events"]
            event_analysis.similar_events(s, 1)
    check(traced, run)
    plans = [" ".join(plan) for q, plan in traced[1] if "contributions" in q and "user_id=?" in q]
    assert plans and all("contributions_user_pt" in p or "PRIMARY KEY" in p for p in plans), plans


def test_watch_list_reads_only_the_watched_gifters(traced):
    """気になるギフターの一覧は、気になる人の分だけを送り主の表から読む (前は全員の分を読んでいた。1 回 平均 28 秒)。"""
    with Store(traced[0]) as s:
        with s.tx() as c:
            c.execute("INSERT INTO gift_senders(room_key, user_id, times, last_at, name, points) VALUES ('k1', 101, 3, 1, 'a', 30)")
            c.execute("INSERT INTO gift_senders(room_key, user_id, times, last_at, name, points) VALUES ('k1', 999, 3, 1, 'b', 30)")
        s.set_watch(101, True, "", NOW)
    check(traced, lambda d: research.watch_list(d, ["k2"]))
    assert not [p for q, plan in traced[1] for p in plan if p.startswith("SCAN gift_senders") and "gift_room_totals" not in q]


def test_room_parts_do_not_read_whole_tables(traced, monkeypatch):
    """ルームのページの重い部品 (リス活・重なるルーム・ギフターごとのルーム数) を作る・読む・作り直す所。"""
    from showroom_keeper import room_parts

    monkeypatch.setattr(room_parts, "SAVE", True)
    monkeypatch.setattr(room_parts, "WAIT", True)
    check(traced, lambda d: explore.room_detail(d, "k2"))  # 作って保存する
    check(traced, lambda d: explore.room_detail(d, "k2"))  # 作っておいた分を読む

    def step(d):
        with Store(d) as s:
            room_parts.step(s, NOW + room_parts.EVERY + 60)
    check(traced, step)


def test_gifter_status_does_not_read_whole_tables(traced):
    """ギフターのステータスを作る (1 日 1 回、少しずつ)・ページで読む・戦力ランキング・一覧の列。"""
    from showroom_keeper import gifter_analysis, gifter_status

    gifter_analysis._cuts.clear()
    with Store(traced[0]) as s:
        s.set_meta("user_month_upto", True)
        s.set_contributions(1, 2, [{"user_id": 201, "point": 50}], NOW, True)  # 月の量を入れる

    def run(d):
        with Store(d) as s:
            for _ in range(20):
                if not gifter_status.step(s, NOW):
                    break
    check(traced, run)

    def read(d):
        with Store(d) as s:
            gifter_status.read(s, 201)
            gifter_status.ranking(s)
            for g in ("S", "B", "D"):  # ランクで絞る時は、境目の総合力で索引を読む
                gifter_status.ranking(s, 500, g)
            gifter_status.lookup(s, [201, 101])
    check(traced, read)


def test_next_ranking_is_chosen_from_the_index_only(tmp_path):
    """次に取る貢献ランキングを選ぶ SQL は、表を引き直さずに索引 (room_events_todo) だけで選ぶ。

    前は、ポイント 0 で取らないルームも索引に入っていて、その 1 行ずつ表を探しに行っていた (サーバーで平均 5.5 秒・最大 15 秒)。
    """
    with Store(tmp_path) as s:
        s.upsert_events([{"event_id": e, "name": f"e{e}", "start": NOW - 9 * DAY, "end": NOW - 5 * DAY} for e in range(1, 4)])
        s.link((r, e) for r in range(1, 4) for e in range(1, 4))
        s.conn.execute("UPDATE room_events SET point = room_id - 1")  # ルーム 1 はポイント 0 (取らない)
        args = (NOW - 3600, research.RANKING_RETRIES)
        for sql in (sweep.NEXT_RANKING_SQL + " LIMIT 100",
                    f"SELECT COUNT(*) FROM room_events re JOIN events e USING(event_id) WHERE {research.PENDING_RANKING}"):
            plan = " ".join(r[3] for r in s.conn.execute("EXPLAIN QUERY PLAN " + sql, args))
            assert "COVERING INDEX room_events_todo" in plan, plan
        assert s.scalar("SELECT COUNT(*) FROM room_events INDEXED BY room_events_todo WHERE final=0 AND (point IS NULL OR point > 0)") == 6
        assert s.one("SELECT 1 FROM sqlite_master WHERE name='room_events_pending'") is None


def test_next_ranking_reads_newest_events_first_whatever_the_statistics(tmp_path):
    """次に取る貢献ランキングは、イベントを終わった順の索引で新しい方から読む (並べ替えのために全部読まない)。

    サーバーの統計 (sqlite_stat1) だと、取っていない分を全部読んでから並べ替える読み方が選ばれていた (平均 5.3 秒)。
    統計がそう見えても、読む順番が変わらないことを確かめる。
    """
    with Store(tmp_path) as s:
        s.upsert_events([{"event_id": e, "name": f"e{e}", "start": NOW - 9 * DAY, "end": NOW - 5 * DAY - e} for e in range(1, 4)])
        s.link((r, e) for r in range(1, 4) for e in range(1, 4))
        s.conn.execute("ANALYZE")
        s.conn.execute("DELETE FROM sqlite_stat1 WHERE idx IN ('events_end', 'room_events_todo')")
        s.conn.execute("""INSERT INTO sqlite_stat1(tbl, idx, stat) VALUES ('events', 'events_end', '30000 30000'),
                          ('room_events', 'room_events_todo', '1000 1 1 1 1 1 1')""")
    with Store(tmp_path) as s:  # 統計は開き直した接続から使われる
        args = (NOW - 3600, research.RANKING_RETRIES)
        plan = " ".join(r[3] for r in s.conn.execute("EXPLAIN QUERY PLAN " + sweep.NEXT_RANKING_SQL + " LIMIT 100", args))
        assert "USING INDEX events_end" in plan and "INDEX room_events_todo" in plan and "TEMP B-TREE" not in plan, plan
        assert [(r["room_id"], r["event_id"]) for r in s.all(sweep.NEXT_RANKING_SQL, args)][:3] == [(1, 1), (2, 1), (3, 1)]


def test_event_backtest_reads_rooms_by_event_and_by_room(traced):
    from datetime import datetime

    from showroom_keeper import backtest_event
    from showroom_keeper.status import JST
    month = datetime.fromtimestamp(NOW - 17 * DAY, JST).strftime("%Y-%m")  # イベント 1 の始まった月
    state_dir, seen = traced
    seen.clear()
    out = backtest_event.run(state_dir, [month], [])
    assert out["periods"]["dev"]["all"]["label_candidates"] and any("room_events r" in q for q, _ in seen)  # 本当に読んでいる
    assert full_scans(state_dir, seen) == []
