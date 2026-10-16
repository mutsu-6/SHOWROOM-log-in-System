"""管理画面 (ブラウザで開く操作画面)。

`run` の実行中に標準ライブラリの HTTP サーバーを別スレッドで動かし、次をまとめて扱えるようにする。
- 配信状況・ログイン状態・視聴とギフト記録の様子 (ダッシュボード)
- コメントの投稿 (comment コマンドと同じ受け渡しを使い、投稿間隔の下限もそのまま効く)
- ギフト履歴・イベント集計の表示
- ルーム設定の変更 (保存すると keeper がブラウザとギフト記録をやり直して反映する)
- 最近のログ

既定では自分の PC (localhost) からだけ開ける。パスワードを設定した時だけ、他の端末からの接続を受け付ける。
"""

from __future__ import annotations

import base64
import hmac
import json
import logging
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .config import SETTINGS_KEYS, normalize_room_key, save_settings
from .status import LOGIN_KEY, fetch, read_live_stats

log = logging.getLogger("web")

PAGE = Path(__file__).with_name("admin.html")
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}
# 変えると視聴とギフト記録をやり直す設定 (応援ルーム・調べるルームは情報収集だけに使うので含めない)
WATCH_KEYS = ("rooms", "record_rooms", "events", "enter_rooms")
STATUS_TTL = 15  # 配信状況 (SHOWROOM への問い合わせ) を使い回す秒数
STORE_STATUS_TTL = 30  # 全ルームの収集の件数を使い回す秒数
_store_status: dict[str, Any] = {"at": 0.0, "data": None}
COMMENT_TIMEOUT = 80.0  # 投稿処理 (入力・送信・反映の確認) の上限に余裕を持たせる
CSRF_HEADER = "X-Showroom-Keeper"  # 変更を伴う要求に必須。他のサイトから送らせることはできない


class LogBuffer(logging.Handler):
    """最近のログを管理画面に出すために溜めておく。"""

    def __init__(self, size: int = 500):
        super().__init__(level=logging.INFO)
        self.records: deque[dict[str, Any]] = deque(maxlen=size)
        self.seq = 0
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001
            msg = str(record.msg)
        with self._lock:
            self.seq += 1
            self.records.append({"seq": self.seq, "at": record.created, "level": record.levelname,
                                 "name": record.name, "message": msg})

    def since(self, seq: int) -> list[dict[str, Any]]:
        with self._lock:
            return [r for r in self.records if r["seq"] > seq]


class StatusCache:
    """SHOWROOM の配信状況を短時間だけ使い回す (画面の自動更新で問い合わせが増えすぎないように)。"""

    def __init__(self, ttl: float = STATUS_TTL, fetcher: Any = fetch):
        self.ttl = ttl
        self._fetch = fetcher
        self._cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(8)

    def get_many(self, keys: list[str]) -> dict[str, dict[str, Any]]:
        now = time.time()
        with self._lock:
            fresh = {k: v for k, (at, v) in self._cache.items() if k in keys and now - at < self.ttl}
        missing = [k for k in keys if k not in fresh]

        def one(key: str) -> tuple[str, dict[str, Any]]:
            try:
                return key, self._fetch(key)
            except Exception as e:  # noqa: BLE001
                return key, {"key": key, "error": str(e).splitlines()[0]}

        for key, value in self._pool.map(one, missing):
            fresh[key] = value
            if "error" not in value:
                with self._lock:
                    self._cache[key] = (now, value)
        return fresh


def watcher_view(w: Any) -> dict[str, Any]:
    status = w.status
    alive = w._page_alive()
    if w.blocked:
        state = "blocked"
    elif status is None:
        state = "starting"
    elif status.is_live and alive:
        state = "watching"
    elif status.is_live:
        state = "opening"
    else:
        state = "waiting"
    return {
        "room": w.key, "name": status.room_name if status else w.key, "room_id": status.room_id if status else None,
        "is_live": bool(status and status.is_live), "page_open": alive, "state": state,
        "watched_minutes": round(w.watched_seconds / 60, 1), "reloads": w.reload_count,
        "stall": w.stall_count, "can_comment": w.can_comment,
    }


