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
import threading
import time
from pathlib import Path
from typing import Any, Callable

from . import research
from .research import PENDING_RANKING, RANKING_RETRIES, SKIPPED_ROOM, Collector, anchor_order, is_missing

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
RETRY_AFTER = 86400  # 失敗したルーム・イベントを後回しにする時間 (秒)
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
        self._history_failures = 0
        self._aggregated = False
        self.doing = ""
        self.last_error: str | None = None
        self.running = False
        self.started_at: float | None = None
        self._stop = threading.Event()

    @property
    def store(self):
        return self.c.store

    def state(self) -> dict[str, Any]:
        return {"enabled": True, "running": self.running, "doing": self.doing, "started_at": self.started_at,
                "last_error": self.last_error, "requests": self.c.requests, "failures": self.c.failures,
                "scan": self.scan}

    # ---------------------------------------------------------------- 動かす
    def stop(self) -> None:
        self._stop.set()

    def run_forever(self) -> None:
        self.running, self.started_at = True, self._clock()
        log.info("SHOWROOM の全ルームの情報を集め始めます (ルーム番号の調べ: %s)", "する" if self.scan else "しない")
        try:
            while not self._stop.is_set():
                try:
                    worked = self.step()
                except Exception as e:  # noqa: BLE001 - 1 つの仕事の失敗で止めない
                    self.last_error = f"{self.doing}: {str(e).splitlines()[0] if str(e) else type(e).__name__}"
                    log.warning("全ルームの収集で失敗しました (%s)", self.last_error)
                    worked = True
                    self._stop.wait(5)
                if not worked:
                    self.doing = "待機中"
                    self._stop.wait(IDLE)
        finally:
            self.running = False
            self.c.close()

    def step(self) -> bool:
        """仕事を 1 つ行う。することが無ければ False。"""
        now = self._clock()
        st = self.store
        if now - (st.meta("live_at") or 0) >= LIVE_EVERY:
            return self._onlives(now)
        if now - (st.meta("events_at") or 0) >= EVENTS_EVERY:
            return self._event_list(now)
        self._aggregate()
        # 登録時期の目印を先にそろえる (その間も、開催中のイベントの推移は 1 時間ごとに残す)
        if not st.meta("anchor_done_at") and (self._next_progress(now) or self._next_signup_anchor(now)):
            return True
        jobs = [self._next_progress, self._next_ranking, self._next_room, self._next_event_rooms, self._next_event_history]
        if self.scan:
            jobs.append(self._next_scan)
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
        rooms = [{"room_id": x.get("room_id"), "key": x.get("room_url_key"), "name": x.get("main_name"),
                  "followers": x.get("follower_num"), "live_at": now}
                 for g in data.get("onlives") or [] for x in g.get("lives") or [] if isinstance(x, dict)]
        new = self.store.upsert_rooms(rooms, "live", now)
        if new:
            log.info("配信中のルームから %d ルームを新しく見つけました", new)
        return True

    def _event_list(self, now: float) -> bool:
        self.doing = "イベントの一覧"
        self.store.set_meta("events_at", now)
        events: list[dict[str, Any]] = []
        try:
            for status in (1, 3, 4):  # 開催中・開催予定・終了
                page: Any = 1
                while page and page <= EVENT_PAGES:
                    data = self.c.get(f"/api/event/search?status={status}&page={page}") or {}
                    events += [research._event_info(e) for e in data.get("event_list") or [] if isinstance(e, dict)]
                    page = data.get("next_page")
        finally:
            self.store.upsert_events(events)  # 途中で失敗しても、読めた分は残す
        return True

    def _read_event_rooms(self, event_id: int, now: float) -> list[dict[str, Any]]:
        """イベントの参加ルームと、各ルームのイベントのポイント・順位を読む (古いイベントも番号で読める)。"""
        rooms = []
        page: Any = 1
        while page and page <= EVENT_ROOM_PAGES:
            try:
                data = self.c.get(f"/api/event/room_list?event_id={event_id}&p={page}") or {}
            except Exception as err:  # noqa: BLE001
                if not is_missing(err):
                    raise
                break
            rooms += [{"room_id": r.get("room_id"), "key": r.get("room_url_key"), "name": r.get("room_name"),
                       "followers": r.get("follower_num"), "live_at": r.get("last_lived_at") or None,
                       "point": r.get("point"), "rank": r.get("rank")}
                      for r in data.get("list") or [] if isinstance(r, dict) and r.get("room_id")]
            page = data.get("next_page")
        self.store.set_event_rooms(event_id, rooms, now)
        return rooms

    def _next_event_rooms(self, now: float) -> bool:
        """一覧で見つけたイベントの参加ルームを、まだ読んでいない (または読み直す時期の) ものから 1 つ読む。

        開催中・開催予定のイベントは EVENT_ROOMS_EVERY ごとに、終わったイベントは終わったあと 1 回読み直す
        (ルームのポイントと順位が決まるので)。
        """
        e = self.store.one(
            """SELECT * FROM events WHERE (retry_at IS NULL OR retry_at < ?)
                 AND (rooms_at IS NULL OR (rooms_at < COALESCE(end, 0) + 3600 AND rooms_at < ?))
               ORDER BY rooms_at IS NOT NULL, COALESCE(end, 0) DESC LIMIT 1""",
            (now, now - EVENT_ROOMS_EVERY))
        if e is None:
            return False
        self.doing = f"イベントの参加ルーム ({e['name'] or e['event_id']})"
        try:
            rooms = self._read_event_rooms(e["event_id"], now)
        except Exception as err:  # noqa: BLE001
            self._failed("events", "event_id", e["event_id"], now, err)
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
        self._aggregated = not self.store.build_user_month()

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
            self.store.conn.execute("UPDATE events SET progress_at=? WHERE event_id=?", (now, e["event_id"]))
            self._failed("events", "event_id", e["event_id"], now, err)
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
    def _failed(self, table: str, id_col: str, value: int, now: float, err: BaseException) -> None:
        """失敗したルーム・イベントは RETRY_AFTER 秒後まで後回しにする (同じものを続けて問い合わせない)。"""
        self.store.conn.execute(f"UPDATE {table} SET retry_at=? WHERE {id_col}=?", (now + RETRY_AFTER, value))
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
                self.store.conn.execute("UPDATE rooms SET collected_at=? WHERE room_id=?", (now, r["room_id"]))
            else:
                self._failed("rooms", "room_id", r["room_id"], now, e)
        else:
            self.store.conn.execute("UPDATE rooms SET retry_at=NULL WHERE room_id=?", (r["room_id"],))
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
