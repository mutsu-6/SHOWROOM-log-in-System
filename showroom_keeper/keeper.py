"""ブラウザを管理し、全ルームのウォッチャーを常駐させるスーパーバイザー。"""

from __future__ import annotations

import asyncio
import logging
import re
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path

from playwright.async_api import Browser, BrowserContext, async_playwright

from .api import BASE_URL, ShowroomAPI, account_logged_in, combine_login, summarize_login
from .directory import collect_platform_rooms
from .config import Config
from .comments import CommentDispatcher, queue_dir
from .status import LOGIN_KEY, write_live_stat
from .watcher import RoomWatcher

log = logging.getLogger("keeper")

# 視聴に不要な広告・アクセス解析。初回読み込みが詰まる原因になるため遮断する
TRACKER_RE = re.compile(
    r"^https?://([^/]*\.)?(facebook\.net|facebook\.com|googletagmanager\.com|google-analytics\.com"
    r"|analytics\.google\.com|doubleclick\.net|googlesyndication\.com|googleadservices\.com"
    r"|a-mpd\.com|criteo\.(com|net)|adnxs\.com|amazon-adsystem\.com)(:\d+)?/"
)
MEDIA_PATTERNS = ("**/*.ts", "**/*.m4s", "**/*.aac")


def running_under_rosetta() -> bool:
    """Apple Silicon の Mac で Intel 版 Python (Rosetta 2 経由) から起動しているか。"""
    if sys.platform != "darwin":
        return False
    try:
        out = subprocess.run(["sysctl", "-n", "sysctl.proc_translated"], capture_output=True, text=True, timeout=5)
        return out.stdout.strip() == "1"
    except Exception:  # noqa: BLE001
        return False