def build_overview(keeper: Any, cache: StatusCache) -> dict[str, Any]:
    cfg = keeper.cfg
    state_dir = Path(cfg.state_file).parent
    stats = read_live_stats(state_dir)
    watchers = {w.key: watcher_view(w) for w in list(keeper.watchers)}
    infos = cache.get_many(list(cfg.rooms))
    rooms = []
    for key in cfg.rooms:
        info = dict(infos.get(key) or {"key": key})
        info["live_stat"] = stats.get(key) or {}
        info["watcher"] = watchers.get(key)
        rooms.append(info)
    recorder = keeper.recorder
    return {
        "now": time.time(),
        "started_at": keeper.started_at,
        "browser": keeper.browser_state,
        "login": stats.get(LOGIN_KEY) or {},
        "rooms": rooms,
        "recording": recorder.active() if recorder is not None else [],
        "settings": {key: list(getattr(cfg, key)) for key in SETTINGS_KEYS},
        "password": bool(cfg.web_password),
    }


def live_rooms(keeper: Any) -> dict[str, str]:
    """配信中と分かっているルーム {room_url_key: 名前} (視聴中・ギフト記録中のもの)。"""
    live = {w.key: w.status.room_name for w in list(keeper.watchers) if w.status and w.status.is_live}
    if keeper.recorder is not None:
        live.update({r["room"]: r["name"] for r in keeper.recorder.active()})
    return live


def _days(value: str | None) -> float | None:
    """一覧の期間 (日数)。空・0 は期間で絞り込まない。"""
    if not value:
        return None
    days = float(value)
    return days if days > 0 else None


def post_comment(keeper: Any, text: str, room: str | None, timeout: float = COMMENT_TIMEOUT) -> dict[str, Any]:
    """comment コマンドと同じキューに依頼を置き、keeper の投稿結果を待つ。"""
    from .comments import enqueue, queue_dir, validate

    try:
        text = validate(text)
    except ValueError as e:
        return {"ok": False, "message": str(e)}
    qdir = queue_dir(keeper.cfg)
    req_id = enqueue(qdir, text, normalize_room_key(room) if room else None)
    result = qdir / f"{req_id}.result.json"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if result.exists():
            try:
                data = json.loads(result.read_text(encoding="utf-8"))
            except ValueError:  # 書き込み途中
                time.sleep(0.2)
                continue
            result.unlink(missing_ok=True)
            return {"ok": bool(data.get("ok")), "message": data.get("message", "")}
        time.sleep(0.3)
    (qdir / f"{req_id}.req.json").unlink(missing_ok=True)
    return {"ok": False, "message": "応答がありません。視聴 (ブラウザ) が動いているか確認してください"}


def host_is_loopback(host_header: str | None) -> bool:
    if not host_header:
        return False
    host = host_header.strip()
    if host.startswith("["):  # [::1]:8080
        host = host[1:].split("]", 1)[0]
    elif host.count(":") == 1:
        host = host.split(":", 1)[0]
    return host.lower() in LOOPBACK_HOSTS


