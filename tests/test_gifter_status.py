import json
import time
import urllib.error

from showroom_keeper import gifter_analysis, gifter_status as gs, research
from showroom_keeper.research_store import Store

DAY = 86400
NOW = time.time()


def _event(s, eid, days_ago, rooms, reward=None):
    """rooms: [(room_id, room_point, [(user_id, point), ...]), ...] (順位はポイントの多い順)。"""
    end = NOW - days_ago * DAY
    s.upsert_events([{"event_id": eid, "name": f"イベント{eid}", "start": end - 7 * DAY, "end": end}])
    s.set_event_rooms(eid, [{"room_id": r, "point": p, "rank": i + 1} for i, (r, p, _) in enumerate(rooms)], NOW)
    for r, _, users in rooms:
        s.set_contributions(eid, r, [{"user_id": u, "name": f"u{u}", "point": p, "rank": k + 1} for k, (u, p) in enumerate(users)],
                            NOW, final=True)
    if reward:
        with s.tx() as c:
            c.execute("INSERT OR REPLACE INTO event_info(event_id, at, reward_rank) VALUES (?, ?, ?)", (eid, NOW, reward))


def _db(tmp_path):
    gifter_analysis._cuts.clear()
    with Store(tmp_path) as s:
        s.set_meta("user_month_upto", True)  # 月の量を、貢献ランキングを入れるたびに足す
        s.upsert_rooms([{"room_id": r, "key": f"k{r}", "name": f"ルーム{r}"} for r in range(1, 5)], "live", NOW)
        # 7 は ルーム1 の推し (接戦で毎回大きく出す)。8 は ルーム2 が推しで、ルーム1 には少しだけ (付き合い投げ)
        for i, days in enumerate((40, 70, 100, 130, 160)):
            _event(s, 10 + i, days, [(1, 100_000, [(7, 60_000), (8, 500), (9, 100)]), (2, 95_000, [(8, 50_000), (9, 200)]),
                                     (3, 20_000, [(9, 300)] + [(1000 + k, 10) for k in range(30)])])
    return tmp_path


def _run_all(d, now=NOW):
    with Store(d) as s:
        for _ in range(50):
            if not gs.step(s, now):
                break


def test_battles_are_close_finishes_at_the_reward_line(tmp_path):
    d = _db(tmp_path)
    with Store(d) as s:
        assert gs.build_rounds(s, NOW) == 5
        r = {(x["room_id"]): dict(x) for x in s.all("SELECT * FROM event_rounds WHERE event_id=10")}
    assert r[1]["battle"] == 1 and r[2]["battle"] == 1  # 100,000 と 95,000: 1 位の境目で差が 1 割以内
    assert r[3]["battle"] == 0 and r[1]["reached"] == 1 and r[2]["reached"] == 0
    assert r[1]["gap"] == 5_000


def test_status_of_a_power_gifter(tmp_path):
    d = _db(tmp_path)
    _run_all(d)
    with Store(d) as s:
        st = gs.read(s, 7)
        other = gs.read(s, 8)
    assert st["found"] and st["fire"] == 60_000 and st["fire_basis"] == "battle"
    assert st["hit"] == 1.0 and st["intel"] == 1.0  # 出した回は、どれもルームが 1 位
    assert st["achievements"] == 5 and st["tops"] == 5 and st["exp"] == 5 and st["level"] == 2
    assert st["rooms"][0]["name"] == "ルーム1" and st["rooms"][0]["kind"] == "推し" and st["rooms"][0]["sortie"] == 1.0
    assert st["rank"] == 1 and st["grade"] is not None
    # 8 は、ルーム2 では毎回 2 位 (届かない勝負) なので、知性・命中が低い。ルーム1 は付き合い
    assert other["intel"] < 0.05 and other["hit"] == 0.0  # ルーム1 への少しだけ (500) は届いた回
    kinds = {x["name"]: (x["kind"], x["class"]) for x in other["rooms"]}
    assert kinds["ルーム1"] == ("付き合い", "付き合い") and kinds["ルーム2"][0] == "推し"


def test_ranking_and_lookup(tmp_path):
    d = _db(tmp_path)
    _run_all(d)
    with Store(d) as s:
        rk = gs.ranking(s)
        got = gs.lookup(s, [7, 8, 12345])
    assert [r["user_id"] for r in rk["rows"]][:1] == [7] and rk["rows"][0]["top_name"] == "ルーム1"
    assert set(got) <= {7, 8, 9} and 7 in got and got[7]["fire"] == 60_000
    top = rk["rows"][0]  # 5 回・5 か月送った人: 回数と最大の回を、見込み量と並べて出す
    assert top["rounds"] == 5 and top["active_months"] == 5 and top["max_out"] == 60_000 and top["reliability"] == "中"