async def launch_browser(pw, cfg: Config) -> Browser:
    if running_under_rosetta():
        log.warning(
            "Intel 版 Python (Rosetta 2) で動いています。Chrome も Intel 版として起動され、非常に遅くなります。"
            "Apple Silicon 版 Python で .venv を作り直してください (README のトラブルシューティング参照)"
        )
    options = dict(
        headless=cfg.headless,
        proxy={"server": cfg.proxy} if cfg.proxy else None,
        args=["--autoplay-policy=no-user-gesture-required"]
        + (["--mute-audio"] if cfg.mute else [])
        + (["--disable-3d-apis"] if (cfg.headless if cfg.disable_3d is None else cfg.disable_3d) else []),
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


async def _user_agent(browser: Browser, cfg: Config) -> str | None:
    """ブラウザ本来の User-Agent を使う。ヘッドレス時のみ "HeadlessChrome" の表記を通常の Chrome に直す。"""
    if not cfg.headless:
        return None
    ctx = await browser.new_context()
    try:
        page = await ctx.new_page()
        ua = await page.evaluate("() => navigator.userAgent")
    finally:
        await ctx.close()
    return ua.replace("HeadlessChrome", "Chrome")


async def new_context(browser: Browser, cfg: Config) -> BrowserContext:
    state = str(cfg.state_file) if Path(cfg.state_file).exists() else None
    if state:
        log.info("保存済みセッションを読み込みます: %s", state)
    ctx = await browser.new_context(
        storage_state=state,
        user_agent=await _user_agent(browser, cfg),
        locale="ja-JP",
        timezone_id="Asia/Tokyo",
        # 画面表示時はウィンドウ全体を使う。ヘッドレス時は一般的な画面サイズに固定
        viewport=None if not cfg.headless else {"width": 1280, "height": 720},
        no_viewport=not cfg.headless,
    )
    if cfg.block_trackers:
        await ctx.route(TRACKER_RE, lambda route: route.abort())
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


def record_login(cfg: Config, state: bool | None) -> None:
    """ログイン状態の確認結果を status コマンド用に保存する。"""
    with suppress(Exception):
        write_live_stat(Path(cfg.state_file).parent, LOGIN_KEY, logged_in=state, checked_at=time.time())


async def login_states(api: ShowroomAPI, room_ids: list[int], concurrency: int = 8) -> list[bool | None]:
    """ルームごとのログイン状態。同時に問い合わせる数だけを抑える。"""
    if not room_ids:
        return []
    sem = asyncio.Semaphore(concurrency)

    async def one(room_id: int) -> bool | None:
        async with sem:
            return await api.is_logged_in(room_id)

    return list(await asyncio.gather(*[one(room_id) for room_id in room_ids]))


async def probe_login(api: ShowroomAPI, room_ids: list[int]) -> bool | None:
    """視聴ルームは 1 件でも未ログインなら未ログインとする。"""
    return summarize_login(await login_states(api, room_ids))


async def ensure_login(ctx: BrowserContext, cfg: Config, api: ShowroomAPI, room_ids: list[int]) -> tuple[bool | None, bool]:
    """全ルームがログイン中でなければ再ログインする。戻り値は (状態, 今ログインしたか)。"""
    state = await probe_login(api, room_ids)
    record_login(cfg, state)
    if state is True:
        log.info("ログイン済みです (確認したルーム %d 件)", len(room_ids))
        return True, False
    if not cfg.credentials.available:
        if state is False:
            log.warning("未ログインです。視聴は匿名で継続します (`showroom-keeper login` でログインできます)")
        return state, False
    log.info("ログインします (account_id=%s)", cfg.credentials.account_id)
    if not await api.login(cfg.credentials.account_id, cfg.credentials.password):
        return False, False
    log.info("ログインに成功しました")
    await save_state(ctx, cfg)
    state = await probe_login(api, room_ids)
    confirmed = True if state is None else state
    record_login(cfg, confirmed)
    return confirmed, True


class Keeper:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._platform_count: int | None = None
        self._platform_unconfirmed = -1
        # Python 3.9 の asyncio.Event は生成時のイベントループに結び付くため、run() 内で生成する
        self._stop: asyncio.Event | None = None
        self._stop_requested = False

    def stop(self) -> None:
        self._stop_requested = True
        if self._stop is not None:
            self._stop.set()

    async def run(self) -> None:
        self._stop = asyncio.Event()
        if self._stop_requested:
            self._stop.set()
        recorder = None
        if self.cfg.record_rooms or self.cfg.events:
            from .recorder import GiftRecorder

            recorder = asyncio.ensure_future(GiftRecorder(self.cfg).run())
        try:
            await self._run_loop()
        finally:
            if recorder is not None:
                recorder.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await recorder

    async def _run_loop(self) -> None:
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
            if self.cfg.rooms:
                room_ids = await self._room_ids(api, required=True)
                # ルーム ID が取れていないときは、未確認のままパスワードログインしない
                if room_ids:
                    await ensure_login(ctx, self.cfg, api, room_ids)

            watchers = [
                RoomWatcher(key, self.cfg, api.room_status, ctx.new_page) for key in self.cfg.rooms
            ]
            tasks = [asyncio.create_task(w.run(), name=f"room:{w.key}") for w in watchers]
            tasks.append(asyncio.create_task(CommentDispatcher(queue_dir(self.cfg), watchers).run(), name="comments"))
            saver = asyncio.create_task(self._periodic_save(ctx, api, watchers))
            stop_wait = asyncio.create_task(self._stop.wait())
            disc_wait = asyncio.create_task(disconnected.wait())
            try:
                await asyncio.wait([stop_wait, disc_wait, saver], return_when=asyncio.FIRST_COMPLETED)
            finally:
                for w in watchers:
                    w.stop()
                tasks[-1].cancel()  # コメント受付
                await asyncio.gather(*tasks, return_exceptions=True)
                for t in (saver, stop_wait, disc_wait):
                    t.cancel()
            if disconnected.is_set() and not self._stop.is_set():
                raise RuntimeError("ブラウザが切断されました")
            if saver.done() and not saver.cancelled() and saver.exception():
                raise saver.exception()
            if not self._stop.is_set():
                # 想定外の終了。即座に再起動を繰り返さないよう障害として扱う (バックオフさせる)
                reason = stop_wait.exception() if stop_wait.done() and not stop_wait.cancelled() else None
                raise RuntimeError(f"監視が予期せず終了しました: {reason!r}")
        finally:
            if browser.is_connected():
                with suppress(Exception):
                    await save_state(ctx, self.cfg)
                with suppress(Exception):
                    await browser.close()

    async def _room_ids(self, api: ShowroomAPI, required: bool = False) -> list[int]:
        """監視中の全ルームの room_id。起動時は 1 件も取れなければセッションをやり直す。"""
        ids: list[int] = []
        failures = 0
        for key in self.cfg.rooms:
            try:
                status = await api.room_status(key)
            except Exception as e:  # noqa: BLE001
                failures += 1
                log.warning("%s の状態を取得できません: %s", key, e)
                continue
            if status.room_id is not None:
                ids.append(status.room_id)
        if required and self.cfg.rooms and failures == len(self.cfg.rooms):
            raise RuntimeError("すべてのルームの状態を取得できません")
        return ids

    async def _login_snapshot(self, api: ShowroomAPI) -> bool | None:
        """視聴ルームと、SHOWROOM が公開している全部屋のログイン状態をまとめる。"""
        watch_ids = await self._room_ids(api) if self.cfg.rooms else []
        try:
            platform_ids = await collect_platform_rooms(api.get_path)
        except Exception as e:  # noqa: BLE001
            log.warning("SHOWROOM 全体のルーム一覧を取得できません: %s", e)
            platform_ids = []
        if len(platform_ids) != self._platform_count:
            log.info("SHOWROOM の公開ルーム %d 件をログイン確認の対象にします", len(platform_ids))
            self._platform_count = len(platform_ids)
        # 視聴ルームと公開ルームが重なる分は 1 回だけ問い合わせる
        ordered = list(dict.fromkeys([*watch_ids, *platform_ids]))
        states = {room_id: state for room_id, state in zip(ordered, await login_states(api, ordered))}
        watch = summarize_login([states[room_id] for room_id in watch_ids]) if watch_ids else None
        platform_states = [states[room_id] for room_id in platform_ids]
        unconfirmed = sum(state is False for state in platform_states)
        if unconfirmed != self._platform_unconfirmed:
            confirmed = sum(state is True for state in platform_states)
            if unconfirmed and confirmed:
                log.warning("ログインを確認できない公開ルームが %d 件あります", unconfirmed)
            self._platform_unconfirmed = unconfirmed
        platform = account_logged_in(platform_states) if platform_ids else None
        return combine_login(watch, platform)

    async def _periodic_save(self, ctx: BrowserContext, api: ShowroomAPI, watchers: list[RoomWatcher]) -> None:
        """ログイン状態を定期的に確認し (コメント等は送らず API で問い合わせるだけ)、セッションを保存する。"""
        save_interval = max(60.0, self.cfg.save_state_minutes * 60)
        check_interval = max(60.0, min(self.cfg.login_check_interval, save_interval))
        last_save = time.monotonic()
        last_state: bool | None = True
        while True:
            try:
                state = await self._login_snapshot(api)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - 一覧取得の失敗で視聴中のブラウザまで落とさない
                log.warning("ログイン確認に失敗しました: %s", e)
            else:
                record_login(self.cfg, state)
                if state is False and last_state is not False:
                    log.warning("セッションが切れています%s", "。再ログインします" if self.cfg.credentials.available else
                                " (`showroom-keeper login` でログインし直してください)")
                if state is False and self.cfg.credentials.available:
                    watch_ids = await self._room_ids(api) if self.cfg.rooms else []
                    state, did_login = await ensure_login(ctx, self.cfg, api, watch_ids)
                    # 確認のやり直しだけでログインしていない時は、配信中のページを再読み込みしない
                    if did_login:
                        for watcher in watchers:
                            watcher.reload_reason = "再ログイン"
                        try:
                            state = await self._login_snapshot(api)
                        except asyncio.CancelledError:
                            raise
                        except Exception as e:  # noqa: BLE001
                            log.warning("再ログイン後の確認に失敗しました: %s", e)
                        else:
                            record_login(self.cfg, state)
                elif state is True and last_state is False:
                    log.info("ログイン状態に戻りました")
                last_state = state
            if time.monotonic() - last_save >= save_interval:
                await save_state(ctx, self.cfg)
                last_save = time.monotonic()
            await asyncio.sleep(check_interval)


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


async def diagnose(cfg: Config, url: str = BASE_URL) -> None:
    """ページの読み込みを計測し、遅い・失敗した通信を表示する。"""
    async with async_playwright() as pw:
        browser = await launch_browser(pw, cfg)
        ctx = await new_context(browser, cfg)
        page = await ctx.new_page()
        started: dict = {}
        done: list[tuple[float, str, str]] = []
        t0 = time.monotonic()
        page.on("request", lambda r: started.__setitem__(r, time.monotonic()))
        page.on("requestfinished", lambda r: done.append((time.monotonic() - started.get(r, t0), "OK", r.url)))
        page.on("requestfailed", lambda r: done.append((time.monotonic() - started.get(r, t0), f"失敗 {r.failure}", r.url)))
        print(f"ブラウザ: {browser.version}")
        print(f"User-Agent: {await page.evaluate('() => navigator.userAgent')}")
        print(f"計測中: {url} (最大 90 秒)", flush=True)
        marks = {}
        try:
            await page.goto(url, wait_until="commit", timeout=90000)
            marks["応答開始"] = time.monotonic() - t0
            print(f"  応答開始: {marks['応答開始']:.1f} 秒", flush=True)
            for state, label in (("domcontentloaded", "HTML 読み込み完了"), ("load", "全体の読み込み完了")):
                await page.wait_for_load_state(state, timeout=max(1000, 90000 - (time.monotonic() - t0) * 1000))
                marks[label] = time.monotonic() - t0
                print(f"  {label}: {marks[label]:.1f} 秒", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"タイムアウト/エラー: {str(e).splitlines()[0]}")
        finished = {u for _, _, u in done}
        pending = [r.url for r in started if r.url not in finished]
        # 意図的に遮断した広告・解析の通信は除く
        slow = sorted((d for d in done if (d[0] >= 2 or d[1] != "OK") and not TRACKER_RE.match(d[2])), reverse=True)[:15]
        print("遅い/失敗した通信 (2 秒以上):" if slow else "2 秒以上かかった通信はありません")
        for sec, status, u in slow:
            print(f"  {sec:5.1f} 秒 {status} {u[:120]}")
        if pending and "全体の読み込み完了" not in marks:
            print("終わらなかった通信:")
            for u in pending[:15]:
                print(f"  {u[:120]}")
        await browser.close()
