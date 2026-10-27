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

import heapq
import json
import logging
import math
import os
import threading
import sqlite3
import time
import urllib.error
from array import array
from bisect import bisect_left
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator

from . import memory
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
THROTTLE_FILE = "throttle.json"  # 止めた回数・止める時刻・止めた記録を残すファイル (state のフォルダ。起動し直しても、休みを消さないため)
PAST_EVENT_PAGES = 100  # 過去のイベント一覧を読むページ数の上限 (1 ページ約 30 件)
ROOM_REFRESH = 20 * 3600  # 累計ランキングなどを取り直すまでの時間 (秒)
HOLD_RANKINGS = 30  # 全ルームの収集で、貢献ランキングをこの件数までためてから、まとめて保存する (書く量を減らすため)
HOLD_SECONDS = 120.0  # ためておく時間の上限 (秒)
RANKING_RETRIES = 3  # 貢献ランキングの取得に続けて失敗したら、それ以上は自動で取り直さない
ARCHIVE_HOLD = RANKING_RETRIES + 1  # Internet Archive で見つけたルームの貢献ランキング: ルームがあると分かるまで取りに行かない
WEEKDAYS = "月火水木金土日"
LEFT_NAMES = {"退会済みユーザー"}  # SHOWROOM をやめたユーザー (誘導できないので候補に出さない)
ANALYSIS_TTL = 3600  # (2026-10-19 に 10 分から延ばした。全ルーム分はサーバーで数分かかり、そのあいだディスクを読み続けるため) 分析の結果を使い回す時間 (秒)。全ルーム分の集計は重いので、そのたびには数え直さない


# ------------------------------------------------------------------ 問い合わせの間隔
class Busy(RuntimeError):
    """待てる時間のうちに問い合わせの順番が来ない (休んでいる時など)。問い合わせはしていない。"""


