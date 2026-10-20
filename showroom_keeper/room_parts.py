"""ルームのページの重い部品 (リス活・重なるルーム・ギフターごとのルーム数) を作っておき、ページはそれを読むだけにする。

ルームのページの 1 回目 (ディスクのキャッシュが冷たい時) は 6.7〜29 秒かかり、その 65〜80% がリス活だった
(常連 16〜23 人の全部の貢献 5,500〜17,900 行を、表のあちこちから 1 行ずつ読む。2026-10-20 に Cursor が測った。報告は PR #69)。
重なるルーム・ギフターごとのルーム数も、大きなルームで 2〜4 秒かかっていた。

- ページは、作っておいた分を読むだけ (古くても、そのまま出す)。まだ作っていないルームだけ、開いた時に作り、裏で保存する
- 作り直すのは、ルームのイベントの貢献ランキングが新しく入った時と、1 日 1 回。サーバーが暇な時だけ、1 分に 1 ルームずつ
  (暇 = 自己点検で見たディスク待ちが 10% より少ない・管理画面が 10 分使われていない・自己点検が後回しにしていない)。
  ほかの重い集計とは重ならないように memory.heavy(wait=False) を通す
- 作っておくのは、管理画面で開いたことのあるルームだけ (30 日開かれなければ消す)。全部のルームを作ると、1 日では作り終わらない
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

from . import memory

log = logging.getLogger(__name__)

VERSION = 1  # 作る中身を変えたら上げる (前に作った分を作り直す)
EVERY = 86400  # 作り直す間隔 (秒)
KEEP = 30 * 86400  # これだけ開かれなかったルームは、作っておくのをやめる (秒)
TICK = 60  # 暇かどうかを見て、1 ルーム作る間隔 (秒)
IDLE_ADMIN = 600  # 管理画面がこれだけ使われていなければ「暇」(秒)
IDLE_IOWAIT = 0.10  # 自己点検で見たディスク待ちがこれより少なければ「暇」
IOWAIT_SAMPLE = 5  # 作る直前に、今のディスク待ちを見る秒数
SAVE = True  # 開いた時に作った分を保存するか (テストでは、ほかのテストの値を変えないように False にする)
WAIT = False  # True なら、開いた時に作った分をその場で保存する (テスト用)

_lock = threading.Lock()
_opened: dict[tuple[str, int], float] = {}  # 開いた時刻 (裏で作る時に表へ書く。開くたびには書き込まない)
_built: dict[tuple[str, int], dict[str, Any]] = {}  # 開いた時に作って、裏で保存し終えるまでの分 (その間に開いた人は、これを使う)
_seen = time.time()  # 管理画面がいちばん後に使われた時刻 (起動してすぐはディスクのキャッシュが冷たいので、使われたものとして 10 分待つ)


def seen() -> None:
    """管理画面が使われた (web.py が要求のたびに呼ぶ)。"""
    global _seen
    _seen = time.time()


def idle(now: float | None = None) -> bool:
    """サーバーが暇か (裏で作ってよいか)。"""
    now = time.time() if now is None else now
    if now - _seen < IDLE_ADMIN or memory.holding() > 0:
        return False
    from . import selfcheck

    iowait = (selfcheck.CHECKER.load.get("iowait") if selfcheck.CHECKER is not None else None)
    return iowait is None or iowait < IDLE_IOWAIT  # 測れない所 (Mac など) では、ほかの 2 つで決める


def _cpu() -> tuple[float, float] | None:
    """CPU 全体の時間と、そのうちのディスク待ち (/proc/stat。読めない所では None)。"""
    try:
        fields = [float(x) for x in Path("/proc/stat").read_text().splitlines()[0].split()[1:]]
        return sum(fields[:8]), fields[4]
    except (OSError, ValueError, IndexError):
        return None


def quiet_now(stop: threading.Event, seconds: float = IOWAIT_SAMPLE) -> bool:
    """今 (seconds 秒のあいだ) のディスク待ちが IDLE_IOWAIT より少ないか。測れなければ True。
    自己点検のディスク待ちは 5 分の平均なので、その中の短い山 (10 秒ごとに見て 25〜43%。2026-10-20 に Cursor が測った) の
    最中に作り始めないよう、作る直前にも見る。"""
    a = _cpu()
    if stop.wait(seconds) or a is None:
        return a is None
    b = _cpu()
    if b is None or b[0] <= a[0]:
        return True
    return (b[1] - a[1]) / (b[0] - a[0]) < IDLE_IOWAIT


def source(s: Any, room_id: int) -> float:
    """このルームのイベントの貢献ランキングが、いちばん後に入った時刻 (変わったら作り直す)。"""
    return s.scalar("SELECT MAX(ranking_at) FROM room_events WHERE room_id=? AND final=1", (int(room_id),), 0) or 0


def build(s: Any, room_id: int) -> dict[str, Any]:
    """部品を作る (重い)。{rounds, overlap, rooms}。"""
    from . import explore, room_analysis

    rid = int(room_id)
    rooms = {r[0]: r[1] for r in s.all(
        f"""SELECT g.user_id, (SELECT COUNT(*) FROM user_room o WHERE o.user_id=g.user_id AND MAX(o.cumulative, o.events) > 0)
            FROM (SELECT user_id FROM user_room WHERE room_id=? AND MAX(cumulative, events) > 0
                  ORDER BY MAX(cumulative, events) DESC, user_id LIMIT {explore.ROOM_GIFTERS}) g""", (rid,))}
    return {"rounds": room_analysis.listener_rounds(s, rid), "overlap": explore.overlap_rows(s, rid), "rooms": rooms}


def _row(r: Any) -> dict[str, Any]:
    return {"at": r["at"], "src": r["src"], "rounds": json.loads(r["rounds"]), "overlap": json.loads(r["overlap"]),
            "rooms": {int(k): v for k, v in json.loads(r["rooms"]).items()}}


def save(s: Any, room_id: int, parts: dict[str, Any], src: float, now: float, opened: float | None = None) -> None:
    with s.tx() as c:
        c.execute("""INSERT INTO room_parts(room_id, version, at, src, opened, rounds, overlap, rooms) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                     ON CONFLICT(room_id) DO UPDATE SET version=excluded.version, at=excluded.at, src=excluded.src,
                       opened=MAX(COALESCE(opened, 0), COALESCE(excluded.opened, 0)), rounds=excluded.rounds,
                       overlap=excluded.overlap, rooms=excluded.rooms""",
                  (int(room_id), VERSION, now, src, opened or now, json.dumps(parts["rounds"], ensure_ascii=False),
                   json.dumps(parts["overlap"]), json.dumps(parts["rooms"])))


def for_page(s: Any, room_id: int) -> dict[str, Any]:
    """ルームのページに出す部品。作っておいた分があれば、それを読むだけ (古くても)。無ければその場で作り、裏で保存する。"""
    rid, now = int(room_id), time.time()
    key = (str(s.state_dir), rid)
    with _lock:
        _opened[key] = now
    r = s.one("SELECT * FROM room_parts WHERE room_id=? AND version=?", (rid, VERSION))
    if r is not None:
        return _row(r)
    # 同じルームを同時に開いた時 (URL の名前と番号の両方で開いた時も) は、1 回だけ作る (once。2026-10-20 に Codex が見つけた)
    from . import once

    return once.shared(("room-parts", key), lambda: _build_for_page(s, rid, key, now))


def _build_for_page(s: Any, rid: int, key: tuple[str, int], now: float) -> dict[str, Any]:
    with _lock:
        hit = _built.get(key)  # 作ったが、まだ保存し終えていない
    if hit is not None:
        return hit
    r = s.one("SELECT * FROM room_parts WHERE room_id=? AND version=?", (rid, VERSION))  # 待っている間に、保存し終えた
    if r is not None:
        return _row(r)
    src = source(s, rid)
    out = {"at": now, "src": src, **build(s, rid)}
    if not SAVE:
        return out
    if WAIT:
        save(s, rid, out, src, now)
        return out
    with _lock:
        _built[key] = out
    # 書き込みの順番 (Store.tx) を、ページの要求で待たない
    threading.Thread(target=_save_later, args=(Path(s.state_dir), rid, out, src, now), name="room-parts-save", daemon=True).start()
    return out


def _save_later(state_dir: Path, rid: int, parts: dict[str, Any], src: float, now: float) -> None:
    from .research_store import Store

    try:
        with Store(state_dir) as s:
            save(s, rid, parts, src, now)
    except Exception as e:  # noqa: BLE001 - 保存できなくても、次に裏で作る時か、次に開いた時に作る
        log.debug("ルームの部品を保存できませんでした (%s): %s", rid, e)
    finally:
        with _lock:
            _built.pop((str(state_dir), rid), None)


def step(s: Any, now: float | None = None) -> bool:
    """古くなった (貢献ランキングが新しく入った・1 日たった・作り方を変えた) ルームを 1 つ作り直す。作り直したら True。
    暇かどうか・ほかの重い集計と重ならないかは、呼ぶ側 (run_forever) が見る。"""
    now = time.time() if now is None else now
    sd = str(s.state_dir)
    with _lock:
        opened = {rid: at for (d, rid), at in _opened.items() if d == sd}
    rows = s.all("SELECT room_id, version, at, src, opened FROM room_parts")
    gone = [r["room_id"] for r in rows if max(r["opened"] or 0, opened.get(r["room_id"], 0)) < now - KEEP]
    if gone:
        with s.tx() as c:
            c.executemany("DELETE FROM room_parts WHERE room_id=?", [(rid,) for rid in gone])
    todo = None
    for r in sorted((r for r in rows if r["room_id"] not in gone), key=lambda r: r["at"] or 0):
        if r["version"] != VERSION or (r["at"] or 0) < now - EVERY or source(s, r["room_id"]) != (r["src"] or 0):
            todo = r
            break
    if todo is None:
        return False
    rid = todo["room_id"]
    started = time.time()
    src = source(s, rid)
    save(s, rid, build(s, rid), src, now, max(todo["opened"] or 0, opened.get(rid, 0)))
    log.info("ルーム %s のリス活・重なるルームを作り直しました (%.1f 秒)", rid, time.time() - started)
    return True


def run_forever(state_dir: Path, stop: threading.Event) -> None:
    from .research_store import Store

    while not stop.wait(TICK):
        if not idle() or not quiet_now(stop):
            continue
        try:
            with memory.heavy(wait=False) as got:
                if not got:
                    continue  # ほかの重い集計が動いている: 次の機会に
                with Store(state_dir) as s:
                    step(s)
            memory.release()
        except Exception as e:  # noqa: BLE001 - 1 回の失敗で止めない
            log.warning("ルームの部品を作れませんでした: %s", e)


_stop: threading.Event | None = None


def start(state_dir: Path) -> None:
    """裏で作る仕事を始める (何回呼んでも 1 本だけ)。"""
    global _stop
    if _stop is not None and not _stop.is_set():
        return
    _stop = threading.Event()
    threading.Thread(target=run_forever, args=(Path(state_dir), _stop), name="room-parts", daemon=True).start()


def stop() -> None:
    if _stop is not None:
        _stop.set()
