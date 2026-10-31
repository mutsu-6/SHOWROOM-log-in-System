import logging
import os
import sqlite3
import threading
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from showroom_keeper import listing, memory, research, selfcheck, sweep
from showroom_keeper.research_store import Store, db_path

NOW = time.time()


def make_keeper(tmp_path: Path, **cfg) -> SimpleNamespace:
    base = {"state_file": tmp_path / "storage_state.json", "research_all": False, "research_scan": False,
            "research_ranking_share": 0.7, "research_archive": False, "research_archive_gap": 6.0}
    return SimpleNamespace(cfg=SimpleNamespace(**{**base, **cfg}), watchers=[])


@pytest.fixture
def checker(tmp_path, monkeypatch):
    monkeypatch.setattr(selfcheck, "ERRORS", selfcheck.ErrorLog())
    return selfcheck.Checker(make_keeper(tmp_path), clock=lambda: NOW)


def test_errors_are_grouped_by_message_and_place():
    errors = selfcheck.ErrorLog()
    logger = logging.getLogger("showroom_keeper.test")
    logger.addHandler(errors)
    try:
        for i in range(3):
            logger.warning("失敗しました (%s)", i)
        try:
            raise ValueError("こわれた")
        except ValueError:
            logger.exception("計算に失敗")
    finally:
        logger.removeHandler(errors)
    rows = errors.summary(time.time())
    assert rows[0]["level"] == "error" and rows[0]["exception"] == "ValueError"
    assert rows[0]["where"].startswith("test_selfcheck.py:")
    warn = rows[1]
    assert warn["day"] == 3 and warn["sample"] == "失敗しました (2)"


def test_error_findings(checker):
    selfcheck.ERRORS.emit(logging.LogRecord("x", logging.ERROR, __file__, 1, "ブラウザで障害", (), None))
    for _ in range(selfcheck.REPEAT_WARN):
        selfcheck.ERRORS.emit(logging.LogRecord("x", logging.WARNING, __file__, 2, "同じ注意", (), None))
    found = []
    checker._errors(time.time(), found)
    assert [f["level"] for f in found] == ["error", "warn"]


def test_dead_thread_is_logged(monkeypatch):
    errors = selfcheck.ErrorLog()
    logging.getLogger().addHandler(errors)
    monkeypatch.setattr(threading, "excepthook", selfcheck._thread_died)
    try:
        t = threading.Thread(target=lambda: 1 / 0, name="こわれる")
        t.start()
        t.join()
    finally:
        logging.getLogger().removeHandler(errors)
    rows = errors.summary(time.time())
    assert rows and rows[0]["level"] == "error" and "こわれる" in rows[0]["sample"]


def test_prune_expired_keeps_fresh_entries(monkeypatch):
    from showroom_keeper import event_analysis as ea, explore, feed, gifter_analysis as ga, news, room_analysis as ra

    # ほかのテストが残した使い回しを数えない (決まった時刻で作った分が、日がたつと期限切れになって数に入っていた)
    for mod, name in ((ea, "_series"), (ea, "_choice"), (ea, "_strengths"), (ea, "_lag"), (ga, "_cuts"), (ra, "_congestion"),
                      (explore, "_folded_rooms"), (feed, "_cache"), (news, "_cache"), (listing, "_user_cache")):
        monkeypatch.setattr(mod, name, {})
    monkeypatch.setattr(listing, "_search_cache", {"old": (NOW - listing.SEARCH_TTL - 1, []), "new": (NOW, [])})
    monkeypatch.setattr(listing, "_listeners_cache", {("a", None, None): (NOW - listing.LISTENERS_STALE - 1, {}),
                                                      ("a", 7, None): (NOW, {})})
    assert selfcheck.prune_expired(NOW) == 2
    assert list(listing._search_cache) == ["new"]
    assert list(listing._listeners_cache) == [("a", 7, None)]


