import json
import time

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
    gs._cuts_cache.clear()
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
        gs._cuts_cache.clear()
        top = gs.ranking(s, 3)["rows"]
        assert [r["rank"] for r in top] == [1, 2, 2]  # 同点は同じ順位 (詳しいページと同じ)
        d_rows = gs.ranking(s, 500, "D")["rows"]
        assert d_rows and all(r["grade"] == "D" for r in d_rows)
        # D は上位 500 人の外にもいる。全体の中から絞るので、上位 3 人に絞っても D の人が出る
        assert gs.ranking(s, 3, "D")["rows"]
        assert d_rows[0]["rank"] == s.scalar("SELECT COUNT(*) FROM gifter_status WHERE overall > ?", (d_rows[0]["overall"],)) + 1
        for g in "SABCD":
            assert all(r["grade"] == g for r in gs.ranking(s, 500, g)["rows"])
