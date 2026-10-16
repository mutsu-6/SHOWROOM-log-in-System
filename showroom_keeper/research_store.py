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
CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, name TEXT);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""

HISTORY_EVERY = 25 * 86400  # 累計ランキングの値を残す間隔 (ルームごと。月ごとの推移に使う)

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
                                   ("room_events", "point INTEGER"), ("room_events", "rank INTEGER")):
                    cols = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}
                    if col.split()[0] not in cols:  # この欄が無かったころの research.db
                        self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col}")
                _import_json(self)
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
    def _names(self, c: sqlite3.Connection, rows: list[dict[str, Any]]) -> None:
        c.executemany("INSERT INTO users(user_id, name) VALUES (?, ?) ON CONFLICT(user_id) DO UPDATE SET name=excluded.name",
                      [(r["user_id"], r["name"]) for r in rows if r.get("name")])

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
            self._names(c, rows)

    def set_contributions(self, event_id: int, room_id: int, rows: list[dict[str, Any]], now: float, final: bool) -> None:
        """イベントの貢献ランキングを置き換える (取り直したときは前の分を user_room から引いてから足す)。"""
        event_id, room_id = int(event_id), int(room_id)
        with self.tx() as c:
            c.execute("""UPDATE user_room SET events = events - (SELECT point FROM contributions o
                           WHERE o.event_id=? AND o.room_id=? AND o.user_id=user_room.user_id)
                         WHERE room_id=? AND user_id IN (SELECT user_id FROM contributions WHERE event_id=? AND room_id=?)""",
                      (event_id, room_id, room_id, event_id, room_id))
            c.execute("DELETE FROM contributions WHERE event_id=? AND room_id=?", (event_id, room_id))
            c.executemany("INSERT OR REPLACE INTO contributions(event_id, room_id, user_id, point, rank) VALUES (?, ?, ?, ?, ?)",
                          [(event_id, room_id, r["user_id"], r["point"], r.get("rank")) for r in rows])
            c.executemany("""INSERT INTO user_room(user_id, room_id, events) VALUES (?, ?, ?)
                             ON CONFLICT(user_id, room_id) DO UPDATE SET events = events + excluded.events""",
                          [(r["user_id"], room_id, r["point"]) for r in rows])
            c.execute("""INSERT INTO room_events(room_id, event_id, ranking_at, final) VALUES (?, ?, ?, ?)
                         ON CONFLICT(room_id, event_id) DO UPDATE SET ranking_at=excluded.ranking_at, final=excluded.final,
                           failures=0""", (room_id, event_id, now, int(final)))
            self._names(c, rows)

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
