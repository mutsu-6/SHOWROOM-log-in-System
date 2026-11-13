"""記録のファイルの終わった月の分を .gz にしまう (packed.py)。しまっても、表の中身・読める記録が変わらないこと。"""

import gzip
import json
from datetime import datetime

import pytest

from showroom_keeper import explore, gifts, listing, live_events, packed
from showroom_keeper.research_store import Store
from showroom_keeper.status import JST

OCT = datetime(2026, 10, 15, 12, tzinfo=JST).timestamp()
NOV = datetime(2026, 11, 5, 12, tzinfo=JST).timestamp()
NOV13 = datetime(2026, 11, 13, 12, tzinfo=JST).timestamp()
DEC = datetime(2026, 12, 5, 12, tzinfo=JST).timestamp()
DEC10 = datetime(2026, 12, 10, 12, tzinfo=JST).timestamp()
REAL_SOON = packed.soon  # conftest がほかのテストで止める前の、本物


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    monkeypatch.setattr(packed, "_heads", {})
    monkeypatch.setattr(packed, "_checked", set())
    monkeypatch.setattr(packed, "RATE", 1e12)
    monkeypatch.setattr(listing, "_file_cache", {})


def _catalog(state):
    (state / "cache").mkdir(exist_ok=True)
    (state / "cache" / "gift_catalog.json").write_text(json.dumps({"gifts": {"1": {"point": 100, "free": False},
                                                                             "2": {"point": 1, "free": True}}}))


def _gift(state, room, at, uid, gid=1, n=1):
    gifts.record_gift(state, room, 1, {"u": uid, "ac": f"n{uid}", "g": gid, "n": n, "created_at": int(at)})


def _visit(state, room, at, uid, n):
    live_events.record(state, room, 1, {"t": 18, "u": uid, "m": f"さんが{n}回目の訪問", "created_at": int(at)})


def _fill(state, room="aaa"):
    for i in range(300):  # 10 月: 300 件 (同じ秒にも)
        _gift(state, room, OCT + i * 60, 100 + i % 7, 1 + i % 2, 1 + i % 3)
        _visit(state, room, OCT + i * 60, 100 + i % 7, i + 1)
    _gift(state, room, NOV - 86400 * 30 + 5, 999)  # 時刻が前後した行 (10 月) は、そのあとの行と同じ月に入る
    for i in range(50):  # 11 月
        _gift(state, room, NOV + i * 60, 200 + i % 3)
        _visit(state, room, NOV + i * 60, 200 + i % 3, i + 1)


def _tables(state):
    with Store(state) as s:
        explore.update_gift_senders(state, s, "aaa")
        explore.update_gift_daily(state, s, "aaa")
        explore.update_live_visits(state, s, "aaa")
        return {
            "events": s.all("SELECT user_id, at, gift_id, num FROM gift_events ORDER BY user_id, at, gift_id, num"),
            "senders": s.all("SELECT user_id, times, points, free_points, first_at, last_at FROM gift_senders ORDER BY user_id"),
            "daily": s.all("SELECT day, paid_points, free_points, gifts FROM room_gift_daily ORDER BY day"),
            "visits": s.all("SELECT user_id, at, n FROM live_visits ORDER BY user_id, at"),
        }


def _same(a, b):
    return {k: [tuple(r) for r in v] for k, v in a.items()} == {k: [tuple(r) for r in v] for k, v in b.items()}


def _drop_db(state):
    from showroom_keeper import research_store

    for p in state.glob("research.db*"):
        p.unlink()
    research_store._ready.discard(str(research_store.db_path(state)))


def _pack(state, now):
    return packed.step([gifts.gift_dir(state), live_events.event_dir(state)], now=now)


def test_finished_months_go_to_gz_and_nothing_is_lost(tmp_path):
    _catalog(tmp_path)
    _fill(tmp_path)
    before = _tables(tmp_path)
    recorded = gifts.load_recorded(tmp_path, "aaa")
    loaded = listing._load_file(gifts.gift_dir(tmp_path) / "aaa.jsonl")
    events = live_events.load(tmp_path, "aaa")
    raw = (gifts.gift_dir(tmp_path) / "aaa.jsonl").read_bytes()

    assert _pack(tmp_path, NOV13)
    gz = gifts.gift_dir(tmp_path) / "aaa.202610.jsonl.gz"
    assert gz.exists() and (live_events.event_dir(tmp_path) / "aaa.202610.jsonl.gz").exists()
    now = (gifts.gift_dir(tmp_path) / "aaa.jsonl").read_bytes()
    assert now.startswith(b'{"packed": ["aaa.202610.jsonl.gz"]') and len(now) < len(raw) / 3
    assert gzip.decompress(gz.read_bytes()) + now.split(b"\n", 1)[1] == raw  # つなぐと、しまう前と同じ

    # 読む所は、しまう前と同じ中身を返す。表は作り直さず、そのまま
    listing._file_cache.clear()
    assert gifts.load_recorded(tmp_path, "aaa") == recorded
    assert listing._load_file(gifts.gift_dir(tmp_path) / "aaa.jsonl") == loaded
    assert live_events.load(tmp_path, "aaa") == events
    assert live_events.load(tmp_path, "aaa", NOV) == [e for e in events if e["at"] >= NOV]
    assert packed.size(gifts.gift_dir(tmp_path) / "aaa.jsonl") == len(raw)
    assert listing._added({"aaa": gifts.gift_dir(tmp_path) / "aaa.jsonl"}, {"aaa": len(raw)}) == {"aaa"}
    assert _same(_tables(tmp_path), before)

    # しまったあとに足した分は、そのまま足される
    _gift(tmp_path, "aaa", NOV + 9999, 300)
    _visit(tmp_path, "aaa", NOV + 9999, 300, 1)
    after = _tables(tmp_path)
    assert len(after["events"]) == len(before["events"]) + 1 and len(after["visits"]) == len(before["visits"]) + 1


