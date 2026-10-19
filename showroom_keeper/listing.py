"""管理画面の一覧 (ルーム・イベント・リスナー) を作る。

ルーム ID やイベントの URL、リスナー ID を毎回入力しなくても選べるよう、
設定・記録済みのギフトに加えて、SHOWROOM 全体 (配信中の全ルーム・ルーム検索・
開催中/開催前/終了したイベント・ユーザー ID のプロフィール) から候補を集める。

- ルーム名は記録 (gifts/<ルーム>.jsonl) に入っていないため、視聴・記録・イベント集計のときに
  分かった名前を gifts/_rooms.json に覚えておく
- 記録の読み込みはファイルの更新時刻と大きさで使い回し、画面を開くたびに全ファイルを読み直さない
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from . import memory
from .gifts import contribution_cache, fetch_catalog, gift_dir, gift_label
from .schedule import parse_slots, previous_time
from .status import JST

log = logging.getLogger("web")

ROOM_NAMES_FILE = "_rooms.json"
EVENT_LIST_TTL = 1800  # SHOWROOM のイベント一覧を使い回す秒数
CATALOG_TTL = 86400  # ギフトの名前・ポイント表を使い回す秒数
EVENT_LIST_MAX_PAGES = 20  # 状態ごとの上限 (1 ページ 30 件)
EVENT_STATUSES = {1: "ongoing", 3: "upcoming", 4: "ended"}  # /api/event/search の status (終了は約 1 か月分)
LIVE_TTL = 120  # 配信中の全ルーム (onlives) を使い回す秒数
SEARCH_TTL = 600  # ルーム検索の結果を使い回す秒数
SEARCH_CACHE_MAX = 200
USER_TTL = 3600  # ユーザーのプロフィールを使い回す秒数

NAME_MISS_TTL = 3600  # SHOWROOM に無かったルームを問い合わせ直さない秒数
NAME_ERROR_TTL = 300  # 問い合わせに失敗したルームを問い合わせ直さない秒数
CATALOG_ROOMS_PER_CALL = 10  # 1 回の集計で、ギフトの一覧を新しく取りに行くルームの上限

_lock = threading.Lock()
_names_write_lock = threading.Lock()
_names_cache: dict[str, dict[str, Any]] | None = None
_names_path: Path | None = None
_names_mtime: float | None = None
_name_misses: dict[str, float] = {}  # {room_url_key: 次に問い合わせてよい時刻}
_file_cache: dict[Path, tuple[float, int, list[dict[str, Any]]]] = {}
# 記録を覚えておく行数の上限 (グリッターのルームまで記録するとファイルが多いので、メモリ 2GB を使い切らないように。
# 超えたら、長く使っていないファイルから忘れ、次に要る時に読み直す)
FILE_CACHE_ROWS = 200_000  # (2026-10-19 に 60 万行から下げた。1 行が 0.5KB ほどになるので、60 万行で 300MB を超える)
_event_cache: tuple[float, list[dict[str, Any]]] | None = None
_live_cache: tuple[float, dict[str, dict[str, Any]]] | None = None
_search_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_user_cache: dict[int, tuple[float, dict[str, Any] | None]] = {}


# ------------------------------------------------------------------ ルーム名
def _names_file(state_dir: Path) -> Path:
    return gift_dir(state_dir) / ROOM_NAMES_FILE


def _mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def room_names(state_dir: Path) -> dict[str, dict[str, Any]]:
    """覚えているルーム名 {room_url_key: {name, room_id}}。別のプロセスが書き換えたら読み直す。"""
    global _names_cache, _names_path, _names_mtime
    path = _names_file(state_dir)
    mtime = _mtime(path)
    with _lock:
        if _names_cache is None or _names_path != path or _names_mtime != mtime:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                _names_cache = data if isinstance(data, dict) else {}
            except (OSError, ValueError):
                _names_cache = {}
            _names_path, _names_mtime = path, mtime
        return dict(_names_cache)


def _merge_rooms(current: dict[str, dict[str, Any]], rooms: dict[str, tuple[str | None, Any]]) -> bool:
    changed = False
    for key, (name, room_id) in rooms.items():
        if not key:
            continue
        old = current.get(key) or {}
        new = {"name": name or old.get("name") or key, "room_id": room_id or old.get("room_id")}
        if new != old:
            current[key] = new
            changed = True
    return changed


def remember_rooms(state_dir: Path, rooms: dict[str, tuple[str | None, Any]]) -> None:
    """ルーム名と room_id を覚える ({room_url_key: (名前, room_id)})。変わった時だけ書き込む。"""
    if not _merge_rooms(room_names(state_dir), rooms):
        return
    global _names_cache, _names_mtime
    path = _names_file(state_dir)
    # 同時に書くと片方の名前が消えるので、書く直前の内容に足して書く
    with _names_write_lock:
        current = room_names(state_dir)
        if not _merge_rooms(current, rooms):
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
            tmp.write_text(json.dumps(current, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            return
        with _lock:
            _names_cache, _names_mtime = current, _mtime(path)


NAME_LOOKUP_LIMIT = 40  # 一覧を開いた時に、名前の分からないルームを問い合わせる上限 (1 回あたり)


def fill_unknown_names(state_dir: Path, keys: list[str], get: Any = None, limit: int = NAME_LOOKUP_LIMIT) -> None:
    """名前を覚えていないルームの名前を SHOWROOM に問い合わせて覚える (配信していないルームも分かる)。"""
    from concurrent.futures import ThreadPoolExecutor

    from .gifts import _get

    get = get or _get
    known = room_names(state_dir)
    now = time.time()
    with _lock:
        misses = dict(_name_misses)
    unknown = [k for k in dict.fromkeys(keys)
               if k and not (known.get(k) or {}).get("room_id") and misses.get(k, 0) <= now][:limit]
    if not unknown:
        return

    def one(key: str) -> tuple[str, Any, float]:
        try:
            return key, get(f"/api/room/status?room_url_key={key}"), NAME_MISS_TTL
        except Exception as e:  # noqa: BLE001 - 分からなければ room_url_key のまま出す
            return key, None, NAME_MISS_TTL if getattr(e, "code", None) == 404 else NAME_ERROR_TTL

    with ThreadPoolExecutor(8) as ex:
        results = list(ex.map(one, unknown))
    found = {key: (st.get("room_name"), st.get("room_id")) for key, st, _ in results
             if isinstance(st, dict) and st.get("room_id")}
    with _lock:
        for key in [k for k, until in _name_misses.items() if until <= now]:
            del _name_misses[key]
        for key, _, ttl in results:
            if key not in found:
                _name_misses[key] = now + ttl
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
        with _lock:  # 最近使ったものを後ろに (上限を超えた時に前から忘れる)
            _file_cache.pop(path, None)
            _file_cache[path] = cached
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
        _file_cache.pop(path, None)
        _file_cache[path] = (st.st_mtime, st.st_size, rows)
        total = sum(len(v[2]) for v in _file_cache.values())
        while total > FILE_CACHE_ROWS and len(_file_cache) > 1:
            total -= len(_file_cache.pop(next(iter(_file_cache)))[2])
    return rows


# ------------------------------------------------------------------ ルーム一覧
def _room_item(key: str, name: str | None = None) -> dict[str, Any]:
    return {"key": key, "name": name or key, "room_id": None, "tags": [], "registered": False, "is_live": False,
            "records": 0, "last_at": None, "image": None, "viewers": None, "followers": None, "genre": None,
            "started_at": None, "hit": False}


def showroom_live_rooms(get: Any = None) -> dict[str, dict[str, Any]]:
    """SHOWROOM で今配信している全ルーム {room_url_key: 情報} (2 分ごとに取り直す)。"""
    global _live_cache
    from .gifts import _get

    now = time.time()
    with _lock:
        if _live_cache and now - _live_cache[0] < LIVE_TTL:
            return _live_cache[1]
    data = (get or _get)("/api/live/onlives")
    out: dict[str, dict[str, Any]] = {}
    for group in (data or {}).get("onlives") or []:
        for live in group.get("lives") or []:
            key = live.get("room_url_key")
            if not key:
                continue
            item = out.setdefault(key, {"name": live.get("main_name") or key, "room_id": live.get("room_id"),
                                        "image": live.get("image"), "viewers": live.get("view_num"),
                                        "followers": live.get("follower_num"), "genre": None,
                                        "started_at": live.get("started_at")})
            # 「人気」などのまとめ枠ではなく、そのルームのジャンルの枠の名前を使う
            if group.get("genre_id") == live.get("genre_id") and group.get("genre_name"):
                item["genre"] = group["genre_name"]
    with _lock:
        _live_cache = (now, out)
    return out


def search_showroom_rooms(q: str, get: Any = None) -> list[dict[str, Any]]:
    """SHOWROOM のルーム検索 (名前で最大 100 件程度)。結果は 10 分使い回す。"""
    from urllib.parse import quote

    from .gifts import _get

    q = q.strip()
    if not q:
        return []
    now = time.time()
    with _lock:
        hit = _search_cache.get(q)
        if hit and now - hit[0] < SEARCH_TTL:
            return hit[1]
    data = (get or _get)(f"/api/search/room?word={quote(q)}")
    rows = [{"key": r["room_url_key"], "name": r.get("name") or r["room_url_key"], "room_id": r.get("room_id"),
             "image": r.get("image"), "is_live": bool(r.get("is_online")), "followers": r.get("follower_num"),
             "genre": r.get("genre_name")}
            for r in (data or {}).get("result") or [] if isinstance(r, dict) and r.get("room_url_key")]
    with _lock:
        if len(_search_cache) >= SEARCH_CACHE_MAX:
            _search_cache.pop(min(_search_cache, key=lambda k: _search_cache[k][0]))
        _search_cache[q] = (now, rows)
    return rows


def list_rooms(state_dir: Path, settings: dict[str, list[str]], live: dict[str, str] | None = None,
               lookup: Any = False, q: str = "", showroom: bool = False, get: Any = None) -> list[dict[str, Any]]:
    """ルームの一覧 (名前・区分・記録件数・最後のギフト)。

    設定にあるルームと、ギフトの記録があるルームを出す。live は配信中と分かっているルーム
    {room_url_key: 名前} (視聴中・記録中のもの)。lookup なら、名前を覚えていないルームを
    SHOWROOM に問い合わせる (関数を渡すとそれで取得する)。
    showroom なら SHOWROOM で配信中の全ルームも加え、q があれば SHOWROOM のルーム検索の結果
    (hit=True) も加える。
    """
    from .config import normalize_room_key

    live = live or {}
    files = _record_files(state_dir)
    registered = list(dict.fromkeys([*settings.get("rooms", []), *settings.get("record_rooms", []),
                                     *settings.get("enter_rooms", []), *settings.get("support_rooms", []),
                                     *settings.get("research_rooms", [])]))
    keys = list(dict.fromkeys([*registered, *live, *files]))
    typed = None
    if q.strip():  # URL や room_url_key をそのまま入れた時は、そのルームを出す
        try:
            typed = normalize_room_key(q.strip())
        except ValueError:
            typed = None
        if typed and typed not in keys and showroom:
            keys.append(typed)
            if lookup:  # 名前の分からないルームが多くても、入力したルームは必ず調べる
                fill_unknown_names(state_dir, [typed], get=None if lookup is True else lookup)
    if lookup:
        fill_unknown_names(state_dir, keys, get=None if lookup is True else lookup)
    names = room_names(state_dir)
    out: dict[str, dict[str, Any]] = {}
    for key in keys:
        info = names.get(key) or {}
        if key == typed and key not in registered and key not in files and not info.get("room_id"):
            continue  # SHOWROOM に無いルーム
        rows = _load_file(files[key]) if key in files else []
        item = _room_item(key, live.get(key) or info.get("name"))
        tags = item["tags"]
        if key in settings.get("rooms", []):
            tags.append("watch")
        if key in settings.get("record_rooms", []):
            tags.append("record")
        if key in settings.get("enter_rooms", []):
            tags.append("enter")
        if key in settings.get("support_rooms", []):
            tags.append("support")
        if key in settings.get("research_rooms", []):
            tags.append("research")
        if rows:
            tags.append("has_records")
        item.update(room_id=info.get("room_id"), registered=key in registered, is_live=key in live,
                    records=len(rows), last_at=max((g["at"] for g in rows), default=None), hit=key == typed)
        out[key] = item
    if showroom:
        for key, info in _quiet(lambda: showroom_live_rooms(get), {}).items():
            item = out.get(key) or out.setdefault(key, _room_item(key, info["name"]))
            item.update({k: v for k, v in info.items() if v is not None and (k != "name" or item["name"] == key)})
            item["is_live"] = True
        for info in _quiet(lambda: search_showroom_rooms(q, get), []) if q.strip() else []:
            item = out.get(info["key"]) or out.setdefault(info["key"], _room_item(info["key"], info["name"]))
            for k in ("room_id", "image", "followers", "genre"):
                item[k] = item[k] if item[k] is not None else info[k]
            item["is_live"] = item["is_live"] or info["is_live"]
            item["hit"] = True
    return list(out.values())


def _quiet(fn: Any, default: Any) -> Any:
    """SHOWROOM から取れなくても、手元の一覧だけは出す。"""
    try:
        return fn()
    except Exception as e:  # noqa: BLE001
        log.debug("SHOWROOM から一覧を取得できませんでした: %s", e)
        return default


# ------------------------------------------------------------------ ユーザー
def lookup_user(user_id: Any, get: Any = None) -> dict[str, Any] | None:
    """ユーザー ID から SHOWROOM のプロフィール (名前・アバター・レベル) を調べる。無ければ None。"""
    from .gifts import _get

    uid = int(str(user_id).strip())
    now = time.time()
    with _lock:
        hit = _user_cache.get(uid)
        if hit and now - hit[0] < USER_TTL:
            return hit[1]
    try:
        data = (get or _get)(f"/api/user/profile?user_id={uid}")
    except Exception as e:  # noqa: BLE001 - 404 (いない ID) も含む
        if getattr(e, "code", None) not in (400, 404):
            raise
        data = None
    found = ({"user_id": uid, "name": data.get("name"), "avatar": data.get("avatar_url"),
              "level": data.get("fan_level"), "description": (data.get("description") or "")[:200]}
             if isinstance(data, dict) and data.get("name") else None)
    with _lock:
        _user_cache[uid] = (now, found)
    return found


# ------------------------------------------------------------------ イベント一覧
def _fetch_event_list(get: Any) -> list[dict[str, Any]]:
    """SHOWROOM のイベント一覧 (開催中・開催前・終了したもの)。30 分使い回す。"""
    global _event_cache
    from concurrent.futures import ThreadPoolExecutor

    now = time.time()
    with _lock:
        if _event_cache and now - _event_cache[0] < EVENT_LIST_TTL:
            return _event_cache[1]

    def pages(status: int) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        page: Any = 1
        seen = set()
        while page and page not in seen and len(seen) < EVENT_LIST_MAX_PAGES:
            seen.add(page)
            try:
                data = get(f"/api/event/search?status={status}&page={page}")
            except Exception:  # noqa: BLE001 - 取れた分だけ使う
                if status == 1 and page == 1:
                    raise
                break
            if not isinstance(data, dict):
                break
            events.extend(e for e in data.get("event_list") or [] if isinstance(e, dict))
            page = data.get("next_page")
        return events

    with ThreadPoolExecutor(len(EVENT_STATUSES)) as ex:
        events = [e for part in ex.map(pages, EVENT_STATUSES) for e in part]
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
    """SHOWROOM の公開イベント (開催中・開催前・約 1 か月以内に終了) と、記録の設定にあるイベントの一覧。"""
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


# ------------------------------------------------------------------ ギフトのポイント表
# ギフトの一覧を更新する予定 (日本時間)。SHOWROOM はギフトを頻繁に入れ替えるため、この時刻を過ぎたら取り直す。
# keeper は設定 (gift_update_days / gift_update_times) で置き換え、その時刻に自分から取り直す
GIFT_UPDATE_SLOTS = parse_slots("mon", "0:00,3:00,12:00,15:00,18:00")
CATALOG_ROOMS = 3  # 予定の時刻に取り直す時、記録のあるルームが少なければ配信中のルームで補う数
CATALOG_REFRESH_ROOMS = 50  # 予定の時刻に取り直すルームの上限 (残りは一覧を使う時に CATALOG_ROOMS_PER_CALL ずつ)
_catalog_lock = threading.Lock()


def _catalog_path(state_dir: Path) -> Path:
    return state_dir / "cache" / "gift_catalog.json"


def _read_catalog(state_dir: Path) -> tuple[dict[int, dict[str, Any]], dict[str, float]] | None:
    """保存した一覧 (ギフト, {room_id: 取った時刻})。古い形式 (ルームごとの時刻なし) も読める。"""
    try:
        cached = json.loads(_catalog_path(state_dir).read_text(encoding="utf-8"))
        gifts = {int(k): v for k, v in cached["gifts"].items()}
        fetched = {str(k): float(v) for k, v in (cached.get("rooms") or {}).items()}
        return gifts, fetched
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def catalog_is_stale(fetched_at: float, now: float | None = None) -> bool:
    """取った時刻が古いか (1 日たった、または取ったあとに更新の予定時刻を過ぎた)。"""
    now = time.time() if now is None else now
    return now - fetched_at >= CATALOG_TTL or fetched_at < previous_time(now, GIFT_UPDATE_SLOTS)


def _update_catalog(state_dir: Path, room_ids: list[Any], limit: int, force: bool = False,
                    get_catalog: Any = None) -> tuple[dict[int, dict[str, Any]], dict[int, dict[str, Any]], int]:
    """room_ids のうち古いルームの一覧を (limit ルームまで) 取り直して保存する。

    戻り値は (取り直す前の一覧, 取り直したあとの一覧, 取れたルーム数)。
    取れなかったルームも取った時刻を残し、次の予定の時刻か 1 日後まで取り直さない。
    """
    from concurrent.futures import ThreadPoolExecutor

    get_catalog = get_catalog or fetch_catalog  # テストで差し替えられるよう、呼ぶ時に探す
    now = time.time()
    with _catalog_lock:
        gifts, fetched = _read_catalog(state_dir) or ({}, {})
        old = dict(gifts)
        todo = [rid for rid in dict.fromkeys(room_ids)
                if force or catalog_is_stale(fetched.get(str(rid), 0), now)][:limit]
        if not todo:
            return old, gifts, 0

        def one(room_id: Any) -> dict[int, dict[str, Any]] | None:
            try:
                return get_catalog(int(room_id))
            except Exception as e:  # noqa: BLE001 - 取れたルームの分だけ使う
                log.debug("ギフトの一覧を取れませんでした (room_id %s): %s", room_id, e)
                return None

        ok = 0
        with ThreadPoolExecutor(min(8, len(todo))) as ex:
            for room_id, catalog in zip(todo, ex.map(one, todo)):
                if catalog:
                    gifts.update(catalog)  # 古い表は捨てずに使い、取り直せたルームの分で上書きする
                    ok += 1
                fetched[str(room_id)] = now
        try:
            path = _catalog_path(state_dir)
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
            tmp.write_text(json.dumps({"at": now, "gifts": gifts, "rooms": fetched}, ensure_ascii=False),
                           encoding="utf-8")
            os.replace(tmp, path)
        except (OSError, ValueError, TypeError):
            pass
        return old, gifts, ok


def _live_room_ids() -> list[Any]:
    from .gifts import _get

    return [l["room_id"] for gr in _get("/api/live/onlives").get("onlives", [])
            for l in gr.get("lives", []) if l.get("room_id")]


def gift_catalog(state_dir: Path, rooms: list[str] | None = None) -> dict[int, dict[str, Any]]:
    """ギフトの名前・ポイント・無料かどうか。

    ギフトの一覧はルームごとに少しずつ違う (そのルームだけのギフトがある) ので、rooms (room_url_key)
    それぞれの一覧を取ってまとめる。取ったルームは次の予定の時刻か 1 日後まで使い回し、
    1 回に新しく取るのは CATALOG_ROOMS_PER_CALL ルームまで。
    """
    names = room_names(state_dir)
    room_ids = [names[k]["room_id"] for k in dict.fromkeys(rooms or names) if (names.get(k) or {}).get("room_id")]
    if not room_ids and not (_read_catalog(state_dir) or ({}, {}))[0]:
        # まだルームを覚えていなければ、配信中のルームを 1 つ使う
        try:
            room_ids = _live_room_ids()[:1]
        except Exception:  # noqa: BLE001
            room_ids = []
    return _update_catalog(state_dir, room_ids, CATALOG_ROOMS_PER_CALL)[1]


def refresh_gift_catalog(state_dir: Path, get_catalog: Any = None, live_room_ids: Any = None) -> dict[str, Any]:
    """予定の時刻に、ギフトの一覧を SHOWROOM から取り直す。戻り値は {ok, count, added, changed, first}。

    記録のあるルーム (最近ギフトがあった順に CATALOG_REFRESH_ROOMS まで) を取り直し、
    少なければ配信中のルームで補う。取れなかった時は前の一覧をそのまま使う。
    """
    names = room_names(state_dir)
    files = _record_files(state_dir)
    recent = sorted(files, key=lambda k: _mtime(files[k]) or 0, reverse=True)
    room_ids = [names[k]["room_id"] for k in recent if (names.get(k) or {}).get("room_id")][:CATALOG_REFRESH_ROOMS]
    if len(room_ids) < CATALOG_ROOMS:
        try:
            live = _live_room_ids() if live_room_ids is None else list(live_room_ids)
            room_ids += [r for r in dict.fromkeys(live) if r not in room_ids][:CATALOG_ROOMS - len(room_ids)]
        except Exception:  # noqa: BLE001
            pass
    old, gifts, ok = _update_catalog(state_dir, room_ids, CATALOG_REFRESH_ROOMS, force=True, get_catalog=get_catalog)
    if not ok:
        return {"ok": False, "count": len(gifts), "added": [], "changed": [], "first": False}
    added = [g.get("gift_name") or str(k) for k, g in gifts.items() if k not in old] if old else []
    changed = [g.get("gift_name") or str(k) for k, g in gifts.items()
               if k in old and (old[k].get("point"), old[k].get("free")) != (g.get("point"), g.get("free"))]
    return {"ok": True, "count": len(gifts), "added": added, "changed": changed, "first": not old}


def _points(catalog: dict[int, dict[str, Any]], g: dict[str, Any]) -> tuple[int, bool]:
    info = catalog.get(g.get("gift_id")) or {}
    return int(info.get("point") or 0) * int(g.get("num") or 1), bool(info.get("free", True))


# ------------------------------------------------------------------ リスナー一覧・履歴
def _records(state_dir: Path, room: str | None, since: float | None,
             files: dict[str, Path] | None = None) -> list[tuple[str, dict[str, Any]]]:
    files = files if files is not None else _record_files(state_dir)  # files: 何ルームも続けて読む時に、一度だけ探したもの
    keys = [room] if room else list(files)
    out = []
    for key in keys:
        if key not in files:
            continue
        for g in _load_file(files[key]):
            if since is None or g["at"] >= since:
                out.append((key, g))
    return out


TOP_ROOMS = 5  # ギフターの一覧に出す、人ごとの多いルームの数


def _sender_rooms(state_dir: Path, user: int | None = None, since: float | None = None,
                  table: bool = False) -> tuple[list[str], list[Any], list[Any]]:
    """記録のファイルのうち、読む必要のあるルーム (送り主の表 gift_senders から選ぶ。ファイル全部を読まないため)。

    戻り値は (読むルーム, 表の行, 表の名前の行)。user なら、その人が送ったルーム。since なら、その時刻のあとに送りのあったルーム。
    table なら、表に足し終えたルームはファイルを読まず、表の行 ([人ごとの合計, 人ごとの多いルーム TOP_ROOMS 個]) を返す。
    表にまだ足し終えていないルーム (ギフトの一覧がまだ無い時など) は、いつもファイルを読む。
    前は、ギフターのページ・ギフターの一覧を開くたびに、記録のファイル全部 (サーバーで 66MB) を読んでいた (2026-10-19)。
    """
    from .explore import update_all_gift_senders
    from .research_store import Store

    files = _record_files(state_dir)
    with Store(state_dir) as s:
        update_all_gift_senders(state_dir, s)  # 前に足したあとの新しい分だけ
        pos = {r[0]: r[1] for r in s.all("SELECT room_key, pos FROM gift_sender_pos")}
        done = {k for k, p in files.items() if _size(p) is not None and pos.get(k) == _size(p)}
        rows: list[Any] = []
        names: list[Any] = []
        if user is not None:
            picked = {r[0] for r in s.all("SELECT room_key FROM gift_senders WHERE user_id=?", (int(user),))}
        elif since is not None:
            picked = {r[0] for r in s.all("SELECT room_key FROM gift_senders GROUP BY room_key HAVING MAX(last_at) >= ?", (since,))}
        else:
            picked = set(done)
        if table and since is None:
            # 人ごとの合計は SQL でまとめる (ルーム × 人の行は何十万にもなり、Python で 1 行ずつ足すと遅い)
            cur = s.conn.cursor()
            cur.row_factory = None
            cur.execute("CREATE TEMP TABLE IF NOT EXISTS sender_done (room_key TEXT PRIMARY KEY)")
            cur.execute("DELETE FROM sender_done")
            cur.executemany("INSERT INTO sender_done VALUES (?)", [(k,) for k in done])
            # name は、last_at が一番新しい行のもの (SQLite は MAX() と一緒に選んだ列を、その行から返す)
            totals = cur.execute("""SELECT user_id, SUM(times), SUM(points), SUM(free_points), MIN(COALESCE(first_at, last_at)),
                                           MAX(last_at), name, COUNT(*) FROM gift_senders
                                    WHERE room_key IN (SELECT room_key FROM sender_done) GROUP BY user_id""").fetchall()
            top = cur.execute(f"""SELECT user_id, room_key, points FROM (
                                     SELECT user_id, room_key, points, ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY points DESC) AS n
                                     FROM gift_senders WHERE room_key IN (SELECT room_key FROM sender_done)) WHERE n <= {TOP_ROOMS}
                                  ORDER BY user_id, n""").fetchall()
            rows = [totals, top]
            names = cur.execute("SELECT user_id, name, at FROM gift_sender_names ORDER BY user_id, at DESC").fetchall()
            picked = set()
    return sorted((picked & set(files)) | (set(files) - done)), rows, names


def _period_rows(state_dir: Path, since: float) -> tuple[list[str], list[Any], list[Any]]:
    """期間を決めたギフターの一覧の、ギフト 1 件ずつの表 (gift_events) から SQL でまとめた分。戻り値は _sender_rooms と同じ形
    (読むルーム, [人ごとの合計, 人ごとの多いルーム TOP_ROOMS 個], 名前の行)。表に足し終えていないルームだけファイルを読む。
    前は、その間に送りのあったルーム (サーバーではほぼ全部の 816 ルーム・70MB) のファイルを読み、41〜44 秒・メモリ +520MB かかった (2026-10-19)。
    """
    from .explore import update_all_gift_senders
    from .research_store import Store

    files = _record_files(state_dir)
    with Store(state_dir) as s:
        update_all_gift_senders(state_dir, s)  # 送り主の表と一緒に、ギフト 1 件ずつの表にも新しい分を足す
        pos = {r[0]: r[1] for r in s.all("SELECT room_key, pos FROM gift_event_pos")}
        done = {k for k, p in files.items() if _size(p) is not None and pos.get(k) == _size(p)}
        catalog = gift_catalog(state_dir, sorted(done))
        cur = s.conn.cursor()
        cur.row_factory = None
        for sql in PERIOD_TEMP:
            cur.execute(sql)
        cur.execute("DELETE FROM event_done")
        cur.executemany("INSERT INTO event_done VALUES (?)", [(k,) for k in done])
        cur.execute("DELETE FROM period_catalog")
        cur.executemany("INSERT INTO period_catalog VALUES (?, ?, ?)",
                        [(int(k), int(g.get("point") or 0), int(bool(g.get("free", True)))) for k, g in catalog.items()])
        # 一覧に無いギフトは 0 pt・無料として数える (_points と同じ)。
        # CROSS JOIN でルームから先に読む (ふつうの JOIN だと、GROUP BY のために gift_events を人の索引で全部読むことがある)
        per_room = cur.execute(PERIOD_SQL, (since,)).fetchall()
        names = cur.execute("SELECT user_id, name, at FROM gift_sender_names WHERE at >= ?", (since,)).fetchall()
    return sorted(set(files) - done), per_room, names


PERIOD_TEMP = ("CREATE TEMP TABLE IF NOT EXISTS event_done (room_key TEXT PRIMARY KEY)",
               "CREATE TEMP TABLE IF NOT EXISTS period_catalog (gift_id INTEGER PRIMARY KEY, point INTEGER, free INTEGER)")
PERIOD_SQL = """SELECT e.user_id, e.room_key, SUM(e.num * COALESCE(c.point, 0)),
                       SUM(CASE WHEN COALESCE(c.free, 1) THEN e.num * COALESCE(c.point, 0) ELSE 0 END),
                       SUM(e.num), COUNT(*), MIN(e.at), MAX(e.at)
                FROM event_done d CROSS JOIN gift_events e ON e.room_key = d.room_key AND e.at >= ?
                LEFT JOIN period_catalog c ON c.gift_id = e.gift_id
                GROUP BY e.user_id, e.room_key"""


def _size(path: Path) -> int | None:
    try:
        return path.stat().st_size
    except OSError:
        return None


LISTENERS_TTL = 60  # ギフターの一覧 (記録したギフト) を使い回す秒数 (何人もが続けて開いた時に、数え直さない)
_listeners_cache: dict[tuple[str, Any, Any], tuple[float, dict[str, Any]]] = {}
LISTENERS_STALE = 6 * 3600  # これより古い結果は返さずに数え直す (収集の合間に先に数えておくので、ふだんは古くならない)
_listeners_lock = threading.Lock()
_listeners_running: set[tuple[str, Any, Any]] = set()


def warm_listeners(state_dir: Path) -> None:
    """収集の合間に、ギフターの一覧 (すべての期間) を先に数えておく (開いた人を待たせない)。"""
    key = (str(state_dir), None, None)
    if key in _listeners_cache and memory.tight():
        return  # メモリの空きが少ない時 (視聴のブラウザと重なった時) は、前の結果のまま次の機会に
    with _listeners_lock:
        if key in _listeners_running:
            return
        _listeners_running.add(key)
    _refresh_listeners(state_dir, None, None, key)


def list_listeners(state_dir: Path, days: float | None = None, room: str | None = None) -> dict[str, Any]:
    """記録したギフトのリスナー一覧 (全ルームまたは 1 ルーム)。ポイントの多い順。LISTENERS_TTL 秒は前の結果を使い回す。"""
    key = (str(state_dir), days or None, room or None)
    hit = _listeners_cache.get(key)
    if hit and time.time() - hit[0] < LISTENERS_TTL:
        return hit[1]
    if hit and LISTENERS_TTL > 0 and time.time() - hit[0] < LISTENERS_STALE:
        # 古ければ、前の結果をすぐ返して、裏で数え直す (サーバーで 1 回目 11.8 秒かかっていた。2026-10-19)
        with _listeners_lock:
            if key not in _listeners_running and not memory.tight():  # 空きが少なければ前の結果のまま
                _listeners_running.add(key)
                threading.Thread(target=_refresh_listeners, args=(state_dir, days, room, key), name="listeners", daemon=True).start()
        return hit[1]
    return _refresh_listeners(state_dir, days, room, key)


def _refresh_listeners(state_dir: Path, days: float | None, room: str | None, key: tuple[str, Any, Any]) -> dict[str, Any]:
    try:
        out = _list_listeners(state_dir, days, room)
        if len(_listeners_cache) > 20:
            _listeners_cache.clear()
        _listeners_cache[key] = (time.time(), out)
        return out
    finally:
        with _listeners_lock:
            _listeners_running.discard(key)
        memory.release()


def _from_table(rows: list[Any], sender_names: list[Any], names: dict[str, Any], catalog: dict[int, Any]) -> dict[str, Any]:
    """すべての期間の一覧を、送り主の表の行 (人ごとの合計・多いルーム・名前。どれも人の順に並んだもの) から、そのまま組み立てる
    (人ごとに途中の形を作ってから並べ直すと、何万人もいる時に遅い。サーバーで 7.5 秒。2026-10-19)。"""
    totals, top = rows
    out: dict[Any, dict[str, Any]] = {}
    for uid, times, pt, free, first, last, name, n in totals:
        pt, free = pt or 0, free or 0
        out[uid] = {"user_id": uid, "name": name, "points": pt, "paid_points": pt - free, "free_points": free, "count": None,
                    "times": times or 0, "first_at": first, "last_at": last, "names": [], "room_count": n, "rooms": []}
    for uid, key, pt in top:  # ポイントの多い順
        out[uid]["rooms"].append({"key": key, "name": (names.get(key) or {}).get("name") or key, "points": pt or 0})
    for uid, name, _ in sender_names:  # 新しい順
        u = out.get(uid)
        if u is not None:
            u["names"].append(name)
    listeners = sorted(out.values(), key=lambda u: (u["points"], u["times"]), reverse=True)
    return {"listeners": listeners, "total": len(listeners), "catalog": bool(catalog)}


def _list_listeners(state_dir: Path, days: float | None = None, room: str | None = None) -> dict[str, Any]:
    """記録したギフトのリスナー一覧 (全ルームまたは 1 ルーム)。ポイントの多い順。

    全ルーム・すべての期間なら送り主の表 (gift_senders) から、期間を決めたらギフト 1 件ずつの表 (gift_events) から SQL でまとめる。
    個数 (count) は送り主の表に無いので、すべての期間で表から出した人は None。
    """
    since = time.time() - days * 86400 if days else None
    rows: list[Any] = []
    sender_names: list[Any] = []
    period: list[Any] = []
    if room:
        records = _records(state_dir, room, since)
    elif since is not None:
        keys, period, sender_names = _period_rows(state_dir, since)
        files = _record_files(state_dir)
        records = [kg for key in keys for kg in _records(state_dir, key, since, files)]
    else:
        keys, rows, sender_names = _sender_rooms(state_dir, since=since, table=True)
        files = _record_files(state_dir)
        records = [kg for key in keys for kg in _records(state_dir, key, since, files)]
    # ギフトの一覧は、ファイルから読んだギフトがある時だけ取る (表から出した分はポイントが入っているので要らない。
    # 取ると、古いルームの一覧を SHOWROOM に問い合わせて 2 秒ほど待つことがあった)。一覧があるかどうかは、残してある一覧で見る
    catalog = (gift_catalog(state_dir, list(dict.fromkeys(key for key, _ in records))) if records
               else (_read_catalog(state_dir) or ({}, {}))[0])
    names = room_names(state_dir)
    if rows and not records:
        return _from_table(rows, sender_names, names, catalog)
    users: dict[Any, dict[str, Any]] = {}
    totals, top = rows or ([], [])
    for uid, times, pt, free, first, last, name, n in totals:  # 表から (人ごとの合計)
        pt, free = pt or 0, free or 0
        users[uid] = {"user_id": uid, "name": name, "names": {}, "points": pt, "paid_points": pt - free, "free_points": free,
                      "count": None, "times": times or 0, "rooms": defaultdict(int), "room_count": n, "first_at": first, "last_at": last}
    for uid, key, pt in top:
        users[uid]["rooms"][key] = pt or 0
    for uid, key, pt, free, count, times, first, last in period:  # ギフト 1 件ずつの表から (人 × ルームの合計)
        pt, free = pt or 0, free or 0
        u = users.setdefault(uid, {"user_id": uid, "name": None, "names": {}, "points": 0, "paid_points": 0, "free_points": 0,
                                   "count": 0, "times": 0, "rooms": defaultdict(int), "room_count": 0,
                                   "first_at": first, "last_at": last})
        u["points"] += pt
        u["free_points"] += free
        u["paid_points"] += pt - free
        u["count"] += count or 0
        u["times"] += times or 0
        u["rooms"][key] += pt
        u["room_count"] += 1
        u["first_at"], u["last_at"] = min(u["first_at"], first), max(u["last_at"], last)
    for uid, name, at in sender_names:
        u = users.get(uid)
        if u is not None:
            u["names"][name] = max(u["names"].get(name, 0), at or 0)
    for u in users.values():
        if u["name"] is None and u["names"]:  # ギフト 1 件ずつの表には名前が無いので、一番新しく使った名前
            u["name"] = max(u["names"].items(), key=lambda kv: kv[1])[0]
    for key, g in records:
        uid = g.get("user_id")
        if uid is None:
            continue
        u = users.setdefault(uid, {"user_id": uid, "name": g.get("name"), "names": {}, "points": 0, "paid_points": 0,
                                   "free_points": 0, "count": 0, "times": 0, "rooms": defaultdict(int), "room_count": 0,
                                   "first_at": g["at"], "last_at": g["at"]})
        if key not in u["rooms"]:
            u["room_count"] += 1  # ファイルから読むルームは、表に入っていないルームなので、重ならない
        pt, free = _points(catalog, g)
        u["points"] += pt
        u["free_points" if free else "paid_points"] += pt
        if u["count"] is not None:
            u["count"] += int(g.get("num") or 1)
        u["times"] += 1
        u["rooms"][key] += pt
        if g.get("name"):
            u["names"][g["name"]] = max(u["names"].get(g["name"], 0), g["at"])
        if g["at"] >= (u["last_at"] or 0):
            u["last_at"], u["name"] = g["at"], g.get("name") or u["name"]
        u["first_at"] = min(u["first_at"] or g["at"], g["at"])
    out = []
    for u in users.values():
        rooms = sorted(u["rooms"].items(), key=lambda kv: -kv[1])
        out.append({**{k: u[k] for k in ("user_id", "name", "points", "paid_points", "free_points", "count", "times",
                                          "first_at", "last_at")},
                    "names": [n for n, _ in sorted(u["names"].items(), key=lambda kv: -kv[1])],
                    "room_count": u["room_count"],
                    "rooms": [{"key": k, "name": (names.get(k) or {}).get("name") or k, "points": p} for k, p in rooms[:TOP_ROOMS]]})
    out.sort(key=lambda u: (u["points"], u["times"]), reverse=True)
    return {"listeners": out, "total": len(out), "catalog": bool(catalog)}


def listener_detail(state_dir: Path, user_id: Any, days: float | None = None) -> dict[str, Any]:
    """1 人のリスナーの、記録した全ルームでの履歴 (ルーム別・日別・一覧)。"""
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        raise ValueError("ユーザー ID は数字で指定してください") from None
    since = time.time() - days * 86400 if days else None
    keys = _sender_rooms(state_dir, user=uid)[0]  # この人が送ったルーム (と、送り主の表にまだ足し終えていないルーム) のファイルだけ
    files = _record_files(state_dir)
    hits = [(key, g) for key in keys for key, g in _records(state_dir, key, since, files) if g.get("user_id") == uid]
    if not hits:
        return {"user_id": uid, "found": False}
    catalog = gift_catalog(state_dir, list(dict.fromkeys(key for key, _ in hits)))
    names = room_names(state_dir)
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
    ranked = sorted(rooms.values(), key=lambda r: -r["points"])
    return {
        "user_id": uid, "found": True,
        "names": list(dict.fromkeys(g.get("name") for _, g in reversed(hits) if g.get("name"))),
        "total": total,
        "rooms": ranked,
        "days": [{"day": d, **v} for d, v in days_map.items()],
        "history": list(reversed(history[-500:])),
        "first_at": hits[0][1]["at"], "last_at": hits[-1][1]["at"],
        "charts": _behavior(state_dir, hits, catalog, [r["key"] for r in ranked[:CHART_ROOMS]]),
    }


CHART_ROOMS = 5  # 日別のグラフで色分けするルームの数 (残りは「その他」)
CHART_MAX_DAYS = 180  # 日別のグラフに出す日数の上限 (最後に送った日から)
TIMING_BIN_MIN = 10  # 配信のどの辺りで送ったかのグラフの刻み (分)
TIMING_MAX_MIN = 180  # これより後は最後の棒にまとめる
SIZE_BINS = (1, 10, 100, 1000, 10000)  # 1 回に送ったポイントの区切り


def _behavior(state_dir: Path, hits: list[tuple[str, dict[str, Any]]], catalog: dict[int, dict[str, Any]],
              top_rooms: list[str]) -> dict[str, Any]:
    """1 人のリスナーの行動のグラフ用の集計 (いつ・どこに・配信のどの辺りで・どれくらいずつ送るか)。"""
    from datetime import timedelta

    # 配信の始まりは記録に無いので、そのルームの記録で、その配信の最初のギフトの時刻を使う
    files = _record_files(state_dir)
    live_start: dict[tuple[str, Any], float] = {}
    for key in dict.fromkeys(k for k, _ in hits):
        for g in _load_file(files[key]) if key in files else []:
            lk = (key, g.get("live_id"))
            if g["at"] < live_start.get(lk, float("inf")):
                live_start[lk] = g["at"]

    daily: dict[str, dict[str, Any]] = {}
    heat = {kind: [[0] * 24 for _ in range(7)] for kind in ("paid", "free")}  # [曜日 (月=0)][時]
    timing = [{"paid": 0, "free": 0} for _ in range(TIMING_MAX_MIN // TIMING_BIN_MIN + 1)]
    sizes = [{"paid": 0, "free": 0} for _ in SIZE_BINS]
    for key, g in hits:
        pt, free = _points(catalog, g)
        kind = "free" if free else "paid"
        t = datetime.fromtimestamp(g["at"], JST)
        d = daily.setdefault(t.strftime("%Y-%m-%d"), {"paid": 0, "free": 0, "rooms": defaultdict(int)})
        d[kind] += pt
        d["rooms"][key if key in top_rooms else "_other"] += pt
        heat[kind][t.weekday()][t.hour] += pt
        # live_id の無い記録 (イベントのルームで配信が分かる前の分) は、どの配信か分からないので数えない
        start = live_start.get((key, g.get("live_id"))) if g.get("live_id") is not None else None
        if start is not None:
            timing[min(int((g["at"] - start) // 60), TIMING_MAX_MIN) // TIMING_BIN_MIN][kind] += 1
        if pt > 0:
            sizes[max(i for i, low in enumerate(SIZE_BINS) if pt >= low)][kind] += 1

    last = datetime.fromtimestamp(hits[-1][1]["at"], JST).date()
    first = max(datetime.fromtimestamp(hits[0][1]["at"], JST).date(), last - timedelta(days=CHART_MAX_DAYS - 1))
    days = []
    for i in range((last - first).days + 1):
        day = (first + timedelta(days=i)).strftime("%Y-%m-%d")
        d = daily.get(day) or {"paid": 0, "free": 0, "rooms": {}}
        days.append({"day": day, "paid": d["paid"], "free": d["free"], "rooms": dict(d["rooms"])})
    return {
        "daily": days,
        "heatmap": heat,
        "timing": [{"from": i * TIMING_BIN_MIN, **v} for i, v in enumerate(timing)],
        "sizes": [{"from": low, **v} for low, v in zip(SIZE_BINS, sizes)],
        "rooms": top_rooms,
    }
