"""調査で集めた情報の保存先 (SQLite、state/research.db)。

全ルームを集めると貢献ランキングだけで数百万行になるので、JSON ファイルを全部読み込む方式はやめて
SQLite に入れる。ギフターごとの集計に使う user_room (ギフター × ルームの送ったポイント) は、
ランキングを保存するたびに更新しておき、分析のときに全部を数え直さないようにする。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator

log = logging.getLogger("research")

DB_NAME = "research.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS rooms (
    room_id INTEGER PRIMARY KEY, key TEXT, name TEXT, followers INTEGER,
    found_via TEXT, found_at REAL, live_at REAL, collected_at REAL, retry_at REAL, history_at REAL);
CREATE INDEX IF NOT EXISTS rooms_key ON rooms(key);
CREATE TABLE IF NOT EXISTS events (
    event_id INTEGER PRIMARY KEY, key TEXT, name TEXT, start REAL, end REAL, type TEXT,
    show_ranking INTEGER DEFAULT 1, rooms_at REAL, retry_at REAL);
CREATE TABLE IF NOT EXISTS watch (user_id INTEGER PRIMARY KEY, memo TEXT, added_at REAL, updated_at REAL);
CREATE TABLE IF NOT EXISTS event_progress (
    event_id INTEGER, room_id INTEGER, at REAL, point INTEGER, rank INTEGER, PRIMARY KEY (event_id, room_id, at));
CREATE INDEX IF NOT EXISTS event_progress_at ON event_progress(event_id, at);
CREATE TABLE IF NOT EXISTS room_events (
    room_id INTEGER, event_id INTEGER, ranking_at REAL, final INTEGER DEFAULT 0, failures INTEGER DEFAULT 0,
    point INTEGER, rank INTEGER, PRIMARY KEY (room_id, event_id)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS room_events_event ON room_events(event_id);
CREATE TABLE IF NOT EXISTS cumulative (
    room_id INTEGER, user_id INTEGER, point INTEGER, visits INTEGER, rank INTEGER, PRIMARY KEY (room_id, user_id))
    WITHOUT ROWID;
-- 累計ランキングの、ルームごとにおよそ月 1 回の値 (期間ごとの増え方を出す)
CREATE TABLE IF NOT EXISTS cumulative_history (
    room_id INTEGER, user_id INTEGER, at REAL, point INTEGER, visits INTEGER, PRIMARY KEY (user_id, room_id, at))
    WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS contributions (
    event_id INTEGER, room_id INTEGER, user_id INTEGER, point INTEGER, rank INTEGER,
    PRIMARY KEY (event_id, room_id, user_id)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS contributions_user ON contributions(user_id);
CREATE TABLE IF NOT EXISTS user_room (
    user_id INTEGER, room_id INTEGER, cumulative INTEGER DEFAULT 0, cumulative_rank INTEGER,
    visits INTEGER DEFAULT 0, events INTEGER DEFAULT 0, PRIMARY KEY (user_id, room_id)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS user_room_room ON user_room(room_id);
-- ギフターの月ごとのイベントの貢献ポイント (イベントが終わった月、日本時間)。貢献ランキングを保存するたびに足す
-- (支出の増え減りを、全部の貢献ランキングを数え直さずに出すため)
CREATE TABLE IF NOT EXISTS user_month (
    user_id INTEGER, month TEXT, points INTEGER, PRIMARY KEY (user_id, month)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS user_month_month ON user_month(month);
CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, name TEXT);
-- 前に使っていた名前 (名前を変えた時・退会した時に前の名前を残す。Internet Archive の古いページからも足す)
CREATE TABLE IF NOT EXISTS user_names (
    user_id INTEGER, name TEXT, at REAL, source TEXT, PRIMARY KEY (user_id, name)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS user_names_name ON user_names(name);
CREATE INDEX IF NOT EXISTS events_key ON events(key);
CREATE INDEX IF NOT EXISTS users_name ON users(name);
-- Internet Archive に残っている SHOWROOM のページ (消えたルーム・退会したユーザーの情報を取り戻すため)
CREATE TABLE IF NOT EXISTS archive_pages (
    page TEXT PRIMARY KEY, kind TEXT, event_key TEXT, room_id INTEGER, url TEXT, stamps TEXT,
    tries INTEGER DEFAULT 0, fetched_at REAL, rows INTEGER);
CREATE INDEX IF NOT EXISTS archive_pages_todo ON archive_pages(kind, fetched_at);
-- Internet Archive の貢献ランキング (ユーザー ID は載っていない。突き合わせて分かった人だけ user_id を入れる)
CREATE TABLE IF NOT EXISTS archive_contributions (
    event_id INTEGER, room_id INTEGER, rank INTEGER, name TEXT, point INTEGER, user_id INTEGER, at REAL,
    PRIMARY KEY (event_id, room_id, rank, name)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
-- ルームの 1 日ごとの様子 (配信中のルームの一覧から。フォロワー・配信の数・視聴者数)。ROOM_DAILY_KEEP 日で消す
CREATE TABLE IF NOT EXISTS room_daily (
    room_id INTEGER, day TEXT, followers INTEGER, lives INTEGER DEFAULT 0, view_max INTEGER, view_sum INTEGER DEFAULT 0,
    view_n INTEGER DEFAULT 0, last_start REAL, PRIMARY KEY (room_id, day)) WITHOUT ROWID;
-- 終わったイベントの、ルームごとの走り方 (ポイントの推移から。room_analysis が作る)
CREATE TABLE IF NOT EXISTS room_event_style (
    event_id INTEGER, room_id INTEGER, style TEXT, early REAL, mid REAL, late REAL, final REAL, last_day_share REAL,
    climb REAL, lead_share REAL, PRIMARY KEY (event_id, room_id)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS room_event_style_room ON room_event_style(room_id);
-- ルームのタイプ・勢い・戦術 (room_analysis が 1 日 1 回作り直す。ルームの一覧の絞り込みに使う)
CREATE TABLE IF NOT EXISTS room_profile (
    room_id INTEGER PRIMARY KEY, at REAL, events INTEGER, size_class INTEGER, top1 REAL, top3 REAL, contributors REAL,
    capped INTEGER, rel_top10 REAL, rel_per_contrib REAL, rel_point REAL, type TEXT,
    momentum REAL, recent_place REAL, prev_place REAL, style TEXT, style_events INTEGER, style_counts TEXT);
"""