class Throttle:
    """SHOWROOM への問い合わせを 1 本ずつ、間隔を空けて流す (スレッドをまたいで共有する)。

    403 / 429 / 5xx / 通信の失敗が続いたら間隔を倍に広げ (MAX_GAP まで)、PAUSE_AFTER 回続いたら止める。
    止める時間は 1 時間・2 時間・4 時間… と、続けて止めるたびに倍にする (MAX_PAUSE まで)。再開したあと
    RECOVER_AFTER 回続けて成功したら 1 時間に戻す (SHOWROOM に止められている時に、何度も試さないように)。
    成功するたびに間隔を少しずつ元に戻す。404 (無いルーム・ランキング) は失敗に数えない。
    """

    def __init__(self, gap: float = REQUEST_GAP, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic, wall: Callable[[], float] = time.time, name: str = "SHOWROOM"):
        self.name = name  # ログに出す問い合わせ先
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
        self._file: Path | None = None  # 止めた回数と休みを残すファイル (keep で決める。None なら残さない)

    def keep(self, state_dir: Path) -> None:
        """止めた回数・休みの終わり・止めた記録を state_dir に残し、前に残した分を読む (keeper の起動の時に 1 回)。

        前は止めた回数と休みがメモリの中だけで、起動し直すと 0 に戻った。マージのたびに自動で起動し直すので、
        SHOWROOM に止められて休んでいる時に起動し直すと、休みが消えてすぐに問い合わせ、止められる時間を延ばすおそれがあった
        (全体の見直し 2026-10-27 の S2)。休みの終わりは時計の時刻で残し、読む時に今の時計からの残りに直す。"""
        self._file = Path(state_dir) / THROTTLE_FILE
        try:
            saved = json.loads(self._file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(saved, dict):
            return
        with self._lock:
            try:
                self.pauses = max(self.pauses, int(saved.get("pauses") or 0))
                self.pause_log = [p for p in saved.get("pause_log") or [] if isinstance(p, dict)][:PAUSE_LOG] or self.pause_log
                left = float(saved.get("until") or 0) - self._wall()
            except (TypeError, ValueError):
                return
            if left > 0:
                self.paused_until = max(self.paused_until, self._clock() + min(left, MAX_PAUSE))
                log.warning("起動し直す前に始めた SHOWROOM への問い合わせの休みを続けます (あと %.0f 分。続けて %d 回目)",
                            left / 60, self.pauses)

    def _save(self) -> None:
        """止めた回数と休みをファイルに残す (止めた時と、止める時間を戻した時だけ。ふだんの問い合わせでは書かない)。"""
        if self._file is None:
            return
        left = self.paused_until - self._clock()
        data = {"pauses": self.pauses, "until": self._wall() + left if left > 0 else 0, "pause_log": self.pause_log}
        try:
            tmp = self._file.with_name(self._file.name + ".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self._file)
        except OSError as e:  # 残せなくても、問い合わせは止めない
            log.warning("問い合わせの休みをファイルに残せませんでした: %s", e)

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

    def call(self, get: Callable[[str], Any], path: str, max_wait: float | None = None, soft: bool = False) -> Any:
        """max_wait: 順番待ちと休みを合わせて、この秒数より長く待つなら問い合わせずに Busy を出す (None は休みが終わるまで待つ)。
        soft: タイムアウトなどの通信の失敗を、止めるまでの失敗に数えない (ギフトの記録など、収集でない問い合わせ。
        SHOWROOM が断った 403 / 429 / 5xx は数える)。"""
        if not self._lock.acquire(timeout=-1 if max_wait is None else max_wait):
            raise Busy(f"{self.name} への問い合わせの順番が {max_wait:g} 秒たっても来ません")
        try:
            now = self._clock()
            wait = max(self._next, self.paused_until) - now
            if max_wait is not None and wait > max_wait:
                raise Busy(f"{self.name} への問い合わせを休んでいます" if self.paused_until > now else
                           f"{self.name} への問い合わせの間隔が {wait:.0f} 秒に広がっています")
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
                if not soft:
                    self._failed(str(e).splitlines()[0] if str(e) else type(e).__name__)
                raise
            else:
                self._ok()
                return data
            finally:
                self._next = self._clock() + self.gap
        finally:
            self._lock.release()

    def _ok(self) -> None:
        self.errors = 0
        self.gap = max(self.base, self.gap * 0.8)
        self.ok_streak += 1
        if self.pauses and self.ok_streak >= RECOVER_AFTER:
            log.info("SHOWROOM への問い合わせが続けて成功したので、次に止める時間を %d 分に戻します", PAUSE // 60)
            self.pauses = 0
            self._save()

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
            self._save()
            text = f"{length / 3600:g} 時間" if length >= 3600 else f"{length / 60:.0f} 分"
            if self.name == "SHOWROOM":
                log.warning("SHOWROOM への問い合わせが続けて失敗したので、全ルームの収集などを %s止めます (続けて %d 回目。%s)。"
                            "視聴とログインの維持は続けます", text, self.pauses, why)
            else:
                log.warning("%s への問い合わせが続けて失敗したので、%s止めます (続けて %d 回目。%s)", self.name, text, self.pauses, why)


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


def page_text(html: str) -> str:
    """HTML のページを、読める文章だけにする (スクリプト・スタイルを除き、行ごとに分ける)。"""
    import html as _html
    import re

    s = re.sub(r"(?is)<(script|style|noscript|svg)\b.*?</\1>", "", html)
    s = re.sub(r"(?i)<br\s*/?>|</(p|div|li|h[1-6]|tr|dt|dd|section|table)>", "\n", s)
    s = _html.unescape(re.sub(r"<[^>]+>", "\n", s))
    lines = [" ".join(x.split()) for x in s.splitlines()]
    return "\n".join(x for x in lines if x)


def _get_html(path: str, cookie: str | None = None) -> str:
    import urllib.request

    from .api import BASE_URL

    headers = {"User-Agent": "showroom-keeper", "Accept-Language": "ja"}
    if cookie:
        headers["Cookie"] = cookie
    req = urllib.request.Request(f"{BASE_URL}{path}", headers=headers)
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.read().decode("utf-8", "replace")


def _rows(data: Any, cumulative: bool = False) -> list[dict[str, Any]]:
    out = []
    for r in (data or {}).get("ranking") or []:
        if isinstance(r, dict) and r.get("user_id"):
            row = {"user_id": int(r["user_id"]), "name": r.get("name"), "point": int(r.get("point") or 0), "rank": r.get("rank")}
            if cumulative:
                row["visits"] = int(r.get("visit_count") or 0)
            out.append(row)
    return out


def _ranking_rows(data: Any) -> list[dict[str, Any]] | None:
    """貢献ランキングの答えの行。形が思っていたものでなければ None (辞書でない・ranking が配列でない・user_id か point の無い行や
    数字でない値が混じる)。point が無い行を 0 pt とすると、本当の 0 と分からないものが混ざるので、形の違う答えとして扱う。"""
    if not isinstance(data, dict) or not isinstance(data.get("ranking"), list):
        return None
    if not all(isinstance(r, dict) and r.get("user_id") and r.get("point") is not None for r in data["ranking"]):
        return None
    try:
        return _rows(data)
    except (TypeError, ValueError):
        return None


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
        # 貢献ランキングをためてから保存する時の、ためている分 (None なら 1 件ずつすぐ保存する)。全ルームの収集だけが使う
        self.held: list[tuple[int, int, list[dict[str, Any]], bool]] | None = None
        self._held_at = 0.0

    @property
    def store(self) -> Store:
        if self._store is None:
            self._store = Store(self.state_dir)
        return self._store

    def close(self) -> None:
        if self._store is not None:
            try:
                self.flush_rankings()
            finally:
                self._store.close()
                self._store = None

    def hold_rankings(self) -> None:
        """これから取る貢献ランキングを、HOLD_RANKINGS 件か HOLD_SECONDS 秒ぶんためてから、まとめて保存する。

        1 件ずつ保存すると、100 人分の人で引く表と索引のページを毎回書き直す (サーバーでは 1 件 500 か所ほど)。
        まとめると、同じページは 1 回分で済む。ためている間に止まった分は、保存していないので、あとで取り直す。
        """
        if self.held is None:
            self.held = []

    def flush_rankings(self, due_only: bool = False) -> None:
        """ためている貢献ランキングを保存する。due_only なら、ためる件数か時間を過ぎた時だけ。"""
        if not self.held:
            return
        if due_only and len(self.held) < HOLD_RANKINGS and self._clock() - self._held_at < HOLD_SECONDS:
            return
        items, self.held = self.held, []
        self.store.set_contributions_many(items, self._clock())

    def _save_ranking(self, event_id: int, room_id: int, rows: list[dict[str, Any]], final: bool) -> None:
        if self.held is None:
            self.store.set_contributions(event_id, room_id, rows, self._clock(), final)
            return
        if not self.held:
            self._held_at = self._clock()
        self.held.append((event_id, room_id, rows, final))
        self.flush_rankings(due_only=True)

    def get(self, path: str) -> Any:
        self.requests += 1
        return self.throttle.call(self._get, path)

    def get_page(self, path: str) -> str:
        """HTML のページ (イベントのルールなど) を読む。問い合わせの間隔は get と同じ。"""
        self.requests += 1
        return self.throttle.call(self._get_page, path)

    _get_page = staticmethod(_get_html)

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
                self.store.ranking_tried(event_id, room_id, self._clock(), "error")
                log.warning("イベント %s (ルーム %s) の貢献ランキングを取れませんでした: %s", event_id, room_id,
                            str(e).splitlines()[0] if str(e) else type(e).__name__)
                return False
            return self._ranking_gone(event_id, room_id, final)
        ev = data.get("event") if isinstance(data, dict) else None
        if isinstance(ev, dict) and ev.get("ended_at"):
            # 番号で読んだ古いイベントは、ここで初めて日付が分かる (「順位を公開しない」はここでは見ない)
            self.store.upsert_events([{**_event_info({**ev, "event_id": event_id}), "show_ranking": None}])
            self.store.upsert_event_info([{**ev, "event_id": event_id}], self._clock())
        rows = _ranking_rows(data)
        if rows is None:
            # 思っていた形でない答え (辞書でない・ranking が配列でない・おかしな行が混じる): 前の有無にかかわらず、何も書き換えない
            # (一時の失敗と同じに数え、あとでもう一度読む。RANKING_RETRIES 回で読むのをやめ、try_status=shape で理由が分かる)
            return self._ranking_not_taken(event_id, room_id, "shape", "思っていた形ではありませんでした")
        if not rows:
            if self.store.one("SELECT 1 FROM contributions WHERE event_id=? AND room_id=? AND guessed IS NULL LIMIT 1",
                              (event_id, room_id)):
                # 前は人がいたのに空の答え: 前に取れた分を空で上書きしない
                return self._ranking_not_taken(event_id, room_id, "200-empty", "空でした")
            if self.store.one("SELECT 1 FROM contributions WHERE event_id=? AND room_id=? AND guessed IS NOT NULL",
                              (event_id, room_id)):
                # SHOWROOM に残っていなかった: 名前で推測した分 (Internet Archive) を消さずに残し、取り直さない印だけ付ける
                # (ranking_at・failures は前のまま。取れたことにはしない)
                self.store.conn.execute("UPDATE room_events SET final=MAX(final, ?) WHERE event_id=? AND room_id=?",
                                        (int(final), event_id, room_id))
                self.store.ranking_tried(event_id, room_id, self._clock(), "200-empty")
                return True
        self._save_ranking(event_id, room_id, rows, final)
        # ok_at (確かめて取れた) は、形が合い、1 人以上いた時だけ。空の答えは、どこまでの一覧かを確かめられないので「200-empty」
        # (正常な空とは決めない。採点では分からない回として外す)
        status = "200" if rows else "200-empty"
        self.store.ranking_tried(event_id, room_id, self._clock(), status, ok=status == "200")
        return True

    def _ranking_not_taken(self, event_id: int, room_id: int, status: str, what: str) -> bool:
        """答えは返ったが使えなかった。行・ranking_at・final は変えず、失敗として数える。"""
        self.failures += 1
        self.store.ranking_failed(event_id, room_id)
        self.store.ranking_tried(event_id, room_id, self._clock(), status)
        log.warning("イベント %s (ルーム %s) の貢献ランキングが%s。前に取れた分を残します", event_id, room_id, what)
        return False

    def _has_ranking(self, event_id: int, room_id: int) -> bool:
        return self.store.one("SELECT 1 FROM contributions WHERE event_id=? AND room_id=? LIMIT 1", (event_id, room_id)) is not None

    def _ranking_gone(self, event_id: int, room_id: int, final: bool) -> bool:
        """SHOWROOM にもうランキングが無かった (404)。取り直さないように終わった印を付けるが、前に取れた分は消さない。

        取れていた回は ranking_at・c_* を前のままにする (gone_at で 404 だったと分かる)。何も無かった回は、前と同じく空として入れる。
        終わっていないイベント (final が False) は、前と同じく終わった印を付けない (あとでもう一度読む)。
        """
        now = self._clock()
        if self._has_ranking(event_id, room_id):
            self.store.conn.execute("UPDATE room_events SET final=MAX(final, ?), failures=0, ranking_at=COALESCE(ranking_at, ?) "
                                    "WHERE event_id=? AND room_id=?", (int(final), now, event_id, room_id))
        else:
            self._save_ranking(event_id, room_id, [], final)
        self.store.ranking_tried(event_id, room_id, now, "404", gone=True)
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
            # 推移の表は数百万行になるので、行の数は数えずに番号の最大 (おおよその数) を使う
            "progress_rows": s.scalar("SELECT MAX(rowid) FROM event_progress", default=0),
            "progress_events": s.scalar("SELECT COUNT(*) FROM events WHERE progress_at IS NOT NULL"),
            "db_mb": round(size / 1e6, 1),
        }


# 全ルームの収集で取らないルーム: ルーム番号の調べで見つけた、フォロワー 0 人で配信の記録も無いもの (ギフターがいない)。
# 集まり具合の「見つけたルームのうち集めた割合」でも、分子・分母の両方から外す
SKIPPED_ROOM = "found_via = 'scan' AND COALESCE(followers, 0) = 0 AND live_at IS NULL"

# 終わって 1 時間たち、終わってからはまだ取っていない貢献ランキング (ポイントが 0 のルームは誰も送っていないので除く)
# イベントの正式な結果が出る時刻 (終わった次の日の正午。日本時間) に、1 時間の余裕を足したもの。SHOWROOM は、不正なポイント
# (複数のアカウントなど) を引いてから正式な結果を出すので、終わった直後とポイント・順位が変わることがある (公式のよくある質問)
CONFIRMED_SQL = "((CAST((e.end + 32400) / 86400 AS INTEGER) + 1) * 86400 + 43200 - 32400 + 3600)"
# 貢献ランキングを取るのを待っているもの。? は (今 - 3600, 失敗の上限)。正式な結果が出てから取る
PENDING_RANKING = f"""re.final=0 AND (re.ranking_at IS NULL OR re.ranking_at < e.end + 3600) AND e.show_ranking=1
    AND e.end IS NOT NULL AND {CONFIRMED_SQL} <= ? + 3600 AND re.failures < ? AND (re.point IS NULL OR re.point > 0)"""


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


# 記録したギフト (カタログの単価 × 個数。倍率未反映) を、累計ランキング・貢献ランキングとくらべる時の物差し。
# SHOWROOM の決まり (docs/SHOWROOM公式の決まり.md 2-2) で、ルームの累計ランキングでは有料ギフトが 2.5 倍 (2021-03-01 から)。
# 無料の星はそのまま。まとめ投げの 1.2 倍・連打のボーナスはイベントの pt だけなので掛けない (「少なくとも」の値になる)。
# レインボースターも 2.5 倍だが、カタログでは無料と見分けていないので、そのままで数える (少なめに出る)
PAID_RANK_RATE = 2.5


def ranked_points(points: int, free: int) -> int:
    """記録したギフトの元 pt を、累計ランキングと同じ物差し (有料 × 2.5 + 無料) にした値 (推定。切り捨て)。"""
    paid = max(0, (points or 0) - (free or 0))
    return int(paid * PAID_RANK_RATE) + min(points or 0, free or 0)


def room_score(slot: dict[str, Any]) -> int:
    """そのルームに送った (少なくとも) ポイント。累計ランキングはイベントの分も含むので、合計せず大きい方を使う。
    記録したギフトは、累計ランキングと同じ物差しにした値 (recorded_ranked) でくらべる (元 pt のままだと、倍率の分だけ小さく出る)。"""
    return max(slot["cumulative"], slot["events"], slot.get("recorded_ranked", slot["recorded"]))


class _Recorded:
    """記録したギフトを、ギフターごとにまとめたもの。

    全員の合計 (人 × ルームの pt・そのうち無料・最初の時刻・最後の名前) は送り主の表 (gift_senders。収集の合間に 10 分ごとに
    ファイルの新しい分だけ足し、読む前にもそのあとの新しい分だけ足す) から読む。1 人のギフト 1 件ずつ (gifts) は、その人が送ったルームのファイルだけを読む。
    前は、ページを開くたびにファイル全部を読み直していた (789 ルーム・66MB で 40 秒・メモリ 420MB。2026-10-19)。
    user を渡すと、その人と、その人が送ったルームの人の分だけを読む (ギフターのページ)。
    """

    def __init__(self, state_dir: Path, store: Store | None = None, user: int | None = None, users: list[int] | None = None,
                 catch_up: bool = True):
        self.state_dir = Path(state_dir)
        self.by_user: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        self.points = _PairPoints(self.by_user)  # (人, ルーム) → pt (by_user と同じものを、別の形で見る。メモリを 2 重に使わない)
        self.free: dict[tuple[int, str], int] = defaultdict(int)  # そのうち無料のギフトのポイント (無料の分がある組だけ)
        self.unknown: dict[tuple[int, str], int] = {}  # ギフトの一覧に無かったギフトの回数 (ある組だけ。ポイントを 0 で数えている)
        self.names: dict[int, str] = {}
        self.first_at: dict[int, float] = {}
        from .explore import update_all_gift_senders

        s = store if store is not None else Store(state_dir)
        try:
            if catch_up:
                update_all_gift_senders(state_dir, s)  # 前に足したあとの新しい分だけ (ふだんは 10 分ごとに収集の合間に足してある)
            else:
                from . import catchup

                catchup.soon(state_dir)  # ページを開く時: 書き込みの順番を待たず、前に足した分で数える (新しい分は裏で足す)
            if users is not None:
                # 決まった人たちの分だけ (気になるギフターの一覧。前は全員の分を読んでいた。1 回 平均 28 秒・1 時間に 12 回)
                rows = [r for part in _chunks(list(users)) for r in s.all(
                    f"""SELECT room_key, user_id, points, free_points, first_at, last_at, name, unknown FROM gift_senders
                        WHERE user_id IN ({','.join('?' * len(part))})""", part)]
            elif user is None:
                rows = s.all("SELECT room_key, user_id, points, free_points, first_at, last_at, name, unknown FROM gift_senders")
            else:
                keys = [r[0] for r in s.all("SELECT room_key FROM gift_senders WHERE user_id=?", (int(user),))]
                rows = [r for k in keys for r in s.all("""SELECT room_key, user_id, points, free_points, first_at, last_at, name, unknown
                                                          FROM gift_senders WHERE room_key=?""", (k,))]
        finally:
            if store is None:
                s.close()
        last: dict[int, float] = {}
        for r in rows:
            uid, key = int(r["user_id"]), r["room_key"]
            if r["free_points"]:
                self.free[(uid, key)] += r["free_points"]
            if r["unknown"]:
                self.unknown[(uid, key)] = r["unknown"]
            self.by_user[uid][key] += r["points"] or 0
            if r["first_at"] is not None and r["first_at"] < self.first_at.get(uid, float("inf")):
                self.first_at[uid] = r["first_at"]
            if r["name"] and (r["last_at"] or 0) >= last.get(uid, -1):
                self.names[uid], last[uid] = r["name"], r["last_at"] or 0
        self.gifts = _RecordedGifts(self)

    def ranked(self, uid: int, key: str) -> int:
        """(人, ルーム) の記録したギフトを、累計ランキングと同じ物差しにした値 (ranked_points)。"""
        return ranked_points(self.points.get((uid, key), 0), self.free.get((uid, key), 0))


class _PairPoints:
    """by_user ({人: {ルーム: pt}}) を、{(人, ルーム): pt} のように読むもの。"""

    def __init__(self, by_user: dict[int, dict[str, int]]):
        self._by_user = by_user

    def get(self, pair: tuple[int, str], default: Any = None) -> Any:
        u = self._by_user.get(pair[0])
        return u.get(pair[1], default) if u is not None else default

    def items(self) -> Any:
        return (((u, k), pt) for u, rooms in self._by_user.items() for k, pt in rooms.items())

    def __iter__(self) -> Any:
        return ((u, k) for u, rooms in self._by_user.items() for k in rooms)

    def __bool__(self) -> bool:
        return bool(self._by_user)


class _RecordedGifts:
    """1 人のギフト 1 件ずつ [(時刻, pt, ルーム)] を、ギフト 1 件ずつの表 (gift_events) から人で引く (dict の get のように使う)。

    前は、その人が送ったルームの記録のファイルを読み直していた (送る量の多い人のページで 9.2 秒。2026-10-19 に Cursor が測った)。
    表は、送り主の表 (gift_senders) と一緒に足すので、_Recorded を作った時には新しい分まで入っている。
    """

    def __init__(self, rec: _Recorded):
        self.rec = rec
        self._store: Store | None = None
        self._catalog: dict[int, dict[str, Any]] | None = None
        self._done: dict[int, list[tuple[float, int, str]]] = {}

    def _s(self) -> Store:
        if self._store is None:  # _Recorded の接続は閉じていることがあるので、自分の接続を使う
            self._store = Store(self.rec.state_dir)
        return self._store

    def close(self) -> None:
        if self._store is not None:
            self._store.close()
            self._store = None

    def _points(self, gift_id: Any, num: int) -> int:
        from .listing import _points, gift_catalog

        if self._catalog is None:
            self._catalog = gift_catalog(self.rec.state_dir, sorted({k for _, k in self.rec.points}))
        return _points(self._catalog, {"gift_id": gift_id, "num": num})[0]

    def get(self, uid: int, default: Any = None) -> list[tuple[float, int, str]]:
        uid = int(uid)
        if uid not in self._done:
            rooms = self.rec.by_user.get(uid, {})
            rows = self._s().conn.execute("SELECT at, gift_id, num, room_key FROM gift_events WHERE user_id=? ORDER BY at",
                                          (uid,)).fetchall() if rooms else []
            self._done[uid] = [(at, self._points(gid, n), key) for at, gid, n, key in rows if key in rooms]
        return self._done[uid]

    def room(self, key: str) -> list[tuple[float, int]]:
        """そのルームの、記録した全員のギフト [(時刻, 人)]。"""
        return [(r[0], r[1]) for r in self._s().conn.execute("SELECT at, user_id FROM gift_events WHERE room_key=? ORDER BY at", (key,))]


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
        # イベントごとのいちばん大きいユーザー ID は event_max_user に持っている (貢献ランキングを保存する時と、前の分は収集の合間に作る)
        seen = [(r[0], r[1]) for r in s.all("""SELECT e.end, m.uid FROM event_max_user m JOIN events e USING(event_id)
                                               WHERE m.uid IS NOT NULL AND e.end IS NOT NULL""")]
        seen += [(at, uid) for uid, at in rec.first_at.items()]
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


ANALYSIS_CACHE_KB = 16384  # 分析の接続の作業用のメモリ (KB。終われば接続ごと戻る。user_room は順に 1 回読むだけなので、大きくしても速くならない。2026-10-20 に 64MB から下げた)
SUMMARY_STEP = 50  # 分析で、ルームごとの内訳をまとめて読む人の数


def _chunks(items: list[Any], n: int = 500) -> list[list[Any]]:
    return [items[i:i + n] for i in range(0, len(items), n)]


def _slots(store: Store, rooms: _Rooms, rec: _Recorded, uids: list[int]) -> dict[int, dict[int, dict[str, Any]]]:
    """ギフターごと・ルームごとの送ったポイント (記録・累計ランキング・貢献ランキング)。"""
    out: dict[int, dict[int, dict[str, Any]]] = defaultdict(dict)
    for part in _chunks(uids):
        for r in store.all(f"SELECT * FROM user_room WHERE user_id IN ({','.join('?' * len(part))})", part):
            out[r["user_id"]][r["room_id"]] = {"recorded": 0, "recorded_ranked": 0, "cumulative": r["cumulative"] or 0,
                                               "visits": r["visits"] or 0, "events": r["events"] or 0,
                                               "cumulative_rank": r["cumulative_rank"]}
    for uid in uids:
        for key, pt in rec.by_user.get(uid, {}).items():
            if not pt:
                continue
            slot = out[uid].setdefault(rooms.id(key), {"recorded": 0, "recorded_ranked": 0, "cumulative": 0, "visits": 0, "events": 0,
                                                       "cumulative_rank": None})
            slot["recorded"] += pt  # 元 pt (倍率未反映。表示用)
            slot["recorded_ranked"] += rec.ranked(uid, key)  # 累計ランキングの物差し (くらべる用)
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
    part = _summary_part(slots, ev, gifts, support)
    return _summary_from(uid, name, part, {rid: room_score(v) for rid, v in slots.items() if rid in support}, rooms, support, signup)


SUMMARY_TOP = 50  # 人ごとのまとめに残す、送ったポイントの多いルームの数 (見るルームを除いて上位 3 つを出すため)


def _summary_part(slots: dict[int, dict[str, Any]], ev: Any, gifts: list[Any], support: set[int] = frozenset()) -> dict[str, Any]:
    """1 人のまとめのうち、見るルームに関係ない分 (使い回せる分)。"""
    scores = {rid: room_score(v) for rid, v in slots.items()}
    total = sum(scores.values())
    top = sorted(((rid, s) for rid, s in scores.items() if s > 0), key=lambda kv: -kv[1])
    # 上位 SUMMARY_TOP ルームを残す (ほかの見るルームでも、それを除いた上位 3 つを出せるように)。
    # 今の見るルームを除いた上位 3 つが入りきらなければ、それも足す
    keep = top[:SUMMARY_TOP]
    short = 3 - sum(1 for rid, _ in keep if rid not in support)
    if short > 0:
        keep += [kv for kv in top[SUMMARY_TOP:] if kv[0] not in support][:short]
    h = habits(gifts)
    return {
        "total": total, "room_count": sum(1 for s in scores.values() if s > 0),
        "focus": round(max(scores.values()) / total, 2) if total else None,
        "top": [[rid, s] for rid, s in keep],
        "event_count": ev["n"] if ev else 0,
        "event_points": ev["p"] if ev else 0,
        "best_rank": ev["best"] if ev else None,
        "visits": max((v["visits"] for v in slots.values()), default=0),
        "habits": None if h is None else {k: h[k] for k in ("times", "top_weekday", "top_hours", "avg_point", "last_at")},
        "sources": sorted({src for v in slots.values() for src in ("recorded", "cumulative", "events") if v[src]}),
    }


def _summary_from(uid: int, name: str | None, part: dict[str, Any], sup_scores: dict[int, int], rooms: _Rooms, support: set[int],
                  signup: tuple[float, float, float] | None = None) -> dict[str, Any]:
    """使い回せる分 (part) に、見るルームの分 (sup_scores: 見るルーム → その人が送ったポイント) を当てはめる。"""
    support_points = sum(sup_scores.values())
    top = [(rid, s) for rid, s in part["top"] if rid not in support][:3]
    return {
        "user_id": uid, "name": name or "(名前なし)",
        # 推測した登録時期と、その前後の目印 (この間に登録したと考えられる)
        "signup_at": signup and signup[0], "signup_from": signup and signup[1], "signup_to": signup and signup[2],
        "support_points": support_points,
        "other_points": part["total"] - support_points, "total_points": part["total"],
        "room_count": part["room_count"],
        # いちばん多く送るルームの割合 (1 に近いほど 1 つのルームに集中している)
        "focus": part["focus"],
        "top_rooms": [{"key": rooms.label(rid)[0], "name": rooms.label(rid)[1], "points": s} for rid, s in top],
        "event_count": part["event_count"], "event_points": part["event_points"], "best_rank": part["best_rank"],
        "visits": part["visits"], "habits": part["habits"], "sources": part["sources"],
    }


SUMMARY_TTL = 6 * 3600  # 分析で、人ごとのまとめ (見るルームに関係ない分) を使い回す時間 (秒)


class _SummaryCache:
    """分析で出す人 (1,700 人ほど) の、見るルームに関係ないまとめを SUMMARY_TTL 秒使い回す (cache の小さな DB)。

    前は 1 時間ごとの数え直しのたびに、全員の user_room・貢献ランキング・記録したギフトを読み直していて、
    サーバーで見るルームごとの集計が 342 秒 (そのうちディスクから読む所が 232 秒。2026-10-21 に Cursor が測った)。
    同じ人は数え直しても多くが同じなので、作ってから SUMMARY_TTL 秒たった人だけ読み直す。
    """

    def __init__(self, state_dir: Path):
        path = Path(state_dir) / "cache" / "analysis-summary.db"
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), timeout=30, isolation_level=None)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("CREATE TABLE IF NOT EXISTS s (user_id INTEGER PRIMARY KEY, at REAL, data TEXT)")

    def get(self, uids: list[int], now: float) -> dict[int, dict[str, Any]]:
        out = {}
        for part in _chunks(sorted(uids)):
            for uid, data in self.conn.execute(f"SELECT user_id, data FROM s WHERE user_id IN ({','.join('?' * len(part))}) AND at > ?",
                                               (*part, now - SUMMARY_TTL)):
                out[uid] = json.loads(data)
        return out

    def put(self, parts: dict[int, dict[str, Any]], now: float) -> None:
        self.conn.execute("BEGIN")
        try:
            self.conn.execute("DELETE FROM s WHERE at <= ?", (now - SUMMARY_TTL,))
            self.conn.executemany("INSERT OR REPLACE INTO s VALUES (?, ?, ?)",
                                  [(uid, now, json.dumps(p, ensure_ascii=False)) for uid, p in sorted(parts.items())])
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        self.conn.execute("COMMIT")

    def close(self) -> None:
        self.conn.close()


