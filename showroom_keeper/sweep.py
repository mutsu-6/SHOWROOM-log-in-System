"""SHOWROOM の全ルームの情報を、少しずつ集め続ける (research_all を有効にしたときだけ動く)。

ルームの見つけ方:
- 配信中のルーム /api/live/onlives (LIVE_EVERY 秒ごと)
- イベントの一覧 /api/event/search (開催中・開催予定・終了) と、その参加ルーム /api/event/room_list
- 過去のイベント: イベント番号を新しい順に 1 までさかのぼり、参加ルームと各ルームのポイント・順位を読む
  (2014 年からのギフトの量の移り変わりを見るため)
- 登録時期の目印: 古いイベントを番号で飛び飛びに (粗い間隔から細かく) 読み、数ルームの貢献ランキングを取る
  (ユーザー ID から登録時期を推測するため。最初に 1 時間ほどでそろえる)
- ルーム番号を 1 から順に調べる /api/room/profile?room_id= (最初の 1 回だけ。見つかっている最大の番号を
  SCAN_MARGIN 過ぎたら終わる)

見つけたルームごとに、累計ランキング・参加したイベントを取り (research.Collector.refresh_room)、
終わったイベントの貢献ランキングを新しいイベントから 1 件ずつ取る (古いイベントの分も)。問い合わせは指定のルームの収集と同じ research.THROTTLE を通すので、
両方が動いていても SHOWROOM への問い合わせは合わせて 1 秒に 1 回まで (混んでいれば自動で間隔を広げる)。

1 回の step() では小さな仕事を 1 つだけ行い、仕事の種類は順番に回す (どれかが溜まっても、ほかが止まらないように)。
"""

from __future__ import annotations

import logging
import shutil
from datetime import datetime
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Callable

from . import research, room_analysis
from .recorder import next_page_number
from .status import JST
from .research import ARCHIVE_HOLD, PENDING_RANKING, RANKING_RETRIES, SKIPPED_ROOM, Collector, anchor_order, is_missing

log = logging.getLogger("research")

PROGRESS_EVERY = 3600  # 開催中のイベントのポイントを残す間隔 (秒)
LIVE_EVERY = 300  # 配信中のルームを見る間隔 (秒)
EVENTS_EVERY = 6 * 3600  # イベントの一覧を見る間隔 (秒)
EVENT_PAGES = 30  # イベントの一覧 (1 ページ 30 件) を状態ごとに読むページ数の上限
EVENT_ROOM_PAGES = 100  # 1 つのイベントの参加ルーム (1 ページ 30 件) を読むページ数の上限
EVENT_ROOMS_EVERY = 86400  # 開催中・開催予定のイベントの参加ルームを読み直す間隔 (秒)
ROOM_EVERY = 7 * 86400  # 最近配信したルームの累計ランキングなどを取り直す間隔 (秒)
ACTIVE_DAYS = 30  # この日数のうちに配信したルームを、取り直しの対象にする
SCAN_MARGIN = 20000  # ルーム番号の順番の調べを、見つかっている最大の番号からこれだけ過ぎたら終える
IDLE = 60  # することが無いときに待つ時間 (秒)
STALL = 900  # 1 つの仕事がこれより長くかかったら、どこで止まっているかをログに書く (秒)
RETRY_AFTER = 86400  # 失敗したルーム・イベントを後回しにする時間 (秒)
FINAL_WINDOW = 3 * 3600  # 終わるこれだけ前から、上位 30 ルームのポイントを細かく残す (秒)
FINAL_EVERY = 60  # その間隔 (秒)
FINAL_LAST = 600  # 終わるこれだけ前からは、さらに細かく (秒)
FINAL_LAST_EVERY = 15  # その間隔 (秒)
FINAL_SPRINT = 60  # 終わるこれだけ前からは、もっと細かく (ラスト数秒のまとめ投げを見るため。秒)
FINAL_SPRINT_EVERY = 2  # その間隔 (秒。同時に終わるイベントが多い時は、問い合わせの間隔 0.5 秒の分ずつ遅れる)
FINAL_AFTER = 900  # 終わったあとも、これだけのあいだは 1 分ごとに読む (最後のギフトの反映を見るため。秒)
FANS_EVERY = 7 * 86400  # ルームのアクティブファンを読み直す間隔 (秒)
FANS_PAGE = 50  # アクティブファンの 1 回に読む人数 (SHOWROOM の 1 ページ)
FANS_PAGES = 20  # 1 ルームで読むページの上限 (1000 人)
FANS_PAST_PAGES = 4  # 前の月を読み足す時に、1 か月で読むページの上限 (200 人。レベルの高い順なので、常連は入る)
FANS_FIRST = "202202"  # SHOWROOM に残っている、いちばん古いアクティブファンの月 (2026-10-18 に確かめた。それより前は 404)
FANS_EMPTY_STOP = 3  # 前の月を読み足して、この月数続けて 0 人ならやめる (ルームができる前)
OWNER_EVERY = 90 * 86400  # ユーザーのプロフィール (持っているルーム) を読み直す間隔 (秒)
CONFIRM_DAYS = 14  # 終わってからこの日数のうちのイベントは、正式な結果が出たあとに参加ルームを読み直す
PAGE_RETRY = 86400  # イベントのページが読めなかった時に、読み直すまでの時間 (秒)
NOTICES_EVERY = 86400  # お知らせを読む間隔 (秒)
NOTICE_PAGES = 50  # お知らせの一覧を読むページの上限 (最初の回に、古い分までさかのぼる)
NOTICE_COUNT = 50  # お知らせの一覧の 1 ページの件数
PROGRESS_RETRY = 600  # 開催中のイベントのポイントが読めなかった時に、読み直すまでの時間 (秒。推移が抜けないように短く)
DISK_MIN = 2 << 30  # ディスクの空きがこれより少なくなったら、収集を止める (ログイン状態などが書けなくならないように)
DISK_CHECK_EVERY = 600  # ディスクの空きを確かめる間隔 (秒)
BATCH = 100  # 次の仕事の候補をまとめて調べる件数
ANCHOR_ROOMS = 3  # 1 つのイベントで貢献ランキングを取るルームの数 (上位・中ほど・下位)


