"""視聴ミッションを自動でする (昼 3:00〜14:59・夜 15:00〜2:59 に 1 回ずつ)。

- ミッションの進み具合は GET /api/mission?room_id=… (どのルームで聞いても、デイリーは同じ。限定のミッションはルームごとに出る)
- 「視聴」は、ブラウザのページを開かず、入室 (/api/live/current_user) と /api/live/polling を 30 秒以上続けると数えられる
  (2026-10-22 にサーバーで確かめた。1 ルームごとに「配信を視聴しよう (n/20)」が 1 段進む。入室と greeting だけでは数えられない)
- コメントは POST /api/live/post_live_comment、報酬の受取は POST /api/mission/receive (どちらも CSRF トークンを付けたフォーム)
- きっかけ配信は配信中の一覧の live_type が 4。そのルームを今月と前月で 6 時間未満しか見ていない人だけが対象
  (current_user の is_targeted_discovery_live)。新人の配信は new_days_count_label (New1day〜New30day) が付いている
- 同じ配信は 1 回しか数えないので、見た配信 (live_id) を覚えておく
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable

from .entrance import Session
from .status import JST

log = logging.getLogger("mission")

STATE_FILE = "mission.json"
CHECK_EVERY = 60.0  # 始める時刻になったかを見る間隔 (秒)
POLL_EVERY = 30.0  # 視聴中に polling を送る間隔 (秒。配信ページと同じくらい)
ROOM_GAP = 2.0  # ルームとルームの間を空ける時間 (秒)
RECEIVE_GAP = 1.0  # 受取と受取の間 (秒)
RETRY_AFTER = 1800.0  # ルームが足りない・ログインしていない時に、同じ区切りの中で試し直すまで (秒)
KIKKAKE_MAX = 3  # 1 回に見るきっかけ配信の上限 (限定のミッションは 1 日 2 回ほど)
NEW_MAX = 3  # 1 回に見る新人の配信の上限
EXTRA_ROOMS = 5  # 「配信を視聴しよう (n/20)」が進まない時に、mission_rooms より多く試す数
LIVE_NORMAL, LIVE_KIKKAKE = 0, 4
NEW_GENRES = {762, 763, 764}  # New1day / New7day / New30day
COMMENT_OFF = {"", "off", "none", "false", "なし"}


class NotLoggedIn(RuntimeError):
    pass


def period_of(now: datetime) -> tuple[str, str, datetime, datetime]:
    """日本時間の now が入る区切り (名前, "day"/"night", 始まり, 終わり)。夜の 0:00〜2:59 は前の日の夜。"""
    now = now.astimezone(JST)
    base = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if now.hour < 3:
        base -= timedelta(days=1)
    if 3 <= now.hour < 15:
        kind, start, end = "day", base.replace(hour=3), base.replace(hour=15)
    else:
        kind, start, end = "night", base.replace(hour=15), base + timedelta(days=1, hours=3)
    return f"{base:%Y-%m-%d}-{kind}", kind, start, end


def parse_times(text: str) -> list[tuple[int, int]]:
    out = []
    for part in (text or "").replace("、", ",").split(","):
        part = part.strip()
        if not part:
            continue
        h, _, m = part.partition(":")
        hour, minute = int(h), int(m or 0)
        if not (0 <= hour < 24 and 0 <= minute < 60):
            raise ValueError(f"時刻が正しくありません: {part}")
        out.append((hour, minute))
    return out


def start_at(now: datetime, times: list[tuple[int, int]]) -> datetime | None:
    """今の区切りの中で、始める時刻 (区切りの中に時刻が無ければ None = この区切りはしない)。"""
    _, _, start, end = period_of(now)
    found = []
    for hour, minute in times:
        for day in (start, start + timedelta(days=1)):
            at = day.replace(hour=hour, minute=minute)
            if start <= at < end:
                found.append(at)
    return min(found) if found else None


def flatten(data: dict[str, Any]) -> list[dict[str, Any]]:
    """ミッションの一覧を 1 列に (デイリーは今の区切りの分だけ)。genre に "daily" / "limited" を付ける。"""
    out: list[dict[str, Any]] = []
    for g in data.get("genre_list") or []:
        if g.get("genre") == "daily":
            part = g.get(g.get("current_period") or "") or {}
            items = [*(part.get("continuous_mission") or []), *(part.get("single_mission") or [])]
            if part.get("composite_mission"):
                items.append(part["composite_mission"])
        else:
            items = list(g.get("all_days_mission") or [])
        out += [{**m, "genre": g.get("genre")} for m in items if isinstance(m, dict)]
    return out


def views_left(missions: list[dict[str, Any]]) -> int | None:
    """「配信を視聴しよう (n/20)」の残りの段 (無ければ None)。段は自動で進み、報酬は受け取るまで残る。"""
    for m in missions:
        if m.get("genre") == "daily" and "視聴" in str(m.get("title")) and int(m.get("max_level") or 1) > 1:
            done = int(m.get("current_value") or 0) >= int(m.get("target_value") or 1)
            return max(0, int(m["max_level"]) - int(m.get("current_level") or 1) + (0 if done else 1))
    return None


def comment_wanted(missions: list[dict[str, Any]]) -> bool:
    """毎日のコメントのミッション (きっかけ配信のものは除く) が残っているか。1 回送れば全部済む。"""
    return any("コメント" in str(m.get("title")) and "きっかけ" not in str(m.get("title"))
               and (m.get("genre") == "daily" or m.get("is_daily")) and m.get("is_active", 1)
               and int(m.get("current_value") or 0) < int(m.get("target_value") or 1) for m in missions)


def candidates(onlives: dict[str, Any], skip_keys: set[str], seen_lives: set[int],
               rng: random.Random | None = None) -> list[dict[str, Any]]:
    """見るルームの順 (きっかけ配信 → 新人 → ほか)。有料の配信・見続けているルーム・もう見た配信は外す。"""
    rng = rng or random.Random()
    rooms: dict[int, dict[str, Any]] = {}
    for g in onlives.get("onlives") or []:
        for live in g.get("lives") or []:
            rid, lid = live.get("room_id"), live.get("live_id")
            if not rid or not lid or rid in rooms or live.get("premium_room_type"):
                continue
            if live.get("room_url_key") in skip_keys or int(lid) in seen_lives:
                continue
            kind = live.get("live_type") or LIVE_NORMAL
            if kind not in (LIVE_NORMAL, LIVE_KIKKAKE):
                continue
            new = bool(live.get("new_days_count_label")) or live.get("genre_id") in NEW_GENRES
            rooms[rid] = {"room_id": int(rid), "live_id": int(lid), "key": live.get("room_url_key"),
                          "kind": "kikkake" if kind == LIVE_KIKKAKE else "new" if new else "normal"}
    groups = {k: [r for r in rooms.values() if r["kind"] == k] for k in ("kikkake", "new", "normal")}
    for rs in groups.values():
        rng.shuffle(rs)
    first = groups["kikkake"][:KIKKAKE_MAX] + groups["new"][:NEW_MAX]
    rest = groups["kikkake"][KIKKAKE_MAX:] + groups["new"][NEW_MAX:] + groups["normal"]
    rng.shuffle(rest)
    return first + [r for r in rest if r["kind"] != "kikkake"] + [r for r in rest if r["kind"] == "kikkake"]


class MissionRunner:
    def __init__(self, cfg: Any, cookies: Callable[[], Awaitable[list[dict[str, Any]]]],
                 fetch_onlives: Callable[[], Awaitable[dict[str, Any]]],
                 session: Callable[..., Session] = Session, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 clock: Callable[[], float] = time.time, rng: random.Random | None = None):
        self.cfg = cfg
        self._cookies = cookies
        self._fetch_onlives = fetch_onlives
        self._session = session
        self._sleep = sleep
        self._clock = clock
        self._rng = rng
        self._path = Path(cfg.state_file).parent / STATE_FILE
        self.state: dict[str, Any] = self._load()

    def _load(self) -> dict[str, Any]:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.state, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self._path)
        except OSError as e:
            log.debug("視聴ミッションの記録を保存できません: %s", e)

    def _period(self, name: str) -> dict[str, Any]:
        if self.state.get("period") != name:
            if self.state.get("period"):
                self.state["last"] = {k: v for k, v in self.state.items() if k not in ("last", "live_ids")}
            self.state = {"period": name, "live_ids": [], "views": 0, "kikkake": 0, "new": 0, "comments": 0,
                          "rewards": {}, "done": False, "retry_at": 0.0, "last": self.state.get("last")}
        return self.state

    async def run(self) -> None:
        try:
            times = parse_times(self.cfg.mission_times)
        except ValueError as e:
            log.warning("視聴ミッションを始める時刻が正しくありません (%s)。12:00,21:00 にします", e)
            times = [(12, 0), (21, 0)]
        log.info("視聴ミッションを自動でします (%s に始め、1 回に %d ルーム)", self.cfg.mission_times, self.cfg.mission_rooms)
        while True:
            try:
                await self.step(times)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - 次の確認で試し直す (視聴とログインの維持は止めない)
                log.warning("視聴ミッションで問題が起きました: %s", str(e).splitlines()[0] if str(e) else type(e).__name__)
                self.state["retry_at"] = self._clock() + RETRY_AFTER
                self._save()
            await self._sleep(CHECK_EVERY)

    async def step(self, times: list[tuple[int, int]]) -> None:
        now = datetime.fromtimestamp(self._clock(), JST)
        name, kind, _, end = period_of(now)
        at = start_at(now, times)
        st = self._period(name)
        if at is None or now < at or st["done"] or self._clock() < st.get("retry_at", 0):
            return
        await self.run_once(kind, end)

    async def run_once(self, kind: str, end: datetime) -> None:
        st = self.state
        s = self._session(await self._cookies())
        lives = await self._fetch_onlives()
        cands = candidates(lives, set(self.cfg.rooms) | set(self.cfg.no_visit_rooms), set(st["live_ids"]), self._rng)
        if not cands:
            st["retry_at"] = self._clock() + RETRY_AFTER
            self._save()
            log.info("視聴ミッション: 見られる配信がありません。%d 分後に試し直します", RETRY_AFTER // 60)
            return
        missions = flatten(await self._get_json(s, f"/api/mission?room_id={cands[0]['room_id']}"))
        left = views_left(missions)
        budget = self.cfg.mission_rooms + 2 * EXTRA_ROOMS - st["views"]  # 数えられない時に、試し直しで見続けないように
        want = self.cfg.mission_rooms - st["views"] if left is None else left
        want = max(0, min(want, self.cfg.mission_rooms, budget))
        tries = want + EXTRA_ROOMS
        last_room = cands[0]
        for room in cands:
            if want <= 0 or tries <= 0 or datetime.fromtimestamp(self._clock(), JST) >= end - timedelta(minutes=2):
                break
            tries -= 1
            if not await self.view(s, room):
                continue
            last_room = room
            st["live_ids"].append(room["live_id"])
            st["views"] += 1
            if room["kind"] != "normal":
                st[room["kind"]] += 1
            want -= 1
            self._save()
            await self._sleep(ROOM_GAP)
        text = (self.cfg.mission_comment or "").strip()
        if text.lower() not in COMMENT_OFF and comment_wanted(missions) and st["views"]:
            await self.comment(s, last_room, text)
        got = await self.receive(s, last_room["room_id"])
        missions = flatten(await self._get_json(s, f"/api/mission?room_id={last_room['room_id']}"))
        left = views_left(missions)
        st["done"] = not left
        if left and st["views"] >= self.cfg.mission_rooms + 2 * EXTRA_ROOMS:
            st["done"] = True
            log.warning("視聴ミッション: %d ルームを見ても「配信を視聴しよう」が %d 段残っています。この区切りはここで止めます",
                        st["views"], left)
        if not st["done"]:
            st["retry_at"] = self._clock() + RETRY_AFTER
        self._save()
        rewards = "、".join(f"{k} {v}" for k, v in got.items()) or "なし"
        log.info("視聴ミッション (%s): %d ルームを見ました (きっかけ配信 %d・新人 %d)。コメント %d 回。受け取ったもの: %s%s",
                 "昼" if kind == "day" else "夜", st["views"], st["kikkake"], st["new"], st["comments"], rewards,
                 "" if st["done"] else f"。視聴の残り {left} 段 ({RETRY_AFTER // 60:.0f} 分後に続けます)")

    async def view(self, s: Session, room: dict[str, Any]) -> bool:
        """入室して、mission_view_seconds 秒のあいだ polling を送る。きっかけ配信の対象でなければ見ない。"""
        user = await self._get_json(s, f"/api/live/current_user?room_id={room['room_id']}")
        if not user.get("is_login"):
            raise NotLoggedIn("ログインしていません (次の確認で試し直します)")
        if room["kind"] == "kikkake" and not user.get("is_targeted_discovery_live"):
            return False
        waited = 0.0
        while True:
            await asyncio.to_thread(s.get, f"/api/live/polling?room_id={room['room_id']}")
            if waited >= self.cfg.mission_view_seconds:
                return True
            step = min(POLL_EVERY, self.cfg.mission_view_seconds - waited)
            await self._sleep(step)
            waited += step

    async def comment(self, s: Session, room: dict[str, Any], text: str) -> None:
        try:
            answer = json.loads(await asyncio.to_thread(
                s.post, "/api/live/post_live_comment", {"live_id": room["live_id"], "comment": text, "is_delay": 0}))
        except (OSError, ValueError, RuntimeError) as e:
            log.warning("視聴ミッションのコメントを送れませんでした: %s", e)
            return
        if answer.get("ok"):
            self.state["comments"] += 1
        else:
            log.warning("視聴ミッションのコメントを送れませんでした: %s", str(answer)[:100])

    async def receive(self, s: Session, room_id: int) -> dict[str, int]:
        """受け取れる報酬をすべて受け取る。{報酬の名前: 数}。"""
        got: dict[str, int] = {}
        for m in flatten(await self._get_json(s, f"/api/mission?room_id={room_id}")):
            if int(m.get("remain_reward") or 0) <= 0 or not m.get("is_active", 1) or not m.get("mission_id"):
                continue
            try:
                await asyncio.to_thread(s.post, "/api/mission/receive", {"mission_id": m["mission_id"]})
            except (OSError, ValueError, RuntimeError) as e:
                log.warning("ミッションの報酬を受け取れませんでした (%s): %s", m.get("title"), e)
                continue
            name = str(m.get("reward_name") or "?")
            got[name] = got.get(name, 0) + int(m.get("reward_num") or 0) * int(m["remain_reward"])
            await self._sleep(RECEIVE_GAP)
        for name, num in got.items():
            self.state["rewards"][name] = self.state["rewards"].get(name, 0) + num
        return got

    async def _get_json(self, s: Session, path: str) -> dict[str, Any]:
        data = json.loads(await asyncio.to_thread(s.get, path))
        return data if isinstance(data, dict) else {}