_analysis: dict[Any, tuple[float, dict[str, Any]]] = {}  # 見るルームごとの結果 (土台を数えた時刻, 結果)
_bases: dict[str, list[Any]] = {}  # 土台 (見るルームに関係ない、全ルーム分の重い集計): state_dir → [確かめた時刻, 土台]
_analysis_lock = threading.Lock()  # 土台を同時に何本も数えない
_view_lock = threading.Lock()  # 見るルームごとの集計も 1 本ずつ
_last_view: dict[str, tuple[list[str], int, Any]] = {}  # いちばん後に開いた見るルーム: state_dir → (見るルーム, 人数, 結果の鍵)
_analysis_running: set[Any] = set()
_analysis_guard = threading.Lock()
SIGNUP_BASE_LIMIT = SIGNUP_YEAR_LIMIT + 400  # 土台に残す、登録した年ごとの上位の人数 (見るルームのギフターを除いても足りるように)


def forget_analysis() -> None:
    """集め終わったときなど: 次の analyze で数え直す (それまでは前の結果を出し、裏で数え直す)。"""
    for b in _bases.values():
        b[0] = 0.0


def _analysis_path(state_dir: Path, key: Any) -> Path:
    import hashlib

    return Path(state_dir) / "cache" / f"analysis-{hashlib.sha1(repr(key[1:]).encode()).hexdigest()[:12]}.json"


