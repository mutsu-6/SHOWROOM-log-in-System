"""応援しているルームへギフターを誘導する作戦のための調査 (情報収集と分析)。

集めるもの (どれも SHOWROOM が公開している情報):
- ルームの累計ポイントランキング (上位 30 人の累計ポイントと来場回数) /api/live/summary_ranking
- ルームの過去のイベント /api/rooms/{room_id}/past_events と、開催中のイベント /api/room/event_and_support
- イベントごとの貢献ランキング (ルームごとに上位 100 人) /api/event/contribution_ranking
  (2015 年ごろのイベントまで返る。ギフトの量の移り変わりを見るため、古いイベントも取る)
- 累計ランキングの値をルームごとにおよそ月 1 回残す (cumulative_history。期間ごとの増え方を出す)

問い合わせは 1 件ずつ間隔を空けて行い (Throttle。全ルームの収集 sweep.py とも共有する)、
SHOWROOM が混んでいる・止められていそうなときは間隔を自動で広げる。
終わったイベントの貢献ランキングは一度取ったら取り直さない。集めた情報は research_store (SQLite) に入れる。

分析すること:
- ギフターの行動のくせ (記録したギフトから: よく送る曜日・時間帯、1 回あたりのポイント、送るルームの偏り)
- イベントの貢献ランキングでの実績 (どのイベントで、どのルームに、何ポイント)
- 応援ルームとギフターが重なるルーム (似ているルーム) と、そこから誘導できそうなギフター (誘導の候補)
"""

from __future__ import annotations

import logging
import math
import threading
import time
import urllib.error
from bisect import bisect_left
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .research_store import MONTH_SQL, Store, db_path
from .status import JST

log = logging.getLogger("research")

REQUEST_GAP = 1.0  # SHOWROOM への問い合わせの間隔 (秒)
MAX_GAP = 60.0  # 混んでいるときに広げる間隔の上限 (秒)
PAUSE_AFTER = 5  # 続けてこの回数失敗したら、しばらく問い合わせを止める
PAUSE = 3600.0  # 1 回目に止める時間 (秒)。続けて止めるたびに倍にする
MAX_PAUSE = 24 * 3600.0  # 止める時間の上限 (秒)
RECOVER_AFTER = 200  # 再開したあと、続けてこの回数成功したら、止める時間を 1 回目の長さに戻す
PAUSE_LOG = 20  # 覚えておく、止めた記録の数
PAST_EVENT_PAGES = 100  # 過去のイベント一覧を読むページ数の上限 (1 ページ約 30 件)
ROOM_REFRESH = 20 * 3600  # 累計ランキングなどを取り直すまでの時間 (秒)
RANKING_RETRIES = 3  # 貢献ランキングの取得に続けて失敗したら、それ以上は自動で取り直さない
ARCHIVE_HOLD = RANKING_RETRIES + 1  # Internet Archive で見つけたルームの貢献ランキング: ルームがあると分かるまで取りに行かない
WEEKDAYS = "月火水木金土日"
LEFT_NAMES = {"退会済みユーザー"}  # SHOWROOM をやめたユーザー (誘導できないので候補に出さない)
ANALYSIS_TTL = 600  # 分析の結果を使い回す時間 (秒)。全ルーム分の集計は重いので、そのたびには数え直さない


