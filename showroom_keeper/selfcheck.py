"""自己点検 (バグチェック) と自浄。

keeper の中で 5 分ごとに、これまでサーバーで実際に起きた困りごとが起きていないかを確かめ、
サーバーを起動し直さず・集めたデータを消さずに直せるものは、その場で直す。結果は管理画面の「収集」の「自己点検」に出す。

確かめること (どれも軽いものだけ。大きな表は読まない)
- エラー: ログに出たエラー・同じ注意の繰り返し・落ちたスレッドを、出た場所 (ファイルと行) ごとに数える
- メモリ: Python 本体の大きさと、メモリが足りずに開けていない視聴のページ (2026-10-19 に 0.83GB まで膨らみ、2 ルームを視聴できなかった)
- research.db-wal の大きさ (2026-10-20 に 607MB までふくらんでいた)
- 裏で動く仕事: 全ルームの収集・Internet Archive のスレッドが止まっていないか、重い集計が長く続いていないか
- 記録が止まっていないか: 配信中のルームの一覧と、開催中のイベントの 1 時間ごとのポイント
- スレッドの数・開いているファイルの数 (増え続けるなら、閉じ忘れのバグ)
- 重い処理 (これもバグとして数える): 管理画面の開くのが遅い要求・時間のかかった重い集計・遅い SQL・
  視聴の処理 (asyncio) が待たされた時間・ディスク待ちと Python の CPU・長く続く収集の仕事

自分で直すこと (自浄)
- 期限の切れた使い回しの結果を捨て、使い終えたメモリを OS に返す (次に開く時は、どのみち数え直すものだけ)
- メモリが足りず視聴のページを開けない時・Python が大きすぎる時は、使い回しの結果を減らして空ける (視聴を優先する)
- research.db-wal が大きい時は、本体に移して縮める
- 止まった収集のスレッドを、もう一度動かす (1 日 3 回まで)
- 途中で止まって残った作業用のファイル (分析の古い土台・.tmp) を消す (集めたデータではないもの)
- サーバーが重い時 (視聴の処理が待たされた・ディスク待ちが多い) は、前の結果がある裏の数え直しを 10 分後回しにする

ここで直さないもの (利用者に知らせるだけ): サーバーの起動し直し・集めたデータを消すこと・止まったまま戻らないスレッド。
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
import threading
import time
import traceback
from collections import deque
from pathlib import Path
from typing import Any

from . import memory

log = logging.getLogger(__name__)

CHECK_EVERY = 300  # 点検の間隔 (秒)
FIRST_CHECK = 60  # 起動してから最初の点検までの時間 (秒。起動の仕事が終わってから)
PY_MEM_CLEAN_MB = 600  # Python 本体 (メモリ + swap) がこれより大きければ、使い回しの結果を減らす (MB)
PY_MEM_WARN_MB = 800  # これより大きければ知らせ、もっと減らす (MB。2026-10-19 に 830MB で視聴のページが開けなかった)
WAL_WARN = 256 << 20  # research.db-wal がこれより大きければ縮める (バイト。ふだんは 64MB まで)
HEAVY_WARN = 1800  # 重い集計 (analyze など) がこれより長く続いたら知らせる (秒。サーバーで 2 分ほど)
LIVE_STALE = 1800  # 配信中のルームの一覧 (5 分ごと) がこれより古ければ知らせる (秒)
PROGRESS_STALE = 3 * 3600  # 開催中のイベントのポイント (1 時間ごと) がこれより古ければ知らせる (秒)
THREADS_WARN = 300  # スレッドの数
FDS_WARN_SHARE = 0.8  # 開いているファイルの数が、上限のこの割合を超えたら知らせる
TEMP_AGE = 86400  # 作業用のファイル (.tmp) が、これより古ければ消す (秒)
BASE_AGE = 6 * 3600  # 分析の古い土台 (cache/analysis-base-*.db) が、これより古ければ消す (秒)
RESTART_LIMIT = 3  # 止まったスレッドを、24 時間にもう一度動かす回数の上限
ERROR_WINDOW = 86400  # エラーを数える期間 (秒)
REPEAT_WARN = 60  # 同じ注意が 1 時間にこれより多く出たら知らせる (1 分に 1 回より多い。ふだんの失敗の後回しは数えすぎない)
ERROR_KINDS = 200  # 覚えておくエラーの種類の上限
FIX_KEEP = 50  # 覚えておく自浄の記録の数
STATE_FILE = "selfcheck.json"
SLOW_PAGE = 5.0  # 管理画面の要求がこれより長くかかったら知らせる (秒)
SLOW_HEAVY = 600  # 重い集計 1 回がこれより長くかかったら知らせる (秒。analyze はサーバーで 2 分ほど)
SLOW_SQL_WARN = 5.0  # 読み込みの SQL 1 回がこれより長くかかったら知らせる (秒。2 秒から記録する)
SLOW_STEP = 900  # 全ルームの収集の 1 つの仕事がこれより長く続いたら知らせる (秒)
LOOP_LAG_WARN = 2.0  # 視聴の処理 (asyncio) がこれより長く待たされたら知らせる (秒)
LOOP_LAG_HOLD = 5.0  # これより長く待たされたら、裏の数え直しを後回しにする (秒)
IOWAIT_WARN = 0.3  # 点検の間のディスク待ちの割合がこれより多ければ知らせる
IOWAIT_HOLD = 0.5  # これより多ければ、裏の数え直しを後回しにする
CPU_WARN = 0.9  # Python が点検の間に使った CPU (1 つ分を 1) がこれより多ければ知らせる
HOLD_FOR = 600  # 後回しにする時間 (秒)
HOLD_GAP = 1200  # 後回しが終わってから、次に後回しにできるまでの時間 (秒。数え直しがずっとできなくならないように)
SLOW_WINDOW = 3600  # 遅い処理を知らせる期間 (秒。表には 24 時間分を出す)


# ---------------------------------------------------------------- エラーを数える
def _where(record: logging.LogRecord) -> str:
    """エラーの出た場所。例外があれば、このプログラムの中でいちばん奥の行 (原因に近い所)。"""
    if record.exc_info and record.exc_info[2] is not None:
        inner = [f for f in traceback.extract_tb(record.exc_info[2]) if "showroom_keeper" in f.filename]
        if inner:
            f = inner[-1]
            return f"{Path(f.filename).name}:{f.lineno} ({f.name})"
    return f"{Path(record.pathname).name}:{record.lineno}"


class ErrorLog(logging.Handler):
    """警告とエラーを、ログの文の形 (%s を埋める前) と出た場所ごとに数える。"""

    def __init__(self, kinds: int = ERROR_KINDS) -> None:
        super().__init__(level=logging.WARNING)
        self.kinds = kinds
        self._lock = threading.Lock()
        self.items: dict[tuple[Any, ...], dict[str, Any]] = {}

    def emit(self, record: logging.LogRecord) -> None:
        if record.name == __name__:
            return  # 自己点検そのものの知らせは数えない
        try:
            exc = record.exc_info[0].__name__ if record.exc_info and record.exc_info[0] else None
            where = _where(record)
            key = (record.name, str(record.msg), exc, where)
            sample = record.getMessage().splitlines()[0][:300] if record.getMessage() else ""
            with self._lock:
                it = self.items.get(key)
                if it is None:
                    if len(self.items) >= self.kinds:  # いちばん長く出ていない種類を忘れる
                        self.items.pop(min(self.items, key=lambda k: self.items[k]["last"]))
                    it = self.items[key] = {"logger": record.name, "where": where, "exception": exc, "level": record.levelno,
                                            "first": record.created, "times": deque(maxlen=500)}
                it["times"].append(record.created)
                it["last"] = record.created
                it["sample"] = sample
                it["level"] = max(it["level"], record.levelno)
        except Exception:  # noqa: BLE001 - 数えられなくても、ログそのものは止めない
            pass

    def summary(self, now: float, limit: int = 10) -> list[dict[str, Any]]:
        """24 時間に出た種類を、エラー・回数の多い順に。"""
        out = []
        with self._lock:
            for it in self.items.values():
                day = sum(1 for t in it["times"] if t > now - ERROR_WINDOW)
                if not day:
                    continue
                hour = sum(1 for t in it["times"] if t > now - 3600)
                full = len(it["times"]) == it["times"].maxlen and it["times"][0] > now - ERROR_WINDOW
                out.append({"level": "error" if it["level"] >= logging.ERROR else "warn", "where": it["where"],
                            "logger": it["logger"], "exception": it["exception"], "sample": it["sample"],
                            "day": day, "more": full, "hour": hour, "first": it["first"], "last": it["last"]})
        out.sort(key=lambda r: (r["level"] != "error", -r["day"]))
        return out[:limit]


ERRORS = ErrorLog()


# ---------------------------------------------------------------- 遅い処理を数える
class SlowLog:
    """時間のかかった処理を、種類と名前 (画面の要求の道・SQL の頭・集計の名前) ごとに数える。"""

    def __init__(self, kinds: int = ERROR_KINDS) -> None:
        self.kinds = kinds
        self._lock = threading.Lock()
        self.items: dict[tuple[str, str], dict[str, Any]] = {}

    def note(self, kind: str, name: str, seconds: float, now: float | None = None) -> None:
        now = time.time() if now is None else now
        key = (kind, " ".join(str(name).split())[:120])
        with self._lock:
            it = self.items.get(key)
            if it is None:
                if len(self.items) >= self.kinds:
                    self.items.pop(min(self.items, key=lambda k: self.items[k]["times"][-1][0]))
                it = self.items[key] = {"times": deque(maxlen=100)}
            it["times"].append((now, float(seconds)))

    def summary(self, now: float, window: float = ERROR_WINDOW, limit: int = 15) -> list[dict[str, Any]]:
        """window 秒のうちに出たものを、いちばん遅かった順に。"""
        out = []
        with self._lock:
            for (kind, name), it in self.items.items():
                recent = [sec for t, sec in it["times"] if t > now - window]
                if recent:
                    out.append({"kind": kind, "name": name, "count": len(recent), "max": round(max(recent), 1),
                                "avg": round(sum(recent) / len(recent), 1), "last": it["times"][-1][0]})
        out.sort(key=lambda r: -r["max"])
        return out[:limit]


SLOW = SlowLog()
SLOW_LABELS = {"page": "管理画面", "sql": "SQL の読み込み", "heavy": "重い集計", "loop": "視聴の処理の待ち"}


def note_page(path: str, seconds: float) -> None:
    """管理画面の要求にかかった時間 (web.py が呼ぶ。速いものは数えない)。"""
    if seconds >= SLOW_PAGE / 2:
        SLOW.note("page", path, seconds)


def _note_sql(sql: str, seconds: float) -> None:
    SLOW.note("sql", sql, seconds)


async def watch_loop(every: float = 1.0) -> None:
    """視聴の処理 (asyncio) が、ほかの重い処理に待たされていないかを測る (1 秒ごとに起きて、遅れた分を数える)。
    keeper が別の仕事として動かす。"""
    import asyncio

    while True:
        started = time.monotonic()
        await asyncio.sleep(every)
        lag = time.monotonic() - started - every
        if lag >= LOOP_LAG_WARN / 2:
            SLOW.note("loop", "視聴の処理", lag)


def _thread_died(args: Any) -> None:
    """スレッドが例外で落ちた時に、ログに出す (エラーとして数え、管理画面に出る)。"""
    if args.exc_type is SystemExit:
        return
    name = args.thread.name if args.thread is not None else "?"
    log_dead.error("スレッド「%s」が止まりました: %s", name, args.exc_value,
                   exc_info=(args.exc_type, args.exc_value, args.exc_traceback))


log_dead = logging.getLogger("thread")


# ---------------------------------------------------------------- 調べる道具
def _proc_mb() -> tuple[float | None, float | None]:
    """この Python のメモリ (RSS) と swap に追い出された分 (MB)。"""
    rss = swap = None
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                rss = int(line.split()[1]) / 1024
            elif line.startswith("VmSwap:"):
                swap = int(line.split()[1]) / 1024
    except (OSError, ValueError):
        pass
    return rss, swap


def _fds() -> tuple[int | None, int | None]:
    """開いているファイルの数と、その上限。"""
    try:
        n = len(os.listdir("/proc/self/fd"))
    except OSError:
        return None, None
    try:
        import resource

        soft = resource.getrlimit(resource.RLIMIT_NOFILE)[0]
    except (ImportError, ValueError, OSError):
        soft = None
    return n, soft if soft and soft > 0 else None


def _prune(cache: dict[Any, Any], older: float, lock: Any = None) -> int:
    """(作った時刻, ...) の形の使い回しから、older より前に作ったものを捨てる。捨てた数。"""
    def run() -> int:
        n = 0
        for k, v in list(cache.items()):
            if isinstance(v, tuple) and v and isinstance(v[0], (int, float)) and v[0] < older:
                cache.pop(k, None)
                n += 1
        return n
    if lock is None:
        return run()
    with lock:
        return run()


def prune_expired(now: float) -> int:
    """期限の切れた使い回しの結果を捨てる。どれも、期限が切れたら次に開く時に数え直すものなので、画面は遅くならない。"""
    from . import event_analysis as ea, explore, feed, gifter_analysis as ga, listing, news, room_analysis as ra

    n = 0
    for cache, ttl in ((ea._series, ea.SERIES_TTL), (ea._choice, ea.CHOICE_TTL), (ea._strengths, ea.STRENGTH_TTL),
                       (ea._lag, ea.LAG_TTL), (ga._cuts, ga.CUT_TTL), (ra._congestion, ra.CONGESTION_TTL),
                       (explore._folded_rooms, explore.FOLDED_TTL)):
        n += _prune(cache, now - ttl)
    n += _prune(feed._cache, now - feed.FEED_TTL, feed._lock)
    n += _prune(news._cache, now - news.NEWS_TTL, news._lock)
    n += _prune(listing._search_cache, now - listing.SEARCH_TTL, listing._lock)
    n += _prune(listing._user_cache, now - listing.USER_TTL, listing._lock)
    # ギフターの一覧は、LISTENERS_STALE より古いものは返さずに数え直すので、持っていても使われない
    n += _prune(listing._listeners_cache, now - listing.LISTENERS_STALE)
    return n


def shrink_caches(state_dir: Path, strong: bool) -> int:
    """メモリが足りない時に、使い回しの結果を減らす。strong の時は、次に開く時に少し待つものも減らす (視聴を優先する)。"""
    from . import listing, research

    n = 0
    # 分析: 前の土台から作った結果は、もう使わない (土台を数え直したら作り直す)。strong の時は最後に開いた見るルームの分だけ残す
    #  (ほかはファイルに残っているので、開いた時はファイルから読み直す)
    bases = [b[0] for b in list(research._bases.values()) if b]
    last_keys = {v[2] for v in list(research._last_view.values())}
    for k, v in list(research._analysis.items()):
        if (bases and v[0] not in bases) or (strong and k not in last_keys):
            research._analysis.pop(k, None)
            n += 1
    if strong:
        # ギフターの一覧は、ふだん開く「すべての期間」の分だけ残す
        for k in list(listing._listeners_cache):
            if k[1] is not None or k[2] is not None:
                listing._listeners_cache.pop(k, None)
                n += 1
        # 記録のファイルの読み込みは、古く使った方から半分を捨てる (後ろほど最近使ったもの)
        with listing._lock:
            for k in list(listing._file_cache)[: len(listing._file_cache) // 2]:
                listing._file_cache.pop(k, None)
                n += 1
    return n


# ---------------------------------------------------------------- 点検
class Checker:
    def __init__(self, keeper: Any, clock: Any = time.time):
        self.keeper = keeper
        self._clock = clock
        self.findings: list[dict[str, Any]] = []
        self.fixes: list[dict[str, Any]] = []  # 自分で直したこと (新しい順)
        self.restarts: list[float] = []
        self.at: float | None = None
        self.pruned = 0
        self.memory: dict[str, Any] = {}
        self._down: dict[str, int] = {}  # 続けて止まっていた点検の回数
        self._waiting = 0  # 続けて視聴のページを開けていなかった点検の回数
        self._heavy_told: float | None = None
        self._runs_seen = 0.0  # 自己点検が見終えた重い集計の終わりの時刻
        self._cpu: tuple[float, float, float, float] | None = None  # 前の点検の (時刻, Python の CPU 秒, CPU 全体, ディスク待ち)
        self.load: dict[str, Any] = {}
        self._held_at = 0.0  # 最後に後回しにした時刻
        self._load()

    @property
    def state_dir(self) -> Path:
        return Path(self.keeper.cfg.state_file).parent

    def _load(self) -> None:
        try:
            data = json.loads((self.state_dir / STATE_FILE).read_text(encoding="utf-8"))
            self.fixes = list(data.get("fixes") or [])[:FIX_KEEP]
            self.restarts = [float(t) for t in data.get("restarts") or []]
        except (OSError, ValueError, TypeError, AttributeError):
            pass

    def _save(self) -> None:
        path = self.state_dir / STATE_FILE
        try:
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.view(), ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            pass

    def _fixed(self, what: str, now: float) -> None:
        log.info("自己点検: %s", what)
        self.fixes.insert(0, {"at": now, "what": what})
        del self.fixes[FIX_KEEP:]

    def run_once(self) -> list[dict[str, Any]]:
        now = self._clock()
        found: list[dict[str, Any]] = []
        for step in (self._errors, self._memory, self._wal, self._workers, self._freshness, self._leaks, self._leftovers,
                     self._heavy_work):
            try:
                step(now, found)
            except Exception as e:  # noqa: BLE001 - 1 つの点検の失敗で、ほかの点検を止めない
                found.append({"level": "warn", "title": "自己点検の一部ができませんでした",
                              "detail": f"{step.__name__}: {str(e).splitlines()[0] if str(e) else type(e).__name__}"})
        old = {f["title"] for f in self.findings}
        for f in found:
            if f["title"] not in old:
                log.warning("自己点検: %s%s", f["title"], f" ({f['detail']})" if f.get("detail") else "")
        self.findings, self.at = found, now
        self._save()
        return found

    # ---------------------------------------------------------------- 1 つずつの点検
    def _errors(self, now: float, found: list[dict[str, Any]]) -> None:
        for e in ERRORS.summary(now, limit=50):
            if e["level"] == "error":
                found.append({"level": "error", "title": f"エラーが出ています ({e['where']})",
                              "detail": f"24 時間に {e['day']}{'+' if e['more'] else ''} 回。最後: {e['sample']}"})
            elif e["hour"] >= REPEAT_WARN:
                found.append({"level": "warn", "title": f"同じ注意が続いています ({e['where']})",
                              "detail": f"1 時間に {e['hour']} 回。最後: {e['sample']}"})

    def _memory(self, now: float, found: list[dict[str, Any]]) -> None:
        pruned = prune_expired(now)
        self.pruned += pruned
        rss, swap = _proc_mb()
        py = (rss or 0) + (swap or 0) if rss is not None else None
        free = memory.free_mb()
        waiting = [w.key for w in list(getattr(self.keeper, "watchers", []) or []) if getattr(w, "waiting_memory", False)]
        self._waiting = self._waiting + 1 if waiting else 0
        self.memory = {"rss_mb": round(rss) if rss is not None else None, "swap_mb": round(swap) if swap is not None else None,
                       "free_mb": round(free) if free is not None else None, "waiting": waiting}
        if waiting:
            found.append({"level": "warn", "title": f"メモリが足りず、視聴のページを {len(waiting)} ルーム開けていません",
                          "detail": "、".join(waiting)})
        if py is not None and py > PY_MEM_WARN_MB:
            found.append({"level": "warn", "title": f"Python が {py:.0f}MB まで大きくなっています",
                          "detail": f"メモリ {rss:.0f}MB・swap {swap or 0:.0f}MB"})
        strong = self._waiting >= 2 or (py is not None and py > PY_MEM_WARN_MB)
        if strong or waiting or (py is not None and py > PY_MEM_CLEAN_MB) or (free is not None and free < memory.HEAVY_MIN_FREE_MB):
            dropped = shrink_caches(self.state_dir, strong)
            memory.release()
            after = _proc_mb()
            after_py = (after[0] or 0) + (after[1] or 0) if after[0] is not None else None
            if dropped or (py is not None and after_py is not None and py - after_py >= 20):
                size = f" (Python {py:.0f}MB → {after_py:.0f}MB)" if py is not None and after_py is not None else ""
                why = "視聴のページを開けるように" if waiting else "メモリが少ないので"
                self._fixed(f"{why}、使い回しの結果を {dropped} 件捨てて、メモリを空けました{size}", now)
        elif pruned:
            memory.release()

    def _wal(self, now: float, found: list[dict[str, Any]]) -> None:
        from .research_store import db_path

        db = db_path(self.state_dir)
        wal = db.with_name(db.name + "-wal")
        try:
            size = wal.stat().st_size
        except OSError:
            return
        if size <= WAL_WARN:
            return
        con = sqlite3.connect(str(db), timeout=1)
        try:
            con.execute("PRAGMA busy_timeout=1000")
            busy, frames, done = con.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
            if frames >= 0 and done >= frames:  # 全部を本体に移せたら、ファイルを縮める (読んでいる所があれば、次の回に)
                con.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        except sqlite3.Error as e:
            log.debug("書き込み待ちのファイルを縮められませんでした: %s", e)
        finally:
            con.close()
        try:
            after = wal.stat().st_size
        except OSError:
            after = 0
        if after < size:
            self._fixed(f"research.db-wal を {size / 2**20:.0f}MB から {after / 2**20:.0f}MB に縮めました", now)
        if after > WAL_WARN:
            found.append({"level": "warn", "title": f"research.db-wal が {after / 2**20:.0f}MB あります",
                          "detail": "長く読んでいる処理があり、本体に移せていません。次の点検でもう一度縮めます"})

    def _workers(self, now: float, found: list[dict[str, Any]]) -> None:
        from . import archive, sweep

        cfg = self.keeper.cfg
        self.restarts = [t for t in self.restarts if t > now - 86400]
        if getattr(cfg, "research_all", False):
            self._worker("sweep", "全ルームの収集", sweep.SWEEPER, now, found,
                         lambda: sweep.start(self.state_dir, scan=cfg.research_scan, ranking_share=cfg.research_ranking_share))
            if getattr(cfg, "research_archive", False):
                self._worker("archive", "Internet Archive からの取り戻し", archive.WORKER, now, found,
                             lambda: archive.start(self.state_dir, cfg.research_archive_gap))
        holder = memory.heavy_holder()
        if holder and now - holder["since"] > HEAVY_WARN:
            minutes = (now - holder["since"]) // 60
            found.append({"level": "warn", "title": f"重い集計「{holder['name']}」が {minutes:.0f} 分続いています",
                          "detail": "ほかの重い集計 (分析・ギフターの一覧・集まり具合) が待たされています"})
            if self._heavy_told != holder["since"]:  # どこで止まっているかを、1 回だけログに書く
                self._heavy_told = holder["since"]
                frame = sys._current_frames().get(holder["ident"])
                if frame is not None:
                    log.warning("自己点検: 重い集計「%s」のいまの場所:\n%s", holder["name"],
                                "".join(traceback.format_stack(frame)[-8:]))

    def _worker(self, key: str, label: str, worker: Any, now: float, found: list[dict[str, Any]], start: Any) -> None:
        stopped = worker is not None and worker._stop.is_set()  # 止めるように言われて止まったものは、動かし直さない
        if stopped or (worker is not None and worker.running):
            self._down[key] = 0
            return
        self._down[key] = self._down.get(key, 0) + 1
        if self._down[key] < 2:  # 起動の途中かもしれないので、2 回続けて止まっていたら
            return
        if len(self.restarts) >= RESTART_LIMIT:
            found.append({"level": "error", "title": f"{label}が止まっています",
                          "detail": f"24 時間に {RESTART_LIMIT} 回動かし直しても、また止まりました。ログのエラーを見てください"})
            return
        start()
        self.restarts.append(now)
        self._down[key] = 0
        self._fixed(f"止まっていた{label}を、もう一度動かしました", now)

    def _freshness(self, now: float, found: list[dict[str, Any]]) -> None:
        from . import research, sweep
        from .research_store import Store, db_path

        sw = sweep.SWEEPER
        if sw is None or not sw.running or sw.disk_low is not None or research.THROTTLE.state()["paused"]:
            return  # 収集を止めている時は、記録が古くなるのは当たり前
        if sw.started_at is None or now - sw.started_at < PROGRESS_STALE or not db_path(self.state_dir).exists():
            return  # 動き始めたばかり (起動し直した直後) は数えない
        with Store(self.state_dir) as s:
            live_at = s.meta("live_at") or 0
            stale = s.scalar("""SELECT COUNT(*) FROM events WHERE start IS NOT NULL AND start <= ? AND end > ?
                                  AND (progress_at IS NULL OR progress_at < ?) AND (retry_at IS NULL OR retry_at < ?)""",
                             (now - PROGRESS_STALE, now, now - PROGRESS_STALE, now))
        if now - live_at > LIVE_STALE:
            found.append({"level": "warn", "title": f"配信中のルームの一覧が {(now - live_at) / 60:.0f} 分読めていません",
                          "detail": "ふだんは 5 分ごと。全ルームの収集の順番が回っていないかもしれません"})
        if stale:
            found.append({"level": "warn", "title": f"開催中のイベント {stale} 件の 1 時間ごとのポイントが 3 時間以上残せていません",
                          "detail": "ポイントの推移が抜けます。全ルームの収集の順番が回っていないかもしれません"})

    def _leaks(self, now: float, found: list[dict[str, Any]]) -> None:
        threads = threading.active_count()
        fds, limit = _fds()
        self.memory.update(threads=threads, fds=fds)
        if threads > THREADS_WARN:
            found.append({"level": "warn", "title": f"スレッドが {threads} 本あります",
                          "detail": "終わらずに増え続けているなら、プログラムの不具合です"})
        if fds is not None and limit and fds > limit * FDS_WARN_SHARE:
            found.append({"level": "warn", "title": f"開いているファイルが {fds} 個あります (上限 {limit})",
                          "detail": "閉じ忘れが続くと、ファイルを開けなくなります"})

    def _leftovers(self, now: float, found: list[dict[str, Any]]) -> None:
        from . import research

        removed, size = 0, 0
        cache = self.state_dir / "cache"
        # 分析の土台: 今使っているもの (覚えているもの) 以外で古いものは、数える途中で止まって残ったもの
        held = research._bases.get(str(self.state_dir.resolve()))
        current = held[1].get("db") if held and isinstance(held[1], dict) else None
        if current and cache.is_dir():
            for p in cache.glob("analysis-base-*.db"):
                removed, size = self._remove_old(p, current, BASE_AGE, now, removed, size)
        for folder in (self.state_dir, cache):
            if folder.is_dir():
                for p in folder.glob("*.tmp"):
                    removed, size = self._remove_old(p, None, TEMP_AGE, now, removed, size)
        if removed:
            self._fixed(f"途中で止まって残った作業用のファイルを {removed} 個 ({size / 2**20:.0f}MB) 消しました", now)

    def _heavy_work(self, now: float, found: list[dict[str, Any]]) -> None:
        """重い処理を見つける。サーバーが重い時は、裏の数え直しを後回しにする。"""
        from . import sweep

        for r in list(memory.runs):  # 終わった重い集計
            if r["at"] > self._runs_seen:
                SLOW.note("heavy", r["name"] or "?", r["seconds"], r["at"])
        if memory.runs:
            self._runs_seen = max(r["at"] for r in list(memory.runs))
        lag_max = 0.0
        for e in SLOW.summary(now, SLOW_WINDOW, limit=ERROR_KINDS):
            label = SLOW_LABELS.get(e["kind"], e["kind"])
            if e["kind"] == "loop":
                lag_max = e["max"]
                if e["max"] >= LOOP_LAG_WARN:
                    found.append({"level": "warn", "title": f"視聴の処理が最大 {e['max']:.0f} 秒待たされました",
                                  "detail": f"1 時間に {e['count']} 回。ほかの重い処理が、視聴と同じ所で動いています"})
            elif (e["kind"] == "page" and e["max"] >= SLOW_PAGE) or (e["kind"] == "sql" and e["max"] >= SLOW_SQL_WARN) \
                    or (e["kind"] == "heavy" and e["max"] >= SLOW_HEAVY):
                found.append({"level": "warn", "title": f"{label}が遅くなっています: {e['name'][:60]}",
                              "detail": f"1 時間に {e['count']} 回・最大 {e['max']:.0f} 秒・平均 {e['avg']:.0f} 秒"})
        from . import research

        busy = (sweep.state() or {}).get("busy_seconds") or 0
        # SHOWROOM に止められて休んでいる間は、仕事が長く続くのは当たり前 (休みを縮めたり、動かし直したりはしない)
        if busy > SLOW_STEP and not research.THROTTLE.state()["paused"]:
            found.append({"level": "warn", "title": f"全ルームの収集の仕事が {busy // 60:.0f} 分続いています",
                          "detail": f"「{sweep.state().get('doing') or ''}」。どこで止まっているかはログにあります"})
        iowait, cpu = self._cpu_share(now)
        self.load = {"iowait": None if iowait is None else round(iowait, 2), "cpu": None if cpu is None else round(cpu, 2),
                     "holding": round(memory.holding())}
        if iowait is not None and iowait > IOWAIT_WARN:
            found.append({"level": "warn", "title": f"ディスク待ちが {iowait:.0%} あります",
                          "detail": "大きな表を読む処理が重なっているかもしれません。CPU の時間の多くが、ディスクを待っています"})
        if cpu is not None and cpu > CPU_WARN:
            found.append({"level": "warn", "title": f"Python が CPU 1 つ分の {cpu:.0%} を使っています",
                          "detail": "5 分のあいだずっと、何かを数え続けています"})
        if (lag_max >= LOOP_LAG_HOLD or (iowait is not None and iowait > IOWAIT_HOLD)) \
                and not memory.holding() and now - self._held_at > HOLD_FOR + HOLD_GAP:
            memory.hold_off(HOLD_FOR)
            self._held_at = now
            why = f"視聴の処理が {lag_max:.0f} 秒待たされた" if lag_max >= LOOP_LAG_HOLD else f"ディスク待ちが {iowait:.0%} ある"
            self._fixed(f"サーバーが重い ({why}) ので、前の結果がある裏の数え直しを {HOLD_FOR // 60} 分後回しにしました", now)

    def _cpu_share(self, now: float) -> tuple[float | None, float | None]:
        """前の点検からの、ディスク待ちの割合 (CPU 全体の時間のうち) と、Python が使った CPU (1 つ分を 1)。"""
        try:
            fields = [float(x) for x in Path("/proc/stat").read_text().splitlines()[0].split()[1:]]
            total, iowait = sum(fields[:8]), fields[4]
            parts = Path("/proc/self/stat").read_text().rsplit(")", 1)[1].split()
            mine = (float(parts[11]) + float(parts[12])) / os.sysconf("SC_CLK_TCK")
        except (OSError, ValueError, IndexError):
            return None, None
        prev, self._cpu = self._cpu, (now, mine, total, iowait)
        if prev is None or total <= prev[2] or now <= prev[0]:
            return None, None
        return (iowait - prev[3]) / (total - prev[2]), (mine - prev[1]) / (now - prev[0])

    @staticmethod
    def _remove_old(p: Path, keep: str | None, age: float, now: float, removed: int, size: int) -> tuple[int, int]:
        try:
            st = p.stat()
            if p.name != keep and now - st.st_mtime > age:
                p.unlink()
                return removed + 1, size + st.st_size
        except OSError:
            pass
        return removed, size

    # ---------------------------------------------------------------- 画面に出すもの
    def view(self) -> dict[str, Any]:
        now = self._clock()
        return {"at": self.at, "every": CHECK_EVERY, "findings": list(self.findings),
                "fixes": [f for f in self.fixes if f["at"] > now - 7 * 86400][:20], "restarts": self.restarts,
                "errors": ERRORS.summary(now), "memory": dict(self.memory), "pruned": self.pruned,
                "slow": SLOW.summary(now), "load": dict(self.load)}

    def run_forever(self, stop: threading.Event) -> None:
        if stop.wait(FIRST_CHECK):
            return
        while True:
            try:
                self.run_once()
            except Exception as e:  # noqa: BLE001 - 点検の失敗で keeper を止めない
                log.warning("自己点検に失敗しました: %s", e)
            if stop.wait(CHECK_EVERY):
                return


CHECKER: Checker | None = None
_stop: threading.Event | None = None
_installed = False


def install() -> None:
    """エラーを数え始め、落ちたスレッドをログに出す (何回呼んでも 1 回だけ)。"""
    global _installed
    if _installed:
        return
    _installed = True
    from . import research_store

    research_store.slow_hooks.append(_note_sql)
    logging.getLogger().addHandler(ERRORS)
    threading.excepthook = _thread_died


def start(keeper: Any) -> Checker:
    """keeper の自己点検を別のスレッドで始める (2 回目からは、点検する keeper だけを取り替える)。"""
    global CHECKER, _stop
    install()
    if CHECKER is None:
        CHECKER, _stop = Checker(keeper), threading.Event()
        threading.Thread(target=CHECKER.run_forever, args=(_stop,), daemon=True, name="selfcheck").start()
    else:
        CHECKER.keeper = keeper
    return CHECKER


def stop() -> None:
    """keeper を止めた時に、点検もやめる。"""
    global CHECKER
    if _stop is not None:
        _stop.set()
    CHECKER = None


def view() -> dict[str, Any] | None:
    return CHECKER.view() if CHECKER is not None else None


def summary() -> dict[str, Any] | None:
    """管理画面の上に出す、短いまとめ (問題の数と、いちばん重いもの)。"""
    if CHECKER is None or CHECKER.at is None:
        return None
    f = CHECKER.findings
    return {"at": CHECKER.at, "problems": len(f), "errors": sum(1 for x in f if x["level"] == "error"),
            "first": f[0]["title"] if f else None}
