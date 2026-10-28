"""Internet Archive (web.archive.org) に残っている SHOWROOM の古いページから、消えた情報を取り戻す。

SHOWROOM は、消えたルームを参加ルームの一覧から外し、退会したユーザーの名前を「退会済みユーザー」に置き換える。
古いページには、その前の姿が残っていることがある:

- イベントのページ (/event/<key>): 参加ルームの順位・ルーム番号・URL・名前 → 消えたルームを rooms と room_events に足す
- 貢献ランキングのページ (/event/contribution/<key>?room_id=N): 上位 100 人の順位・名前・ポイント (ユーザー ID は無い)
  - 同じイベント・ルームの貢献ランキングを SHOWROOM から取ってあれば、イベントが終わったあとの保存で、順位とポイントが両方同じ人が
    1 人だけの時に、その人の当時の名前を user_names に残す (退会したユーザーの、退会前の名前も分かる)
  - 消えたルームの分は、名前が 1 人に決まり・ページに 1 行だけで・前後 180 日に SHOWROOM で見えている人だけユーザー ID に結び付けて
    contributions に足す (「名前で推測」の印 guessed=1。分析の合計には入れ、ギフターのページで分けて見せる)
  - 確かでない時は結び付けない (2026-10-29。前の決まりで結び付けた分は recheck_step が 1 回だけ見直す)

問い合わせ先は SHOWROOM ではないので、research.THROTTLE とは別に ARCHIVE_GAP 秒に 1 回まで (混んでいれば広げる)。
"""

from __future__ import annotations

import html
import json
import logging
import re
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import memory
from .research import ARCHIVE_HOLD, LEFT_NAMES, Throttle, is_missing
from .research_store import Store

log = logging.getLogger("archive")

ARCHIVE = "https://web.archive.org"
ARCHIVE_GAP = 6.0  # Internet Archive への問い合わせの間隔 (秒)。4 秒ではつなぎを断られることが続いたので下げた (2026-10-18)
INDEX_URL = "showroom-live.com/event/*"  # 保存されているページの一覧を取る範囲
PAGE_TRIES = 3  # 1 つのページで、読めるものが見つかるまで試す保存の数 (新しい保存から)
IDLE = 3600  # することが無いときに待つ時間 (秒)。新しく集めたイベント・ルームと、後から突き合わせられる
REMATCH_EVERY = 200  # この回数ページを読むごとに、まとめて突き合わせ直す
MATCH_NEAR = 180 * 86400  # 名前で推測する人が、そのイベントの前後この秒数に SHOWROOM の貢献ランキングに出ていること
RECHECK_NAMES = 20000  # 見直しで、1 回に見る前の名前の数
RECHECK_PAGES = 100  # 見直しで、1 回に突き合わせ直す貢献ランキングのページの数
RECHECK_PAUSE = 1.0  # 見直しの 1 回ごとに休む秒数
NOT_EVENT_KEYS = {"contribution", "search", "list", "ranking", "calendar", "archive", "entry"}

_KEY = re.compile(r"^[A-Za-z0-9_\-]+$")
_LEFT = ",".join("?" * len(LEFT_NAMES))


def _not_page_fault(e: BaseException) -> bool:
    """ページの中身ではなく、つなぎ方の問題の失敗か (つながらない・断られた・時間切れ・混んでいる)。"""
    import socket
    import urllib.error

    if isinstance(e, urllib.error.HTTPError):
        return e.code == 429 or e.code >= 500
    return isinstance(e, (urllib.error.URLError, ConnectionError, socket.timeout, TimeoutError, OSError))


def _fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "showroom-keeper (research; contact via repository)"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read().decode("utf-8", "replace")


