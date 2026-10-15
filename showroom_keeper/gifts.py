"""見張っているルームのギフト履歴を記録・集計する。

- 視聴中の keeper が、配信ページの受信するリアルタイム通信のギフト (t=2) をすべて
  <state_dir>/gifts/<ルーム>.jsonl に追記する (無料の星も含む)。
- SHOWROOM の gift_log API (直近 100 件、主に有料ギフト) も合わせて読み、重複を除いて集計する。
"""

from __future__ import annotations

import json
import time
import urllib.request
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from .api import BASE_URL
from .status import JST


def gift_dir(state_dir: Path) -> Path:
    return state_dir / "gifts"


def record_gift(state_dir: Path, room_key: str, live_id: int | None, event: dict[str, Any]) -> None:
    """リアルタイム通信のギフトイベント (t=2) を 1 行追記する。"""
    entry = {
        "at": int(event.get("created_at") or time.time()),
        "live_id": live_id,
        "user_id": event.get("u"),
        "name": event.get("ac"),
        "gift_id": event.get("g"),
        "num": int(event.get("n") or 1),
    }
    path = gift_dir(state_dir) / f"{room_key}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def load_recorded(state_dir: Path, room_key: str) -> list[dict[str, Any]]:
    path = gift_dir(state_dir) / f"{room_key}.jsonl"
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def _get(path: str) -> Any:
    req = urllib.request.Request(f"{BASE_URL}{path}", headers={"User-Agent": "showroom-keeper", "Accept-Language": "ja"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.load(resp)


def from_api_log(log: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {"at": int(g.get("created_at", 0)), "user_id": g.get("user_id"), "name": g.get("name"),
         "gift_id": g.get("gift_id"), "num": int(g.get("num") or 1)}
        for g in log
    ]


def merge(recorded: list[dict[str, Any]], api: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """記録と API の履歴を合わせ、同じギフト (送信者・ギフト・個数・時刻±2秒) の重複を除く。"""
    out = list(recorded)
    seen = {(g["user_id"], g["gift_id"], g["num"]): [] for g in recorded}
    for g in recorded:
        seen[(g["user_id"], g["gift_id"], g["num"])].append(g["at"])
    for g in api:
        times = seen.get((g["user_id"], g["gift_id"], g["num"]), [])
        if not any(abs(t - g["at"]) <= 2 for t in times):
            out.append(g)
    return sorted(out, key=lambda g: g["at"])


def summarize(gifts: list[dict[str, Any]], catalog: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
    """リスナーごとの合計ポイント・個数・回数・内訳。ポイントの多い順。"""
    users: dict[Any, dict[str, Any]] = {}
    for g in gifts:
        info = catalog.get(g["gift_id"], {})
        u = users.setdefault(g["user_id"], {"user_id": g["user_id"], "name": g["name"], "points": 0,
                                             "count": 0, "times": 0, "items": defaultdict(int)})
        u["name"] = g["name"] or u["name"]
        u["points"] += int(info.get("point") or 0) * g["num"]
        u["count"] += g["num"]
        u["times"] += 1
        u["items"][info.get("gift_name") or f"ギフト{g['gift_id']}"] += g["num"]
    return sorted(users.values(), key=lambda u: (u["points"], u["count"]), reverse=True)


def fetch_catalog(room_id: int) -> dict[int, dict[str, Any]]:
    data = _get(f"/api/live/gift_list?room_id={room_id}")
    catalog = {}
    for group in ("normal", "enquete"):
        for g in data.get(group) or []:
            catalog[int(g["gift_id"])] = {"gift_name": g.get("gift_name"), "point": g.get("point") or 0}
    return catalog


def match_user(gifts: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    """ユーザー ID (数字) なら完全一致、それ以外は名前の部分一致で絞り込む。

    名前で探した場合も、見つかったユーザーの ID で集め直すので、途中で名前を変えた分も含まれる。
    """
    query = query.strip()
    if query.isdigit():
        ids = {int(query)}
    else:
        ids = {g["user_id"] for g in gifts if query.lower() in str(g.get("name") or "").lower()}
    return [g for g in gifts if g["user_id"] in ids]


def show_user(gifts: list[dict[str, Any]], catalog: dict[int, dict[str, Any]], query: str) -> None:
    hits = match_user(gifts, query)
    if not hits:
        print(f"   「{query}」に当てはまるリスナーのギフトはありません")
        return
    by_user: dict[Any, list[dict[str, Any]]] = defaultdict(list)
    for g in hits:
        by_user[g["user_id"]].append(g)
    for user_id, items in by_user.items():
        names = list(dict.fromkeys(g["name"] for g in items if g.get("name")))
        total = summarize(items, catalog)[0]
        print(f"\n👤 {' / '.join(names)} (ユーザー ID {user_id})")
        print(f"   合計 {total['points']:,} pt / {total['count']:,} 個 / {total['times']} 回")
        days: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for g in items:
            days[datetime.fromtimestamp(g["at"], JST).strftime("%m/%d")].append(g)
        print("   【日別】")
        for day, day_items in days.items():
            d = summarize(day_items, catalog)[0]
            print(f"     {day}  {d['points']:,} pt / {d['count']:,} 個 ({d['times']} 回)")
        print("   【履歴】")
        for g in items:
            info = catalog.get(g["gift_id"], {})
            gname = info.get("gift_name") or f"ギフト{g['gift_id']}"
            pt = int(info.get("point") or 0) * g["num"]
            print(f"     {datetime.fromtimestamp(g['at'], JST):%m/%d %H:%M:%S}  {gname}×{g['num']}  ({pt:,} pt)")


def show(state_dir: Path, room_key: str, hours: float | None, top: int, log_lines: int,
         user: str | None = None) -> int:
    status = _get(f"/api/room/status?room_url_key={room_key}")
    room_id = status.get("room_id")
    name = status.get("room_name") or room_key
    if not room_id:
        print(f"❌ {room_key}: ルームが見つかりません")
        return 1
    since = time.time() - hours * 3600 if hours else (status.get("started_at") if status.get("is_live") else None)
    recorded = [g for g in load_recorded(state_dir, room_key) if not since or g["at"] >= since]
    api = [g for g in from_api_log(_get(f"/api/live/gift_log?room_id={room_id}").get("gift_log") or [])
           if not since or g["at"] >= since]
    gifts = merge(recorded, api)
    catalog = fetch_catalog(room_id)
    period = ("記録のすべて" if hours and hours >= 1e8 else f"直近 {hours:g} 時間" if hours else
              f"今回の配信 ({datetime.fromtimestamp(since, JST):%m/%d %H:%M} 開始)" if since else "記録のすべて")
    if user:
        print(f"🎁 {name} — 「{user}」のギフト履歴 ({period})")
        show_user(gifts, catalog, user)
        return 0
    print(f"🎁 {name} のギフト履歴 — {period}")
    print(f"   記録 {len(recorded)} 件 + API {len(api)} 件 (重複除外後 {len(gifts)} 件)")
    if not gifts:
        print("   ギフトはありません")
        return 0
    print(f"\n【リスナー別ランキング】 上位 {top} 人")
    for i, u in enumerate(summarize(gifts, catalog)[:top], 1):
        items = "、".join(f"{k}×{v}" for k, v in sorted(u["items"].items(), key=lambda kv: -kv[1])[:3])
        print(f"{i:>3}. {u['name']} [ID {u['user_id']}]  {u['points']:,} pt / {u['count']:,} 個 ({u['times']} 回)  {items}")
    if log_lines:
        print(f"\n【最近のギフト】 {log_lines} 件")
        for g in gifts[-log_lines:]:
            info = catalog.get(g["gift_id"], {})
            gname = info.get("gift_name") or f"ギフト{g['gift_id']}"
            print(f"  {datetime.fromtimestamp(g['at'], JST):%m/%d %H:%M:%S}  {g['name']}  {gname}×{g['num']}")
    return 0
