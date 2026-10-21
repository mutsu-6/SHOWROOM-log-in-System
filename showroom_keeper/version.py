"""動いている版と、GitHub の作業ブランチの最新の版 (管理画面で「更新が残っていないか」を見るため)。

動いている版は、tools/update.sh が作り直す前に showroom_keeper/VERSION に書く (コミットの番号と日時)。
最新の版は、GitHub の公開 API から 1 時間に 1 回だけ、別のスレッドで調べる (画面の表示は待たせない)。
動いている版が変わった時 (自動の更新が、説明書だけの変更で起動し直さずに VERSION だけ進めた時など) は、すぐ調べ直す。
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("version")

VERSION_FILE = Path(__file__).with_name("VERSION")
REPO = "mutsu-6/SHOWROOM-log-in-System"
BRANCH = "claude/showroom-login-persistence-gfdxzo"
CHECK_EVERY = 3600  # GitHub に最新の版を問い合わせる間隔 (秒)
RETRY_AFTER = 600  # 調べられなかった時に、もう一度試すまでの時間 (秒)

# seen = 最後に調べ始めた時の動いている版、for = data を調べた時の動いている版
_latest: dict[str, Any] = {"at": 0.0, "data": None, "checking": False, "seen": None, "for": None}
_lock = threading.Lock()


def current(path: Path = VERSION_FILE) -> dict[str, Any] | None:
    """動いている版 {commit, date}。VERSION が無ければ (tools/update.sh を使わずに作った時) None。"""
    try:
        commit, _, date = path.read_text(encoding="utf-8").strip().partition(" ")
    except OSError:
        return None
    return {"commit": commit[:12], "date": date or None} if commit else None


def _fetch_latest() -> dict[str, Any]:
    url = f"https://api.github.com/repos/{REPO}/commits/{BRANCH}"
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "showroom-keeper"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    commit = data.get("commit") or {}
    return {"commit": str(data.get("sha", ""))[:12], "date": (commit.get("committer") or {}).get("date"),
            "message": str(commit.get("message") or "").splitlines()[0][:120]}


def _check(fetch: Callable[[], dict[str, Any]], seen: str | None) -> None:
    try:
        data = fetch()
        with _lock:
            _latest.update(at=time.time(), data=data, **{"for": seen})
    except Exception as e:  # noqa: BLE001 - 調べられなくても動作には関係ない
        log.info("GitHub で最新の版を調べられませんでした (%s)", str(e).splitlines()[0] if str(e) else type(e).__name__)
        with _lock:
            _latest["at"] = time.time() - CHECK_EVERY + RETRY_AFTER
    finally:
        with _lock:
            _latest["checking"] = False


def info(fetch: Callable[[], dict[str, Any]] = _fetch_latest, wait: bool = False, path: Path = VERSION_FILE) -> dict[str, Any]:
    """{current, latest, outdated}。outdated は、分からない時 None。"""
    cur = current(path)
    seen = cur["commit"] if cur else None
    with _lock:
        changed = seen != _latest["seen"]
        due = not _latest["checking"] and (changed or time.time() - _latest["at"] >= CHECK_EVERY)
        if due:
            _latest.update(checking=True, seen=seen)
    if due:
        t = threading.Thread(target=_check, args=(fetch, seen), daemon=True, name="version")
        t.start()
        if wait:
            t.join()
    with _lock:
        latest, stale = _latest["data"], _latest["for"] != seen
    # 前の版のときに調べた答えではくらべない (「更新が残っています」と間違えて出さない)
    outdated = (None if stale or not cur or not latest or not latest.get("commit")
                else not latest["commit"].startswith(cur["commit"][:7]))
    return {"current": cur, "latest": latest, "outdated": outdated}
