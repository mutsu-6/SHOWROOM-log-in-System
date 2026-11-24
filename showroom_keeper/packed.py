"""記録のファイル (gifts/<ルーム>.jsonl・live_events/<ルーム>.jsonl) の、終わった月の分を gzip にしまう。

2026-11-13 に空きが 3.5GB まで減り、research.db の写しが作れなかった。gifts は 1.65GB で、gzip にすると 11〜13 分の 1、
live_events は 9 分の 1 になる (2026-11-06 に Cursor が測った)。消さずに縮める (集めたデータは減らさない)。

## 形
- 終わった月の分は <ルーム>.<YYYYMM>.jsonl.gz に移し、<ルーム>.jsonl には今の月の分だけを残す。
  月が変わって CUT_DAYS 日たってから (配信中に月をまたいだ分を、急いでしまわないため)
- しまったあとの <ルーム>.jsonl の 1 行目には、しまったファイルの名前と、しまった分のバイト数を書く (頭書き):
  {"packed": ["<ルーム>.202610.jsonl.gz"], "base": 123456}
  読む所は、頭書きに書いてある .gz を古い順に読んでから、<ルーム>.jsonl の 2 行目から先を読む (read_text)。
  .gz は、頭書きに名前が載ったあとは変えない・消さない。頭書きに載っていない .gz (途中で止まった時の残り) は読まない
- 「どこまで読んだか」を覚えている所 (explore の gift_senders・gift_events・room_gift_daily・live_visits) は、
  しまう前と同じ「通しの位置」(しまった分 + 今のファイルの位置) で覚える (size・read_from)。
  しまってもファイルが小さくなったことにはならないので、表を作り直さない。research.db を前の写しに戻した時も、
  その位置から先を (.gz の中から) 読む
- 入れ替えは、全部を新しいファイルに書いてから os.replace で 1 度に行う。途中で止まっても、前の <ルーム>.jsonl がそのまま残る。
  書き込み (append) と入れ替えは同じ鍵 (LOCK) を取る。入れ替えで鍵を持つのは、最後の少しを写して名前を替える間だけ
  (視聴のループも書き込みでこの鍵を取るので、長く持たない)

## 重くしない
裏の catchup が BUDGET 秒ずつ、1 秒に RATE バイトまで読んで進める。視聴を先にする時 (holding) は、そのファイルをやめて次に回す。
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from .status import JST

log = logging.getLogger(__name__)

LOCK = threading.Lock()  # 記録のファイルへの書き込み (1 行足す) と、しまったあとの入れ替え
HEAD = b'{"packed": '
CUT_DAYS = 2  # 月が変わってから、前の月の分をしまうまでの日数
BUDGET = 20.0  # 1 回に使う秒数 (catchup は収集の合間の 10 分ごと・管理画面を開いた時に、60 秒あけて動く)
RATE = 10e6  # 1 秒に読むバイト数の上限 (ディスクと CPU を取り合わないように。手元で 1 秒に 54MB しまえたので、CPU 1 つの 2 割ほど)
BLOCK = 1 << 20  # 1 回に読む大きさ
LEVEL = 6  # gzip の強さ (9 とほぼ同じ大きさで、ずっと速い)

_AT = re.compile(rb'(?m)^\{"at": (\d+)')  # 記録の行は、どれも "at" から書いている (gifts.record_gift・live_events.record)
_PART = re.compile(r"\.(\d{6})(?:-\d+)?\.jsonl\.gz$")
_heads: dict[str, tuple[int, int, int, list[str]]] = {}  # パス -> (inode, base, 頭書きのバイト数, しまったファイル)
_checked: set[tuple[str, int]] = set()  # この月の分まで見終えたファイル (起き直すまで、見直さない)
_heads_lock = threading.Lock()
_step_lock = threading.Lock()  # しまう仕事は 1 本だけ (catchup と収集の合間の両方から頼まれる)
_cleaned: set[str] = set()
GAP = 300  # soon: 前に始めてから、次に始めるまで空ける秒数
_soon_at = 0.0


# ------------------------------------------------------------------ 書く
def append(path: Path, line: str) -> None:
    """記録のファイルに 1 行足す (入れ替えの途中に足した行を落とさないため、鍵を取る)。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with LOCK, path.open("a", encoding="utf-8") as f:
        f.write(line)


