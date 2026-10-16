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


def gift_label(catalog: dict[int, dict[str, Any]], gift_id: Any) -> str:
    return (catalog.get(gift_id) or {}).get("gift_name") or f"ギフト{gift_id}"


def _rank_row(u: dict[str, Any]) -> dict[str, Any]:
    """summarize の 1 人分を、画面表示・JSON 用の形にする (内訳は多い順)。"""
    items = sorted(u["items"].items(), key=lambda kv: -kv[1])
    return {k: u[k] for k in ("user_id", "name", "points", "paid_points", "free_points", "count", "times")} | {
        "items": [[k, v] for k, v in items]}


def user_history(gifts: list[dict[str, Any]], catalog: dict[int, dict[str, Any]], query: str) -> list[dict[str, Any]]:
    """特定リスナーの履歴 (合計・日別・一覧)。名前で探した場合も、見つかった ID の改名前後をまとめる。"""
    by_user: dict[Any, list[dict[str, Any]]] = defaultdict(list)
    for g in match_user(gifts, query):
        by_user[g["user_id"]].append(g)
    out = []
    for user_id, items in by_user.items():
        days: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for g in items:
            days[datetime.fromtimestamp(g["at"], JST).strftime("%m/%d")].append(g)
        out.append({
            "user_id": user_id,
            "names": list(dict.fromkeys(g["name"] for g in items if g.get("name"))),
            "total": _rank_row(summarize(items, catalog)[0]),
            "days": [{"day": day, **_rank_row(summarize(d, catalog)[0])} for day, d in days.items()],
            "history": [{"at": g["at"], "gift": gift_label(catalog, g["gift_id"]), "num": g["num"],
                         "points": int((catalog.get(g["gift_id"]) or {}).get("point") or 0) * g["num"],
                         "room": g.get("room")} for g in items],
        })
    return out


def show_user(gifts: list[dict[str, Any]], catalog: dict[int, dict[str, Any]], query: str) -> None:
    users = user_history(gifts, catalog, query)
    if not users:
        print(f"   「{query}」に当てはまるリスナーのギフトはありません")
        return
    for u in users:
        _print_user(u)


def _print_user(u: dict[str, Any]) -> None:
    total = u["total"]
    print(f"\n👤 {' / '.join(u['names'])} (ユーザー ID {u['user_id']})")
    print(f"   合計 {total['points']:,} pt ({split_points(total)}) / {total['count']:,} 個 / {total['times']} 回")
    print("   【日別】")
    for d in u["days"]:
        print(f"     {d['day']}  {d['points']:,} pt / {d['count']:,} 個 ({d['times']} 回)")
    print("   【履歴】")
    for h in u["history"]:
        where = f"  @{h['room']}" if h.get("room") else ""
        print(f"     {datetime.fromtimestamp(h['at'], JST):%m/%d %H:%M:%S}  {h['gift']}×{h['num']}  ({h['points']:,} pt){where}")


def _remember(rooms: dict[str, tuple[Any, Any]], state_dir: Path) -> None:
    """管理画面の一覧に名前で出せるよう、分かったルーム名を覚えておく。"""
    try:
        from .listing import remember_rooms

        remember_rooms(state_dir, rooms)
    except Exception:  # noqa: BLE001
        pass


def room_report(state_dir: Path, room_key: str, hours: float | None = None, user: str | None = None) -> dict[str, Any]:
    """1 ルームのギフト履歴の集計 (CLI と管理画面で共通)。hours が None なら今回の配信、1e8 以上なら記録のすべて。"""
    status = _get(f"/api/room/status?room_url_key={room_key}")
    room_id = status.get("room_id")
    name = status.get("room_name") or room_key
    if not room_id:
        raise LookupError(f"{room_key}: ルームが見つかりません")
    all_records = bool(hours and hours >= 1e8)
    since = (None if all_records else time.time() - hours * 3600 if hours
             else status.get("started_at") if status.get("is_live") else None)
    recorded = [g for g in load_recorded(state_dir, room_key) if not since or g["at"] >= since]
    api = [g for g in from_api_log(_get(f"/api/live/gift_log?room_id={room_id}").get("gift_log") or [])
           if not since or g["at"] >= since]
    gifts = merge(recorded, api)
    catalog = fetch_catalog(room_id)
    _remember({room_key: (name, room_id)}, state_dir)
    period = ("記録のすべて" if all_records else f"直近 {hours:g} 時間" if hours else
              f"今回の配信 ({datetime.fromtimestamp(since, JST):%m/%d %H:%M} 開始)" if since else "記録のすべて")
    report: dict[str, Any] = {
        "room": room_key, "name": name, "is_live": bool(status.get("is_live")), "period": period,
        "recorded": len(recorded), "api": len(api), "total": len(gifts),
        "ranking": [_rank_row(u) for u in summarize(gifts, catalog)],
        "recent": [{"at": g["at"], "user_id": g["user_id"], "name": g["name"], "gift": gift_label(catalog, g["gift_id"]),
                    "num": g["num"]} for g in reversed(gifts[-200:])],
    }
    if user:
        report["user"] = {"query": user, "users": user_history(gifts, catalog, user)}
    return report


