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
from typing import Any

from playwright.async_api import Browser, BrowserContext, async_playwright

from .api import BASE_URL, ShowroomAPI
from .config import Config, apply_settings, settings_path
from .comments import CommentDispatcher, queue_dir
from .entrance import AllRoomsEntrance, RoomEntrance
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
# 画質の名前と、SHOWROOM の配信 URL 一覧 (/api/live/streaming_url) の quality の値
VIDEO_QUALITY = {"low": 100, "medium": 200, "original": 1000}
STREAMING_URL_PATTERN = "**/api/live/streaming_url*"


def narrow_streaming_urls(data: Any, quality: int) -> Any:
    """配信ページが受け取る画質の一覧を、指定の画質のものだけにする (その画質が無ければそのまま)。

    再生プレーヤーは一覧から再生する URL を選ぶため、一覧を絞ると指定の画質で再生される
    (再生プレーヤーの画質の切り替えで選ぶのと同じ映像)。
    """
    if not isinstance(data, dict) or not isinstance(data.get("streaming_url_list"), list):
        return data
    keep = [e for e in data["streaming_url_list"] if isinstance(e, dict) and e.get("quality") == quality]
    if not keep:
        return data
    return {**data, "streaming_url_list": [dict(e, is_default=i == 0) for i, e in enumerate(keep)]}


async def _limit_quality(ctx: BrowserContext, quality: int) -> None:
    async def handle(route: Any) -> None:
        try:
            resp = await route.fetch()
        except Exception:  # noqa: BLE001 - 取れなければページ側の通常の処理に任せる
            await route.continue_()
            return
        try:
            await route.fulfill(response=resp, json=narrow_streaming_urls(await resp.json(), quality))
        except Exception:  # noqa: BLE001 - 形式が違う時は手を加えずに渡す
            await route.fulfill(response=resp)

    await ctx.route(STREAMING_URL_PATTERN, handle)
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
    pages = len(cfg.rooms)
    if cfg.keep_open_when_offline is not None:
        need = BROWSER_BASE_GB + ROOM_GB * pages
        mem = memory_gb() if mem is None else mem
        if cfg.keep_open_when_offline and mem is not None and mem < need:
            log.warning("%d ルームのページを常に開いておくには約 %.1fGB のメモリが必要ですが、このサーバーは %.1fGB です。"
                        "重くなって視聴やコメントが止まる場合は keep_open_when_offline: false にしてください",
                        len(cfg.rooms), need, mem)
        return cfg.keep_open_when_offline
    need = BROWSER_BASE_GB + ROOM_GB * pages
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
    state = _readable_state(Path(cfg.state_file))
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
    quality = VIDEO_QUALITY.get(str(cfg.video_quality).lower())
    if quality:
        await _limit_quality(ctx, quality)
    elif str(cfg.video_quality).lower() != "auto":
        log.warning("video_quality %r は使えません (low / medium / original / auto)。SHOWROOM の再生プレーヤーに任せます", cfg.video_quality)
    return ctx


def page_factory(ctx: BrowserContext, cfg: Config) -> Any:
    """配信ページを開く関数。background_tabs なら、開いたページを空白のページの裏に回す。

    ヘッドレスのブラウザでも、前面に無いタブは画面の描き直しが減り CPU が下がる
    (映像の再生・ページのスクリプト・コメントの受信と投稿はそのまま動く)。
    """
    cover: Any = None

    async def new_page() -> Any:
        nonlocal cover
        page = await ctx.new_page()
        if cfg.background_tabs:
            try:
                if cover is None or cover.is_closed():
                    cover = await ctx.new_page()
                await cover.bring_to_front()
            except Exception as e:  # noqa: BLE001 - 裏に回せなくても視聴はできる
                log.debug("空白のページを前面にできませんでした: %s", e)
        return page

    return new_page


def _readable_state(path: Path) -> str | None:
    """読めるログイン状態のファイル。壊れていれば (前の版の書き込みが途中で止まったなど)、前の版 (.bak) を使う。"""
    import json

    for p in (path, path.with_name(path.name + ".bak")):
        try:
            if isinstance(json.loads(p.read_text(encoding="utf-8")), dict):
                if p != path:
                    log.warning("ログイン状態のファイルが壊れていたので、前の版 (%s) を使います", p.name)
                return str(p)
        except (OSError, ValueError):
            continue
    return None


