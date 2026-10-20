import sqlite3
import threading
import time

from showroom_keeper import catchup, feed, memory, once, research
from showroom_keeper.gifts import record_gift
from showroom_keeper.research_store import Store, db_path


def test_pages_do_not_wait_for_the_write_lock(tmp_path, monkeypatch):
    """ページを開く時は書き足さず (書き込みの順番を待たず)、前に足した分で返す。新しい分は裏の仕事が足す。"""
    monkeypatch.setattr(catchup, "WAIT", False)
    monkeypatch.setattr(catchup, "_last", {})
    with Store(tmp_path):
        pass
    record_gift(tmp_path, "room_a", 1, {"u": 7, "ac": "たろう", "g": 1, "n": 1, "created_at": time.time()})
    lock = sqlite3.connect(str(db_path(tmp_path)), timeout=60)
    lock.execute("BEGIN IMMEDIATE")  # 収集が長く書いている間
    done = threading.Event()
    threading.Thread(target=lambda: (feed.build(tmp_path, [], use_cache=False), research.watch_list(tmp_path, []), done.set()),
                     daemon=True).start()
    assert done.wait(10), "ページが書き込みの順番を待っている"
    lock.rollback()
    lock.close()
    for _ in range(100):  # 裏の仕事が、書き込めるようになってから足す
        with Store(tmp_path) as s:
            if s.scalar("SELECT COUNT(*) FROM gift_events"):
                break
        time.sleep(0.05)
    with Store(tmp_path) as s:
        assert s.scalar("SELECT COUNT(*) FROM gift_events") == 1
    for _ in range(100):  # 裏の仕事が終わるまで待つ (次のテストに持ち込まない)
        if not catchup._running:
            break
        time.sleep(0.05)


def test_catch_up_runs_once_at_a_time_and_waits_while_held(tmp_path, monkeypatch):
    monkeypatch.setattr(catchup, "WAIT", False)
    monkeypatch.setattr(catchup, "_last", {})
    monkeypatch.setattr(catchup, "_running", False)
    started = []
    gate = threading.Event()
    monkeypatch.setattr(catchup, "_run", lambda d, mark=True: (started.append(d), gate.wait(5),
                                                               setattr(catchup, "_running", False)))
    monkeypatch.setattr(memory, "_hold_until", time.time() + 60)
    assert not catchup.soon(tmp_path)  # 自己点検が後回しにしている間は足さない (視聴を先に)
    monkeypatch.setattr(memory, "_hold_until", 0.0)
    assert catchup.soon(tmp_path)
    assert not catchup.soon(tmp_path)  # 動いている間・GAP 秒以内は、続けて足さない
    gate.set()
    for _ in range(100):
        if not catchup._running:
            break
        time.sleep(0.01)
    assert not catchup.soon(tmp_path)  # GAP 秒以内
    catchup._last.clear()
    assert catchup.soon(tmp_path) and len(started) == 2


def test_same_page_at_the_same_time_is_counted_once():
    calls = []
    gate = threading.Event()

    def slow():
        calls.append(1)
        gate.wait(5)
        return {"n": len(calls)}

    out = []
    threads = [threading.Thread(target=lambda: out.append(once.shared(("t", 1), slow))) for _ in range(3)]
    for t in threads:
        t.start()
    time.sleep(0.2)
    gate.set()
    for t in threads:
        t.join(5)
    assert calls == [1] and out == [{"n": 1}] * 3


def test_shared_results_are_kept_for_ttl_and_forgotten_on_change():
    n = []
    assert once.shared(("w", 1), lambda: n.append(1) or len(n), ttl=60) == 1
    assert once.shared(("w", 1), lambda: n.append(1) or len(n), ttl=60) == 1
    once.forget(lambda k: k[0] == "w")
    assert once.shared(("w", 1), lambda: n.append(1) or len(n), ttl=60) == 2
