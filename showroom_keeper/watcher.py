"""1 ルーム分の視聴状態を維持するウォッチャー。"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, Protocol

from .api import RoomStatus
from .config import Config

# 動画要素の状態を返しつつ、止まっていたら(ミュートで)再生を再開する
PROBE_JS = """
() => {
  const text = document.body ? document.body.innerText : '';
  if (text.includes('視聴不可のルーム')) return { blocked: true };
  const v = document.querySelector('video');
  if (!v) return null;
  if (MUTE) v.muted = true;
  if (v.paused) { v.play().catch(() => {}); }
  return { time: v.currentTime, paused: v.paused, ready: v.readyState };
}
"""


COMMENT_INPUT = 'input[placeholder="コメントを投稿"]'
COMMENT_SUBMIT = "button.st-comment__button"

# 自分のコメントがコメント履歴に載ったかを確認する
COMMENT_LOG_JS = """
async ([roomId, text, since]) => {
  const get = async (path) => (await fetch(path, { credentials: 'include' })).json();
  const me = await get('/api/live/current_user?room_id=' + roomId).catch(() => ({}));
  const log = (await get('/api/live/comment_log?room_id=' + roomId)).comment_log || [];
  return log.some(c => c.comment === text && (c.created_at || since) >= since
                       && (!me.user_id || c.user_id === me.user_id));
}
"""


class Page(Protocol):  # playwright.async_api.Page と互換
    async def goto(self, url: str, **kwargs: Any) -> Any: ...
    async def fill(self, selector: str, value: str, **kwargs: Any) -> None: ...
    async def click(self, selector: str, **kwargs: Any) -> None: ...
    async def reload(self, **kwargs: Any) -> Any: ...
    async def evaluate(self, expression: str, arg: Any = None) -> Any: ...
    async def close(self) -> None: ...
    def is_closed(self) -> bool: ...


StatusFetcher = Callable[[str], Awaitable[RoomStatus]]
PageFactory = Callable[[], Awaitable[Page]]
Clock = Callable[[], float]


class RoomWatcher:
    def __init__(
        self,
        room_url_key: str,
        config: Config,
        fetch_status: StatusFetcher,
        new_page: PageFactory,
        clock: Clock = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self.key = room_url_key
        self.cfg = config
        self._fetch_status = fetch_status
        self._new_page = new_page
        self._clock = clock
        self._sleep = sleep
        self.log = logging.getLogger(f"room.{room_url_key}")

        self.page: Page | None = None
        self.status: RoomStatus | None = None
        self.last_video_time: float | None = None
        self.stall_count = 0
        self.opened_at: float | None = None
        self.last_reload_at: float | None = None
        self.last_status_at: float | None = None
        self.watched_seconds = 0.0
        self.reload_count = 0
        self.blocked = False
        self._failures = 0
        self._stop = asyncio.Event()

    # ------------------------------------------------------------------ 公開 API
    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        self.log.info("監視を開始します")
        try:
            while not self._stop.is_set():
                delay = await self.step()
                await self._wait(delay)
        finally:
            await self._close_page()
            self.log.info("監視を終了しました (累計視聴 %.0f 分)", self.watched_seconds / 60)

    async def step(self) -> float:
        """1 サイクル分の処理を行い、次のサイクルまでの待ち時間(秒)を返す。"""
        try:
            now = self._clock()
            if self.last_status_at is None or now - self.last_status_at >= self.cfg.status_poll_interval:
                await self._refresh_status()

            should_open = bool(self.status and (self.status.is_live or self.cfg.keep_open_when_offline))
            if should_open:
                if not self._page_alive():
                    await self._open_page()
                else:
                    await self._maintain_page()
            elif self.page is not None:
                self.log.info("配信が終了しました。次の配信開始を待ちます")
                await self._close_page()

            self._failures = 0
            return self.cfg.health_check_interval if self.page else self.cfg.status_poll_interval
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - どんな障害でも監視は継続する
            self._failures += 1
            delay = min(self.cfg.max_backoff, 5 * 2 ** (self._failures - 1))
            self.log.warning("エラーが発生しました (%d 回目): %s / %.0f 秒後に再試行", self._failures, e, delay)
            await self._close_page()
            return delay

    async def post_comment(self, text: str) -> tuple[bool, str]:
        """開いている配信ページのコメント欄から投稿し、コメント履歴で反映を確認する。"""
        if not self._page_alive() or not (self.status and self.status.is_live):
            return False, "配信中のページが開いていません"
        if self.blocked:
            return False, "このルームは視聴できない状態です"
        page = self.page
        assert page is not None
        since = int(time.time()) - 5
        await page.fill(COMMENT_INPUT, text, timeout=10000)
        await page.click(COMMENT_SUBMIT, timeout=10000)
        for _ in range(8):
            await self._sleep(1)
            if await page.evaluate(COMMENT_LOG_JS, [self.status.room_id, text, since]):
                return True, "投稿しました"
        return False, "投稿を確認できませんでした (未ログイン、コメント制限、または同じ内容の連投の可能性)"

    # ------------------------------------------------------------------ 内部処理
    async def _wait(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def _refresh_status(self) -> None:
        prev = self.status
        self.status = await self._fetch_status(self.key)
        self.last_status_at = self._clock()
        if prev is None or prev.is_live != self.status.is_live:
            state = "配信中" if self.status.is_live else "配信していません"
            self.log.info("%s: %s", self.status.room_name, state)

    def _page_alive(self) -> bool:
        return self.page is not None and not self.page.is_closed()

    async def _open_page(self) -> None:
        await self._close_page()
        assert self.status is not None
        self.log.info("視聴ページを開きます: %s", self.status.url)
        page = await self._new_page()
        self.page = page
        await page.goto(self.status.url, wait_until="commit", timeout=60000)
        self.opened_at = self.last_reload_at = self._clock()
        self._reset_health()

    async def _reload(self, reason: str) -> None:
        assert self.page is not None
        self.log.info("ページを再読み込みします (%s)", reason)
        await self.page.reload(wait_until="commit", timeout=60000)
        self.last_reload_at = self._clock()
        self.reload_count += 1
        self._reset_health()

    def _reset_health(self) -> None:
        self.last_video_time = None
        self.stall_count = 0

    async def _maintain_page(self) -> None:
        assert self.page is not None
        now = self._clock()
        period = self.cfg.periodic_reload_minutes * 60
        if period > 0 and self.last_reload_at is not None and now - self.last_reload_at >= period:
            await self._reload("定期リロード")
            return

        if self.cfg.block_media:
            # 映像を受信しない設定では再生状況を判定できないため、ページの生存のみ確認
            await self.page.evaluate("() => document.readyState")
            self.watched_seconds += self.cfg.health_check_interval
            return

        js = PROBE_JS.replace("MUTE", "true" if self.cfg.mute else "false")
        probe = await self.page.evaluate(js)
        if isinstance(probe, dict) and probe.get("blocked"):
            # 年齢制限など。リロードしても解決しないので、定期リロード時に再確認する
            if not self.blocked:
                self.log.error("このルームは視聴できません (未ログイン・年齢制限など)。ログイン状態を確認してください")
            self.blocked = True
            self._reset_health()
            return
        self.blocked = False
        video_time = probe.get("time") if isinstance(probe, dict) else None
        progressing = (
            video_time is not None
            and self.last_video_time is not None
            and video_time > self.last_video_time
        )
        if progressing:
            self.watched_seconds += video_time - self.last_video_time
            self.stall_count = 0
        elif self.last_video_time is not None or video_time is None:
            self.stall_count += 1
            self.log.debug("再生が進んでいません (%d/%d)", self.stall_count, self.cfg.stall_threshold)
        self.last_video_time = video_time

        if self.stall_count >= self.cfg.stall_threshold:
            await self._reload("再生停止を検出")

    async def _close_page(self) -> None:
        page, self.page = self.page, None
        self._reset_health()
        if page is not None and not page.is_closed():
            try:
                await page.close()
            except Exception as e:  # noqa: BLE001
                self.log.debug("ページのクローズに失敗: %s", e)
