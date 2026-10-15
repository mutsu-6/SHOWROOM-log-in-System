"""SHOWROOM が公開しているルームの一覧。

サイトに「存在する全部屋」の単一の一覧 API はない。配信中・配信予定・開催中イベントの
参加ルームを集め、公開されている範囲の全部屋をログイン確認の対象にする。
"""

from __future__ import annotations

import logging
import re
from typing import Any, Awaitable, Callable

from .recorder import next_page_number

log = logging.getLogger("directory")

MAX_LIST_PAGES = 30
EVENT_KEY_RE = re.compile(r"[A-Za-z0-9_-]+")

GetJson = Callable[[str], Awaitable[Any]]


def _room_id(value: Any) -> int | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        room_id = int(value)
    except (TypeError, ValueError):
        return None
    return room_id if room_id > 0 else None


def add_room(found: dict[int, str], room_id: Any, room_url_key: Any = None) -> None:
    rid = _room_id(room_id)
    if rid is None:
        return
    found.setdefault(rid, str(room_url_key or ""))


def rooms_from_onlives(data: Any, found: dict[int, str]) -> None:
    if not isinstance(data, dict):
        return
    for genre in data.get("onlives") or []:
        if not isinstance(genre, dict):
            continue
        for live in genre.get("lives") or []:
            if isinstance(live, dict):
                add_room(found, live.get("room_id"), live.get("room_url_key"))


def rooms_from_upcoming(data: Any, found: dict[int, str]) -> None:
    if not isinstance(data, dict):
        return
    for room in data.get("upcomings") or []:
        if isinstance(room, dict):
            add_room(found, room.get("room_id"), room.get("room_url_key"))


def genre_id(value: Any) -> int | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        gid = int(value)
    except (TypeError, ValueError):
        return None
    return gid if gid > 0 else None


def genre_ids_from(genres: Any, ranking: Any) -> list[int]:
    ids: list[int] = []
    for genre in genres or []:
        if isinstance(genre, dict):
            gid = genre_id(genre.get("genre_id"))
            if gid is not None and gid not in ids:
                ids.append(gid)
    groups = ranking.get("room_genre") if isinstance(ranking, dict) else None
    for genre in groups or []:
        if isinstance(genre, dict):
            gid = genre_id(genre.get("genre_id"))
            if gid is not None and gid not in ids:
                ids.append(gid)
    return ids


async def event_room_ids(get_json: GetJson, event_key: str, found: dict[int, str]) -> None:
    page = 1
    seen: set[int] = set()
    while page not in seen and len(seen) < MAX_LIST_PAGES:
        seen.add(page)
        data = await get_json(f"/api/event/{event_key}/rooms?page={page}")
        if not isinstance(data, dict):
            return
        batch = data.get("rooms") or []
        if not isinstance(batch, list) or not batch:
            return
        for room in batch:
            if isinstance(room, dict):
                add_room(found, room.get("room_id"), room.get("room_url_key"))
        nxt = next_page_number(page, data.get("next_page"))
        if nxt is None:
            return
        if len(seen) >= MAX_LIST_PAGES:
            log.warning("イベント %s の参加ルームが %d ページを超えたため打ち切ります", event_key, MAX_LIST_PAGES)
            return
        page = nxt


async def collect_platform_rooms(get_json: GetJson) -> list[int]:
    """公開中の配信・予定・イベントからルーム ID を重複なく集める。"""
    found: dict[int, str] = {}
    try:
        rooms_from_onlives(await get_json("/api/live/onlives"), found)
    except Exception as e:  # noqa: BLE001
        log.warning("配信中ルームの一覧を取得できません: %s", e)
    try:
        genres = await get_json("/api/search/room_genres")
    except Exception as e:  # noqa: BLE001
        log.debug("ジャンル一覧を取得できません: %s", e)
        genres = []
    try:
        ranking = await get_json("/api/genre_ranking/room_genre")
    except Exception as e:  # noqa: BLE001
        log.debug("ジャンル区分を取得できません: %s", e)
        ranking = {}
    for gid in genre_ids_from(genres, ranking):
        try:
            rooms_from_upcoming(await get_json(f"/api/live/upcoming?genre_id={gid}"), found)
        except Exception as e:  # noqa: BLE001
            log.debug("ジャンル %s の配信予定を取得できません: %s", gid, e)
    page = 1
    seen: set[int] = set()
    while page not in seen and len(seen) < MAX_LIST_PAGES:
        seen.add(page)
        try:
            data = await get_json(f"/api/event/search?page={page}")
        except Exception as e:  # noqa: BLE001
            log.warning("イベント一覧を取得できません: %s", e)
            break
        if not isinstance(data, dict):
            break
        for event in data.get("event_list") or []:
            if not isinstance(event, dict) or event.get("is_closed") or event.get("is_public") is False:
                continue
            key = event.get("event_url_key")
            if not isinstance(key, str) or not EVENT_KEY_RE.fullmatch(key):
                continue
            try:
                await event_room_ids(get_json, str(key), found)
            except Exception as e:  # noqa: BLE001
                log.debug("イベント %s の参加ルームを取得できません: %s", key, e)
        nxt = next_page_number(page, data.get("next_page"))
        if nxt is None:
            break
        if len(seen) >= MAX_LIST_PAGES:
            log.warning("イベント一覧が %d ページを超えたため打ち切ります", MAX_LIST_PAGES)
            break
        page = nxt
    return list(found)