def test_shrink_caches_strong_keeps_what_is_opened(tmp_path, monkeypatch):
    monkeypatch.setattr(listing, "_listeners_cache", {(str(tmp_path), None, None): (NOW, {}), (str(tmp_path), 7, None): (NOW, {})})
    monkeypatch.setattr(listing, "_file_cache", {Path(f"f{i}"): (0, 0, []) for i in range(4)})
    monkeypatch.setattr(research, "_bases", {"x": [100.0, {}]})
    monkeypatch.setattr(research, "_last_view", {"x": ([], 50, "last")})
    monkeypatch.setattr(research, "_analysis", {"last": (100.0, {}), "other": (100.0, {}), "old": (50.0, {})})
    assert selfcheck.shrink_caches(tmp_path, strong=False) == 1  # 前の土台から作った結果だけ
    assert set(research._analysis) == {"last", "other"}
    selfcheck.shrink_caches(tmp_path, strong=True)
    assert set(research._analysis) == {"last"}
    assert list(listing._listeners_cache) == [(str(tmp_path), None, None)]
    assert list(listing._file_cache) == [Path("f2"), Path("f3")]  # 最近使った方を残す


def test_memory_frees_room_for_waiting_viewer(checker, monkeypatch):
    checker.keeper.watchers = [SimpleNamespace(key="room_a", waiting_memory=True)]
    monkeypatch.setattr(selfcheck, "_proc_mb", lambda: (300.0, 0.0))
    shrunk = []
    monkeypatch.setattr(selfcheck, "shrink_caches", lambda sd, strong: shrunk.append(strong) or 3)
    found = []
    checker._memory(NOW, found)
    checker._memory(NOW, found)
    assert shrunk == [False, True]  # 2 回続けて開けなければ、強く片付ける
    assert "視聴のページを 1 ルーム" in found[0]["title"]
    assert checker.fixes and "視聴のページを開けるように" in checker.fixes[0]["what"]


def test_memory_quiet_when_fine(checker, monkeypatch):
    monkeypatch.setattr(selfcheck, "_proc_mb", lambda: (300.0, 10.0))
    found = []
    checker._memory(NOW, found)
    assert found == [] and checker.fixes == []


def test_wal_is_shrunk(checker, tmp_path, monkeypatch):
    monkeypatch.setattr(selfcheck, "WAL_WARN", 1024)
    con = sqlite3.connect(str(db_path(tmp_path)))
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA wal_autocheckpoint=0")
    con.execute("CREATE TABLE t (x TEXT)")
    con.executemany("INSERT INTO t VALUES (?)", [("x" * 1000,) for _ in range(200)])
    con.commit()
    wal = Path(str(db_path(tmp_path)) + "-wal")
    assert wal.stat().st_size > 1024
    found = []
    checker._wal(NOW, found)
    assert wal.stat().st_size == 0 and found == []
    con.close()
    assert "research.db-wal" in checker.fixes[0]["what"]


class FakeWorker:
    def __init__(self, running=False, stopped=False):
        self.running = running
        self._stop = threading.Event()
        if stopped:
            self._stop.set()


def test_dead_sweep_is_restarted(tmp_path, monkeypatch):
    c = selfcheck.Checker(make_keeper(tmp_path, research_all=True), clock=lambda: NOW)
    started = []
    monkeypatch.setattr(sweep, "SWEEPER", FakeWorker())
    monkeypatch.setattr(sweep, "start", lambda *a, **k: started.append(1))
    found = []
    c._workers(NOW, found)
    assert started == []  # 1 回目は起動の途中かもしれないので待つ
    c._workers(NOW, found)
    assert started == [1] and "全ルームの収集" in c.fixes[0]["what"]
    c.restarts = [NOW] * selfcheck.RESTART_LIMIT
    c._workers(NOW, found)
    c._workers(NOW, found)
    assert started == [1] and found[-1]["level"] == "error"


def test_stopped_sweep_is_left_alone(tmp_path, monkeypatch):
    c = selfcheck.Checker(make_keeper(tmp_path, research_all=True), clock=lambda: NOW)
    monkeypatch.setattr(sweep, "SWEEPER", FakeWorker(stopped=True))
    monkeypatch.setattr(sweep, "start", lambda *a, **k: pytest.fail("動かし直さない"))
    for _ in range(3):
        c._workers(NOW, [])


def test_long_heavy_work_is_reported(checker, monkeypatch):
    monkeypatch.setattr(memory, "_holder", {"ident": -1, "name": "analysis", "since": NOW - selfcheck.HEAVY_WARN - 60})
    found = []
    checker._workers(NOW, found)
    assert "analysis" in found[0]["title"]


