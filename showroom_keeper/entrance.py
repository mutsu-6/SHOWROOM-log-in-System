"""視聴はしないルームに、配信が始まったら一度だけ入室する (ブラウザのページは開かない)。

観客席 (ステージ) にアバターが並ぶのは /api/live/current_user を呼んだ時 (ステージの一覧 stage_user_list に入る)。
配信ページは、ログインしていれば開いてから 10 秒後に /api/room/greeting を呼び、SHOWROOM はこれを受けて
入室のお知らせ (リアルタイム通信の t=18。「○○さんが初訪問」など) を流すことがある (毎回ではない。配信ページと
同じ順に呼んで 10 秒待っても、流れないことがある。2026-10-18 に確かめた)。
greeting には、配信ページを開くと受け取る Cookie (uuid) が要る。uuid を 1 回もらって使い回し、
入室は current_user と greeting の 2 回で済ませる (待たない)。
"""

from __future__ import annotations

import asyncio
import http.cookiejar
import json
import logging
import os
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable

from .api import BASE_URL, RoomStatus

log = logging.getLogger("entrance")

ENTERED_FILE = "entered.json"
UUID_EVERY = 6 * 3600.0  # uuid の Cookie を配信ページで取り直す間隔 (秒)。期限の無い Cookie なので念のため
ENTER_GAP = 1.0  # 全ルームの入室で、入室と入室の間を空ける時間 (秒)。起動直後に数百件をまとめて送らないように
ENTER_TRIES = 3  # 1 つの配信で入室を試す回数 (失敗が続く時に、配信が終わるまで入り直し続けないように)
REPORT_EVERY = 3600.0  # 全ルームの入室で、入室した件数を記録 (ログ) に出す間隔 (秒)
FORGET_AFTER = 3600.0  # 全ルームの入室で、配信中の一覧にこの秒数出てこないルームは入室済みの記録から消す
REQUEST_TIMEOUT = 20.0
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/130.0.0.0 Safari/537.36")