class Sweeper:
    def __init__(self, state_dir: Path, collector: Collector | None = None, scan: bool = True,
                 clock: Callable[[], float] = time.time):
        self.state_dir = Path(state_dir)
        self.c = collector or Collector(self.state_dir)
        self.scan = scan
        self._clock = clock
        self._turn = 0
        self._rooms: list[Any] = []
        self._rankings: list[Any] = []
        self._fan_rooms: list[Any] = []
        self._owners: list[Any] = []
        self._fan_history: list[Any] = []
        self._pages: list[Any] = []
        self._history_failures = 0
        self._aggregated = False
        self.doing = ""
        self.last_error: str | None = None
        self.running = False
        self.started_at: float | None = None
        self.step_started_at: float | None = None  # 今の仕事を始めた時刻 (仕事の合間は None)
        self.last_step_at: float | None = None  # 最後に仕事を終えた時刻
        self._thread_id: int | None = None
        self._disk_checked = 0.0
        self._final_at: dict[int, float] = {}  # 終わる直前の推移を、イベントごとに最後に読んだ時刻
        self.disk_low: int | None = None  # ディスクの空きが少なくて止めている時の空き (バイト)
        self._stop = threading.Event()

    @property
    def store(self):
        return self.c.store

    def state(self) -> dict[str, Any]:
        busy = self._clock() - self.step_started_at if self.step_started_at else 0
        return {"enabled": True, "running": self.running, "doing": self.doing, "started_at": self.started_at,
                "last_error": self.last_error, "requests": self.c.requests, "failures": self.c.failures,
                "scan": self.scan, "last_step_at": self.last_step_at, "busy_seconds": round(busy), "disk_low": self.disk_low}

    # ---------------------------------------------------------------- 動かす
    def stop(self) -> None:
        self._stop.set()

    def _watch(self) -> None:
        """1 つの仕事が STALL 秒より長くかかっていたら、収集のスレッドが今どこにいるかをログに書く (1 つの仕事につき 1 回)。

        収集が止まったように見える時に、原因 (問い合わせの待ち・データベースの待ち・同じ処理の繰り返し) を見つけるため。
        """
        reported = None
        while not self._stop.wait(60):
            started = self.step_started_at
            if not started or self._clock() - started < STALL or reported == started:
                continue
            reported = started
            frame = sys._current_frames().get(self._thread_id or -1)
            where = "".join(traceback.format_stack(frame)[-6:]) if frame else "(スレッドが見つかりません)"
            log.warning("全ルームの収集の仕事「%s」が %d 分続いています。いまの場所:\n%s",
                        self.doing, (self._clock() - started) // 60, where)

    def run_forever(self) -> None:
        self.running, self.started_at = True, self._clock()
        self._thread_id = threading.get_ident()
        threading.Thread(target=self._watch, daemon=True, name="sweep-watch").start()
        log.info("SHOWROOM の全ルームの情報を集め始めます (ルーム番号の調べ: %s)", "する" if self.scan else "しない")
        try:
            while not self._stop.is_set():
                self.step_started_at = self._clock()
                try:
                    worked = self.step()
                except Exception as e:  # noqa: BLE001 - 1 つの仕事の失敗で止めない
                    self.last_error = f"{self.doing}: {str(e).splitlines()[0] if str(e) else type(e).__name__}"
                    log.warning("全ルームの収集で失敗しました (%s)", self.last_error)
                    worked = True
                    self._stop.wait(5)
                finally:
                    self.step_started_at, self.last_step_at = None, self._clock()
                if not worked:
                    self.doing = "待機中"
                    self._stop.wait(IDLE)
        finally:
            self.running = False
            self.c.close()

    def _disk_ok(self, now: float) -> bool:
        """ディスクの空きが DISK_MIN より多いか (DISK_CHECK_EVERY ごとに確かめる)。少ない時は収集を止めて、ログと画面に出す。"""
        if now - self._disk_checked >= DISK_CHECK_EVERY:
            self._disk_checked = now
            free = shutil.disk_usage(self.state_dir).free
            if free < DISK_MIN:
                if self.disk_low is None:
                    log.warning("ディスクの空きが %.1fGB しかないので、全ルームの収集を止めます (空きが %.0fGB を超えたら再開します)。"
                                "視聴とログインの維持は続けます", free / 2**30, DISK_MIN / 2**30)
                self.disk_low = free
            elif self.disk_low is not None:
                log.info("ディスクの空きが戻ったので、全ルームの収集を再開します")
                self.disk_low = None
        return self.disk_low is None

    def step(self) -> bool:
        """仕事を 1 つ行う。することが無ければ False。"""
        now = self._clock()
        if not self._disk_ok(now):
            self.doing = "ディスクの空きが少ないので止めています"
            return False
        st = self.store
        if self._next_final(now):  # 終わる直前の推移は、時刻が大事なので、ほかの仕事より先に行う
            return True
        if now - (st.meta("live_at") or 0) >= LIVE_EVERY:
            return self._onlives(now)
        if now - (st.meta("events_at") or 0) >= EVENTS_EVERY:
            return self._event_list(now)
        self._aggregate()
        if room_analysis.step(st, now):  # ルームの分析 (SHOWROOM には問い合わせないので、仕事の順番には数えない)
            self.doing = "ルームの分析"
        # 登録時期の目印を先にそろえる (その間も、開催中のイベントの推移は 1 時間ごとに残す)
        if not st.meta("anchor_done_at") and (self._next_progress(now) or self._next_signup_anchor(now)):
            return True
        # 貢献ランキングはギフター分析にいちばん効くので、ほかの仕事 1 つごとに 1 回回す (問い合わせのおよそ半分を使う)
        others = [self._next_progress, self._next_room, self._next_event_rooms, self._next_fans, self._next_event_history,
                  self._next_owner, self._next_event_page, self._next_notices, self._next_fans_history]
        if self.scan:
            others.append(self._next_scan)
        jobs = [j for job in others for j in (job, self._next_ranking)]
        for i in range(len(jobs)):
            job = jobs[(self._turn + i) % len(jobs)]
            if job(now):
                self._turn = (self._turn + i + 1) % len(jobs)
                return True
        return False

    # ---------------------------------------------------------------- ルームを見つける
    def _onlives(self, now: float) -> bool:
        self.doing = "配信中のルーム"
        self.store.set_meta("live_at", now)  # 失敗しても次は LIVE_EVERY 秒後 (続けて問い合わせない)
        data = self.c.get("/api/live/onlives") or {}
        lives = [x for g in data.get("onlives") or [] for x in g.get("lives") or [] if isinstance(x, dict)]
        rooms = [{"room_id": x.get("room_id"), "key": x.get("room_url_key"), "name": x.get("main_name"),
                  "followers": x.get("follower_num"), "live_at": now, "viewers": x.get("view_num"),
                  "started_at": x.get("started_at")} for x in lives]
        new = self.store.upsert_rooms(rooms, "live", now)
        # 同じルームが「人気」などのまとめ枠にも出るので、1 ルーム 1 回にする
        self.store.add_room_daily(list({r["room_id"]: r for r in rooms if r["room_id"]}.values()), now)
        if lives and not self.store.meta("onlives_keys"):  # 一覧にどの項目があるか (視聴者数などを使えるか確かめるため)
            self.store.set_meta("onlives_keys", sorted(lives[0]))
        if new:
            log.info("配信中のルームから %d ルームを新しく見つけました", new)
        return True

    def _event_list(self, now: float) -> bool:
        self.doing = "イベントの一覧"
        self.store.set_meta("events_at", now)
        events: list[dict[str, Any]] = []
        raws: list[dict[str, Any]] = []  # 一覧の 1 件そのまま (説明文・続きものの親・特典の順位など)
        try:
            for status in (1, 3, 4):  # 開催中・開催予定・終了
                page: Any = 1
                while page and page <= EVENT_PAGES:
                    data = self.c.get(f"/api/event/search?status={status}&page={page}") or {}
                    got = [e for e in data.get("event_list") or [] if isinstance(e, dict)]
                    events += [research._event_info(e) for e in got]
                    raws += got
                    page = next_page_number(page, data.get("next_page"))
        finally:
            self.store.upsert_events(events)  # 途中で失敗しても、読めた分は残す
            self.store.upsert_event_info(raws, now)
        return True

    def _read_event_rooms(self, event_id: int, now: float) -> list[dict[str, Any]]:
        """イベントの参加ルームと、各ルームのイベントのポイント・順位を読む (古いイベントも番号で読める)。"""
        rooms: list[dict[str, Any]] = []
        seen: set[Any] = set()
        page: Any = 1
        while page and page <= EVENT_ROOM_PAGES:
            try:
                data = self.c.get(f"/api/event/room_list?event_id={event_id}&p={page}") or {}
            except Exception as err:  # noqa: BLE001
                if not is_missing(err):
                    raise
                break
            got = [{"room_id": r.get("room_id"), "key": r.get("room_url_key"), "name": r.get("room_name"),
                     "followers": r.get("follower_num"), "live_at": r.get("last_lived_at") or None,
                     "point": r.get("point"), "rank": r.get("rank")}
                   for r in data.get("list") or [] if isinstance(r, dict) and r.get("room_id")]
            new = [r for r in got if r["room_id"] not in seen]
            rooms += new
            seen.update(r["room_id"] for r in new)
            # 次のページの番号が進まない・同じルームしか返ってこない時は、そこでやめる
            # (最後のページより先を頼むと、1 ページ目や最後のページが返ってくるため。同じページを読み続けないように)
            page = next_page_number(page, data.get("next_page")) if new else None
        self.store.set_event_rooms(event_id, rooms, now)
        return rooms

    def _next_event_rooms(self, now: float) -> bool:
        """一覧で見つけたイベントの参加ルームを、まだ読んでいない (または読み直す時期の) ものから 1 つ読む。

        開催中・開催予定のイベントは EVENT_ROOMS_EVERY ごとに、終わったイベントは終わったあと 1 回読み直す
        (ルームのポイントと順位が決まるので)。最近終わったイベントは、正式な結果が出たあと (次の日の正午すぎ) にもう 1 回読む
        (不正なポイントが引かれて、ポイント・順位が変わることがあるため。終わった直後の値は event_final に残っている)。
        """
        confirmed = research.CONFIRMED_SQL.replace("e.end", "end")
        e = self.store.one(
            f"""SELECT * FROM events WHERE (retry_at IS NULL OR retry_at < ?)
                 AND (rooms_at IS NULL OR (rooms_at < COALESCE(end, 0) + 3600 AND rooms_at < ?)
                      OR (end > ? AND rooms_at < {confirmed} AND {confirmed} < ?))
               ORDER BY rooms_at IS NOT NULL, COALESCE(end, 0) DESC LIMIT 1""",
            (now, now - EVENT_ROOMS_EVERY, now - CONFIRM_DAYS * 86400, now))
        if e is None:
            return False
        self.doing = f"イベントの参加ルーム ({e['name'] or e['event_id']})"
        try:
            rooms = self._read_event_rooms(e["event_id"], now)
        except Exception as err:  # noqa: BLE001
            # 開催中・これからのイベントは、推移を止めないように PROGRESS_RETRY 後に読み直す
            ongoing = e["end"] is None or e["end"] > now
            self._failed("events", "event_id", e["event_id"], now, err, after=PROGRESS_RETRY if ongoing else RETRY_AFTER)
            return True
        if e["progress_at"] is not None and e["end"] is not None and e["end"] <= now:
            # 推移を残していたイベントが終わった: 終わった時のポイント (最終結果) も、終わった時刻の値として推移に入れる
            self.store.add_progress(e["event_id"], rooms, e["end"])
        return True

    def _aggregate(self) -> None:
        """前に集めた貢献ランキングを、月ごとの集計 (user_month) に少しずつ入れる。

        SHOWROOM には問い合わせないので、仕事の順番には数えず、ほかの仕事の前に少しずつ行う。
        """
        if self._aggregated:
            return
        self.doing = "月ごとの集計"
        months = self.store.build_user_month()
        self._aggregated = not (self.store.build_room_stats() or months)

    def _next_final(self, now: float) -> bool:
        """終わる直前の推移: 終わる FINAL_WINDOW 前から FINAL_EVERY ごと (最後の FINAL_LAST は FINAL_LAST_EVERY ごと、
        最後の FINAL_SPRINT は FINAL_SPRINT_EVERY ごと)、
        終わったあと FINAL_AFTER のあいだも FINAL_EVERY ごとに、参加ルームの 1 ページ目 (上位 30 ルーム) を event_final に残す。

        追い上げがどれだけ終わりぎりぎりに集まっているか (差し・追い込みの上手さ) を見るため。1 時間ごとの推移では見えない。
        1 イベントにつき 3 時間で 250 回ほど問い合わせる (同時に終わるイベントが多い時は、間隔の過ぎたものから順に)。
        ポイントは遅れずに反映される (利用者に確かめた。ラスト 5 秒にまとめて投げることもできる) ので、最後の 1 分を細かく読む。
        """
        due = []
        for e in self.store.all("""SELECT event_id, name, end FROM events WHERE progress_at IS NOT NULL AND end > ? AND end <= ?""",
                                (now - FINAL_AFTER, now + FINAL_WINDOW)):
            left = e["end"] - now
            every = (FINAL_SPRINT_EVERY if 0 < left <= FINAL_SPRINT else FINAL_LAST_EVERY if 0 < left <= FINAL_LAST
                     else FINAL_EVERY)
            last = self._final_at.get(e["event_id"], 0)
            if now - last >= every:
                due.append((now - last - every, e))
        if not due:
            return False
        _, e = max(due, key=lambda x: x[0])  # いちばん遅れているもの
        self._final_at[e["event_id"]] = now
        self.doing = f"終わる直前のポイント ({e['name'] or e['event_id']})"
        try:
            data = self.c.get(f"/api/event/room_list?event_id={e['event_id']}&p=1") or {}
        except Exception as err:  # noqa: BLE001 - 次の回にまた読む
            self.last_error = f"{self.doing}: {str(err).splitlines()[0] if str(err) else type(err).__name__}"
            return True
        rows = [{"room_id": r.get("room_id"), "point": r.get("point"), "rank": r.get("rank")}
                for r in data.get("list") or [] if isinstance(r, dict)]
        self.store.add_final(e["event_id"], rows, now)
        return True

    def _next_progress(self, now: float) -> bool:
        """開催中のイベントの参加ルームのポイントと順位を、PROGRESS_EVERY ごとに残す (ポイントの推移)。

        推移は集め始めないと貯まらないので、画面や予想より先に集める。1 つのイベントにつき 1 時間に 1 回、
        参加ルームの一覧のページ数だけ問い合わせる (開催中のイベントが 60 件なら 1 時間に 200 回ほど)。
        """
        e = self.store.one(
            """SELECT * FROM events WHERE start IS NOT NULL AND start <= ? AND end > ?
                 AND (progress_at IS NULL OR progress_at < ?) AND (retry_at IS NULL OR retry_at < ?)
               ORDER BY progress_at IS NOT NULL, progress_at LIMIT 1""",
            (now, now, now - PROGRESS_EVERY, now))
        if e is None:
            return False
        self.doing = f"開催中のイベントのポイント ({e['name'] or e['event_id']})"
        try:
            rooms = self._read_event_rooms(e["event_id"], now)
        except Exception as err:  # noqa: BLE001
            # 1 回の失敗で丸 1 日止めると、その日の推移が抜けて走り方が出せなくなるので、PROGRESS_RETRY 後に読み直す
            self.store.conn.execute("UPDATE events SET progress_at=? WHERE event_id=?",
                                    (now - PROGRESS_EVERY + PROGRESS_RETRY, e["event_id"]))
            self._failed("events", "event_id", e["event_id"], now, err, after=PROGRESS_RETRY)
            return True
        self.store.add_progress(e["event_id"], rooms, now)
        return True

    def _next_event_history(self, now: float) -> bool:
        """イベント番号を新しい順にさかのぼって、過去のイベントの参加ルームとポイントを 1 つ読む (2014 年から)。"""
        st = self.store
        eid = st.meta("history_next")
        if eid is None:
            eid = int(st.scalar("SELECT MAX(event_id) FROM events", default=0))
            if not eid:
                return False  # イベントの一覧を読んでから始める
        eid = int(eid)
        while eid > 0 and st.one("SELECT 1 FROM events WHERE event_id=? AND (rooms_at IS NOT NULL OR name IS NOT NULL)", (eid,)):
            eid -= 1  # 一覧やルームの過去のイベントで分かっているもの (_next_event_rooms で読む)
        if eid <= 0:
            if not st.meta("history_done_at"):
                st.set_meta("history_done_at", now)
                log.info("過去のイベントをすべて読みました")
            st.set_meta("history_next", 0)
            return False
        self.doing = f"過去のイベント (番号 {eid})"
        try:
            self._read_event_rooms(eid, now)
        except Exception as err:  # noqa: BLE001
            self._history_failures += 1
            if self._history_failures < RANKING_RETRIES:
                raise  # 番号は進めない (次の回にもう一度)
            self.last_error = f"{self.doing}: {str(err).splitlines()[0] if str(err) else type(err).__name__}"
            log.warning("過去のイベントを読めなかったので飛ばします (%s)", self.last_error)
        self._history_failures = 0
        st.set_meta("history_next", eid - 1)
        return True

    def _next_signup_anchor(self, now: float) -> bool:
        """登録時期の目印を 1 つ読む: 古いイベントの参加ルームと、数ルームの貢献ランキング (イベントの日付も分かる)。"""
        st = self.store
        top = st.meta("anchor_max")
        if top is None:
            top = int(st.scalar("SELECT MAX(event_id) FROM events", default=0))
            if not top:
                return False  # イベントの一覧を読んでから始める
            st.set_meta("anchor_max", top)
        order = anchor_order(int(top))
        pos = int(st.meta("anchor_pos", 0))
        while pos < len(order) and st.one("""SELECT 1 FROM room_events re JOIN events e USING(event_id)
                                            WHERE re.event_id=? AND re.ranking_at IS NOT NULL AND e.end IS NOT NULL""", (order[pos],)):
            pos += 1  # 日付と貢献ランキングが分かっているイベント
        if pos >= len(order):
            st.set_meta("anchor_pos", pos)
            st.set_meta("anchor_done_at", now)
            log.info("登録時期の目印をそろえました (%d イベント)", len(order))
            return False
        eid = order[pos]
        self.doing = f"登録時期の目印 (イベント番号 {eid})"
        try:
            self._read_event_rooms(eid, now)
            rooms = [r[0] for r in st.all("SELECT room_id FROM room_events WHERE event_id=? AND point > 0 ORDER BY rank, room_id",
                                          (eid,))]
            for rid in dict.fromkeys(rooms[i] for i in (0, len(rooms) // 2, -1)[:ANCHOR_ROOMS] if rooms):
                self.c.fetch_ranking(eid, rid, final=True)
        except Exception as err:  # noqa: BLE001 - 目印は飛び飛びなので、読めないものは飛ばす
            if not is_missing(err):
                self.last_error = f"{self.doing}: {str(err).splitlines()[0] if str(err) else type(err).__name__}"
                log.warning("登録時期の目印を読めなかったので飛ばします (%s)", self.last_error)
        st.set_meta("anchor_pos", pos + 1)
        return True

    def _next_scan(self, now: float) -> bool:
        """ルーム番号を 1 つ調べる (無い番号は 404)。"""
        st = self.store
        if st.meta("scan_done_at"):
            return False
        rid = int(st.meta("scan_next", 1))
        end = int(st.scalar("SELECT MAX(room_id) FROM rooms", default=0)) + SCAN_MARGIN
        st.set_meta("scan_end", end)
        if rid > end:
            st.set_meta("scan_done_at", now)
            log.info("ルーム番号を %d まで調べ終えました", rid - 1)
            return False
        self.doing = f"ルーム番号 {rid}"
        try:
            p = self.c.get(f"/api/room/profile?room_id={rid}") or {}
        except Exception as e:  # noqa: BLE001
            if not is_missing(e):
                raise  # 番号は進めない (次の回にもう一度)
            p = None
        if p and p.get("room_url_key"):
            st.upsert_rooms([{"room_id": rid, "key": p.get("room_url_key"), "name": p.get("room_name"),
                              "followers": p.get("follower_num"), "live_at": now if p.get("is_onlive") else None}],
                            "scan", now)
        st.set_meta("scan_next", rid + 1)
        return True

    # ---------------------------------------------------------------- 集める
    def _failed(self, table: str, id_col: str, value: int, now: float, err: BaseException, after: float = RETRY_AFTER) -> None:
        """失敗したルーム・イベントは after 秒後まで後回しにする (同じものを続けて問い合わせない)。"""
        self.store.conn.execute(f"UPDATE {table} SET retry_at=? WHERE {id_col}=?", (now + after, value))
        self.last_error = f"{self.doing}: {str(err).splitlines()[0] if str(err) else type(err).__name__}"
        log.warning("全ルームの収集で失敗したので後回しにします (%s)", self.last_error)

    def _take(self, queue: list[Any], sql: str, args: tuple[Any, ...]) -> Any:
        """次の仕事を 1 つ取り出す。候補は BATCH 件ずつまとめて調べる (仕事のたびに全件を並べ替えない)。"""
        if not queue:
            queue.extend(self.store.all(f"{sql} LIMIT {BATCH}", args))
            queue.reverse()
        return queue.pop() if queue else None

    def _next_room(self, now: float) -> bool:
        """累計ランキング・参加したイベントを、まだ取っていない (または取り直す時期の) ルームから 1 つ取る。

        まだ取っていないルームは、最近配信した → イベントで見つけた → フォロワーの多い順。
        ルーム番号の調べで見つけたフォロワー 0 人のルームは取らない (ギフターがいない)。
        """
        r = self._take(self._rooms, f"""SELECT room_id, name, key FROM rooms
               WHERE (retry_at IS NULL OR retry_at < ?)
                 AND ((collected_at IS NULL AND NOT ({SKIPPED_ROOM}))
                      OR (collected_at < ? AND live_at > ?))
               ORDER BY collected_at IS NOT NULL, COALESCE(live_at, 0) DESC, found_via != 'event', COALESCE(followers, 0) DESC""",
                       (now, now - ROOM_EVERY, now - ACTIVE_DAYS * 86400))
        if r is None:
            return False
        self.doing = f"ルーム ({r['name'] or r['key'] or r['room_id']})"
        try:
            self.c.refresh_room(r["room_id"])
        except Exception as e:  # noqa: BLE001
            if is_missing(e):  # 無くなったルーム
                self.store.conn.execute("UPDATE rooms SET collected_at=?, gone=1 WHERE room_id=?", (now, r["room_id"]))
            else:
                self._failed("rooms", "room_id", r["room_id"], now, e)
        else:
            with self.store.tx() as c:
                c.execute("UPDATE rooms SET retry_at=NULL, gone=NULL WHERE room_id=?", (r["room_id"],))
                c.execute("UPDATE room_events SET failures=0 WHERE room_id=? AND failures=?", (r["room_id"], ARCHIVE_HOLD))
                # ルームがあると分かった: Internet Archive から名前で推測して足した貢献ランキングは、
                # SHOWROOM から本物 (ユーザー ID つき・全員) を取り直す
                c.execute("""UPDATE room_events SET final=0, ranking_at=NULL WHERE room_id=? AND event_id IN (
                               SELECT event_id FROM contributions WHERE room_id=? AND guessed IS NOT NULL)""",
                          (r["room_id"], r["room_id"]))
        return True

    def _next_fans(self, now: float) -> bool:
        """最近配信したルームのアクティブファン (その月のファンレベル) を、FANS_EVERY ごとに読む。

        ファンレベルは、見る・コメント・ギフトで上がるので、貢献ランキングに出ない (ポイントで貢献していない) 常連も分かる。
        """
        r = self._take(self._fan_rooms, """SELECT room_id, name, key FROM rooms
               WHERE live_at > ? AND (fans_at IS NULL OR fans_at < ?) ORDER BY fans_at IS NOT NULL, fans_at, live_at DESC""",
                       (now - ACTIVE_DAYS * 86400, now - FANS_EVERY))
        if r is None:
            return False
        self.doing = f"アクティブファン ({r['name'] or r['key'] or r['room_id']})"
        ym = datetime.fromtimestamp(now, JST).strftime("%Y%m")
        users: list[dict[str, Any]] = []
        data: dict[str, Any] = {}
        for page in range(FANS_PAGES):
            try:
                data = self.c.get(f"/api/active_fan/users?room_id={r['room_id']}&ym={ym}&offset={page * FANS_PAGE}&limit={FANS_PAGE}") or {}
            except Exception as err:  # noqa: BLE001
                if not is_missing(err):
                    raise
                break
            got = [u for u in data.get("users") or [] if isinstance(u, dict)]
            users += got
            if len(got) < FANS_PAGE or len(users) >= int(data.get("total_user_count") or 0):
                break
        self.store.set_active_fans(r["room_id"], ym, data.get("fan_power"), data.get("total_user_count"), users, now)
        return True

    def _next_event_page(self, now: float) -> bool:
        """イベントのページ (ルール・特典・応募条件・ポイントの数え方) を読んで、文章を残す。

        1 イベント 1 回。開催中・開催予定のうちに読んだものは、終わったあとにもう 1 回読む (途中でルールが書き足されるため)。
        新しいイベントから読む。
        """
        r = self._take(self._pages, """SELECT e.event_id, e.key, e.name FROM events e LEFT JOIN event_pages p USING(event_id)
               WHERE e.key IS NOT NULL AND (p.event_id IS NULL OR (p.status = 0 AND p.at < ?)
                                            OR (p.status = 200 AND e.end IS NOT NULL AND p.at < e.end AND e.end < ?))
               ORDER BY COALESCE(e.end, 9e18) DESC""", (now - PAGE_RETRY, now))
        if r is None:
            return False
        self.doing = f"イベントのページ ({r['name'] or r['key']})"
        try:
            text = research.page_text(self.c.get_page(f"/event/{r['key']}"))
            self.store.set_event_page(r["event_id"], 200, text, now)
        except Exception as err:  # noqa: BLE001
            self.store.set_event_page(r["event_id"], 404 if is_missing(err) else 0, None, now)
            if not is_missing(err):
                log.debug("イベント %s のページを読めませんでした: %s", r["key"], err)
        return True

    def _next_notices(self, now: float) -> bool:
        """SHOWROOM のお知らせ (一覧と中身) を 1 日 1 回読む。一覧はログインが要るので、ログイン状態の Cookie を使う。

        新しいお知らせが無くなったところで止める (最初の回は NOTICE_PAGES ページまでさかのぼる)。
        """
        if now - (self.store.meta("notices_at") or 0) < NOTICES_EVERY:
            return False
        from .recorder import get_json, session_cookie

        self.store.set_meta("notices_at", now)
        self.doing = "お知らせ"
        cookie = session_cookie(self.state_dir / "storage_state.json")
        if not cookie:
            self.store.set_meta("notices_error", "ログイン状態がありません")
            return True
        page = 1
        try:
            while page and page <= NOTICE_PAGES:
                data = self.c.throttle.call(lambda p: get_json(p, cookie), f"/api/notices?count={NOTICE_COUNT}&page={page}") or {}
                if data.get("errors") or data.get("error"):
                    self.store.set_meta("notices_error", str(data.get("error_user_msg") or data.get("error") or data.get("errors"))[:200])
                    return True
                items = [x for x in data.get("list") or [] if isinstance(x, dict)]
                new = [x for x in items if x.get("notice_id")
                       and self.store.one("SELECT 1 FROM notices WHERE notice_id=? AND contents IS NOT NULL", (int(x["notice_id"]),)) is None]
                for x in new:  # 中身を読む
                    try:
                        detail = self.c.throttle.call(lambda p: get_json(p, cookie), f"/api/notices/{x['notice_id']}") or {}
                        x = {**x, **{k: v for k, v in detail.items() if k in ("label", "title", "contents", "created_at")}}
                    except Exception as err:  # noqa: BLE001 - 中身が読めなくても、題名は残す
                        log.debug("お知らせ %s の中身を読めませんでした: %s", x.get("notice_id"), err)
                    self.store.upsert_notices([x], now)
                if items and not new:
                    break  # ここから先は前に読んだもの
                page = next_page_number(page, data.get("next_page") or (page + 1 if page < int(data.get("last_page") or 0) else None))
            self.store.set_meta("notices_error", None)
        except Exception as err:  # noqa: BLE001
            self.store.set_meta("notices_error", str(err).splitlines()[0][:200] if str(err) else type(err).__name__)
        return True

    def _next_fans_history(self, now: float) -> bool:
        """前の月のアクティブファンを、新しい月からさかのぼって読み足す (SHOWROOM に FANS_FIRST から残っている)。

        最近配信したルームから、フォロワーの多い順に。1 回に 1 ルームの 1 か月 (FANS_PAST_PAGES ページまで)。
        rooms.fans_back に読んだいちばん古い月を残し、FANS_FIRST まで読むか、FANS_EMPTY_STOP か月続けて 0 人なら 'done'。
        """
        r = self._take(self._fan_history, """SELECT room_id, name, key, fans_back FROM rooms
               WHERE live_at > ? AND fans_at IS NOT NULL AND (fans_back IS NULL OR fans_back != 'done')
               ORDER BY COALESCE(followers, 0) DESC""", (now - ACTIVE_DAYS * 86400,))
        if r is None:
            return False
        this = datetime.fromtimestamp(now, JST).strftime("%Y%m")
        start = r["fans_back"] or this
        y, m = int(start[:4]), int(start[4:])
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
        ym = f"{y:04d}{m:02d}"
        if ym < FANS_FIRST:
            self.store.conn.execute("UPDATE rooms SET fans_back='done' WHERE room_id=?", (r["room_id"],))
            return True
        self.doing = f"前の月のアクティブファン ({r['name'] or r['key'] or r['room_id']}・{ym[:4]}/{ym[4:]})"
        users: list[dict[str, Any]] = []
        data: dict[str, Any] = {}
        missing = False
        for page in range(FANS_PAST_PAGES):
            try:
                data = self.c.get(f"/api/active_fan/users?room_id={r['room_id']}&ym={ym}&offset={page * FANS_PAGE}&limit={FANS_PAGE}") or {}
            except Exception as err:  # noqa: BLE001
                if not is_missing(err):
                    raise
                missing = True
                break
            got = [u for u in data.get("users") or [] if isinstance(u, dict)]
            users += got
            if len(got) < FANS_PAGE or len(users) >= int(data.get("total_user_count") or 0):
                break
        if not missing:
            self.store.set_active_fans(r["room_id"], ym, data.get("fan_power"), data.get("total_user_count"), users, now, current=False)
        # 読んだ月から新しい方へ FANS_EMPTY_STOP か月が、どれも 0 人なら、それより前にはルームが無かったとみる
        recent = [x[0] for x in self.store.all("SELECT total FROM room_fans WHERE room_id=? AND ym>=? ORDER BY ym LIMIT ?",
                                                (r["room_id"], ym, FANS_EMPTY_STOP))]
        empty = missing or (len(recent) >= FANS_EMPTY_STOP and not any(recent))
        self.store.conn.execute("UPDATE rooms SET fans_back=? WHERE room_id=?", ("done" if empty else ym, r["room_id"]))
        return True

    def _next_owner(self, now: float) -> bool:
        """ユーザーのプロフィールを読み、その人が持っているルーム (配信者のアカウントとルームの結び付き) を残す。

        アクティブファンのレベルの高い人から読む (配信者はほかのルームでも常連になっていることが多いため)。
        """
        r = self._take(self._owners, """SELECT af.user_id FROM active_fans af LEFT JOIN user_rooms ur USING(user_id)
               WHERE ur.user_id IS NULL OR ur.checked_at < ? OR (ur.room_id IS NOT NULL AND ur.room_key IS NULL)
               GROUP BY af.user_id ORDER BY MAX(af.level) DESC""",
                       (now - OWNER_EVERY,))
        if r is None:
            return False
        self.doing = "ユーザーのプロフィール"
        try:
            p = self.c.get(f"/api/user/profile?user_id={r['user_id']}") or {}
        except Exception as err:  # noqa: BLE001
            if not is_missing(err):
                raise
            p = {}
        room = p.get("room_profile") if isinstance(p.get("room_profile"), dict) else {}
        self.store.set_user_room(r["user_id"], room.get("room_id"), now, room.get("room_url_key"))
        if room.get("room_id"):
            self.store.upsert_rooms([{"room_id": room["room_id"], "key": room.get("room_url_key"), "name": room.get("room_name"),
                                      "followers": room.get("follower_num")}], "owner", now)
        return True

    def _next_ranking(self, now: float) -> bool:
        """終わったイベントの貢献ランキングを、新しいイベントから 1 件取る (失敗は数えて RANKING_RETRIES 回まで)。"""
        r = self._take(self._rankings, f"""SELECT re.room_id, re.event_id, e.name FROM room_events re JOIN events e USING(event_id)
                WHERE {PENDING_RANKING} ORDER BY e.end DESC""", (now - 3600, RANKING_RETRIES))
        if r is None:
            return False
        self.doing = f"貢献ランキング ({r['name'] or r['event_id']})"
        self.c.fetch_ranking(r["event_id"], r["room_id"], final=True)
        return True


SWEEPER: Sweeper | None = None


def start(state_dir: Path, scan: bool = True) -> Sweeper:
    """全ルームの収集を別のスレッドで始める (2 回目からは動いているものを返す)。"""
    global SWEEPER
    if SWEEPER is None or not SWEEPER.running:
        SWEEPER = Sweeper(state_dir, scan=scan)
        threading.Thread(target=SWEEPER.run_forever, daemon=True, name="sweep").start()
    return SWEEPER


def state() -> dict[str, Any]:
    return SWEEPER.state() if SWEEPER is not None else {"enabled": False}
