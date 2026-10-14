"""ブラウザを管理し、全ルームのウォッチャーを常駐させるスーパーバイザー。"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from pathlib import Path

from playwright.async_api import Browser, BrowserContext, async_playwright

from .api import BASE_URL, ShowroomAPI
from .config import Config
from .watcher import RoomWatcher

log = logging.getLogger("keeper")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)
MEDIA_PATTERNS = ("**/*.ts", "**/*.m4s", "**/*.aac")


async def launch_browser(pw, cfg: Config) -> Browser:
    options = dict(
        headless=cfg.headless,
        proxy={"server": cfg.proxy} if cfg.proxy else None,
        args=["--autoplay-policy=no-user-gesture-required"] + (["--mute-audio"] if cfg.mute else []),
    )
    if cfg.proxy:
        log.info("プロキシを使用します: %s", cfg.proxy)
    if cfg.browser_executable:
        return await pw.chromium.launch(executable_path=cfg.browser_executable, **options)
    if cfg.browser_channel:
        try:
            browser = await pw.chromium.launch(channel=cfg.browser_channel, **options)
            log.info("ブラウザを起動しました: %s %s", cfg.browser_channel, browser.version)
            return browser
        except Exception as e:  # noqa: BLE001
            log.warning(
                "ブラウザ %r を起動できませんでした (%s)。付属の Chromium を使いますが、"
                "H.264 非対応のため映像を再生できない可能性があります。Google Chrome のインストールを推奨します",
                cfg.browser_channel,
                str(e).splitlines()[0],
            )
    return await pw.chromium.launch(**options)


async def new_context(browser: Browser, cfg: Config) -> BrowserContext:
    state = str(cfg.state_file) if Path(cfg.state_file).exists() else None
    if state:
        log.info("保存済みセッションを読み込みます: %s", state)
    ctx = await browser.new_context(
        storage_state=state,
        user_agent=USER_AGENT,
        locale="ja-JP",
        timezone_id="Asia/Tokyo",
        viewport={"width": 1280, "height": 720},
    )
    if cfg.block_media:
        for pattern in MEDIA_PATTERNS:
            await ctx.route(pattern, lambda route: route.abort())
    return ctx


async def save_state(ctx: BrowserContext, cfg: Config) -> None:
    path = Path(cfg.state_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    await ctx.storage_state(path=str(path))
    with suppress(OSError):
        path.chmod(0o600)
    log.debug("セッションを保存しました: %s", path)


async def ensure_login(ctx: BrowserContext, cfg: Config, room_id: int | None) -> None:
    api = ShowroomAPI(ctx.request)
    state = await api.is_logged_in(room_id)
    if state is True:
        log.info("ログイン済みです")
        return
    if not cfg.credentials.available:
        if state is False:
            log.warning("未ログインです。視聴は匿名で継続します (`showroom-keeper login` でログインできます)")
        return
    log.info("ログインします (account_id=%s)", cfg.credentials.account_id)
    if await api.login(cfg.credentials.account_id, cfg.credentials.password):
        log.info("ログインに成功しました")
        await save_state(ctx, cfg)


class Keeper:
    def __init__(self, cfg: Config):
        if not cfg.rooms:
            raise ValueError("監視するルームが指定されていません (rooms / SHOWROOM_ROOMS)")
        self.cfg = cfg
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        failures = 0
        async with async_playwright() as pw:
            while not self._stop.is_set():
                try:
                    await self._run_session(pw)
                    failures = 0
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001
                    failures += 1
                    delay = min(self.cfg.max_backoff, 10 * 2 ** (failures - 1))
                    log.error("ブラウザで障害が発生しました: %s / %.0f 秒後に再起動します", e, delay)
                    with suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(self._stop.wait(), timeout=delay)

    async def _run_session(self, pw) -> None:
        browser = await launch_browser(pw, self.cfg)
        disconnected = asyncio.Event()
        browser.on("disconnected", lambda _: disconnected.set())
        ctx = await new_context(browser, self.cfg)
        api = ShowroomAPI(ctx.request)
        try:
            first = await api.room_status(self.cfg.rooms[0])
            await ensure_login(ctx, self.cfg, first.room_id)

            watchers = [
                RoomWatcher(key, self.cfg, api.room_status, ctx.new_page) for key in self.cfg.rooms
            ]
            tasks = [asyncio.create_task(w.run(), name=f"room:{w.key}") for w in watchers]
            saver = asyncio.create_task(self._periodic_save(ctx, api, first.room_id))
            stop_wait = asyncio.create_task(self._stop.wait())
            disc_wait = asyncio.create_task(disconnected.wait())
            try:
                await asyncio.wait([stop_wait, disc_wait, saver], return_when=asyncio.FIRST_COMPLETED)
            finally:
                for w in watchers:
                    w.stop()
                await asyncio.gather(*tasks, return_exceptions=True)
                for t in (saver, stop_wait, disc_wait):
                    t.cancel()
            if disconnected.is_set() and not self._stop.is_set():
                raise RuntimeError("ブラウザが切断されました")
            if saver.done() and not saver.cancelled() and saver.exception():
                raise saver.exception()
        finally:
            if browser.is_connected():
                with suppress(Exception):
                    await save_state(ctx, self.cfg)
                with suppress(Exception):
                    await browser.close()

    async def _periodic_save(self, ctx: BrowserContext, api: ShowroomAPI, room_id: int | None) -> None:
        interval = max(60.0, self.cfg.save_state_minutes * 60)
        while True:
            await asyncio.sleep(interval)
            # セッション切れなら再ログインを試みる
            if await api.is_logged_in(room_id) is False and self.cfg.credentials.available:
                log.warning("セッションが切れています。再ログインします")
                await ensure_login(ctx, self.cfg, room_id)
            await save_state(ctx, self.cfg)


async def interactive_login(cfg: Config) -> None:
    """ブラウザを表示し、ユーザー自身にログインしてもらってセッションを保存する。"""
    async with async_playwright() as pw:
        cfg.headless = False
        browser = await launch_browser(pw, cfg)
        ctx = await new_context(browser, cfg)
        page = await ctx.new_page()
        print("開いたブラウザで SHOWROOM にログインしてください。")
        print("ログインが完了したら、このターミナルで Enter キーを押してください。")
        try:
            # 読み込みが遅くてもログイン操作はできるので、タイムアウトしても中断しない
            await page.goto(BASE_URL, wait_until="commit", timeout=60000)
        except Exception as e:  # noqa: BLE001
            print(f"(ページの読み込みに時間がかかっています: {str(e).splitlines()[0]})")
            print("ブラウザに SHOWROOM が表示されない場合は、アドレス欄に www.showroom-live.com を入力してください。")
        await asyncio.get_running_loop().run_in_executor(None, input)
        await save_state(ctx, cfg)
        print(f"セッションを保存しました: {cfg.state_file}")
        state = await ShowroomAPI(ctx.request).is_logged_in(await _any_room_id(ctx))
        if state is True:
            print("ログイン状態を確認できました。")
        elif state is False:
            print("まだログインしていないようです。もう一度 login を実行してください。")
        await browser.close()


async def _any_room_id(ctx: BrowserContext) -> int | None:
    """ログイン状態の確認に使うため、配信中のルームを 1 つ探す。"""
    try:
        resp = await ctx.request.get(f"{BASE_URL}/api/live/onlives", timeout=15000)
        for genre in (await resp.json()).get("onlives", []):
            for live in genre.get("lives", []):
                if live.get("room_id"):
                    return int(live["room_id"])
    except Exception:  # noqa: BLE001
        pass
    return None
