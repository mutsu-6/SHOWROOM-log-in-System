"""ブラウザを開かずにギフトを記録する (指定ルーム・イベント参加ルーム)。

- 設定の record_rooms (最大 30) と events (最大 3、参加ルームすべて) が対象。
- 2 分ごとに配信状況を確認し、配信中のルームだけリアルタイム通信 (WebSocket) を購読してギフト (t=2) を記録する。
  受信するだけで、こちらからはコメント等を送らない (接続維持の PING のみ)。
- イベントは終了日時を過ぎると自動で対象外になる。
- 視聴中 (rooms) のルームはブラウザ側で記録しているため対象外にする (二重記録防止)。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

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


def get_json(path: str) -> Any:
    req = urllib.request.Request(f"{BASE_URL}{path}", headers={"User-Agent": "showroom-keeper", "Accept-Language": "ja"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.load(resp)


async def aget(path: str) -> Any:
    return await asyncio.get_running_loop().run_in_executor(None, get_json, path)


async def event_rooms(event_key: str) -> list[dict[str, Any]]:
    """イベントの参加ルーム一覧 (全ページ)。"""
    rooms, page = [], 1
    while page:
        data = await aget(f"/api/event/{event_key}/rooms?page={page}")
        rooms.extend(data.get("rooms") or [])
        page = data.get("next_page") if data.get("next_page") and data.get("next_page") != page else None
        if page and page > 50:  # 念のための上限
            break
    return rooms


class GiftRecorder:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.state_dir = Path(cfg.state_file).parent
        self._tasks: dict[str, asyncio.Task] = {}
        self._events: dict[str, dict[str, Any]] = {}
        self._ended_logged: set[str] = set()

    async def run(self) -> None:
        log.info("ギフト記録を開始します (ルーム %d 件、イベント %d 件)", len(self.cfg.record_rooms), len(self.cfg.events))
        try:
            while True:
                try:
                    await self._refresh()
                except Exception as e:  # noqa: BLE001
                    log.warning("配信状況の確認に失敗: %s", e)
                await asyncio.sleep(REFRESH_INTERVAL)
        finally:
            for t in self._tasks.values():
                t.cancel()

    async def _targets(self) -> dict[str, dict[str, Any]]:
        """配信中の記録対象ルーム {room_url_key: {room_id, source}}。"""
        watched = set(self.cfg.rooms)
        live: dict[str, dict[str, Any]] = {}
        for key in self.cfg.record_rooms:
            if key in watched:
                continue
            st = await aget(f"/api/room/status?room_url_key={key}")
            if st.get("is_live"):
                live[key] = {"room_id": st["room_id"], "source": "ルーム指定"}
        now = time.time()
        for ev in self.cfg.events:
            info = self._events.get(ev)
            if info is None:
                info = await aget(f"/api/event/{ev}/abstraction")
                self._events[ev] = info
                end = datetime.fromtimestamp(info.get("event_end_at") or 0, JST)
                log.info("イベント「%s」を記録対象にします (%s 終了)", info.get("title") or ev, f"{end:%m/%d %H:%M}")
            if info.get("event_end_at") and now > info["event_end_at"] + EVENT_GRACE:
                if ev not in self._ended_logged:
                    log.info("イベント「%s」は終了したため記録をやめます", info.get("title") or ev)
                    self._ended_logged.add(ev)
                continue
            if info.get("event_start_at") and now < info["event_start_at"] - 3600:
                continue
            for r in await event_rooms(ev):
                key = r.get("room_url_key")
                if key and r.get("is_on_live") and key not in watched:
                    live.setdefault(key, {"room_id": r["room_id"], "source": info.get("title") or ev})
        return live

    async def _refresh(self) -> None:
        targets = await self._targets()
        for key, t in targets.items():
            task = self._tasks.get(key)
            if task is None or task.done():
                self._tasks[key] = asyncio.ensure_future(self._record_room(key, t["room_id"]))
        for key in [k for k, t in self._tasks.items() if t.done()]:
            del self._tasks[key]

    async def _record_room(self, key: str, room_id: int) -> None:
        import websockets  # 記録機能を使う時だけ必要

        try:
            info = await aget(f"/api/live/live_info?room_id={room_id}")
            if info.get("live_status") != 2 or not info.get("bcsvr_key"):
                return
            live_id = info.get("live_id")
            log.info("記録開始: %s", info.get("room_name") or key)
            count = 0
            async with websockets.connect(WS_URL, open_timeout=20, ping_interval=None) as ws:
                await ws.send(f"SUB\t{info['bcsvr_key']}")
                pinger = asyncio.ensure_future(self._ping(ws))
                try:
                    async for message in ws:
                        data = parse_message(message)
                        if not data:
                            continue
                        if data.get("t") == 2:
                            record_gift(self.state_dir, key, live_id, data)
                            count += 1
                        elif data.get("t") == 101:  # 配信終了
                            break
                finally:
                    pinger.cancel()
            log.info("記録終了: %s (ギフト %d 件)", info.get("room_name") or key, count)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - 次の確認で配信中なら再接続する
            log.debug("%s の記録が中断しました: %s", key, e)

    @staticmethod
    async def _ping(ws: Any) -> None:
        while True:
            await asyncio.sleep(PING_INTERVAL)
            await ws.send("PING\tshowroom")
