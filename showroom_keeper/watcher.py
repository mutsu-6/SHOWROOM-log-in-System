"""1 ルーム分の視聴状態を維持するウォッチャー。"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol

from . import comment_counts
from .api import RoomStatus
from .activity import ActivityTracker, parse_message
from .gifts import record_gift
from .config import Config
from .status import write_live_stat

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


STATUS_RETRY = 30  # 配信状況の確認に失敗した時、確認し直すまでの秒数
COMMENT_FILL_TIMEOUT = 30000  # コメント欄への入力を待つ上限 (ミリ秒)。サーバーが重い時は時間がかかる
COMMENT_ECHO_WAIT = 6.0  # 配信ページが受信するコメント (自分の分も届く) で反映を確かめる待ち時間 (秒)
COMMENT_LOG_WAIT = 15.0  # 届かなかった時にコメント履歴で確かめる上限 (秒)。履歴 API は 7〜10 秒ほど遅れて載る
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


# SHOWROOM のサロン形式で使われている在室確認 API。応答の viewer_count がリアルタイム視聴者数 (要ログイン)
VIEWER_COUNT_JS = """
async ([liveId, roomId]) => {
  // 在室確認はサロン形式 (broadcast_style=2) の配信でのみ公式に使われている。
  // クラシック形式では誰も送らないため人数が意味を持たない (自分だけの 1 人になる)。送らずに返す
  const profile = await (await fetch('/api/room/profile?room_id=' + roomId, { credentials: 'include' })).json();
  if (Number(profile.broadcast_style) !== 2) return { style: 'classic' };
  const t = await (await fetch('/api/csrf_token', { credentials: 'include' })).json();
  const r = await fetch('/api/viewer_presence/heartbeat', {
    method: 'POST', credentials: 'include',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({ live_id: String(liveId), csrf_token: t.csrf_token || '' }),
  });
  let data = null;
  try { data = await r.json(); } catch (e) {}
  return { status: r.status, data };
}
"""


class Page(Protocol):  # playwright.async_api.Page と互換
    async def goto(self, url: str, **kwargs: Any) -> Any: ...
    async def fill(self, selector: str, value: str, **kwargs: Any) -> None: ...
    async def press(self, selector: str, key: str, **kwargs: Any) -> None: ...
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
        self._state_dir = Path(config.state_file).parent

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
        self.viewer_count: int | None = None
        self.activity = ActivityTracker()
        self._activity_live_id: int | None = None
        self._viewer_count_at: float | None = None
        self._viewer_count_error: str | None = None
        self._failures = 0
        self._status_failures = 0
        # 投稿したコメントの反映待ち (本文, 待っている Future)。配信ページが受信するコメントで解決する
        self._comment_waiters: list[tuple[str, asyncio.Future]] = []
        self._my_user_id: int | None = None
        self.comment_echo_wait = COMMENT_ECHO_WAIT
        self._became_live = False
        self._went_offline = False
        self.reload_reason: str | None = None
        # 配信していない時もページを開いておくか。keeper がメモリを見て決める (None なら設定どおり)
        self.keep_open: bool | None = None
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
                try:
                    await self._refresh_status()
                    self._status_failures = 0
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001
                    if self.status is None:
                        raise
                    # 配信状況が分からないだけで、開いているページは正常なので閉じない (重い時に開き直しが重なると悪化する)。
                    # 前回の状況のまま続け、少し待ってから確認し直す
                    self._status_failures += 1
                    retry = min(self.cfg.status_poll_interval, STATUS_RETRY)
                    self.last_status_at = now - self.cfg.status_poll_interval + retry
                    self.log.warning("配信状況を確認できません (%d 回目): %s / ページはそのまま続け、%.0f 秒後に確認し直します",
                                     self._status_failures, str(e).splitlines()[0], retry)

            keep_open = self.keep_open if self.keep_open is not None else self.cfg.keep_open_when_offline is not False
            should_open = bool(self.status and (self.status.is_live or keep_open))
            if should_open:
                became_live = self._became_live
                went_offline = self._went_offline
                self._became_live = False
                self._went_offline = False
                reason = self.reload_reason
                self.reload_reason = None
                if not self._page_alive():
                    await self._open_page()
                elif became_live:
                    self._sync_activity_live()
                    await self._reload("配信開始")
                elif reason:
                    await self._reload(reason)
                elif went_offline:
                    # 終わった配信の再生を止め、ルームのページにはログインしたまま居る
                    await self._reload("配信終了")
                else:
                    await self._maintain_page()
            elif self.page is not None:
                self.log.info("配信が終了しました。次の配信開始を待ちます")
                await self._close_page()

            self._failures = 0
            if self._page_alive() and self.status and self.status.is_live:
                return self.cfg.health_check_interval
            return self.cfg.status_poll_interval
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - どんな障害でも監視は継続する
            self._failures += 1
            delay = min(self.cfg.max_backoff, 5 * 2 ** (self._failures - 1))
            self.log.warning("エラーが発生しました (%d 回目): %s / %.0f 秒後に再試行", self._failures, e, delay)
            await self._close_page()
            return delay

    @property
    def can_comment(self) -> bool:
        return self._page_alive() and bool(self.status and self.status.is_live) and not self.blocked

    async def post_comment(self, text: str) -> tuple[bool, str]:
        """開いている配信ページのコメント欄から投稿し、コメント履歴で反映を確認する。"""
        if not self._page_alive() or not (self.status and self.status.is_live):
            return False, "配信中のページが開いていません"
        if self.blocked:
            return False, "このルームは視聴できない状態です"
        page = self.page
        assert page is not None
        since = int(time.time()) - 5
        started = time.monotonic()
        await self._ensure_my_user_id(page)  # 通常は視聴中に取得済み
        t_uid = time.monotonic()
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        waiter = (text, fut)
        self._comment_waiters.append(waiter)
        try:
            try:
                await page.fill(COMMENT_INPUT, text, timeout=COMMENT_FILL_TIMEOUT)
                t_fill = time.monotonic()
                # ボタンのクリックはお知らせ等の重なりで阻まれることがあるため、入力欄で Enter を押して送信する
                await page.press(COMMENT_INPUT, "Enter", timeout=COMMENT_FILL_TIMEOUT)
            except Exception as e:  # noqa: BLE001
                if "Timeout" not in type(e).__name__ + str(e):
                    raise
                return False, ("配信ページの反応が遅く、コメント欄に入力できませんでした。サーバーのメモリや CPU が足りていない可能性があります "
                               "(見張るルームを減らすか、keep_open_when_offline: false にしてください)")
            sent = time.monotonic()
            steps = f"ID 確認 {t_uid - started:.1f} 秒 / 入力 {t_fill - t_uid:.1f} 秒 / 送信 {sent - t_fill:.1f} 秒"
            # 配信ページが受信するコメントで確かめる (ほぼすぐ届く)
            try:
                await asyncio.wait_for(asyncio.shield(fut), timeout=self.comment_echo_wait)
                self.log.info("コメントの反映を確認しました (%s / 反映 %.1f 秒)", steps, time.monotonic() - sent)
                return True, "投稿しました"
            except asyncio.TimeoutError:
                pass
            # 届かなければコメント履歴で確かめる (履歴は 7〜10 秒ほど遅れて載る)
            for _ in range(int(COMMENT_LOG_WAIT // 2)):
                if fut.done() or await page.evaluate(COMMENT_LOG_JS, [self.status.room_id, text, since]):
                    self.log.info("コメントの反映を確認しました (%s / 反映 %.1f 秒、履歴で確認)", steps, time.monotonic() - sent)
                    return True, "投稿しました"
                await self._sleep(2)
        finally:
            if waiter in self._comment_waiters:
                self._comment_waiters.remove(waiter)
        return False, "投稿を確認できませんでした (NGワード、未ログイン、コメント制限、または同じ内容の連投の可能性)"

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
        self._became_live = bool(prev is not None and not prev.is_live and self.status.is_live)
        self._went_offline = bool(prev is not None and prev.is_live and not self.status.is_live)
        if prev is None or (prev.room_name, prev.room_id) != (self.status.room_name, self.status.room_id):
            self._remember_name()
        if prev is None or prev.is_live != self.status.is_live:
            state = "配信中" if self.status.is_live else "配信していません"
            self.log.info("%s: %s", self.status.room_name, state)

    def _remember_name(self) -> None:
        """管理画面の一覧に名前で出せるよう、ルーム名を覚えておく。"""
        from .listing import remember_rooms

        try:
            remember_rooms(self._state_dir, {self.key: (self.status.room_name, self.status.room_id)})
        except Exception as e:  # noqa: BLE001 - 名前を覚えられなくても視聴は続ける
            self.log.debug("ルーム名を保存できません: %s", e)

    def _page_alive(self) -> bool:
        return self.page is not None and not self.page.is_closed()

    async def _open_page(self) -> None:
        await self._close_page()
        assert self.status is not None
        self.log.info("視聴ページを開きます: %s", self.status.url)
        page = await self._new_page()
        self.page = page
        if hasattr(page, "on"):
            page.on("websocket", self._on_websocket)
        self._sync_activity_live()
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

    def _sync_activity_live(self) -> None:
        """配信が変わったら推定アクティブ人数を最初から数え直す。"""
        live_id = self.status.live_id if self.status else None
        if live_id != self._activity_live_id:
            self._activity_live_id = live_id
            self.activity.reset(time.time())

    def _reset_health(self) -> None:
        self.last_video_time = None
        self.stall_count = 0

    def _on_websocket(self, ws: Any) -> None:
        # ページが受信するコメント・ギフト・入室のお知らせを読む (受信を読むだけで送信はしない)
        ws.on("framereceived", self._on_frame)

    def _on_frame(self, payload: Any) -> None:
        now = time.time()
        self.activity.feed(payload, now)
        data = parse_message(payload)
        if data and data.get("t") == 1 and self._comment_waiters:  # コメント (自分の投稿も同じように届く)
            self._resolve_comment(data)
        if data and data.get("t") == 1 and self.cfg.record_gifts and self.status:  # コメント (数だけ数える)
            try:
                comment_counts.counter(self._state_dir).feed(self.key, self.status.live_id, data)
            except Exception as e:  # noqa: BLE001
                self.log.debug("コメントの数の記録に失敗: %s", e)
        if data and data.get("t") == 2 and self.cfg.record_gifts:  # ギフト
            try:
                record_gift(self._state_dir, self.key, self.status.live_id if self.status else None, data)
            except Exception as e:  # noqa: BLE001
                self.log.debug("ギフトの記録に失敗: %s", e)

    def _resolve_comment(self, data: dict[str, Any]) -> None:
        text = str(data.get("cm") or "")
        try:
            sender = int(data.get("u"))
        except (TypeError, ValueError):
            sender = None
        for want, fut in list(self._comment_waiters):
            if want == text and (self._my_user_id is None or sender == self._my_user_id) and not fut.done():
                fut.set_result(True)

    async def _ensure_my_user_id(self, page: Page) -> None:
        if self._my_user_id is not None or not self.status:
            return
        try:
            me = await page.evaluate(
                "async (roomId) => (await fetch('/api/live/current_user?room_id=' + roomId, { credentials: 'include' })).json()",
                self.status.room_id)
            uid = me.get("user_id") if isinstance(me, dict) else None
            self._my_user_id = int(uid) if uid else None
        except Exception as e:  # noqa: BLE001 - 分からなければ本文だけで確かめる
            self.log.debug("ユーザー ID を取得できません: %s", e)

    def _save_activity(self) -> None:
        now = time.time()
        write_live_stat(self._state_dir, self.key, active_users=self.activity.count(now), active_at=now,
                        active_observed=self.activity.observed_seconds(now))

    async def _update_viewer_count(self) -> None:
        """リアルタイム視聴者数を取得し、status コマンド用に保存する。"""
        if not self.cfg.viewer_count or not self._page_alive() or not (self.status and self.status.live_id):
            return
        now = self._clock()
        if self._viewer_count_at is not None and now - self._viewer_count_at < self.cfg.viewer_count_interval:
            return
        self._viewer_count_at = now
        assert self.page is not None
        res = await self.page.evaluate(VIEWER_COUNT_JS, [self.status.live_id, self.status.room_id])
        if isinstance(res, dict) and res.get("style") == "classic":
            if self._viewer_count_error != "classic":
                self.log.info("クラシック形式の配信のため、リアルタイム視聴者数は公開されていません (推定アクティブ人数を表示します)")
            self.viewer_count, self._viewer_count_error = None, "classic"
            write_live_stat(self._state_dir, self.key, viewer_count=None, viewer_count_error=None, broadcast_style="classic")
            return
        data = res.get("data") if isinstance(res, dict) else None
        count = data.get("viewer_count") if isinstance(data, dict) else None
        if isinstance(count, int):
            if self.viewer_count is None:
                self.log.info("リアルタイム視聴者数を取得できました: %d 人", count)
            self.viewer_count, self._viewer_count_error = count, None
            write_live_stat(self._state_dir, self.key, viewer_count=count, viewer_count_at=time.time(),
                            viewer_count_error=None, broadcast_style="salon")
        else:
            msg = (data or {}).get("error_user_msg") or (data or {}).get("error") or f"HTTP {res.get('status') if isinstance(res, dict) else '?'}"
            if msg != self._viewer_count_error:
                self.log.warning("リアルタイム視聴者数を取得できません: %s", msg)
            self._viewer_count_error = msg
            write_live_stat(self._state_dir, self.key, viewer_count=None, viewer_count_error=msg)

    async def _maintain_page(self) -> None:
        assert self.page is not None
        now = self._clock()
        period = self.cfg.periodic_reload_minutes * 60
        if period > 0 and self.last_reload_at is not None and now - self.last_reload_at >= period:
            await self._reload("定期リロード")
            return
        if not (self.status and self.status.is_live):
            # 配信前でもページは開いたままにし、ログイン済みのルームに居続ける
            await self.page.evaluate("() => document.readyState")
            return
        try:
            self._save_activity()
            await self._ensure_my_user_id(self.page)  # コメント投稿時に問い合わせなくて済むよう先に取っておく
            await self._update_viewer_count()
        except Exception as e:  # noqa: BLE001 - 視聴者数の取得失敗で視聴を止めない
            self.log.debug("視聴者数の取得に失敗: %s", e)
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
            state = (
                f"再生位置 {probe.get('time', 0):.0f} 秒, paused={probe.get('paused')}, readyState={probe.get('ready')}"
                if isinstance(probe, dict) else "動画要素なし"
            )
            await self._reload(f"再生停止を検出: {state}, 累計視聴 {self.watched_seconds / 60:.1f} 分")

    async def _close_page(self) -> None:
        page, self.page = self.page, None
        self._reset_health()
        if page is not None and not page.is_closed():
            try:
                await page.close()
            except Exception as e:  # noqa: BLE001
                self.log.debug("ページのクローズに失敗: %s", e)
