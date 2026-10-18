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
FILE_CACHE_ROWS = 600_000
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
    records = _records(state_dir, room, since)
    catalog = gift_catalog(state_dir, list(dict.fromkeys(key for key, _ in records)))
    names = room_names(state_dir)
    users: dict[Any, dict[str, Any]] = {}
    for key, g in records:
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
    hits = [(key, g) for key, g in _records(state_dir, None, since) if g.get("user_id") == uid]
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
