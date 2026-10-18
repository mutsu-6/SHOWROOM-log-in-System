"""ブラウザを開かずにギフトを記録する (指定ルーム・イベント参加ルーム)。

- 設定の record_rooms と events (参加ルームすべて) と、グリッター (デイリーグリッターなど) を取ったルームが対象。件数の上限はない。
- 保存済みのログインセッションがあれば、問い合わせとリアルタイム通信に使う。
- 2 分ごとに配信状況を確認し、配信中のルームだけリアルタイム通信 (WebSocket) を購読してギフト (t=2) を記録する。
  受信するだけで、こちらからはコメント等を送らない (接続維持の PING のみ)。
- イベントは終了日時を過ぎると自動で対象外になる。
- 視聴中 (rooms) のルームはブラウザ側で記録しているため対象外にする (二重記録防止)。
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
import json
import logging
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

from . import comment_counts, live_events, stage_ranks
from .activity import parse_message
from .api import BASE_URL
from .config import Config
from .gifts import record_gift
from .status import JST

log = logging.getLogger("recorder")

WS_URL = "wss://online.showroom-live.com/"
REFRESH_INTERVAL = 120  # 配信状況の確認間隔 (秒)
PING_INTERVAL = 60
EVENT_GRACE = 600  # イベント終了後もこの秒数は記録を続ける (終了直前の配信の取りこぼし防止)
# 件数の上限はない。同時に投げる通信だけを抑え、API と接続の集中を避ける
FETCH_CONCURRENCY = 8
CONNECT_CONCURRENCY = 8
STAGE_EVERY = 900  # 配信の順位 (ステージの上位 100 人。ポイントは出ない) を読む間隔 (秒)。配信の終わりにも 1 回読む
GLITTER_KEEP = 7 * 86400  # グリッターを取ったルームを、この秒数のあいだ配信のたびに記録する
GLITTER_FILE = "glitter_rooms.json"  # {room_url_key: 最後にグリッターのバッジを見た時刻} (起動し直しても続けるため)


def glitter_badge(live: dict[str, Any]) -> bool:
    """配信一覧の 1 件に、グリッター (D = デイリー・W = ウィークリーなど) のバッジが付いているか。"""
    return any(isinstance(b, dict) and b.get("type") == "glitter" for b in live.get("badge_list") or [])


def _showroom_domain(domain: str) -> bool:
    host = domain.lstrip(".").lower()
    return host == "showroom-live.com" or host.endswith(".showroom-live.com")


def _cookie_expired(cookie: dict[str, Any], now: float | None = None) -> bool:
    expires = cookie.get("expires", -1)
    try:
        exp = float(expires)
    except (TypeError, ValueError):
        return False
    # -1 と 0 はセッション Cookie (期限なし)
    return exp > 0 and exp < (time.time() if now is None else now)


def _cookie_pair(name: Any, value: Any) -> str | None:
    if not name or value is None:
        return None
    pair = f"{name}={value}"
    if any(ch in pair for ch in "\r\n"):
        return None
    try:
        pair.encode("latin-1")
    except UnicodeEncodeError:
        return None
    return pair


def session_cookie(state_file: Path, now: float | None = None) -> str | None:
    """保存済みログインセッションから SHOWROOM 向けの Cookie ヘッダを作る。"""
    try:
        data = json.loads(Path(state_file).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    pairs = []
    for cookie in data.get("cookies") or []:
        if not isinstance(cookie, dict) or not _showroom_domain(str(cookie.get("domain") or "")):
            continue
        if _cookie_expired(cookie, now):
            continue
        pair = _cookie_pair(cookie.get("name"), cookie.get("value"))
        if pair:
            pairs.append(pair)
    return "; ".join(pairs) or None


def get_json(path: str, cookie: str | None = None) -> Any:
    headers = {"User-Agent": "showroom-keeper", "Accept-Language": "ja"}
    if cookie:
        headers["Cookie"] = cookie
    req = urllib.request.Request(f"{BASE_URL}{path}", headers=headers)
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.load(resp)


async def aget(path: str, cookie: str | None = None) -> Any:
    return await asyncio.get_running_loop().run_in_executor(None, get_json, path, cookie)


def ws_header_param(connect: Any) -> str:
    """websockets の版によって接続時のヘッダ引数名が違う。"""
    import inspect

    params = inspect.signature(connect).parameters
    return "additional_headers" if "additional_headers" in params else "extra_headers"


def next_page_number(current: int, nxt: Any) -> int | None:
    """次ページ番号。進まない値や循環では None (無限リクエストを止める)。"""
    if isinstance(nxt, str) and nxt.isdigit():
        nxt = int(nxt)
    if isinstance(nxt, bool) or not isinstance(nxt, (int, float)):
        return None
    page = int(nxt)
    if page <= current:
        return None
    return page


async def event_rooms(event_key: str, cookie: str | None = None, fetch: Any = None) -> list[dict[str, Any]]:
    """イベントの参加ルーム一覧 (全ページ)。"""
    get = fetch or aget
    rooms: list[dict[str, Any]] = []
    page = 1
    seen: set[int] = set()
    while page not in seen:
        seen.add(page)
        data = await get(f"/api/event/{event_key}/rooms?page={page}", cookie)
        if not isinstance(data, dict):
            break
        batch = data.get("rooms") or []
        if not isinstance(batch, list) or not batch:
            break
        rooms.extend(room for room in batch if isinstance(room, dict))
        nxt = next_page_number(page, data.get("next_page"))
        if nxt is None:
            break
        page = nxt
    return rooms


class GiftRecorder:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.state_dir = Path(cfg.state_file).parent
        self._tasks: dict[str, asyncio.Task] = {}
        # 記録中の配信の live_id。同じルームでも配信が変われば live_id が変わるので、それで接続し直す
        self._live_ids: dict[str, int | None] = {}
        # 直近の確認で取得に失敗したものがあるか (あれば、対象から外れたルームでも記録を止めない)
        self._incomplete = False
        self._events: dict[str, dict[str, Any]] = {}
        self._ended_logged: set[str] = set()
        # 管理画面に表示する、記録中ルームの名前と記録している理由 (ルーム指定・イベント名)
        self._names: dict[str, str] = {}
        self._sources: dict[str, str] = {}
        self._fetch_slots: asyncio.Semaphore | None = None
        self._connect_slots: asyncio.Semaphore | None = None
        self._glitter: dict[str, float] = self._load_glitter()
        self._glitter_saved = 0.0

    def _load_glitter(self) -> dict[str, float]:
        try:
            data = json.loads((self.state_dir / GLITTER_FILE).read_text(encoding="utf-8"))
            return {str(k): float(v) for k, v in data.items()}
        except (OSError, ValueError, AttributeError, TypeError):
            return {}

    def _save_glitter(self) -> None:
        path = self.state_dir / GLITTER_FILE
        tmp = path.with_suffix(".tmp")
        with suppress(OSError):
            tmp.write_text(json.dumps(self._glitter), encoding="utf-8")
            tmp.replace(path)

    async def _add_glitter(self, cookie: str | None, watched: set[str], live: dict[str, dict[str, Any]], now: float) -> None:
        """配信中の全ルーム (1 回の問い合わせ) から、グリッターのバッジのあるルームを覚え、
        GLITTER_KEEP のあいだに見たルームが配信していれば記録の対象にする。"""
        data = await self._fetch("/api/live/onlives", cookie)
        if not isinstance(data, dict):
            raise RuntimeError("配信中のルームの一覧の形式が不正です")
        lives = {x["room_url_key"]: x for g in data.get("onlives") or [] if isinstance(g, dict)
                 for x in g.get("lives") or [] if isinstance(x, dict) and x.get("room_url_key") and x.get("room_id")}
        new = [k for k, x in lives.items() if glitter_badge(x) and k not in self._glitter]
        for key, x in lives.items():
            if glitter_badge(x):
                self._glitter[key] = now
        old = [k for k, at in self._glitter.items() if now - at > GLITTER_KEEP]
        for key in old:
            del self._glitter[key]
        if new or old or now - self._glitter_saved > 3600:
            self._glitter_saved = now
            self._save_glitter()
        if new:
            log.info("グリッターを取ったルームを %d 件、新しく記録の対象にします (全部で %d 件)", len(new), len(self._glitter))
        for key in self._glitter:
            x = lives.get(key)
            if x is not None and key not in watched:
                live.setdefault(key, {"room_id": x["room_id"], "live_id": x.get("live_id"), "source": "グリッター"})

    async def run(self) -> None:
        log.info("ギフト記録を開始します (ルーム %d 件、イベント %d 件%s)", len(self.cfg.record_rooms), len(self.cfg.events),
                 "、グリッターを取ったルーム" if self.cfg.record_glitter else "")
        try:
            while True:
                try:
                    await self._refresh()
                except Exception as e:  # noqa: BLE001
                    log.warning("配信状況の確認に失敗: %s", e)
                await asyncio.sleep(REFRESH_INTERVAL)
        finally:
            for task in self._tasks.values():
                task.cancel()

    def _limits(self) -> None:
        # ループの中で作る (Python 3.9 の Semaphore は実行中のループに結び付く)
        if self._fetch_slots is None:
            self._fetch_slots = asyncio.Semaphore(FETCH_CONCURRENCY)
        if self._connect_slots is None:
            self._connect_slots = asyncio.Semaphore(CONNECT_CONCURRENCY)

    async def _fetch(self, path: str, cookie: str | None = None) -> Any:
        self._limits()
        assert self._fetch_slots is not None
        async with self._fetch_slots:
            return await aget(path, cookie)

    def _cookie(self, key: str | None = None) -> str | None:
        """ログインの Cookie。ログインして訪問しないルーム (no_visit_rooms) には付けない。"""
        if key is not None and key in set(self.cfg.no_visit_rooms or []):
            return None
        return session_cookie(Path(self.cfg.state_file))

    def _skip_cookie(self, key: str, cookie: str | None) -> str | None:
        return None if key in set(self.cfg.no_visit_rooms or []) else cookie

    async def _room_status(self, key: str, cookie: str | None) -> Any:
        try:
            st = await self._fetch(f"/api/room/status?room_url_key={key}", cookie)
        except Exception as e:  # noqa: BLE001
            log.warning("%s の配信状況を確認できません: %s", key, e)
            st = None
        if not isinstance(st, dict):
            self._incomplete = True
            return None
        return st

    async def _targets(self) -> dict[str, dict[str, Any]]:
        """配信中の記録対象ルーム {room_url_key: {room_id, live_id, source}}。

        イベントの参加ルーム一覧には live_id が無いため、イベント由来のルームの live_id は None。
        """
        cookie = self._cookie()
        watched = set(self.cfg.rooms)
        live: dict[str, dict[str, Any]] = {}
        self._incomplete = False

        keys = [key for key in self.cfg.record_rooms if key not in watched]
        statuses = await asyncio.gather(*[self._room_status(key, self._skip_cookie(key, cookie)) for key in keys])
        for key, st in zip(keys, statuses):
            room_id = st.get("room_id") if st else None
            if room_id and st.get("is_live"):
                live[key] = {"room_id": room_id, "live_id": st.get("live_id"), "source": "ルーム指定"}
        now = time.time()
        if self.cfg.record_glitter:
            try:
                await self._add_glitter(cookie, watched, live, now)
            except Exception as e:  # noqa: BLE001
                self._incomplete = True
                log.warning("グリッターのルームの確認に失敗: %s", e)
        for ev in self.cfg.events:
            try:
                await self._add_event(ev, cookie, watched, live, now)
            except Exception as e:  # noqa: BLE001 - 1 件の失敗で他のイベントまで止めない
                self._incomplete = True
                log.warning("イベント %s の確認に失敗: %s", ev, e)
        return live

    async def _add_event(self, ev: str, cookie: str | None, watched: set[str], live: dict[str, dict[str, Any]], now: float) -> None:
        info = self._events.get(ev)
        if info is None:
            info = await self._fetch(f"/api/event/{ev}/abstraction", cookie)
            if not isinstance(info, dict):
                raise RuntimeError("イベント情報の形式が不正です")
            self._events[ev] = info
            end = datetime.fromtimestamp(info.get("event_end_at") or 0, JST)
            log.info("イベント「%s」を記録対象にします (%s 終了)", info.get("title") or ev, f"{end:%m/%d %H:%M}")
        if info.get("event_end_at") and now > info["event_end_at"] + EVENT_GRACE:
            if ev not in self._ended_logged:
                log.info("イベント「%s」は終了したため記録をやめます", info.get("title") or ev)
                self._ended_logged.add(ev)
            return
        if info.get("event_start_at") and now < info["event_start_at"] - 3600:
            return
        for room in await event_rooms(ev, cookie, self._fetch):
            key = room.get("room_url_key")
            room_id = room.get("room_id")
            if key and room_id and room.get("is_on_live") and key not in watched:
                live.setdefault(key, {"room_id": room_id, "live_id": None, "source": info.get("title") or ev})

    async def _refresh(self) -> None:
        targets = await self._targets()
        running = {key for key, task in self._tasks.items() if not task.done()}

        # 配信終了のお知らせを受け損ねると古い配信に接続したまま残るため、配信していないと確認できたら止める。
        # 取得に失敗したものがある時は、失敗のせいで対象から外れただけかもしれないので止めない
        if not self._incomplete:
            for key in running - set(targets):
                log.info("%s は配信していないため記録を終えます", key)
                self._stop_task(key)

        # イベント由来のルームは live_id が分からないため、記録中のものだけ個別に確認する
        cookie = self._cookie()
        unknown = [key for key in running & set(targets) if targets[key].get("live_id") is None]
        for key, st in zip(unknown, await asyncio.gather(*[self._room_status(key, self._skip_cookie(key, cookie))
                                                           for key in unknown])):
            if st and st.get("live_id"):
                targets[key]["live_id"] = st["live_id"]

        for key, target in targets.items():
            task = self._tasks.get(key)
            if task is not None and not task.done():
                recorded, current = self._live_ids.get(key), target.get("live_id")
                if recorded is None or current is None or recorded == current:
                    continue
                log.info("%s の配信が切り替わったため接続し直します", key)
                self._stop_task(key)
            self._live_ids[key] = target.get("live_id")
            self._sources[key] = target.get("source") or ""
            self._tasks[key] = asyncio.ensure_future(self._record_room(key, target["room_id"]))
        for key in [k for k, task in self._tasks.items() if task.done()]:
            del self._tasks[key]
            self._live_ids.pop(key, None)

    def active(self) -> list[dict[str, Any]]:
        """記録中のルーム (管理画面用)。"""
        return [{"room": key, "name": self._names.get(key, key), "source": self._sources.get(key, ""),
                 "live_id": self._live_ids.get(key)}
                for key, task in list(self._tasks.items()) if not task.done()]

    def _stop_task(self, key: str) -> None:
        task = self._tasks.pop(key, None)
        self._live_ids.pop(key, None)
        if task is not None:
            task.cancel()

    async def _record_room(self, key: str, room_id: int) -> None:
        import websockets  # 記録機能を使う時だけ必要

        try:
            cookie = self._cookie(key)
            info = await self._fetch(f"/api/live/live_info?room_id={room_id}", cookie)
            if not isinstance(info, dict) or info.get("live_status") != 2 or not info.get("bcsvr_key"):
                return
            live_id = info.get("live_id")
            self._live_ids[key] = live_id
            self._names[key] = info.get("room_name") or key
            with suppress(Exception):  # 管理画面の一覧に名前で出すため
                from .listing import remember_rooms

                remember_rooms(self.state_dir, {key: (info.get("room_name"), room_id)})
            log.info("記録開始: %s", info.get("room_name") or key)
            count = 0
            connect_kwargs: dict[str, Any] = {"open_timeout": 20, "ping_interval": None}
            if cookie:
                connect_kwargs[ws_header_param(websockets.connect)] = {"Cookie": cookie}
            self._limits()
            assert self._connect_slots is not None
            connect_cm = websockets.connect(WS_URL, **connect_kwargs)
            # 接続の確立だけ同時数を抑え、つながったあとは件数の上限なく受信を続ける
            async with self._connect_slots:
                ws = await connect_cm.__aenter__()
            comments = comment_counts.counter(self.state_dir)
            comments.start(key, live_id)
            try:
                await ws.send(f"SUB\t{info['bcsvr_key']}")
                pinger = asyncio.ensure_future(self._ping(ws))
                stager = asyncio.ensure_future(self._stage_loop(key, room_id, live_id))
                try:
                    async for message in ws:
                        data = parse_message(message)
                        if not data:
                            continue
                        if data.get("t") == 2:
                            record_gift(self.state_dir, key, live_id, data)
                            count += 1
                        elif data.get("t") == 1:  # コメント (数だけ数える)
                            comments.feed(key, live_id, data)
                        elif data.get("t") in (5, 18, 1001):  # 来場・ファンレベルなどの知らせ
                            with suppress(Exception):
                                live_events.record(self.state_dir, key, live_id, data)
                        elif data.get("t") == 101:  # 配信終了
                            break
                finally:
                    pinger.cancel()
                    stager.cancel()
                    with suppress(Exception):
                        await self._save_stage(key, room_id, live_id)  # 配信の終わり (最後の順位)
                    with suppress(Exception):
                        comments.flush(key)
            finally:
                await connect_cm.__aexit__(None, None, None)
            log.info("記録終了: %s (ギフト %d 件)", info.get("room_name") or key, count)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - 次の確認で配信中なら再接続する
            log.debug("%s の記録が中断しました: %s", key, e)

    async def _stage_loop(self, key: str, room_id: int, live_id: Any) -> None:
        while True:
            await asyncio.sleep(STAGE_EVERY)
            with suppress(Exception):
                await self._save_stage(key, room_id, live_id)

    async def _save_stage(self, key: str, room_id: int, live_id: Any) -> None:
        """配信の順位 (ステージの上位 100 人) を、ルームの live_ranks に 1 行足す。"""
        data = await self._fetch(f"/api/live/stage_user_list?room_id={room_id}", self._cookie(key))
        rows = [(int(x.get("rank") or x.get("order_no") or 0), (x.get("user") or {}).get("user_id"))
                for x in (data or {}).get("stage_user_list") or [] if isinstance(x, dict)]
        rows = [[r, u] for r, u in rows if u]
        if rows:
            stage_ranks.append(self.state_dir, key, live_id, rows)

    @staticmethod
    async def _ping(ws: Any) -> None:
        while True:
            await asyncio.sleep(PING_INTERVAL)
            await ws.send("PING\tshowroom")