def test_one_big_round_is_marked_as_reference_only():
    """1 回・1 か月だけの人は、その 1 回がそのまま火力・月の量になるので「参考」(見込み量の順位の上に来ても、そうと分かる)。"""
    assert gs.reliability(1, 1) == "参考" and gs.reliability(5, 1) == "参考"
    assert gs.reliability(3, 4) == "低" and gs.reliability(6, 6) == "中" and gs.reliability(12, 8) == "高"
    assert gs.reliability(None, None) is None  # この欄ができる前に作った人


def test_runs_once_a_day_and_drops_people_who_left(tmp_path):
    d = _db(tmp_path)
    _run_all(d)
    with Store(d) as s:
        assert gs.step(s, NOW) is False  # 回し終えた日は、もう何もしない
        assert gs.step(s, NOW + gs.EVERY + 60) is True  # 1 日たったら、また回す
        s.conn.execute("DELETE FROM gifter_status_queue")  # 今回は誰も作らなかったことにする
        gs.step(s, NOW + gs.EVERY + 60)  # 回し終える
        assert s.scalar("SELECT COUNT(*) FROM gifter_status") == 0


def test_gifter_page_carries_the_status(tmp_path):
    d = _db(tmp_path)
    _run_all(d)
    out = research.gifter_detail(d, 7, [])
    assert out["status"]["found"] and out["status"]["fire"] == 60_000
    assert research.gifter_detail(d, 9, [])["status"] is not None


def test_status_carries_the_mind_chart(tmp_path):
    d = _db(tmp_path)
    _run_all(d)
    with Store(d) as s:
        st = gs.read(s, 7)
    mind = st["psyche"]
    assert mind["phase"] and len(mind["money"]) == 24 and mind["outlook"]["room_id"] == 1
    assert mind["outlook"]["room_name"] == "ルーム1"


# ---- 再設計の決定案 ① (2026-10-23)
def test_running_event_counts_toward_spent_but_not_firepower(tmp_path):
    """開催中のイベントの途中の値は、残弾・疲労には入るが、火力などの比べる回には入らない (Codex の再現 #120)。"""
    d = _db(tmp_path)
    with Store(d) as s:
        s.upsert_events([{"event_id": 90, "name": "開催中", "start": NOW - 3 * DAY, "end": NOW + 2 * DAY}])
        s.set_event_rooms(90, [{"room_id": 1, "point": 10_000, "rank": 1}], NOW)
        s.set_contributions(90, 1, [{"user_id": 7, "name": "u7", "point": 5_000, "rank": 1}], NOW, final=False)
        gs.build_rounds(s, NOW)
        st = gs.compute(s, 7, NOW)
    assert st["fire"] == 60_000 and st["max_out"] == 60_000 and st["fire_rounds"] == 5  # 5,000 は火力の回に入らない
    # 最近 90 日は 40・70 日前に終わった回、その前の 90 日は 100・130・160 日前の回。終わっていない回は入らない
    assert st["recent90"] == 120_000 and st["prev90"] == 180_000 and st["max_out_rounds"] == 5
    assert st["recent90_rounds"] == 2 and st["prev90_rounds"] == 3


def test_recent_90_days_leave_out_guessed_and_unfinished_rows(tmp_path):
    """最近 90 日は、取り終えた回 (final) の、名前で推測していない行だけ。その前の 90 日も同じ条件。"""
    d = _db(tmp_path)
    with Store(d) as s:
        s.upsert_events([{"event_id": 91, "name": "推測", "start": NOW - 27 * DAY, "end": NOW - 20 * DAY},
                         {"event_id": 92, "name": "取り終えていない", "start": NOW - 37 * DAY, "end": NOW - 30 * DAY},
                         {"event_id": 93, "name": "前の推測", "start": NOW - 127 * DAY, "end": NOW - 120 * DAY}])
        for eid in (91, 92, 93):
            s.set_event_rooms(eid, [{"room_id": 4, "point": 10_000, "rank": 1}], NOW)
        s.set_contributions(91, 4, [{"user_id": 7, "name": "u7", "point": 7_000, "rank": 1, "guessed": True}], NOW, final=True)
        s.set_contributions(92, 4, [{"user_id": 7, "name": "u7", "point": 8_000, "rank": 1}], NOW, final=False)
        s.set_contributions(93, 4, [{"user_id": 7, "name": "u7", "point": 9_000, "rank": 1, "guessed": True}], NOW, final=True)
        gs.build_rounds(s, NOW)
        st = gs.compute(s, 7, NOW)
    assert st["recent90"] == 120_000 and st["recent90_rounds"] == 2
    assert st["prev90"] == 180_000 and st["prev90_rounds"] == 3