def _base_path(state_dir: Path) -> Path:
    return Path(state_dir) / "cache" / "analysis-base.json"


def _read_json(path: Path) -> Any:
    import json

    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_json(path: Path, data: Any) -> None:
    import json
    import os

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except (OSError, TypeError, ValueError) as e:  # 残せなくても、この起動の間は覚えている
        log.debug("分析の結果を残せませんでした: %s", e)


def _load_analysis(state_dir: Path, key: Any) -> tuple[float, dict[str, Any]] | None:
    """起動し直したあと: 前に数えた結果をファイルから読む (古ければ、それを出して裏で数え直す)。"""
    result = _read_json(_analysis_path(state_dir, key))
    try:
        return float(result["base_at"]), result
    except (KeyError, TypeError, ValueError):
        return None


def _get_base(state_dir: Path) -> list[Any] | None:
    """いちばん新しい土台 ([確かめた時刻, 土台])。起動し直したあとは、ファイルに残したものを使う。"""
    sd = str(Path(state_dir).resolve())
    hit = _bases.get(sd)
    if hit is None:
        base = _read_json(_base_path(state_dir))
        if not isinstance(base, dict) or not (Path(state_dir) / "cache" / str(base.get("db", ""))).is_file():
            return None
        hit = _bases[sd] = [float(base["at"]), base]
    return hit


def analyze(state_dir: Path, support: list[str], limit: int = 300, max_age: float = ANALYSIS_TTL) -> dict[str, Any]:
    """集めた情報をギフターとルームの両方から分析する。

    見るルーム (support) を選べば、それを基準に誘導の候補・そのルームのギフター・似ているルームを出す。
    無ければ (mode "all")、ギフター一覧 (分かったポイントの多い順)・ルーム同士の重なり・ルームごとのまとめを出す。

    2 段に分けて数える (2026-10-20)。
    - 土台: 全ルームの「誰がどのルームにいくら送ったか」を全部読む重い集計 (サーバーで数分)。見るルームに関係ないので、
      1 つだけ作って max_age 秒使い回す。古くなったら前の土台をすぐ使い、裏で数え直す。結果はファイルにも残す。
    - 見るルームごとの集計: 土台に、そのルームのギフター (索引で読む) を当てはめるだけの軽い集計。
    前は見るルームの組み合わせごとに全部を読み直していて、見るルームを切り替えるたびに数分かかっていた。
    max_age=0 (「集計し直す」) の時だけ、土台を数え終わるまで待つ。
    """
    sd = Path(state_dir)
    key = (str(sd.resolve()), tuple(support), limit)
    if max_age <= 0:
        base = _run_base(sd, limit, max_age)
    else:
        b = _get_base(sd)
        if b is None:
            base = _run_base(sd, limit, max_age)
        else:
            base = b[1]
            if time.time() - b[0] >= max_age:
                with _analysis_guard:
                    # メモリの空きが少ない時 (視聴のブラウザと重なった時) は数え直さず、前の結果を出す (次に開いた時にまた確かめる)
                    if sd.resolve() not in _analysis_running and not memory.tight():
                        _analysis_running.add(sd.resolve())
                        threading.Thread(target=_refresh_analysis, args=(sd, limit), name="analysis", daemon=True).start()
                return {**_view(sd, base, support, limit, key), "stale": True}
    return _view(sd, base, support, limit, key)


def _view(state_dir: Path, base: dict[str, Any], support: list[str], limit: int, key: Any) -> dict[str, Any]:
    """土台に見るルームを当てはめた結果 (同じ土台から作ったものがあれば、それを使う)。"""
    _last_view[str(Path(state_dir).resolve())] = (list(support), limit, key)
    hit = _analysis.get(key)
    if hit is None:
        hit = _load_analysis(state_dir, key)
        if hit is not None:
            _analysis[key] = hit
    if hit and hit[0] == base["at"]:
        return hit[1]
    with _view_lock:
        hit = _analysis.get(key)
        if hit and hit[0] == base["at"]:  # 待っている間に、ほかが作り終えた
            return hit[1]
        started = time.time()
        result = _analyze_view(state_dir, base, support, limit)
        result["base_at"] = base["at"]
        result["analyzed_at"] = base["at"]
        result["analysis_seconds"] = base["seconds"]
        result["view_seconds"] = round(time.time() - started, 1)
        _analysis[key] = (base["at"], result)
        _write_json(_analysis_path(state_dir, key), result)
    memory.release()
    return result


def _refresh_analysis(state_dir: Path, limit: int) -> None:
    try:
        base = _run_base(state_dir, limit, ANALYSIS_TTL)
        # いちばん後に開いた見るルームの集計も、続けて作っておく (数え直したあと、最初に開いた人が
        # 見るルームの集計を待たないように。サーバーで 33 秒。2026-10-20 に Cursor が測った)
        last = _last_view.get(str(Path(state_dir).resolve()))
        if last is not None and not memory.tight():
            _view(state_dir, base, last[0], last[1], last[2])
    except Exception as e:  # noqa: BLE001 - 数え直せなくても、前の結果を使い続ける
        log.warning("分析を数え直せませんでした: %s", e)
    finally:
        with _analysis_guard:
            _analysis_running.discard(Path(state_dir).resolve())


def _run_base(state_dir: Path, limit: int, max_age: float) -> dict[str, Any]:
    with _analysis_lock:  # 同時に何本も数えない
        b = _get_base(state_dir)
        if b and max_age > 0 and time.time() - b[0] < max_age:  # 待っている間に、ほかが数え終えた
            return b[1]
        started = time.time()
        with memory.heavy():
            base = _analyze_base(state_dir, limit)
        base["at"] = time.time()
        base["seconds"] = round(base["at"] - started, 1)
        cache = Path(state_dir) / "cache"
        _write_json(_base_path(state_dir), base)
        _bases[str(Path(state_dir).resolve())] = [base["at"], base]
        for old in cache.glob("analysis-base-*.db"):  # 前の土台 (読んでいる最中でも、消してよい。読み終わるまでは残る)
            if old.name != base["db"]:
                try:
                    old.unlink()
                except OSError:
                    pass
    memory.release()  # 数えるのに使った一時のメモリを OS に返す (数十 MB 増えたままにしない)
    return base


SCAN_CHUNK = 50000  # 土台で user_room を一度に読む行数 (読むたびに読み込みを終えて、書き込み待ち (WAL) を本体に移せるようにする)


