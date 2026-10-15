"""視聴はしないルームに、配信が始まったら一度だけ入室する。"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

from .api import RoomStatus
from .watcher import Page

log = logging.getLogger("entrance")

ENTER_TIMEOUT = 60000  # ページの読み込みを待つ上限 (ミリ秒)


class RoomEntrance:
    """配信状況を順に確かめ、新しい配信を見つけたらページを開いて読み込みが終わったらすぐ閉じる。

    ページは同時に 1 つしか開かない (小さいサーバーで視聴やコメントを遅くしないため)。
    """

    def __init__(
        self,
        rooms: list[str],
        interval: float,
        fetch_status: Callable[[str], Awaitable[RoomStatus]],
        new_page: Callable[[], Awaitable[Page]],
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self.rooms = rooms
        self.interval = interval
        self._fetch_status = fetch_status
        self._new_page = new_page
        self._sleep = sleep
        # ルームごとに、入室を済ませた配信の live_id
        self.entered: dict[str, int] = {}

    async def run(self) -> None:
        log.info("配信が始まったら一度だけ入室するルーム: %d 件", len(self.rooms))
        while True:
            await self.step()
            await self._sleep(self.interval)

    async def step(self) -> None:
        for key in self.rooms:
            try:
                status = await self._fetch_status(key)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - 1 ルームの失敗で他のルームを止めない
                log.debug("%s の配信状況を取得できません: %s", key, e)
                continue
            # live_id が分からない配信は、同じ配信に何度も入室しないよう見送る
            if not status.is_live or status.live_id is None or self.entered.get(key) == status.live_id:
                continue
            if await self._enter(status):
                self.entered[key] = status.live_id

    async def _enter(self, status: RoomStatus) -> bool:
        log.info("%s の配信が始まったので入室します", status.room_name)
        page = await self._new_page()
        try:
            await page.goto(status.url, wait_until="load", timeout=ENTER_TIMEOUT)
            return True
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - 次の確認でもう一度試す
            log.warning("%s に入室できませんでした: %s", status.room_name, str(e).splitlines()[0])
            return False
        finally:
            try:
                await page.close()
            except Exception as e:  # noqa: BLE001
                log.debug("ページのクローズに失敗: %s", e)
