import json
import urllib.error

from showroom_keeper import archive, research, research_store
from showroom_keeper.research_store import Store

NOW = 1_800_000_000.0
END = 1_577_880_000.0  # 2020-01-01 12:00 (UTC)

EVENT_PAGE = """<html><script>var event_id = 26419;</script><h2 class="title">もこもこ冬祭り</h2>
<div class="contentlist-link"><div class="label-ranking listcard-ranking is-rank-1">1</div>
  <a href="/alive_room" data-room-id="100" class="room-url">x</a><h4 class="listcardinfo-main-text">いるルーム</h4></div>
<li><div class="contentlist-link"><div class="is-rank-2"></div><a href="/event/contribution/moko?room_id=200">貢献</a>
  <a href="https://www.showroom-live.com/gone_room" class="room-url">x</a><p class="listcardinfo-main-text">消えた &amp; ルーム</p></li>
</html>"""


def contribution_page(rows):
    body = "".join(f"<tr><td>{r}</td><td>{n}</td><td>{p:,}pt</td></tr>" for r, n, p in rows)
    return f"""<div><a href="/event/contribution/moko?room_id=1" title="Event Contributor Ranking">x</a>
<table><tr><td>1</td><td>レベル</td><td>5pt</td></tr></table><ul><li><td>1</td><td>レベル</td><td>5pt</td></li></ul>
<h3>Event Contributor Ranking(Top 100)</h3><table><tr><th>順位</th></tr>{body}</table>
<table><tr><td>1</td><td>別の表</td><td>9pt</td></tr></table></div>"""


INDEX = "\n".join([
    "20200101000000 https://www.showroom-live.com/event/moko",
    "20200105000000 https://www.showroom-live.com/event/moko",
    "20200102000000 https://www.showroom-live.com/event/contribution/moko?room_id=100",
    "20200102000000 https://www.showroom-live.com/event/contribution/moko?room_id=200",
    "20200102000000 https://www.showroom-live.com/event/search",
    "20200102000000 https://www.showroom-live.com/alive_room",
])


class FakeArchive:
    def __init__(self, pages):
        self.pages = pages
        self.asked = []

    def __call__(self, url):
        self.asked.append(url)
        for key, text in self.pages.items():
            if key in url:
                if isinstance(text, Exception):
                    raise text
                return text
        raise urllib.error.HTTPError(url, 404, "missing", {}, None)


def collector(tmp_path, pages):
    fake = FakeArchive(pages)
    t = research.Throttle(0.0, sleep=lambda s: None)
    return archive.ArchiveCollector(tmp_path, fetch=fake, throttle=t, clock=lambda: NOW), fake


def run(c, n=50):
    for _ in range(n):
        if not c.step():
            return


def test_classify():
    assert archive.classify("https://www.showroom-live.com/event/moko") == ("event", "moko", None)
    assert archive.classify("http://showroom-live.com/event/contribution/moko?room_id=12") == ("contribution", "moko", 12)
    assert archive.classify("https://www.showroom-live.com/event/search?q=1") is None
    assert archive.classify("https://www.showroom-live.com/event/contribution/moko") is None
    assert archive.classify("https://www.showroom-live.com/room/profile?room_id=1") is None


def test_parse_pages():
    ev = archive.parse_event_page(EVENT_PAGE)
    assert ev["event_id"] == 26419 and ev["name"] == "もこもこ冬祭り"
    assert ev["rooms"] == [{"room_id": 100, "rank": 1, "key": "alive_room", "name": "いるルーム"},
                           {"room_id": 200, "rank": 2, "key": "gone_room", "name": "消えた & ルーム"}]
    rows = archive.parse_contribution_page(contribution_page([(1, "あ&amp;い", 12345), (2, "う", 7)]))
    assert rows == [{"rank": 1, "name": "あ&い", "point": 12345}, {"rank": 2, "name": "う", "point": 7}]
    assert archive.parse_contribution_page("<html>エラー</html>") == []


