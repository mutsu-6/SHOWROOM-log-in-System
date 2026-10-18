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


def test_backup_finishes_while_writes_never_stop(tmp_path):
    # 前の作り (Connection.backup) は、写す途中に書き込みがあると最初から写し直し、書き込みが続くと終わらなかった
    _fill(tmp_path, 60000)  # 十数 MB
    stop = threading.Event()

    def writer():
        with Store(tmp_path) as s:
            while not stop.is_set():
                s.conn.execute("INSERT INTO t(v) VALUES ('y')")
                time.sleep(0.002)

    t = threading.Thread(target=writer)
    t.start()
    started = time.time()
    try:
        r = backup.backup_research_db(tmp_path, now=1792000000)
    finally:
        stop.set()
        t.join()
    assert r["ok"] and time.time() - started < 20


def test_backup_removes_old_copies_before_making_new_one(tmp_path, monkeypatch):
    _fill(tmp_path, 10)
    backup.backup_research_db(tmp_path, keep=1, now=1792000000)
    seen = []
    real = backup.shutil.disk_usage

    def usage(path):  # 空きを調べる時には、もう古い写しが消えている
        seen.append(sorted(p.name for p in backup.backup_dir(tmp_path).glob("research-*.db")))
        return real(path)

    monkeypatch.setattr(backup.shutil, "disk_usage", usage)
    r = backup.backup_research_db(tmp_path, keep=1, now=1792000000 + 86400)
    assert r["ok"] and seen == [[]]
    assert len(list(backup.backup_dir(tmp_path).glob("research-*.db"))) == 1


def test_upload_gcs_sends_in_chunks(tmp_path, monkeypatch):
    import io
    import json as _json
    import urllib.request

    from showroom_keeper import backup as b

    folder = b.backup_dir(tmp_path)
    folder.mkdir(parents=True)
    (folder / "research-20261018-0400.db").write_bytes(b"x" * 2500)
    monkeypatch.setattr(b, "GCS_CHUNK", 1000)
    calls = []

    class Resp(io.BytesIO):
        headers = {"Location": "https://upload/session"}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake(req, timeout=0):
        if isinstance(req, str) or req.full_url == b.TOKEN_URL:
            return Resp(_json.dumps({"access_token": "t", "expires_in": 3600}).encode())
        calls.append((req.get_method(), req.full_url, req.headers.get("Content-range"), len(req.data or b"")))
        if req.get_method() == "PUT" and not calls[-1][2].endswith("2499/2500"):
            raise urllib.error.HTTPError(req.full_url, 308, "Resume Incomplete", {}, None)
        return Resp(b"{}")

    import urllib.error
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    now = 1792292400.0  # 2026-10-18 (日) 12:00 日本時間
    r = b.upload_gcs(tmp_path, "my-bucket", now)
    assert r["ok"] and r["name"] == "research-sun.db"
    assert calls[0][0] == "POST" and "my-bucket" in calls[0][1] and "name=research-sun.db" in calls[0][1]
    assert [c[2] for c in calls[1:]] == ["bytes 0-999/2500", "bytes 1000-1999/2500", "bytes 2000-2499/2500"]
    assert b.last_upload(tmp_path)["ok"]

    def denied(req, timeout=0):
        if req.full_url == b.TOKEN_URL:
            return Resp(_json.dumps({"access_token": "t", "expires_in": 3600}).encode())
        raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", denied)
    r = b.upload_gcs(tmp_path, "my-bucket", now)
    assert not r["ok"] and "権限" in r["reason"]
    assert b.upload_gcs(tmp_path, "", now) is None


def test_record_disk_keeps_sizes_and_growth(tmp_path, monkeypatch):
    from showroom_keeper import backup as b

    (tmp_path / "gifts" / "room").mkdir(parents=True)
    (tmp_path / "live_events").mkdir()
    (tmp_path / "gifts" / "room" / "a.jsonl").write_bytes(b"x" * 1000)
    (tmp_path / "live_events" / "r.jsonl").write_bytes(b"y" * 100)
    (tmp_path / "research.db").write_bytes(b"z" * 50)
    (tmp_path / "research.db-wal").write_bytes(b"z" * 5)
    now = 1792292400.0
    b.record_disk(tmp_path, now)
    hist = b._disk_history(tmp_path / b.DISK_FILE)
    assert hist[0][2] == {"gifts": 1000, "live_events": 100, "research.db": 55}
    assert b.growth(tmp_path, now) == []  # 1 日分たまるまでは出さない
    (tmp_path / "gifts" / "room" / "b.jsonl").write_bytes(b"x" * 2000)
    b.record_disk(tmp_path, now + 3600)  # 1 日に 1 回だけ残す
    assert len(b._disk_history(tmp_path / b.DISK_FILE)) == 1
    b.record_disk(tmp_path, now + 2 * 86400)
    assert b.growth(tmp_path, now + 2 * 86400)[0] == ("gifts", 1000.0)


def test_backup_measures_table_sizes_and_disk_history_keeps_them(tmp_path):
    import sqlite3

    from showroom_keeper import backup as b
    from showroom_keeper.research_store import db_path

    con = sqlite3.connect(db_path(tmp_path))
    con.execute("CREATE TABLE room_lives (room_id INTEGER, started_at REAL)")
    con.execute("CREATE INDEX room_lives_start ON room_lives(started_at)")
    con.executemany("INSERT INTO room_lives VALUES (?, ?)", [(i, i * 1.5) for i in range(5000)])
    con.commit()
    con.close()
    r = b.backup_research_db(tmp_path, now=1792292400.0)
    tables = r["tables"]
    if not tables:  # dbstat の無い SQLite
        return
    assert "room_lives" in tables and "room_lives_start" not in tables  # 索引は表の分に入れる
    b.record_disk(tmp_path, 1792292400.0)
    assert b._disk_history(tmp_path / b.DISK_FILE)[0][2]["research.db の room_lives"] == tables["room_lives"]