def _stamp_time(stamp: str) -> float:
    return datetime.strptime(stamp[:14], "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc).timestamp()


def classify(original: str) -> tuple[str, str, int | None] | None:
    """保存されている URL を (種類, イベントのキー, ルーム番号) にする。使わないページは None。"""
    u = urllib.parse.urlsplit(original)
    parts = [p for p in u.path.split("/") if p]
    if len(parts) == 3 and parts[:2] == ["event", "contribution"] and _KEY.match(parts[2]):
        room = urllib.parse.parse_qs(u.query).get("room_id", [""])[0]
        return ("contribution", parts[2], int(room)) if room.isdigit() else None
    if len(parts) == 2 and parts[0] == "event" and _KEY.match(parts[1]) and parts[1] not in NOT_EVENT_KEYS:
        return "event", parts[1], None
    return None


def _text(fragment: str) -> str:
    return html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", fragment))).strip()


def parse_event_page(page: str) -> dict[str, Any]:
    """イベントのページ: イベント番号・名前と、参加ルーム (順位・ルーム番号・URL のキー・名前)。"""
    ids = re.findall(r'event_id["\'\s:=]+["\']?(\d+)', page)
    event_id = max(set(ids), key=ids.count) if ids else None
    title = re.search(r"<h2[^>]*>(.*?)</h2>", page, re.S)
    rooms = []
    for block in re.split(r'class="contentlist-link"', page)[1:]:
        rid = re.search(r"event/contribution/[^\"?]+\?room_id=(\d+)", block) or re.search(r'data-room-id="(\d+)"', block)
        if not rid:
            continue
        rank = re.search(r"is-rank-(\d+)", block)
        key = re.search(r'href="(?:https?://(?:www\.)?showroom-live\.com)?/([A-Za-z0-9_\-]+)"[^>]*class="room-url"', block)
        name = re.search(r'listcardinfo-main-text">(.*?)<', block, re.S)
        rooms.append({"room_id": int(rid.group(1)), "rank": int(rank.group(1)) if rank else None,
                      "key": key.group(1) if key else None, "name": _text(name.group(1)) if name else None})
    return {"event_id": int(event_id) if event_id else None, "name": _text(title.group(1)) if title else None,
            "rooms": list({r["room_id"]: r for r in rooms}.values())}


def parse_contribution_page(page: str) -> list[dict[str, Any]]:
    """貢献ランキングのページの「Event Contributor Ranking(Top 100)」(順位・名前・ポイント)。"""
    i = page.find(">Event Contributor Ranking")
    if i < 0:
        return []
    end = page.find("</table>", i)
    out = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", page[i:end if end > 0 else None], re.S):
        cells = [_text(c) for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
        if len(cells) == 3 and cells[0].isdigit() and cells[2].endswith("pt"):
            point = cells[2][:-2].replace(",", "").strip()
            if point.isdigit():
                out.append({"rank": int(cells[0]), "name": cells[1], "point": int(point)})
    return out


class ArchiveCollector:
    """Internet Archive の一覧を取り、ページを 1 つずつ読んで research.db に足す (1 つのスレッドの中で使う)。"""

    def __init__(self, state_dir: Path, fetch: Callable[[str], str] | None = None, throttle: Throttle | None = None,
                 clock: Callable[[], float] = time.time):
        self.state_dir = Path(state_dir)
        self._fetch = fetch or _fetch
        self.throttle = throttle or Throttle(ARCHIVE_GAP)
        self._clock = clock
        self._store: Store | None = None
        self.read = 0
        self.doing = ""
        self.last_error: str | None = None

    @property
    def store(self) -> Store:
        if self._store is None:
            self._store = Store(self.state_dir)
        return self._store

    def close(self) -> None:
        if self._store is not None:
            self._store.close()
            self._store = None

    def get(self, url: str) -> str:
        return self.throttle.call(self._fetch, url)

    # ---------------------------------------------------------------- 一覧
    def index_step(self) -> bool:
        """保存されているページの一覧を 1 ページ分読む (最初の 1 回だけ。全部で数十回)。"""
        st = self.store
        if st.meta("archive_index_done_at"):
            return False
        pages = st.meta("archive_index_pages")
        if pages is None:
            self.doing = "Internet Archive の一覧の大きさ"
            pages = int(self.get(f"{ARCHIVE}/cdx/search/cdx?url={INDEX_URL}&filter=statuscode:200&showNumPages=true").strip() or 0)
            st.set_meta("archive_index_pages", pages)
            return True
        n = int(st.meta("archive_index_next", 0))
        if n >= pages:
            st.set_meta("archive_index_done_at", self._clock())
            log.info("Internet Archive の一覧を読み終えました (%d ページ)", st.scalar("SELECT COUNT(*) FROM archive_pages"))
            return False
        self.doing = f"Internet Archive の一覧 ({n + 1}/{pages})"
        text = self.get(f"{ARCHIVE}/cdx/search/cdx?url={INDEX_URL}&fl=timestamp,original&filter=statuscode:200&page={n}")
        found: dict[str, dict[str, Any]] = {}
        for line in text.splitlines():
            stamp, _, original = line.strip().partition(" ")
            got = classify(original) if stamp.isdigit() else None
            if not got:
                continue
            kind, key, room = got
            page = f"{kind}:{key}" + (f":{room}" if room else "")
            item = found.setdefault(page, {"kind": kind, "key": key, "room": room, "url": original, "stamps": set()})
            item["stamps"].add(stamp)
            if stamp >= max(item["stamps"]):
                item["url"] = original
        with st.tx() as c:
            for page, item in found.items():
                row = c.execute("SELECT stamps FROM archive_pages WHERE page=?", (page,)).fetchone()
                stamps = sorted(set(json.loads(row[0]) if row else []) | item["stamps"])
                c.execute("""INSERT INTO archive_pages(page, kind, event_key, room_id, url, stamps) VALUES (?, ?, ?, ?, ?, ?)
                             ON CONFLICT(page) DO UPDATE SET stamps=excluded.stamps, url=excluded.url""",
                          (page, item["kind"], item["key"], item["room"], item["url"], json.dumps(stamps)))
            st.set_meta("archive_index_next", n + 1)
        return True

    # ---------------------------------------------------------------- ページを読む
    def _next_page(self) -> Any:
        st = self.store
        page = st.one("SELECT * FROM archive_pages WHERE kind='event' AND fetched_at IS NULL LIMIT 1")
        if page is not None:
            return page
        # 貢献ランキング: 消えたルーム → 退会したユーザーがいる → 突き合わせられる (SHOWROOM から取ってある) → 残り の順
        return st.one("""SELECT p.*, e.event_id FROM archive_pages p JOIN events e ON e.key = p.event_key
                         LEFT JOIN rooms r ON r.room_id = p.room_id
                         WHERE p.kind='contribution' AND p.fetched_at IS NULL
                         ORDER BY COALESCE(r.gone, 0) = 0,
                           NOT EXISTS (SELECT 1 FROM contributions c JOIN users u USING(user_id)
                                       WHERE c.event_id=e.event_id AND c.room_id=p.room_id AND u.name IN ({_LEFT})),
                           NOT EXISTS (SELECT 1 FROM contributions c WHERE c.event_id=e.event_id AND c.room_id=p.room_id)
                         LIMIT 1""".replace("{_LEFT}", _LEFT), tuple(sorted(LEFT_NAMES)))

    def page_step(self) -> bool:
        """保存されているページを 1 つ読む。読むものが無ければ False。"""
        page = self._next_page()
        if page is None:
            return False
        stamps = json.loads(page["stamps"] or "[]")
        tries = int(page["tries"] or 0)
        if tries >= min(PAGE_TRIES, len(stamps)):
            self._done(page["page"], 0)
            return True
        stamp = stamps[-1 - tries]  # 新しい保存から (古い保存はイベントの途中のことがある)
        self.doing = f"Internet Archive ({page['kind']} {page['event_key']}{' ルーム ' + str(page['room_id']) if page['room_id'] else ''})"
        try:
            text = self.get(f"{ARCHIVE}/web/{stamp}id_/{page['url']}")
        except Exception as e:  # noqa: BLE001
            if not is_missing(e):
                # つながらない・断られた・混んでいる (429・5xx) 時は、ページのせいではないので、試した数に数えない
                # (数えると、Internet Archive に断られているあいだに、読めるはずのページを「読み終えた」にしてしまう)
                if not _not_page_fault(e):
                    self.store.conn.execute("UPDATE archive_pages SET tries=tries+1 WHERE page=?", (page["page"],))
                raise
            text = ""
        at = _stamp_time(stamp)
        n = self._apply_event(page, text, at) if page["kind"] == "event" else self._apply_contribution(page, text, at)
        self.read += 1
        if n:
            self._done(page["page"], n)
        else:  # エラーのページなどは、1 つ前の保存を試す
            self.store.conn.execute("UPDATE archive_pages SET tries=tries+1 WHERE page=?", (page["page"],))
        if self.read % REMATCH_EVERY == 0:
            self.rematch()
        return True

    def _done(self, page: str, rows: int) -> None:
        self.store.conn.execute("UPDATE archive_pages SET fetched_at=?, rows=? WHERE page=?", (self._clock(), rows, page))

    def _apply_event(self, page: Any, text: str, at: float) -> int:
        info = parse_event_page(text)
        st, now = self.store, self._clock()
        # 新しいイベントのページにはイベント番号が無いので、SHOWROOM から取ったイベントのキーで引く
        known = st.one("SELECT event_id FROM events WHERE key=? ORDER BY event_id DESC LIMIT 1", (page["event_key"],))
        eid = info["event_id"] or (known["event_id"] if known else None)
        if not eid or not info["rooms"]:
            return 0
        with st.tx() as c:
            c.execute("INSERT OR IGNORE INTO events(event_id) VALUES (?)", (eid,))
            c.execute("UPDATE events SET key=COALESCE(key, ?), name=COALESCE(name, ?) WHERE event_id=?",
                      (page["event_key"], info["name"], eid))
            for r in info["rooms"]:
                known = c.execute("SELECT COALESCE(gone, 0) FROM rooms WHERE room_id=?", (r["room_id"],)).fetchone()
                # 今の SHOWROOM で見つかっていないルームだけ足す (あとで全ルームの収集が 1 回だけ確かめ、無ければ gone にする)
                c.execute("""INSERT OR IGNORE INTO rooms(room_id, key, name, found_via, found_at) VALUES (?, ?, ?, 'archive', ?)""",
                          (r["room_id"], r["key"], r["name"], now))
                # 消えたルームの貢献ランキングは SHOWROOM から取れないので取りに行かない。まだ見つかっていないルームは、
                # 全ルームの収集がルームを開けて、あると分かってから取る (sweep._next_room が保留を外す)
                c.execute("""INSERT OR IGNORE INTO room_events(room_id, event_id, rank, failures) VALUES (?, ?, ?, ?)""",
                          (r["room_id"], eid, r["rank"], ARCHIVE_HOLD if known is None or known[0] else 0))
        return len(info["rooms"])

    def _apply_contribution(self, page: Any, text: str, at: float) -> int:
        rows = parse_contribution_page(text)
        if not rows:
            return 0
        st, eid, rid = self.store, int(page["event_id"]), int(page["room_id"])
        with st.tx() as c:
            c.executemany("""INSERT OR REPLACE INTO archive_contributions(event_id, room_id, rank, name, point, at)
                             VALUES (?, ?, ?, ?, ?, ?)""", [(eid, rid, r["rank"], r["name"], r["point"], at) for r in rows])
        self._match(eid, rid)
        return len(rows)

    # ---------------------------------------------------------------- 突き合わせ
    # 確かな時だけ結び付ける (2026-10-29 に M さんが「過去の名前の紐付けも間違いが多い、不確かなものは消して良い」)。
    # 前は、同点の人・イベントの途中の保存・同じページに同じ名前が 2 行・あとから同じ名前の人が見つかった時も結び付けていて、
    # 別々の人を 1 人にまとめることがあった (project の資料「Geminiへの回答_過去名と用語_2026-10-26」の 1 章)。
    # 見た行には checked=1 を付ける (結び付けなかった行を、何度も見直さない)
    def _match(self, event_id: int, room_id: int) -> None:
        """1 つのイベント・ルームの、Internet Archive の貢献ランキングをユーザーに結び付ける。

        - SHOWROOM から取った貢献ランキングがある: イベントが終わったあとの保存で、順位とポイントが両方同じ人が 1 人だけの時
        - 無い (消えたルームなど。「名前で推測」): その名前がページに 1 行だけ・今の名前と SHOWROOM で見た前の名前で 1 人に決まる・
          その人がイベントの前後 MATCH_NEAR 秒に SHOWROOM の貢献ランキングに出ている、の全部の時
        - どちらも、同じページで 2 行以上が同じ人になったら、その人はどの行にも結び付けない
        """
        st = self.store
        event_id, room_id = int(event_id), int(room_id)
        rows = st.all("SELECT rank, name, point, at FROM archive_contributions WHERE event_id=? AND room_id=?", (event_id, room_id))
        if not rows:
            return
        ev = st.one("SELECT end FROM events WHERE event_id=?", (event_id,))
        end = ev["end"] if ev is not None else None
        api = st.all("SELECT user_id, rank, point FROM contributions WHERE event_id=? AND room_id=? AND guessed IS NULL",
                     (event_id, room_id))
        links: dict[tuple[int, str], int] = {}
        if api:
            same: dict[tuple[Any, Any], list[int]] = {}
            for c in api:
                same.setdefault((c["rank"], c["point"]), []).append(c["user_id"])
            for r in rows:
                hit = same.get((r["rank"], r["point"])) or []
                if end is not None and r["at"] is not None and r["at"] >= end and len(hit) == 1:
                    links[(r["rank"], r["name"])] = hit[0]
        elif end is not None:
            count: dict[str, int] = {}
            for r in rows:
                count[r["name"]] = count.get(r["name"], 0) + 1
            for r in rows:
                name = r["name"] or ""
                if len(name) < 2 or count[name] > 1 or name in LEFT_NAMES:
                    continue
                ids = {x[0] for x in st.all("""SELECT user_id FROM users WHERE name=?
                                               UNION SELECT user_id FROM user_names WHERE name=? AND source='showroom'""", (name, name))}
                if len(ids) == 1 and self._seen_near(next(iter(ids)), end):
                    links[(r["rank"], name)] = next(iter(ids))
        claimed: dict[int, int] = {}
        for uid in links.values():
            claimed[uid] = claimed.get(uid, 0) + 1
        links = {k: u for k, u in links.items() if claimed[u] == 1}
        with st.tx() as c:
            c.execute("UPDATE archive_contributions SET user_id=NULL, checked=1 WHERE event_id=? AND room_id=?", (event_id, room_id))
            c.executemany("UPDATE archive_contributions SET user_id=? WHERE event_id=? AND room_id=? AND rank=? AND name=?",
                          [(u, event_id, room_id, rank, name) for (rank, name), u in links.items()])
            c.execute("""INSERT OR IGNORE INTO user_names(user_id, name, at, source)
                         SELECT a.user_id, a.name, a.at, 'archive' FROM archive_contributions a JOIN users u USING(user_id)
                         WHERE a.event_id=? AND a.room_id=? AND a.user_id IS NOT NULL AND a.name != '' AND a.name != u.name
                           AND a.name NOT IN ({_LEFT})""".replace("{_LEFT}", _LEFT), (event_id, room_id, *sorted(LEFT_NAMES)))
        if not api:
            matched = [r for r in rows if (r["rank"], r["name"]) in links]
            if matched:
                st.set_contributions(event_id, room_id, [{"user_id": links[(r["rank"], r["name"])], "name": None, "point": r["point"],
                                                          "rank": r["rank"], "guessed": True} for r in matched], self._clock(), final=True)
            else:
                st.drop_guessed(event_id, room_id)

    def _seen_near(self, uid: int, end: float) -> bool:
        """その人が、イベントの終わりの前後 MATCH_NEAR 秒に終わったイベントの、SHOWROOM の貢献ランキングに出ているか
        (人の索引 contributions_user_pt で 1 人分を読む)。"""
        return self.store.one("""SELECT 1 FROM contributions c JOIN events e ON e.event_id = c.event_id
                                 WHERE c.user_id=? AND c.guessed IS NULL AND e.end BETWEEN ? AND ? LIMIT 1""",
                              (int(uid), end - MATCH_NEAR, end + MATCH_NEAR)) is not None

    def rematch(self) -> int:
        """あとから SHOWROOM の貢献ランキングが取れたイベント・ルームを、まとめて突き合わせ直す。"""
        pairs = self.store.all("""SELECT DISTINCT a.event_id, a.room_id FROM archive_contributions a
                                  WHERE a.user_id IS NULL AND a.checked IS NULL AND EXISTS (SELECT 1 FROM contributions c
                                    WHERE c.event_id=a.event_id AND c.room_id=a.room_id AND c.rank=a.rank AND c.point=a.point
                                      AND c.guessed IS NULL)
                                  LIMIT 500""")
        for p in pairs:
            self._match(p["event_id"], p["room_id"])
        return len(pairs)

    def recheck_step(self) -> bool:
        """前の決まりで結び付けた分を、今の決まりで 1 回だけ見直す (少しずつ。終われば meta の archive_recheck が done)。

        1. Internet Archive から足した前の名前 (user_names の source='archive') を消す (確かなものは 2 で足し直す)
        2. 貢献ランキングのページを 1 つずつ _match し直す (確かでない「名前で推測」の貢献は消える)
        検索の索引に残った前の名前は、検索の時に user_names にあるものだけを出す (explore.search)。"""
        st = self.store
        state = st.meta("archive_recheck") or {}
        if state == "done":
            return False
        if memory.holding():
            return False  # 視聴を先にする
        if not state.get("names_done"):
            after = state.get("names") or [-1, ""]
            rows = st.all("""SELECT user_id, name, source FROM user_names WHERE (user_id, name) > (?, ?)
                             ORDER BY user_id, name LIMIT ?""", (*after, RECHECK_NAMES))
            with st.tx() as c:
                c.executemany("DELETE FROM user_names WHERE user_id=? AND name=?",
                              [(r["user_id"], r["name"]) for r in rows if r["source"] == "archive"])
            state = {**state, "names": [rows[-1]["user_id"], rows[-1]["name"]] if rows else after,
                     "names_done": len(rows) < RECHECK_NAMES}
            worked = bool(rows)
        else:
            after = state.get("pages") or [-1, -1]
            pairs = st.all("""SELECT DISTINCT event_id, room_id FROM archive_contributions WHERE (event_id, room_id) > (?, ?)
                              ORDER BY event_id, room_id LIMIT ?""", (*after, RECHECK_PAGES))
            for p in pairs:
                self._match(p["event_id"], p["room_id"])
            if len(pairs) < RECHECK_PAGES:
                st.set_meta("archive_recheck", "done")
                log.info("Internet Archive から結び付けた前の名前と「名前で推測」の貢献を、確かなものだけに見直しました")
                return True
            state = {**state, "pages": [pairs[-1]["event_id"], pairs[-1]["room_id"]]}
            worked = True
        st.set_meta("archive_recheck", state)
        if worked:
            time.sleep(RECHECK_PAUSE)  # 収集と視聴の書き込みを長く待たせない
        return True

    def step(self) -> bool:
        self._requeue_once()
        return self.recheck_step() or self.index_step() or self.page_step()

    def _requeue_once(self) -> None:
        """前の版は、Internet Archive に断られた (つながらない) 回もページの試した数に数え、読めるはずのページを
        「読み終えた (0 行)」にしていた。そういうページを 1 回だけ読み直しの列に戻す (本当に中身の無いページも戻るが、数回聞くだけ)。"""
        st = self.store
        if st.meta("archive_requeued"):
            return
        with st.tx() as c:
            n = c.execute("""UPDATE archive_pages SET tries=0, fetched_at=NULL
                             WHERE rows=0 OR (fetched_at IS NULL AND tries > 0)""").rowcount
            c.execute("INSERT INTO meta(k, v) VALUES ('archive_requeued', 'true') ON CONFLICT(k) DO UPDATE SET v=excluded.v")
        if n:
            log.info("Internet Archive の読めなかったページ %d 件を、読み直しの列に戻しました", n)


def status(store: Store) -> dict[str, Any]:
    """Internet Archive から取り戻した分 (管理画面の上の欄に出す)。"""
    pages = {r["kind"]: (r["n"], r["done"]) for r in store.all(
        "SELECT kind, COUNT(*) AS n, COUNT(fetched_at) AS done FROM archive_pages GROUP BY kind")}
    return {"index_done": bool(store.meta("archive_index_done_at")),
            "events": pages.get("event", (0, 0)), "contributions": pages.get("contribution", (0, 0)),
            "rooms": store.scalar("SELECT COUNT(*) FROM rooms WHERE found_via='archive'"),
            "gone_rooms": store.scalar("SELECT COUNT(*) FROM rooms WHERE gone=1"),
            "names": store.scalar("SELECT COUNT(*) FROM user_names WHERE source='archive'"),
            "left_named": store.scalar(f"""SELECT COUNT(DISTINCT n.user_id) FROM user_names n JOIN users u USING(user_id)
                                           WHERE u.name IN ({','.join('?' * len(LEFT_NAMES))})""", tuple(LEFT_NAMES))}


class ArchiveWorker:
    def __init__(self, state_dir: Path, gap: float = ARCHIVE_GAP):
        self.c = ArchiveCollector(state_dir, throttle=Throttle(gap, name="Internet Archive"))
        self.running = False
        self._stop = threading.Event()

    def run_forever(self) -> None:
        self.running = True
        log.info("Internet Archive から、消えたルームと前の名前を取り戻し始めます")
        try:
            while not self._stop.is_set():
                try:
                    worked = self.c.step()
                except Exception as e:  # noqa: BLE001 - 1 つの失敗で止めない
                    self.c.last_error = f"{self.c.doing}: {str(e).splitlines()[0] if str(e) else type(e).__name__}"
                    log.warning("Internet Archive から読めませんでした (%s)", self.c.last_error)
                    worked = True
                    self._stop.wait(30)
                if not worked:
                    self.c.doing = "待機中"
                    self.c.rematch()
                    self._stop.wait(IDLE)
        finally:
            self.running = False
            self.c.close()

    def state(self) -> dict[str, Any]:
        return {"enabled": True, "running": self.running, "doing": self.c.doing, "read": self.c.read,
                "last_error": self.c.last_error, "throttle": self.c.throttle.state()}


WORKER: ArchiveWorker | None = None


def start(state_dir: Path, gap: float = ARCHIVE_GAP) -> ArchiveWorker:
    global WORKER
    if WORKER is None or not WORKER.running:
        WORKER = ArchiveWorker(state_dir, gap)
        threading.Thread(target=WORKER.run_forever, daemon=True, name="archive").start()
    return WORKER


def state() -> dict[str, Any]:
    return WORKER.state() if WORKER is not None else {"enabled": False}