def test_recovers_gone_room_and_former_names(tmp_path):
    with Store(tmp_path) as s:
        s.upsert_rooms([{"room_id": 100, "key": "alive_room", "name": "いるルーム"}], "list", NOW)
        # イベントは、貢献ランキングのページが保存された 2020-01-02 より前に終わっている (途中の保存ではない)
        s.upsert_events([{"event_id": 26419, "key": None, "name": None, "end": END}, {"event_id": 26000, "key": "old", "end": END - 30 * 86400}])
        # 名前で推測する人は、前後 180 日に SHOWROOM の貢献ランキングに出ている
        s.set_contributions(26000, 300, [{"user_id": 3, "name": "ひとりだけの名前", "point": 10, "rank": 1}], NOW - 7200, final=True)
        # 生きているルームは SHOWROOM から取ってある (退会した人は名前が「退会済みユーザー」)
        s.set_contributions(26419, 100, [{"user_id": 1, "name": "退会済みユーザー", "point": 500, "rank": 1},
                                         {"user_id": 2, "name": "今の名前", "point": 300, "rank": 2}], NOW - 3600, final=True)
        s.set_cumulative(300, [{"user_id": 3, "name": "ひとりだけの名前", "point": 10, "visits": None}], NOW)
    c, fake = collector(tmp_path, {
        "showNumPages": "1",
        "cdx/search": INDEX,
        "20200105000000id_/https://www.showroom-live.com/event/moko": EVENT_PAGE,
        "room_id=100": contribution_page([(1, "あぐまる", 500), (2, "前の名前", 300)]),
        "room_id=200": contribution_page([(1, "ひとりだけの名前", 900), (2, "誰か分からない", 50)]),
    })
    run(c)
    c.close()
    with Store(tmp_path) as s:
        pages = {r["page"]: dict(r) for r in s.all("SELECT * FROM archive_pages")}
        assert set(pages) == {"event:moko", "contribution:moko:100", "contribution:moko:200"}
        assert json.loads(pages["event:moko"]["stamps"]) == ["20200101000000", "20200105000000"]
        assert all(p["fetched_at"] for p in pages.values())
        assert s.one("SELECT key FROM events WHERE event_id=26419")["key"] == "moko"
        gone = s.one("SELECT * FROM rooms WHERE room_id=200")
        assert (gone["key"], gone["name"], gone["found_via"]) == ("gone_room", "消えた & ルーム", "archive")
        # 消えたルームの貢献ランキングは SHOWROOM に取りに行かない。生きているルームはいつも通り
        e200 = s.one("SELECT failures, final FROM room_events WHERE room_id=200 AND event_id=26419")
        assert e200["final"] or e200["failures"] == research.ARCHIVE_HOLD
        names = {(r["user_id"], r["name"], r["source"]) for r in s.all("SELECT * FROM user_names")}
        assert (1, "あぐまる", "archive") in names and (2, "前の名前", "archive") in names
        # 消えたルームは、名前が 1 人に決まる人だけ貢献ランキングに足す
        got = {r["user_id"]: r["point"] for r in s.all("SELECT * FROM contributions WHERE room_id=200")}
        assert got == {3: 900}
        # 名前で決めた分には「名前で推測」の印を付ける (SHOWROOM から取った分には付けない)。合計には入れる
        assert {r["room_id"]: r["guessed"] for r in s.all("SELECT room_id, guessed FROM contributions")} == {100: None, 200: 1, 300: None}
        assert s.one("SELECT c_count FROM room_events WHERE room_id=200 AND event_id=26419")[0] is None  # タイプ分けに使わない
        assert s.one("SELECT c_count FROM room_events WHERE room_id=100 AND event_id=26419")[0] == 2
        assert s.one("SELECT events FROM user_room WHERE user_id=3 AND room_id=200")[0] == 900
        st = archive.status(s)
        assert st["rooms"] == 1 and st["names"] == 2 and st["left_named"] == 1
        assert st["events"] == (1, 1) and st["contributions"] == (2, 2) and st["index_done"]
        assert research._user_names(s, research._Recorded(tmp_path), [1])[1] == "退会済みユーザー (前の名前: あぐまる)"
    assert not any("event/search" in u for u in fake.asked)
    d = research.gifter_detail(tmp_path, 3, [])
    assert d["guessed"] == {"events": 1, "points": 900} and sorted(e["guessed"] for e in d["events"]) == [0, 1]
    # 前の版で足した分 (印が無い) にも、起動した時に 1 回だけ印を付ける
    with Store(tmp_path) as s:
        s.conn.execute("UPDATE contributions SET guessed=NULL")
        s.conn.execute("DELETE FROM meta WHERE k='guessed_marked'")
    research_store._ready.clear()
    with Store(tmp_path) as s:
        assert {r["room_id"]: r["guessed"] for r in s.all("SELECT room_id, guessed FROM contributions")} == {100: None, 200: 1, 300: None}