class _PairScan:
    """ギフター × ルームの送ったポイント (累計ランキング・貢献ランキング・記録したギフトの大きい方) を 1 回だけ読んで、
    人ごとのまとめ (表 u)・ルームごとのまとめ (room_rows)・ルーム同士の重なりに使う人ごとのルームの並び (user_rooms・user_ends)
    を一度に作る。

    user_room は人の順に読めるので、人ごとに読み終えた所でまとめる (並べ替えも、写しの一時表も要らない)。
    - SCAN_CHUNK 行ずつ、それぞれを 1 つのトランザクションで読み、u に書く。前は u に 1 行書くたびに 1 回ずつ書き終えていて
      (自動コミット)、100 万人なら 100 万回ファイルに書いていた。読み込みも区切るので、そのあいだ止まっていた書き込み待ち (WAL) を本体に移せる
    - ルームごとのまとめは、ルームの番号 → 並びの位置にして、数の並び (array) で持つ (ルームごとに list を作らない。メモリを小さく)
    - 記録したギフトのない人 (ほとんど) は、行を読んだその場でルームに足す (人ごとに dict を作り直さない。サーバーで、
      1 人ずつまとめる所が 24 秒・読んだ行を回す所が 8.5 秒かかっていた。2026-10-20 に Cursor が測った)。
      複数のルームに送る人の数 (multi) は、ギフターの数から 1 つのルームにだけ送る人の数を引いて出す
    """

    def __init__(self, s: Store, recorded: dict[int, dict[int, int]], chunk: int = SCAN_CHUNK):
        self.at: dict[int, int] = {}  # ルーム → 並びの位置
        self.ids = array("q")  # 位置 → ルーム
        self.gifters = array("q")
        self.points = array("q")
        self.singles = array("q")  # 1 つのルームにだけ送る人の数
        self.top_user = array("q")
        self.top_points = array("q")
        self.user_rooms = array("q")  # 2〜PAIR_USER_ROOMS ルームに送っている人の、ルームの番号を人ごとに続けて並べたもの
        self.user_ends = array("q")  # 人ごとの終わりの位置
        self.batch: list[tuple[int, int, int]] = []
        self.s = s
        rec_users = sorted(recorded)
        k = 0
        start, limit = None, chunk
        c = s.conn
        read = c.cursor()
        read.row_factory = None  # ただの tuple で受け取る (速く、並べた位置で探せる)
        at, ids, gifters, points, top_user, top_points = self.at, self.ids, self.gifters, self.points, self.top_user, self.top_points
        user_rooms = self.user_rooms
        while True:
            c.execute("BEGIN")
            try:
                rows = read.execute(f"""SELECT user_id, room_id, MAX(cumulative, events) FROM user_room
                                     WHERE {'1' if start is None else 'user_id >= ?'} AND MAX(cumulative, events) > 0
                                     ORDER BY user_id, room_id LIMIT ?""", (limit,) if start is None else (start, limit)).fetchall()
                last = len(rows) < limit
                if not last:
                    if rows[0][0] == rows[-1][0]:  # 1 人で limit 行を超える: 区切りを広げて読み直す
                        c.execute("COMMIT")
                        limit *= 2
                        continue
                    start = rows[-1][0]  # いちばん後の人は続きがあるかもしれないので、次に読み直す
                    rows = rows[:bisect_left(rows, (start,))]
                    limit = chunk
                cur = None
                mine: dict[int, int] | None = None  # 記録したギフトのある人だけ、まとめてから足す
                n = total = first = 0
                for uid, rid, pt in rows:
                    if uid != cur:
                        if cur is not None:
                            if mine is None:
                                self._end(cur, n, total, first)
                            else:
                                self._user(cur, mine)
                        while k < len(rec_users) and rec_users[k] < uid:  # 記録したギフトにだけ出てくる人
                            self._user(rec_users[k], dict(recorded[rec_users[k]]))
                            k += 1
                        cur, n, total, first = uid, 0, 0, len(user_rooms)
                        if k < len(rec_users) and rec_users[k] == uid:
                            k += 1
                            mine = dict(recorded[uid])
                        else:
                            mine = None
                    if mine is not None:
                        if pt > mine.get(rid, 0):
                            mine[rid] = pt
                        continue
                    n += 1
                    total += pt
                    user_rooms.append(rid)
                    i = at.get(rid)
                    if i is None:
                        at[rid] = len(ids)
                        ids.append(rid)
                        gifters.append(1)
                        points.append(pt)
                        self.singles.append(0)
                        top_user.append(uid)
                        top_points.append(pt)
                    else:
                        gifters[i] += 1
                        points[i] += pt
                        if pt > top_points[i]:  # 同じポイントなら先 (ユーザー ID の小さい人)
                            top_user[i] = uid
                            top_points[i] = pt
                if cur is not None:
                    if mine is None:
                        self._end(cur, n, total, first)
                    else:
                        self._user(cur, mine)
                if last:
                    for uid in rec_users[k:]:
                        self._user(uid, dict(recorded[uid]))
                self._flush()
            except BaseException:
                c.execute("ROLLBACK")
                raise
            c.execute("COMMIT")
            if last:
                break
        self.multi = array("q", (g - one for g, one in zip(gifters, self.singles)))
        del self.singles

    def _end(self, uid: int, n: int, total: int, first: int) -> None:
        """1 人の行を読み終えた (ルームには足してある)。first は、この人のルームを user_rooms に並べ始めた位置。"""
        if n == 1:
            self.singles[self.at[self.user_rooms[first]]] += 1
        if not 2 <= n <= PAIR_USER_ROOMS:
            del self.user_rooms[first:]
        else:
            self.user_ends.append(len(self.user_rooms))
        self.batch.append((uid, total, n))

    def _user(self, uid: int, mine: dict[int, int]) -> None:
        """記録したギフトのある人: 記録と user_room の大きい方をまとめた mine (ルーム → pt) を足す。"""
        first = len(self.user_rooms)
        n = total = 0
        for rid, pt in mine.items():
            if pt <= 0:
                continue
            n += 1
            total += pt
            self.user_rooms.append(rid)
            i = self.at.get(rid)
            if i is None:
                self.at[rid] = len(self.ids)
                self.ids.append(rid)
                self.gifters.append(1)
                self.points.append(pt)
                self.singles.append(0)
                self.top_user.append(uid)
                self.top_points.append(pt)
            else:
                self.gifters[i] += 1
                self.points[i] += pt
                if pt > self.top_points[i]:
                    self.top_user[i] = uid
                    self.top_points[i] = pt
        if n:
            self._end(uid, n, total, first)

    def _flush(self) -> None:
        self.s.conn.executemany("INSERT INTO u VALUES (?, ?, ?)", self.batch)
        self.batch = []

    def room(self, rid: int) -> list[int] | None:
        """ルームのまとめ [ギフターの数, ポイント, 複数のルームに送る人, いちばん送っている人, その人のポイント]。"""
        i = self.at.get(rid)
        return None if i is None else [self.gifters[i], self.points[i], self.multi[i], self.top_user[i], self.top_points[i]]

    def room_rows(self) -> Iterator[tuple[int, int, int, int, int, int]]:
        return zip(self.ids, self.gifters, self.points, self.multi, self.top_user, self.top_points)


def _name_sql(table: str) -> str:
    """ギフターの名前 (記録したギフトの名前 → research.db の名前)。退会したユーザーを除くのに使う。"""
    return (f"COALESCE((SELECT name FROM rname r WHERE r.user_id={table}.user_id),"
            f" (SELECT name FROM users n WHERE n.user_id={table}.user_id), '')")


def _fill_temp(c: sqlite3.Connection, sql: str, rows: list[tuple[Any, ...]]) -> None:
    """一時表に、まとめて 1 回で書く (1 行ずつ書き終えると遅い。rows は主キーの順に並べておくと、さらに速い)。
    一時表だけに書くので、research.db の書き込みは止めない。"""
    c.execute("BEGIN")
    try:
        c.executemany(sql, rows)
    except BaseException:
        c.execute("ROLLBACK")
        raise
    c.execute("COMMIT")


def _rec_names(s: Store, rec: _Recorded) -> None:
    s.conn.execute("CREATE TEMP TABLE IF NOT EXISTS rname (user_id INTEGER PRIMARY KEY, name TEXT)")
    _fill_temp(s.conn, "INSERT OR REPLACE INTO rname VALUES (?, ?)", sorted(rec.names.items()))


