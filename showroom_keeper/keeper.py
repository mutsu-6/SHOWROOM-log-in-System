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

from .api import BASE_URL, ShowroomAPI, summarize_login
from .config import Config, apply_settings, settings_path
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
# ページを開いておくのに必要なメモリの目安 (GB)。計測では 1 ルーム目で約 0.6〜1GB、以降 1 ルームにつき約 0.5GB
BROWSER_BASE_GB = 0.5
ROOM_GB = 0.5


def memory_gb() -> float | None:
    """使えるメモリの総量 (GB)。コンテナの上限があればそちらを使う。分からなければ None。"""
    total = None
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                total = int(line.split()[1]) / 1024 / 1024
                break
    except (OSError, ValueError):
        pass
    for path in ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
        try:
            raw = Path(path).read_text().strip()
            if raw.isdigit():
                limit = int(raw) / 1024 ** 3
                if limit < (total or float("inf")):
                    total = limit
        except OSError:
            continue
    return total


def resolve_keep_open(cfg: Config, mem: float | None = None) -> bool:
    """配信していないルームのページも開いておくかを決める。

    keep_open_when_offline が未指定なら、全ルームを開いてもメモリが足りる時だけ開いておく。
    """
    if cfg.keep_open_when_offline is not None:
        need = BROWSER_BASE_GB + ROOM_GB * len(cfg.rooms)
        mem = memory_gb() if mem is None else mem
        if cfg.keep_open_when_offline and mem is not None and mem < need:
            log.warning("%d ルームのページを常に開いておくには約 %.1fGB のメモリが必要ですが、このサーバーは %.1fGB です。"
                        "重くなって視聴やコメントが止まる場合は keep_open_when_offline: false にしてください",
                        len(cfg.rooms), need, mem)
        return cfg.keep_open_when_offline
    need = BROWSER_BASE_GB + ROOM_GB * len(cfg.rooms)
    mem = memory_gb() if mem is None else mem
    if mem is None or mem >= need:
        return True
    log.info("メモリ (%.1fGB) が %d ルームのページを常に開いておくには足りない (約 %.1fGB 必要) ため、"
             "配信中のルームだけページを開きます (keep_open_when_offline で変更できます)", mem, len(cfg.rooms), need)
    return False


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
        # 端末の Ctrl+C でブラウザが先に終了するとセッションを保存できないため、終了は keeper 側で行う
        handle_sigint=False,
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
    """ログイン状態の確認結果を status コマンド用に保存する (確認の間隔も残し、途絶えたかの判定に使う)。"""
    with suppress(Exception):
        write_live_stat(Path(cfg.state_file).parent, LOGIN_KEY, logged_in=state, checked_at=time.time(),
                        check_interval=login_check_seconds(cfg))


def login_check_seconds(cfg: Config) -> float:
    return max(60.0, float(cfg.login_check_interval))


def login_room_ids(watchers: list[RoomWatcher]) -> list[int]:
    """ログイン確認に使うルーム。ログインはアカウント単位でどのルームで聞いても同じなので 1 つだけ使う。

    配信中のルームがあればそれを優先する (配信していないルームでも確認はできる)。
    """
    known = [w.status for w in watchers if w.status and w.status.room_id is not None]
    known.sort(key=lambda s: not s.is_live)
    return [known[0].room_id] if known else []


async def probe_login(api: ShowroomAPI, room_ids: list[int]) -> bool | None:
    """指定したルームでログイン状態を確認する (アカウントのログインなので通常は 1 ルームで足りる)。"""
    return summarize_login([await api.is_logged_in(room_id) for room_id in room_ids])