def test_tries_older_snapshot_then_gives_up(tmp_path):
    with Store(tmp_path) as s:
        s.upsert_events([{"event_id": 1, "key": "moko"}])
    c, fake = collector(tmp_path, {
        "showNumPages": "1",
        "cdx/search": "20200101000000 https://www.showroom-live.com/event/moko\n"
                      "20200105000000 https://www.showroom-live.com/event/moko",
        "20200105000000id_": "<html>メンテナンス中</html>",
    })
    run(c)
    c.close()
    asked = [u for u in fake.asked if "id_/" in u]
    assert [u.split("/web/")[1][:14] for u in asked] == ["20200105000000", "20200101000000"]
    with Store(tmp_path) as s:
        p = s.one("SELECT * FROM archive_pages")
        assert p["fetched_at"] and p["rows"] == 0


def test_rename_keeps_old_name(tmp_path):
    with Store(tmp_path) as s:
        s.set_cumulative(1, [{"user_id": 5, "name": "はじめ", "point": 10, "visits": None}], NOW)
        s.set_cumulative(1, [{"user_id": 5, "name": "退会済みユーザー", "point": 10, "visits": None}], NOW + 1)
        assert [r["name"] for r in s.all("SELECT name FROM user_names WHERE user_id=5")] == ["はじめ"]
        assert research._user_names(s, research._Recorded(tmp_path), [5])[5] == "退会済みユーザー (前の名前: はじめ)"


def test_sweep_marks_gone_room(tmp_path):
    from showroom_keeper import sweep

    with Store(tmp_path) as s:
        s.upsert_rooms([{"room_id": 7, "key": "x", "name": "x", "live_at": NOW}], "list", NOW)

    def get(path, **params):
        raise urllib.error.HTTPError(path, 404, "missing", {}, None)

    t = research.Throttle(0.0, sleep=lambda s: None)
    sw = sweep.Sweeper(tmp_path, collector=research.Collector(tmp_path, get=get, throttle=t), scan=False)
    assert sw._next_room(NOW)
    with Store(tmp_path) as s:
        assert s.one("SELECT gone FROM rooms WHERE room_id=7")["gone"] == 1


def test_gone_room_is_not_asked_to_showroom(tmp_path):
    with Store(tmp_path) as s:
        s.upsert_rooms([{"room_id": 100, "key": "alive_room", "name": "いるルーム"}], "list", NOW)
    c, _ = collector(tmp_path, {"showNumPages": "1", "cdx/search": "20200105000000 https://www.showroom-live.com/event/moko",
                                "event/moko": EVENT_PAGE})
    run(c)
    c.close()
    with Store(tmp_path) as s:
        f = {r["room_id"]: r["failures"] for r in s.all("SELECT room_id, failures FROM room_events")}
        assert f == {100: 0, 200: research.ARCHIVE_HOLD}


def test_sweep_releases_hold_when_room_exists(tmp_path):
    from showroom_keeper import sweep

    with Store(tmp_path) as s:
        s.upsert_rooms([{"room_id": 8, "key": "y", "name": "y", "live_at": NOW}], "archive", NOW)
        s.upsert_events([{"event_id": 1, "key": "e", "end": NOW - 86400}])
        s.conn.execute("INSERT INTO room_events(room_id, event_id, failures) VALUES (8, 1, ?)", (research.ARCHIVE_HOLD,))
    c = research.Collector(tmp_path, get=lambda path, **p: {}, throttle=research.Throttle(0.0, sleep=lambda s: None))
    c.refresh_room = lambda room_id: None
    sw = sweep.Sweeper(tmp_path, collector=c, scan=False)
    assert sw._next_room(NOW)
    with Store(tmp_path) as s:
        assert s.one("SELECT failures FROM room_events WHERE room_id=8")["failures"] == 0