def test_recent_90_days_keep_a_partial_value_left_by_a_404(tmp_path):
    """開催中に 300 pt を取り、終わったあと 404 で final が付いた回: 途中の値のまま残り、最近 90 日に入る。
    だから最近 90 日は「保存済みの貢献 pt」で、最終の確定値とは呼ばない (Codex の受入例)。"""
    d = _db(tmp_path)
    with Store(d) as s:
        s.upsert_events([{"event_id": 94, "name": "途中で 404", "start": NOW - 12 * DAY, "end": NOW - 10 * DAY, "show_ranking": 1}])
        s.set_event_rooms(94, [{"room_id": 4, "point": 10_000, "rank": 1}], NOW - 11 * DAY)
        s.set_contributions(94, 4, [{"user_id": 7, "name": "u7", "point": 300, "rank": 1}], NOW - 11 * DAY, final=False)
    def gone(path):
        raise urllib.error.HTTPError("https://www.showroom-live.com/api", 404, "error", {}, None)

    c = research.Collector(d, get=gone, throttle=research.Throttle(0, sleep=lambda s: None), clock=lambda: NOW)
    assert c.fetch_ranking(94, 4, final=True)
    c.close()
    with Store(d) as s:
        assert s.one("SELECT final, try_status FROM room_events WHERE event_id=94 AND room_id=4")["try_status"] == "404"
        gs.build_rounds(s, NOW)
        st = gs.compute(s, 7, NOW)
    assert st["recent90"] == 120_300 and st["recent90_rounds"] == 3
    assert st["formula"] == gs.FORMULA
    assert st["rooms"][0]["cap"] == 60_000


def test_state_is_unknown_when_fan_lists_are_not_complete(tmp_path):
    """3 か月出していない人は、ファンの名簿が全員分で居ないと分かる時だけ「離脱」。分からなければ「不明」。"""
    d = _db(tmp_path)
    later = NOW + 200 * DAY  # 最後のイベントから 240 日あと (どこにも出していない)
    with Store(d) as s:
        gs.build_rounds(s, later)
        assert gs.compute(s, 7, later)["state"] == "不明"  # ファンの名簿を読んでいない
        months = [gifter_analysis._add(gifter_analysis.last_month(later), -k) for k in range(3)]
        for m in months:  # ルーム1 の名簿を、全員分 (7 は居ない) で、月が終わってから読んだ
            s.set_active_fans(1, m.replace("-", ""), 0, 1, [{"user_id": 555, "level": 3}], later)
        st = gs.compute(s, 7, later, cache={})
    assert st["state"] in ("不明", "離脱")
    # ルーム2・3 (7 が送ったのはルーム1 だけだが、名簿を読む対象は送ったルーム) がすべて全員分なら離脱
    assert st["state"] == "離脱"


def test_ranking_ties_share_a_rank_and_grade_filter_reads_the_whole_table(tmp_path):
    with Store(tmp_path) as s:
        for uid, ov in ((1, 900), (2, 500), (3, 500), (4, 100)) + tuple((100 + k, 10 + k) for k in range(200)):
            s.conn.execute("INSERT INTO gifter_status(user_id, overall, version, data, at) VALUES (?, ?, ?, '{}', ?)",
                           (uid, ov, gs.VERSION, NOW))
        xs = sorted(r[0] for r in s.all("SELECT overall FROM gifter_status"))
        s.set_meta(gs.CUTS_META, json.dumps({"at": NOW, "people": len(xs),
                                             "cuts": {"overall": [xs[min(len(xs) - 1, int(len(xs) * q / 100))] for q in range(1, 100)]}}))
        top = gs.ranking(s, 3)["rows"]
        assert [r["rank"] for r in top] == [1, 2, 2]  # 同点は同じ順位 (詳しいページと同じ)
        d_rows = gs.ranking(s, 500, "D")["rows"]
        assert d_rows and all(r["grade"] == "D" for r in d_rows)
        # D は上位 500 人の外にもいる。全体の中から絞るので、上位 3 人に絞っても D の人が出る
        assert gs.ranking(s, 3, "D")["rows"]
        assert d_rows[0]["rank"] == s.scalar("SELECT COUNT(*) FROM gifter_status WHERE overall > ?", (d_rows[0]["overall"],)) + 1
        for g in "SABCD":
            assert all(r["grade"] == g for r in gs.ranking(s, 500, g)["rows"])