def _analyze_base(state_dir: Path, limit: int) -> dict[str, Any]:
    """土台: 全ルームの「誰がどのルームにいくら送ったか」を 1 回だけ読んで、見るルームに関係ない集計を作る。

    人ごとのまとめ (u) とルームごとのまとめ (rs) は、見るルームごとの集計が読むので、cache の小さな DB に残す
    (research.db には入れない。写しやバックアップを大きくしないため)。
    """
    rec = _Recorded(state_dir, catch_up=False)  # 記録の新しい分は裏で足す (書き込みの順番を待たない)
    cache = Path(state_dir) / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    db_name = f"analysis-base-{time.time_ns()}.db"
    with Store(state_dir) as s:
        rooms = _Rooms(s, state_dir)
        c = s.conn
        merged: dict[int, dict[int, int]] = defaultdict(lambda: defaultdict(int))  # 記録したギフト: 人 → ルーム → pt (累計ランキングの物差し)
        for (uid, key), pt in rec.points.items():
            if pt:
                merged[uid][rooms.id(key)] += rec.ranked(uid, key)
        _rec_names(s, rec)
        # この接続の作業用のメモリ (メモリの空きが少ない時は、空きの 8 分の 1 まで。ブラウザを追い出さない)
        free = memory.free_mb()
        c.execute(f"PRAGMA cache_size=-{ANALYSIS_CACHE_KB if free is None else max(8192, min(ANALYSIS_CACHE_KB, int(free * 1024 / 8)))}")
        c.execute("ATTACH DATABASE ? AS b", (str(cache / db_name),))
        try:
            c.execute("PRAGMA b.journal_mode=OFF")
            c.execute("PRAGMA b.synchronous=OFF")
            c.execute("CREATE TABLE b.u (user_id INTEGER PRIMARY KEY, total INTEGER, n INTEGER)")
            scan = _PairScan(s, merged)
            del merged
            c.execute("CREATE INDEX b.u_total ON u(total)")
            c.execute("""CREATE TABLE b.rs (room_id INTEGER PRIMARY KEY, gifters INTEGER, points INTEGER, multi INTEGER,
                                            top_user INTEGER, top_points INTEGER)""")
            c.execute("BEGIN")  # まとめて 1 回で書く (1 行ずつ書き終えると遅い)
            c.executemany("INSERT INTO b.rs VALUES (?, ?, ?, ?, ?, ?)", scan.room_rows())
            c.execute("COMMIT")
            left = tuple(LEFT_NAMES)
            alive = f"total > 0 AND {_name_sql('u')} NOT IN ({','.join('?' * len(left))})"
            signup = _Signup.build(s, rec)
            with s.tx():
                s.set_meta(SIGNUP_META, signup.steps)
            # 登録した年ごとの人数と上位 (見るルームのギフターは、見るルームごとの集計で除く)
            signup_years = []
            for year, lo, hi, rough in signup.years():
                band = (*left, lo, hi)
                n = s.scalar(f"SELECT COUNT(*) FROM b.u WHERE {alive} AND user_id BETWEEN ? AND ?", band)
                if n:
                    top = [r[0] for r in s.all(f"""SELECT user_id FROM b.u WHERE {alive} AND user_id BETWEEN ? AND ?
                                                   ORDER BY total DESC, user_id LIMIT ?""", (*band, SIGNUP_BASE_LIMIT))]
                    signup_years.append({"year": year, "count": n, "rough": rough, "lo": lo, "hi": hi, "top": top})
            alive_count = s.scalar(f"SELECT COUNT(*) FROM b.u WHERE {alive}", left)
            gifter_count = s.scalar("SELECT COUNT(*) FROM b.u")
            room_count = s.scalar("SELECT COUNT(*) FROM b.rs")
            multi_count = s.scalar(f"SELECT COUNT(*) FROM b.u WHERE n > 1 AND {_name_sql('u')} NOT IN ({','.join('?' * len(left))})", left)
            room_rows = s.all("SELECT * FROM b.rs ORDER BY points DESC LIMIT 200")
            pairs = _room_pairs(s, rooms, scan)
            del scan
        finally:
            c.commit()
            c.execute("DETACH DATABASE b")

        cov = coverage(state_dir)  # 古ければ前の結果 (裏で数え直す)。数えるのに 20 秒かかるので、分析のたびに数え直さない
        trend = _trend(s, cov, left)
        top_names = _user_names(s, rec, list({r["top_user"] for r in room_rows}))
        room_list = []
        for r in room_rows:
            key, label = rooms.label(r["room_id"])
            info = s.room(room_id=r["room_id"]) if r["room_id"] > 0 else None
            room_list.append({"room_id": r["room_id"], "key": key, "name": label, "gifters": r["gifters"], "points": r["points"],
                              "multi": r["multi"],
                              "events": s.scalar("SELECT COUNT(*) FROM room_events WHERE room_id=?", (r["room_id"],)),
                              "researched": bool(info is not None and info["collected_at"]),
                              "top": {"user_id": r["top_user"], "name": top_names.get(r["top_user"]) or "(名前なし)",
                                      "points": r["top_points"]}})
        # 記録したギフトにだけ出てくるルームの仮の番号 (見るルームごとの集計でも同じ番号を使う)
        temp_ids = {key: rid for key, rid in rooms.ids.items() if rid < 0}
    return {"db": db_name, "limit": limit, "temp_ids": temp_ids, "alive_count": alive_count, "gifter_count": gifter_count,
            "room_count": room_count, "multi_count": multi_count, "rooms": room_list, "pairs": pairs, "coverage": cov,
            "trend": trend, "signup_years": signup_years}


def _support_scores(s: Store, rooms: _Rooms, rec: _Recorded, sup: set[int]) -> dict[int, dict[int, int]]:
    """見るルームに送ったポイント (人 → 見るルーム → pt)。ルームごとに、累計ランキング・貢献ランキング・記録したギフト
    (ルームの記録の合計) の大きい方 (room_score と同じ)。"""
    pts: dict[tuple[int, int], int] = {}
    for part in _chunks(sorted(sup)):
        for uid, rid, pt in s.conn.execute(f"""SELECT user_id, room_id, MAX(cumulative, events) FROM user_room
                                               WHERE room_id IN ({','.join('?' * len(part))}) AND MAX(cumulative, events) > 0""",
                                           part):
            pts[(uid, rid)] = pt
    recorded: dict[tuple[int, int], int] = defaultdict(int)
    for (uid, key), pt in rec.points.items():
        if pt:
            rid = rooms.id(key)
            if rid in sup:
                recorded[(uid, rid)] += rec.ranked(uid, key)  # 累計ランキングの物差し (room_score と同じ)
    for pair, pt in recorded.items():
        if pt > pts.get(pair, 0):
            pts[pair] = pt
    out: dict[int, dict[int, int]] = defaultdict(dict)
    for (uid, rid), pt in pts.items():
        out[uid][rid] = pt
    return dict(out)


def _fan_rooms(s: Store, rooms: _Rooms, rec: _Recorded, fans: list[int], sup: set[int]) -> dict[int, int]:
    """見るルームのギフターが送っている、見るルームでないルーム → 人数。

    見るルームのギフター (一時表 fan) の行を、SQLite の中で数える (前は Python で 1 行ずつ人ごとの set に入れていて、
    ギフターの多いルームほど遅かった)。記録したギフトにだけあるルームは一時表 fan_rec に入れて、user_room と重ねずに足す。
    """
    c = s.conn
    c.execute("CREATE TEMP TABLE IF NOT EXISTS fan_rec (user_id INTEGER, room_id INTEGER, PRIMARY KEY (user_id, room_id)) WITHOUT ROWID")
    c.execute("DELETE FROM fan_rec")
    _fill_temp(c, "INSERT OR IGNORE INTO fan_rec VALUES (?, ?)",
               sorted((uid, rooms.id(key)) for uid in fans for key, pt in rec.by_user.get(uid, {}).items() if pt))
    out = {}
    for rid, n in c.execute("""SELECT room_id, COUNT(*) FROM (
                                   SELECT room_id FROM user_room WHERE user_id IN (SELECT user_id FROM fan) AND MAX(cumulative, events) > 0
                                   UNION ALL
                                   SELECT room_id FROM fan_rec f WHERE NOT EXISTS (SELECT 1 FROM user_room r WHERE r.user_id=f.user_id
                                       AND r.room_id=f.room_id AND MAX(r.cumulative, r.events) > 0))
                               GROUP BY room_id"""):
        if rid not in sup:
            out[rid] = n
    return out


