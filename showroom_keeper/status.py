"""監視中ルームの配信状況 (視聴者数など) を表示する。ブラウザを使わず公開 API だけで取得する。"""

from __future__ import annotations

import json
import time
import urllib.request
from datetime import datetime, timedelta, timezone

from .api import BASE_URL

JST = timezone(timedelta(hours=9))


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
        "viewers": None,
    }
    if info["is_live"] and status.get("room_id"):
        polling = _get(f"/api/live/polling?room_id={status['room_id']}")
        info["viewers"] = polling.get("online_user_num")
    return info


def format_status(info: dict, now: float | None = None) -> str:
    if not info["is_live"]:
        return f"⚪ {info['name']}: 配信していません"
    line = f"🔴 {info['name']}: 視聴者数 {info['viewers']:,} 人" if info["viewers"] is not None else f"🔴 {info['name']}: 配信中"
    if info["started_at"]:
        started = datetime.fromtimestamp(info["started_at"], JST)
        elapsed = int((now or time.time()) - info["started_at"])
        line += f" (開始 {started:%m/%d %H:%M}、経過 {elapsed // 3600} 時間 {elapsed % 3600 // 60} 分)"
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
