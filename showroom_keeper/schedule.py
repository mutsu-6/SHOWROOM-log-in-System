"""決まった曜日・時刻 (日本時間) の予定。ギフトの一覧の更新などに使う。

例: 曜日 "mon"、時刻 "0:00,3:00,12:00,15:00,18:00" → 毎週月曜日の 0 時・3 時・12 時・15 時・18 時。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from .status import JST

WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
            "月": 0, "火": 1, "水": 2, "木": 3, "金": 4, "土": 5, "日": 6}
WEEKDAY_NAMES = "月火水木金土日"


def parse_slots(days: str, times: str) -> list[tuple[int, int, int]]:
    """曜日 ("mon,thu" / "月,木" / "毎日" / "") と時刻 ("0:00,18:30") から [(曜日, 時, 分)] を作る。"""
    day_words = [d.strip().lower() for d in str(days).replace("、", ",").split(",") if d.strip()]
    if not day_words or day_words in (["daily"], ["毎日"], ["*"]):
        weekdays = list(range(7))
    else:
        weekdays = []
        for word in day_words:
            key = word if word in WEEKDAYS else word[:3]
            if key not in WEEKDAYS:
                raise ValueError(f"曜日が分かりません: {word!r} (mon〜sun / 月〜日 / 毎日)")
            weekdays.append(WEEKDAYS[key])
    slots = set()
    for item in str(times).replace("、", ",").split(","):
        item = item.strip()
        if not item:
            continue
        hour, _, minute = item.partition(":")
        h, m = int(hour), int(minute or 0)
        if not (0 <= h < 24 and 0 <= m < 60):
            raise ValueError(f"時刻が正しくありません: {item!r} (例 18:00)")
        slots.update((d, h, m) for d in weekdays)
    if not slots:
        raise ValueError("時刻を 1 つ以上指定してください (例 0:00,12:00)")
    return sorted(slots)


def _candidates(now: datetime, slots: list[tuple[int, int, int]]) -> list[datetime]:
    """now の前後 1 週間にある予定の時刻。"""
    monday = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    return [monday + timedelta(weeks=w, days=d, hours=h, minutes=m)
            for w in (-1, 0, 1) for d, h, m in slots]


def previous_time(now: float, slots: list[tuple[int, int, int]]) -> float:
    """now 以前で最も新しい予定の時刻 (UNIX 時刻)。"""
    dt = datetime.fromtimestamp(now, JST)
    return max(c for c in _candidates(dt, slots) if c <= dt).timestamp()


def next_time(now: float, slots: list[tuple[int, int, int]]) -> float:
    """now より後で最も近い予定の時刻 (UNIX 時刻)。"""
    dt = datetime.fromtimestamp(now, JST)
    return min(c for c in _candidates(dt, slots) if c > dt).timestamp()


def describe(slots: list[tuple[int, int, int]]) -> str:
    """「毎週月曜日 0:00・3:00」のような説明。"""
    by_time: dict[tuple[int, int], list[int]] = {}
    for d, h, m in slots:
        by_time.setdefault((h, m), []).append(d)
    days = sorted({tuple(v) for v in by_time.values()})
    times = "・".join(f"{h}:{m:02d}" for h, m in sorted(by_time))
    if len(days) == 1:
        d = days[0]
        label = "毎日" if len(d) == 7 else "毎週" + "・".join(WEEKDAY_NAMES[i] for i in d) + "曜日"
        return f"{label} {times}"
    return "、".join(f"{WEEKDAY_NAMES[d]} {h}:{m:02d}" for d, h, m in slots)
