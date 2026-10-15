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
from concurrent.futures import ThreadPoolExecutor
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
                                             "paid_points": 0, "free_points": 0,
                                             "count": 0, "times": 0, "items": defaultdict(int)})
        u["name"] = g["name"] or u["name"]
        pt = int(info.get("point") or 0) * g["num"]
        u["points"] += pt
        # 無料ギフト (星・種など) かどうかはギフト一覧の free で分かる。不明なギフトは有料に含めない
        u["free_points" if info.get("free", True) else "paid_points"] += pt
        u["count"] += g["num"]
        u["times"] += 1
        u["items"][info.get("gift_name") or f"ギフト{g['gift_id']}"] += g["num"]
    return sorted(users.values(), key=lambda u: (u["points"], u["count"]), reverse=True)


def fetch_catalog(room_id: int) -> dict[int, dict[str, Any]]:
    data = _get(f"/api/live/gift_list?room_id={room_id}")
    catalog = {}
    for group in ("normal", "enquete"):
        for g in data.get(group) or []:
            catalog[int(g["gift_id"])] = {"gift_name": g.get("gift_name"), "point": g.get("point") or 0,
                                          "free": bool(g.get("free"))}
    return catalog


def split_points(u: dict[str, Any]) -> str:
    """「有料 X / 無料 Y」の表示。"""
    return f"有料 {u['paid_points']:,} / 無料 {u['free_points']:,}"


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
        print(f"   合計 {total['points']:,} pt ({split_points(total)}) / {total['count']:,} 個 / {total['times']} 回")
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
            where = f"  @{g['room']}" if g.get("room") else ""
            print(f"     {datetime.fromtimestamp(g['at'], JST):%m/%d %H:%M:%S}  {gname}×{g['num']}  ({pt:,} pt){where}")


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
        print(f"{i:>3}. {u['name']} [ID {u['user_id']}]  {u['points']:,} pt ({split_points(u)}) / {u['count']:,} 個 ({u['times']} 回)  {items}")
    if log_lines:
        print(f"\n【最近のギフト】 {log_lines} 件")
        for g in gifts[-log_lines:]:
            info = catalog.get(g["gift_id"], {})
            gname = info.get("gift_name") or f"ギフト{g['gift_id']}"
            print(f"  {datetime.fromtimestamp(g['at'], JST):%m/%d %H:%M:%S}  {g['name']}  {gname}×{g['num']}")
    return 0


CONTRIB_WORKERS = 8  # 公式の貢献ランキングを同時に取得するルーム数
CONTRIB_CACHE_TTL = 600  # 開催中のイベントの貢献ランキングを使い回す秒数 (終了済みは確定値なので期限なし)


def contribution_cache(state_dir: Path, event_id: Any) -> Path:
    return state_dir / "cache" / f"contribution_{event_id}.json"


def cached_contributions(state_dir: Path, event_id: Any, now: float | None = None) -> list[dict[str, Any]] | None:
    """保存済みの貢献ランキング。無いか期限切れなら None。"""
    now = time.time() if now is None else now
    try:
        cached = json.loads(contribution_cache(state_dir, event_id).read_text(encoding="utf-8"))
        if cached.get("final") or now - float(cached["at"]) < CONTRIB_CACHE_TTL:
            return cached["rows"]
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def fetch_contributions(state_dir: Path, event_id: Any, rooms: list[dict[str, Any]], final: bool,
                        fetch: Any = None, now: float | None = None) -> tuple[list[dict[str, Any]], int]:
    """SHOWROOM が公開しているイベントの貢献ランキング (ルームごとに上位 100 人) を全ルーム分集める。

    記録を始める前の分や keeper を止めていた間の分も含まれる。戻り値は (行の一覧, 取得に失敗したルーム数)。
    取得し直すと参加ルーム数に応じて数十秒かかるため、ファイルに保存して使い回す。
    """
    cached = cached_contributions(state_dir, event_id, now)
    if cached is not None:
        return cached, 0
    now = time.time() if now is None else now
    path = contribution_cache(state_dir, event_id)
    get = fetch or _get

    def one(room: dict[str, Any]) -> list[dict[str, Any]] | None:
        name = room.get("room_name") or room.get("room_url_key")
        try:
            data = get(f"/api/event/contribution_ranking?event_id={event_id}&room_id={room['room_id']}")
        except Exception:  # noqa: BLE001 - 失敗したルームは数えて知らせる
            return None
        return [{"room": name, "rank": r.get("rank"), "user_id": r.get("user_id"), "name": r.get("name"),
                 "point": int(r.get("point") or 0)}
                for r in (data.get("ranking") or []) if isinstance(r, dict)]

    targets = [r for r in rooms if r.get("room_id")]
    with ThreadPoolExecutor(CONTRIB_WORKERS) as ex:
        results = list(ex.map(one, targets))
    failed = sum(1 for r in results if r is None)
    rows = [row for r in results if r for row in r]
    if not failed:  # 一部が欠けた結果は保存しない (終了済みイベントで欠けたまま固定されないように)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"at": now, "final": final, "rows": rows}, ensure_ascii=False), encoding="utf-8")
    return rows, failed


