"""research.db (集めた情報) のバックアップ。

全ルームの収集は全部そろうまで約 2 か月かかるので、壊れたり消えたりした時に集め直さずに済むよう、
毎日 1 回、state_dir/backup/ に写しを作る。書き込み中のファイルをそのままコピーすると壊れた写しになることが
あるため (research.db は WAL を使う)、SQLite の VACUUM INTO で写す。

sqlite3.Connection.backup は、写している途中にほかの接続が書き込むと最初から写し直すので、収集が 1 秒ごとに
書き込んでいると、DB が大きいほどいつまでも終わらない。VACUUM INTO は、始めた時の中身を 1 回読むだけで写せて、
そのあいだも収集の書き込みは止めない。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
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
KEEP = 2  # 残す写しの数 (古いものから消す)
MIN_FREE = 1 << 30  # 写しを作ったあとにも残しておくディスクの空き (1GB)
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


def backup_research_db(state_dir: Path, keep: int = KEEP, now: float | None = None) -> dict[str, Any] | None:
    """research.db の写しを作る。research.db が無ければ None。戻り値は {ok, at, file, mb, seconds, reason}。"""
    state_dir = Path(state_dir)
    src = db_path(state_dir)
    if not src.exists():
        return None
    now = time.time() if now is None else now
    folder = backup_dir(state_dir)
    folder.mkdir(parents=True, exist_ok=True)
    size = _db_size(src)
    # 新しい写しを作る前に、古い写しを消しておく (作ったあとに消すと、一時的に DB の何倍もの空きが要るため)。
    # 作るあいだも keep - 1 個は残しておくので、作るのに失敗しても前の写しはある
    for old in sorted(folder.glob("research-*.db"), key=lambda p: p.name, reverse=True)[max(keep - 1, 0):]:
        old.unlink(missing_ok=True)
    free = shutil.disk_usage(folder).free
    if free < size * 1.2 + MIN_FREE:
        result = {"ok": False, "at": now, "reason": f"ディスクの空きが足りません (空き {free / 1e9:.1f}GB、写しに約 {size / 1e9:.1f}GB 要る)"}
        log.warning("research.db のバックアップをやめました: %s", result["reason"])
        _save_last(state_dir, result)
        return result

    name = datetime.fromtimestamp(now, JST).strftime("research-%Y%m%d-%H%M.db")  # 日本時間
    tmp = folder / f"{name}.tmp"
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
        os.replace(tmp, folder / name)
    except Exception as e:  # noqa: BLE001 - 失敗しても収集は続ける
        tmp.unlink(missing_ok=True)
        result = {"ok": False, "at": now, "reason": str(e).splitlines()[0] if str(e) else type(e).__name__}
        log.warning("research.db のバックアップに失敗しました: %s", result["reason"])
        _save_last(state_dir, result)
        return result

    # 古い写しを消す (新しい方から keep 個を残す)
    copies = sorted(folder.glob("research-*.db"), key=lambda p: p.name, reverse=True)
    for old in copies[max(keep, 1):]:
        old.unlink(missing_ok=True)
    mb = round((folder / name).stat().st_size / 1e6, 1)
    result = {"ok": True, "at": now, "file": name, "mb": mb, "seconds": round(time.time() - started, 1),
              "copies": min(len(copies), max(keep, 1))}
    log.info("research.db のバックアップを作りました: %s (%.1f MB、%.0f 秒)", name, mb, result["seconds"])
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
    copies = sorted(backup_dir(state_dir).glob("research-*.db"), key=lambda p: p.name, reverse=True)
    if not bucket or not copies:
        return None
    src, now = copies[0], time.time() if now is None else now
    name = f"research-{('mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun')[datetime.fromtimestamp(now, JST).weekday()]}.db"
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


def record_disk(state_dir: Path, now: float | None = None) -> None:
    """ディスクの空きを 1 日 1 回残す (毎日のバックアップの時と、管理画面を開いた時。400 日分)。"""
    now = time.time() if now is None else now
    path = Path(state_dir) / DISK_FILE
    hist = _disk_history(path)
    if hist and now - hist[-1][0] < 86400:
        return
    try:
        hist = (hist + [[now, shutil.disk_usage(state_dir).free]])[-400:]
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