def test_a_new_or_restored_research_db_reads_the_gz_too(tmp_path):
    """research.db を作り直した時・前の写しに戻した時 (読んだ位置が、しまった分の中) も、.gz から読んで同じ表になる。"""
    _catalog(tmp_path)
    _fill(tmp_path)
    full = _tables(tmp_path)
    _drop_db(tmp_path)
    path = gifts.gift_dir(tmp_path) / "aaa.jsonl"
    lines = path.read_bytes().splitlines(keepends=True)
    keep = path.read_bytes()
    path.write_bytes(b"".join(lines[:120]))  # 途中まで読んだ research.db (前の写し) を作る
    epath = live_events.event_dir(tmp_path) / "aaa.jsonl"
    ekeep = epath.read_bytes()
    epath.write_bytes(b"".join(ekeep.splitlines(keepends=True)[:77]))
    _tables(tmp_path)
    path.write_bytes(keep)
    epath.write_bytes(ekeep)
    packed._heads.clear()
    assert _pack(tmp_path, NOV13)
    assert _same(_tables(tmp_path), full)

    _drop_db(tmp_path)  # 何も無い research.db
    assert _same(_tables(tmp_path), full)


def test_packing_twice_keeps_the_order(tmp_path):
    _catalog(tmp_path)
    _fill(tmp_path)
    _pack(tmp_path, NOV13)
    for i in range(20):
        _gift(tmp_path, "aaa", DEC + i, 500)
    path = gifts.gift_dir(tmp_path) / "aaa.jsonl"
    whole = packed.read_text(path)
    mid = packed.size(path)
    assert _pack(tmp_path, DEC10)
    assert json.loads(path.read_bytes().split(b"\n", 1)[0])["packed"] == ["aaa.202610.jsonl.gz", "aaa.202611.jsonl.gz"]
    assert packed.read_text(path) == whole
    assert packed.size(path) == mid
    assert packed.read_from(path, 10, mid) == whole.encode()[10:]
    assert packed.read_from(path, mid - 30, mid) == whole.encode()[-30:]
    assert packed.read_from(path, 0, mid) == whole.encode()


def test_nothing_is_packed_until_two_days_into_the_month(tmp_path):
    _fill(tmp_path)
    raw = (gifts.gift_dir(tmp_path) / "aaa.jsonl").read_bytes()
    _pack(tmp_path, datetime(2026, 11, 2, 12, tzinfo=JST).timestamp())  # 11/2 は、まだ 10 月をしまわない
    assert (gifts.gift_dir(tmp_path) / "aaa.jsonl").read_bytes() == raw
    assert not list(gifts.gift_dir(tmp_path).glob("*.gz"))


def test_lines_written_while_packing_are_kept(tmp_path, monkeypatch):
    """しまっている間に視聴が書いた行は、落とさない。"""
    _fill(tmp_path)
    monkeypatch.setattr(packed, "BLOCK", 1000)
    added = []

    def writing():
        _gift(tmp_path, "aaa", NOV + 50000 + len(added), 700)
        added.append(1)
        return False

    path = gifts.gift_dir(tmp_path) / "aaa.jsonl"
    count = len(gifts.load_recorded(tmp_path, "aaa"))
    packed.step([gifts.gift_dir(tmp_path)], holding=writing, now=NOV13)
    assert added and path.read_bytes().startswith(packed.HEAD)
    assert len(gifts.load_recorded(tmp_path, "aaa")) == count + len(added)


def test_stopping_midway_leaves_the_file_as_it_was(tmp_path, monkeypatch):
    _fill(tmp_path)
    monkeypatch.setattr(packed, "BLOCK", 1000)
    path = gifts.gift_dir(tmp_path) / "aaa.jsonl"
    raw = path.read_bytes()
    calls = []
    assert not packed.step([gifts.gift_dir(tmp_path)], holding=lambda: calls.append(1) or len(calls) > 3, now=NOV13)
    assert path.read_bytes() == raw
    assert sorted(p.name for p in gifts.gift_dir(tmp_path).iterdir()) == ["aaa.jsonl"]
    # 載っていない .gz (前に途中で止まった残り) は読まない
    (gifts.gift_dir(tmp_path) / "aaa.202610.jsonl.gz").write_bytes(gzip.compress(raw))
    assert packed.read_text(path) == raw.decode()
    assert packed.step([gifts.gift_dir(tmp_path)], now=NOV13)  # 次の回にしまう (残りは上書き)
    assert packed.read_text(path) == raw.decode()


def test_files_already_checked_are_not_read_again(tmp_path, monkeypatch):
    _fill(tmp_path)
    assert _pack(tmp_path, NOV13)
    opened = []
    real = packed.pack_file
    monkeypatch.setattr(packed, "pack_file", lambda *a, **k: opened.append(a) or real(*a, **k))
    assert _pack(tmp_path, NOV13)
    assert opened == []


def test_soon_runs_one_step_in_the_background(tmp_path, monkeypatch):
    import threading

    real = REAL_SOON
    monkeypatch.setattr(packed, "_soon_at", 0.0)
    _fill(tmp_path)
    monkeypatch.setattr(packed, "cut_month", lambda now=None: 202611)
    assert real([gifts.gift_dir(tmp_path)])
    assert not real([gifts.gift_dir(tmp_path)])  # GAP 秒は、続けて始めない
    for t in threading.enumerate():
        if t.name == "pack-records":
            t.join(10)
    assert (gifts.gift_dir(tmp_path) / "aaa.202610.jsonl.gz").exists()
