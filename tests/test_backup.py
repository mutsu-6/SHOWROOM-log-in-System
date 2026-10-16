import sqlite3
import threading
import time

from showroom_keeper import backup
from showroom_keeper.research_store import Store, db_path


def _fill(state_dir, n):
    with Store(state_dir) as s:
        s.conn.execute("CREATE TABLE IF NOT EXISTS t (i INTEGER PRIMARY KEY, v TEXT)")
        s.conn.executemany("INSERT INTO t(v) VALUES (?)", [("x" * 200,)] * n)


def test_backup_makes_a_consistent_copy_while_writing(tmp_path):
    _fill(tmp_path, 2000)
    stop = threading.Event()

    def writer():  # 集めている途中の書き込みの代わり
        with Store(tmp_path) as s:
            while not stop.is_set():
                s.conn.execute("INSERT INTO t(v) VALUES ('y')")
                time.sleep(0.001)

    t = threading.Thread(target=writer)
    t.start()
    try:
        r = backup.backup_research_db(tmp_path, now=1792000000)
    finally:
        stop.set()
        t.join()
    assert r["ok"] and r["file"].startswith("research-") and r["mb"] > 0
    copy = sqlite3.connect(str(backup.backup_dir(tmp_path) / r["file"]))
    assert copy.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert copy.execute("SELECT COUNT(*) FROM t").fetchone()[0] >= 2000
    copy.close()
    assert backup.last_backup(tmp_path)["file"] == r["file"]


def test_backup_keeps_only_newest_copies(tmp_path):
    _fill(tmp_path, 10)
    for day in range(4):
        backup.backup_research_db(tmp_path, keep=2, now=1792000000 + day * 86400)
    names = sorted(p.name for p in backup.backup_dir(tmp_path).glob("research-*.db"))
    from datetime import datetime

    from showroom_keeper.status import JST

    assert len(names) == 2 and names[-1].startswith(datetime.fromtimestamp(1792000000 + 3 * 86400, JST).strftime("research-%Y%m%d"))


def test_backup_skips_without_db_or_disk_space(tmp_path, monkeypatch):
    assert backup.backup_research_db(tmp_path) is None  # research.db がまだ無い
    _fill(tmp_path, 10)

    class Full:
        free = 100

    monkeypatch.setattr(backup.shutil, "disk_usage", lambda path: Full)
    r = backup.backup_research_db(tmp_path)
    assert r["ok"] is False and "空き" in r["reason"] and not list(backup.backup_dir(tmp_path).glob("research-*.db"))
    assert db_path(tmp_path).exists()
