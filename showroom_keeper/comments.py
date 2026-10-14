"""手動コメントの受け渡し。

`showroom-keeper comment "..."` がキューディレクトリに依頼ファイルを置き、
常駐中の keeper がそれを拾って開いている配信ページから投稿し、結果ファイルを書く。
人が 1 件ずつ送ることを前提にしており、連投を防ぐため投稿間隔に下限を設けている。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol

from .config import Config

log = logging.getLogger("comments")

MAX_LENGTH = 50  # SHOWROOM のコメント上限
MIN_INTERVAL = 3.0  # 投稿と投稿の最小間隔 (秒)
REQUEST_TTL = 60.0  # これより古い依頼は投稿しない (keeper 停止中に溜まった依頼の誤投稿防止)


def queue_dir(cfg: Config) -> Path:
    return Path(cfg.state_file).parent / "comments"


def validate(text: str) -> str:
    text = " ".join(text.split())
    if not text:
        raise ValueError("コメントが空です")
    if len(text) > MAX_LENGTH:
        raise ValueError(f"コメントは {MAX_LENGTH} 文字以内にしてください ({len(text)} 文字)")
    return text


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def enqueue(qdir: Path, text: str, room: str | None = None, now: float | None = None) -> str:
    # ファイル名順 = 依頼順になるよう、ナノ秒の時刻を固定幅で先頭に付ける
    req_id = f"{time.time_ns():020d}-{uuid.uuid4().hex[:8]}"
    _write_json(qdir / f"{req_id}.req.json", {"text": validate(text), "room": room, "at": now or time.time()})
    return req_id


async def wait_result(qdir: Path, req_id: str, timeout: float = 30.0) -> dict[str, Any] | None:
    path = qdir / f"{req_id}.result.json"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            path.unlink(missing_ok=True)
            return data
        await asyncio.sleep(0.5)
    # 受け取られなかった依頼は取り消す (後から勝手に投稿されないように)
    (qdir / f"{req_id}.req.json").unlink(missing_ok=True)
    return None


class CommentTarget(Protocol):
    key: str

    async def post_comment(self, text: str) -> tuple[bool, str]: ...


class CommentDispatcher:
    """キューを監視し、依頼を該当ルームのウォッチャーに渡して投稿させる。"""

    def __init__(
        self,
        qdir: Path,
        targets: list[CommentTarget],
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        min_interval: float = MIN_INTERVAL,
    ):
        self.qdir = qdir
        self.targets = {t.key: t for t in targets}
        self._clock = clock
        self._sleep = sleep
        self.min_interval = min_interval
        self._last_post: float | None = None

    async def run(self, poll_interval: float = 1.0) -> None:
        self.qdir.mkdir(parents=True, exist_ok=True)
        while True:
            await self.process_pending()
            await self._sleep(poll_interval)

    async def process_pending(self) -> None:
        for req in sorted(self.qdir.glob("*.req.json")):
            req_id = req.name[: -len(".req.json")]
            try:
                data = json.loads(req.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            req.unlink(missing_ok=True)
            ok, message = await self._handle(data)
            log.info("コメント%s: %s (%s)", "投稿" if ok else "失敗", data.get("text"), message)
            _write_json(self.qdir / f"{req_id}.result.json", {"ok": ok, "message": message})

    async def _handle(self, data: dict[str, Any]) -> tuple[bool, str]:
        if self._clock() - float(data.get("at", 0)) > REQUEST_TTL:
            return False, "依頼が古いため投稿しませんでした"
        try:
            text = validate(str(data.get("text", "")))
        except ValueError as e:
            return False, str(e)
        room = data.get("room")
        if room:
            target = self.targets.get(room)
            if target is None:
                return False, f"監視していないルームです: {room}"
        elif len(self.targets) == 1:
            target = next(iter(self.targets.values()))
        else:
            return False, f"ルームを --room で指定してください ({', '.join(self.targets)})"

        if self._last_post is not None:
            wait = self.min_interval - (self._clock() - self._last_post)
            if wait > 0:
                await self._sleep(wait)
        try:
            ok, message = await target.post_comment(text)
        except Exception as e:  # noqa: BLE001
            ok, message = False, f"投稿中にエラーが発生しました: {e}"
        self._last_post = self._clock()
        return ok, message