def _analyze_view(state_dir: Path, base: dict[str, Any], support: list[str], limit: int) -> dict[str, Any]:
    """見るルームごとの集計: 土台に、見るルームのギフターを当てはめる (索引で読む分だけ。全部は読まない)。"""
    rec = _Recorded(state_dir, catch_up=False)  # 記録の新しい分は裏で足す (書き込みの順番を待たない)
    with Store(state_dir) as s:
        rooms = _Rooms(s, state_dir)
        for key, rid in base["temp_ids"].items():  # 記録にだけ出てくるルームは、土台と同じ番号にする
            if s.room(key=key) is not None or (rooms.known.get(key) or {}).get("room_id"):
                continue  # そのあと room_id が分かった
            rooms.ids[key] = rid
            rooms.labels[rid] = (key, (rooms.known.get(key) or {}).get("name") or key)
        sup_ids = list(dict.fromkeys(rooms.id(k) for k in support))
        sup = set(sup_ids)
        c = s.conn
        c.execute("ATTACH DATABASE ? AS b", (str(Path(state_dir) / "cache" / base["db"]),))
        try:
            _rec_names(s, rec)
            sup_scores = _support_scores(s, rooms, rec, sup) if sup else {}
            points = {uid: sum(v.values()) for uid, v in sup_scores.items()}
            fan_order = sorted(points, key=lambda u: (-points[u], u))
            fan_count = len(fan_order)
            fan_ids = fan_order[:limit]
            c.execute("CREATE TEMP TABLE fan (user_id INTEGER PRIMARY KEY)")
            _fill_temp(c, "INSERT INTO fan VALUES (?)", [(u,) for u in sorted(fan_order)])
            left = tuple(LEFT_NAMES)
            alive = f"{_name_sql('u')} NOT IN ({','.join('?' * len(left))})"
            # 誘導の候補: 見るルームに送っていない人 (ポイントの多い順。total の索引を上から読んで、見るルームのギフターを飛ばす)
            fans_alive = s.scalar(f"SELECT COUNT(*) FROM b.u WHERE user_id IN (SELECT user_id FROM fan) AND total > 0 AND {alive}",
                                  left) if fan_order else 0
            candidate_count = base["alive_count"] - fans_alive
            cand_ids = [r[0] for r in s.all(f"""SELECT user_id FROM b.u INDEXED BY u_total WHERE total > 0
                                               AND user_id NOT IN (SELECT user_id FROM fan) AND {alive}
                                               ORDER BY total DESC, user_id LIMIT ?""", (*left, limit))]
            overall = set(cand_ids)
            signup_years, extra_ids = [], []
            for y in base["signup_years"]:
                band_fans = s.scalar("SELECT COUNT(*) FROM fan WHERE user_id BETWEEN ? AND ?", (y["lo"], y["hi"])) if fan_order else 0
                n = y["count"] - (s.scalar(f"""SELECT COUNT(*) FROM b.u WHERE user_id IN (SELECT user_id FROM fan
                                                WHERE user_id BETWEEN ? AND ?) AND total > 0 AND {alive}""",
                                           (y["lo"], y["hi"], *left)) if band_fans else 0)
                if n:
                    signup_years.append({"year": y["year"], "count": n, "rough": y["rough"]})
                    extra_ids += [u for u in [u for u in y["top"] if u not in points][:SIGNUP_YEAR_LIMIT] if u not in overall]
            fan_rooms = _fan_rooms(s, rooms, rec, fan_order, sup) if fan_order else {}
            gifters = {r[0]: r[1] for part in _chunks(list(fan_rooms) + sup_ids)
                       for r in s.all(f"SELECT room_id, gifters FROM b.rs WHERE room_id IN ({','.join('?' * len(part))})", part)}
        finally:
            c.execute("DETACH DATABASE b")
        # 似ているルーム: 見るルームのギフターが、ほかにどのルームに送っているか
        similar = []
        for rid, shared in sorted(((r, n) for r, n in fan_rooms.items() if r in gifters), key=lambda kv: (
                -kv[1], -kv[1] / (gifters[kv[0]] + fan_count - kv[1]), kv[0]))[:100]:
            key, label = rooms.label(rid)
            similar.append({"key": key, "name": label, "gifters": gifters[rid], "shared": shared,
                            "overlap": round(shared / (gifters[rid] + fan_count - shared), 3), "candidates": gifters[rid] - shared})
        trend = base["trend"]
        trend_ids = [r["user_id"] for kind in ("falling", "rising", "hidden") for r in trend[kind]]
        uids = list(dict.fromkeys(fan_ids + cand_ids + extra_ids + trend_ids))
        names = _user_names(s, rec, uids)
        signup = _signup(s, rec)
        now = time.time()
        cache = _SummaryCache(state_dir)
        try:
            parts = cache.get(uids, now)  # 見るルームに関係ない分は、SUMMARY_TTL 秒使い回す
            todo = [u for u in uids if u not in parts]
            evs = _event_stats(s, todo)
            # ルームごとの内訳は SUMMARY_STEP 人ずつまとめて読み、まとめを作ったら捨てる
            # (前は 1 人ずつ読んでいて、サーバーで 1,400 回・24 秒。全員を一度に読むと、数万ルームに出てくる人もいてメモリが足りなくなる)
            made = {}
            for part in _chunks(todo, SUMMARY_STEP):
                slots = _slots(s, rooms, rec, part)
                made.update({u: _summary_part(slots.get(u, {}), evs.get(u), rec.gifts.get(u, []), sup) for u in part})
                del slots
            if made:
                cache.put(made, now)
        finally:
            cache.close()
        parts.update(made)
        summary = {u: _summary_from(u, names.get(u), parts[u], sup_scores.get(u, {}), rooms, sup, signup.guess(u)) for u in uids}
        rec.gifts.close()
        room_list = [{**{k: v for k, v in r.items() if k != "room_id"}, "support": r["room_id"] in sup} for r in base["rooms"]]
        return {"mode": "support" if sup_ids else "all",
                "support": [{"key": k, "name": rooms.label(rooms.id(k))[1], "gifters": gifters.get(rooms.id(k), 0)}
                            for k in support],
                "candidates": [summary[u] for u in cand_ids], "candidate_count": candidate_count,
                "signup_candidates": [summary[u] for u in extra_ids], "signup_years": signup_years,
                "fans": [summary[u] for u in fan_ids], "fan_count": fan_count, "similar": similar,
                "pairs": base["pairs"], "rooms": room_list, "room_count": base["room_count"], "multi_count": base["multi_count"],
                "gifter_count": base["gifter_count"], "coverage": base["coverage"],
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
    if hit is None and now is None:
        # 起動し直したあと: 前に数えた結果を research.db から読む (古ければ下で裏で数え直す。数えるのに 20 秒かかり、ページを待たせていた)
        with Store(state_dir) as s:
            saved = s.meta("coverage_cache")
        if saved and saved.get("result") is not None:
            hit = _coverage_cache[key] = (float(saved["at"]), saved["result"])
    if hit and now is None:
        if time.time() - hit[0] < max_age:
            return hit[1]
        if max_age == ANALYSIS_TTL:
            # 古くなった時は、前の結果をすぐ返して、裏で数え直す (重い表を読むので、ページを開いた人を待たせない)
            with _coverage_lock:
                if key not in _coverage_running and not memory.tight():  # 空きが少なければ前の結果のまま (次の機会に)
                    _coverage_running.add(key)
                    threading.Thread(target=_refresh_coverage, args=(state_dir, key), name="coverage", daemon=True).start()
            return hit[1]
    with memory.heavy(), Store(state_dir) as s:
        result = _coverage(s, time.time() if now is None else now)
        if now is None:
            _save_coverage(s, result)
    _coverage_cache[key] = (time.time(), result)
    return result


def _save_coverage(s: Store, result: dict[str, Any]) -> None:
    try:
        s.set_meta("coverage_cache", {"at": time.time(), "result": result})
    except (TypeError, ValueError, sqlite3.Error) as e:  # 残せなくても、この起動の間は覚えている
        log.debug("集まり具合を残せませんでした: %s", e)


_coverage_lock = threading.Lock()
_coverage_running: set[str] = set()


def _refresh_coverage(state_dir: Path, key: str) -> None:
    try:
        with memory.heavy(wait=False) as got, Store(state_dir) as s:
            if not got:  # ほかの重い集計が動いている: 前の結果のまま、次の機会に
                return
            result = _coverage(s, time.time())
            _save_coverage(s, result)
            _coverage_cache[key] = (time.time(), result)
    except Exception as e:  # noqa: BLE001 - 数え直せなくても、前の結果を使い続ける
        log.debug("集まり具合を数え直せませんでした: %s", e)
    finally:
        with _coverage_lock:
            _coverage_running.discard(key)
        memory.release()


def _coverage(s: Store, now: float) -> dict[str, Any]:
    rows = s.all(f"""SELECT {MONTH_SQL} AS month, SUM(re.final=1) AS done, SUM({PENDING_RANKING}) AS pending
                     FROM room_events re JOIN events e USING(event_id)
                     WHERE e.end IS NOT NULL AND e.end < ? AND e.show_ranking=1 GROUP BY month ORDER BY month""",
                 (now - 3600, RANKING_RETRIES, now - 3600))
    # 1 回で数える (rooms を読むのはサーバーで 1 回 5 秒。前は 2 回読んでいた)
    counts = s.one(f"""SELECT COALESCE(SUM(NOT ({SKIPPED_ROOM})), 0) AS total,
                              COALESCE(SUM(collected_at IS NOT NULL AND NOT ({SKIPPED_ROOM})), 0) AS done FROM rooms""")
    rooms_total, rooms_done = counts["total"], counts["done"]
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
            "cumulative_from": (lambda at: _month(at) if at else None)(_cumulative_from(s))}


def _cumulative_from(s: Store) -> float | None:
    """累計ランキングの値を残し始めた時刻 (cumulative_history のいちばん古い at)。

    表は人の順に並んでいて at の索引が無いので、数えると表を全部読む (サーバーで 7 秒)。値は、残すたびにその時刻で足すので
    一度分かれば変わらない。research.db に覚えておく。"""
    at = s.meta("cumulative_from_at")
    if at is None:
        at = s.scalar("SELECT MIN(at) FROM cumulative_history", default=None)
        if at is not None:
            with s.tx():
                s.set_meta("cumulative_from_at", at)
    return at


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