class AdminHandler(BaseHTTPRequestHandler):
    server_version = "showroom-keeper"
    keeper: Any = None
    cache: StatusCache
    logs: LogBuffer

    # ------------------------------------------------------------ 共通
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - 標準の名前
        log.debug("%s %s", self.address_string(), format % args)

    def _send(self, code: int, body: bytes, ctype: str, extra: dict[str, str] | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, data: Any, code: int = 200) -> None:
        self._send(code, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _error(self, code: int, message: str) -> None:
        self._json({"ok": False, "message": message}, code)

    def _allowed(self) -> bool:
        password = self.keeper.cfg.web_password
        if password:
            auth = self.headers.get("Authorization", "")
            ok = False
            if auth.startswith("Basic "):
                try:
                    _, _, given = base64.b64decode(auth[6:]).decode("utf-8").partition(":")
                    ok = hmac.compare_digest(given.encode("utf-8"), password.encode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    ok = False
            if not ok:
                self._send(401, "パスワードが必要です".encode("utf-8"), "text/plain; charset=utf-8",
                           {"WWW-Authenticate": 'Basic realm="showroom-keeper", charset="UTF-8"'})
            return ok
        # パスワードが無い時は localhost 宛ての接続だけ受け付ける (DNS リバインディング対策を兼ねる)
        if not host_is_loopback(self.headers.get("Host")):
            self._error(403, "この管理画面は localhost からだけ開けます (他の端末から使うにはパスワードを設定してください)")
            return False
        return True

    def _state_dir(self) -> Path:
        return Path(self.keeper.cfg.state_file).parent

    def _settings(self) -> dict[str, list[str]]:
        cfg = self.keeper.cfg
        return {key: list(getattr(cfg, key, []) or [])
                for key in ("rooms", "record_rooms", "events", "enter_rooms", "support_rooms", "research_rooms")}

    def _body(self) -> dict[str, Any] | None:
        if self.headers.get(CSRF_HEADER) != "1" or "application/json" not in (self.headers.get("Content-Type") or ""):
            self._error(403, "不正な要求です")
            return None
        try:
            length = int(self.headers.get("Content-Length") or 0)
            data = json.loads(self.rfile.read(min(length, 65536)) or b"{}")
        except (ValueError, OSError):
            self._error(400, "要求の形式が不正です")
            return None
        if not isinstance(data, dict):
            self._error(400, "要求の形式が不正です")
            return None
        return data

    # ------------------------------------------------------------ GET
    def do_GET(self) -> None:  # noqa: N802 - 標準の名前
        if not self._allowed():
            return
        url = urlparse(self.path)
        q = {k: v[-1] for k, v in parse_qs(url.query).items()}
        try:
            if url.path in ("/", "/index.html"):
                self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8",
                           {"Content-Security-Policy": "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                            "script-src 'self' 'unsafe-inline'; img-src 'self' data: https://static.showroom-live.com; connect-src 'self'"})
            elif url.path == "/api/overview":
                self._json(build_overview(self.keeper, self.cache))
            elif url.path == "/api/logs":
                self._json({"logs": self.logs.since(int(q.get("after") or 0))})
            elif url.path == "/api/gifts/room":
                from .gifts import room_report

                state_dir = Path(self.keeper.cfg.state_file).parent
                hours = float(q["hours"]) if q.get("hours") else None
                self._json(room_report(state_dir, normalize_room_key(q.get("room", "")), hours, q.get("user") or None))
            elif url.path == "/api/gifts/event":
                from .config import normalize_event_key
                from .gifts import event_report

                state_dir = Path(self.keeper.cfg.state_file).parent
                self._json(event_report(state_dir, normalize_event_key(q.get("event", "")), q.get("user") or None,
                                        official=q.get("official", "1") != "0"))
            elif url.path == "/api/lists/rooms":
                from .listing import list_rooms

                self._json({"rooms": list_rooms(self._state_dir(), self._settings(), live_rooms(self.keeper), lookup=True,
                                                q=q.get("q", "")[:100], showroom=q.get("showroom") == "1")})
            elif url.path == "/api/lists/events":
                from .listing import list_events

                self._json({"events": list_events(self._state_dir(), list(self.keeper.cfg.events))})
            elif url.path == "/api/listeners":
                from .listing import list_listeners

                room = normalize_room_key(q["room"]) if q.get("room") else None
                self._json(list_listeners(self._state_dir(), _days(q.get("days")), room))
            elif url.path == "/api/research":
                from . import research

                cfg = self.keeper.cfg
                max_age = 0 if q.get("refresh") == "1" else research.ANALYSIS_TTL
                self._json({**research.analyze(self._state_dir(), self._support(q), max_age=max_age),
                            **self._research_progress(),
                            "support_rooms": list(cfg.support_rooms), "research_rooms": list(cfg.research_rooms)})
            elif url.path == "/api/research/status":
                self._json(self._research_progress())
            elif url.path == "/api/research/gifter":
                from . import research

                self._json(research.gifter_detail(self._state_dir(), q.get("user"), self._support(q)))
            elif url.path == "/api/lookup/user":
                from .listing import lookup_user

                if not str(q.get("id", "")).strip().isdigit():
                    raise ValueError("ユーザー ID は数字で指定してください")
                self._json({"user": lookup_user(q["id"])})
            elif url.path == "/api/listener":
                from .listing import listener_detail

                self._json(listener_detail(self._state_dir(), q.get("user"), _days(q.get("days"))))
            else:
                self._error(404, "見つかりません")
        except (ValueError, LookupError) as e:
            self._error(400, str(e))
        except (BrokenPipeError, ConnectionResetError):
            return  # 画面を閉じるなどで、返事を送る前に接続が切れた
        except Exception as e:  # noqa: BLE001
            log.warning("管理画面の処理に失敗: %s %s", url.path, e)
            self._error(502, f"取得できませんでした ({str(e).splitlines()[0] if str(e) else type(e).__name__})")

    do_HEAD = do_GET

    def _research_progress(self) -> dict[str, Any]:
        """情報収集の進み具合 (分析は数え直さないので軽い。集めている間の画面の更新に使う)。"""
        from . import research, sweep

        cfg = self.keeper.cfg
        out = {"job": research.JOB.state(), "status": research.status(self._state_dir(), research.research_keys(cfg)),
               "throttle": research.THROTTLE.state(), "sweep": sweep.state()}
        if cfg.research_all:
            now = time.time()
            if now - _store_status["at"] > STORE_STATUS_TTL:  # 全体の件数は数えるのに時間がかかるので使い回す
                _store_status.update(at=now, data=research.store_status(self._state_dir()))
            out["store"] = _store_status["data"]
        return out

    # ------------------------------------------------------------ POST
    def _start_research(self, only_new: bool, force: bool = False) -> bool:
        """応援ルーム・調べるルームの情報を集め始める。only_new ならまだ集めていないルームだけ。"""
        from . import research

        keys = research.research_keys(self.keeper.cfg)
        if only_new:
            keys = [r["key"] for r in research.status(self._state_dir(), keys) if not r["at"]]
        return bool(keys) and research.JOB.start(self._state_dir(), keys, "管理画面から", force=force)

    def _support(self, q: dict[str, str]) -> list[str]:
        if q.get("support"):
            return [normalize_room_key(k) for k in q["support"].split(",") if k.strip()]
        return list(self.keeper.cfg.support_rooms)

    def do_POST(self) -> None:  # noqa: N802 - 標準の名前
        if not self._allowed():
            return
        data = self._body()
        if data is None:
            return
        path = urlparse(self.path).path
        try:
            if path == "/api/comment":
                self._json(post_comment(self.keeper, str(data.get("text", "")), data.get("room") or None))
            elif path == "/api/settings":
                cfg = self.keeper.cfg
                before = {k: list(getattr(cfg, k, []) or []) for k in WATCH_KEYS}
                values = save_settings(cfg, data)
                log.info("管理画面からルーム設定が変更されました")
                # 応援ルーム・調べるルームは視聴に関係しないので、すぐ反映して新しいルームを集め始める
                cfg.support_rooms, cfg.research_rooms = values["support_rooms"], values["research_rooms"]
                if any(values.get(k, before[k]) != before[k] for k in WATCH_KEYS):
                    self.keeper.request_reload()
                self._start_research(only_new=True)
                self._json({"ok": True, "settings": values})
            elif path == "/api/research/collect":
                started = self._start_research(only_new=False, force=bool(data.get("force")))
                self._json({"ok": True, "started": started})
            else:
                self._error(404, "見つかりません")
        except ValueError as e:
            self._error(400, str(e))
        except (BrokenPipeError, ConnectionResetError):
            return  # 画面を閉じるなどで、返事を送る前に接続が切れた
        except Exception as e:  # noqa: BLE001
            log.warning("管理画面の処理に失敗: %s %s", path, e)
            self._error(500, "処理できませんでした")


class AdminServer:
    def __init__(self, httpd: ThreadingHTTPServer, thread: threading.Thread, logs: LogBuffer):
        self.httpd, self.thread, self.logs = httpd, thread, logs

    @property
    def port(self) -> int:
        return self.httpd.server_address[1]

    def shutdown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        logging.getLogger().removeHandler(self.logs)


def start_admin(keeper: Any, cache: StatusCache | None = None) -> AdminServer | None:
    """管理画面を別スレッドで起動する。ポートが使えなければ警告だけ出して視聴は続ける。"""
    cfg = keeper.cfg
    logs = LogBuffer()
    logging.getLogger().addHandler(logs)
    handler = type("Handler", (AdminHandler,), {"keeper": keeper, "cache": cache or StatusCache(), "logs": logs})
    try:
        httpd = ThreadingHTTPServer((cfg.web_host, cfg.web_port), handler)
    except OSError as e:
        logging.getLogger().removeHandler(logs)
        log.warning("管理画面を起動できませんでした (%s:%s): %s", cfg.web_host, cfg.web_port, e)
        return None
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, name="admin", daemon=True)
    thread.start()
    server = AdminServer(httpd, thread, logs)
    shown = "localhost" if cfg.web_host in ("127.0.0.1", "0.0.0.0", "::", "") else cfg.web_host
    log.info("管理画面: http://%s:%d/", shown, server.port)
    if cfg.web_host not in ("127.0.0.1", "localhost", "::1") and not cfg.web_password:
        log.warning("管理画面のパスワードが未設定のため、localhost 宛て以外の接続は拒否します (SHOWROOM_WEB_PASSWORD で設定)")
    return server