def test_guessed_ranking_is_refetched_when_room_exists(tmp_path):
    from showroom_keeper import sweep

    with Store(tmp_path) as s:
        s.upsert_rooms([{"room_id": 8, "key": "y", "name": "y", "live_at": NOW}], "archive", NOW)
        s.upsert_events([{"event_id": 1, "key": "e", "end": NOW - 86400}, {"event_id": 2, "key": "f", "end": NOW - 86400}])
        for eid in (1, 2):
            s.set_contributions(eid, 8, [{"user_id": 5, "point": 100, "rank": 1, "guessed": True}], NOW - 60, final=True)
    answers = {"event_id=1&": {"ranking": [{"user_id": 6, "name": "本物", "point": 120, "rank": 1}]}, "event_id=2&": {"ranking": []}}
    get = lambda path, **p: next((v for k, v in answers.items() if k in path), {})  # noqa: E731
    c = research.Collector(tmp_path, get=get, throttle=research.Throttle(0.0, sleep=lambda s: None))
    c.refresh_room = lambda room_id: None
    sw = sweep.Sweeper(tmp_path, collector=c, scan=False)
    assert sw._next_room(NOW)  # ルームがあると分かったので、名前で推測した分を取り直す
    while sw._next_ranking(NOW):
        pass
    with Store(tmp_path) as s:
        rows = {(r["event_id"], r["user_id"]): r["guessed"] for r in s.all("SELECT * FROM contributions")}
        # 本物が取れたイベントは置き換える。SHOWROOM に残っていなかったイベントは、推測した分を残す
        assert rows == {(1, 6): None, (2, 5): 1}
        assert s.scalar("SELECT COUNT(*) FROM room_events WHERE room_id=8 AND final=1") == 2


def test_refused_connections_do_not_use_up_page_tries(tmp_path):
    with Store(tmp_path) as s:
        s.upsert_events([{"event_id": 26419, "key": "moko"}])
    refused = urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))
    c, fake = collector(tmp_path, {"showNumPages": "1", "cdx/search": INDEX, "event/moko": refused})
    for _ in range(10):  # 断られ続けても
        try:
            c.step()
        except Exception:  # noqa: BLE001
            pass
    with Store(tmp_path) as s:
        p = s.one("SELECT tries, fetched_at FROM archive_pages WHERE page='event:moko'")
    assert p["tries"] == 0 and p["fetched_at"] is None  # 「読み終えた」にしない
    fake.pages["event/moko"] = EVENT_PAGE  # つながるようになったら読める
    run(c)
    with Store(tmp_path) as s:
        assert s.one("SELECT rows FROM archive_pages WHERE page='event:moko'")["rows"] == 2
    c.close()


def test_pages_lost_to_refusals_are_read_again_once(tmp_path):
    with Store(tmp_path) as s:
        s.conn.execute("""INSERT INTO archive_pages(page, kind, event_key, url, stamps, tries, fetched_at, rows)
                          VALUES ('event:lost', 'event', 'lost', 'u', '["20200101000000"]', 3, 1, 0),
                                 ('event:ok', 'event', 'ok', 'u', '["20200101000000"]', 1, 1, 5)""")
        s.set_meta("archive_index_done_at", 1)
    c, _ = collector(tmp_path, {})
    c._requeue_once()
    with Store(tmp_path) as s:
        rows = {r["page"]: (r["tries"], r["fetched_at"]) for r in s.all("SELECT * FROM archive_pages")}
        assert rows == {"event:lost": (0, None), "event:ok": (1, 1)}
        s.conn.execute("UPDATE archive_pages SET tries=3, fetched_at=1, rows=0 WHERE page='event:lost'")
    c._requeue_once()  # 2 回目からは戻さない
    with Store(tmp_path) as s:
        assert s.one("SELECT tries FROM archive_pages WHERE page='event:lost'")[0] == 3
    c.close()


def _page(s, eid, rid, rows, at):
    s.conn.executemany("INSERT INTO archive_contributions(event_id, room_id, rank, name, point, at) VALUES (?, ?, ?, ?, ?, ?)",
                       [(eid, rid, r, n, p, at) for r, n, p in rows])