def _room_pairs(s: Store, rooms: _Rooms, scan: "_PairScan") -> list[dict[str, Any]]:
    """ルーム同士の重なり: どのルームとどのルームに、同じギフターが送っているか (ギフターの好みが近いルーム)。

    全ルームの組は数が多すぎるので、ギフターの多い PAIR_ROOMS ルームの中で調べる。
    組ごとの共通のギフターの数を、人ごとに (読み込みの時に残した、人ごとのルームの並び) 数えて、ルームの番号 × 番号の表に足す。
    """
    top = heapq.nsmallest(PAIR_ROOMS, zip(scan.ids, scan.gifters), key=lambda kv: (-kv[1], kv[0]))
    gifters = dict(top)
    ids = sorted(gifters)
    at = {rid: i for i, rid in enumerate(ids)}
    n = len(ids)
    counts = [0] * (n * n)
    flat, ends = scan.user_rooms, scan.user_ends
    start = 0
    for end in ends:
        mine = [at[r] for r in flat[start:end] if r in at]
        start = end
        if len(mine) < 2:  # 上位のルームの組にならない人 (多い) は、並べ替えない
            continue
        mine.sort()
        for i, x in enumerate(mine):
            base = x * n
            for y in mine[i + 1:]:
                counts[base + y] += 1
    found = heapq.nlargest(1000, ((v, i) for i, v in enumerate(counts) if v >= PAIR_MIN_SHARED))
    del counts
    out = []
    for shared, i in found:
        a, b = ids[i // n], ids[i % n]
        if gifters[b] > gifters[a] or (gifters[b] == gifters[a] and rooms.label(b)[0] < rooms.label(a)[0]):
            a, b = b, a
        out.append({"a": rooms.label(a)[0], "a_name": rooms.label(a)[1], "b": rooms.label(b)[0], "b_name": rooms.label(b)[1],
                    "shared": shared, "overlap": round(shared / (gifters[a] + gifters[b] - shared), 3)})
    out.sort(key=lambda p: (-p["shared"], -p["overlap"]))
    return out[:100]


DETAIL_ROWS = 300  # 1 人の詳しい表示で返すルーム・イベントの数 (数万ルームに出てくる人もいる)


def gifter_detail(state_dir: Path, user_id: Any, support: list[str]) -> dict[str, Any]:
    """1 人のギフターの詳しい情報 (ルームごとの内訳・イベントの実績・行動のくせ)。同じページを同時に開いた時は、1 回だけ数える (once)。"""
    from . import once

    return once.shared(("gifter", str(state_dir), str(user_id), tuple(support)), lambda: _gifter_detail(state_dir, user_id, support))


def _gifter_detail(state_dir: Path, user_id: Any, support: list[str]) -> dict[str, Any]:
    try:
        uid = int(str(user_id).strip())
    except (TypeError, ValueError):
        raise ValueError("ユーザー ID は数字で指定してください") from None
    with Store(state_dir) as s:
        rec = _Recorded(state_dir, s, user=uid, catch_up=False)  # この人と、この人が送ったルームの人の分だけ
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
        visits = _visit_habits(state_dir, rooms, uid)
        overlap = _time_overlap(s, rooms, habits(gifts) or visits, [r["key"] for r in room_list[:OVERLAP_ROOMS]], support)
        recorded = _recorded_types(s, rooms, rec, uid, gifts)
        involve = _involvement(state_dir, s, rooms, rec, uid)
    cov = coverage(state_dir)
    from . import gifter_status, support_card

    with Store(state_dir) as s:
        rooms.store = s  # 最初の接続は閉じたので、名前を引く接続を替える (多くのルームに出た人で、閉じた接続を使って止まっていた)
        return {"user_id": uid, "found": True,
                "coverage": {k: cov[k] for k in ("complete_from", "last_full", "cumulative_from")}, "room_total": len(room_list), "event_total": event_total,
                "guessed": guessed,
                "watch": {"memo": w["memo"] or "", "added_at": w["added_at"]} if w else None,
                "former_names": [dict(r) for r in s.all("""SELECT name, at, source FROM user_names WHERE user_id=?
                                                          ORDER BY at DESC LIMIT 20""", (uid,))],
                "summary": _summary(uid, name, slots, _event_stats(s, [uid]).get(uid), gifts, rooms, sup,
                                    _signup(s, rec).guess(uid)),
                "rooms": room_list[:DETAIL_ROWS], "events": events, "habits": habits(gifts), "recorded_types": recorded, "involvement": involve, "timeline": _timeline(s, rooms, gifts, uid),
                "visits": visits, "overlap": overlap,
                "states": _states(s, rooms, uid), "status": gifter_status.read(s, uid),
                "support": support_card.by_room(s, uid, time.time(), rooms.label)}


def _visits_of(state_dir: Path, s: Store, uid: int) -> list[tuple[int, str, int]]:
    """記録しているルームの、その人の来場の知らせ [(時刻, ルーム, 何回目)] (live_visits を索引で引く。新しい分は裏で足す)。"""
    from . import catchup

    catchup.soon(state_dir)  # 新しい分は裏で足す (書き込みの順番を待たない。前はここで待っていた。2026-10-20 に Cursor が見た)
    return [(r[0], r[1], r[2]) for r in s.all("SELECT at, room_key, n FROM live_visits WHERE user_id=? ORDER BY at", (int(uid),))]


def _comments_in(state_dir: Path, keys: set[str]) -> dict[str, dict[int, int]]:
    """そのルームたちの、人ごとのコメントの数 (配信ごとの by_user を足したもの。2026-10-19 から) {ルーム: {人: 数}}。
    要るルームのファイルだけ読む。"""
    from . import comment_counts

    out: dict[str, dict[int, int]] = {}
    for key in keys:
        room: dict[int, int] = {}
        for live in comment_counts.load(Path(state_dir), key):
            for u, n in (live.get("by_user") or {}).items():
                try:
                    room[int(u)] = room.get(int(u), 0) + int(n)
                except (TypeError, ValueError):
                    continue
        out[key] = room
    return out


def _involvement(state_dir: Path, s: Store, rooms: "_Rooms", rec: _Recorded, uid: int) -> list[dict[str, Any]]:
    """関わり方 (gifter_analysis.involvement)。送ったルームと来場したルームで見る。ルームは名前つきにする。"""
    from .gifter_analysis import involvement

    visits = _visits_of(state_dir, s, uid)
    keys = {k for (u, k) in rec.points if u == uid} | {k for _, k, _ in visits}
    comments = _comments_in(state_dir, keys)
    paid: dict[str, dict[int, int]] = {k: {} for k in keys}
    for (u, k), pt in rec.points.items():
        if k in paid:
            p = pt - rec.free.get((u, k), 0)
            if p > 0:
                paid[k][u] = p
    out = involvement(uid, {k: comments.get(k, {}) for k in keys}, paid, visits, keys)
    for r in out:
        r["name"] = rooms.label(rooms.id(r["key"]))[1]
    return out


def _recorded_types(s: Store, rooms: "_Rooms", rec: _Recorded, uid: int, gifts: list[tuple[float, int, str]]) -> dict[str, Any] | None:
    """記録しているルームのギフト 1 件ずつからの型 (gifter_analysis.recorded_types)。ルームのイベントと配信の始まりは、ルームごとに主キーで引く。"""
    from .gifter_analysis import recorded_types

    if not gifts:
        return None
    keys = {k for _, _, k in gifts}
    # ルーム全員のギフト 1 件ずつは、盛り上がり型を見るルーム (記録したポイントの多い HYPE_ROOMS 個) だけ読む。
    # 星だけの大貢献の順位は、ルームごとの合計 (rec.points) でくらべる
    from .gifter_analysis import HYPE_ROOMS

    hype = sorted(keys, key=lambda k: -rec.points.get((uid, k), 0))[:HYPE_ROOMS]
    others = {k: rec.gifts.room(k) for k in hype}
    room_users: dict[str, list[int]] = {k: [] for k in keys}
    for u, rm in rec.by_user.items():
        for k in rm:
            if k in room_users:
                room_users[k].append(u)
    events, starts = {}, {}
    for k in keys:
        rid = rooms.id(k)
        events[k] = [(r[0], r[1]) for r in s.all("""SELECT e.start, e.end FROM room_events re JOIN events e USING(event_id)
                                                     WHERE re.room_id=? AND e.start IS NOT NULL AND e.end IS NOT NULL""", (rid,))]
        starts[k] = [r[0] for r in s.all("SELECT started_at FROM room_lives WHERE room_id=? ORDER BY started_at", (rid,))]
    out = recorded_types(uid, gifts, rec.points, rec.free, others, events, starts, getattr(rec, "unknown", None), room_users)
    for x in out.get("stars", []):
        x["name"] = rooms.label(rooms.id(x["key"]))[1]
    return out


def _visit_habits(state_dir: Path, rooms: "_Rooms", uid: int) -> dict[str, Any] | None:
    """来場の時間帯 (記録しているルームの「○○さんが N 度目の訪問」の知らせから)。"""
    vs = _visits_of(state_dir, rooms.store, uid)
    if not vs:
        return None
    hour, weekday = [0] * 24, [0] * 7
    per: dict[str, dict[str, Any]] = {}
    for at, room, n in vs:
        dt = datetime.fromtimestamp(at, JST)
        hour[dt.hour] += 1
        weekday[dt.weekday()] += 1
        r = per.setdefault(room, {"key": room, "times": 0, "max_n": 0, "last": 0})
        r["times"] += 1
        r["max_n"] = max(r["max_n"], n)
        r["last"] = max(r["last"], at)
    for r in per.values():
        r["name"] = rooms.label(rooms.id(r["key"]))[1] if rooms.id(r["key"]) else r["key"]
    return {"times": len(vs), "hour": hour, "weekday": weekday, "top_hours": [h for h in sorted(range(24), key=lambda h: -hour[h])[:3] if hour[h]],
            "rooms": sorted(per.values(), key=lambda r: -r["times"]), "first": min(v[0] for v in vs), "last": max(v[0] for v in vs)}


OVERLAP_ROOMS = 5  # 時間帯の重なりを見る、このギフターがよく送るルームの数 (応援ルームも足す)


def _time_overlap(s: Store, rooms: "_Rooms", hab: dict[str, Any] | None, keys: list[str], support: list[str]) -> list[dict[str, Any]]:
    """ギフターがよく送る時間帯 (記録したギフト) と、ルームが配信している時間帯 (直近 30 日の配信の記録) の重なり。

    重なり = 2 つの時間帯の分け方 (それぞれ合計を 1 にしたもの) の、時間ごとの小さい方の合計 (0〜1)。
    記録したギフトが無い人は出さない。
    """
    from . import room_analysis

    if not hab or not sum(hab["hour"]):
        return []  # ギフトを記録していない人は、来場の時間帯でくらべる (呼ぶ側で、来場の分を渡す)
    g = [x / sum(hab["hour"]) for x in hab["hour"]]
    out = []
    for key in dict.fromkeys([*support, *keys]):
        rid = rooms.id(key)
        h = room_analysis.live_habits(s, rid) if rid else None
        if not h or not sum(h["coverage"]):
            continue
        r = [x / sum(h["coverage"]) for x in h["coverage"]]
        out.append({"key": key, "name": rooms.label(rid)[1], "support": key in support,
                    "overlap": sum(min(a, b) for a, b in zip(g, r)), "slot": h["slot"], "per_week": h["per_week"]})
    return sorted(out, key=lambda x: -x["overlap"])


def _states(s: Store, rooms: "_Rooms", uid: int) -> dict[str, Any]:
    """ルームごとの今の状態・乗り換え・月ごとの大口/中堅/少額 (gifter_analysis)。ルームは名前つきにする。"""
    from .gifter_analysis import gifter_states

    st = gifter_states(s, uid)
    for r in st["rooms"] + st["pillars"] + st["life"]["rooms"] + st["engage"]["rooms"] + st["engage"]["dependency"]["rooms"]:
        r["key"], r["name"] = rooms.label(r.pop("room_id"))
    for q in st["focus"]["quarters"]:
        if q["top"] is not None:
            q["top_key"], q["top_name"] = rooms.label(q.pop("top"))
    if st["switch"]:
        sw = st["switch"]
        sw["from_key"], sw["from_name"] = rooms.label(sw.pop("from"))
        sw["to_key"], sw["to_name"] = rooms.label(sw.pop("to"))
    return st


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
    from . import once

    once.forget(lambda k: k[0] == "watch")  # 一覧を使い回さず、次に開いた時に作り直す


WATCH_TTL = 60  # 気になるギフターの一覧を使い回す秒数 (入れる・外すと作り直す)


def watch_list(state_dir: Path, support: list[str]) -> list[dict[str, Any]]:
    """気になるギフターの一覧 (メモと、分析と同じ要約つき)。新しく入れた順。
    同時に開いた時は 1 回だけ数え、WATCH_TTL 秒は使い回す (前は 2 本ずつ同時に動き、1 回 平均 28 秒・1 時間に 12 回。2026-10-20)。"""
    from . import once

    return once.shared(("watch", str(state_dir), tuple(support)), lambda: _watch_list(state_dir, support), ttl=WATCH_TTL)


def _watch_list(state_dir: Path, support: list[str]) -> list[dict[str, Any]]:
    with Store(state_dir) as s:
        rows = [dict(r) for r in s.watch_rows()]
        if not rows:
            return []
        rec = _Recorded(state_dir, s, users=[r["user_id"] for r in rows], catch_up=False)  # 気になる人の分だけ
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