def test_heavy_holder():
    assert memory.heavy_holder() == {}
    with memory.heavy():
        with memory.heavy():
            held = memory.heavy_holder()
        assert memory.heavy_holder() == held  # 内側が終わっても、外側の始まりのまま
    assert memory.heavy_holder() == {}


def test_freshness(checker, tmp_path, monkeypatch):
    with Store(tmp_path) as s:
        s.set_meta("live_at", NOW - selfcheck.LIVE_STALE - 60)
        s.conn.execute("INSERT INTO events(event_id, start, end, progress_at) VALUES (1, ?, ?, ?)",
                       (NOW - 86400, NOW + 86400, NOW - selfcheck.PROGRESS_STALE - 60))
        s.conn.execute("INSERT INTO events(event_id, start, end, progress_at) VALUES (2, ?, ?, ?)",
                       (NOW - 86400, NOW + 86400, NOW - 600))
    sw = SimpleNamespace(running=True, disk_low=None, started_at=NOW - 86400)
    monkeypatch.setattr(sweep, "SWEEPER", sw)
    found = []
    checker._freshness(NOW, found)
    assert len(found) == 2 and "1 件" in found[1]["title"]
    sw.started_at = NOW - 60  # 起動し直した直後は数えない
    found = []
    checker._freshness(NOW, found)
    assert found == []


