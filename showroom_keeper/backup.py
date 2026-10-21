"""research.db (集めた情報) のバックアップ。

全ルームの収集は全部そろうまで約 2 か月かかるので、壊れたり消えたりした時に集め直さずに済むよう、
毎日 1 回、state_dir/backup/ に写しを作る。書き込み中のファイルをそのままコピーすると壊れた写しになることが
あるため (research.db は WAL を使う)、SQLite の VACUUM INTO で写す。

sqlite3.Connection.backup は、写している途中にほかの接続が書き込むと最初から写し直すので、収集が 1 秒ごとに
書き込んでいると、DB が大きいほどいつまでも終わらない。VACUUM INTO は、始めた時の中身を 1 回読むだけで写せて、
そのあいだも収集の書き込みは止めない。

写しは gzip で縮めて (research-YYYYMMDD-HHMM.db.gz、半分ほどになる) 1 つだけ残す。古い日に戻す時は、
Google Cloud が毎日作るディスクの写し (14 日分) を使う。空きが足りない時は、前の写しを消してから作り、
それでも足りなければ作らない (どちらもディスクの写しがあるので戻せる)。
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import shutil
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

from .research_store import db_path
from .status import JST

log = logging.getLogger("backup")

BACKUP_DIR = "backup"
KEEP = 1  # 残す写しの数 (古いものから消す。古い日は Google Cloud のディスクの写しで戻す)
MIN_FREE = 1 << 30  # 写しを作ったあとにも残しておくディスクの空き (1GB)
COMPRESS_LEVEL = 1  # gzip の強さ。サーバーで 300MB が 55 秒で 146MB。6 にしても 133MB で、時間は 2 倍以上
COMPRESS_RATIO = 0.6  # 縮めた写しの大きさの見込み (元の何倍か。空きを確かめる時に、少し多めに見る)
COMPRESS_CHUNK = 16 << 20  # 縮める時に 1 回に読む大きさ (メモリ 2GB でも小さく)
LAST_FILE = "last.json"
GCS_CHUNK = 16 << 20  # サーバーの外 (Google Cloud Storage) に送る時の 1 回の大きさ (256KB の倍数。メモリ 2GB でも小さく)
GCS_FILE = "gcs.json"
TOKEN_URL = "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token"


def backup_dir(state_dir: Path) -> Path:
    return Path(state_dir) / BACKUP_DIR


def _db_size(path: Path) -> int:
    return sum(p.stat().st_size for p in (path, path.with_name(path.name + "-wal")) if p.exists())


def last_backup(state_dir: Path) -> dict[str, Any] | None:
    """前回のバックアップの結果 (管理画面用)。"""
    try:
        return json.loads((backup_dir(state_dir) / LAST_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _save_last(state_dir: Path, result: dict[str, Any]) -> None:
    folder = backup_dir(state_dir)
    folder.mkdir(parents=True, exist_ok=True)
    tmp = folder / f"{LAST_FILE}.tmp"
    tmp.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, folder / LAST_FILE)


def copies(state_dir: Path) -> list[Path]:
    """残っている写し (新しい順)。縮めた写し (.db.gz) と、縮める前の版で作った写し (.db) の両方。"""
    folder = backup_dir(state_dir)
    return sorted([*folder.glob("research-*.db"), *folder.glob("research-*.db.gz")], key=lambda p: p.name, reverse=True)


_FALLOC_PUNCH = 0x01 | 0x02  # FALLOC_FL_KEEP_SIZE | FALLOC_FL_PUNCH_HOLE (Linux)


def _punch(fd: int, offset: int, length: int) -> bool:
    """ファイルの offset から length バイトのディスクを返す (中身は 0 に見える。Linux の ext4 など)。できなければ False。"""
    if not sys.platform.startswith("linux"):
        return False
    try:
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        libc.fallocate.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_long, ctypes.c_long]
        return libc.fallocate(fd, _FALLOC_PUNCH, offset, length) == 0
    except (OSError, AttributeError):
        return False


def _compress(src: Path, dest: Path) -> None:
    """src を gzip で縮めて dest に書く (少しずつ読むので、メモリは COMPRESS_CHUNK ほど)。
    縮め終えた所から src のディスクを返していくので、一時的に要る空きは src の大きさほどで済む (できる所では)。"""
    with src.open("r+b") as f, dest.open("wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=COMPRESS_LEVEL, mtime=0) as out:
        done, punch = 0, True
        while True:
            chunk = f.read(COMPRESS_CHUNK)
            if not chunk:
                break
            if shutil.disk_usage(dest.parent).free < MIN_FREE // 2:  # 見込みより空きが減った時に、収集の書き込みを止めない
                raise OSError("ディスクの空きが少なくなったので、写しを縮めるのをやめました")
            out.write(chunk)
            if punch:
                out.flush()
                raw.flush()
                os.fsync(raw.fileno())  # 縮めた分がディスクに書けてから、元の分を返す
                punch = _punch(f.fileno(), done, len(chunk))
            done += len(chunk)


def _need(size: int) -> float:
    """写しを作るのに要る空きの見込み (縮める前の一時の写し + 縮めた写し + 残しておく空き)。"""
    packed = 0.0 if sys.platform.startswith("linux") else COMPRESS_RATIO  # 返していける所では、縮めた写しは返した所に入る
    return size * (1.2 + packed) + MIN_FREE


def backup_research_db(state_dir: Path, keep: int = KEEP, now: float | None = None) -> dict[str, Any] | None:
    """research.db の縮めた写しを作る。research.db が無ければ None。戻り値は {ok, at, file, mb, raw_mb, seconds, reason}。"""
    state_dir = Path(state_dir)
    src = db_path(state_dir)
    if not src.exists():
        return None
    now = time.time() if now is None else now
    folder = backup_dir(state_dir)
    folder.mkdir(parents=True, exist_ok=True)
    size = _db_size(src)
    need = _need(size)
    # 新しい写しを作る前に、古い写しを消しておく (作ったあとに消すと、一時的に DB の何倍もの空きが要るため)。
    # 作るあいだも keep - 1 個は残しておくので、作るのに失敗しても前の写しはある
    for old in copies(state_dir)[max(keep - 1, 0):]:
        old.unlink(missing_ok=True)
    free = shutil.disk_usage(folder).free
    if free < need and copies(state_dir):
        # 空きが足りない時は、残しておいた前の写しも消して作る (古い日は Google Cloud のディスクの写しで戻せる)
        for old in copies(state_dir):
            old.unlink(missing_ok=True)
        log.warning("ディスクの空きが少ないので、前の写しを消してから作ります (空き %.1fGB、約 %.1fGB 要る)", free / 1e9, need / 1e9)
        free = shutil.disk_usage(folder).free
    if free < need:
        result = {"ok": False, "at": now, "reason": f"ディスクの空きが足りません (空き {free / 1e9:.1f}GB、写しに約 {need / 1e9:.1f}GB 要る)"}
        log.warning("research.db のバックアップをやめました: %s", result["reason"])
        _save_last(state_dir, result)
        return result

    name = datetime.fromtimestamp(now, JST).strftime("research-%Y%m%d-%H%M.db.gz")  # 日本時間
    tmp = folder / f"{name[:-3]}.tmp"
    packed = folder / f"{name}.tmp"
    started = time.time()
    try:
        tmp.unlink(missing_ok=True)  # VACUUM INTO は、同じ名前のファイルがあると失敗する
        source = sqlite3.connect(str(src), timeout=60)
        try:
            source.execute("VACUUM INTO ?", (str(tmp),))
        finally:
            source.close()
        dest = sqlite3.connect(str(tmp))
        try:
            check = dest.execute("PRAGMA quick_check").fetchone()[0]
        finally:
            dest.close()
        if check != "ok":
            raise sqlite3.DatabaseError(f"写しの確認で問題が見つかりました ({check})")
        tables = table_sizes(tmp)
        raw = tmp.stat().st_size
        _compress(tmp, packed)
        os.replace(packed, folder / name)
    except Exception as e:  # noqa: BLE001 - 失敗しても収集は続ける
        packed.unlink(missing_ok=True)
        result = {"ok": False, "at": now, "reason": str(e).splitlines()[0] if str(e) else type(e).__name__}
        log.warning("research.db のバックアップに失敗しました: %s", result["reason"])
        _save_last(state_dir, result)
        return result
    finally:
        tmp.unlink(missing_ok=True)

    # 古い写しを消す (新しい方から keep 個を残す)
    left = copies(state_dir)
    for old in left[max(keep, 1):]:
        old.unlink(missing_ok=True)
    mb = round((folder / name).stat().st_size / 1e6, 1)
    result = {"ok": True, "at": now, "file": name, "mb": mb, "raw_mb": round(raw / 1e6, 1), "seconds": round(time.time() - started, 1),
              "copies": min(len(left), max(keep, 1)), "tables": tables}
    log.info("research.db のバックアップを作りました: %s (%.1f MB、縮める前 %.1f MB、%.0f 秒)", name, mb, result["raw_mb"], result["seconds"])
    _save_last(state_dir, result)
    return result


def last_upload(state_dir: Path) -> dict[str, Any] | None:
    """前回サーバーの外 (Google Cloud Storage) に送った結果 (管理画面用)。"""
    try:
        return json.loads((backup_dir(state_dir) / GCS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _token(cache: dict[str, Any]) -> str:
    """VM のサービスアカウントのトークン (期限の 5 分前に取り直す)。"""
    if cache.get("token") and time.time() < cache.get("until", 0):
        return cache["token"]
    req = urllib.request.Request(TOKEN_URL, headers={"Metadata-Flavor": "Google"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.load(resp)
    cache.update(token=data["access_token"], until=time.time() + float(data.get("expires_in", 300)) - 300)
    return cache["token"]


def upload_gcs(state_dir: Path, bucket: str, now: float | None = None) -> dict[str, Any] | None:
    """いちばん新しい写しを gs://<bucket>/ に曜日ごとの名前 (research-mon.db など) で送る。7 日分が順に上書きされて残る。

    サーバーが壊れたり消えたりしても集めた情報が残るように。VM のサービスアカウントに Cloud Storage へ書き込む権限が要る。
    大きなファイルを一度に読まないよう、分けて送る (resumable upload)。戻り値は {ok, at, name, mb, seconds, reason}。
    """
    state_dir = Path(state_dir)
    found = copies(state_dir)
    if not bucket or not found:
        return None
    src, now = found[0], time.time() if now is None else now
    suffix = ".db.gz" if src.name.endswith(".gz") else ".db"
    name = f"research-{('mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun')[datetime.fromtimestamp(now, JST).weekday()]}{suffix}"
    size, started, cache = src.stat().st_size, time.time(), {}
    try:
        query = urllib.parse.urlencode({"uploadType": "resumable", "name": name})
        req = urllib.request.Request(f"https://storage.googleapis.com/upload/storage/v1/b/{urllib.parse.quote(bucket)}/o?{query}",
                                     data=b"", method="POST", headers={"Authorization": f"Bearer {_token(cache)}",
                                                                       "X-Upload-Content-Length": str(size)})
        with urllib.request.urlopen(req, timeout=60) as resp:
            session = resp.headers["Location"]
        with src.open("rb") as f:
            sent = 0
            while sent < size:
                chunk = f.read(GCS_CHUNK)
                req = urllib.request.Request(session, data=chunk, method="PUT", headers={
                    "Authorization": f"Bearer {_token(cache)}", "Content-Range": f"bytes {sent}-{sent + len(chunk) - 1}/{size}"})
                try:
                    with urllib.request.urlopen(req, timeout=300):
                        pass
                except urllib.error.HTTPError as e:
                    if e.code != 308:  # 308 はまだ続きがあるという返事
                        raise
                sent += len(chunk)
    except Exception as e:  # noqa: BLE001 - 送れなくても収集は続ける (次の日にまた送る)
        reason = str(e).splitlines()[0] if str(e) else type(e).__name__
        if isinstance(e, urllib.error.HTTPError) and e.code == 403:
            reason += " (VM に Cloud Storage へ書き込む権限がありません)"
        result = {"ok": False, "at": now, "bucket": bucket, "reason": reason}
        log.warning("バックアップをサーバーの外に送れませんでした: %s", reason)
    else:
        result = {"ok": True, "at": now, "bucket": bucket, "name": name, "mb": round(size / 1e6, 1),
                  "seconds": round(time.time() - started, 1)}
        log.info("バックアップを gs://%s/%s に送りました (%.1f MB、%.0f 秒)", bucket, name, result["mb"], result["seconds"])
    folder = backup_dir(state_dir)
    tmp = folder / f"{GCS_FILE}.tmp"
    tmp.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, folder / GCS_FILE)
    return result


DISK_FILE = "disk_history.json"  # ディスクの空きを 1 日 1 回残したもの (増え方から、いっぱいになる日を見積もる)


# 大きさを見張る記録 (state_dir の中)。増え方の大きいものを、ディスクの知らせに出す
WATCHED = ("research.db", "gifts", "live_events", "comment_counts", "backup")


def _size(path: Path) -> int:
    if path.is_file():
        return sum(p.stat().st_size for p in (path, path.with_name(path.name + "-wal")) if p.exists())
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.stat(os.path.join(root, name)).st_size
            except OSError:
                continue
    return total


FTS_PARTS = ("data", "idx", "content", "docsize", "config")


def table_sizes(path: Path) -> dict[str, int]:
    """research.db の表ごとの大きさ (索引も含む、バイト)。全体を読むので、動いている DB ではなく、作ったばかりの写しで行う。
    dbstat が使えない SQLite では空。"""
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            owner = dict(con.execute("SELECT name, tbl_name FROM sqlite_master WHERE type IN ('table', 'index')").fetchall())
            # 全文検索の索引 (FTS5) は、中身が <名前>_data・_idx などの表に分かれるので、索引の名前にまとめる
            virtual = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE sql LIKE 'CREATE VIRTUAL TABLE%'")]
            for v in virtual:
                for part in FTS_PARTS:
                    owner[f"{v}_{part}"] = v
            out: dict[str, int] = {}
            for name, size in con.execute("SELECT name, pgsize FROM dbstat WHERE aggregate = 1"):
                table = owner.get(name, name)
                table = owner.get(table, table)
                out[table] = out.get(table, 0) + int(size or 0)
            return out
        finally:
            con.close()
    except sqlite3.Error as e:
        log.debug("表ごとの大きさを測れませんでした: %s", e)
        return {}


def record_disk(state_dir: Path, now: float | None = None) -> None:
    """ディスクの空きと、見張る記録 (WATCHED) の大きさを 1 日 1 回残す (毎日のバックアップの時と、管理画面を開いた時。400 日分)。"""
    now = time.time() if now is None else now
    path = Path(state_dir) / DISK_FILE
    hist = _disk_history(path)
    if hist and now - hist[-1][0] < 86400:
        return
    try:
        sizes = {name: _size(Path(state_dir) / name) for name in WATCHED if (Path(state_dir) / name).exists()}
        # research.db の表ごと (毎日のバックアップの時に、写しで測ったもの)
        sizes.update({f"research.db の {t}": b for t, b in ((last_backup(state_dir) or {}).get("tables") or {}).items()})
        hist = (hist + [[now, shutil.disk_usage(state_dir).free, sizes]])[-400:]
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(hist), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def _disk_history(path: Path) -> list[list[float]]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def disk_days_left(state_dir: Path, free: int, now: float) -> float | None:
    """最近 30 日の空きの減り方で、あと何日でいっぱいになるかを見積もる (7 日分たまるまでは None)。"""
    record_disk(state_dir, now)
    old = [h for h in _disk_history(Path(state_dir) / DISK_FILE) if now - h[0] <= 31 * 86400]
    if not old or now - old[0][0] < 7 * 86400:
        return None
    per_day = (old[0][1] - free) / ((now - old[0][0]) / 86400)
    return free / per_day if per_day > 0 else None


RANKING_TABLES = ("contributions", "user_room", "user_month", "room_events")  # 貢献ランキングを 1 つ取るたびに増える表
BACKLOG_TTL = 600  # 取り終わるまでの見込みを覚えておく秒数 (管理画面は 15 秒ごとに読み直す)
_backlog: dict[str, tuple[float, dict[str, Any] | None]] = {}


def backlog(state_dir: Path, now: float | None = None) -> dict[str, Any] | None:
    """取り終わっていない貢献ランキングの件数と、取り終えるまでにディスクをあと何バイト使うかの見込み。

    件数は、収集が数えて research.db に覚えている集まり具合 (coverage_cache) を読むだけ (ここでは数え直さない)。
    1 件の大きさは、毎朝の写しで測った RANKING_TABLES の大きさ ÷ 取った件数。写しも縮めた分だけ大きくなるので足す。"""
    now = time.time() if now is None else now
    key = str(state_dir)
    hit = _backlog.get(key)
    if hit and now - hit[0] < BACKLOG_TTL:
        return hit[1]
    out = None
    try:
        con = sqlite3.connect(f"file:{db_path(Path(state_dir))}?mode=ro", uri=True, timeout=5)
        try:
            row = con.execute("SELECT v FROM meta WHERE k='coverage_cache'").fetchone()
        finally:
            con.close()
        months = ((json.loads(row[0]) if row else {}).get("result") or {}).get("months") or []
        done, pending = sum(m.get("done") or 0 for m in months), sum(m.get("pending") or 0 for m in months)
        last = last_backup(state_dir) or {}
        size = sum((last.get("tables") or {}).get(t, 0) for t in RANKING_TABLES)
        if done and size and pending:
            packed = (last["mb"] / last["raw_mb"]) if last.get("mb") and last.get("raw_mb") else COMPRESS_RATIO
            per = size / done
            out = {"pending": pending, "per_ranking": per, "bytes": pending * per * (1 + packed)}
    except (sqlite3.Error, OSError, ValueError, TypeError, AttributeError):
        out = None
    _backlog[key] = (now, out)
    return out


def growth(state_dir: Path, now: float) -> list[tuple[str, float]]:
    """見張る記録の、最近 30 日の 1 日あたりの増え方 (バイト)。多い順。1 日分たまるまでは空。"""
    hist = [h for h in _disk_history(Path(state_dir) / DISK_FILE) if len(h) > 2 and now - h[0] <= 31 * 86400]
    if len(hist) < 2 or hist[-1][0] - hist[0][0] < 86400:
        return []
    days = (hist[-1][0] - hist[0][0]) / 86400
    first, last = hist[0][2], hist[-1][2]
    out = [(name, (last[name] - first.get(name, 0)) / days) for name in last]
    return sorted(out, key=lambda x: -x[1])
