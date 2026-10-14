"""監視中ルームの配信状況を表示する。ブラウザを使わず公開 API だけで取得する。

SHOWROOM は同時接続数を公開していないため、次の 2 つを目安として表示する。
- 盛り上がり度: SHOWROOM が表示している値 (API 上は online_user_num)
- 直近のコメント人数: 直近 10 分間にコメントしたユーザー数 (コメント履歴は最新 100 件までのため、
  100 件が 10 分以内に収まる場合は「以上」と表示する)
"""

from __future__ import annotations

import json
import time
import urllib.request
from datetime import datetime, timedelta, timezone

from .api import BASE_URL

JST = timezone(timedelta(hours=9))
COMMENT_WINDOW = 600  # 直近のコメント人数を数える期間 (秒)


def _get(path: str) -> dict:
    req = urllib.request.Request(f"{BASE_URL}{path}", headers={"User-Agent": "showroom-keeper"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.load(resp)


def fetch(room_url_key: str) -> dict:
    status = _get(f"/api/room/status?room_url_key={room_url_key}")
    info = {
        "key": room_url_key,
        "name": status.get("room_name") or room_url_key,
        "is_live": bool(status.get("is_live")),
        "started_at": status.get("started_at"),
        "excitement": None,
        "commenters": None,
        "commenters_capped": False,
    }
    if info["is_live"] and status.get("room_id"):
        room_id = status["room_id"]
        info["excitement"] = _get(f"/api/live/polling?room_id={room_id}").get("online_user_num")
        log = _get(f"/api/live/comment_log?room_id={room_id}").get("comment_log") or []
        info["commenters"], info["commenters_capped"] = count_recent_commenters(log, time.time())
    return info


def count_recent_commenters(log: list[dict], now: float, window: float = COMMENT_WINDOW) -> tuple[int, bool]:
    """直近 window 秒にコメントしたユーザー数と、履歴の上限で数え切れていない可能性があるか。"""
    recent = {c.get("user_id") for c in log if now - c.get("created_at", 0) <= window}
    oldest = min((c.get("created_at", now) for c in log), default=now)
    capped = len(log) >= 100 and now - oldest < window
    return len(recent), capped


def format_status(info: dict, now: float | None = None) -> str:
    if not info["is_live"]:
        return f"⚪ {info['name']}: 配信していません"
    line = f"🔴 {info['name']}"
    if info["excitement"] is not None:
        line += f"\n   盛り上がり度: {info['excitement']:,}"
    if info["commenters"] is not None:
        more = " 以上" if info["commenters_capped"] else ""
        line += f"\n   直近10分のコメント人数: {info['commenters']} 人{more}"
    if info["started_at"]:
        started = datetime.fromtimestamp(info["started_at"], JST)
        elapsed = int((now or time.time()) - info["started_at"])
        line += f"\n   開始 {started:%m/%d %H:%M} (経過 {elapsed // 3600} 時間 {elapsed % 3600 // 60} 分)"
    return line


def print_status(rooms: list[str]) -> int:
    print(datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S"))
    code = 0
    for key in rooms:
        try:
            print(format_status(fetch(key)))
        except Exception as e:  # noqa: BLE001
            print(f"❌ {key}: 取得できませんでした ({e})")
            code = 1
    return code
