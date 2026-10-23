"""配信ページがリアルタイム通信 (WebSocket) で受け取るイベントから、推定アクティブ人数を数える。

SHOWROOM のクラシック形式の配信は同時接続数を公開していないため、その目安として
直近一定時間に「入室・コメント・ギフト」のいずれかをしたユーザー数を数える。
入室のお知らせが全員分出るとは限らず、見ているだけの人は数えられないため、実際の同接より少なくなる。
ページが受信しているデータを読むだけで、こちらから送信はしない。
"""

from __future__ import annotations

import json

# WebSocket メッセージの種類 (t): 1=コメント, 2=ギフト, 18=入室のお知らせ
ACTIVITY_TYPES = {1, 2, 18}
WINDOW = 600  # 数える期間 (秒)


def parse_frame(payload: str | bytes) -> tuple[str, dict] | None:
    """`MSG\t<key>\t<json>` 形式のメッセージを (配信の印 key, 辞書) にする。
    key はどの配信の知らせかを表す (ページはほかの配信の知らせも受け取ることがある)。"""
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8", errors="replace")
    if not isinstance(payload, str) or not payload.startswith("MSG\t"):
        return None
    parts = payload.split("\t", 2)
    if len(parts) < 3:
        return None
    try:
        data = json.loads(parts[2])
    except ValueError:
        return None
    return (parts[1], data) if isinstance(data, dict) else None


def parse_message(payload: str | bytes) -> dict | None:
    """`MSG\t<key>\t<json>` 形式のメッセージを辞書にする。"""
    frame = parse_frame(payload)
    return frame[1] if frame else None


def parse_event(payload: str | bytes) -> tuple[int, int] | None:
    """メッセージから (種類, ユーザー ID) を取り出す。"""
    data = parse_message(payload)
    try:
        return (int(data["t"]), int(data["u"])) if data else None
    except (KeyError, TypeError, ValueError):
        return None


class ActivityTracker:
    def __init__(self, window: float = WINDOW):
        self.window = window
        self.started_at: float | None = None
        self._last_seen: dict[int, float] = {}

    def reset(self, now: float) -> None:
        self.started_at = now
        self._last_seen.clear()

    def feed(self, payload: str | bytes, now: float) -> None:
        event = parse_event(payload)
        if event and event[0] in ACTIVITY_TYPES:
            self._last_seen[event[1]] = now

    def count(self, now: float) -> int:
        self._last_seen = {u: t for u, t in self._last_seen.items() if now - t <= self.window}
        return len(self._last_seen)

    def observed_seconds(self, now: float) -> float:
        return 0.0 if self.started_at is None else now - self.started_at
