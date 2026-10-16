"""応援しているルームへギフターを誘導する作戦のための調査 (情報収集と分析)。

集めるもの (どれも SHOWROOM が公開している情報):
- ルームの累計ポイントランキング (上位 30 人の累計ポイントと来場回数) /api/live/summary_ranking
- ルームの過去のイベント /api/rooms/{room_id}/past_events と、開催中のイベント /api/room/event_and_support
- イベントごとの貢献ランキング (ルームごとに上位 100 人) /api/event/contribution_ranking
  (約 9 か月より前のイベントは SHOWROOM が空の一覧を返すので問い合わせない)

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
import threading
import time
import urllib.error
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .research_store import Store, db_path
from .status import JST

log = logging.getLogger("research")

REQUEST_GAP = 1.0  # SHOWROOM への問い合わせの間隔 (秒)
MAX_GAP = 60.0  # 混んでいるときに広げる間隔の上限 (秒)
PAUSE_AFTER = 5  # 続けてこの回数失敗したら、しばらく問い合わせを止める
PAUSE = 3600.0  # 止める時間 (秒)
PAST_EVENT_PAGES = 10  # 過去のイベント一覧を読むページ数の上限 (1 ページ約 30 件)
ROOM_REFRESH = 20 * 3600  # 累計ランキングなどを取り直すまでの時間 (秒)
RANKING_DAYS = 280  # これより前に終わったイベントの貢献ランキングは SHOWROOM が返さないので取らない (日)
RANKING_RETRIES = 3  # 貢献ランキングの取得に続けて失敗したら、それ以上は自動で取り直さない
WEEKDAYS = "月火水木金土日"
LEFT_NAMES = {"退会済みユーザー"}  # SHOWROOM をやめたユーザー (誘導できないので候補に出さない)
ANALYSIS_TTL = 600  # 分析の結果を使い回す時間 (秒)。全ルーム分の集計は重いので、そのたびには数え直さない


# ------------------------------------------------------------------ 問い合わせの間隔
class Throttle:
    """SHOWROOM への問い合わせを 1 本ずつ、間隔を空けて流す (スレッドをまたいで共有する)。

    429 / 5xx / 通信の失敗が続いたら間隔を倍に広げ (MAX_GAP まで)、PAUSE_AFTER 回続いたら PAUSE 秒止める。
    成功するたびに間隔を少しずつ元に戻す。404 (無いルーム・ランキング) は失敗に数えない。
    """

    def __init__(self, gap: float = REQUEST_GAP, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic):
        self.base = self.gap = float(gap)
        self._sleep, self._clock = sleep, clock
        self._lock = threading.Lock()
        self._next = 0.0
        self.errors = 0
        self.paused_until = 0.0
        self.requests = 0

    def set_base(self, gap: float) -> None:
        self.base = max(float(gap), 0.2)
        self.gap = max(self.gap, self.base)

    def state(self) -> dict[str, Any]:
        left = self.paused_until - self._clock()
        return {"gap": round(self.gap, 2), "base": self.base, "errors": self.errors,
                "paused": round(left) if left > 0 else 0, "requests": self.requests}

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
                if e.code == 429 or e.code >= 500:
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

    def _failed(self, why: str) -> None:
        self.errors += 1
        self.gap = min(MAX_GAP, max(self.gap * 2, self.base))
        if self.errors >= PAUSE_AFTER:
            self.paused_until = self._clock() + PAUSE
            self.errors = 0
            log.warning("SHOWROOM への問い合わせが続けて失敗したので %d 分止めます (%s)", PAUSE // 60, why)


THROTTLE = Throttle()


def is_missing(e: BaseException) -> bool:
    return isinstance(e, urllib.error.HTTPError) and e.code == 404


# ------------------------------------------------------------------ 情報収集
def _event_info(e: dict[str, Any]) -> dict[str, Any]:
    url = e.get("event_url") or ""
    key = e.get("event_url_key") or (url.rstrip("/").rsplit("/", 1)[-1] if url else None)
    return {"event_id": e.get("event_id"), "name": e.get("event_name") or str(e.get("event_id")), "key": key,
            "start": e.get("started_at"), "end": e.get("ended_at"), "type": e.get("event_type"),
            "show_ranking": bool(e.get("show_ranking", 1))}


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
        st.set_cumulative(room_id, _rows(self.get(f"/api/live/summary_ranking?room_id={room_id}"), cumulative=True))
        row = st.room(room_id=room_id)
        complete = row is not None and row["collected_at"] is not None  # 前回は最後まで読めた
        known = {e["event_id"] for e in st.room_events(room_id)}
        cutoff = now - RANKING_DAYS * 86400
        events: list[dict[str, Any]] = []
        page: Any = 1
        while page and page <= PAST_EVENT_PAGES:
            data = self.get(f"/api/rooms/{room_id}/past_events?page={page}") or {}
            got = [_event_info(e) for e in data.get("events") or [] if isinstance(e, dict) and e.get("event_id")]
            events += got
            if not got or (complete and all(e["event_id"] in known for e in got)):
                break  # 新しい順なので、ここから先は前に読んだもの
            if all(e["end"] and float(e["end"]) < cutoff for e in got):
                break  # ここから先は貢献ランキングを取れない古いイベント
            page = data.get("next_page")
        current = (self.get(f"/api/room/event_and_support?room_id={room_id}") or {}).get("event")
        if isinstance(current, dict) and current.get("event_id"):
            events.append(_event_info(current))
        st.upsert_events(events)
        st.link((room_id, e["event_id"]) for e in events)
        st.conn.execute("UPDATE rooms SET collected_at=? WHERE room_id=?", (now, int(room_id)))

    def fetch_rankings(self, room_id: int, force: bool = False, current: bool = False) -> int:
        """ルームが参加したイベントの貢献ランキングを取る。current なら開催中のものも (取り直しも) 取る。"""
        now = self._clock()
        cutoff = now - RANKING_DAYS * 86400
        added = 0
        for ev in self.store.room_events(room_id):
            if not ev["show_ranking"] or ev["final"]:
                continue
            if (ev["end"] and float(ev["end"]) < cutoff) or (ev["start"] and float(ev["start"]) > now):
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
        self.store.set_contributions(event_id, room_id, _rows(data), self._clock(), final)
        return True


# ------------------------------------------------------------------ 集めた情報の読み込み
def _event_dict(e: Any, now: float) -> dict[str, Any]:
    current = bool(e["start"] and e["end"] and float(e["start"]) <= now <= float(e["end"]))
    return {"event_id": e["event_id"], "name": e["name"], "key": e["key"], "start": e["start"], "end": e["end"],
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
                (now - 3600, now - RANKING_DAYS * 86400, RANKING_RETRIES)),
            "gifters": s.scalar("SELECT COUNT(*) FROM users"),
            "scan_next": s.meta("scan_next", 1), "scan_end": s.meta("scan_end"), "scan_done_at": s.meta("scan_done_at"),
            "db_mb": round(size / 1e6, 1),
        }


# 終わって 1 時間たち、終わってからはまだ取っていない (取れる期間内の) 貢献ランキング
PENDING_RANKING = """re.final=0 AND (re.ranking_at IS NULL OR re.ranking_at < e.end + 3600) AND e.show_ranking=1
    AND e.end IS NOT NULL AND e.end < ? AND e.end > ? AND re.failures < ?"""


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


def _chunks(items: list[Any], n: int = 500) -> list[list[Any]]:
    return [items[i:i + n] for i in range(0, len(items), n)]


def _slots(store: Store, rooms: _Rooms, rec: _Recorded, uids: list[int]) -> dict[int, dict[int, dict[str, Any]]]:
    """ギフターごと・ルームごとの送ったポイント (記録・累計ランキング・貢献ランキング)。"""
    out: dict[int, dict[int, dict[str, Any]]] = defaultdict(dict)
    for part in _chunks(uids):
        for r in store.all(f"SELECT * FROM user_room WHERE user_id IN ({','.join('?' * len(part))})", part):
            out[r["user_id"]][r["room_id"]] = {"recorded": 0, "cumulative": r["cumulative"] or 0, "visits": r["visits"] or 0,
                                               "events": r["events"] or 0, "cumulative_rank": r["cumulative_rank"]}
    wanted = set(uids)
    for (uid, key), pt in rec.points.items():
        if uid in wanted and pt:
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


def _user_names(store: Store, rec: _Recorded, uids: list[int]) -> dict[int, str]:
    out = {}
    for part in _chunks(uids):
        for r in store.all(f"SELECT user_id, name FROM users WHERE user_id IN ({','.join('?' * len(part))})", part):
            out[r["user_id"]] = r["name"]
    out.update({u: rec.names[u] for u in uids if u in rec.names})
    return out


def _summary(uid: int, name: str | None, slots: dict[int, dict[str, Any]], ev: Any, gifts: list[Any],
             rooms: _Rooms, support: set[int]) -> dict[str, Any]:
    scores = {rid: room_score(v) for rid, v in slots.items()}
    total = sum(scores.values())
    other = {rid: s for rid, s in scores.items() if rid not in support and s > 0}
    top = sorted(other.items(), key=lambda kv: -kv[1])
    h = habits(gifts)
    return {
        "user_id": uid, "name": name or "(名前なし)",
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

        uids = list(dict.fromkeys(fan_ids + cand_ids))
        slots, evs, names = _slots(s, rooms, rec, uids), _event_stats(s, uids), _user_names(s, rec, uids)
        sup = set(sup_ids)
        summary = {u: _summary(u, names.get(u), slots.get(u, {}), evs.get(u), rec.gifts.get(u, []), rooms, sup) for u in uids}
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
                "fans": [summary[u] for u in fan_ids], "fan_count": fan_count, "similar": similar,
                "pairs": pairs, "rooms": room_list, "room_count": room_count, "multi_count": multi_count,
                "gifter_count": gifter_count}


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
        events = [dict(r) for r in s.all("""SELECT c.event_id, e.name AS event, c.room_id, c.point, c.rank, e.end
                                            FROM contributions c LEFT JOIN events e USING(event_id)
                                            WHERE c.user_id=? ORDER BY COALESCE(e.end, 0) DESC""", (uid,))]
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
        return {"user_id": uid, "found": True,
                "summary": _summary(uid, name, slots, _event_stats(s, [uid]).get(uid), gifts, rooms, sup),
                "rooms": room_list, "events": events, "habits": habits(gifts)}


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
