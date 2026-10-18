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

from showroom_keeper import explore, research, research_store, room_analysis as ra, sweep
from showroom_keeper.research_store import Store

NOW = time.time()
DAY = 86400
# 大きくなる表 (ルーム × イベント・ギフター × なにか で増えるもの)。room_profile は 1 ルーム 1 行で小さく、
# ルームの一覧で絞り込み・並べ替えのために全部読むので入れない
BIG = {"contributions", "event_progress", "user_room", "cumulative_history", "room_events", "user_month", "cumulative",
       "user_names", "users", "archive_contributions", "room_daily", "room_event_style", "active_fans", "user_rooms"}
# わざと全部読むもの: (読み方の一部, SQL の一部, 理由)
ALLOWED = [
    ("SCAN room_daily", "DELETE FROM room_daily WHERE day <", "古い日の分を消す。1 日 1 回だけ"),
    ("USING INDEX room_events_pending", "WHERE re.final=0", "取っていない分だけの索引を読む (取り終えた分は読まない)"),
    ("SCAN af", "FROM active_fans af LEFT JOIN user_rooms", "プロフィールを読む人を 100 人ずつまとめて選ぶ (100 回に 1 回だけ)"),
    ("SCAN contributions", "MAX(user_id) AS uid FROM contributions", "登録時期の目印。分析のたびに 1 回作って meta に残し、ギフターのページでは使い回す"),
]


@pytest.fixture
def traced(tmp_path, monkeypatch):
    """実際に流れた SQL を拾う。"""
    seen: list[str] = []
    connect = sqlite3.connect

    def tracing(*a, **k):
        c = connect(*a, **k)
        c.set_trace_callback(seen.append)
        return c

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
    seen.clear()
    return tmp_path, seen


def full_scans(state_dir, seen: list[str]) -> list[tuple[str, str]]:
    bad = []
    with Store(state_dir) as s:
        for q in dict.fromkeys(seen):
            if not re.match(r"\s*(SELECT|UPDATE|DELETE|INSERT|WITH)", q, re.I):
                continue
            try:
                plan = [r[3] for r in s.conn.execute("EXPLAIN QUERY PLAN " + q)]
            except sqlite3.Error:
                continue
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
    check(traced, lambda d: explore.event_detail(d, "1", ["k2"]))
    check(traced, lambda d: explore.event_choice(d, ["k2"]))
    check(traced, lambda d: explore.room_profiles(d))


def test_saving_does_not_read_whole_tables(traced):
    def save(d):
        with Store(d) as s:
            s.set_contributions(1, 2, [{"user_id": 9, "point": 5}], NOW, True)
            s.set_event_rooms(1, [{"room_id": 1, "point": 3, "rank": 1}], NOW)
            s.add_progress(1, [{"room_id": 1, "point": 3}], NOW)
            s.add_room_daily([{"room_id": 1, "followers": 11}], NOW + 2 * DAY)
    check(traced, save)


def test_room_analysis_does_not_read_whole_tables(traced):
    def analyze(d):
        with Store(d) as s:
            ra.room_analysis(s, 2, NOW)
            ra.event_styles(s, 1)
            s.set_meta("room_stats_upto", True)
            while ra.step(s, NOW):  # 1 日 1 回の作り直しも、ルームごと・イベントごとに読む
                pass
    check(traced, analyze)


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
