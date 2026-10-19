import logging
import os
import sqlite3
import threading
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
