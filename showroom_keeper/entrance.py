"""視聴はしないルームに、配信が始まったら一度だけ入室する (ブラウザのページは開かない)。

配信ページは、ログインしていれば開いてから 10 秒後に /api/room/greeting を呼ぶ。SHOWROOM はこれを受けて
入室のお知らせ (リアルタイム通信の t=18。「○○さんが初訪問」など) を流し、観客席にアバターが出る。
配信ページを開いて受け取る Cookie (uuid) を付けたまま、ページと同じ順に API を呼べば、ブラウザがなくても同じになる
(2026-10 に確かめた。ページを 10 秒より前に閉じると、人数には数えられるがアバターは出ない)。
"""

from __future__ import annotations

import asyncio
import http.cookiejar
import json
import logging
import os
import urllib.request
from pathlib import Path
from typing import Any, Awaitable, Callable

from .api import BASE_URL, RoomStatus

log = logging.getLogger("entrance")

ENTERED_FILE = "entered.json"
GREETING_DELAY = 10.0  # 配信ページが greeting を呼ぶまでの時間 (秒)
REQUEST_TIMEOUT = 20.0
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/130.0.0.0 Safari/537.36")

# Get(path) -> 応答の本文。1 回の入室の間、Cookie (配信ページで受け取る uuid も) を引き継ぐ
Get = Callable[[str], str]


def cookie_session(cookies: list[dict[str, Any]]) -> Get:
    """ブラウザのログイン状態の Cookie を持った、1 回の入室用の問い合わせ。"""
    jar = http.cookiejar.CookieJar()
    for c in cookies:
        domain = c.get("domain") or ""
        if "showroom-live.com" not in domain:
            continue
        jar.set_cookie(http.cookiejar.Cookie(
            0, c["name"], c["value"], None, False, domain, True, domain.startswith("."), c.get("path") or "/", True,
            bool(c.get("secure")), None, False, None, None, {}))
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    opener.addheaders = [("User-Agent", USER_AGENT), ("Accept-Language", "ja")]

    def get(path: str) -> str:
        with opener.open(BASE_URL + path, timeout=REQUEST_TIMEOUT) as resp:
            return resp.read().decode("utf-8", "replace")

    return get


class RoomEntrance:
    """配信状況を順に確かめ、新しい配信を見つけたら入室する (同時に 1 ルームずつ)。"""

    def __init__(
        self,
        rooms: list[str],
        interval: float,
        fetch_status: Callable[[str], Awaitable[RoomStatus]],
        cookies: Callable[[], Awaitable[list[dict[str, Any]]]],
        state_dir: Path | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        session: Callable[[list[dict[str, Any]]], Get] = cookie_session,
        greeting_delay: float = GREETING_DELAY,
    ):
        self.rooms = rooms
        self.interval = interval
        self._fetch_status = fetch_status
        self._cookies = cookies
        self._sleep = sleep
        self._session = session
        self.greeting_delay = greeting_delay
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
        if status.room_id is None:
            return False
        log.info("%s の配信が始まったので入室します", status.room_name)
        try:
            get = self._session(await self._cookies())
            if not await asyncio.to_thread(self._open, get, status):
                log.warning("%s に入室できません: ログインしていません (次の確認で入室し直します)", status.room_name)
                return False
            # 配信ページと同じく、開いてから greeting_delay 秒たってから呼ぶ (これで入室のお知らせとアバターが出る)
            await self._sleep(self.greeting_delay)
            answer = await asyncio.to_thread(get, f"/api/room/greeting?room_id={status.room_id}")
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - 次の確認でもう一度試す
            log.warning("%s に入室できませんでした: %s", status.room_name, str(e).splitlines()[0] if str(e) else type(e).__name__)
            return False
        if not _ok(answer):
            log.warning("%s に入室できませんでした (greeting の応答: %s)", status.room_name, answer[:100])
            return False
        log.info("%s に入室しました", status.room_name)
        return True

    @staticmethod
    def _open(get: Get, status: RoomStatus) -> bool:
        """配信ページを開いたときと同じ順に問い合わせる。ログインしていれば True。"""
        rid = status.room_id
        get(f"/r/{status.room_url_key}")  # ここで受け取る Cookie (uuid) が無いと、greeting を呼んでもアバターが出ない
        get(f"/api/live/live_info?room_id={rid}")
        user = json.loads(get(f"/api/live/current_user?room_id={rid}") or "{}")
        get(f"/api/live/polling?room_id={rid}")
        return bool(user.get("is_login"))


def _ok(answer: str) -> bool:
    try:
        return bool(json.loads(answer).get("ok"))
    except (ValueError, AttributeError):
        return False

