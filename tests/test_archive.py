import json
import urllib.error

from showroom_keeper import archive, research
from showroom_keeper.research_store import Store

NOW = 1_800_000_000.0

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
        s.upsert_events([{"event_id": 26419, "key": None, "name": None}])
        # 生きているルームは SHOWROOM から取ってある (退会した人は名前が「退会済みユーザー」)
        s.set_contributions(26419, 100, [{"user_id": 1, "name": "退会済みユーザー", "point": 500, "rank": 1},
                                         {"user_id": 2, "name": "今の名前", "point": 300, "rank": 2}], NOW, final=True)
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
        st = archive.status(s)
        assert st["rooms"] == 1 and st["names"] == 2 and st["left_named"] == 1
        assert st["events"] == (1, 1) and st["contributions"] == (2, 2) and st["index_done"]
        assert research._user_names(s, research._Recorded(tmp_path), [1])[1] == "退会済みユーザー (前の名前: あぐまる)"
    assert not any("event/search" in u for u in fake.asked)


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