async def save_state(ctx: BrowserContext, cfg: Config) -> None:
    """ログイン状態を保存する。ディスクが一杯などで書き込みが途中で止まっても、前のログイン状態が残るように、
    別の名前に書いて中身を確かめてから置き換える (前の版は .bak に残す)。"""
    import json
    import os
    import shutil

    path = Path(cfg.state_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        await ctx.storage_state(path=str(tmp))
        data = json.loads(tmp.read_text(encoding="utf-8"))  # 途中で切れていれば、ここで失敗する
        if not isinstance(data, dict) or "cookies" not in data:
            raise ValueError("ログイン状態の形が正しくありません")
        with suppress(OSError):
            tmp.chmod(0o600)
        if path.exists():
            with suppress(OSError):
                shutil.copy2(path, path.with_name(path.name + ".bak"))
        os.replace(tmp, path)
    except (OSError, ValueError) as e:
        with suppress(OSError):
            tmp.unlink()
        log.warning("ログイン状態を保存できませんでした (前の状態のまま使います): %s", e)
        return
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


async def probe_login(api: ShowroomAPI, room_ids: list[int], tries: int = 3) -> bool | None:
    """ログイン状態を確認する。

    ログインはアカウント単位で、どのルームで確かめても結果は同じなので、判定できた最初の 1 ルームで決める。
    問い合わせが失敗したときだけ次のルームを試す。
    """
    for room_id in room_ids[:tries]:
        state = await api.is_logged_in(room_id)
        if state is not None:
            return state
    return None


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
        if not (cfg.rooms or cfg.enter_rooms or cfg.enter_all or cfg.record_rooms or cfg.events or cfg.web_port):
            raise ValueError("監視するルームが指定されていません (rooms / record_rooms / events)")
        self.cfg = cfg
        # 視聴ルームが無いときにログイン確認に使う配信中のルーム。確認できなくなったら探し直す
        self._probe_room: int | None = None
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
        gift_updates = asyncio.ensure_future(self._gift_updates())
        research = asyncio.ensure_future(self._research_updates())
        backups = asyncio.ensure_future(self._backup_updates())
        try:
            while not self._stop.is_set():
                if self._reload_requested:
                    self._reload_requested = False
                    apply_settings(self.cfg)
                    log.info("ルーム設定を反映しました (視聴 %d / 入室 %d / ギフト記録 %d / イベント %d)",
                             len(self.cfg.rooms), len(self.cfg.enter_rooms), len(self.cfg.record_rooms), len(self.cfg.events))
                if not self._stop.is_set():
                    self._wake.clear()
                await self._run_targets()
        finally:
            gift_updates.cancel()
            research.cancel()
            backups.cancel()
            if admin is not None:
                admin.shutdown()

    async def _gift_updates(self) -> None:
        """ギフトの一覧 (名前・ポイント) を、起動時と予定の時刻に SHOWROOM から取り直す。"""
        from . import listing
        from .schedule import describe, next_time, parse_slots

        try:
            slots = parse_slots(self.cfg.gift_update_days, self.cfg.gift_update_times)
        except ValueError as e:
            slots = listing.GIFT_UPDATE_SLOTS
            log.warning("ギフトの一覧を更新する予定が正しくありません (%s)。%s に更新します", e, describe(slots))
        listing.GIFT_UPDATE_SLOTS = slots
        log.info("ギフトの一覧を起動時と %s (日本時間) に SHOWROOM から取り直します", describe(slots))
        state_dir = Path(self.cfg.state_file).parent
        loop = asyncio.get_running_loop()
        while True:
            try:
                r = await loop.run_in_executor(None, listing.refresh_gift_catalog, state_dir)
                if r["ok"] and r.get("first"):
                    log.info("ギフトの一覧を取得しました: %d 種類", r["count"])
                elif r["ok"]:
                    added = "、".join(r["added"][:5]) + (" ほか" if len(r["added"]) > 5 else "")
                    log.info("ギフトの一覧を更新しました: %d 種類 (新しいギフト %d%s / ポイントが変わったギフト %d)",
                             r["count"], len(r["added"]), f": {added}" if added else "", len(r["changed"]))
                else:
                    log.warning("ギフトの一覧を取得できませんでした。前の一覧を使います (%d 種類)", r["count"])
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - 次の予定でもう一度試す
                log.warning("ギフトの一覧の更新に失敗: %s", e)
            target = next_time(time.time(), slots)
            # 長く眠る間に時計がずれても予定を過ぎないよう、10 分ごとに残り時間を測り直す
            while time.time() < target:
                await asyncio.sleep(min(600.0, max(1.0, target - time.time())))

    async def _research_updates(self) -> None:
        """応援ルーム・調べるルームの情報を、予定の時刻 (research_days / research_times) に集める。

        起動した時は、まだ集めていないルームや古くなったルームがある時だけ集める。
        """
        from . import research
        from .schedule import describe, next_time, parse_slots

        try:
            slots = parse_slots(self.cfg.research_days, self.cfg.research_times)
        except ValueError as e:
            slots = parse_slots("毎日", "4:30")
            log.warning("情報を集める予定が正しくありません (%s)。%s に集めます", e, describe(slots))
        state_dir = Path(self.cfg.state_file).parent
        research.THROTTLE.set_base(self.cfg.research_gap)
        if self.cfg.research_all:
            from . import sweep

            sweep.start(state_dir, scan=self.cfg.research_scan)
            if self.cfg.research_archive:
                from . import archive

                archive.start(state_dir, self.cfg.research_archive_gap)
        loop = asyncio.get_running_loop()
        first = True
        while True:
            keys = research.research_keys(self.cfg)  # 管理画面で変えたルームも次の回から使う
            if first and keys:
                log.info("応援ルーム・調べるルームの情報を %s (日本時間) に集めます", describe(slots))
            todo, reason = keys, "予定の時刻"
            if first:
                rows = research.status(state_dir, keys)
                todo = [r["key"] for r in rows if not r["at"] or time.time() - r["at"] > research.ROOM_REFRESH]
                reason = "起動時"
            first = False
            if todo:
                try:
                    await loop.run_in_executor(None, research.JOB.run, state_dir, todo, reason)
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001 - 次の予定でもう一度試す
                    log.warning("情報の収集に失敗: %s", e)
            target = next_time(time.time(), slots)
            while time.time() < target:
                await asyncio.sleep(min(600.0, max(1.0, target - time.time())))

    async def _backup_updates(self) -> None:
        """research.db のバックアップを毎日決まった時刻 (research_backup_times) に作る。

        起動した時は、前回のバックアップから 1 日以上たっていれば作る (research.db がある時だけ)。
        """
        from . import backup
        from .schedule import next_time, parse_slots

        try:
            slots = parse_slots("毎日", self.cfg.research_backup_times)
        except ValueError as e:
            slots = parse_slots("毎日", "4:00")
            log.warning("バックアップの時刻が正しくありません (%s)。毎日 4:00 に作ります", e)
        state_dir = Path(self.cfg.state_file).parent
        loop = asyncio.get_running_loop()
        last = backup.last_backup(state_dir) or {}
        due = not last.get("ok") or time.time() - float(last.get("at") or 0) > 86400
        while True:
            if due:
                try:
                    made = await loop.run_in_executor(None, backup.backup_research_db, state_dir, self.cfg.research_backup_keep)
                    if made and made.get("ok") and self.cfg.research_backup_gcs:
                        await loop.run_in_executor(None, backup.upload_gcs, state_dir, self.cfg.research_backup_gcs)
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001 - 次の日にもう一度試す
                    log.warning("バックアップに失敗: %s", e)
            due = True
            target = next_time(time.time(), slots)
            while time.time() < target:
                await asyncio.sleep(min(600.0, max(1.0, target - time.time())))

    async def _run_targets(self) -> None:
        """今の設定で視聴とギフト記録を動かす。停止か設定の反映を求められたら戻る。"""
        recorder = None
        self.recorder = None
        if self.cfg.record_rooms or self.cfg.events or self.cfg.record_glitter:
            from .recorder import GiftRecorder

            self.recorder = GiftRecorder(self.cfg)
            recorder = asyncio.ensure_future(self.recorder.run())
        try:
            if self.cfg.rooms or self.cfg.enter_rooms or self.cfg.enter_all:
                await self._run_loop()
            else:
                # 視聴・入室するルームが無くギフト記録だけの場合は、メモリを使うブラウザを起動しない
                if self.cfg.record_rooms or self.cfg.events or self.cfg.record_glitter:
                    log.info("視聴・入室するルームが無いため、ブラウザを開かずギフトの記録だけを行います")
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
            # ログインはアカウント単位なので、確認は 1 ルームで足りる
            room_ids = await self._login_rooms(api, (await self._room_ids(api, required=True))[:1])
            keep_open = resolve_keep_open(self.cfg)
            if room_ids:
                await ensure_login(ctx, self.cfg, api, room_ids)
            else:
                # 確認できないままパスワードでログインし直すと、有効なセッションでも毎回ログインしてしまう
                log.warning("ログイン状態を確認できるルームが見つかりません。次の確認で再度試します")

            new_page = page_factory(ctx, self.cfg)  # 空白のページはルーム全体で 1 つ
            watchers = [RoomWatcher(key, self.cfg, api.room_status, new_page) for key in self.cfg.rooms]
            for w in watchers:
                w.keep_open = keep_open
            self.watchers = watchers
            enter_rooms = [key for key in self.cfg.enter_rooms if key not in self.cfg.rooms]
            self.browser_state = "動作中"
            tasks = [asyncio.create_task(w.run(), name=f"room:{w.key}") for w in watchers]
            # 止める指示を待たず動き続けるので、終了時に取り消す
            background = [asyncio.create_task(CommentDispatcher(queue_dir(self.cfg), watchers).run(), name="comments")]
            if self.cfg.enter_all:  # 入室するルーム (enter_rooms) も配信中の一覧から見つかる
                entrance: RoomEntrance = AllRoomsEntrance(self.cfg.status_poll_interval, api.onlives, ctx.cookies,
                                                          skip=self.cfg.rooms, state_dir=Path(self.cfg.state_file).parent)
                background.append(asyncio.create_task(entrance.run(), name="entrance"))
            elif enter_rooms:
                entrance = RoomEntrance(enter_rooms, self.cfg.status_poll_interval, api.room_status, ctx.cookies,
                                        state_dir=Path(self.cfg.state_file).parent)
                background.append(asyncio.create_task(entrance.run(), name="entrance"))
            tasks += background
            saver = asyncio.create_task(self._periodic_save(ctx, api, watchers))
            wake_wait = asyncio.create_task(self._wake.wait())
            disc_wait = asyncio.create_task(disconnected.wait())
            try:
                await asyncio.wait([wake_wait, disc_wait, saver], return_when=asyncio.FIRST_COMPLETED)
            finally:
                for w in watchers:
                    w.stop()
                for t in background:
                    t.cancel()
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
        if required and self.cfg.rooms and failures == len(self.cfg.rooms):
            raise RuntimeError("すべてのルームの状態を取得できません")
        return ids

    async def _login_rooms(self, api: ShowroomAPI, room_ids: list[int]) -> list[int]:
        """ログイン確認に使うルーム。視聴ルームが無ければ、配信中のルームを 1 つだけ使う。"""
        if room_ids:
            return room_ids
        if self._probe_room is None:
            self._probe_room = await api.live_room_id()
        return [self._probe_room] if self._probe_room is not None else []

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
                try:
                    last_state = await self._check_login(ctx, api, watchers, last_state)
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001 - 確認の失敗で視聴中のブラウザまで落とさない
                    log.warning("ログイン状態を確認できませんでした: %s", e)
            if time.monotonic() >= next_save:
                next_save = time.monotonic() + save_interval
                await save_state(ctx, self.cfg)

    async def _check_login(self, ctx: BrowserContext, api: ShowroomAPI, watchers: list[RoomWatcher],
                           last_state: bool | None) -> bool | None:
        # ルームの room_id はウォッチャーが配信状況の確認で取得済みなので、問い合わせ直さない
        room_ids = login_room_ids(watchers) or (await self._room_ids(api) if self.cfg.rooms else [])[:1]
        room_ids = await self._login_rooms(api, room_ids)
        state = await probe_login(api, room_ids)
        if state is None:
            # 確認に使った配信中のルームが使えなくなった可能性があるので、次は探し直す
            self._probe_room = None
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
        api = ShowroomAPI(ctx.request)
        state = await api.is_logged_in(await api.live_room_id())
        if state is True:
            print("ログイン状態を確認できました。")
        elif state is False:
            print("まだログインしていないようです。もう一度 login を実行してください。")
        await browser.close()


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
