"""応援しているルームへギフターを誘導する作戦のための調査 (情報収集と分析)。

集めるもの (どれも SHOWROOM が公開している情報):
- ルームの累計ポイントランキング (上位 30 人の累計ポイントと来場回数) /api/live/summary_ranking
- ルームの過去のイベント /api/rooms/{room_id}/past_events と、開催中のイベント /api/room/event_and_support
- イベントごとの貢献ランキング (ルームごとに上位 100 人) /api/event/contribution_ranking
  (約 9 か月より前のイベントは SHOWROOM が空の一覧を返す)

問い合わせは 1 件ずつ間隔を空けて行い、終わったイベントの貢献ランキングは一度取ったら取り直さない。

分析すること:
- ギフターの行動のくせ (記録したギフトから: よく送る曜日・時間帯、1 回あたりのポイント、送るルームの偏り)
- イベントの貢献ランキングでの実績 (どのイベントで、どのルームに、何ポイント)
- 応援ルームとギフターが重なるルーム (似ているルーム) と、そこから誘導できそうなギフター (誘導の候補)
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .status import JST

log = logging.getLogger("research")

RESEARCH_DIR = "research"
REQUEST_GAP = 1.0  # SHOWROOM への問い合わせの間隔 (秒)
PAST_EVENT_PAGES = 10  # 過去のイベント一覧を読むページ数の上限 (1 ページ約 30 件)
ROOM_REFRESH = 20 * 3600  # 累計ランキングなどを取り直すまでの時間 (秒)
WEEKDAYS = "月火水木金土日"
LEFT_NAMES = {"退会済みユーザー"}  # SHOWROOM をやめたユーザー (誘導できないので候補に出さない)


# ------------------------------------------------------------------ 保存先
def research_dir(state_dir: Path) -> Path:
    return Path(state_dir) / RESEARCH_DIR


def _room_file(state_dir: Path, key: str) -> Path:
    return research_dir(state_dir) / "rooms" / f"{key}.json"


def _event_file(state_dir: Path, event_id: Any, room_id: Any) -> Path:
    return research_dir(state_dir) / "events" / f"{int(event_id)}_{int(room_id)}.json"


def _read(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


# ------------------------------------------------------------------ 情報収集
class Collector:
    """ルームごとの累計ランキング・イベント・貢献ランキングを集める。問い合わせは REQUEST_GAP 秒ずつ空ける。"""

    def __init__(self, state_dir: Path, get: Callable[[str], Any] | None = None,
                 sleep: Callable[[float], None] = time.sleep, gap: float = REQUEST_GAP,
                 clock: Callable[[], float] = time.time):
        from .gifts import _get

        self.state_dir = Path(state_dir)
        self._get = get or _get
        self._sleep = sleep
        self.gap = gap
        self._clock = clock
        self.requests = 0
        self.failures = 0

    def get(self, path: str) -> Any:
        if self.requests:
            self._sleep(self.gap)
        self.requests += 1
        return self._get(path)

    def collect(self, keys: list[str], force: bool = False,
                progress: Callable[[str], None] | None = None) -> dict[str, Any]:
        """keys のルームを順に集める。戻り値は {rooms, events, requests, failures}。"""
        rooms = events = 0
        for i, key in enumerate(dict.fromkeys(keys)):
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
        return {"rooms": rooms, "events": events, "requests": self.requests, "failures": self.failures}

    def collect_room(self, key: str, force: bool = False) -> int:
        """1 ルーム分を集めて保存する。戻り値は新しく取った貢献ランキングの数。"""
        from .listing import remember_rooms, room_names

        now = self._clock()
        path = _room_file(self.state_dir, key)
        old = _read(path) or {}
        info = room_names(self.state_dir).get(key) or {}
        room_id, name = info.get("room_id") or old.get("room_id"), info.get("name") or old.get("name")
        if not room_id:
            st = self.get(f"/api/room/status?room_url_key={key}")
            room_id, name = st.get("room_id"), st.get("room_name") or key
            if not room_id:
                raise ValueError("ルームが見つかりません")
            remember_rooms(self.state_dir, {key: (name, room_id)})

        fresh = not force and old.get("at") and now - float(old["at"]) < ROOM_REFRESH
        if fresh:
            room = old
        else:
            ranking = self.get(f"/api/live/summary_ranking?room_id={room_id}").get("ranking") or []
            past: list[dict[str, Any]] = []
            page: Any = 1
            while page and len(past) < PAST_EVENT_PAGES * 40 and page <= PAST_EVENT_PAGES:
                data = self.get(f"/api/rooms/{room_id}/past_events?page={page}")
                past += [e for e in data.get("events") or [] if isinstance(e, dict)]
                page = data.get("next_page")
            current = (self.get(f"/api/room/event_and_support?room_id={room_id}") or {}).get("event")
            events = [_event_info(e, current=False) for e in past if e.get("event_id")]
            if isinstance(current, dict) and current.get("event_id"):
                events = [e for e in events if e["event_id"] != current["event_id"]]
                events.insert(0, _event_info(current, current=True))
            room = {"key": key, "room_id": room_id, "name": name or key, "at": now,
                    "cumulative": [{"user_id": r.get("user_id"), "name": r.get("name"), "point": int(r.get("point") or 0),
                                    "visits": int(r.get("visit_count") or 0), "rank": r.get("rank")}
                                   for r in ranking if isinstance(r, dict) and r.get("user_id")],
                    "events": events}
            _write(path, room)

        added = 0
        for ev in room["events"]:
            if not ev.get("show_ranking"):
                continue
            epath = _event_file(self.state_dir, ev["event_id"], room_id)
            cached = _read(epath)
            ended = bool(ev.get("end")) and now > float(ev["end"]) + 3600
            if cached and (cached.get("final") or (not force and fresh)):
                continue
            data = self.get(f"/api/event/contribution_ranking?event_id={ev['event_id']}&room_id={room_id}")
            rows = [{"user_id": r.get("user_id"), "name": r.get("name"), "point": int(r.get("point") or 0), "rank": r.get("rank")}
                    for r in (data or {}).get("ranking") or [] if isinstance(r, dict) and r.get("user_id")]
            _write(epath, {"event_id": ev["event_id"], "room_id": room_id, "room": key, "name": ev["name"],
                           "start": ev.get("start"), "end": ev.get("end"), "at": now, "final": ended, "rows": rows})
            added += 1
        return added


def _event_info(e: dict[str, Any], current: bool) -> dict[str, Any]:
    url = e.get("event_url") or ""
    return {"event_id": e.get("event_id"), "name": e.get("event_name") or str(e.get("event_id")),
            "key": url.rstrip("/").rsplit("/", 1)[-1] if url else None,
            "start": e.get("started_at"), "end": e.get("ended_at"), "type": e.get("event_type"),
            "show_ranking": bool(e.get("show_ranking", 1)), "current": current}


# ------------------------------------------------------------------ 集めた情報の読み込み
def load_rooms(state_dir: Path) -> dict[str, dict[str, Any]]:
    folder = research_dir(state_dir) / "rooms"
    out = {}
    for p in sorted(folder.glob("*.json")) if folder.is_dir() else []:
        data = _read(p)
        if isinstance(data, dict) and data.get("key"):
            out[data["key"]] = data
    return out


def load_event_rankings(state_dir: Path) -> list[dict[str, Any]]:
    folder = research_dir(state_dir) / "events"
    out = []
    for p in sorted(folder.glob("*.json")) if folder.is_dir() else []:
        data = _read(p)
        if isinstance(data, dict) and isinstance(data.get("rows"), list):
            out.append(data)
    return out


def status(state_dir: Path, keys: list[str]) -> list[dict[str, Any]]:
    """ルームごとの収集の状況 (管理画面用)。"""
    rooms = load_rooms(state_dir)
    counts: dict[str, int] = defaultdict(int)
    for ev in load_event_rankings(state_dir):
        counts[ev.get("room")] += 1
    out = []
    for key in dict.fromkeys(keys):
        r = rooms.get(key) or {}
        out.append({"key": key, "name": r.get("name") or key, "at": r.get("at"), "cumulative": len(r.get("cumulative") or []),
                    "events": len(r.get("events") or []), "rankings": counts.get(key, 0)})
    return out


# ------------------------------------------------------------------ 分析
def _empty_user(uid: Any) -> dict[str, Any]:
    return {"user_id": uid, "name": None, "rooms": defaultdict(lambda: {"recorded": 0, "cumulative": 0, "visits": 0, "events": 0}),
            "events": [], "gifts": []}


def build_gifters(state_dir: Path) -> tuple[dict[Any, dict[str, Any]], dict[str, str]]:
    """記録したギフト・累計ランキング・貢献ランキングをギフターごとにまとめる。戻り値は (ギフター, ルーム名)。"""
    from .listing import _points, _records, gift_catalog, room_names

    users: dict[Any, dict[str, Any]] = {}
    names = {k: (v.get("name") or k) for k, v in room_names(state_dir).items()}

    def user(uid: Any) -> dict[str, Any]:
        return users.setdefault(uid, _empty_user(uid))

    for key, room in load_rooms(state_dir).items():
        names[key] = room.get("name") or names.get(key) or key
        for r in room.get("cumulative") or []:
            u = user(r["user_id"])
            u["name"] = u["name"] or r.get("name")
            slot = u["rooms"][key]
            slot["cumulative"] = max(slot["cumulative"], r["point"])
            slot["visits"] = max(slot["visits"], r.get("visits") or 0)
            slot["cumulative_rank"] = r.get("rank")
    for ev in load_event_rankings(state_dir):
        for r in ev["rows"]:
            u = user(r["user_id"])
            u["name"] = u["name"] or r.get("name")
            u["rooms"][ev["room"]]["events"] += r["point"]
            u["events"].append({"event_id": ev["event_id"], "event": ev.get("name"), "room": ev["room"],
                                "point": r["point"], "rank": r.get("rank"), "end": ev.get("end")})
    records = _records(state_dir, None, None)
    catalog = gift_catalog(state_dir, list(dict.fromkeys(key for key, _ in records)))
    for key, g in records:
        uid = g.get("user_id")
        if uid is None:
            continue
        pt, _free = _points(catalog, g)
        u = user(uid)
        u["name"] = g.get("name") or u["name"]  # 記録は新しい順ではないので、後のもので上書きする
        u["rooms"][key]["recorded"] += pt
        u["gifts"].append((g["at"], pt, key))
    return users, names


def room_score(slot: dict[str, Any]) -> int:
    """そのルームに送った (少なくとも) ポイント。累計ランキングはイベントの分も含むので、合計せず大きい方を使う。"""
    return max(slot["cumulative"], slot["events"], slot["recorded"])


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


def _summary(u: dict[str, Any], names: dict[str, str], support: set[str]) -> dict[str, Any]:
    scores = {k: room_score(v) for k, v in u["rooms"].items()}
    total = sum(scores.values())
    other = {k: s for k, s in scores.items() if k not in support and s > 0}
    top = sorted(other.items(), key=lambda kv: -kv[1])
    h = habits(u["gifts"])
    return {
        "user_id": u["user_id"], "name": u["name"] or "(名前なし)",
        "support_points": sum(s for k, s in scores.items() if k in support),
        "other_points": sum(other.values()), "total_points": total,
        "room_count": sum(1 for s in scores.values() if s > 0),
        # いちばん多く送るルームの割合 (1 に近いほど 1 つのルームに集中している)
        "focus": round(max(scores.values()) / total, 2) if total else None,
        "top_rooms": [{"key": k, "name": names.get(k, k), "points": s} for k, s in top[:3]],
        "event_count": len({e["event_id"] for e in u["events"]}),
        "event_points": sum(e["point"] for e in u["events"]),
        "best_rank": min((e["rank"] for e in u["events"] if e.get("rank")), default=None),
        "visits": max((v["visits"] for v in u["rooms"].values()), default=0),
        "habits": None if h is None else {k: h[k] for k in ("times", "top_weekday", "top_hours", "avg_point", "last_at")},
        "sources": sorted({src for v in u["rooms"].values() for src in ("recorded", "cumulative", "events") if v[src]}),
    }


PAIR_MIN_SHARED = 2  # ルーム同士の重なりに出す、共通のギフターの最少人数


def analyze(state_dir: Path, support: list[str], limit: int = 300) -> dict[str, Any]:
    """集めた情報をギフターとルームの両方から分析する。

    応援ルームがあれば、それを基準に誘導の候補・応援ルームのギフター・似ているルームを出す。
    無ければ (mode "all")、ギフター一覧 (分かったポイントの多い順)・ルーム同士の重なり・ルームごとのまとめを出す。
    """
    users, names = build_gifters(state_dir)
    sup = set(support)
    summaries = [_summary(u, names, sup) for u in users.values()]
    active = [s for s in summaries if s["name"] not in LEFT_NAMES]
    fans = sorted((s for s in summaries if s["support_points"] > 0), key=lambda s: -s["support_points"])
    # 応援ルームがあれば「応援ルームにはまだ送っておらず、ほかのルームに送っている人」(誘導の候補)、
    # 無ければ分かったギフター全員
    candidates = sorted((s for s in active if s["support_points"] == 0 and s["other_points"] > 0),
                        key=lambda s: -s["other_points"])

    givers: dict[str, set[Any]] = defaultdict(set)
    points: dict[str, int] = defaultdict(int)
    top: dict[str, tuple[int, Any]] = {}
    for u in users.values():
        for k, v in u["rooms"].items():
            score = room_score(v)
            if score > 0:
                givers[k].add(u["user_id"])
                points[k] += score
                if score > top.get(k, (0, None))[0]:
                    top[k] = (score, u["user_id"])

    # 似ているルーム: 応援ルームのギフターが、ほかにどのルームに送っているか
    fan_ids = set().union(*(givers.get(k, set()) for k in sup)) if sup else set()
    similar = []
    for k, ids in givers.items():
        shared = ids & fan_ids
        if k in sup or not shared:
            continue
        similar.append({"key": k, "name": names.get(k, k), "gifters": len(ids), "shared": len(shared),
                        "overlap": round(len(shared) / len(ids | fan_ids), 3), "candidates": len(ids - fan_ids)})
    similar.sort(key=lambda r: (-r["shared"], -r["overlap"]))

    # ルーム同士の重なり: どのルームとどのルームに、同じギフターが送っているか (ギフターの好みが近いルーム)
    keys = sorted(givers, key=lambda k: -len(givers[k]))
    pairs = []
    for i, a in enumerate(keys):
        for b in keys[i + 1:]:
            shared = givers[a] & givers[b]
            if len(shared) >= PAIR_MIN_SHARED:
                pairs.append({"a": a, "a_name": names.get(a, a), "b": b, "b_name": names.get(b, b), "shared": len(shared),
                              "overlap": round(len(shared) / len(givers[a] | givers[b]), 3)})
    pairs.sort(key=lambda r: (-r["shared"], -r["overlap"]))

    by_id = {s["user_id"]: s for s in summaries}
    researched = load_rooms(state_dir)
    rooms = sorted(({"key": k, "name": names.get(k, k), "gifters": len(ids), "points": points[k],
                     "multi": sum(1 for uid in ids if by_id[uid]["room_count"] > 1),
                     "events": len((researched.get(k) or {}).get("events") or []), "researched": k in researched,
                     "support": k in sup,
                     "top": {"user_id": top[k][1], "name": by_id[top[k][1]]["name"], "points": top[k][0]} if k in top else None}
                    for k, ids in givers.items()), key=lambda r: -r["points"])
    return {"mode": "support" if sup else "all",
            "support": [{"key": k, "name": names.get(k, k), "gifters": len(givers.get(k, set()))} for k in support],
            "candidates": candidates[:limit], "candidate_count": len(candidates),
            "fans": fans[:limit], "fan_count": len(fans), "similar": similar[:100],
            "pairs": pairs[:100], "rooms": rooms[:200], "room_count": len(rooms),
            "multi_count": sum(1 for s in active if s["room_count"] > 1),
            "gifter_count": len(users)}


def gifter_detail(state_dir: Path, user_id: Any, support: list[str]) -> dict[str, Any]:
    """1 人のギフターの詳しい情報 (ルームごとの内訳・イベントの実績・行動のくせ)。"""
    try:
        uid = int(str(user_id).strip())
    except (TypeError, ValueError):
        raise ValueError("ユーザー ID は数字で指定してください") from None
    users, names = build_gifters(state_dir)
    u = users.get(uid)
    if not u:
        return {"user_id": uid, "found": False}
    sup = set(support)
    rooms = sorted(({"key": k, "name": names.get(k, k), "support": k in sup, "points": room_score(v), **v}
                    for k, v in u["rooms"].items()), key=lambda r: -r["points"])
    events = sorted(u["events"], key=lambda e: -(e.get("end") or 0))
    for e in events:
        e["room_name"] = names.get(e["room"], e["room"])
    return {"user_id": uid, "found": True, "summary": _summary(u, names, sup), "rooms": rooms,
            "events": events, "habits": habits(u["gifts"])}


# ------------------------------------------------------------------ 収集の実行 (予定の時刻と管理画面から)
class ResearchJob:
    """情報収集を 1 つずつ実行する (予定の時刻と管理画面の「今すぐ集める」が重ならないように)。"""

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
                         f" / 失敗 {result['failures']} ルーム" if result["failures"] else "")
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