# ------------------------------------------------------------------ 読む
def _read_head(f: Any) -> tuple[int, int, list[str]]:
    f.seek(0)
    if f.read(len(HEAD)) != HEAD:
        return 0, 0, []
    f.seek(0)
    line = f.readline()
    try:
        h = json.loads(line)
        return int(h["base"]), len(line), [str(p) for p in h["packed"]]
    except (ValueError, KeyError, TypeError) as e:
        raise OSError(f"{f.name}: 頭書きが読めません") from e


def _head(f: Any) -> tuple[int, int, list[str]]:
    """開いたファイルの頭書き (base, 頭書きのバイト数, しまったファイル)。入れ替えると inode が変わるので、inode ごとに覚える。"""
    ino = os.fstat(f.fileno()).st_ino
    key = str(f.name)
    with _heads_lock:
        got = _heads.get(key)
    if got and got[0] == ino:
        return got[1], got[2], got[3]
    base, hlen, parts = _read_head(f)
    with _heads_lock:
        _heads[key] = (ino, base, hlen, parts)
    return base, hlen, parts


def size(path: Path) -> int | None:
    """通しの大きさ (しまった分 + 今のファイルの頭書きより先)。無ければ None。"""
    try:
        st = os.stat(path)
        with _heads_lock:
            got = _heads.get(str(path))
        if got and got[0] == st.st_ino:  # 頭書きを前に読んだファイル (ふだんは開かずに、大きさを見るだけ)
            return got[1] + st.st_size - got[2]
        with open(path, "rb") as f:
            base, hlen, _ = _head(f)
            return base + os.fstat(f.fileno()).st_size - hlen
    except OSError:
        return None


def read_from(path: Path, pos: int, end: int) -> bytes:
    """通しの位置 pos から end までのバイト (pos がしまった分の中なら、.gz から読む)。"""
    with open(path, "rb") as f:
        base, hlen, parts = _head(f)
        if pos >= base:
            f.seek(pos - base + hlen)
            return f.read(max(0, end - pos))
        out = bytearray()
        skip = pos
        for name in parts:
            with gzip.open(path.parent / name, "rb") as g:
                data = g.read()
            if skip >= len(data):
                skip -= len(data)
                continue
            out += data[skip:]
            skip = 0
        f.seek(hlen)
        out += f.read(max(0, end - base))
        return bytes(out[: max(0, end - pos)])


def _month_end(month: int) -> float:
    y, m = divmod(month, 100)
    y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return datetime(y, m, 1, tzinfo=JST).timestamp()


def read_text(path: Path, since: float | None = None) -> str:
    """しまった分 (.gz) と今のファイルを、古い順につないだ中身 (頭書きは除く)。無ければ OSError。
    since なら、その時刻より前に終わった月の .gz は読まない (その月の行は、どれもその月の終わりより前)。"""
    with open(path, "rb") as f:
        _, hlen, parts = _head(f)
        chunks = []
        for name in parts:
            m = _PART.search(name)
            if since is not None and m and _month_end(int(m.group(1))) <= since:
                continue
            with gzip.open(path.parent / name, "rb") as g:
                chunks.append(g.read())
        f.seek(hlen)
        chunks.append(f.read())
    return b"".join(chunks).decode("utf-8", errors="replace")


# ------------------------------------------------------------------ しまう
def _month(at: int) -> int:
    d = datetime.fromtimestamp(at, JST)
    return d.year * 100 + d.month


def cut_month(now: float | None = None) -> int:
    """この月より前の分をしまう (月が変わって CUT_DAYS 日たつまでは、前の前の月まで)。"""
    return _month(int((time.time() if now is None else now) - CUT_DAYS * 86400))