def test_match_links_only_sure_rows(tmp_path):
    with Store(tmp_path) as s:
        s.upsert_events([{"event_id": 1, "key": "a", "end": END}, {"event_id": 2, "key": "b", "end": END},
                         {"event_id": 3, "key": "c", "end": END}, {"event_id": 9, "key": "z", "end": END - 86400}])
        # SHOWROOM の分: 2 位と 3 位が同点 (同じ順位・ポイントの人が 2 人)
        s.set_contributions(1, 10, [{"user_id": 1, "name": "今1", "point": 500, "rank": 1},
                                    {"user_id": 2, "name": "今2", "point": 300, "rank": 2},
                                    {"user_id": 3, "name": "今3", "point": 300, "rank": 2}], NOW, final=True)
        s.set_contributions(2, 10, [{"user_id": 1, "name": "今1", "point": 500, "rank": 1}], NOW, final=True)
        s.set_contributions(9, 99, [{"user_id": 4, "name": "ひとり", "point": 5, "rank": 1},
                                    {"user_id": 5, "name": "ふたご", "point": 5, "rank": 2}], NOW, final=True)
        _page(s, 1, 10, [(1, "前1", 500), (2, "前2", 300)], END + 60)
        _page(s, 2, 10, [(1, "途中の名前", 500)], END - 3600)  # イベントの途中の保存
        # 消えたルーム: 同じ名前が 2 行 (別の 2 人)・1 人に決まる名前・前後 180 日に出ていない人
        s.set_cumulative(300, [{"user_id": 6, "name": "見えない人", "point": 1, "visits": None}], NOW)
        _page(s, 3, 20, [(1, "ふたご", 90), (2, "ふたご", 80), (3, "ひとり", 70), (4, "見えない人", 60)], END + 60)
    c, _ = collector(tmp_path, {})
    for eid, rid in ((1, 10), (2, 10), (3, 20)):
        c._match(eid, rid)
    c.close()
    with Store(tmp_path) as s:
        links = {(r["event_id"], r["rank"], r["name"]): r["user_id"] for r in s.all("SELECT * FROM archive_contributions")}
        assert links == {(1, 1, "前1"): 1, (1, 2, "前2"): None, (2, 1, "途中の名前"): None,
                         (3, 1, "ふたご"): None, (3, 2, "ふたご"): None, (3, 3, "ひとり"): 4, (3, 4, "見えない人"): None}
        assert {(r["user_id"], r["name"]) for r in s.all("SELECT * FROM user_names WHERE source='archive'")} == {(1, "前1")}
        assert {r["user_id"]: r["point"] for r in s.all("SELECT * FROM contributions WHERE event_id=3")} == {4: 70}
        assert s.scalar("SELECT COUNT(*) FROM archive_contributions WHERE checked IS NULL") == 0


def test_recheck_removes_unsure_links_made_before(tmp_path):
    with Store(tmp_path) as s:
        s.upsert_events([{"event_id": 1, "key": "a", "end": END}, {"event_id": 3, "key": "c", "end": END}])
        s.set_contributions(1, 10, [{"user_id": 1, "name": "今1", "point": 500, "rank": 1}], NOW, final=True)
        # 前の決まりで足した分: 途中の保存から付けた前の名前と、同じ名前が 2 行のページから「名前で推測」した貢献
        _page(s, 1, 10, [(1, "途中の名前", 500)], END - 3600)
        _page(s, 3, 20, [(1, "今1", 90), (2, "今1", 80)], END + 60)
        s.conn.execute("UPDATE archive_contributions SET user_id=1")
        s.conn.execute("INSERT INTO user_names(user_id, name, at, source) VALUES (1, '途中の名前', 0, 'archive'), (1, '本当の前', 0, 'showroom')")
        s.set_contributions(3, 20, [{"user_id": 1, "name": None, "point": 90, "rank": 1, "guessed": True}], NOW, final=True)
        assert s.one("SELECT events FROM user_room WHERE user_id=1 AND room_id=20")[0] == 90
    c, _ = collector(tmp_path, {})
    archive.RECHECK_PAUSE, pause = 0, archive.RECHECK_PAUSE
    try:
        while c.recheck_step():
            pass
    finally:
        archive.RECHECK_PAUSE = pause
    c.close()
    with Store(tmp_path) as s:
        assert s.meta("archive_recheck") == "done"
        assert s.scalar("SELECT COUNT(*) FROM archive_contributions WHERE user_id IS NOT NULL") == 0
        assert {(r["name"], r["source"]) for r in s.all("SELECT * FROM user_names")} == {("本当の前", "showroom")}
        assert s.scalar("SELECT COUNT(*) FROM contributions WHERE event_id=3") == 0
        assert s.one("SELECT events FROM user_room WHERE user_id=1 AND room_id=20")[0] == 0
        assert s.one("SELECT c_count FROM room_events WHERE room_id=20 AND event_id=3")[0] is None