async def ensure_login(ctx: BrowserContext, cfg: Config, api: ShowroomAPI, room_ids: list[int]) -> tuple[bool | None, bool]:
    """ログイン中でなければ再ログインする。戻り値は (状態, 今ログインしたか)。"""
    state = await probe_login(api, room_ids)
    record_login(cfg, state)
    if state is True:
        log.info("ログイン済みです")
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
        if not (cfg.rooms or cfg.record_rooms or cfg.events or cfg.web_port):
            raise ValueError("監視するルームが指定されていません (rooms / record_rooms / events)")
        self.cfg = cfg
        # Python 3.9 の asyncio.Event は生成時のイベントループに結び付くため、run() 内で生成する
        self._stop: asyncio.Event | None = None
        # 停止か設定の反映を求められたら立つ。待っている処理はこれで起きて、どちらかを確かめる
        self._wake: asyncio.Event | None = None
        self._stop_requested = False
        self._reload_requested = False
        self._loop: asyncio.AbstractEventLoop | None = None
        # 管理画面が読む現在の状態
        self.watchers: list[RoomWatcher] = []
        self.recorder = None
        self.started_at = time.time()
        self.browser_state = "停止中"

    def stop(self) -> None:
        self._stop_requested = True
        if self._stop is not None:
            self._stop.set()
            self._wake.set()

    def request_reload(self) -> None:
        """保存されたルーム設定を読み直し、ブラウザとギフト記録をやり直す (管理画面のスレッドからも呼べる)。"""
        def apply() -> None:
            self._reload_requested = True
            if self._wake is not None:
                self._wake.set()

        if self._loop is not None and self._loop.is_running():
            self._loop.call_soon_threadsafe(apply)
        else:
            apply()

    def _interrupted(self) -> bool:
        return self._stop.is_set() or self._reload_requested

    async def _sleep_or_wake(self, seconds: float) -> None:
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._wake.wait(), timeout=seconds)

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        if self._stop_requested:
            self.stop()
        if settings_path(self.cfg).exists():
            log.info("管理画面で保存したルーム設定を使います (%s。.env / config.yaml のルーム指定より優先)",
                     settings_path(self.cfg))
        admin = None
        if self.cfg.web_port:
            from .web import start_admin

            admin = start_admin(self)
        try:
            while not self._stop.is_set():
                if self._reload_requested:
                    self._reload_requested = False
                    apply_settings(self.cfg)
                    log.info("ルーム設定を反映しました (視聴 %d / ギフト記録 %d / イベント %d)",
                             len(self.cfg.rooms), len(self.cfg.record_rooms), len(self.cfg.events))
                if not self._stop.is_set():
                    self._wake.clear()
                await self._run_targets()
        finally:
            if admin is not None:
                admin.shutdown()

    async def _run_targets(self) -> None:
        """今の設定で視聴とギフト記録を動かす。停止か設定の反映を求められたら戻る。"""
        recorder = None
        self.recorder = None
        if self.cfg.record_rooms or self.cfg.events:
            from .recorder import GiftRecorder

            self.recorder = GiftRecorder(self.cfg)
            recorder = asyncio.ensure_future(self.recorder.run())
        try:
            if self.cfg.rooms:
                await self._run_loop()
            else:
                # 視聴ルームが無くギフト記録だけの場合はブラウザを起動しない
                if self.cfg.record_rooms or self.cfg.events:
                    log.info("視聴するルームが無いため、ブラウザを開かずギフトの記録だけを行います")
                else:
                    log.info("ルームが設定されていません。管理画面から追加してください")
                self.browser_state = "使用しない"
                await self._wake.wait()
        finally:
            self.watchers = []
            if recorder is not None:
                recorder.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await recorder
            self.recorder = None

    async def _run_loop(self) -> None:
        failures = 0
        async with async_playwright() as pw:
            while not self._interrupted():
                try:
                    self.browser_state = "起動中"
                    await self._run_session(pw)
                    failures = 0
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001
                    failures += 1
                    delay = min(self.cfg.max_backoff, 10 * 2 ** (failures - 1))
                    self.browser_state = f"障害 ({delay:.0f} 秒後に再起動)"
                    log.error("ブラウザで障害が発生しました: %s / %.0f 秒後に再起動します", e, delay)
                    await self._sleep_or_wake(delay)
        self.browser_state = "停止中"

    async def _run_session(self, pw) -> None:
        browser = await launch_browser(pw, self.cfg)
        disconnected = asyncio.Event()
        browser.on("disconnected", lambda _: disconnected.set())
        ctx = await new_context(browser, self.cfg)
        api = ShowroomAPI(ctx.request)
        try:
            room_ids = await self._room_ids(api, required=True)
            keep_open = resolve_keep_open(self.cfg)
            # ログインはアカウント単位なので、確認は 1 ルームで足りる
            await ensure_login(ctx, self.cfg, api, room_ids[:1])

            watchers = [
                RoomWatcher(key, self.cfg, api.room_status, ctx.new_page) for key in self.cfg.rooms
            ]
            for w in watchers:
                w.keep_open = keep_open
            self.watchers = watchers
            self.browser_state = "動作中"
            tasks = [asyncio.create_task(w.run(), name=f"room:{w.key}") for w in watchers]
            tasks.append(asyncio.create_task(CommentDispatcher(queue_dir(self.cfg), watchers).run(), name="comments"))
            saver = asyncio.create_task(self._periodic_save(ctx, api, watchers))
            wake_wait = asyncio.create_task(self._wake.wait())
            disc_wait = asyncio.create_task(disconnected.wait())
            try:
                await asyncio.wait([wake_wait, disc_wait, saver], return_when=asyncio.FIRST_COMPLETED)
            finally:
                for w in watchers:
                    w.stop()
                tasks[-1].cancel()  # コメント受付
                await asyncio.gather(*tasks, return_exceptions=True)
                for t in (saver, wake_wait, disc_wait):
                    t.cancel()
            if disconnected.is_set() and not self._interrupted():
                raise RuntimeError("ブラウザが切断されました")
            if saver.done() and not saver.cancelled() and saver.exception():
                raise saver.exception()
            if not self._interrupted():
                # 想定外の終了。即座に再起動を繰り返さないよう障害として扱う (バックオフさせる)
                reason = wake_wait.exception() if wake_wait.done() and not wake_wait.cancelled() else None
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
        if required and failures == len(self.cfg.rooms):
            raise RuntimeError("すべてのルームの状態を取得できません")
        return ids

    async def _periodic_save(self, ctx: BrowserContext, api: ShowroomAPI, watchers: list[RoomWatcher]) -> None:
        """ログイン状態を定期的に確認し (コメント等は送らず API で 1 回問い合わせるだけ)、セッションを保存する。

        確認 (既定 2 時間ごと) とセッションの保存 (既定 10 分ごと) は、それぞれの間隔で行う。
        """
        save_interval = max(60.0, self.cfg.save_state_minutes * 60)
        check_interval = login_check_seconds(self.cfg)
        next_check = time.monotonic() + check_interval
        next_save = time.monotonic() + save_interval
        last_state: bool | None = True
        while True:
            await asyncio.sleep(max(1.0, min(next_check, next_save) - time.monotonic()))
            if time.monotonic() >= next_check:
                next_check = time.monotonic() + check_interval
                last_state = await self._check_login(ctx, api, watchers, last_state)
            if time.monotonic() >= next_save:
                next_save = time.monotonic() + save_interval
                await save_state(ctx, self.cfg)

    async def _check_login(self, ctx: BrowserContext, api: ShowroomAPI, watchers: list[RoomWatcher],
                           last_state: bool | None) -> bool | None:
        # ルームの room_id はウォッチャーが配信状況の確認で取得済みなので、問い合わせ直さない
        room_ids = login_room_ids(watchers) or (await self._room_ids(api))[:1]
        state = await probe_login(api, room_ids)
        record_login(self.cfg, state)
        if state is False and last_state is not False:
            log.warning("セッションが切れています%s", "。再ログインします" if self.cfg.credentials.available else
                        " (`showroom-keeper login` でログインし直してください)")
        if state is False and self.cfg.credentials.available:
            state, did_login = await ensure_login(ctx, self.cfg, api, room_ids)
            # 確認のやり直しだけでログインしていない時は、配信中のページを再読み込みしない
            if did_login:
                for watcher in watchers:
                    watcher.reload_reason = "再ログイン"
        elif state is True and last_state is False:
            log.info("ログイン状態に戻りました")
        return state


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