def _line_month(line: bytes) -> int | None:
    m = _AT.match(line)
    if m:
        return _month(int(m.group(1)))
    try:
        at = json.loads(line).get("at")
        return _month(int(at)) if at is not None else None
    except (ValueError, TypeError, AttributeError, OverflowError, OSError):
        return None


class _Abort(Exception):
    pass


def pack_file(path: Path, cut: int, holding: Callable[[], bool] = lambda: False) -> int:
    """path の、cut の月より前の分をしまう。しまったバイト数を返す (しまう分が無ければ 0)。視聴を先にする時は _Abort。"""
    room = path.name[: -len(".jsonl")]
    folder = path.parent
    temps: list[Path] = []
    finals: list[Path] = []
    outs: dict[int, tuple[Path, Any, Any]] = {}  # 月 -> (書きかけのファイル, ファイル, gzip)
    try:
        with open(path, "rb") as f:
            st = os.fstat(f.fileno())
            base, hlen, parts = _read_head(f)
            f.seek(hlen)
            first = f.readline()
            m0 = _line_month(first) if first.endswith(b"\n") else None
            if not first.endswith(b"\n") or (m0 is not None and m0 >= cut):
                return 0  # しまう分が無い (いちばん古い行が、今の月)
            order: list[int] = []

            def put(month: int, data: bytes) -> None:
                if month not in outs:
                    tmp = folder / f"{room}.{month}.jsonl.gz.part"
                    temps.append(tmp)
                    raw = open(tmp, "wb")
                    outs[month] = (tmp, raw, gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=LEVEL, mtime=0))
                    order.append(month)
                outs[month][2].write(data)

            started, read = time.monotonic(), 0

            def pace(n: int) -> None:
                nonlocal read
                read += n
                wait = read / RATE - (time.monotonic() - started)
                if wait > 0:
                    time.sleep(wait)
                if holding():
                    raise _Abort

            run: int | None = None
            pending: list[bytes] = []  # 時刻の分からない行 (時刻の分かる最初の行と同じ月に入れる)
            offset = hlen  # ここまでをしまった (今のファイルの位置)
            carry = b""
            f.seek(hlen)
            stop = False
            while not stop:
                block = f.read(BLOCK)
                if not block:
                    break
                data = carry + block
                cut_i = data.rfind(b"\n") + 1
                carry, data = data[cut_i:], data[:cut_i]
                if not data:
                    continue
                ats = [int(x) for x in _AT.findall(data)]
                lo = _month(min(ats)) if ats else None
                hi = _month(max(ats)) if ats else None
                if ats and lo == hi and not pending and max(run or 0, hi) < cut:  # ふつう: 1 つの月の行だけ
                    run = max(run or 0, hi)
                    put(run, data)
                    offset += len(data)
                else:  # 月の変わり目など: 1 行ずつ
                    for line in data.splitlines(keepends=True):
                        mm = _line_month(line)
                        if mm is not None:
                            run = mm if run is None else max(run, mm)  # 前の行より古い時刻の行は、前の行と同じ月に入れる
                        if run is None:
                            pending.append(line)
                            continue
                        if run >= cut:
                            stop = True
                            break
                        for p in pending:
                            put(run, p)
                            offset += len(p)
                        pending = []
                        put(run, line)
                        offset += len(line)
                pace(len(block))
            packed = offset - hlen
            if packed <= 0:
                return 0
            names: list[str] = []
            for month in order:
                tmp, raw, gz = outs[month]
                gz.close()
                raw.flush()
                os.fsync(raw.fileno())
                raw.close()
                name, n = f"{room}.{month}.jsonl.gz", 1
                while name in parts or name in names:  # 頭書きに載った .gz は変えない
                    n += 1
                    name = f"{room}.{month}-{n}.jsonl.gz"
                os.replace(tmp, folder / name)
                temps.remove(tmp)
                finals.append(folder / name)
                names.append(name)
            # 新しい <ルーム>.jsonl: 頭書き + しまわなかった分
            new = folder / f"{room}.jsonl.new"
            temps.append(new)
            head = json.dumps({"packed": parts + names, "base": base + packed}, ensure_ascii=False) + "\n"
            with open(new, "wb") as out:
                out.write(head.encode("utf-8"))
                f.seek(offset)
                limit = os.fstat(f.fileno()).st_size  # ここまでを先に写す (写す間に足された分は、鍵を取ってから写す)
                while f.tell() < limit:
                    chunk = f.read(min(BLOCK, limit - f.tell()))
                    if not chunk:
                        break
                    out.write(chunk)
                    pace(len(chunk))
                out.flush()
                os.fsync(out.fileno())
                copied = f.tell()
                with LOCK:  # ここだけ書き込みを待たせる (写したあとに足された少しを写し、名前を替える)
                    try:
                        same = os.stat(path).st_ino == st.st_ino
                    except OSError:
                        same = False
                    if not same:
                        raise _Abort  # ほかで入れ替えた
                    f.seek(copied)
                    out.write(f.read())
                    out.flush()
                    os.replace(new, path)
                temps.remove(new)
                os.fsync(out.fileno())
            finals.clear()  # 頭書きに載ったので、もう消さない
        with _heads_lock:
            _heads.pop(str(path), None)
        log.info("%s の %s までの分をしまいました (%.1f MB)", path.name, max(order), packed / 1e6)
        return packed
    finally:
        for _, raw, gz in outs.values():
            if not raw.closed:
                try:
                    gz.close()
                except (OSError, ValueError):
                    pass
                raw.close()
        for p in temps + finals:  # 途中でやめた時の書きかけ (頭書きに載る前の .gz も)
            try:
                p.unlink()
            except OSError:
                pass


