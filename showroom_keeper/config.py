"""設定ファイル (YAML) と環境変数の読み込み。"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, fields
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


EVENT_URL_RE = re.compile(r"showroom-live\.com/event/([A-Za-z0-9_\-]+)")


def normalize_event_key(value: str) -> str:
    """イベントの URL でも event_url_key でも受け付ける。"""
    value = value.strip()
    m = EVENT_URL_RE.search(value)
    if m:
        return m.group(1)
    if not re.fullmatch(r"[A-Za-z0-9_\-]+", value):
        raise ValueError(f"不正なイベント指定です: {value!r}")
    return value


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
    # 視聴はせずギフトだけ記録するルームと、参加ルームすべてを記録するイベント (件数の上限なし)
    record_rooms: list[str] = field(default_factory=list)
    events: list[str] = field(default_factory=list)
    # 視聴はせず、配信が始まったら一度だけ入室するルーム (視聴するルームと重なる分は除く)
    enter_rooms: list[str] = field(default_factory=list)
    # SHOWROOM の全ルームで、配信が始まったら一度だけ入室する (配信中の一覧を status_poll_interval ごとに 1 回取る。
    # 1 日に数千回入室するので、アカウントが制限されるおそれがある)。入室するルーム (enter_rooms) も含む
    enter_all: bool = False
    # ログインした状態で訪問しないルーム (入室しない・ギフトの記録やログイン確認にもログインを使わない)。
    # 視聴するルーム (rooms) にも入れた時は、視聴を優先する
    no_visit_rooms: list[str] = field(default_factory=list)
    # ギフターを調べるルーム。累計ランキングとイベントの貢献ランキングを集める (research_days / research_times の時刻に、
    # 問い合わせの間隔を空けて)。support_rooms (前の「応援しているルーム」) は、読んだ時に調べるルームへ移す
    # (2026-10-20 に利用者が決めた。分析の基準にするルームは、設定ではなく分析の画面でその場で選ぶ)。
    # 移したあとも support_rooms は残し、画面で見るルームを初めて選ぶ前の、最初の見るルームに使う
    support_rooms: list[str] = field(default_factory=list)
    research_rooms: list[str] = field(default_factory=list)
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
    login_check_interval: float = 7200.0
    # セッション(Cookie)を保存する間隔 (分)
    save_state_minutes: float = 10.0
    # 配信していない時も全ルームのページを開いたままにするか。None (既定) はメモリに余裕がある時だけ開いておく
    # (1 ルームにつき約 0.5GB 使うため、小さいサーバーで全ルームを開くと重くなり視聴やコメントが止まる)
    keep_open_when_offline: bool | None = None
    # 映像を受信せず帯域を節約する (視聴カウントに影響する可能性あり)
    block_media: bool = False
    # 視聴する映像の画質。"low" (256x144・約 0.2Mbps) / "medium" (640x360・約 0.8Mbps) /
    # "original" (配信そのまま・最大 1080p 約 6Mbps) / "auto" (SHOWROOM の再生プレーヤー任せ)。
    # 画質は SHOWROOM の再生プレーヤーで視聴者が選べるもの。既定の medium は再生枠 (640x360) と同じ大きさで、
    # 1080p の配信でも映像の処理が軽い。low は通信量は最も少ないが、引き伸ばして描くため CPU はかえって増える
    video_quality: str = "medium"
    # 配信ページを空白のページの裏 (背面のタブ) に置いて開く。映像の再生・コメントの受信と投稿はそのままで、
    # 画面の描き直しが減るため CPU を約 3 割減らせる (計測: 1 ルーム 21% → 14%)
    background_tabs: bool = True
    # ギフトの一覧 (名前・ポイント) を SHOWROOM から取り直す曜日と時刻 (日本時間)。
    # 曜日は mon〜sun / 月〜日 をカンマ区切り (毎日 なら毎日)、時刻は 0:00,18:30 のようにカンマ区切り
    gift_update_days: str = "mon"
    gift_update_times: str = "0:00,3:00,12:00,15:00,18:00"
    # 応援ルーム・調べるルームの情報 (累計ランキング・イベントの貢献ランキング) を集める曜日と時刻 (日本時間)
    research_days: str = "毎日"
    research_times: str = "4:30"
    # SHOWROOM の全ルームの情報を少しずつ集め続ける (配信中・イベント参加のルームを見つけて、累計ランキングと
    # 終わったイベントの貢献ランキングを取る)。問い合わせは research_gap 秒に 1 回 (混んでいれば自動で広げる)。
    # research.db (SQLite) が数 GB になる (2015 年ごろからの貢献ランキング)。research_scan はルーム番号を 1 から順に 1 回だけ調べる (約 58 万回)
    research_all: bool = False
    research_scan: bool = True
    # 全ルームの収集と一緒に、Internet Archive に残っている SHOWROOM の古いページから、消えたルームと
    # 退会したユーザーの前の名前を取り戻す (問い合わせ先は SHOWROOM ではない。4 秒に 1 回、数日かかる)
    research_archive: bool = True
    research_gap: float = 0.5
    # 全ルームの収集の問い合わせのうち、終わったイベントの貢献ランキングに回す割合 (0〜0.95。取る分が残っている間。
    # 開催中のイベントの推移・配信中の一覧などは、割合に数えず先に行う)。ページが遅くなった時は下げる
    research_ranking_share: float = 0.7
    research_archive_gap: float = 6.0  # Internet Archive への問い合わせの間隔 (秒)
    # research.db のバックアップ (state_dir/backup/ に SQLite のバックアップの仕組みで写す) の時刻 (日本時間、毎日) と残す数
    research_backup_times: str = "4:00"
    research_backup_keep: int = 2
    # バックアップをサーバーの外にも送る Google Cloud Storage のバケット名 (空なら送らない。VM に書き込む権限が要る)
    research_backup_gcs: str = ""
    # リアルタイム視聴者数を取得する (SHOWROOM のサロン形式で使われる在室確認 API。要ログイン)
    # 見張っているルームのギフト (無料の星を含む) を state_dir/gifts/ に記録する
    record_gifts: bool = True
    # デイリーグリッター (配信一覧の「D」などのグリッターのバッジ) を取ったルームのギフトも、ブラウザを開かずに記録する。
    # 一度取ったルームは GLITTER_KEEP 日のあいだ、配信のたびに最初から記録する
    record_glitter: bool = True
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
    # 管理画面 (ブラウザで開く操作画面)。web_port: 0 で無効。既定では自分の PC からだけ開ける
    web_port: int = 8080
    web_host: str = "127.0.0.1"
    # 管理画面のパスワード。設定すると、localhost 以外からの接続にも使える (ユーザー名は何でもよい)
    web_password: str | None = None

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
        # その他の設定項目はすべて SHOWROOM_<項目名を大文字> で指定できる (例: SHOWROOM_LOGIN_CHECK_INTERVAL=7200)。
        # Docker では config.yaml を読まないため、サーバーではこれで設定を変える
        apply_env_overrides(cfg, env)

        if "SHOWROOM_BROWSER_CHANNEL" in env:
            cfg.browser_channel = env["SHOWROOM_BROWSER_CHANNEL"]
        if not cfg.proxy:
            cfg.proxy = env.get("SHOWROOM_PROXY") or env.get("HTTPS_PROXY") or env.get("https_proxy") or None

        if env.get("SHOWROOM_RECORD_ROOMS"):
            cfg.record_rooms = [r for r in env["SHOWROOM_RECORD_ROOMS"].split(",") if r.strip()]
        if env.get("SHOWROOM_EVENTS"):
            cfg.events = [e for e in env["SHOWROOM_EVENTS"].split(",") if e.strip()]
        if env.get("SHOWROOM_ENTER_ROOMS"):
            cfg.enter_rooms = [r for r in env["SHOWROOM_ENTER_ROOMS"].split(",") if r.strip()]
        cfg.enter_rooms = list(dict.fromkeys(normalize_room_key(str(r)) for r in cfg.enter_rooms or []))
        if env.get("SHOWROOM_NO_VISIT_ROOMS"):
            cfg.no_visit_rooms = [r for r in env["SHOWROOM_NO_VISIT_ROOMS"].split(",") if r.strip()]
        cfg.no_visit_rooms = list(dict.fromkeys(normalize_room_key(str(r)) for r in cfg.no_visit_rooms or []))
        for name in ("support_rooms", "research_rooms"):
            if env.get(env_key(name)):
                setattr(cfg, name, [r for r in env[env_key(name)].split(",") if r.strip()])
            setattr(cfg, name, list(dict.fromkeys(normalize_room_key(str(r)) for r in getattr(cfg, name) or [])))
        cfg.record_rooms = list(dict.fromkeys(normalize_room_key(str(r)) for r in cfg.record_rooms or []))
        cfg.events = list(dict.fromkeys(normalize_event_key(str(e)) for e in cfg.events or []))

        cfg.rooms = list(dict.fromkeys(normalize_room_key(str(r)) for r in rooms))
        apply_settings(cfg)
        fold_support(cfg)
        cfg.validate()
        return cfg

    def validate(self) -> None:
        for name in ("status_poll_interval", "health_check_interval", "max_backoff"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} は正の数である必要があります")
        if self.stall_threshold < 1:
            raise ValueError("stall_threshold は 1 以上である必要があります")


ENV_PREFIX = "SHOWROOM_"
# 環境変数を個別に扱う項目 (カンマ区切りの一覧・ログイン情報・空の値に意味がある・HTTPS_PROXY も見る)
ENV_SPECIAL = {"rooms", "record_rooms", "events", "enter_rooms", "no_visit_rooms", "support_rooms", "research_rooms", "credentials",
               "browser_channel", "proxy"}
TRUE_WORDS = {"1", "true", "yes", "on"}
FALSE_WORDS = {"0", "false", "no", "off"}


def env_key(name: str) -> str:
    return ENV_PREFIX + name.upper()


def parse_env_value(type_name: str, raw: str) -> Any:
    """環境変数の文字列を設定項目の型に変換する。"""
    value = raw.strip()
    base = type_name.replace("| None", "").replace("None |", "").strip()
    if base == "bool":
        word = value.lower()
        if "None" in type_name and word == "auto":
            return None
        if word in TRUE_WORDS:
            return True
        if word in FALSE_WORDS:
            return False
        raise ValueError("true / false で指定してください" + (" (auto も可)" if "None" in type_name else ""))
    if base == "int":
        return int(value)
    if base == "float":
        return float(value)
    if base == "Path":
        return Path(value)
    return value


def apply_env_overrides(cfg: "Config", env: dict[str, str]) -> None:
    """SHOWROOM_<項目名を大文字> の環境変数で設定項目を上書きする (空の値は無視)。"""
    for f in fields(cfg):
        if f.name in ENV_SPECIAL:
            continue
        raw = env.get(env_key(f.name))
        if raw is None or not raw.strip():
            continue
        try:
            setattr(cfg, f.name, parse_env_value(str(f.type), raw))
        except ValueError as e:
            raise ValueError(f"環境変数 {env_key(f.name)} の値が不正です: {raw!r} ({e})") from None


SETTINGS_KEYS = ("rooms", "record_rooms", "events", "enter_rooms", "no_visit_rooms", "support_rooms", "research_rooms")


def settings_path(cfg: Config) -> Path:
    """管理画面で変更したルーム設定の保存先。設定ファイル・環境変数より優先する。"""
    return Path(cfg.state_file).parent / "settings.json"


def normalize_settings(data: dict[str, Any]) -> dict[str, list[str]]:
    """管理画面から受け取ったルーム設定を検証・正規化する (URL でも room_url_key でもよい)。"""
    out: dict[str, list[str]] = {}
    for key in SETTINGS_KEYS:
        values = data.get(key) or []
        if isinstance(values, str):
            values = values.split(",")
        if not isinstance(values, list):
            raise ValueError(f"{key} の形式が不正です")
        norm = normalize_event_key if key == "events" else normalize_room_key
        out[key] = list(dict.fromkeys(norm(str(v)) for v in values if str(v).strip()))
    return out


def apply_settings(cfg: Config) -> bool:
    """管理画面で保存したルーム設定があれば反映する。反映したら True。"""
    import json

    try:
        data = json.loads(settings_path(cfg).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict):
        return False
    try:
        values = normalize_settings(data)
    except ValueError:
        return False
    # 保存したファイルに無い項目 (あとから増えた項目など) は、.env / config.yaml の指定のまま使う
    for key in SETTINGS_KEYS:
        if key in data:
            setattr(cfg, key, values[key])
    fold_support(cfg)
    return True


def fold_support(cfg: Config) -> None:
    """前の「応援しているルーム」を、調べるルームに移す (毎日の収集とお知らせが、そのまま続くように)。"""
    cfg.research_rooms = list(dict.fromkeys([*cfg.support_rooms, *cfg.research_rooms]))


def save_settings(cfg: Config, data: dict[str, Any]) -> dict[str, list[str]]:
    import json
    import os

    values = normalize_settings(data)
    path = settings_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(values, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)
    return values
