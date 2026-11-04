"""ギフトの時間指定 (予約) 投げ。管理画面で予約し、決めた時刻にログイン中のアカウントで 1 回だけ送る。

守ること (2026-11-05 に利用者の「作れ」で作った。くわしくは docs/ギフトの予約投げ.md)
- **二重に送らない**: 送る前に「送信中」をファイルに残す。再起動などで「送信中」のまま残った予約は「不明」にして、送り直さない。
  送れたか分からない (時間切れ・接続が切れた) 時も「不明」。無効になるより、二重に送る方が高くつくため
- **送りすぎない**: 有料 (SG) は、1 回の予約の上限 `gift_paid_max` と、1 日 (日本時間) の上限 `gift_paid_daily_max` を超えて予約できない。
  既定はどちらも 0 で、有料は受け付けない。送る直前にも上限を確かめ直す
- **確かめてから予約する**: 画面は合計の SG を出し、同じ数を `confirm_sg` で送り返さないと予約できない
- **遅れたら送らない**: 予定の時刻から `gift_late_limit` 秒より遅れたら「見送り」。再起動のあと、過ぎた予約をまとめて送らない
- **視聴を止めない**: 問い合わせとファイルの読み書きは、すべて `asyncio.to_thread` の中でする (視聴の asyncio の中では待たない)。
  ファイルの鍵 (`_LOCK`) は、別のスレッドの中だけで持つ。視聴のブラウザ (Playwright) は使わず、ミッションと同じく
  ブラウザから Cookie を借りた別の問い合わせで送る
- **練習**: `gift_send` が off (既定) の間は、送る直前まで全部して、送らずに「練習」と残す (時刻の正確さを確かめられる)
- 収集の順番待ち (`research.THROTTLE`) には並ばない (締め切りに遅れるため)。予約は 1 件で数回しか問い合わせない

SHOWROOM の送り先 (`gift_free_path`・`gift_paid_path`) と、送る項目 (gift_id・live_id・num・is_delay・csrf_token) は、
**まだ確かめていない推定** (2026-11-05)。サーバーで確かめてから `gift_send` を on にする。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable

from .api import BASE_URL, parse_room_status
from .entrance import Session
from .status import JST

log = logging.getLogger("gift_plan")

PLAN_FILE = "gift_plan.json"
PREP = 15.0  # 送る何秒前に、配信の番号と CSRF トークンを取っておくか
POLL = 5.0  # 新しい予約を見に行く間隔 (秒)
MAX_AHEAD = 7 * 86400  # 何日先まで予約できるか
MIN_AHEAD = 20.0  # 今から何秒より先の時刻なら予約できるか (送る前の準備の時間)
MAX_WAITING = 30  # 待っている予約の数の上限
KEEP_DONE = 200  # 済んだ予約を残す数
FREE_NUMS = (1, 10)
PAID_NUMS = (1, 5, 10)
SEND_TIMEOUT = 10.0

WAITING, SENDING, SENT, FAILED, UNKNOWN, SKIPPED, CANCELLED, PRACTICE = (
    "予約", "送信中", "送った", "失敗", "不明", "見送り", "取り消し", "練習")
DONE = {SENT, FAILED, UNKNOWN, SKIPPED, CANCELLED, PRACTICE}
# 1 日の有料の合計に数える状態 (送ったかもしれないものは全部数える)
COUNTED = {WAITING, SENDING, SENT, UNKNOWN}

_LOCK = threading.Lock()  # 別のスレッド (管理画面・to_thread) の中だけで持つ。asyncio の中では持たない


def plan_path(state_dir: Path) -> Path:
    return Path(state_dir) / PLAN_FILE


def load(state_dir: Path) -> list[dict[str, Any]]:
    try:
        data = json.loads(plan_path(state_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    plans = data.get("plans") if isinstance(data, dict) else None
    return [p for p in plans or [] if isinstance(p, dict) and p.get("id")]


def _save(state_dir: Path, plans: list[dict[str, Any]]) -> None:
    waiting = [p for p in plans if p.get("state") not in DONE]
    done = sorted((p for p in plans if p.get("state") in DONE), key=lambda p: p.get("at") or 0)[-KEEP_DONE:]
    path = plan_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"plans": sorted(waiting + done, key=lambda p: p.get("at") or 0)}, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    os.replace(tmp, path)


def update(state_dir: Path, plan_id: str, **changes: Any) -> dict[str, Any] | None:
    """1 件の予約を書き換えて残す (スレッドの中で呼ぶ)。"""
    with _LOCK:
        plans = load(state_dir)
        hit = next((p for p in plans if p["id"] == plan_id), None)
        if hit is None:
            return None
        hit.update(changes)
        _save(state_dir, plans)
        return dict(hit)


def day_of(at: float) -> str:
    return datetime.fromtimestamp(at, JST).strftime("%Y-%m-%d")


def paid_on(plans: list[dict[str, Any]], day: str, skip: str | None = None) -> int:
    """その日 (日本時間) の有料の予約と送った分の合計 SG。"""
    return sum(int(p.get("sg") or 0) for p in plans
               if p.get("state") in COUNTED and p.get("id") != skip and day_of(p.get("at") or 0) == day)


def _public_get(path: str) -> Any:
    req = urllib.request.Request(BASE_URL + path, headers={"User-Agent": "showroom-keeper", "Accept-Language": "ja"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.load(resp)


def room_info(room_key: str, get: Callable[[str], Any] = _public_get) -> dict[str, Any]:
    data = get("/api/room/status?" + urllib.parse.urlencode({"room_url_key": room_key}))
    if not isinstance(data, dict) or not data.get("room_id"):
        raise ValueError(f"ルーム {room_key} が見つかりません")
    return {"room_id": int(data["room_id"]), "room_name": str(data.get("room_name") or room_key)}


def gift_catalog(room_id: int, get: Callable[[str], Any] = _public_get) -> list[dict[str, Any]]:
    """予約に使えるギフト (配信ページのギフト一覧)。無料が先、あとは安い順。"""
    data = get(f"/api/live/gift_list?room_id={room_id}")
    out = []
    for group in ("normal", "enquete"):
        for g in (data or {}).get(group) or []:
            if g.get("gift_id") is None:
                continue
            out.append({"gift_id": int(g["gift_id"]), "name": g.get("gift_name") or f"ギフト{g['gift_id']}",
                        "point": int(g.get("point") or 0), "free": bool(g.get("free")), "image": g.get("image")})
    return sorted(out, key=lambda g: (not g["free"], g["point"], g["gift_id"]))


def settings(cfg: Any) -> dict[str, Any]:
    return {"send": bool(getattr(cfg, "gift_send", False)), "paid_max": int(getattr(cfg, "gift_paid_max", 0) or 0),
            "paid_daily_max": int(getattr(cfg, "gift_paid_daily_max", 0) or 0),
            "late_limit": float(getattr(cfg, "gift_late_limit", 2.0) or 0)}


def overview(state_dir: Path, cfg: Any, now: float | None = None) -> dict[str, Any]:
    now = time.time() if now is None else now
    plans = load(state_dir)
    return {"plans": plans, "settings": settings(cfg), "paid_today": paid_on(plans, day_of(now)), "now": now}


def check_paid(cfg: Any, plans: list[dict[str, Any]], sg: int, at: float, skip: str | None = None) -> None:
    """有料の上限を超えるなら ValueError。"""
    if sg <= 0:
        return
    s = settings(cfg)
    if s["paid_max"] <= 0:
        raise ValueError("有料ギフトの予約は、上限 (SHOWROOM_GIFT_PAID_MAX) が 0 なので受け付けていません")
    if sg > s["paid_max"]:
        raise ValueError(f"1 回の有料の上限 {s['paid_max']:,} SG を超えています ({sg:,} SG)")
    used = paid_on(plans, day_of(at), skip)
    if used + sg > s["paid_daily_max"]:
        raise ValueError(f"その日の有料の上限 {s['paid_daily_max']:,} SG を超えます (予約・送った分 {used:,} SG + {sg:,} SG)")


def create(state_dir: Path, cfg: Any, data: dict[str, Any], now: float | None = None,
           get: Callable[[str], Any] = _public_get) -> dict[str, Any]:
    """管理画面からの予約 (管理画面のスレッドで呼ぶ)。"""
    from .config import normalize_room_key

    now = time.time() if now is None else now
    room_key = normalize_room_key(str(data.get("room") or ""))
    try:
        gift_id, num, at = int(data.get("gift_id")), int(data.get("num")), float(data.get("at"))
    except (TypeError, ValueError):
        raise ValueError("ギフト・個数・時刻を指定してください") from None
    if not now + MIN_AHEAD <= at <= now + MAX_AHEAD:
        raise ValueError(f"時刻は、今から {int(MIN_AHEAD)} 秒より先、{MAX_AHEAD // 86400} 日以内にしてください")
    room = room_info(room_key, get)
    gift = next((g for g in gift_catalog(room["room_id"], get) if g["gift_id"] == gift_id), None)
    if gift is None:
        raise ValueError("そのギフトは、このルームのギフト一覧にありません")
    if num not in (FREE_NUMS if gift["free"] else PAID_NUMS):
        raise ValueError(f"個数は {'・'.join(map(str, FREE_NUMS if gift['free'] else PAID_NUMS))} から選んでください")
    sg = 0 if gift["free"] else gift["point"] * num
    try:
        confirm = int(data.get("confirm_sg"))
    except (TypeError, ValueError):
        confirm = -1
    if confirm != sg:
        raise ValueError(f"確認の金額が合いません (このギフトは {sg:,} SG です)。画面を開き直して確かめてください")
    plan = {"id": uuid.uuid4().hex[:12], "room": room_key, "room_id": room["room_id"], "room_name": room["room_name"],
            "gift_id": gift_id, "gift_name": gift["name"], "point": gift["point"], "free": gift["free"], "num": num,
            "sg": sg, "at": at, "state": WAITING, "created": now, "memo": str(data.get("memo") or "")[:100]}
    with _LOCK:
        plans = load(state_dir)
        if sum(1 for p in plans if p.get("state") not in DONE) >= MAX_WAITING:
            raise ValueError(f"待っている予約が {MAX_WAITING} 件あります。済んでから予約してください")
        check_paid(cfg, plans, sg, at)
        plans.append(plan)
        _save(state_dir, plans)
    log.info("ギフトを予約しました: %s に %s × %d (%s SG) を %s", room["room_name"], gift["name"], num, f"{sg:,}",
             datetime.fromtimestamp(at, JST).strftime("%m-%d %H:%M:%S.%f")[:-3])
    return plan


def cancel(state_dir: Path, plan_id: str) -> dict[str, Any]:
    with _LOCK:
        plans = load(state_dir)
        hit = next((p for p in plans if p["id"] == plan_id), None)
        if hit is None:
            raise ValueError("その予約は見つかりません")
        if hit.get("state") != WAITING:
            raise ValueError(f"「{hit.get('state')}」の予約は取り消せません")
        hit["state"] = CANCELLED
        _save(state_dir, plans)
        return dict(hit)


def recover(state_dir: Path) -> int:
    """起動した時に「送信中」のまま残った予約を「不明」にする (送り直さない)。直した数。"""
    with _LOCK:
        plans = load(state_dir)
        hit = [p for p in plans if p.get("state") == SENDING]
        for p in hit:
            p["state"], p["note"] = UNKNOWN, "送る途中で止まりました。送れたかは SHOWROOM のギフトの履歴で確かめてください"
        if hit:
            _save(state_dir, plans)
        return len(hit)


def gift_form(plan: dict[str, Any], live_id: int) -> dict[str, Any]:
    return {"gift_id": plan["gift_id"], "live_id": live_id, "num": plan["num"], "is_delay": 0}


def judge(status: int | None, body: str) -> tuple[str, str]:
    """送った返事から状態を決める。分からないものは「不明」(送り直さない)。"""
    if status is None:
        return UNKNOWN, "返事がありません (時間切れか接続が切れた)。送れたかは履歴で確かめてください"
    try:
        data = json.loads(body)
    except ValueError:
        data = None
    if 200 <= status < 300:
        if isinstance(data, dict) and (data.get("error") or data.get("errors") or data.get("ok") is False):
            return FAILED, "SHOWROOM が断りました"
        return SENT, ""
    if 400 <= status < 500:
        return FAILED, f"SHOWROOM が断りました (HTTP {status})"
    return UNKNOWN, f"SHOWROOM の不調 (HTTP {status})。送れたかは履歴で確かめてください"


def send_at(session: Session, path: str, form: dict[str, Any], token: str, at: float, late_limit: float,
            clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep,
            send: bool = True) -> dict[str, Any]:
    """at まで待って 1 回だけ送る (スレッドの中で呼ぶ)。"""
    while True:
        left = at - clock()
        if left <= 0:
            break
        sleep(min(left, 0.5) if left > 0.02 else left)
    start = clock()
    if start - at > late_limit:
        return {"state": SKIPPED, "note": f"予定より {start - at:.1f} 秒遅れたので送りませんでした", "started": start}
    if not send:
        return {"state": PRACTICE, "note": "練習 (gift_send が off なので送っていません)", "started": start, "late": start - at}
    status, body = None, ""
    try:
        status, body = session.post_with(path, form, token, timeout=SEND_TIMEOUT)
    except urllib.error.HTTPError as e:
        status = e.code
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            body = ""
    except Exception as e:  # noqa: BLE001 - 時間切れ・接続切れは「不明」
        body = repr(e)
    done = clock()
    state, note = judge(status, body)
    return {"state": state, "note": note, "status": status, "reply": body[:300], "started": start, "done": done,
            "late": start - at, "took": done - start}


class GiftRunner:
    """予約を見張り、時刻になったら送る。視聴と同じ asyncio で動くが、待つものはすべてスレッドの中。"""

    def __init__(self, cfg: Any, cookies: Callable[[], Awaitable[list[dict[str, Any]]]], state_dir: Path,
                 session: Callable[..., Session] = Session, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 clock: Callable[[], float] = time.time, thread_sleep: Callable[[float], None] = time.sleep):
        self.cfg, self._cookies, self.state_dir = cfg, cookies, Path(state_dir)
        self._session, self._sleep, self._clock, self._thread_sleep = session, sleep, clock, thread_sleep

    async def run(self) -> None:
        fixed = await asyncio.to_thread(recover, self.state_dir)
        if fixed:
            log.warning("送る途中で止まったギフトの予約が %d 件あります。「不明」にしました (送り直しません)", fixed)
        while True:
            try:
                await self.step()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - 予約投げの失敗で視聴を止めない
                log.warning("ギフトの予約投げで問題: %s", e)
            await self._sleep(POLL)

    async def step(self) -> None:
        plans = await asyncio.to_thread(load, self.state_dir)
        now = self._clock()
        late = float(settings(self.cfg)["late_limit"])
        for p in sorted((p for p in plans if p.get("state") == WAITING), key=lambda p: p["at"]):
            if p["at"] + late < now:
                await asyncio.to_thread(update, self.state_dir, p["id"], state=SKIPPED,
                                        note="予定の時刻を過ぎていました (止まっていた間の予約は送りません)")
            elif p["at"] - now <= PREP + POLL:
                await self.fire(p)
                now = self._clock()

    async def fire(self, plan: dict[str, Any]) -> None:
        # 準備は PREP 秒前から (配信の番号と CSRF トークン)。それより前に起きたら待つ
        wait = plan["at"] - PREP - self._clock()
        if wait > 0:
            await self._sleep(wait)
        current = next((p for p in await asyncio.to_thread(load, self.state_dir) if p["id"] == plan["id"]), None)
        if current is None or current.get("state") != WAITING:
            return  # 取り消された
        s = settings(self.cfg)
        try:
            check_paid(self.cfg, await asyncio.to_thread(load, self.state_dir), int(plan.get("sg") or 0), plan["at"],
                       skip=plan["id"])
        except ValueError as e:
            await asyncio.to_thread(update, self.state_dir, plan["id"], state=SKIPPED, note=f"上限: {e}")
            return
        cookies = await self._cookies()
        session = self._session(cookies)
        try:
            live_id, token = await asyncio.to_thread(self._prepare, session, str(plan["room"]))
        except Exception as e:  # noqa: BLE001
            await asyncio.to_thread(update, self.state_dir, plan["id"], state=SKIPPED, note=f"準備できませんでした: {e}")
            return
        if not live_id:
            await asyncio.to_thread(update, self.state_dir, plan["id"], state=SKIPPED, note="配信していませんでした")
            return
        # ここから先は送ったかもしれないので、先に「送信中」を残す (止まったら「不明」になり、送り直さない)
        await asyncio.to_thread(update, self.state_dir, plan["id"], state=SENDING, live_id=live_id)
        path = self.cfg.gift_free_path if plan.get("free") else self.cfg.gift_paid_path
        result = await asyncio.to_thread(send_at, session, path, gift_form(plan, live_id), token, plan["at"],
                                         s["late_limit"], self._clock, self._thread_sleep, s["send"])
        await asyncio.to_thread(update, self.state_dir, plan["id"], **result)
        log.info("ギフトの予約: %s × %d → %s %s (遅れ %s 秒)", plan.get("gift_name"), plan.get("num"), result["state"],
                 result.get("note") or "", f"{result.get('late', 0):.3f}")

    @staticmethod
    def _prepare(session: Session, room_key: str) -> tuple[int | None, str]:
        status = parse_room_status(room_key, json.loads(session.get(
            "/api/room/status?" + urllib.parse.urlencode({"room_url_key": room_key}))))
        live_id = status.live_id if status.is_live else None
        token = json.loads(session.get("/api/csrf_token")).get("csrf_token")
        if not token:
            raise RuntimeError("CSRF トークンを取得できませんでした")
        return (int(live_id) if live_id else None), token