def soon(dirs: list[Path], holding: Callable[[], bool] = lambda: False) -> bool:
    """裏のスレッドで step を 1 回動かす (動いている時・GAP 秒以内に始めた時・視聴を先にする時は何もしない)。始めたら True。"""
    global _soon_at
    if holding() or _step_lock.locked() or time.time() - _soon_at < GAP:
        return False
    _soon_at = time.time()

    def run() -> None:
        try:
            step(dirs, holding)
        except Exception as e:  # noqa: BLE001
            log.warning("記録のファイルの古い分をしまう仕事が進みませんでした: %s", e)

    threading.Thread(target=run, name="pack-records", daemon=True).start()
    return True


def _clean(folder: Path) -> None:
    """前に途中で止まって残った書きかけ (起き直したあとに 1 回だけ。しまう仕事は 1 本だけなので、今書いているものではない)。"""
    if str(folder) in _cleaned:
        return
    _cleaned.add(str(folder))
    for pattern in ("*.jsonl.gz.part", "*.jsonl.new"):
        for p in folder.glob(pattern):
            try:
                p.unlink()
            except OSError:
                pass


def step(dirs: list[Path], holding: Callable[[], bool] = lambda: False, budget: float = BUDGET,
         now: float | None = None) -> bool:
    """BUDGET 秒まで、記録のファイルの終わった月の分をしまう。全部見終えていれば True (そのあとは、月が変わるまで何もしない)。"""
    if not _step_lock.acquire(blocking=False):
        return False  # ほかで動いている
    try:
        return _step(dirs, holding, budget, now)
    finally:
        _step_lock.release()


def _step(dirs: list[Path], holding: Callable[[], bool], budget: float, now: float | None) -> bool:
    until = time.monotonic() + budget
    cut = cut_month(now)
    for folder in dirs:
        if not folder.is_dir():
            continue
        _clean(folder)
        for path in sorted(folder.glob("*.jsonl")):
            key = (str(path), cut)
            if key in _checked:
                continue
            if holding() or time.monotonic() > until:
                return False
            try:
                pack_file(path, cut, holding)
            except _Abort:
                return False
            except Exception as e:  # noqa: BLE001 - 1 つのファイルの失敗で、ほかを止めない (前のファイルはそのまま残る)
                log.warning("%s の古い分をしまえませんでした: %s", path.name, e)
            _checked.add(key)
    return True