def show(state_dir: Path, room_key: str, hours: float | None, top: int, log_lines: int,
         user: str | None = None) -> int:
    try:
        r = room_report(state_dir, room_key, hours, user)
    except LookupError as e:
        print(f"❌ {e}")
        return 1
    if user:
        print(f"🎁 {r['name']} — 「{user}」のギフト履歴 ({r['period']})")
        if not r["user"]["users"]:
            print(f"   「{user}」に当てはまるリスナーのギフトはありません")
        for u in r["user"]["users"]:
            _print_user(u)
        return 0
    print(f"🎁 {r['name']} のギフト履歴 — {r['period']}")
    print(f"   記録 {r['recorded']} 件 + API {r['api']} 件 (重複除外後 {r['total']} 件)")
    if not r["total"]:
        print("   ギフトはありません")
        return 0
    print(f"\n【リスナー別ランキング】 上位 {top} 人")
    for i, u in enumerate(r["ranking"][:top], 1):
        items = "、".join(f"{k}×{v}" for k, v in u["items"][:3])
        print(f"{i:>3}. {u['name']} [ID {u['user_id']}]  {u['points']:,} pt ({split_points(u)}) / {u['count']:,} 個 ({u['times']} 回)  {items}")
    if log_lines:
        print(f"\n【最近のギフト】 {log_lines} 件")
        for g in reversed(r["recent"][:log_lines]):
            print(f"  {datetime.fromtimestamp(g['at'], JST):%m/%d %H:%M:%S}  {g['name']}  {g['gift']}×{g['num']}")
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