def test_rebuild_switches_everyone_at_once(tmp_path):
    """作り直しの途中は、前の回の値・順位のまま。回し終えた時に、全員・順位・ランクの区切りが一度に入れ替わる (#127)。"""
    d = _db(tmp_path)
    _run_all(d)
    with Store(d) as s:
        before = {r[0]: r[1] for r in s.all("SELECT user_id, overall FROM gifter_status")}
        s.conn.execute("UPDATE gifter_status SET overall = overall + 1, data = json_set(data, '$.formula', 1)")
        old = {r[0]: r[1] for r in s.all("SELECT user_id, overall FROM gifter_status")}
        later = NOW + gs.EVERY + 60
        assert gs.step(s, later, users=1)  # 次の回を始める
        assert gs.step(s, later, users=1)  # 1 人だけ作る
        assert {r[0]: r[1] for r in s.all("SELECT user_id, overall FROM gifter_status")} == old  # 一覧はまだ前の回のまま
        assert s.scalar("SELECT COUNT(*) FROM gifter_status_next") == 1
        for _ in range(50):
            if not gs.step(s, later):
                break
        assert {r[0]: r[1] for r in s.all("SELECT user_id, overall FROM gifter_status")} == before
        assert s.scalar("SELECT COUNT(*) FROM gifter_status_next") == 0
        assert all(json.loads(r[0])["formula"] == gs.FORMULA for r in s.all("SELECT data FROM gifter_status"))


def test_run_started_before_the_switch_starts_over(tmp_path):
    """入れ替えの仕組みより前に始めた途中の回 (途中の人を一覧に直接書いていた) は、はじめからやり直す。"""
    d = _db(tmp_path)
    _run_all(d)
    with Store(d) as s:
        s.set_meta(gs.RUN_META, json.dumps({"started": NOW - 3600, "version": gs.VERSION, "people": 3}))
        s.conn.execute("INSERT INTO gifter_status_queue VALUES (8)")
        n = s.scalar("SELECT COUNT(*) FROM gifter_status")
        assert gs.step(s, NOW)
        run = json.loads(s.meta(gs.RUN_META))
        assert run["staged"] and run["started"] == NOW and s.scalar("SELECT COUNT(*) FROM gifter_status_queue") == run["people"]
        assert s.scalar("SELECT COUNT(*) FROM gifter_status") == n  # やり直している間も、前の一覧は出したまま


def test_reads_stay_on_one_round_while_another_connection_switches(tmp_path):
    """読んでいる途中に別の接続が回し終えて入れ替えても、値・順位・ランクの区切りは同じ回のものを読む (#127)。"""
    d = _db(tmp_path)
    _run_all(d)
    with Store(d) as a, Store(d) as b:
        with gs._snapshot(a):
            before = gs.ranking(a)["rows"]
            cuts = gs._cuts(a)
            b.conn.execute(f"INSERT INTO gifter_status_next({gs.COLS}) SELECT {gs.COLS} FROM gifter_status")  # 次の回を作り終えた
            b.conn.execute("UPDATE gifter_status_next SET overall = overall * 10")
            gs.finish(b, {"started": NOW, "version": gs.VERSION}, NOW + 1)
            assert gs.ranking(a)["rows"] == before and gs._cuts(a) == cuts  # 前の回のまま
        after = gs.ranking(a)["rows"]
    assert [r["overall"] for r in after] == [r["overall"] * 10 for r in before]


def _coverage(s, months, low):
    """集まり具合 (research.coverage の結果) を、months のうち low の月だけ取った割合が低いとして残す。"""
    s.set_meta("coverage_cache", {"at": NOW, "result": {"months": [{"month": m, "ratio": 0.3 if m in low else 1.0} for m in months],
                                                         "read_from": None, "read_done": True}})


def test_months_still_being_collected_do_not_count(tmp_path):
    """集め終わっていない古い月が少なく見えても、それで「上り調子」にしない (直し A)。"""
    gifter_analysis._cuts.clear()
    with Store(tmp_path) as s:
        s.set_meta("user_month_upto", True)
        s.upsert_rooms([{"room_id": r, "key": f"k{r}", "name": f"ルーム{r}"} for r in (1, 2)], "live", NOW)
        last = gifter_analysis.last_month(NOW)
        months = [gifter_analysis._add(last, -k) for k in range(11, -1, -1)]
        for i, m in enumerate(months):  # 毎月 1 回。4〜6 か月前だけ、集め終わっていないので少なく見える
            end = gs._month_start(m) + 15 * DAY
            pt = 5_000 if i in (6, 7, 8) else 50_000
            _event(s, 500 + i, (NOW - end) / DAY, [(1, 100_000, [(7, pt)] + [(1000 + k, 10) for k in range(30)]), (2, 1_000, [(9, 10)])])
        plain = gs.compute(s, 7, NOW)
        _coverage(s, months, set(months[6:9]))
        fixed = gs.compute(s, 7, NOW, cache={})
    gifter_analysis._cuts.clear()
    assert plain["state"] == "上り調子" and plain["momentum"] > 5
    assert fixed["state"] != "上り調子" and fixed["momentum"] is None and fixed["gaps"] == 3
    assert fixed["hp"] == 50_000  # 予算も、少なく見える月を入れない


def test_part_averages_known_months():
    assert gs._part([None, 2.0, 4.0]) == 9.0 and gs._part([None, None]) is None


