"""設定ファイル (YAML) と環境変数の読み込み。"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

ROOM_URL_RE = re.compile(r"showroom-live\.com/(?:r/)?([A-Za-z0-9_\-]+)/?(?:\?.*)?$")


def normalize_room_key(value: str) -> str:
    """URL でも room_url_key でも受け付け、room_url_key に正規化する。"""
    value = value.strip()
    m = ROOM_URL_RE.search(value)
    if m:
        return m.group(1)
    if not re.fullmatch(r"[A-Za-z0-9_\-]+", value):
        raise ValueError(f"不正なルーム指定です: {value!r}")
    return value


def load_dotenv(path: str | os.PathLike = ".env", environ: dict[str, str] | None = None) -> None:
    """.env の KEY=VALUE を環境変数に読み込む。既に設定済みの変数と空の値は無視する。"""
    environ = os.environ if environ is None else environ
    p = Path(path)
    if not p.is_file():
        return
    for line in p.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key and value and key not in environ:
            environ[key] = value


@dataclass
class Credentials:
    account_id: str | None = None
    password: str | None = None

    @property
    def available(self) -> bool:
        return bool(self.account_id and self.password)


@dataclass
class Config:
    rooms: list[str] = field(default_factory=list)
    credentials: Credentials = field(default_factory=Credentials)
    state_file: Path = Path("state/storage_state.json")
    headless: bool = True
    browser_executable: str | None = None
    # 使用するブラウザ。"chrome" / "msedge" (インストール済みのものを使用) / "" (Playwright 付属 Chromium)
    # 付属 Chromium は H.264 に非対応で SHOWROOM の映像を再生できないため、Chrome を推奨
    browser_channel: str = "chrome"
    # ブラウザが使うプロキシ (例: http://127.0.0.1:8080)。未指定なら環境変数 HTTPS_PROXY
    proxy: str | None = None
    # 配信中かどうかを確認する間隔 (秒)
    status_poll_interval: float = 60.0
    # 再生が進んでいるかを確認する間隔 (秒)
    health_check_interval: float = 30.0
    # 何回連続で再生停止を検出したらリロードするか
    stall_threshold: int = 3
    # 定期的にページをリロードする間隔 (分)。0 で無効
    periodic_reload_minutes: float = 60.0
    # ログイン状態を確認する間隔 (秒)。API に問い合わせるだけでコメント等は送らない
    login_check_interval: float = 180.0
    # セッション(Cookie)を保存する間隔 (分)
    save_state_minutes: float = 10.0
    # 配信していない時もページを開いたままにするか
    keep_open_when_offline: bool = False
    # 映像を受信せず帯域を節約する (視聴カウントに影響する可能性あり)
    block_media: bool = False
    # リアルタイム視聴者数を取得する (SHOWROOM のサロン形式で使われる在室確認 API。要ログイン)
    # 見張っているルームのギフト (無料の星を含む) を state_dir/gifts/ に記録する
    record_gifts: bool = True
    viewer_count: bool = True
    viewer_count_interval: float = 60.0
    # アバター等の 3D 描画 (WebGL) を止めて CPU 負荷を約 1/10 にする。
    # GPU の無いサーバーでは 3D 描画が CPU を使い切り映像が止まるため。None なら画面非表示時のみ止める
    disable_3d: bool | None = None
    # 広告・アクセス解析の読み込みを遮断する (読み込みが速くなる)
    block_trackers: bool = True
    mute: bool = True
    # 障害時の再試行待ち (秒) の上限
    max_backoff: float = 300.0
    log_level: str = "INFO"

    @classmethod
    def load(cls, path: str | os.PathLike | None, env: dict[str, str] | None = None) -> "Config":
        env = dict(os.environ if env is None else env)
        data: dict[str, Any] = {}
        if path is not None:
            p = Path(path)
            if p.exists():
                data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            elif path != "config.yaml":
                raise FileNotFoundError(f"設定ファイルが見つかりません: {p}")

        cfg = cls()
        creds = data.pop("credentials", {}) or {}
        rooms = data.pop("rooms", []) or []
        for key, value in data.items():
            if not hasattr(cfg, key):
                raise ValueError(f"未知の設定項目です: {key}")
            current = getattr(cfg, key)
            if isinstance(current, Path):
                value = Path(value)
            setattr(cfg, key, value)

        # 環境変数は設定ファイルより優先 (パスワードはファイルに書かない運用を推奨)
        cfg.credentials = Credentials(
            account_id=env.get("SHOWROOM_ACCOUNT_ID") or creds.get("account_id"),
            password=env.get("SHOWROOM_PASSWORD") or creds.get("password"),
        )
        if env.get("SHOWROOM_ROOMS"):
            rooms = [r for r in env["SHOWROOM_ROOMS"].split(",") if r.strip()]
        if env.get("SHOWROOM_HEADLESS"):
            cfg.headless = env["SHOWROOM_HEADLESS"].lower() not in ("0", "false", "no")
        if env.get("SHOWROOM_STATE_FILE"):
            cfg.state_file = Path(env["SHOWROOM_STATE_FILE"])
        if env.get("SHOWROOM_BROWSER_EXECUTABLE"):
            cfg.browser_executable = env["SHOWROOM_BROWSER_EXECUTABLE"]

        if "SHOWROOM_BROWSER_CHANNEL" in env:
            cfg.browser_channel = env["SHOWROOM_BROWSER_CHANNEL"]
        if not cfg.proxy:
            cfg.proxy = env.get("SHOWROOM_PROXY") or env.get("HTTPS_PROXY") or env.get("https_proxy") or None

        cfg.rooms = list(dict.fromkeys(normalize_room_key(str(r)) for r in rooms))
        cfg.validate()
        return cfg

    def validate(self) -> None:
        for name in ("status_poll_interval", "health_check_interval", "max_backoff"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} は正の数である必要があります")
        if self.stall_threshold < 1:
            raise ValueError("stall_threshold は 1 以上である必要があります")
