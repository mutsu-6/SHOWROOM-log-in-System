"""視聴はしないルームに、配信が始まったら一度だけ入室する。"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any, Awaitable, Callable

from .api import RoomStatus
from .watcher import Page

log = logging.getLogger("entrance")

ENTERED_FILE = "entered.json"
# 配信ページは読み込み (load) のあとにリアルタイム通信へつなぎ、SUB を送って配信に参加する (計測では開いてから約 6 秒)。
# 重いサーバーでは読み込み自体が遅いため、全体の上限は長めに取る
ENTER_TIMEOUT = 30.0  # 開いてから参加を確認するまでの上限 (秒)
STAY_AFTER_JOIN = 5.0  # 参加を確認してから閉じるまで (秒)


def is_join_frame(payload: Any) -> bool:
    """配信への参加 (リアルタイム通信で送る SUB) か。"""
    if isinstance(payload, (bytes, bytearray)):
        payload = payload.decode("utf-8", "replace")
    return isinstance(payload, str) and payload.startswith("SUB\t")


class RoomEntrance:
    """配信状況を順に確かめ、新しい配信を見つけたらページを開き、配信に参加したのを確かめてから閉じる。

    ページは同時に 1 つしか開かない (小さいサーバーで視聴やコメントを遅くしないため)。
    """

    def __init__(
        self,
        rooms: list[str],
        interval: float,
        fetch_status: Callable[[str], Awaitable[RoomStatus]],
        new_page: Callable[[], Awaitable[Page]],
        state_dir: Path | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        enter_timeout: float = ENTER_TIMEOUT,
        stay: float = STAY_AFTER_JOIN,
    ):
        self.rooms = rooms
        self.interval = interval
        self._fetch_status = fetch_status
        self._new_page = new_page
        self._sleep = sleep
        self.enter_timeout = enter_timeout
        self.stay = stay
        # 再起動や設定の反映のあとに同じ配信へ入室し直さないよう、ファイルにも残す
        self._path = Path(state_dir) / ENTERED_FILE if state_dir is not None else None
        # ルームごとに、入室を済ませた配信の live_id
        self.entered: dict[str, int] = self._load()

    def _load(self) -> dict[str, int]:
        if self._path is None:
            return {}
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {k: v for k, v in data.items() if k in self.rooms and isinstance(v, int)}

    def _save(self) -> None:
        if self._path is None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.entered), encoding="utf-8")
            os.replace(tmp, self._path)
        except OSError as e:
            log.debug("入室済みの記録を保存できません: %s", e)

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
                self._save()

    async def _enter(self, status: RoomStatus) -> bool:
        log.info("%s の配信が始まったので入室します", status.room_name)
        joined = asyncio.Event()

        def on_websocket(ws: Any) -> None:
            ws.on("framesent", lambda payload: joined.set() if is_join_frame(payload) else None)

        page = await self._new_page()
        try:
            if hasattr(page, "on"):
                page.on("websocket", on_websocket)
            await asyncio.wait_for(self._join(page, status, joined), timeout=self.enter_timeout + self.stay)
            log.info("%s に入室しました", status.room_name)
            return True
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            log.warning("%s で配信への参加を %.0f 秒以内に確認できませんでした。次の確認で入室し直します",
                        status.room_name, self.enter_timeout)
            return False
        except Exception as e:  # noqa: BLE001 - 次の確認でもう一度試す
            log.warning("%s に入室できませんでした: %s", status.room_name, str(e).splitlines()[0])
            return False
        finally:
            try:
                await page.close()
            except Exception as e:  # noqa: BLE001
                log.debug("ページのクローズに失敗: %s", e)

    async def _join(self, page: Page, status: RoomStatus, joined: asyncio.Event) -> None:
        # watcher と同じく応答が始まった時点で次へ進む (重いサーバーでは load まで待つと遅い)
        await page.goto(status.url, wait_until="commit", timeout=self.enter_timeout * 1000)
        await joined.wait()
        await asyncio.sleep(self.stay)