MONTH_SQL = "strftime('%Y-%m', e.end + 9 * 3600, 'unixepoch')"  # イベントが終わった月 (日本時間)
USER_MONTH_STEP = 2000  # user_month を前に集めた分から作るとき、1 回に足すイベントの数
HISTORY_EVERY = 25 * 86400  # 累計ランキングの値を残す間隔 (ルームごと。月ごとの推移に使う)
ROOM_STATS_STEP = 100  # 貢献ランキングの数字 (c_*) を前に集めた分から作るとき、1 回に作るイベントの数
ROOM_DAILY_KEEP = 400  # room_daily を残す日数
DAY_SQL = "strftime('%Y-%m-%d', ? + 9 * 3600, 'unixepoch')"  # 日本時間の日付


CONTRIB_FULL = 100  # 貢献ランキングに載る人数 (この順位の人のポイントを、ファンの層の厚さに使う)


def contribution_stats(points: Iterable[Any]) -> tuple[int, int, int, int, int, int]:
    """貢献ランキングの (人数, 合計, 1 位, 上位 3 人, 上位 10 人, 100 位) のポイント (ルームのタイプ分けに使う)。

    100 位のポイントは、100 人に届かなければ 0 (101 人目以降は誰も送っていないのと同じ扱い)。
    """
    pts = sorted((int(p or 0) for p in points), reverse=True)
    p100 = pts[CONTRIB_FULL - 1] if len(pts) >= CONTRIB_FULL else 0
    return len(pts), sum(pts), sum(pts[:1]), sum(pts[:3]), sum(pts[:10]), p100