# ------------------------------------------------------------------ 問い合わせの間隔
class Throttle:
    """SHOWROOM への問い合わせを 1 本ずつ、間隔を空けて流す (スレッドをまたいで共有する)。

    403 / 429 / 5xx / 通信の失敗が続いたら間隔を倍に広げ (MAX_GAP まで)、PAUSE_AFTER 回続いたら止める。
    止める時間は 1 時間・2 時間・4 時間… と、続けて止めるたびに倍にする (MAX_PAUSE まで)。再開したあと
    RECOVER_AFTER 回続けて成功したら 1 時間に戻す (SHOWROOM に止められている時に、何度も試さないように)。
    成功するたびに間隔を少しずつ元に戻す。404 (無いルーム・ランキング) は失敗に数えない。
    """

    def __init__(self, gap: float = REQUEST_GAP, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic, wall: Callable[[], float] = time.time):
        self.base = self.gap = float(gap)
        self._sleep, self._clock, self._wall = sleep, clock, wall
        self._lock = threading.Lock()
        self._next = 0.0
        self.errors = 0
        self.paused_until = 0.0
        self.requests = 0
        self.pauses = 0  # 続けて止めた回数 (RECOVER_AFTER 回成功すると 0 に戻る)
        self.ok_streak = 0
        self.pause_log: list[dict[str, Any]] = []  # 止めた記録 (新しい順。管理画面に出す)

    def set_base(self, gap: float) -> None:
        self.base = max(float(gap), 0.2)
        self.gap = max(self.gap, self.base)

    def state(self) -> dict[str, Any]:
        left = self.paused_until - self._clock()
        return {"gap": round(self.gap, 2), "base": self.base, "errors": self.errors,
                "paused": round(left) if left > 0 else 0, "requests": self.requests,
                "pauses": self.pauses, "next_pause": round(self._pause_length(self.pauses + 1)),
                "pause_log": list(self.pause_log)}

    @staticmethod
    def _pause_length(n: int) -> float:
        return min(MAX_PAUSE, PAUSE * 2 ** max(n - 1, 0))

    def call(self, get: Callable[[str], Any], path: str) -> Any:
        with self._lock:
            now = self._clock()
            wait = max(self._next, self.paused_until) - now
            if wait > 0:
                self._sleep(wait)
            self.requests += 1
            try:
                data = get(path)
            except urllib.error.HTTPError as e:
                if e.code in (403, 429) or e.code >= 500:  # 403 は SHOWROOM 側に止められているとき
                    self._failed(f"HTTP {e.code}")
                else:
                    self._ok()
                raise
            except Exception as e:  # noqa: BLE001 - 通信の失敗 (タイムアウトなど)
                self._failed(str(e).splitlines()[0] if str(e) else type(e).__name__)
                raise
            else:
                self._ok()
                return data
            finally:
                self._next = self._clock() + self.gap

    def _ok(self) -> None:
        self.errors = 0
        self.gap = max(self.base, self.gap * 0.8)
        self.ok_streak += 1
        if self.pauses and self.ok_streak >= RECOVER_AFTER:
            log.info("SHOWROOM への問い合わせが続けて成功したので、次に止める時間を %d 分に戻します", PAUSE // 60)
            self.pauses = 0

    def _failed(self, why: str) -> None:
        self.errors += 1
        self.ok_streak = 0
        self.gap = min(MAX_GAP, max(self.gap * 2, self.base))
        if self.errors >= PAUSE_AFTER:
            self.pauses += 1
            length = self._pause_length(self.pauses)
            self.paused_until = self._clock() + length
            self.errors = 0
            now = self._wall()
            self.pause_log = [{"at": now, "until": now + length, "seconds": length, "count": self.pauses, "why": why},
                              *self.pause_log][:PAUSE_LOG]
            text = f"{length / 3600:g} 時間" if length >= 3600 else f"{length / 60:.0f} 分"
            log.warning("SHOWROOM への問い合わせが続けて失敗したので、全ルームの収集などを %s止めます (続けて %d 回目。%s)。"
                        "視聴とログインの維持は続けます", text, self.pauses, why)


THROTTLE = Throttle()


def is_missing(e: BaseException) -> bool:
    return isinstance(e, urllib.error.HTTPError) and e.code == 404


# ------------------------------------------------------------------ 情報収集
def _event_info(e: dict[str, Any]) -> dict[str, Any]:
    url = e.get("event_url") or ""
    key = e.get("event_url_key") or (url.rstrip("/").rsplit("/", 1)[-1] if url else None)
    # 分からない項目は None (ほかの取り方で分かった名前や「順位を公開しない」を上書きしない)
    return {"event_id": e.get("event_id"), "name": e.get("event_name") or None, "key": key,
            "start": e.get("started_at"), "end": e.get("ended_at"), "type": e.get("event_type"),
            "show_ranking": None if e.get("show_ranking") is None else bool(e["show_ranking"])}


def _rows(data: Any, cumulative: bool = False) -> list[dict[str, Any]]:
    out = []
    for r in (data or {}).get("ranking") or []:
        if isinstance(r, dict) and r.get("user_id"):
            row = {"user_id": int(r["user_id"]), "name": r.get("name"), "point": int(r.get("point") or 0), "rank": r.get("rank")}
            if cumulative:
                row["visits"] = int(r.get("visit_count") or 0)
            out.append(row)
    return out


class Collector:
    """ルームの累計ランキング・イベント・貢献ランキングを集めて research.db に入れる。

    1 つのスレッドの中で使う (research.db への接続をスレッドごとに持つため)。
    """

    def __init__(self, state_dir: Path, get: Callable[[str], Any] | None = None,
                 throttle: Throttle | None = None, clock: Callable[[], float] = time.time):
        from .gifts import _get

        self.state_dir = Path(state_dir)
        self._get = get or _get
        self.throttle = throttle or THROTTLE
        self._clock = clock
        self._store: Store | None = None
        self.requests = 0
        self.failures = 0

    @property
    def store(self) -> Store:
        if self._store is None:
            self._store = Store(self.state_dir)
        return self._store

    def close(self) -> None:
        if self._store is not None:
            self._store.close()
            self._store = None

    def get(self, path: str) -> Any:
        self.requests += 1
        return self.throttle.call(self._get, path)

    def collect(self, keys: list[str], force: bool = False,
                progress: Callable[[str], None] | None = None) -> dict[str, Any]:
        """keys のルームを順に集める。戻り値は {rooms, events, requests, failures}。"""
        rooms = events = 0
        keys = list(dict.fromkeys(keys))
        try:
            for i, key in enumerate(keys):
                if progress:
                    progress(f"{i + 1}/{len(keys)} {key}")
                try:
                    added = self.collect_room(key, force)
                except Exception as e:  # noqa: BLE001 - 1 ルームの失敗で全体を止めない
                    self.failures += 1
                    log.warning("%s の情報を集められませんでした: %s", key, str(e).splitlines()[0] if str(e) else e)
                    continue
                rooms += 1
                events += added
        finally:
            self.close()
        return {"rooms": rooms, "events": events, "requests": self.requests, "failures": self.failures}

    def resolve(self, key: str) -> int:
        """ルームの URL キーから room_id を調べる (分かっていれば問い合わせない)。"""
        from .listing import remember_rooms, room_names

        row = self.store.room(key=key)
        if row:
            return int(row["room_id"])
        info = room_names(self.state_dir).get(key) or {}
        room_id, name = info.get("room_id"), info.get("name")
        if not room_id:
            st = self.get(f"/api/room/status?room_url_key={key}")
            room_id, name = st.get("room_id"), st.get("room_name") or key
            if not room_id:
                raise ValueError("ルームが見つかりません")
            remember_rooms(self.state_dir, {key: (name, room_id)})
        self.store.upsert_rooms([{"room_id": room_id, "key": key, "name": name or key}], "settings", self._clock())
        return int(room_id)

    def collect_room(self, key: str, force: bool = False) -> int:
        """指定のルーム 1 つ分を集める (開催中のイベントの貢献ランキングも)。戻り値は新しく取った貢献ランキングの数。"""
        room_id = self.resolve(key)
        row = self.store.room(room_id=room_id)
        fresh = not force and row["collected_at"] and self._clock() - float(row["collected_at"]) < ROOM_REFRESH
        if not fresh:
            self.refresh_room(room_id)
        return self.fetch_rankings(room_id, force=force, current=True)

    def refresh_room(self, room_id: int) -> None:
        """累計ランキング・過去と開催中のイベントを取り直す。"""
        now = self._clock()
        st = self.store
        st.set_cumulative(room_id, _rows(self.get(f"/api/live/summary_ranking?room_id={room_id}"), cumulative=True), now)
        row = st.room(room_id=room_id)
        complete = row is not None and row["collected_at"] is not None  # 前回は最後まで読めた
        known = {e["event_id"] for e in st.room_events(room_id)}
        events: list[dict[str, Any]] = []
        page: Any = 1
        while page and page <= PAST_EVENT_PAGES:
            data = self.get(f"/api/rooms/{room_id}/past_events?page={page}") or {}
            got = [_event_info(e) for e in data.get("events") or [] if isinstance(e, dict) and e.get("event_id")]
            events += got
            if not got or (complete and all(e["event_id"] in known for e in got)):
                break  # 新しい順なので、ここから先は前に読んだもの
            from .recorder import next_page_number

            page = next_page_number(page, data.get("next_page"))  # 番号が進まない時は、同じページを読み続けないようにやめる
        current = (self.get(f"/api/room/event_and_support?room_id={room_id}") or {}).get("event")
        if isinstance(current, dict) and current.get("event_id"):
            events.append(_event_info(current))
        st.upsert_events(events)
        st.link((room_id, e["event_id"]) for e in events)
        st.conn.execute("UPDATE rooms SET collected_at=? WHERE room_id=?", (now, int(room_id)))

    def fetch_rankings(self, room_id: int, force: bool = False, current: bool = False) -> int:
        """ルームが参加したイベントの貢献ランキングを取る。current なら開催中のものも (取り直しも) 取る。"""
        now = self._clock()
        added = 0
        for ev in self.store.room_events(room_id):
            if not ev["show_ranking"] or ev["final"]:
                continue
            if ev["start"] and float(ev["start"]) > now:
                continue
            ended = bool(ev["end"]) and now > float(ev["end"]) + 3600
            if not ended and not current:
                continue
            if not force and ev["ranking_at"] and now - float(ev["ranking_at"]) < ROOM_REFRESH:
                continue
            if not force and ev["failures"] >= RANKING_RETRIES:
                continue
            if self.fetch_ranking(ev["event_id"], room_id, final=ended):
                added += 1
        return added

    def fetch_ranking(self, event_id: int, room_id: int, final: bool) -> bool:
        """1 つのイベント・ルームの貢献ランキングを取る。失敗しても例外にせず False (ほかのイベントは続ける)。"""
        try:
            data = self.get(f"/api/event/contribution_ranking?event_id={event_id}&room_id={room_id}")
        except Exception as e:  # noqa: BLE001
            if not is_missing(e):
                self.failures += 1
                self.store.ranking_failed(event_id, room_id)
                log.warning("イベント %s (ルーム %s) の貢献ランキングを取れませんでした: %s", event_id, room_id,
                            str(e).splitlines()[0] if str(e) else type(e).__name__)
                return False
            data = {}
        ev = (data or {}).get("event")
        if isinstance(ev, dict) and ev.get("ended_at"):
            # 番号で読んだ古いイベントは、ここで初めて日付が分かる (「順位を公開しない」はここでは見ない)
            self.store.upsert_events([{**_event_info({**ev, "event_id": event_id}), "show_ranking": None}])
        rows = _rows(data)
        if not rows and self.store.one("SELECT 1 FROM contributions WHERE event_id=? AND room_id=? AND guessed IS NOT NULL",
                                       (event_id, room_id)):
            # SHOWROOM に残っていなかった: 名前で推測した分 (Internet Archive) を消さずに残す
            self.store.conn.execute("UPDATE room_events SET final=1, ranking_at=?, failures=0 WHERE event_id=? AND room_id=?",
                                    (self._clock(), event_id, room_id))
            return True
        self.store.set_contributions(event_id, room_id, rows, self._clock(), final)
        return True


# ------------------------------------------------------------------ 集めた情報の読み込み
def _event_dict(e: Any, now: float) -> dict[str, Any]:
    current = bool(e["start"] and e["end"] and float(e["start"]) <= now <= float(e["end"]))
    return {"event_id": e["event_id"], "name": e["name"] or str(e["event_id"]), "key": e["key"], "start": e["start"], "end": e["end"],
            "type": e["type"], "show_ranking": bool(e["show_ranking"]), "current": current}


def load_rooms(state_dir: Path, keys: list[str] | None = None) -> dict[str, dict[str, Any]]:
    """指定のルームの集めた情報 ({key: {key, room_id, name, at, cumulative, events}})。"""
    now = time.time()
    out = {}
    with Store(state_dir) as s:
        rows = ([s.room(key=k) for k in dict.fromkeys(keys)] if keys is not None
                else s.all("SELECT * FROM rooms WHERE collected_at IS NOT NULL AND key IS NOT NULL"))
        for r in rows:
            if r is None:
                continue
            cumulative = [dict(x) for x in s.all(
                """SELECT c.user_id, n.name, c.point, c.visits, c.rank FROM cumulative c LEFT JOIN users n USING(user_id)
                   WHERE c.room_id=? ORDER BY c.point DESC""", (r["room_id"],))]
            events = sorted((_event_dict(e, now) for e in s.room_events(r["room_id"])), key=lambda e: not e["current"])
            out[r["key"]] = {"key": r["key"], "room_id": r["room_id"], "name": r["name"] or r["key"], "at": r["collected_at"],
                             "cumulative": cumulative, "events": events}
    return out


def status(state_dir: Path, keys: list[str]) -> list[dict[str, Any]]:
    """指定のルームごとの収集の状況 (管理画面用)。"""
    out = []
    with Store(state_dir) as s:
        for key in dict.fromkeys(keys):
            r = s.room(key=key)
            if r is None:
                out.append({"key": key, "name": key, "at": None, "cumulative": 0, "events": 0, "rankings": 0})
                continue
            rid = r["room_id"]
            out.append({"key": key, "name": r["name"] or key, "at": r["collected_at"],
                        "cumulative": s.scalar("SELECT COUNT(*) FROM cumulative WHERE room_id=?", (rid,)),
                        "events": s.scalar("SELECT COUNT(*) FROM room_events WHERE room_id=?", (rid,)),
                        "rankings": s.scalar("SELECT COUNT(*) FROM room_events WHERE room_id=? AND ranking_at IS NOT NULL", (rid,))})
    return out


ANCHOR_STEPS = (3200, 1600, 800, 400, 200, 100, 50)  # 登録時期の目印に読むイベント番号の間隔 (粗い方から)
ANCHOR_FIRST = 100  # これより小さい番号のイベントは無い


def anchor_order(top: int) -> list[int]:
    """登録時期の目印に読むイベント番号の順番 (粗い間隔で全期間を読んでから、細かくしていく)。"""
    seen: set[int] = set()
    out = []
    for step in ANCHOR_STEPS:
        for eid in range(ANCHOR_FIRST, top + 1, step):
            if eid not in seen:
                seen.add(eid)
                out.append(eid)
    return out


def store_status(state_dir: Path) -> dict[str, Any]:
    """research.db 全体の件数 (全ルームの収集の進み具合)。"""
    now = time.time()
    with Store(state_dir) as s:
        path = db_path(state_dir)
        size = sum(p.stat().st_size for p in (path, path.with_name(path.name + "-wal")) if p.exists())
        return {
            "rooms": s.scalar("SELECT COUNT(*) FROM rooms"),
            "collected": s.scalar("SELECT COUNT(*) FROM rooms WHERE collected_at IS NOT NULL"),
            "live_rooms": s.scalar("SELECT COUNT(*) FROM rooms WHERE live_at > ?", (now - 30 * 86400,)),
            "events": s.scalar("SELECT COUNT(*) FROM events"),
            "rankings": s.scalar("SELECT COUNT(*) FROM room_events WHERE ranking_at IS NOT NULL"),
            "rankings_waiting": s.scalar(
                f"SELECT COUNT(*) FROM room_events re JOIN events e USING(event_id) WHERE {PENDING_RANKING}",
                (now - 3600, RANKING_RETRIES)),
            "gifters": s.scalar("SELECT COUNT(*) FROM users"),
            "scan_next": s.meta("scan_next", 1), "scan_end": s.meta("scan_end"), "scan_done_at": s.meta("scan_done_at"),
            "history_next": s.meta("history_next"), "history_done_at": s.meta("history_done_at"),
            "anchor_pos": s.meta("anchor_pos", 0), "anchor_done_at": s.meta("anchor_done_at"),
            "anchor_total": len(anchor_order(int(s.meta("anchor_max")))) if s.meta("anchor_max") else None,
            "event_max": s.scalar("SELECT MAX(event_id) FROM events"),
            "oldest_ranking": s.scalar("""SELECT MIN(e.end) FROM room_events re JOIN events e USING(event_id)
                                         WHERE re.ranking_at IS NOT NULL AND e.end IS NOT NULL""", default=None),
            "progress_rows": s.scalar("SELECT COUNT(*) FROM event_progress"),
            "progress_events": s.scalar("SELECT COUNT(DISTINCT event_id) FROM event_progress"),
            "db_mb": round(size / 1e6, 1),
        }


# 全ルームの収集で取らないルーム: ルーム番号の調べで見つけた、フォロワー 0 人で配信の記録も無いもの (ギフターがいない)。
# 集まり具合の「見つけたルームのうち集めた割合」でも、分子・分母の両方から外す
SKIPPED_ROOM = "found_via = 'scan' AND COALESCE(followers, 0) = 0 AND live_at IS NULL"

# 終わって 1 時間たち、終わってからはまだ取っていない貢献ランキング (ポイントが 0 のルームは誰も送っていないので除く)
PENDING_RANKING = """re.final=0 AND (re.ranking_at IS NULL OR re.ranking_at < e.end + 3600) AND e.show_ranking=1
    AND e.end IS NOT NULL AND e.end < ? AND re.failures < ? AND (re.point IS NULL OR re.point > 0)"""


# ------------------------------------------------------------------ 分析
def habits(gifts: list[tuple[float, int, str]]) -> dict[str, Any] | None:
    """記録したギフトから、よく送る曜日・時間帯と 1 回あたりのポイントを出す。記録が無ければ None。"""
    if not gifts:
        return None
    weekday, hour = [0] * 7, [0] * 24
    for at, pt, _ in gifts:
        dt = datetime.fromtimestamp(at, JST)
        weekday[dt.weekday()] += max(pt, 1)
        hour[dt.hour] += max(pt, 1)
    paid = [pt for _, pt, _ in gifts if pt > 0]
    top_hours = sorted(range(24), key=lambda h: -hour[h])[:3]
    return {"times": len(gifts), "weekday": weekday, "hour": hour,
            "top_weekday": WEEKDAYS[max(range(7), key=lambda d: weekday[d])],
            "top_hours": [h for h in top_hours if hour[h]],
            "avg_point": round(sum(paid) / len(paid)) if paid else 0, "max_point": max(paid, default=0),
            "first_at": min(at for at, _, _ in gifts), "last_at": max(at for at, _, _ in gifts)}


def room_score(slot: dict[str, Any]) -> int:
    """そのルームに送った (少なくとも) ポイント。累計ランキングはイベントの分も含むので、合計せず大きい方を使う。"""
    return max(slot["cumulative"], slot["events"], slot["recorded"])


class _Recorded:
    """記録したギフト (state/gifts の JSONL) をギフターごとにまとめたもの。"""

    def __init__(self, state_dir: Path):
        from .listing import _points, _records, gift_catalog

        records = _records(state_dir, None, None)
        catalog = gift_catalog(state_dir, list(dict.fromkeys(key for key, _ in records)))
        self.gifts: dict[int, list[tuple[float, int, str]]] = defaultdict(list)
        self.points: dict[tuple[int, str], int] = defaultdict(int)
        self.by_user: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        self.names: dict[int, str] = {}
        for key, g in records:
            uid = g.get("user_id")
            if uid is None:
                continue
            uid = int(uid)
            pt, _free = _points(catalog, g)
            if g.get("name"):
                self.names[uid] = g["name"]  # 後の記録の名前で上書きする
            self.points[(uid, key)] += pt
            self.by_user[uid][key] += pt
            self.gifts[uid].append((g["at"], pt, key))


class _Rooms:
    """ルームの URL キーと room_id の対応と、表示する名前。

    記録したギフトはルームの URL キーで、research.db は room_id で持っているのでつなぐ。
    どちらにも room_id が無いルームには仮の (負の) 番号を付ける。
    """

    def __init__(self, store: Store, state_dir: Path):
        from .listing import room_names

        self.store = store
        self.known = room_names(state_dir)
        self.ids: dict[str, int] = {}
        self.labels: dict[int, tuple[str, str]] = {}

    def id(self, key: str) -> int:
        if key in self.ids:
            return self.ids[key]
        row = self.store.room(key=key)
        info = self.known.get(key) or {}
        if row is not None:
            rid = int(row["room_id"])
        elif info.get("room_id"):
            rid = int(info["room_id"])
        else:
            rid = -(len([i for i in self.ids.values() if i < 0]) + 1)
        self.ids[key] = rid
        if row is None:
            self.labels[rid] = (key, info.get("name") or key)
        return rid

    def label(self, rid: int) -> tuple[str, str]:
        if rid not in self.labels:
            row = self.store.room(room_id=rid)
            key = (row["key"] if row is not None else None) or str(rid)
            name = (row["name"] if row is not None else None) or (self.known.get(key) or {}).get("name") or key
            self.labels[rid] = (key, name)
        return self.labels[rid]


SHOWROOM_START = datetime(2013, 11, 1, tzinfo=JST).timestamp()  # SHOWROOM が始まったころ (ID 0 の目印)
SIGNUP_CLOSE = 92 * 86400  # 目印の間がこれより狭ければ「〜月ごろ」、広ければ「〜年〜年ごろ」と出す
SIGNUP_ROUGH = 400 * 86400  # 目印の間がこれより広い年は、年ごとの絞り込みで「大まか」と出す


class _Signup:
    """ユーザー ID から登録時期を推測する。

    SHOWROOM のユーザー ID は登録順に増える。ある時までに分かった最大の ID (イベントの貢献ランキング・記録したギフト)
    を目印にすると、ID k の人は k 以上の ID が初めて分かった時までには登録している。目印の間は ID と時期を
    直線でつないで推測する (古いイベントを飛び飛びに読んで、目印を先にそろえる: sweep の _next_signup_anchor)。
    """

    def __init__(self, steps: list[list[float]]):
        # ID の小さい順に (それまでに分かった最大の ID, その時)。どちらも増える一方
        self.steps = steps
        self.ids = [0] + [int(i) for i, _ in steps]
        self.ats = [SHOWROOM_START] + [max(float(a), SHOWROOM_START) for _, a in steps]

    @classmethod
    def build(cls, s: Store, rec: _Recorded) -> _Signup:
        seen = [(r[0], r[1]) for r in s.all("""SELECT e.end, m.uid FROM (SELECT event_id, MAX(user_id) AS uid FROM contributions
                                               WHERE guessed IS NULL GROUP BY event_id) m JOIN events e USING(event_id) WHERE e.end IS NOT NULL""")]
        seen += [(min(g[0] for g in gifts), uid) for uid, gifts in rec.gifts.items() if gifts]
        steps, top = [], 0
        for at, uid in sorted(seen):
            if uid > top:
                steps.append([uid, at])
                top = uid
        return cls(steps)

    def at(self, uid: int) -> float | None:
        """遅くともこの時までに登録している。"""
        i = bisect_left(self.ids, uid, 1)
        return self.ats[i] if i < len(self.ids) else None

    def guess(self, uid: int) -> tuple[float, float, float] | None:
        """(推測した登録時期, 前の目印, 次の目印)。分かっている最大の ID より大きければ None。"""
        i = bisect_left(self.ids, uid, 1)
        if i >= len(self.ids):
            return None
        m0, m1, t0, t1 = self.ids[i - 1], self.ids[i], self.ats[i - 1], self.ats[i]
        return t0 + (uid - m0) / (m1 - m0) * (t1 - t0), t0, t1

    def id_at(self, t: float) -> float:
        """推測した登録時期が t になる ID (guess の逆)。"""
        j = bisect_left(self.ats, t)
        if j <= 0:
            return 0
        if j >= len(self.ats):
            return self.ids[-1]
        m0, m1, t0, t1 = self.ids[j - 1], self.ids[j], self.ats[j - 1], self.ats[j]
        return m0 + (t - t0) / (t1 - t0) * (m1 - m0)

    def years(self) -> list[tuple[int, int, int, bool]]:
        """推測した登録年ごとの (年, 最初の ID, 最後の ID, 大まかか) (新しい年から)。"""
        if len(self.ids) < 2:
            return []
        out = []
        for year in range(datetime.fromtimestamp(self.ats[-1], JST).year, 2012, -1):
            start, end = datetime(year, 1, 1, tzinfo=JST).timestamp(), datetime(year + 1, 1, 1, tzinfo=JST).timestamp()
            lo = max(math.ceil(self.id_at(start)), 1)
            hi = self.ids[-1] if end > self.ats[-1] else math.ceil(self.id_at(end)) - 1
            if hi >= lo:
                rough = any(g[2] - g[1] > SIGNUP_ROUGH for g in (self.guess(lo), self.guess(hi)) if g)
                out.append((year, lo, hi, rough))
        return out


SIGNUP_YEAR_LIMIT = 100  # 登録した年ごとに、全体の上位とは別に返すギフターの数 (登録時期で絞り込むため)
SIGNUP_META = "signup_steps"  # 1 人の詳しい表示で使う登録時期の推測 (分析のたびに作り直す)


def _signup(s: Store, rec: _Recorded) -> _Signup:
    steps = s.meta(SIGNUP_META)
    return _Signup(steps) if steps else _Signup.build(s, rec)


def _chunks(items: list[Any], n: int = 500) -> list[list[Any]]:
    return [items[i:i + n] for i in range(0, len(items), n)]


def _slots(store: Store, rooms: _Rooms, rec: _Recorded, uids: list[int]) -> dict[int, dict[int, dict[str, Any]]]:
    """ギフターごと・ルームごとの送ったポイント (記録・累計ランキング・貢献ランキング)。"""
    out: dict[int, dict[int, dict[str, Any]]] = defaultdict(dict)
    for part in _chunks(uids):
        for r in store.all(f"SELECT * FROM user_room WHERE user_id IN ({','.join('?' * len(part))})", part):
            out[r["user_id"]][r["room_id"]] = {"recorded": 0, "cumulative": r["cumulative"] or 0, "visits": r["visits"] or 0,
                                               "events": r["events"] or 0, "cumulative_rank": r["cumulative_rank"]}
    for uid in uids:
        for key, pt in rec.by_user.get(uid, {}).items():
            if not pt:
                continue
            slot = out[uid].setdefault(rooms.id(key), {"recorded": 0, "cumulative": 0, "visits": 0, "events": 0,
                                                       "cumulative_rank": None})
            slot["recorded"] += pt
    return out


def _event_stats(store: Store, uids: list[int]) -> dict[int, Any]:
    out = {}
    for part in _chunks(uids):
        for r in store.all(f"""SELECT user_id, COUNT(DISTINCT event_id) AS n, SUM(point) AS p,
                                      MIN(CASE WHEN rank > 0 THEN rank END) AS best
                               FROM contributions WHERE user_id IN ({','.join('?' * len(part))}) GROUP BY user_id""", part):
            out[r["user_id"]] = r
    return out


def _user_names(store: Store, rec: _Recorded | None, uids: list[int]) -> dict[int, str]:
    out = {}
    for part in _chunks(uids):
        for r in store.all(f"SELECT user_id, name FROM users WHERE user_id IN ({','.join('?' * len(part))})", part):
            out[r["user_id"]] = r["name"]
    if rec is not None:
        out.update({u: rec.names[u] for u in uids if u in rec.names})
    left = [u for u, n in out.items() if n in LEFT_NAMES]
    for part in _chunks(left):
        # 退会したユーザーは、分かっていれば前の名前も出す (いちばん後に使っていた名前)
        for r in store.all(f"""SELECT user_id, name FROM user_names WHERE user_id IN ({','.join('?' * len(part))})
                               AND name NOT IN ({','.join('?' * len(LEFT_NAMES))}) ORDER BY at""", (*part, *LEFT_NAMES)):
            out[r["user_id"]] = f"{sorted(LEFT_NAMES)[0]} (前の名前: {r['name']})"
    return out


def _summary(uid: int, name: str | None, slots: dict[int, dict[str, Any]], ev: Any, gifts: list[Any],
             rooms: _Rooms, support: set[int], signup: tuple[float, float, float] | None = None) -> dict[str, Any]:
    scores = {rid: room_score(v) for rid, v in slots.items()}
    total = sum(scores.values())
    other = {rid: s for rid, s in scores.items() if rid not in support and s > 0}
    top = sorted(other.items(), key=lambda kv: -kv[1])
    h = habits(gifts)
    return {
        "user_id": uid, "name": name or "(名前なし)",
        # 推測した登録時期と、その前後の目印 (この間に登録したと考えられる)
        "signup_at": signup and signup[0], "signup_from": signup and signup[1], "signup_to": signup and signup[2],
        "support_points": sum(s for rid, s in scores.items() if rid in support),
        "other_points": sum(other.values()), "total_points": total,
        "room_count": sum(1 for s in scores.values() if s > 0),
        # いちばん多く送るルームの割合 (1 に近いほど 1 つのルームに集中している)
        "focus": round(max(scores.values()) / total, 2) if total else None,
        "top_rooms": [{"key": rooms.label(rid)[0], "name": rooms.label(rid)[1], "points": s} for rid, s in top[:3]],
        "event_count": ev["n"] if ev else 0,
        "event_points": ev["p"] if ev else 0,
        "best_rank": ev["best"] if ev else None,
        "visits": max((v["visits"] for v in slots.values()), default=0),
        "habits": None if h is None else {k: h[k] for k in ("times", "top_weekday", "top_hours", "avg_point", "last_at")},
        "sources": sorted({src for v in slots.values() for src in ("recorded", "cumulative", "events") if v[src]}),
    }


_analysis: dict[Any, tuple[float, dict[str, Any]]] = {}
_analysis_lock = threading.Lock()


def forget_analysis() -> None:
    """次の analyze で数え直す (集め終わったときなど)。"""
    _analysis.clear()


def analyze(state_dir: Path, support: list[str], limit: int = 300, max_age: float = ANALYSIS_TTL) -> dict[str, Any]:
    """集めた情報をギフターとルームの両方から分析する。

    応援ルームがあれば、それを基準に誘導の候補・応援ルームのギフター・似ているルームを出す。
    無ければ (mode "all")、ギフター一覧 (分かったポイントの多い順)・ルーム同士の重なり・ルームごとのまとめを出す。
    全ルーム分だと重いので、結果は max_age 秒使い回す (同時に何本も数えないように 1 本ずつ)。
    """
    key = (str(Path(state_dir).resolve()), tuple(support), limit)
    with _analysis_lock:
        hit = _analysis.get(key)
        if hit and time.time() - hit[0] < max_age:
            return hit[1]
        started = time.time()
        result = _analyze(Path(state_dir), support, limit)
        result["analyzed_at"] = time.time()
        result["analysis_seconds"] = round(result["analyzed_at"] - started, 1)
        _analysis[key] = (result["analyzed_at"], result)
        return result


def _analyze(state_dir: Path, support: list[str], limit: int) -> dict[str, Any]:
    rec = _Recorded(state_dir)
    with Store(state_dir) as s:
        rooms = _Rooms(s, state_dir)
        sup_ids = list(dict.fromkeys(rooms.id(k) for k in support))
        c = s.conn
        c.execute("CREATE TEMP TABLE rec (user_id INTEGER, room_id INTEGER, pt INTEGER, PRIMARY KEY (user_id, room_id))")
        merged: dict[tuple[int, int], int] = defaultdict(int)
        for (uid, key), pt in rec.points.items():
            if pt:
                merged[(uid, rooms.id(key))] += pt
        c.executemany("INSERT INTO rec VALUES (?, ?, ?)", [(u, r, pt) for (u, r), pt in merged.items()])
        c.execute("CREATE TEMP TABLE sup (room_id INTEGER PRIMARY KEY)")
        c.executemany("INSERT INTO sup VALUES (?)", [(r,) for r in sup_ids])
        c.execute("CREATE TEMP TABLE rname (user_id INTEGER PRIMARY KEY, name TEXT)")
        c.executemany("INSERT INTO rname VALUES (?, ?)", list(rec.names.items()))
        # ギフター × ルームの送ったポイント (累計ランキング・貢献ランキング・記録の大きい方)
        c.execute("""CREATE TEMP TABLE pair AS SELECT user_id, room_id, MAX(s) AS s FROM (
                       SELECT user_id, room_id, MAX(cumulative, events) AS s FROM user_room
                       UNION ALL SELECT user_id, room_id, pt FROM rec)
                     GROUP BY user_id, room_id HAVING MAX(s) > 0""")
        c.execute("""CREATE TEMP TABLE u (user_id INTEGER PRIMARY KEY, total INTEGER, support INTEGER, n INTEGER)""")
        c.execute("""INSERT INTO u SELECT user_id, SUM(s), SUM(CASE WHEN room_id IN (SELECT room_id FROM sup) THEN s ELSE 0 END),
                       COUNT(*) FROM pair GROUP BY user_id""")
        c.execute("CREATE TEMP TABLE fan (user_id INTEGER PRIMARY KEY)")
        c.execute("INSERT INTO fan SELECT user_id FROM u WHERE support > 0")
        fan_count = s.scalar("SELECT COUNT(*) FROM fan")
        fan_ids = [r[0] for r in s.all("SELECT user_id FROM u WHERE support > 0 ORDER BY support DESC, user_id LIMIT ?", (limit,))]
        name = ("COALESCE((SELECT name FROM rname r WHERE r.user_id=u.user_id),"
                " (SELECT name FROM users n WHERE n.user_id=u.user_id), '')")
        left = tuple(LEFT_NAMES)
        where = f"support = 0 AND total > 0 AND {name} NOT IN ({','.join('?' * len(left))})"
        candidate_count = s.scalar(f"SELECT COUNT(*) FROM u WHERE {where}", left)
        cand_ids = [r[0] for r in s.all(f"SELECT user_id FROM u WHERE {where} ORDER BY total DESC, user_id LIMIT ?",
                                        (*left, limit))]
        signup = _Signup.build(s, rec)
        with s.tx():
            s.set_meta(SIGNUP_META, signup.steps)
        signup_years, extra_ids = [], []
        overall = set(cand_ids)
        for year, lo, hi, rough in signup.years():
            band = (*left, lo, hi)
            n = s.scalar(f"SELECT COUNT(*) FROM u WHERE {where} AND user_id BETWEEN ? AND ?", band)
            if n:
                signup_years.append({"year": year, "count": n, "rough": rough})
                extra_ids += [r[0] for r in s.all(f"""SELECT user_id FROM u WHERE {where} AND user_id BETWEEN ? AND ?
                                                     ORDER BY total DESC, user_id LIMIT ?""", (*band, SIGNUP_YEAR_LIMIT))
                              if r[0] not in overall]
        gifter_count = s.scalar("SELECT COUNT(*) FROM u")
        # 似ているルーム: 応援ルームのギフターが、ほかにどのルームに送っているか
        similar = []
        for r in s.all("""SELECT room_id, COUNT(*) AS gifters, SUM(user_id IN (SELECT user_id FROM fan)) AS shared FROM pair
                          WHERE room_id NOT IN (SELECT room_id FROM sup) GROUP BY room_id HAVING shared > 0
                          ORDER BY shared DESC, CAST(shared AS REAL) / (gifters + ? - shared) DESC LIMIT 100""", (fan_count,)):
            key, label = rooms.label(r["room_id"])
            similar.append({"key": key, "name": label, "gifters": r["gifters"], "shared": r["shared"],
                            "overlap": round(r["shared"] / (r["gifters"] + fan_count - r["shared"]), 3),
                            "candidates": r["gifters"] - r["shared"]})
        # ルームごとのまとめ (ギフターの数・分かったポイント・複数のルームに送る人・いちばん送っている人)。
        # MAX(p.s) と一緒に選んだ p.user_id は、SQLite ではその最大の行の値になる
        c.execute("""CREATE TEMP TABLE rs AS SELECT p.room_id, COUNT(*) AS gifters, SUM(p.s) AS points,
                       SUM(u.n > 1) AS multi, p.user_id AS top_user, MAX(p.s) AS top_points
                     FROM pair p JOIN u USING(user_id) GROUP BY p.room_id""")
        givers = {r[0]: r[1] for r in s.all("SELECT room_id, gifters FROM rs")} if sup_ids else {}
        room_count = s.scalar("SELECT COUNT(*) FROM rs")
        multi_count = s.scalar(f"SELECT COUNT(*) FROM u WHERE n > 1 AND {name} NOT IN ({','.join('?' * len(left))})", left)
        room_rows = s.all("SELECT * FROM rs ORDER BY points DESC LIMIT 200")
        pairs = _room_pairs(s, rooms)

        cov = coverage(state_dir, max_age=0)
        trend = _trend(s, cov, left)
        trend_ids = [r["user_id"] for kind in ("falling", "rising", "hidden") for r in trend[kind]]

        uids = list(dict.fromkeys(fan_ids + cand_ids + extra_ids + trend_ids))
        evs, names = _event_stats(s, uids), _user_names(s, rec, uids)
        sup = set(sup_ids)
        # ルームごとの内訳は 1 人ずつ読む (数万ルームに出てくる人もいて、まとめて読むとメモリが足りなくなる)
        summary = {u: _summary(u, names.get(u), _slots(s, rooms, rec, [u]).get(u, {}), evs.get(u), rec.gifts.get(u, []),
                               rooms, sup, signup.guess(u)) for u in uids}
        top_names = _user_names(s, rec, list({r["top_user"] for r in room_rows}))
        room_list = []
        for r in room_rows:
            key, label = rooms.label(r["room_id"])
            info = s.room(room_id=r["room_id"]) if r["room_id"] > 0 else None
            room_list.append({"key": key, "name": label, "gifters": r["gifters"], "points": r["points"], "multi": r["multi"],
                              "events": s.scalar("SELECT COUNT(*) FROM room_events WHERE room_id=?", (r["room_id"],)),
                              "researched": bool(info is not None and info["collected_at"]), "support": r["room_id"] in sup,
                              "top": {"user_id": r["top_user"], "name": top_names.get(r["top_user"]) or "(名前なし)",
                                      "points": r["top_points"]}})
        return {"mode": "support" if sup_ids else "all",
                "support": [{"key": k, "name": rooms.label(rooms.id(k))[1], "gifters": givers.get(rooms.id(k), 0)}
                            for k in support],
                "candidates": [summary[u] for u in cand_ids], "candidate_count": candidate_count,
                "signup_candidates": [summary[u] for u in extra_ids], "signup_years": signup_years,
                "fans": [summary[u] for u in fan_ids], "fan_count": fan_count, "similar": similar,
                "pairs": pairs, "rooms": room_list, "room_count": room_count, "multi_count": multi_count,
                "gifter_count": gifter_count, "coverage": cov,
                "trend": {**trend, **{kind: [{**summary[r["user_id"]], **r} for r in trend[kind]]
                                      for kind in ("falling", "rising", "hidden")}}}


# ------------------------------------------------------------------ 集まり具合と、支出の増え減り
COMPLETE_RATIO = 0.95  # 貢献ランキングをこの割合まで取った月を「集め終わった」とする
ROOMS_RATIO = 0.95  # 見つけたルームのうち、この割合まで累計ランキングなどを取るまでは、どの月も集め終わっていない扱い
TREND_PERIODS = (12, 6, 3)  # 比べる期間 (か月)。集め終わった月で比べられる、いちばん長い期間を使う
TREND_MIN = 10000  # 増え減りの一覧に出す人の、多い方の期間のポイントの下限
TREND_LIMIT = 100  # 増え減りの一覧に出す人数 (それぞれ)
_coverage_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def _add_months(month: str, n: int) -> str:
    y, m = map(int, month.split("-"))
    t = y * 12 + m - 1 + n
    return f"{t // 12:04d}-{t % 12 + 1:02d}"


def coverage(state_dir: Path, max_age: float = ANALYSIS_TTL, now: float | None = None) -> dict[str, Any]:
    """月ごとの集まり具合 (イベントの貢献ランキングを、その月に終わったイベントの分どれだけ取ったか)。

    全ルームの収集は新しいイベントから取るので、古い月ほど少なく見える。集め終わっていない月を比べると
    「最近増えた」ように見えてしまうため、増え減りは集め終わった月どうしでだけ比べる。
    """
    key = str(Path(state_dir).resolve())
    hit = _coverage_cache.get(key)
    if hit and time.time() - hit[0] < max_age and now is None:
        return hit[1]
    with Store(state_dir) as s:
        result = _coverage(s, time.time() if now is None else now)
    _coverage_cache[key] = (time.time(), result)
    return result


def _coverage(s: Store, now: float) -> dict[str, Any]:
    rows = s.all(f"""SELECT {MONTH_SQL} AS month, SUM(re.final=1) AS done, SUM({PENDING_RANKING}) AS pending
                     FROM room_events re JOIN events e USING(event_id)
                     WHERE e.end IS NOT NULL AND e.end < ? AND e.show_ranking=1 GROUP BY month ORDER BY month""",
                 (now - 3600, RANKING_RETRIES, now - 3600))
    rooms_total = s.scalar(f"SELECT COUNT(*) FROM rooms WHERE NOT ({SKIPPED_ROOM})")
    rooms_done = s.scalar(f"SELECT COUNT(*) FROM rooms WHERE collected_at IS NOT NULL AND NOT ({SKIPPED_ROOM})")
    rooms_ok = bool(rooms_total) and rooms_done >= rooms_total * ROOMS_RATIO
    # 過去のイベントの読み込み (番号を新しい順にさかのぼる) がどの月まで進んだか。読み終えていれば全部
    nxt, read_done = s.meta("history_next"), s.meta("history_done_at")
    if read_done:
        read_from = "0000-00"
    else:
        first = s.scalar("SELECT MIN(end) FROM events WHERE event_id > ? AND end IS NOT NULL", (nxt,), default=None) \
            if nxt is not None else None
        read_from = _add_months(_month(first), 1) if first else "9999-99"  # その月の途中のイベントは、まだ読んでいないかもしれない
    this_month = _month(now)
    last_full = _add_months(this_month, -1)
    by_month = {r["month"]: (r["done"] or 0, r["pending"] or 0) for r in rows}
    months = []
    if by_month:
        m = min(by_month)
        while m <= this_month:
            done, pending = by_month.get(m, (0, 0))
            ratio = done / (done + pending) if done + pending else None
            months.append({"month": m, "done": done, "pending": pending, "ratio": None if ratio is None else round(ratio, 3),
                           "complete": m < this_month and rooms_ok and m >= read_from and (ratio is None or ratio >= COMPLETE_RATIO)})
            m = _add_months(m, 1)
    complete_from = None
    for row in reversed([x for x in months if x["month"] <= last_full]):
        if not row["complete"]:
            break
        complete_from = row["month"]
    years: dict[str, dict[str, Any]] = {}
    for row in months:
        y = years.setdefault(row["month"][:4], {"year": int(row["month"][:4]), "done": 0, "pending": 0, "months": 0, "complete": 0})
        y["done"] += row["done"]
        y["pending"] += row["pending"]
        y["months"] += 1
        y["complete"] += row["complete"]
    for y in years.values():
        y["ratio"] = round(y["done"] / (y["done"] + y["pending"]), 3) if y["done"] + y["pending"] else None
    return {"months": months, "years": sorted(years.values(), key=lambda y: -y["year"]), "complete_from": complete_from,
            "last_full": last_full, "rooms": {"total": rooms_total, "collected": rooms_done, "ok": rooms_ok},
            "read_from": None if read_from == "9999-99" else read_from, "read_done": bool(read_done),
            "cumulative_from": (lambda at: _month(at) if at else None)(s.scalar("SELECT MIN(at) FROM cumulative_history", default=None))}


def _trend(s: Store, cov: dict[str, Any], left: tuple[str, ...]) -> dict[str, Any]:
    """離れつつある人・伸びている人 (集め終わった月どうしで、最近の期間とその前の期間のイベントの貢献ポイントを比べる)。"""
    while s.build_user_month():  # 前に集めた分がまだ入っていなければ、ここで入れ終える
        pass
    last, start = cov["last_full"], cov["complete_from"]
    period = next((p for p in TREND_PERIODS if start and _add_months(last, -(2 * p - 1)) >= start), None)
    out: dict[str, Any] = {"period": period, "min": TREND_MIN, "falling": [], "rising": [], "hidden": [], "falling_count": 0,
                           "rising_count": 0, "hidden_count": 0}
    if period is None:
        return out
    recent_from, before_from = _add_months(last, -(period - 1)), _add_months(last, -(2 * period - 1))
    out.update({"recent": [recent_from, last], "before": [before_from, _add_months(recent_from, -1)]})
    s.conn.execute("DROP TABLE IF EXISTS temp.tr")
    s.conn.execute("""CREATE TEMP TABLE tr AS SELECT user_id, SUM(CASE WHEN month >= ? THEN points ELSE 0 END) AS recent,
                        SUM(CASE WHEN month < ? THEN points ELSE 0 END) AS before FROM user_month
                      WHERE month BETWEEN ? AND ? GROUP BY user_id""", (recent_from, recent_from, before_from, last))
    gone = f"(SELECT name FROM users n WHERE n.user_id=tr.user_id) IN ({','.join('?' * len(left))})"
    for kind, cond, order in (("falling", "before >= ? AND recent * 2 <= before", "before - recent"),
                              ("rising", "recent >= ? AND recent >= before * 2", "recent - before")):
        where = f"{cond} AND NOT COALESCE({gone}, 0)"
        out[f"{kind}_count"] = s.scalar(f"SELECT COUNT(*) FROM tr WHERE {where}", (TREND_MIN, *left))
        limit = TREND_CHECK if kind == "falling" else TREND_LIMIT
        out[kind] = [dict(r) for r in s.all(f"SELECT user_id, recent, before FROM tr WHERE {where} ORDER BY {order} DESC, user_id LIMIT ?",
                                            (TREND_MIN, *left, limit))]
    _check_falling(s, out, (_month_start(before_from), _month_start(recent_from)), (_month_start(recent_from), _month_start(_add_months(last, 1))))
    return out


TREND_CHECK = 500  # 離れつつある人の候補のうち、「見えなくなっただけ」かを 1 人ずつ確かめる人数 (減った量の多い順)
FALLING_ROOMS = 5  # 前の期間に送っていたルームのうち、確かめるルームの数 (多い順)


def _month_start(month: str) -> float:
    y, m = (int(x) for x in month.split("-"))
    return datetime(y, m, 1, tzinfo=JST).timestamp()


def _check_falling(s: Store, out: dict[str, Any], before: tuple[float, float], recent: tuple[float, float]) -> None:
    """離れつつある人の候補を、1 人ずつ確かめる。

    - 見えない分の上限: 貢献ランキングは上位 100 人しか見えないので、101 位に落ちただけでも 0 に見える。前の期間に送っていた
      ルームの、最近の期間のイベントのうち、その人が載っていないイベントは「100 位の人のポイントより少なく送ったかもしれない」
      として、100 位のポイントを足す (100 人に届かないイベントは、送っていれば載るので足さない)。
      それでも最近 (上限) が前の期間の半分以下の人だけを「離れつつある」にする。そうでない人は「見えなくなっただけかも」
    - 理由: いちばん送っていたルームが、最近の期間にイベントに出ていない・消えた → 「推しのルームが休んだ・消えた」。
      それ以外は「本人が減った」
    """
    confirmed, hidden = [], []
    for u in out["falling"]:
        uid = u["user_id"]
        rooms = s.all("""SELECT c.room_id, SUM(c.point) AS points FROM contributions c JOIN events e USING(event_id)
                         WHERE c.user_id=? AND e.end >= ? AND e.end < ? GROUP BY c.room_id ORDER BY points DESC LIMIT ?""",
                      (uid, *before, FALLING_ROOMS))
        upper = 0
        for r in rooms:
            upper += s.scalar("""SELECT SUM(re.c_p100) FROM room_events re JOIN events e USING(event_id)
                                 WHERE re.room_id=? AND re.final=1 AND re.c_count >= 100 AND e.end >= ? AND e.end < ?
                                   AND NOT EXISTS (SELECT 1 FROM contributions c WHERE c.event_id=re.event_id
                                                   AND c.room_id=re.room_id AND c.user_id=?)""",
                              (r["room_id"], *recent, uid), default=0)
        main = rooms[0]["room_id"] if rooms else None
        reason = "本人が減った"
        if main is not None:
            room = s.room(room_id=main)
            active = s.scalar("""SELECT COUNT(*) FROM room_events re JOIN events e USING(event_id)
                                 WHERE re.room_id=? AND e.end >= ? AND e.end < ?""", (main, *recent))
            if (room is not None and room["gone"]) or not active:
                reason = "推しのルームが休んだ・消えた"
        u.update({"hidden_upper": upper, "recent_upper": u["recent"] + upper, "reason": reason, "main_room": main})
        (confirmed if (u["recent"] + upper) * 2 <= u["before"] else hidden).append(u)
    checked = len(out["falling"])
    out["falling_checked"] = checked
    out["falling_count"] = len(confirmed) + max(0, out["falling_count"] - checked)  # 確かめていない人は、そのまま数える
    out["falling"] = confirmed[:TREND_LIMIT]
    out["hidden"] = hidden[:TREND_LIMIT]
    out["hidden_count"] = len(hidden)
    reasons: dict[str, int] = {}
    for u in confirmed:
        reasons[u["reason"]] = reasons.get(u["reason"], 0) + 1
    out["falling_reasons"] = reasons


PAIR_MIN_SHARED = 2  # ルーム同士の重なりに出す、共通のギフターの最少人数
PAIR_ROOMS = 300  # ルーム同士の重なりを調べるルームの数 (ギフターの多い順)
PAIR_USER_ROOMS = 100  # これより多くのルームに送っている人は、重なりの数に入れない (組の数が増えすぎるため)


def _room_pairs(s: Store, rooms: _Rooms) -> list[dict[str, Any]]:
    """ルーム同士の重なり: どのルームとどのルームに、同じギフターが送っているか (ギフターの好みが近いルーム)。

    全ルームの組は数が多すぎるので、ギフターの多い PAIR_ROOMS ルームの中で調べる。
    """
    c = s.conn
    c.execute("CREATE TEMP TABLE top_rooms AS SELECT room_id, gifters FROM rs ORDER BY gifters DESC LIMIT ?", (PAIR_ROOMS,))
    c.execute("""CREATE TEMP TABLE tp AS SELECT p.user_id, p.room_id FROM pair p JOIN u USING(user_id)
                 WHERE p.room_id IN (SELECT room_id FROM top_rooms) AND u.n BETWEEN 2 AND ?""", (PAIR_USER_ROOMS,))
    c.execute("CREATE INDEX temp.tp_user ON tp(user_id)")
    gifters = {r[0]: r[1] for r in s.all("SELECT room_id, gifters FROM top_rooms")}
    out = []
    for r in s.all("""SELECT a.room_id AS ra, b.room_id AS rb, COUNT(*) AS shared FROM tp a
                      JOIN tp b ON a.user_id = b.user_id AND a.room_id < b.room_id
                      GROUP BY ra, rb HAVING shared >= ? ORDER BY shared DESC LIMIT 1000""", (PAIR_MIN_SHARED,)):
        a, b = r["ra"], r["rb"]
        if gifters[b] > gifters[a] or (gifters[b] == gifters[a] and rooms.label(b)[0] < rooms.label(a)[0]):
            a, b = b, a
        out.append({"a": rooms.label(a)[0], "a_name": rooms.label(a)[1], "b": rooms.label(b)[0], "b_name": rooms.label(b)[1],
                    "shared": r["shared"], "overlap": round(r["shared"] / (gifters[a] + gifters[b] - r["shared"]), 3)})
    out.sort(key=lambda p: (-p["shared"], -p["overlap"]))
    return out[:100]


DETAIL_ROWS = 300  # 1 人の詳しい表示で返すルーム・イベントの数 (数万ルームに出てくる人もいる)


def gifter_detail(state_dir: Path, user_id: Any, support: list[str]) -> dict[str, Any]:
    """1 人のギフターの詳しい情報 (ルームごとの内訳・イベントの実績・行動のくせ)。"""
    try:
        uid = int(str(user_id).strip())
    except (TypeError, ValueError):
        raise ValueError("ユーザー ID は数字で指定してください") from None
    rec = _Recorded(state_dir)
    with Store(state_dir) as s:
        rooms = _Rooms(s, state_dir)
        slots = _slots(s, rooms, rec, [uid]).get(uid, {})
        events = [dict(r) for r in s.all("""SELECT c.event_id, e.name AS event, c.room_id, c.point, c.rank, e.end,
                                                   c.guessed IS NOT NULL AS guessed
                                            FROM contributions c LEFT JOIN events e USING(event_id)
                                            WHERE c.user_id=? ORDER BY COALESCE(e.end, 0) DESC LIMIT ?""", (uid, DETAIL_ROWS))]
        event_total = s.scalar("SELECT COUNT(*) FROM contributions WHERE user_id=?", (uid,))
        g = s.one("SELECT COUNT(*) AS n, SUM(point) AS points FROM contributions WHERE user_id=? AND guessed IS NOT NULL", (uid,))
        guessed = {"events": g["n"], "points": g["points"] or 0}
        gifts = rec.gifts.get(uid, [])
        if not slots and not events and not gifts:
            return {"user_id": uid, "found": False}
        sup = {rooms.id(k) for k in support}
        name = _user_names(s, rec, [uid]).get(uid)
        for e in events:
            e["room"], e["room_name"] = rooms.label(e.pop("room_id"))
            e["event"] = e["event"] or str(e["event_id"])
        room_list = sorted(({"key": rooms.label(rid)[0], "name": rooms.label(rid)[1], "support": rid in sup,
                             "points": room_score(v), **v} for rid, v in slots.items()), key=lambda r: -r["points"])
        w = s.one("SELECT memo, added_at FROM watch WHERE user_id=?", (uid,))
    cov = coverage(state_dir)
    with Store(state_dir) as s:
        return {"user_id": uid, "found": True,
                "coverage": {k: cov[k] for k in ("complete_from", "last_full", "cumulative_from")}, "room_total": len(room_list), "event_total": event_total,
                "guessed": guessed,
                "watch": {"memo": w["memo"] or "", "added_at": w["added_at"]} if w else None,
                "former_names": [dict(r) for r in s.all("""SELECT name, at, source FROM user_names WHERE user_id=?
                                                          ORDER BY at DESC LIMIT 20""", (uid,))],
                "summary": _summary(uid, name, slots, _event_stats(s, [uid]).get(uid), gifts, rooms, sup,
                                    _signup(s, rec).guess(uid)),
                "rooms": room_list[:DETAIL_ROWS], "events": events, "habits": habits(gifts), "timeline": _timeline(s, rooms, gifts, uid)}


MEMO_MAX = 500  # 気になるギフターのメモの長さの上限 (文字)


def set_watch(state_dir: Path, user_id: Any, watched: bool, memo: Any = None) -> None:
    """気になるギフターに入れる・外す (メモつき)。"""
    try:
        uid = int(str(user_id).strip())
    except (TypeError, ValueError):
        raise ValueError("ユーザー ID は数字で指定してください") from None
    if memo is not None:
        memo = str(memo).strip()
        if len(memo) > MEMO_MAX:
            raise ValueError(f"メモは {MEMO_MAX} 文字までです")
    with Store(state_dir) as s:
        s.set_watch(uid, bool(watched), memo, time.time())


def watch_list(state_dir: Path, support: list[str]) -> list[dict[str, Any]]:
    """気になるギフターの一覧 (メモと、分析と同じ要約つき)。新しく入れた順。"""
    with Store(state_dir) as s:
        rows = [dict(r) for r in s.watch_rows()]
        if not rows:
            return []
        rec = _Recorded(state_dir)
        rooms = _Rooms(s, state_dir)
        uids = [r["user_id"] for r in rows]
        slots = _slots(s, rooms, rec, uids)
        stats = _event_stats(s, uids)
        names = _user_names(s, rec, uids)
        sup = {rooms.id(k) for k in support}
        signup = _signup(s, rec)
        out = []
        for r in rows:
            uid = r["user_id"]
            summary = _summary(uid, names.get(uid), slots.get(uid, {}), stats.get(uid), rec.gifts.get(uid, []), rooms, sup,
                               signup.guess(uid))
            out.append({**summary, "memo": r["memo"] or "", "added_at": r["added_at"], "updated_at": r["updated_at"]})
        return out


TIMELINE_ROOMS = 5  # 月ごとの推移で色分けするルームの数 (ほかは「その他」)


def _month(at: float) -> str:
    return datetime.fromtimestamp(float(at), JST).strftime("%Y-%m")


def _timeline(s: Store, rooms: _Rooms, gifts: list[tuple[float, int, str]], uid: int) -> dict[str, Any]:
    """月ごとの支出の推移 (ルーム別)。種類ごとに分ける (重なりがあるので足さない)。

    - events: イベントの貢献ポイント (イベントが終わった月)
    - recorded: 記録したギフト
    - cumulative: 累計ランキングの増え (取り直した間に増えた分を、取り直した月に)
    """
    data: dict[str, dict[str, dict[int, int]]] = {k: defaultdict(lambda: defaultdict(int)) for k in ("events", "recorded", "cumulative")}
    for r in s.all("""SELECT c.room_id, strftime('%Y-%m', e.end + 9 * 3600, 'unixepoch') AS month, SUM(c.point) AS point
                      FROM contributions c JOIN events e USING(event_id)
                      WHERE c.user_id=? AND e.end IS NOT NULL GROUP BY c.room_id, month""", (uid,)):
        data["events"][r["month"]][r["room_id"]] += r["point"]
    for at, pt, key in gifts:
        if pt > 0:
            data["recorded"][_month(at)][rooms.id(key)] += pt
    last: dict[int, int] = {}
    for r in s.all("SELECT room_id, at, point FROM cumulative_history WHERE user_id=? ORDER BY room_id, at", (uid,)):
        before = last.get(r["room_id"])
        if before is not None and r["point"] > before:
            data["cumulative"][_month(r["at"])][r["room_id"]] += r["point"] - before
        last[r["room_id"]] = r["point"]
    months = sorted({m for kind in data.values() for m in kind})
    if not months:
        return {"months": [], "rooms": [], "names": {}}
    totals: dict[int, int] = defaultdict(int)
    for kind in data.values():
        for per in kind.values():
            for rid, pt in per.items():
                totals[rid] += pt
    top = [rid for rid, _ in sorted(totals.items(), key=lambda kv: -kv[1])[:TIMELINE_ROOMS]]
    out = []
    y, m = map(int, months[0].split("-"))
    end = _month(time.time())
    while f"{y:04d}-{m:02d}" <= max(end, months[-1]):  # 送っていない月も 0 として並べる
        key = f"{y:04d}-{m:02d}"
        row: dict[str, Any] = {"month": key}
        for kind, per_month in data.items():
            per = per_month.get(key, {})
            parts = {rooms.label(rid)[0]: per.get(rid, 0) for rid in top if per.get(rid)}
            other = sum(pt for rid, pt in per.items() if rid not in top)
            if other:
                parts["_other"] = other
            row[kind] = {"total": sum(per.values()), "rooms": parts}
        out.append(row)
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return {"months": out, "rooms": [rooms.label(rid)[0] for rid in top],
            "names": {rooms.label(rid)[0]: rooms.label(rid)[1] for rid in top}}


# ------------------------------------------------------------------ 収集の実行 (予定の時刻と管理画面から)
class ResearchJob:
    """指定のルームの情報収集を 1 つずつ実行する (予定の時刻と管理画面の「今すぐ集める」が重ならないように)。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.running = False
        self.progress = ""
        self.started_at: float | None = None
        self.last: dict[str, Any] | None = None  # 前回の結果 {at, rooms, events, requests, failures, reason}

    def state(self) -> dict[str, Any]:
        return {"running": self.running, "progress": self.progress, "started_at": self.started_at, "last": self.last}

    def run(self, state_dir: Path, keys: list[str], reason: str, force: bool = False,
            collector: Collector | None = None) -> dict[str, Any] | None:
        """集める (このスレッドで最後まで)。すでに実行中なら何もせず None。"""
        with self._lock:
            if self.running:
                return None
            self.running, self.progress, self.started_at = True, "", time.time()
        try:
            if not keys:
                result = {"rooms": 0, "events": 0, "requests": 0, "failures": 0}
            else:
                log.info("応援ルーム・調べるルームの情報を集めます (%d ルーム、%s)", len(keys), reason)
                c = collector or Collector(state_dir)
                result = c.collect(keys, force=force, progress=lambda text: setattr(self, "progress", text))
                log.info("情報を集めました: %d ルーム / 新しい貢献ランキング %d 件 / 問い合わせ %d 回%s",
                         result["rooms"], result["events"], result["requests"],
                         f" / 失敗 {result['failures']} 件" if result["failures"] else "")
                forget_analysis()
            self.last = {**result, "at": time.time(), "reason": reason}
            return self.last
        finally:
            self.running, self.progress = False, ""

    def start(self, state_dir: Path, keys: list[str], reason: str, force: bool = False) -> bool:
        """別のスレッドで集め始める。すでに実行中なら False。"""
        if self.running:
            return False
        threading.Thread(target=self.run, args=(state_dir, keys, reason, force), daemon=True, name="research").start()
        return True


JOB = ResearchJob()


def research_keys(cfg: Any) -> list[str]:
    """情報を集めるルーム (応援しているルーム → 調べるルームの順)。"""
    return list(dict.fromkeys([*cfg.support_rooms, *cfg.research_rooms]))
