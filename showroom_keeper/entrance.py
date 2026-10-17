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
import time
import urllib.request
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable

from .api import BASE_URL, RoomStatus

log = logging.getLogger("entrance")

ENTERED_FILE = "entered.json"
GREETING_DELAY = 10.0  # 配信ページが greeting を呼ぶまでの時間 (秒)
ENTER_TRIES = 3  # 1 つの配信で入室を試す回数 (失敗が続く時に、配信が終わるまで入り直し続けないように)
ENTER_PARALLEL = 3  # 全ルームの入室で、同時に入室する数 (1 回 10 数秒なので、1 分に 15 件ほど)
REPORT_EVERY = 3600.0  # 全ルームの入室で、入室した件数を記録 (ログ) に出す間隔 (秒)
FORGET_AFTER = 3600.0  # 全ルームの入室で、配信中の一覧にこの秒数出てこないルームは入室済みの記録から消す
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
        rooms: list[str] | None,
        interval: float,
        fetch_status: Callable[[str], Awaitable[RoomStatus]] | None,
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
        # ルームごとに、入室に失敗した配信の (live_id, 回数)
        self._failures: dict[str, tuple[int, int]] = {}
        # 1 件ごとの記録 (ログ) の重さ。全ルームの入室は件数が多いので細かい記録にまわす
        self._info, self._warn = logging.INFO, logging.WARNING
        self.counts = {"entered": 0, "failed": 0}

    def _load(self) -> dict[str, int]:
        if self._path is None:
            return {}
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {k: v for k, v in data.items() if (self.rooms is None or k in self.rooms) and isinstance(v, int)}

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
        log.info("配信が始まったら一度だけ入室するルーム: %d 件", len(self.rooms or []))
        while True:
            await self.step()
            await self._sleep(self.interval)

    async def step(self) -> None:
        assert self.rooms is not None and self._fetch_status is not None
        for key in self.rooms:
            try:
                status = await self._fetch_status(key)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - 1 ルームの失敗で他のルームを止めない
                log.debug("%s の配信状況を取得できません: %s", key, e)
                continue
            if self._due(status):
                await self._attempt(status)

    def _due(self, status: RoomStatus) -> bool:
        """まだ入室していない配信で、試す回数が残っているか。"""
        # live_id が分からない配信は、同じ配信に何度も入室しないよう見送る
        key = status.room_url_key
        if not status.is_live or status.live_id is None or self.entered.get(key) == status.live_id:
            return False
        live_id, failures = self._failures.get(key, (status.live_id, 0))
        return live_id != status.live_id or failures < ENTER_TRIES

    async def _attempt(self, status: RoomStatus) -> None:
        key = status.room_url_key
        if await self._enter(status):
            self.entered[key] = status.live_id
            self._failures.pop(key, None)
            self.counts["entered"] += 1
            self._save()
            return
        self.counts["failed"] += 1
        live_id, failures = self._failures.get(key, (status.live_id, 0))
        failures = failures + 1 if live_id == status.live_id else 1
        self._failures[key] = (status.live_id, failures)
        if failures >= ENTER_TRIES:
            log.log(self._warn, "%s の今の配信には %d 回入室できなかったので、次の配信まで入室しません",
                    status.room_name, ENTER_TRIES)

    async def _enter(self, status: RoomStatus) -> bool:
        if status.room_id is None:
            return False
        log.log(self._info, "%s の配信が始まったので入室します", status.room_name)
        try:
            get = self._session(await self._cookies())
            if not await asyncio.to_thread(self._open, get, status):
                log.log(self._warn, "%s に入室できません: ログインしていません (次の確認で入室し直します)", status.room_name)
                return False
            # 配信ページと同じく、開いてから greeting_delay 秒たってから呼ぶ (これで入室のお知らせとアバターが出る)
            await self._sleep(self.greeting_delay)
            answer = await asyncio.to_thread(get, f"/api/room/greeting?room_id={status.room_id}")
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - 次の確認でもう一度試す
            log.log(self._warn, "%s に入室できませんでした: %s", status.room_name,
                    str(e).splitlines()[0] if str(e) else type(e).__name__)
            return False
        if not _ok(answer):
            log.log(self._warn, "%s に入室できませんでした (greeting の応答: %s)", status.room_name, answer[:100])
            return False
        log.log(self._info, "%s に入室しました", status.room_name)
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


class AllRoomsEntrance(RoomEntrance):
    """SHOWROOM の全ルームで、配信が始まったら一度だけ入室する。

    配信中の一覧 (1 回の問い合わせで SHOWROOM 全体) を interval ごとに取り、新しい配信に入室する。
    1 回の入室は 10 数秒かかるので、ENTER_PARALLEL 件まで同時に入室し、一覧の取得は止めない。
    """

    def __init__(self, interval: float, fetch_lives: Callable[[], Awaitable[list[RoomStatus]]],
                 cookies: Callable[[], Awaitable[list[dict[str, Any]]]], skip: Iterable[str] = (),
                 state_dir: Path | None = None, clock: Callable[[], float] = time.time, parallel: int = ENTER_PARALLEL,
                 **kw: Any):
        super().__init__(None, interval, None, cookies, state_dir=state_dir, **kw)
        self._fetch_lives = fetch_lives
        self.skip = set(skip)
        self._clock = clock
        self._parallel = parallel
        self._slots: asyncio.Semaphore | None = None
        self._busy: set[str] = set()
        self._tasks: set[asyncio.Task[None]] = set()
        self._info = self._warn = logging.DEBUG
        now = clock()
        self._reported_at = now
        # 配信中の一覧に最後に出た時刻。しばらく出てこないルームは、入室済みの記録から消す (記録を小さく保つ)
        self._seen: dict[str, float] = {k: now for k in self.entered}

    async def run(self) -> None:
        log.info("SHOWROOM の全ルームで、配信が始まったら一度だけ入室します (同時に %d 件まで)", self._parallel)
        try:
            while True:
                await self.step()
                await self._sleep(self.interval)
        finally:
            for t in list(self._tasks):
                t.cancel()

    async def step(self) -> None:
        try:
            lives = await self._fetch_lives()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - 次の確認でもう一度取る
            log.debug("配信中の一覧を取得できません: %s", e)
            return
        if self._slots is None:  # Python 3.9 では、使うイベントループの中で作る
            self._slots = asyncio.Semaphore(self._parallel)
        now = self._clock()
        for status in lives:
            self._seen[status.room_url_key] = now
        gone = [k for k in self.entered if now - self._seen.get(k, now) > FORGET_AFTER]
        for k in gone:
            self.entered.pop(k, None)
            self._seen.pop(k, None)
            self._failures.pop(k, None)
        if gone:
            self._save()
        if now - self._reported_at >= REPORT_EVERY:
            self._reported_at = now
            log.info("全ルームの入室: この 1 時間に %d 件入室しました (失敗 %d 件)", self.counts["entered"], self.counts["failed"])
            self.counts = {"entered": 0, "failed": 0}
        for status in lives:
            key = status.room_url_key
            if key in self.skip or key in self._busy or not self._due(status):
                continue
            self._busy.add(key)
            task = asyncio.ensure_future(self._one(status))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _one(self, status: RoomStatus) -> None:
        assert self._slots is not None
        try:
            async with self._slots:
                await self._attempt(status)
        finally:
            self._busy.discard(status.room_url_key)

    async def wait_idle(self) -> None:
        """始めた入室が全部終わるまで待つ (テスト用)。"""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)


def _ok(answer: str) -> bool:
    try:
        return bool(json.loads(answer).get("ok"))
    except (ValueError, AttributeError):
        return False