def median(values: Iterable[Any]) -> float | None:
    v = sorted(x for x in values if x is not None)
    if not v:
        return None
    m = len(v) // 2
    return float(v[m]) if len(v) % 2 else (v[m - 1] + v[m]) / 2

_ready: set[str] = set()
_ready_lock = threading.Lock()


def db_path(state_dir: Path) -> Path:
    return Path(state_dir) / DB_NAME


class Store:
    """research.db への接続 1 本。スレッドごとに作って使う (with 文で閉じる)。"""

    def __init__(self, state_dir: Path):
        self.state_dir = Path(state_dir)
        path = db_path(self.state_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), timeout=60, isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout=60000")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        with _ready_lock:
            if str(path) not in _ready:
                self.conn.execute("PRAGMA journal_mode=WAL")
                self.conn.executescript(SCHEMA)
                for table, col in (("rooms", "retry_at REAL"), ("rooms", "history_at REAL"), ("events", "retry_at REAL"), ("events", "progress_at REAL"),
                                   ("room_events", "point INTEGER"), ("room_events", "rank INTEGER"),
                                   ("rooms", "gone INTEGER"), ("events", "room_count INTEGER"),
                                   ("events", "median_point INTEGER"), ("events", "style_at REAL"),
                                   ("room_events", "c_count INTEGER"), ("room_events", "c_sum INTEGER"),
                                   ("room_events", "c_top1 INTEGER"), ("room_events", "c_top3 INTEGER"),
                                   ("room_events", "c_top10 INTEGER"), ("contributions", "guessed INTEGER"),
                                   ("room_events", "c_p100 INTEGER"), ("room_profile", "depth_pt REAL"),
                                   ("room_profile", "depth_rel REAL"), ("room_profile", "depth TEXT")):
                    cols = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}
                    if col.split()[0] not in cols:  # この欄が無かったころの research.db
                        self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col}")
                        if (table, col.split()[0]) == ("room_events", "c_p100"):
                            # 100 位のポイントを、前に集めた分からも作り直す (build_room_stats が少しずつ)
                            self.conn.execute("DELETE FROM meta WHERE k='room_stats_upto'")
                _import_json(self)
                _mark_guessed(self)
                _ready.add(str(path))

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        self.conn.execute("COMMIT")

    def one(self, sql: str, args: Iterable[Any] = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, tuple(args)).fetchone()

    def all(self, sql: str, args: Iterable[Any] = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, tuple(args)).fetchall()

    def scalar(self, sql: str, args: Iterable[Any] = (), default: Any = 0) -> Any:
        row = self.one(sql, args)
        return default if row is None or row[0] is None else row[0]

    # ---------------------------------------------------------------- meta
    def meta(self, k: str, default: Any = None) -> Any:
        row = self.one("SELECT v FROM meta WHERE k=?", (k,))
        return default if row is None else json.loads(row[0])

    def set_meta(self, k: str, v: Any) -> None:
        self.conn.execute("INSERT INTO meta(k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                          (k, json.dumps(v)))

    # ---------------------------------------------------------------- ルーム・イベント
    def upsert_rooms(self, rooms: Iterable[dict[str, Any]], via: str, now: float) -> int:
        """ルームを登録する (分かっている項目だけ上書き)。戻り値は新しく見つかったルームの数。"""
        before = self.scalar("SELECT COUNT(*) FROM rooms")
        with self.tx() as c:
            c.executemany(
                """INSERT INTO rooms(room_id, key, name, followers, found_via, found_at, live_at)
                   VALUES (:room_id, :key, :name, :followers, :via, :now, :live_at)
                   ON CONFLICT(room_id) DO UPDATE SET key=COALESCE(excluded.key, key), name=COALESCE(excluded.name, name),
                     followers=COALESCE(excluded.followers, followers),
                     live_at=CASE WHEN excluded.live_at > COALESCE(live_at, 0) THEN excluded.live_at ELSE live_at END""",
                [{"room_id": int(r["room_id"]), "key": r.get("key"), "name": r.get("name"), "followers": r.get("followers"),
                  "live_at": r.get("live_at"), "via": via, "now": now} for r in rooms if r.get("room_id")])
        return self.scalar("SELECT COUNT(*) FROM rooms") - before

    def room(self, key: str | None = None, room_id: int | None = None) -> sqlite3.Row | None:
        if room_id is not None:
            return self.one("SELECT * FROM rooms WHERE room_id=?", (int(room_id),))
        return self.one("SELECT * FROM rooms WHERE key=? ORDER BY room_id LIMIT 1", (key,))

    def upsert_events(self, events: Iterable[dict[str, Any]]) -> None:
        with self.tx() as c:
            c.executemany(
                """INSERT INTO events(event_id, key, name, start, end, type, show_ranking)
                   VALUES (:event_id, :key, :name, :start, :end, :type, COALESCE(:show_ranking, 1))
                   ON CONFLICT(event_id) DO UPDATE SET key=COALESCE(excluded.key, key), name=COALESCE(excluded.name, name),
                     start=COALESCE(excluded.start, start), end=COALESCE(excluded.end, end),
                     type=COALESCE(excluded.type, type), show_ranking=COALESCE(:show_ranking, show_ranking)""",
                [{"key": None, "name": None, "start": None, "end": None, "type": None, **e,
                  "show_ranking": None if e.get("show_ranking") is None else int(bool(e["show_ranking"]))}
                 for e in events if e.get("event_id")])

    def link(self, pairs: Iterable[tuple[int, int]]) -> None:
        """ルームとイベント (参加したもの) を結び付ける。"""
        with self.tx() as c:
            c.executemany("INSERT OR IGNORE INTO room_events(room_id, event_id) VALUES (?, ?)",
                          [(int(r), int(e)) for r, e in pairs])

    def room_events(self, room_id: int) -> list[sqlite3.Row]:
        return self.all("""SELECT e.*, re.ranking_at, re.final, re.failures FROM room_events re
                           JOIN events e USING(event_id) WHERE re.room_id=? ORDER BY COALESCE(e.end, 0) DESC""",
                        (int(room_id),))

    # ---------------------------------------------------------------- ランキング
    def _names(self, c: sqlite3.Connection, rows: list[dict[str, Any]], now: float | None = None) -> None:
        named = [(r["user_id"], r["name"]) for r in rows if r.get("name")]
        # 名前が変わった (退会して「退会済みユーザー」になった時も) なら、前の名前を残す
        c.executemany("""INSERT OR IGNORE INTO user_names(user_id, name, at, source)
                         SELECT user_id, name, ?, 'showroom' FROM users WHERE user_id=? AND name IS NOT NULL AND name != ?""",
                      [(now, u, n) for u, n in named])
        c.executemany("INSERT INTO users(user_id, name) VALUES (?, ?) ON CONFLICT(user_id) DO UPDATE SET name=excluded.name",
                      named)

    def set_cumulative(self, room_id: int, rows: list[dict[str, Any]], now: float) -> None:
        """ルームの累計ランキング (上位 30 人) を置き換える。user_room の累計は小さくしない (上位から外れても送った分は残る)。"""
        room_id = int(room_id)
        with self.tx() as c:
            last = c.execute("SELECT history_at FROM rooms WHERE room_id=?", (room_id,)).fetchone()
            if rows and (last is None or last[0] is None or now - last[0] >= HISTORY_EVERY):
                c.executemany("INSERT OR IGNORE INTO cumulative_history(room_id, user_id, at, point, visits) VALUES (?, ?, ?, ?, ?)",
                              [(room_id, r["user_id"], now, r["point"], r["visits"]) for r in rows])
                c.execute("UPDATE rooms SET history_at=? WHERE room_id=?", (now, room_id))
            c.execute("DELETE FROM cumulative WHERE room_id=?", (room_id,))
            c.executemany("INSERT OR REPLACE INTO cumulative(room_id, user_id, point, visits, rank) VALUES (?, ?, ?, ?, ?)",
                          [(room_id, r["user_id"], r["point"], r["visits"], r.get("rank")) for r in rows])
            c.executemany(
                """INSERT INTO user_room(user_id, room_id, cumulative, cumulative_rank, visits) VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(user_id, room_id) DO UPDATE SET cumulative=MAX(cumulative, excluded.cumulative),
                     cumulative_rank=excluded.cumulative_rank, visits=MAX(visits, excluded.visits)""",
                [(r["user_id"], room_id, r["point"], r.get("rank"), r["visits"]) for r in rows])
            self._names(c, rows, now)

    def set_contributions(self, event_id: int, room_id: int, rows: list[dict[str, Any]], now: float, final: bool) -> None:
        """イベントの貢献ランキングを置き換える (取り直したときは前の分を user_room から引いてから足す)。"""
        event_id, room_id = int(event_id), int(room_id)
        with self.tx() as c:
            c.execute("""UPDATE user_room SET events = events - (SELECT point FROM contributions o
                           WHERE o.event_id=? AND o.room_id=? AND o.user_id=user_room.user_id)
                         WHERE room_id=? AND user_id IN (SELECT user_id FROM contributions WHERE event_id=? AND room_id=?)""",
                      (event_id, room_id, room_id, event_id, room_id))
            months = self._user_month_ready(c, event_id)
            if months:
                c.execute(f"""INSERT INTO user_month(user_id, month, points)
                               SELECT o.user_id, {MONTH_SQL}, -o.point FROM contributions o JOIN events e USING(event_id)
                               WHERE o.event_id=? AND o.room_id=? AND e.end IS NOT NULL
                               ON CONFLICT(user_id, month) DO UPDATE SET points = points + excluded.points""", (event_id, room_id))
            c.execute("DELETE FROM contributions WHERE event_id=? AND room_id=?", (event_id, room_id))
            c.executemany("""INSERT OR REPLACE INTO contributions(event_id, room_id, user_id, point, rank, guessed)
                             VALUES (?, ?, ?, ?, ?, ?)""",
                          [(event_id, room_id, r["user_id"], r["point"], r.get("rank"), 1 if r.get("guessed") else None) for r in rows])
            c.executemany("""INSERT INTO user_room(user_id, room_id, events) VALUES (?, ?, ?)
                             ON CONFLICT(user_id, room_id) DO UPDATE SET events = events + excluded.events""",
                          [(r["user_id"], room_id, r["point"]) for r in rows])
            if months:
                c.execute(f"""INSERT INTO user_month(user_id, month, points)
                               SELECT o.user_id, {MONTH_SQL}, o.point FROM contributions o JOIN events e USING(event_id)
                               WHERE o.event_id=? AND o.room_id=? AND e.end IS NOT NULL
                               ON CONFLICT(user_id, month) DO UPDATE SET points = points + excluded.points""", (event_id, room_id))
            # 名前で推測した分 (Internet Archive) は、分かった人だけの一部なので、ルームのタイプ分けの数字は作らない
            stats = (None,) * 6 if any(r.get("guessed") for r in rows) else contribution_stats(r["point"] for r in rows)
            c.execute("""INSERT INTO room_events(room_id, event_id, ranking_at, final, c_count, c_sum, c_top1, c_top3, c_top10, c_p100)
                         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                         ON CONFLICT(room_id, event_id) DO UPDATE SET ranking_at=excluded.ranking_at, final=excluded.final,
                           failures=0, c_count=excluded.c_count, c_sum=excluded.c_sum, c_top1=excluded.c_top1,
                           c_top3=excluded.c_top3, c_top10=excluded.c_top10, c_p100=excluded.c_p100""", (room_id, event_id, now, int(final), *stats))
            self._names(c, rows, now)

    # ---------------------------------------------------------------- 月ごとの集計 (user_month)
    def _user_month_ready(self, c: sqlite3.Connection, event_id: int) -> bool:
        """このイベントの分が、もう user_month に入っているか (入っていれば、取り直しのたびに足し引きする)。"""
        row = c.execute("SELECT v FROM meta WHERE k='user_month_upto'").fetchone()
        return row is not None and (json.loads(row[0]) is True or event_id <= json.loads(row[0]))

    def build_user_month(self, step: int = USER_MONTH_STEP) -> bool:
        """前に集めた貢献ランキングを、user_month に少しずつ足す (イベント番号の小さい順)。

        戻り値は、まだ残りがあるか。全部足し終えると meta の user_month_upto が true になり、
        そのあとは set_contributions が保存のたびに足す。
        """
        with self.tx() as c:
            row = c.execute("SELECT v FROM meta WHERE k='user_month_upto'").fetchone()
            upto = 0 if row is None else json.loads(row[0])
            if upto is True:
                return False
            ids = [r[0] for r in c.execute("SELECT DISTINCT event_id FROM contributions WHERE event_id > ? ORDER BY event_id LIMIT ?",
                                           (upto, step))]
            if ids:
                c.execute(f"""INSERT INTO user_month(user_id, month, points)
                               SELECT o.user_id, {MONTH_SQL} AS m, SUM(o.point) FROM contributions o JOIN events e USING(event_id)
                               WHERE o.event_id > ? AND o.event_id <= ? AND e.end IS NOT NULL GROUP BY o.user_id, m
                               ON CONFLICT(user_id, month) DO UPDATE SET points = points + excluded.points""", (upto, ids[-1]))
            done = len(ids) < step
            c.execute("INSERT INTO meta(k, v) VALUES ('user_month_upto', ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                      (json.dumps(True if done else ids[-1]),))
            return not done

    def set_event_rooms(self, event_id: int, rows: list[dict[str, Any]], now: float) -> None:
        """イベントの参加ルームと、各ルームのイベントのポイント・順位を入れる。"""
        event_id = int(event_id)
        self.upsert_rooms(rows, "event", now)
        with self.tx() as c:
            if rows:  # 参加ルームの無い番号 (使われていない番号など) は入れない
                c.execute("INSERT OR IGNORE INTO events(event_id) VALUES (?)", (event_id,))
            c.executemany("""INSERT INTO room_events(room_id, event_id, point, rank) VALUES (?, ?, ?, ?)
                             ON CONFLICT(room_id, event_id) DO UPDATE SET point=excluded.point, rank=excluded.rank""",
                          [(int(r["room_id"]), event_id, r.get("point"), r.get("rank")) for r in rows if r.get("room_id")])
            c.execute("UPDATE events SET rooms_at=?, retry_at=NULL WHERE event_id=?", (now, event_id))
            if rows:
                c.execute("UPDATE events SET room_count=?, median_point=? WHERE event_id=?",
                          (len(rows), _median_point(r.get("point") for r in rows), event_id))

    def build_room_stats(self, step: int = ROOM_STATS_STEP) -> bool:
        """前に集めたイベントの参加ルームの数・ポイントの真ん中と、貢献ランキングの数字 (c_*) を、少しずつ作る。

        新しく集めた分は set_event_rooms・set_contributions が作る。戻り値は、まだ残りがあるか。
        """
        upto = self.meta("room_stats_upto", 0)
        if upto is True:
            return False
        ids = [r[0] for r in self.all("SELECT event_id FROM events WHERE event_id > ? ORDER BY event_id LIMIT ?", (upto, step))]
        if ids:
            lo, hi = ids[0], ids[-1]
            points: dict[int, list[Any]] = {}
            for r in self.all("SELECT event_id, point FROM room_events WHERE event_id BETWEEN ? AND ?", (lo, hi)):
                points.setdefault(r[0], []).append(r[1])
            contrib: dict[tuple[int, int], list[int]] = {}
            guessed = set()
            for r in self.all("SELECT event_id, room_id, point, guessed FROM contributions WHERE event_id BETWEEN ? AND ?", (lo, hi)):
                contrib.setdefault((r[0], r[1]), []).append(r[2])
                if r[3]:
                    guessed.add((r[0], r[1]))
            for k in guessed:
                del contrib[k]
            with self.tx() as c:
                c.executemany("UPDATE events SET room_count=?, median_point=? WHERE event_id=?",
                              [(len(p), _median_point(p), e) for e, p in points.items()])
                c.executemany("""UPDATE room_events SET c_count=?, c_sum=?, c_top1=?, c_top3=?, c_top10=?, c_p100=?
                                 WHERE event_id=? AND room_id=?""",
                              [(*contribution_stats(p), e, r) for (e, r), p in contrib.items()])
        done = len(ids) < step
        self.set_meta("room_stats_upto", True if done else ids[-1])
        return not done

    def add_room_daily(self, lives: list[dict[str, Any]], now: float) -> None:
        """配信中のルームの一覧 (5 分ごと) から、ルームの 1 日ごとの様子を足す。"""
        rows = [(int(x["room_id"]), now, x.get("followers"), x.get("viewers"), x.get("started_at"))
                for x in lives if x.get("room_id")]
        with self.tx() as c:
            c.executemany(f"""INSERT INTO room_daily(room_id, day, followers, lives, view_max, view_sum, view_n, last_start)
                              VALUES (?1, {DAY_SQL.replace('?', '?2')}, ?3, 1, ?4, COALESCE(?4, 0), ?4 IS NOT NULL, ?5)
                              ON CONFLICT(room_id, day) DO UPDATE SET followers=COALESCE(excluded.followers, followers),
                                lives = lives + (COALESCE(excluded.last_start, 0) != COALESCE(last_start, 0)),
                                last_start=COALESCE(excluded.last_start, last_start),
                                view_max=MAX(COALESCE(view_max, 0), COALESCE(excluded.view_max, 0)),
                                view_sum = view_sum + excluded.view_sum, view_n = view_n + excluded.view_n""", rows)
            day = c.execute(f"SELECT {DAY_SQL}", (now,)).fetchone()[0]
            row = c.execute("SELECT v FROM meta WHERE k='room_daily_cleaned'").fetchone()
            if row is None or json.loads(row[0]) != day:  # 古い日の分は 1 日 1 回だけ消す
                c.execute(f"DELETE FROM room_daily WHERE day < {DAY_SQL}", (now - ROOM_DAILY_KEEP * 86400,))
                c.execute("INSERT INTO meta(k, v) VALUES ('room_daily_cleaned', ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                          (json.dumps(day),))

    # ---------------------------------------------------------------- 気になるギフター
    def set_watch(self, user_id: int, watched: bool, memo: str | None, now: float) -> None:
        """気になるギフターに入れる (メモつき)・外す。memo が None ならメモは変えない。"""
        uid = int(user_id)
        if not watched:
            self.conn.execute("DELETE FROM watch WHERE user_id=?", (uid,))
            return
        self.conn.execute("""INSERT INTO watch(user_id, memo, added_at, updated_at) VALUES (?, COALESCE(?, ''), ?, ?)
                             ON CONFLICT(user_id) DO UPDATE SET memo=COALESCE(?, memo), updated_at=excluded.updated_at""",
                          (uid, memo, now, now, memo))

    def watch_rows(self) -> list[sqlite3.Row]:
        return self.all("SELECT * FROM watch ORDER BY added_at DESC")

    def add_progress(self, event_id: int, rows: list[dict[str, Any]], now: float) -> int:
        """開催中のイベントの、今のルームごとのポイントと順位を残す (ポイントの推移)。戻り値は残したルームの数。"""
        event_id = int(event_id)
        keep = [(event_id, int(r["room_id"]), now, int(r["point"]), r.get("rank"))
                for r in rows if r.get("room_id") and r.get("point") is not None]
        with self.tx() as c:
            c.executemany("INSERT OR REPLACE INTO event_progress(event_id, room_id, at, point, rank) VALUES (?, ?, ?, ?, ?)", keep)
            c.execute("UPDATE events SET progress_at=? WHERE event_id=?", (now, event_id))
        return len(keep)

    def ranking_failed(self, event_id: int, room_id: int) -> None:
        self.conn.execute("""INSERT INTO room_events(room_id, event_id, failures) VALUES (?, ?, 1)
                             ON CONFLICT(room_id, event_id) DO UPDATE SET failures = failures + 1""",
                          (int(room_id), int(event_id)))


def _median_point(points: Iterable[Any]) -> int | None:
    m = median(p for p in points if p and p > 0)
    return None if m is None else int(m)


def _mark_guessed(store: Store) -> None:
    """前の版が Internet Archive から名前だけで決めて足した貢献ランキングに、「名前で推測」の印を 1 回だけ付ける。

    前の版は印を残していないので、貢献ランキングのページを読んだ時刻 (archive_pages.fetched_at) と、
    その直前に保存した時刻 (room_events.ranking_at) が同じ時のものを、名前で決めた分とみなす。
    """
    if store.meta("guessed_marked"):
        return
    with store.tx() as c:
        pairs = c.execute("""SELECT re.event_id, re.room_id FROM archive_pages p JOIN events e ON e.key = p.event_key
                               JOIN room_events re ON re.event_id = e.event_id AND re.room_id = p.room_id
                             WHERE p.kind = 'contribution' AND p.fetched_at BETWEEN re.ranking_at AND re.ranking_at + 120""").fetchall()
        c.executemany("UPDATE contributions SET guessed=1 WHERE event_id=? AND room_id=?", [tuple(x) for x in pairs])
        c.executemany("""UPDATE room_events SET c_count=NULL, c_sum=NULL, c_top1=NULL, c_top3=NULL, c_top10=NULL, c_p100=NULL
                         WHERE event_id=? AND room_id=?""", [tuple(x) for x in pairs])
        c.execute("INSERT INTO meta(k, v) VALUES ('guessed_marked', 'true') ON CONFLICT(k) DO UPDATE SET v=excluded.v")
    if pairs:
        log.info("名前で推測した貢献ランキングに印を付けました (%d ルーム・イベント)", len(pairs))


def _import_json(store: Store) -> None:
    """前の版が state/research/ に JSON で保存した情報を、1 回だけ取り込む。"""
    folder = store.state_dir / "research"
    if store.meta("json_imported") or not folder.is_dir():
        return

    def read(p: Path) -> Any:
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    rooms = events = 0
    for p in sorted((folder / "rooms").glob("*.json")):
        r = read(p)
        if not isinstance(r, dict) or not r.get("room_id"):
            continue
        store.upsert_rooms([{"room_id": r["room_id"], "key": r.get("key"), "name": r.get("name")}], "settings", r.get("at") or 0)
        store.conn.execute("UPDATE rooms SET collected_at=? WHERE room_id=?", (r.get("at"), int(r["room_id"])))
        store.set_cumulative(r["room_id"], [x for x in r.get("cumulative") or [] if x.get("user_id")], r.get("at") or 0)
        store.upsert_events([{k: e.get(k) for k in ("event_id", "key", "name", "start", "end", "type", "show_ranking")}
                             for e in r.get("events") or []])
        store.link((r["room_id"], e["event_id"]) for e in r.get("events") or [] if e.get("event_id"))
        rooms += 1
    for p in sorted((folder / "events").glob("*.json")):
        e = read(p)
        if not isinstance(e, dict) or not e.get("event_id") or not e.get("room_id"):
            continue
        store.set_contributions(e["event_id"], e["room_id"], [x for x in e.get("rows") or [] if x.get("user_id")],
                                e.get("at") or 0, bool(e.get("final")))
        events += 1
    store.set_meta("json_imported", True)
    if rooms or events:
        log.info("前の版で集めた情報を取り込みました (%d ルーム / 貢献ランキング %d 件)", rooms, events)
