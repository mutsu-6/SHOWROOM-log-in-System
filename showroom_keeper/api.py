"""SHOWROOM の公開 API ラッパー。

Playwright の APIRequestContext を使うため、ブラウザと Cookie (ログイン状態) を共有する。
SHOWROOM の非公式 API は予告なく変わり得るので、レスポンスの解釈は寛容にしている。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol

BASE_URL = "https://www.showroom-live.com"

log = logging.getLogger(__name__)


class RequestContext(Protocol):  # playwright.async_api.APIRequestContext と互換
    async def get(self, url: str, **kwargs: Any) -> Any: ...
    async def post(self, url: str, **kwargs: Any) -> Any: ...


@dataclass
class RoomStatus:
    room_url_key: str
    room_id: int | None
    room_name: str
    is_live: bool
    live_id: int | None = None

    @property
    def url(self) -> str:
        return f"{BASE_URL}/r/{self.room_url_key}"


def parse_room_status(room_url_key: str, data: dict[str, Any]) -> RoomStatus:
    is_live = data.get("is_live")
    if is_live is None:
        # 古い/別形式のレスポンス: live_status == 2 が配信中
        is_live = data.get("live_status") == 2 or bool(data.get("is_onlive"))
    room_id = data.get("room_id")
    return RoomStatus(
        room_url_key=room_url_key,
        room_id=int(room_id) if room_id not in (None, "") else None,
        room_name=str(data.get("room_name") or room_url_key),
        is_live=bool(is_live),
        live_id=int(data["live_id"]) if data.get("live_id") else None,
    )


def parse_login_state(data: Any) -> bool | None:
    """ログイン状態 API のレスポンスを解釈する。判定できなければ None。"""
    if not isinstance(data, dict):
        return None
    for key in ("is_login", "is_logged_in", "login"):
        if key in data:
            return bool(data[key])
    user_id = data.get("user_id")
    if user_id is not None:
        return str(user_id) not in ("", "0")
    return None


class ShowroomAPI:
    def __init__(self, request: RequestContext, timeout_ms: float = 15000):
        self._request = request
        self._timeout = timeout_ms

    async def _get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        resp = await self._request.get(f"{BASE_URL}{path}", params=params, timeout=self._timeout)
        if not resp.ok:
            raise RuntimeError(f"GET {path} が失敗しました: HTTP {resp.status}")
        return await resp.json()

    async def room_status(self, room_url_key: str) -> RoomStatus:
        data = await self._get_json("/api/room/status", {"room_url_key": room_url_key})
        return parse_room_status(room_url_key, data)

    async def is_logged_in(self, room_id: int | None = None) -> bool | None:
        """ログイン中なら True、未ログインなら False、判定不能なら None。"""
        try:
            if room_id is not None:
                data = await self._get_json("/api/live/current_user", {"room_id": room_id})
                state = parse_login_state(data)
                if state is not None:
                    return state
            return None
        except Exception as e:  # noqa: BLE001 - 判定不能として扱う
            log.debug("ログイン状態の確認に失敗: %s", e)
            return None

    async def login(self, account_id: str, password: str) -> bool:
        """ID/パスワードでログインする。CAPTCHA を要求された場合は False。"""
        token = await self._get_json("/api/csrf_token")
        csrf = token.get("csrf_token") if isinstance(token, dict) else None
        if not csrf:
            raise RuntimeError("CSRF トークンを取得できませんでした")
        resp = await self._request.post(
            f"{BASE_URL}/user/login",
            form={"account_id": account_id, "password": password, "csrf_token": csrf},
            timeout=self._timeout,
        )
        try:
            data = await resp.json()
        except Exception:  # noqa: BLE001
            data = {}
        if resp.ok and isinstance(data, dict) and data.get("ok"):
            return True
        if isinstance(data, dict) and data.get("captcha_url"):
            log.error("CAPTCHA が要求されました。`showroom-keeper login` で手動ログインしてください")
        else:
            log.error("ログインに失敗しました (HTTP %s): %s", resp.status, data.get("error") if isinstance(data, dict) else "")
        return False