def summarize_official(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """公式の貢献ランキングをリスナーごとに合計する (どのルームに何 pt か)。ポイントの多い順。"""
    users: dict[Any, dict[str, Any]] = {}
    for r in rows:
        u = users.setdefault(r["user_id"], {"user_id": r["user_id"], "name": r["name"], "points": 0, "rooms": []})
        u["name"] = r["name"] or u["name"]
        u["points"] += r["point"]
        u["rooms"].append((r["room"], r["point"]))
    for u in users.values():
        u["rooms"].sort(key=lambda x: -x[1])
    return sorted(users.values(), key=lambda u: u["points"], reverse=True)


def _rooms_text(rooms: list[str]) -> str:
    return "、".join(rooms[:3]) + (f" ほか {len(rooms) - 3}" if len(rooms) > 3 else "")


def show_official_user(rows: list[dict[str, Any]], query: str) -> None:
    query = query.strip()
    hits = [r for r in rows if (str(r["user_id"]) == query if query.isdigit()
                                else query.lower() in str(r.get("name") or "").lower())]
    print("\n【公式の貢献ランキング】 SHOWROOM 発表 (各ルーム上位 100 人)")
    if not hits:
        print(f"   「{query}」は公式の貢献ランキングに見つかりません (どのルームでも上位 100 人に入っていない可能性)")
        return
    for u in summarize_official(hits):
        print(f"   👤 {u['name']} (ユーザー ID {u['user_id']})  合計 {u['points']:,} pt")
        for room, pt in u["rooms"]:
            rank = next((r["rank"] for r in hits if r["user_id"] == u["user_id"] and r["room"] == room), None)
            print(f"     {room}  {pt:,} pt ({rank} 位)")


def show_event(state_dir: Path, event_key: str, top: int, user: str | None = None, official: bool = True) -> int:
    """イベント参加ルーム全体の記録を集計する (イベント期間のギフトのみ)。

    official なら SHOWROOM 公式の貢献ランキングも合わせて表示し、記録の取りこぼしを確認できるようにする。
    """
    info = _get(f"/api/event/{event_key}/abstraction")
    start, end = info.get("event_start_at") or 0, info.get("event_end_at") or time.time()
    rooms, page = [], 1
    while page:
        data = _get(f"/api/event/{event_key}/rooms?page={page}")
        rooms.extend(data.get("rooms") or [])
        page = data.get("next_page") if data.get("next_page") and data.get("next_page") != page else None
    gifts: list[dict[str, Any]] = []
    per_room: list[tuple[str, list[dict[str, Any]]]] = []
    for r in rooms:
        key = r.get("room_url_key")
        items = [dict(g, room=r.get("room_name") or key) for g in load_recorded(state_dir, key)
                 if start <= g["at"] <= end + 600] if key else []
        gifts.extend(items)
        per_room.append((r.get("room_name") or key, items))
    catalog: dict[int, dict[str, Any]] = {}
    for r in rooms[:3]:  # ギフト名・ポイントの対応表 (ルーム間で共通)
        try:
            catalog.update(fetch_catalog(r["room_id"]))
        except Exception:  # noqa: BLE001
            continue
    title = info.get("title") or event_key
    print(f"🏆 {title}")
    print(f"   期間 {datetime.fromtimestamp(start, JST):%m/%d %H:%M} 〜 {datetime.fromtimestamp(end, JST):%m/%d %H:%M}"
          f" / 参加 {len(rooms)} ルーム / 記録 {len(gifts)} 件")

    rows: list[dict[str, Any]] = []
    if official and info.get("event_id"):
        final = time.time() > end + 86400  # 終了から 1 日たてば確定値として保存し続ける
        if cached_contributions(state_dir, info["event_id"]) is None:
            print(f"   公式の貢献ランキングを取得しています ({len(rooms)} ルーム。数十秒かかることがあります)…", flush=True)
        rows, failed = fetch_contributions(state_dir, info["event_id"], rooms, final)
        if failed:
            print(f"   ⚠️ {failed} ルームの貢献ランキングを取得できませんでした (その分は含まれていません)")

    if user:
        if gifts:
            show_user(sorted(gifts, key=lambda g: g["at"]), catalog, user)
        if rows:
            show_official_user(rows, user)
        if not gifts and not rows:
            print("   記録されたギフトはまだありません")
        return 0

    if gifts:
        print(f"\n【ルーム別 (記録)】 上位 {top}")
        room_rank = sorted(((name, summarize(items, catalog)) for name, items in per_room if items),
                           key=lambda x: sum(u["points"] for u in x[1]), reverse=True)
        for i, (name, users) in enumerate(room_rank[:top], 1):
            pts = sum(u["points"] for u in users)
            paid = sum(u["paid_points"] for u in users)
            print(f"{i:>3}. {name}  {pts:,} pt (有料 {paid:,} / 無料 {pts - paid:,}、リスナー {len(users)} 人)")
        print(f"\n【リスナー別 (記録・イベント全体)】 上位 {top} 人")
        for i, u in enumerate(summarize(gifts, catalog)[:top], 1):
            rooms_of = sorted({g["room"] for g in gifts if g["user_id"] == u["user_id"]})
            print(f"{i:>3}. {u['name']} [ID {u['user_id']}]  {u['points']:,} pt ({split_points(u)}) / {u['count']:,} 個"
                  f"  ({_rooms_text(rooms_of)})")
    elif not rows:
        print("   記録されたギフトはまだありません (記録は設定後に配信したルームから始まります)")

    if rows:
        recorded_pts = {u["user_id"]: u["points"] for u in summarize(gifts, catalog)} if gifts else {}
        ranking = summarize_official(rows)
        print(f"\n【公式の貢献ランキング (イベント全体)】 上位 {top} 人"
              f"  ※ SHOWROOM 発表の各ルーム上位 100 人の合計。記録を始める前の分も含む")
        for i, u in enumerate(ranking[:top], 1):
            mine = f" / 記録 {recorded_pts[u['user_id']]:,} pt" if u["user_id"] in recorded_pts else ""
            print(f"{i:>3}. {u['name']} [ID {u['user_id']}]  {u['points']:,} pt{mine}"
                  f"  ({_rooms_text([room for room, _ in u['rooms']])})")
        print(f"   公式の貢献者 {len(ranking):,} 人 / 計 {sum(r['point'] for r in rows):,} pt")
        if gifts:
            # 記録のあるルームについて、公式の合計と比べて取りこぼしの目安を出す
            recorded_rooms = {g["room"] for g in gifts}
            official_pts = sum(r["point"] for r in rows if r["room"] in recorded_rooms)
            mine_pts = sum(u["points"] for u in summarize(gifts, catalog))
            if official_pts:
                print(f"   記録のある {len(recorded_rooms)} ルームでは、記録 {mine_pts:,} pt / 公式 {official_pts:,} pt"
                      f" ({mine_pts / official_pts:.0%})。差は記録を始める前の分・停止中の分など")
    return 0
