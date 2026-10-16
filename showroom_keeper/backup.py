"""research.db (集めた情報) のバックアップ。

全ルームの収集は全部そろうまで約 2 か月かかるので、壊れたり消えたりした時に集め直さずに済むよう、
毎日 1 回、state_dir/backup/ に写しを作る。書き込み中のファイルをそのままコピーすると壊れた写しになることが
あるため (research.db は WAL を使う)、SQLite のバックアップの仕組み (sqlite3.Connection.backup) で写す。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .research_store import db_path
from .status import JST

log = logging.getLogger("backup")

BACKUP_DIR = "backup"
KEEP = 2  # 残す写しの数 (古いものから消す)
MIN_FREE = 1 << 30  # 写しを作ったあとにも残しておくディスクの空き (1GB)
STEP_PAGES = 4096  # 1 回に写すページ数 (間に休みを入れて、収集の書き込みを止めすぎない)
STEP_SLEEP = 0.02  # ページを写す合間の休み (秒)
LAST_FILE = "last.json"


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
        source = sqlite3.connect(str(src), timeout=60)
        try:
            dest = sqlite3.connect(str(tmp))
            try:
                source.backup(dest, pages=STEP_PAGES, sleep=STEP_SLEEP)
                check = dest.execute("PRAGMA quick_check").fetchone()[0]
            finally:
                dest.close()
        finally:
            source.close()
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