def test_leftovers_are_removed(checker, tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    cache.mkdir()
    old = NOW - 2 * 86400
    files = {name: cache / name for name in ("analysis-base-1.db", "analysis-base-2.db", "analysis-base-3.db", "a.tmp")}
    for p in files.values():
        p.write_bytes(b"x")
        os.utime(p, (old, old))
    os.utime(files["analysis-base-3.db"], (NOW, NOW))  # 数えている途中
    (tmp_path / "b.tmp").write_bytes(b"x")  # 新しい .tmp は書いている途中かもしれない
    monkeypatch.setattr(research, "_bases", {str(tmp_path.resolve()): [NOW, {"db": "analysis-base-2.db"}]})
    checker._leftovers(NOW, [])
    assert sorted(p.name for p in cache.iterdir()) == ["analysis-base-2.db", "analysis-base-3.db"]
    assert (tmp_path / "b.tmp").exists()
    assert "2 個" in checker.fixes[0]["what"]


def test_run_once_saves_and_reloads(checker, tmp_path, monkeypatch):
    monkeypatch.setattr(selfcheck, "_proc_mb", lambda: (None, None))
    checker._fixed("ためし", NOW)
    checker.run_once()
    again = selfcheck.Checker(checker.keeper, clock=lambda: NOW)
    assert again.fixes[0]["what"] == "ためし"
    assert checker.view()["at"] == NOW


def test_one_failing_check_does_not_stop_others(checker, monkeypatch):
    monkeypatch.setattr(selfcheck.Checker, "_wal", lambda self, now, found: 1 / 0)
    monkeypatch.setattr(selfcheck, "_proc_mb", lambda: (None, None))
    found = checker.run_once()
    assert any("自己点検の一部" in f["title"] for f in found)


# ---------------------------------------------------------------- 重い処理
@pytest.fixture
def slow(monkeypatch):
    log = selfcheck.SlowLog()
    monkeypatch.setattr(selfcheck, "SLOW", log)
    monkeypatch.setattr(memory, "_hold_until", 0.0)
    monkeypatch.setattr(memory, "runs", __import__("collections").deque(maxlen=50))
    return log


def test_slow_page_sql_and_heavy_are_reported(checker, slow, monkeypatch):
    monkeypatch.setattr(checker, "_cpu_share", lambda now: (None, None))
    selfcheck.note_page("/api/listeners", 12.0)
    selfcheck.note_page("/api/overview", 0.1)  # 速いものは数えない
    selfcheck._note_sql("SELECT * FROM   contributions WHERE x", 7.0)
    memory.runs.append({"name": "analysis", "at": time.time(), "seconds": 900.0})
    found = []
    checker._heavy_work(time.time(), found)
    titles = " / ".join(f["title"] for f in found)
    assert "管理画面が遅くなっています: /api/listeners" in titles
    assert "SQL の読み込みが遅くなっています: SELECT * FROM contributions" in titles
    assert "重い集計が遅くなっています: analysis" in titles
    assert len(slow.summary(time.time())) == 3
    checker._heavy_work(time.time(), [])
    assert slow.summary(time.time())[0]["count"] == 1  # 同じ集計を 2 回数えない


def test_slow_sql_hook(tmp_path, slow, monkeypatch):
    from showroom_keeper import research_store

    monkeypatch.setattr(research_store, "SLOW_SQL", 0.0)
    monkeypatch.setattr(research_store, "slow_hooks", [selfcheck._note_sql])
    with Store(tmp_path) as s:
        s.scalar("SELECT COUNT(*) FROM events")
    assert "SELECT COUNT(*) FROM events" in [r["name"] for r in slow.summary(time.time(), limit=50)]


def test_loop_lag_holds_off_background_recounts(checker, slow, monkeypatch):
    monkeypatch.setattr(checker, "_cpu_share", lambda now: (None, None))
    slow.note("loop", "視聴の処理", 8.0)
    found = []
    checker._heavy_work(time.time(), found)
    assert "視聴の処理が最大 8 秒" in found[0]["title"]
    assert memory.holding() > 0 and memory.tight()
    assert "後回し" in checker.fixes[0]["what"]
    n = len(checker.fixes)
    checker._heavy_work(time.time(), [])
    assert len(checker.fixes) == n  # 続けては後回しにしない


def test_iowait_and_cpu(checker, slow, monkeypatch):
    monkeypatch.setattr(checker, "_cpu_share", lambda now: (0.6, 0.95))
    found = []
    checker._heavy_work(time.time(), found)
    assert any("ディスク待ちが 60%" in f["title"] for f in found)
    assert any("CPU" in f["title"] for f in found)
    assert memory.holding() > 0


def test_cpu_share_reads_proc(checker):
    if not Path("/proc/stat").exists():
        pytest.skip("Linux だけ")
    assert checker._cpu_share(time.time() - 1) == (None, None)  # 1 回目は比べるものが無い
    io, cpu = checker._cpu_share(time.time())
    assert io is None or 0 <= io <= 1


def test_watch_loop_notes_lag(slow):
    import asyncio

    async def run():
        task = asyncio.ensure_future(selfcheck.watch_loop(every=0.01))
        await asyncio.sleep(0.02)
        time.sleep(selfcheck.LOOP_LAG_NOTE + 0.05)  # 視聴の処理を止めてしまう重い処理
        await asyncio.sleep(0.05)
        task.cancel()

    loop = asyncio.new_event_loop()  # asyncio.run は、Python 3.9 でほかのテストの今のループを消してしまうので使わない
    try:
        loop.run_until_complete(run())
    finally:
        loop.close()
    assert slow.summary(time.time())[0]["kind"] == "loop"


def test_stall_is_blamed_on_the_busy_thread(slow, monkeypatch):
    """視聴の処理が待っているだけの時は、その時に計算していたほかのスレッドのせいにする。"""
    import asyncio

    monkeypatch.setattr(selfcheck, "STALL_SAMPLE", 0.3)
    monkeypatch.setattr(selfcheck, "LOOP_LAG_NOTE", 0.3)

    def busy():  # 視聴の処理の中で止まる重い処理 (このテストは showroom_keeper の外なので、場所は分からない)
        time.sleep(0.8)

    async def run():
        task = asyncio.ensure_future(selfcheck.watch_loop(every=0.05))
        await asyncio.sleep(0.1)
        busy()
        await asyncio.sleep(0.1)
        task.cancel()
        await asyncio.sleep(0)

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(run())
    finally:
        loop.close()
    rows = slow.summary(time.time())
    assert rows and rows[0]["kind"] == "loop"
    assert rows[0]["name"].startswith("ほかの処理に待たされた")  # 止まっている最中に見て、原因を付けた
    assert selfcheck._beat["at"] == 0.0  # 終わったら見張りも止まる


def test_blame_names_gc_and_loop_code(monkeypatch):
    monkeypatch.setitem(selfcheck._gc, "started", time.monotonic())
    assert "GC" in selfcheck.blame_stall()
    monkeypatch.setitem(selfcheck._gc, "started", None)
    from showroom_keeper import memory as m

    frame = sys._getframe()
    place = selfcheck._frame_place(frame)
    assert place is None  # テストの中はこのプログラムの外
    code = compile("import sys\nf = sys._getframe()", m.__file__, "exec")  # showroom_keeper の中の行として見る
    ns = {}
    exec(code, ns)
    assert selfcheck._frame_place(ns["f"]).startswith("memory.py:2")


def test_gc_timer_notes_long_full_collections(slow, monkeypatch):
    selfcheck._gc_timer("start", {"generation": 1})
    assert selfcheck._gc["started"] is None  # 若い世代は測らない
    selfcheck._gc_timer("start", {"generation": 2})
    selfcheck._gc["started"] -= 3  # 3 秒かかったことにする
    selfcheck._gc_timer("stop", {"generation": 2})
    assert selfcheck._gc["started"] is None
    rows = slow.summary(time.time())
    assert rows[0]["kind"] == "gc" and rows[0]["max"] >= 3


def test_short_idle_lags_are_kept_but_not_warned(checker, slow, monkeypatch):
    """何もしていない時の 2〜4.5 秒の待ちは、表には残すが知らせない (2026-10-20)。"""
    monkeypatch.setattr(checker, "_cpu_share", lambda now: (None, None))
    for lag in (2.1, 3.0, 4.5):
        slow.note("loop", "ほかの処理に待たされた: sweep x.py:1 (f)", lag)
    found = []
    checker._heavy_work(time.time(), found)
    assert not any("視聴の処理" in f["title"] for f in found)
    assert memory.holding() == 0
    slow.note("loop", "視聴の処理の中: web.py:9 (g)", 6.0)
    found = []
    checker._heavy_work(time.time(), found)
    hit = [f for f in found if "視聴の処理" in f["title"]]
    assert hit and "web.py:9" in hit[0]["detail"] and "4 回" in hit[0]["detail"]


def test_heavy_runs_are_kept(monkeypatch):
    monkeypatch.setattr(memory, "runs", __import__("collections").deque(maxlen=50))
    with memory.heavy():
        pass
    assert len(memory.runs) == 1 and memory.runs[0]["seconds"] >= 0


def test_long_step_during_showroom_pause_is_not_a_bug(checker, slow, monkeypatch):
    from showroom_keeper import research

    monkeypatch.setattr(checker, "_cpu_share", lambda now: (None, None))
    monkeypatch.setattr(sweep, "state", lambda: {"busy_seconds": 3 * 3600, "doing": "貢献ランキング"})
    th = research.Throttle(0.5)
    monkeypatch.setattr(research, "THROTTLE", th)
    found = []
    checker._heavy_work(time.time(), found)
    assert any("3 時間" in f["title"] or "180 分" in f["title"] for f in found)
    th.paused_until = th._clock() + 3600  # 止められて休んでいる
    found = []
    checker._heavy_work(time.time(), found)
    assert found == []


def test_hold_does_not_stop_a_started_analysis(monkeypatch):
    monkeypatch.setattr(memory, "_hold_until", 0.0)
    memory.hold_off(60)
    assert memory.tight()
    seen = []
    t = threading.Thread(target=lambda: seen.append(memory.tight()), name="analysis")
    t.start()
    t.join()
    assert seen == [False]  # 数え直しの続き (見るルームの集計を先に作る) は止めない


def test_gc_note_inside_a_held_lock_does_not_stop_the_thread():
    """note がロックを持っている最中に GC の知らせが割り込んでも、止まらない (2026-10-31 に、視聴まで止まった)。"""
    import threading as th

    log = selfcheck.SlowLog()
    done = th.Event()

    def inside():
        with log._lock:  # note の途中で、片付けが始まった時と同じ
            selfcheck._gc["started"] = time.monotonic() - 10
            real, selfcheck.SLOW = selfcheck.SLOW, log
            try:
                selfcheck._gc_timer("stop", {"generation": 2})
            finally:
                selfcheck.SLOW = real
        done.set()

    th.Thread(target=inside, daemon=True).start()
    assert done.wait(2)
    rows = log.summary(time.time())
    assert [(r["kind"], r["count"]) for r in rows] == [("gc", 1)] and rows[0]["max"] >= 9