class Session:
    """ブラウザのログイン状態の Cookie (と、あれば使い回す uuid) を持った、1 回の入室用の問い合わせ。"""

    def __init__(self, cookies: list[dict[str, Any]], uuid: str | None = None):
        self._jar = http.cookiejar.CookieJar()
        for c in cookies:
            domain = c.get("domain") or ""
            if "showroom-live.com" in domain:
                self._set(c["name"], c["value"], domain, c.get("path") or "/", bool(c.get("secure")))
        if uuid is not None:
            self._set("uuid", uuid, "www.showroom-live.com", "/", False)
        self._opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self._jar))
        self._opener.addheaders = [("User-Agent", USER_AGENT), ("Accept-Language", "ja")]

    def _set(self, name: str, value: str, domain: str, path: str, secure: bool) -> None:
        self._jar.set_cookie(http.cookiejar.Cookie(0, name, value, None, False, domain, True, domain.startswith("."),
                                                   path, True, secure, None, False, None, None, {}))

    @property
    def uuid(self) -> str | None:
        return next((c.value for c in self._jar if c.name == "uuid"), None)

    def get(self, path: str) -> str:
        with self._opener.open(BASE_URL + path, timeout=REQUEST_TIMEOUT) as resp:
            return resp.read().decode("utf-8", "replace")

    def post(self, path: str, form: dict[str, Any]) -> str:
        """配信ページと同じく、CSRF トークンを付けたフォームで送る (トークンは毎回取る)。"""
        token = json.loads(self.get("/api/csrf_token")).get("csrf_token")
        if not token:
            raise RuntimeError("CSRF トークンを取得できませんでした")
        body = urllib.parse.urlencode({**form, "csrf_token": token}).encode()
        req = urllib.request.Request(BASE_URL + path, data=body, method="POST",
                                     headers={"Content-Type": "application/x-www-form-urlencoded; charset=utf-8"})
        with self._opener.open(req, timeout=REQUEST_TIMEOUT) as resp:
            return resp.read().decode("utf-8", "replace")


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
        session: Callable[..., Session] = Session,
        clock: Callable[[], float] = time.time,
    ):
        self.rooms = rooms
        self.interval = interval
        self._fetch_status = fetch_status
        self._cookies = cookies
        self._sleep = sleep
        self._session = session
        self._clock = clock
        self._uuid: str | None = None  # 使い回す uuid の Cookie と、受け取った時刻
        self._uuid_at = 0.0
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
            cookies = await self._cookies()
            answer = await self._greet(cookies, status, renew=False)
            if not _ok(answer) and self._uuid is not None:  # 使い回した uuid が古くなった時は、取り直して 1 回だけ試し直す
                self._uuid = None
                answer = await self._greet(cookies, status, renew=True)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - 次の確認でもう一度試す
            if getattr(e, "code", None) == 401:
                log.log(self._warn, "%s に入室できません: ログインしていません (次の確認で入室し直します)", status.room_name)
            else:
                log.log(self._warn, "%s に入室できませんでした: %s", status.room_name,
                        str(e).splitlines()[0] if str(e) else type(e).__name__)
            return False
        if not _ok(answer):
            log.log(self._warn, "%s に入室できませんでした (greeting の応答: %s)", status.room_name, answer[:100])
            return False
        log.log(self._info, "%s に入室しました", status.room_name)
        return True

    async def _greet(self, cookies: list[dict[str, Any]], status: RoomStatus, renew: bool) -> str:
        now = self._clock()
        reuse = None if renew or now - self._uuid_at > UUID_EVERY else self._uuid
        s = self._session(cookies, reuse)
        if s.uuid is None:  # ブラウザのログイン状態にも無ければ、配信ページを 1 回開いてもらう
            await asyncio.to_thread(s.get, f"/r/{status.room_url_key}")
            if s.uuid is not None:
                self._uuid, self._uuid_at = s.uuid, now
        # 観客席 (ステージ) にアバターが並ぶのは current_user を呼んだ時。greeting だけではお知らせしか流れない
        await asyncio.to_thread(s.get, f"/api/live/current_user?room_id={status.room_id}")
        return await asyncio.to_thread(s.get, f"/api/room/greeting?room_id={status.room_id}")


class AllRoomsEntrance(RoomEntrance):
    """SHOWROOM の全ルームで、配信が始まったら一度だけ入室する。

    配信中の一覧 (1 回の問い合わせで SHOWROOM 全体) を interval ごとに取り、新しい配信に ENTER_GAP 秒ずつ空けて入室する。
    """

    def __init__(self, interval: float, fetch_lives: Callable[[], Awaitable[list[RoomStatus]]],
                 cookies: Callable[[], Awaitable[list[dict[str, Any]]]], skip: Iterable[str] = (),
                 state_dir: Path | None = None, clock: Callable[[], float] = time.time, gap: float = ENTER_GAP,
                 **kw: Any):
        super().__init__(None, interval, None, cookies, state_dir=state_dir, clock=clock, **kw)
        self._fetch_lives = fetch_lives
        self.skip = set(skip)
        self.gap = gap
        self._info = self._warn = logging.DEBUG
        now = clock()
        self._reported_at = now
        # 配信中の一覧に最後に出た時刻。しばらく出てこないルームは、入室済みの記録から消す (記録を小さく保つ)
        self._seen: dict[str, float] = {k: now for k in self.entered}

    async def run(self) -> None:
        log.info("SHOWROOM の全ルームで、配信が始まったら一度だけ入室します")
        while True:
            await self.step()
            await self._sleep(self.interval)

    async def step(self) -> None:
        try:
            lives = await self._fetch_lives()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - 次の確認でもう一度取る
            log.debug("配信中の一覧を取得できません: %s", e)
            return
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
            if status.room_url_key in self.skip or not self._due(status):
                continue
            await self._attempt(status)
            await self._sleep(self.gap)


def _ok(answer: str) -> bool:
    try:
        return bool(json.loads(answer).get("ok"))
    except (ValueError, AttributeError):
        return False