def official_user(rows: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    """公式の貢献ランキングから特定リスナーを探す (数字はユーザー ID、それ以外は名前の部分一致)。"""
    query = query.strip()
    hits = [r for r in rows if (str(r["user_id"]) == query if query.isdigit()
                                else query.lower() in str(r.get("name") or "").lower())]
    out = []
    for u in summarize_official(hits):
        ranks = {r["room"]: r["rank"] for r in hits if r["user_id"] == u["user_id"]}
        out.append(dict(u, rooms=[[room, pt, ranks.get(room)] for room, pt in u["rooms"]]))
    return out


def event_report(state_dir: Path, event_key: str, user: str | None = None, official: bool = True,
                 progress: Any = None) -> dict[str, Any]:
    """イベント参加ルーム全体の集計 (CLI と管理画面で共通)。

    official なら SHOWROOM 公式の貢献ランキングも集め、記録の取りこぼしを確認できるようにする。
    progress は公式ランキングを取得し直す時に呼ばれる (時間がかかることを知らせる用)。
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
    _remember({r["room_url_key"]: (r.get("room_name"), r.get("room_id")) for r in rooms if r.get("room_url_key")}, state_dir)

    rows: list[dict[str, Any]] = []
    failed = 0
    if official and info.get("event_id"):
        final = time.time() > end + 86400  # 終了から 1 日たてば確定値として保存し続ける
        if progress and cached_contributions(state_dir, info["event_id"]) is None:
            progress(len(rooms))
        rows, failed = fetch_contributions(state_dir, info["event_id"], rooms, final)

    listeners = summarize(gifts, catalog)
    room_rank = []
    for name, items in per_room:
        if items:
            users = summarize(items, catalog)
            pts = sum(u["points"] for u in users)
            paid = sum(u["paid_points"] for u in users)
            room_rank.append({"room": name, "points": pts, "paid_points": paid, "free_points": pts - paid,
                              "listeners": len(users)})
    room_rank.sort(key=lambda r: r["points"], reverse=True)
    report: dict[str, Any] = {
        "event": event_key, "title": info.get("title") or event_key, "start": start, "end": end,
        "rooms": len(rooms), "recorded": len(gifts), "failed": failed, "official_enabled": bool(rows),
        "room_ranking": room_rank,
        "listener_ranking": [dict(_rank_row(u), rooms=sorted({g["room"] for g in gifts if g["user_id"] == u["user_id"]}))
                             for u in listeners],
        "official_ranking": [],
    }
    if rows:
        recorded_pts = {u["user_id"]: u["points"] for u in listeners}
        report["official_ranking"] = [dict(u, recorded=recorded_pts.get(u["user_id"]), rooms=[room for room, _ in u["rooms"]])
                                      for u in summarize_official(rows)]
        report["official_total"] = sum(r["point"] for r in rows)
        if gifts:
            # 記録のあるルームについて、公式の合計と比べて取りこぼしの目安を出す
            recorded_rooms = {g["room"] for g in gifts}
            report["coverage"] = {"rooms": len(recorded_rooms), "recorded": sum(u["points"] for u in listeners),
                                  "official": sum(r["point"] for r in rows if r["room"] in recorded_rooms)}
    if user:
        report["user"] = {"query": user, "users": user_history(sorted(gifts, key=lambda g: g["at"]), catalog, user),
                          "official": official_user(rows, user) if rows else []}
    return report


def show_event(state_dir: Path, event_key: str, top: int, user: str | None = None, official: bool = True) -> int:
    """イベント参加ルーム全体の記録を表示する (イベント期間のギフトのみ)。"""
    def progress(n: int) -> None:
        print(f"   公式の貢献ランキングを取得しています ({n} ルーム。数十秒かかることがあります)…", flush=True)

    r = event_report(state_dir, event_key, user, official, progress)
    print(f"🏆 {r['title']}")
    print(f"   期間 {datetime.fromtimestamp(r['start'], JST):%m/%d %H:%M} 〜 {datetime.fromtimestamp(r['end'], JST):%m/%d %H:%M}"
          f" / 参加 {r['rooms']} ルーム / 記録 {r['recorded']} 件")
    if r["failed"]:
        print(f"   ⚠️ {r['failed']} ルームの貢献ランキングを取得できませんでした (その分は含まれていません)")

    if user:
        u = r["user"]
        if r["recorded"]:
            if not u["users"]:
                print(f"   「{user}」に当てはまるリスナーのギフトはありません")
            for one in u["users"]:
                _print_user(one)
        if r["official_enabled"]:
            print("\n【公式の貢献ランキング】 SHOWROOM 発表 (各ルーム上位 100 人)")
            if not u["official"]:
                print(f"   「{user}」は公式の貢献ランキングに見つかりません (どのルームでも上位 100 人に入っていない可能性)")
            for one in u["official"]:
                print(f"   👤 {one['name']} (ユーザー ID {one['user_id']})  合計 {one['points']:,} pt")
                for room, pt, rank in one["rooms"]:
                    print(f"     {room}  {pt:,} pt ({rank} 位)")
        if not r["recorded"] and not r["official_enabled"]:
            print("   記録されたギフトはまだありません")
        return 0

    if r["recorded"]:
        print(f"\n【ルーム別 (記録)】 上位 {top}")
        for i, room in enumerate(r["room_ranking"][:top], 1):
            print(f"{i:>3}. {room['room']}  {room['points']:,} pt (有料 {room['paid_points']:,} / 無料 {room['free_points']:,}、"
                  f"リスナー {room['listeners']} 人)")
        print(f"\n【リスナー別 (記録・イベント全体)】 上位 {top} 人")
        for i, u in enumerate(r["listener_ranking"][:top], 1):
            print(f"{i:>3}. {u['name']} [ID {u['user_id']}]  {u['points']:,} pt ({split_points(u)}) / {u['count']:,} 個"
                  f"  ({_rooms_text(u['rooms'])})")
    elif not r["official_enabled"]:
        print("   記録されたギフトはまだありません (記録は設定後に配信したルームから始まります)")

    if r["official_enabled"]:
        print(f"\n【公式の貢献ランキング (イベント全体)】 上位 {top} 人"
              f"  ※ SHOWROOM 発表の各ルーム上位 100 人の合計。記録を始める前の分も含む")
        for i, u in enumerate(r["official_ranking"][:top], 1):
            mine = f" / 記録 {u['recorded']:,} pt" if u["recorded"] is not None else ""
            print(f"{i:>3}. {u['name']} [ID {u['user_id']}]  {u['points']:,} pt{mine}  ({_rooms_text(u['rooms'])})")
        print(f"   公式の貢献者 {len(r['official_ranking']):,} 人 / 計 {r['official_total']:,} pt")
        c = r.get("coverage")
        if c and c["official"]:
            print(f"   記録のある {c['rooms']} ルームでは、記録 {c['recorded']:,} pt / 公式 {c['official']:,} pt"
                  f" ({c['recorded'] / c['official']:.0%})。差は記録を始める前の分・停止中の分など")
    return 0
