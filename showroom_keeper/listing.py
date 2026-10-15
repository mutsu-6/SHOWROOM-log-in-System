"""管理画面の一覧 (ルーム・イベント・リスナー) を作る。

ルーム ID やイベントの URL、リスナー ID を毎回入力しなくても選べるよう、
設定・記録済みのギフト・SHOWROOM のイベント一覧から候補を集める。

- ルーム名は記録 (gifts/<ルーム>.jsonl) に入っていないため、視聴・記録・イベント集計のときに
  分かった名前を gifts/_rooms.json に覚えておく
- 記録の読み込みはファイルの更新時刻と大きさで使い回し、画面を開くたびに全ファイルを読み直さない
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from .gifts import contribution_cache, fetch_catalog, gift_dir, gift_label
from .status import JST

ROOM_NAMES_FILE = "_rooms.json"
EVENT_LIST_TTL = 1800  # SHOWROOM のイベント一覧を使い回す秒数
CATALOG_TTL = 86400  # ギフトの名前・ポイント表を使い回す秒数
EVENT_LIST_MAX_PAGES = 20

_lock = threading.Lock()
_names_cache: dict[str, dict[str, Any]] | None = None
_names_path: Path | None = None
_file_cache: dict[Path, tuple[float, int, list[dict[str, Any]]]] = {}
_event_cache: tuple[float, list[dict[str, Any]]] | None = None


# ------------------------------------------------------------------ ルーム名
def _names_file(state_dir: Path) -> Path:
    return gift_dir(state_dir) / ROOM_NAMES_FILE


def room_names(state_dir: Path) -> dict[str, dict[str, Any]]:
    """覚えているルーム名 {room_url_key: {name, room_id}}。"""
    global _names_cache, _names_path
    path = _names_file(state_dir)
    with _lock:
        if _names_cache is None or _names_path != path:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                _names_cache = data if isinstance(data, dict) else {}
            except (OSError, ValueError):
                _names_cache = {}
            _names_path = path
        return dict(_names_cache)


def remember_rooms(state_dir: Path, rooms: dict[str, tuple[str | None, Any]]) -> None:
    """ルーム名と room_id を覚える ({room_url_key: (名前, room_id)})。変わった時だけ書き込む。"""
    current = room_names(state_dir)
    changed = False
    for key, (name, room_id) in rooms.items():
        if not key:
            continue
        old = current.get(key) or {}
        new = {"name": name or old.get("name") or key, "room_id": room_id or old.get("room_id")}
        if new != old:
            current[key] = new
            changed = True
    if not changed:
        return
    global _names_cache
    path = _names_file(state_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps(current, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        return
    with _lock:
        _names_cache = current


NAME_LOOKUP_LIMIT = 40  # 一覧を開いた時に、名前の分からないルームを問い合わせる上限 (1 回あたり)


def fill_unknown_names(state_dir: Path, keys: list[str], get: Any = None, limit: int = NAME_LOOKUP_LIMIT) -> None:
    """名前を覚えていないルームの名前を SHOWROOM に問い合わせて覚える (配信していないルームも分かる)。"""
    from concurrent.futures import ThreadPoolExecutor

    from .gifts import _get

    get = get or _get
    known = room_names(state_dir)
    unknown = [k for k in dict.fromkeys(keys) if k and not (known.get(k) or {}).get("room_id")][:limit]
    if not unknown:
        return

    def one(key: str) -> tuple[str, Any]:
        try:
            return key, get(f"/api/room/status?room_url_key={key}")
        except Exception:  # noqa: BLE001 - 分からなければ room_url_key のまま出す
            return key, None

    with ThreadPoolExecutor(8) as ex:
        found = {key: (st.get("room_name"), st.get("room_id")) for key, st in ex.map(one, unknown)
                 if isinstance(st, dict) and st.get("room_id")}
    remember_rooms(state_dir, found)


# ------------------------------------------------------------------ 記録の読み込み (使い回し)
def _record_files(state_dir: Path) -> dict[str, Path]:
    folder = gift_dir(state_dir)
    if not folder.is_dir():
        return {}
    return {p.stem: p for p in folder.glob("*.jsonl")}


def _load_file(path: Path) -> list[dict[str, Any]]:
    try:
        st = path.stat()
    except OSError:
        return []
    with _lock:
        cached = _file_cache.get(path)
    if cached and cached[0] == st.st_mtime and cached[1] == st.st_size:
        return cached[2]
    rows = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                g = json.loads(line)
            except ValueError:
                continue
            if isinstance(g, dict) and "at" in g and "gift_id" in g:
                rows.append(g)
    except OSError:
        return []
    with _lock:
        _file_cache[path] = (st.st_mtime, st.st_size, rows)
    return rows


# ------------------------------------------------------------------ ルーム一覧
def list_rooms(state_dir: Path, settings: dict[str, list[str]], live: dict[str, str] | None = None,
               lookup: Any = False) -> list[dict[str, Any]]:
    """設定にあるルームと、ギフトの記録があるルームの一覧 (名前・区分・記録件数・最後のギフト)。

    live は配信中と分かっているルーム {room_url_key: 名前} (視聴中・記録中のもの)。
    lookup なら、名前を覚えていないルームを SHOWROOM に問い合わせる (関数を渡すとそれで取得する)。
    """
    live = live or {}
    files = _record_files(state_dir)
    keys = list(dict.fromkeys([*settings.get("rooms", []), *settings.get("record_rooms", []),
                               *settings.get("enter_rooms", []), *live, *files]))
    if lookup:
        fill_unknown_names(state_dir, keys, get=None if lookup is True else lookup)
    names = room_names(state_dir)
    out = []
    for key in keys:
        rows = _load_file(files[key]) if key in files else []
        tags = []
        if key in settings.get("rooms", []):
            tags.append("watch")
        if key in settings.get("record_rooms", []):
            tags.append("record")
        if key in settings.get("enter_rooms", []):
            tags.append("enter")
        if rows:
            tags.append("has_records")
        info = names.get(key) or {}
        out.append({
            "key": key,
            "name": live.get(key) or info.get("name") or key,
            "room_id": info.get("room_id"),
            "tags": tags,
            "is_live": key in live,
            "records": len(rows),
            "last_at": max((g["at"] for g in rows), default=None),
        })
    return out


# ------------------------------------------------------------------ イベント一覧
def _fetch_event_list(get: Any) -> list[dict[str, Any]]:
    global _event_cache
    now = time.time()
    with _lock:
        if _event_cache and now - _event_cache[0] < EVENT_LIST_TTL:
            return _event_cache[1]
    events: list[dict[str, Any]] = []
    page: Any = 1
    seen = set()
    while page and page not in seen and len(seen) < EVENT_LIST_MAX_PAGES:
        seen.add(page)
        data = get(f"/api/event/search?page={page}")
        if not isinstance(data, dict):
            break
        events.extend(e for e in data.get("event_list") or [] if isinstance(e, dict))
        page = data.get("next_page")
    with _lock:
        _event_cache = (now, events)
    return events


def event_state(start: Any, end: Any, now: float) -> str:
    if start and now < float(start):
        return "upcoming"
    if end and now > float(end):
        return "ended"
    return "ongoing"


def list_events(state_dir: Path, configured: list[str], get: Any = None) -> list[dict[str, Any]]:
    """SHOWROOM の公開イベント (開催中・開催前) と、記録の設定にあるイベントの一覧。"""
    from .gifts import _get

    get = get or _get
    now = time.time()
    try:
        public = _fetch_event_list(get)
    except Exception:  # noqa: BLE001 - 取れなくても設定済みのイベントは出す
        public = []
    out: dict[str, dict[str, Any]] = {}
    for e in public:
        key = e.get("event_url_key")
        if not key:
            continue
        out[key] = {
            "key": key, "event_id": e.get("event_id"), "title": e.get("event_name") or key,
            "start": e.get("started_at"), "end": e.get("ended_at"), "image": e.get("image_s") or e.get("image_m"),
            "is_official": bool(e.get("is_official")),
        }
    for key in configured:
        if key not in out:
            try:
                info = get(f"/api/event/{key}/abstraction")
            except Exception:  # noqa: BLE001
                info = {}
            out[key] = {"key": key, "event_id": info.get("event_id"), "title": info.get("title") or key,
                        "start": info.get("event_start_at"), "end": info.get("event_end_at"), "image": None,
                        "is_official": False}
    for e in out.values():
        e["state"] = event_state(e["start"], e["end"], now)
        e["recording"] = e["key"] in configured
        e["official_cached"] = bool(e["event_id"]) and contribution_cache(state_dir, e["event_id"]).exists()
    order = {"ongoing": 0, "upcoming": 1, "ended": 2}
    return sorted(out.values(), key=lambda e: (not e["recording"], order[e["state"]], e["end"] or 0))


# ------------------------------------------------------------------ ギフトのポイント表 (全ルーム共通)
def gift_catalog(state_dir: Path) -> dict[int, dict[str, Any]]:
    """ギフトの名前・ポイント・無料かどうか。1 日 1 回、覚えているルームの 1 つから取り直す。"""
    path = state_dir / "cache" / "gift_catalog.json"
    try:
        cached = json.loads(path.read_text(encoding="utf-8"))
        if time.time() - float(cached["at"]) < CATALOG_TTL:
            return {int(k): v for k, v in cached["gifts"].items()}
    except (OSError, ValueError, KeyError, TypeError):
        cached = None
    room_ids = [v.get("room_id") for v in room_names(state_dir).values() if v.get("room_id")][:3]
    if not room_ids:  # まだルームを覚えていなければ、配信中のルームを 1 つ使う
        try:
            from .gifts import _get

            room_ids = [l["room_id"] for gr in _get("/api/live/onlives").get("onlives", [])
                        for l in gr.get("lives", []) if l.get("room_id")][:1]
        except Exception:  # noqa: BLE001
            room_ids = []
    for room_id in room_ids:
        try:
            catalog = fetch_catalog(int(room_id))
        except Exception:  # noqa: BLE001
            continue
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"at": time.time(), "gifts": catalog}, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass
        return catalog
    if cached:  # 取り直せなければ古い表を使う
        return {int(k): v for k, v in cached.get("gifts", {}).items()}
    return {}


def _points(catalog: dict[int, dict[str, Any]], g: dict[str, Any]) -> tuple[int, bool]:
    info = catalog.get(g.get("gift_id")) or {}
    return int(info.get("point") or 0) * int(g.get("num") or 1), bool(info.get("free", True))


# ------------------------------------------------------------------ リスナー一覧・履歴
def _records(state_dir: Path, room: str | None, since: float | None) -> list[tuple[str, dict[str, Any]]]:
    files = _record_files(state_dir)
    keys = [room] if room else list(files)
    out = []
    for key in keys:
        if key not in files:
            continue
        for g in _load_file(files[key]):
            if since is None or g["at"] >= since:
                out.append((key, g))
    return out


def list_listeners(state_dir: Path, days: float | None = None, room: str | None = None) -> dict[str, Any]:
    """記録したギフトのリスナー一覧 (全ルームまたは 1 ルーム)。ポイントの多い順。"""
    since = time.time() - days * 86400 if days else None
    catalog = gift_catalog(state_dir)
    names = room_names(state_dir)
    users: dict[Any, dict[str, Any]] = {}
    for key, g in _records(state_dir, room, since):
        uid = g.get("user_id")
        if uid is None:
            continue
        u = users.setdefault(uid, {"user_id": uid, "name": g.get("name"), "names": {}, "points": 0, "paid_points": 0,
                                   "free_points": 0, "count": 0, "times": 0, "rooms": defaultdict(int),
                                   "first_at": g["at"], "last_at": g["at"]})
        pt, free = _points(catalog, g)
        u["points"] += pt
        u["free_points" if free else "paid_points"] += pt
        u["count"] += int(g.get("num") or 1)
        u["times"] += 1
        u["rooms"][key] += pt
        if g.get("name"):
            u["names"][g["name"]] = max(u["names"].get(g["name"], 0), g["at"])
        if g["at"] >= u["last_at"]:
            u["last_at"], u["name"] = g["at"], g.get("name") or u["name"]
        u["first_at"] = min(u["first_at"], g["at"])
    out = []
    for u in users.values():
        rooms = sorted(u["rooms"].items(), key=lambda kv: -kv[1])
        out.append({**{k: u[k] for k in ("user_id", "name", "points", "paid_points", "free_points", "count", "times",
                                          "first_at", "last_at")},
                    "names": [n for n, _ in sorted(u["names"].items(), key=lambda kv: -kv[1])],
                    "room_count": len(rooms),
                    "rooms": [{"key": k, "name": (names.get(k) or {}).get("name") or k, "points": p} for k, p in rooms[:5]]})
    out.sort(key=lambda u: (u["points"], u["times"]), reverse=True)
    return {"listeners": out, "total": len(out), "catalog": bool(catalog)}


def listener_detail(state_dir: Path, user_id: Any, days: float | None = None) -> dict[str, Any]:
    """1 人のリスナーの、記録した全ルームでの履歴 (ルーム別・日別・一覧)。"""
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        raise ValueError("ユーザー ID は数字で指定してください") from None
    since = time.time() - days * 86400 if days else None
    catalog = gift_catalog(state_dir)
    names = room_names(state_dir)
    hits = [(key, g) for key, g in _records(state_dir, None, since) if g.get("user_id") == uid]
    if not hits:
        return {"user_id": uid, "found": False}
    hits.sort(key=lambda kg: kg[1]["at"])
    total = {"points": 0, "paid_points": 0, "free_points": 0, "count": 0, "times": 0}
    rooms: dict[str, dict[str, Any]] = {}
    days_map: dict[str, dict[str, int]] = defaultdict(lambda: {"points": 0, "times": 0})
    history = []
    for key, g in hits:
        pt, free = _points(catalog, g)
        for bucket in (total, rooms.setdefault(key, {"key": key, "name": (names.get(key) or {}).get("name") or key,
                                                      "points": 0, "paid_points": 0, "free_points": 0, "count": 0,
                                                      "times": 0, "last_at": 0})):
            bucket["points"] += pt
            bucket["free_points" if free else "paid_points"] += pt
            bucket["count"] += int(g.get("num") or 1)
            bucket["times"] += 1
        rooms[key]["last_at"] = g["at"]
        day = datetime.fromtimestamp(g["at"], JST).strftime("%m/%d")
        days_map[day]["points"] += pt
        days_map[day]["times"] += 1
        history.append({"at": g["at"], "room": key, "room_name": rooms[key]["name"], "name": g.get("name"),
                        "gift": gift_label(catalog, g.get("gift_id")), "num": int(g.get("num") or 1), "points": pt})
    return {
        "user_id": uid, "found": True,
        "names": list(dict.fromkeys(g.get("name") for _, g in reversed(hits) if g.get("name"))),
        "total": total,
        "rooms": sorted(rooms.values(), key=lambda r: -r["points"]),
        "days": [{"day": d, **v} for d, v in days_map.items()],
        "history": list(reversed(history[-500:])),
        "first_at": hits[0][1]["at"], "last_at": hits[-1][1]["at"],
    }
