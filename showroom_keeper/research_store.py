"""調査で集めた情報の保存先 (SQLite、state/research.db)。

全ルームを集めると貢献ランキングだけで数百万行になるので、JSON ファイルを全部読み込む方式はやめて
SQLite に入れる。ギフターごとの集計に使う user_room (ギフター × ルームの送ったポイント) は、
ランキングを保存するたびに更新しておき、分析のときに全部を数え直さないようにする。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator

log = logging.getLogger("research")

DB_NAME = "research.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS rooms (
    room_id INTEGER PRIMARY KEY, key TEXT, name TEXT, followers INTEGER,
    found_via TEXT, found_at REAL, live_at REAL, collected_at REAL, retry_at REAL, history_at REAL);
CREATE INDEX IF NOT EXISTS rooms_key ON rooms(key);
CREATE TABLE IF NOT EXISTS events (
    event_id INTEGER PRIMARY KEY, key TEXT, name TEXT, start REAL, end REAL, type TEXT,
    show_ranking INTEGER DEFAULT 1, rooms_at REAL, retry_at REAL);
CREATE TABLE IF NOT EXISTS watch (user_id INTEGER PRIMARY KEY, memo TEXT, added_at REAL, updated_at REAL);
CREATE TABLE IF NOT EXISTS event_progress (
    event_id INTEGER, room_id INTEGER, at REAL, point INTEGER, rank INTEGER, PRIMARY KEY (event_id, room_id, at));
CREATE INDEX IF NOT EXISTS event_progress_at ON event_progress(event_id, at);
-- 終わる直前の細かい推移 (終わる 3 時間前から 1 分ごと、10 分前から 15 秒ごと、1 分前から 2 秒ごと。上位 30 ルーム)。追い上げの時刻を見るため
CREATE TABLE IF NOT EXISTS event_final (
    event_id INTEGER, at REAL, room_id INTEGER, point INTEGER, rank INTEGER, PRIMARY KEY (event_id, at, room_id)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS room_events (
    room_id INTEGER, event_id INTEGER, ranking_at REAL, final INTEGER DEFAULT 0, failures INTEGER DEFAULT 0,
    point INTEGER, rank INTEGER, PRIMARY KEY (room_id, event_id)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS room_events_event ON room_events(event_id);
CREATE TABLE IF NOT EXISTS cumulative (
    room_id INTEGER, user_id INTEGER, point INTEGER, visits INTEGER, rank INTEGER, PRIMARY KEY (room_id, user_id))
    WITHOUT ROWID;
-- 累計ランキングの、ルームごとにおよそ月 1 回の値 (期間ごとの増え方を出す)
CREATE TABLE IF NOT EXISTS cumulative_history (
    room_id INTEGER, user_id INTEGER, at REAL, point INTEGER, visits INTEGER, PRIMARY KEY (user_id, room_id, at))
    WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS contributions (
    event_id INTEGER, room_id INTEGER, user_id INTEGER, point INTEGER, rank INTEGER,
    PRIMARY KEY (event_id, room_id, user_id)) WITHOUT ROWID;
-- 人で引く索引。point・rank も入れて、表を引き直さずに読む (分析の人ごとのまとめ _event_stats が、サーバーで 34 秒かかっていた。2026-10-19)
CREATE INDEX IF NOT EXISTS contributions_user_pt ON contributions(user_id, point, rank);
CREATE TABLE IF NOT EXISTS user_room (
    user_id INTEGER, room_id INTEGER, cumulative INTEGER DEFAULT 0, cumulative_rank INTEGER,
    visits INTEGER DEFAULT 0, events INTEGER DEFAULT 0, PRIMARY KEY (user_id, room_id)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS user_room_room ON user_room(room_id);
-- ギフターの月ごとのイベントの貢献ポイント (イベントが終わった月、日本時間)。貢献ランキングを保存するたびに足す
-- (支出の増え減りを、全部の貢献ランキングを数え直さずに出すため)
CREATE TABLE IF NOT EXISTS user_month (
    user_id INTEGER, month TEXT, points INTEGER, PRIMARY KEY (user_id, month)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS user_month_month ON user_month(month);
-- 月ごとに多い順に並べる (大口・中堅・少額の区切りを出す)
CREATE INDEX IF NOT EXISTS user_month_points ON user_month(month, points);
CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, name TEXT);
-- 前に使っていた名前 (名前を変えた時・退会した時に前の名前を残す。Internet Archive の古いページからも足す)
CREATE TABLE IF NOT EXISTS user_names (
    user_id INTEGER, name TEXT, at REAL, source TEXT, PRIMARY KEY (user_id, name)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS user_names_name ON user_names(name);
CREATE INDEX IF NOT EXISTS events_key ON events(key);
CREATE INDEX IF NOT EXISTS events_end ON events(end);
CREATE INDEX IF NOT EXISTS events_start ON events(start);
CREATE INDEX IF NOT EXISTS users_name ON users(name);
-- 上の検索欄の索引 (search_index.py) に、まだ入れていない名前の印。名前が入った・変わった時に、下の仕掛け (トリガー) が付ける。
-- 仕掛けはふつうの SQL だけなので、どの接続から書いても動く (名前をそろえる計算は Python なので、索引に入れるのは search_index が後で)。
-- INSERT OR IGNORE は、外の文の ON CONFLICT に負けて失敗するので、NOT EXISTS で確かめる
CREATE TABLE IF NOT EXISTS search_dirty (kind TEXT, ref INTEGER, name TEXT DEFAULT '', PRIMARY KEY (kind, ref, name)) WITHOUT ROWID;
CREATE TRIGGER IF NOT EXISTS users_search_ins AFTER INSERT ON users
    BEGIN INSERT INTO search_dirty(kind, ref) SELECT 'u', new.user_id
        WHERE NOT EXISTS (SELECT 1 FROM search_dirty WHERE kind='u' AND ref=new.user_id AND name=''); END;
CREATE TRIGGER IF NOT EXISTS users_search_upd AFTER UPDATE OF name ON users WHEN old.name IS NOT new.name
    BEGIN INSERT INTO search_dirty(kind, ref) SELECT 'u', new.user_id
        WHERE NOT EXISTS (SELECT 1 FROM search_dirty WHERE kind='u' AND ref=new.user_id AND name=''); END;
CREATE TRIGGER IF NOT EXISTS user_names_search_ins AFTER INSERT ON user_names
    BEGIN INSERT INTO search_dirty(kind, ref, name) SELECT 'f', new.user_id, new.name
        WHERE NOT EXISTS (SELECT 1 FROM search_dirty WHERE kind='f' AND ref=new.user_id AND name=new.name); END;
CREATE TRIGGER IF NOT EXISTS rooms_search_ins AFTER INSERT ON rooms
    BEGIN INSERT INTO search_dirty(kind, ref) SELECT 'r', new.room_id
        WHERE NOT EXISTS (SELECT 1 FROM search_dirty WHERE kind='r' AND ref=new.room_id AND name=''); END;
CREATE TRIGGER IF NOT EXISTS rooms_search_upd AFTER UPDATE OF name, key ON rooms WHEN old.name IS NOT new.name OR old.key IS NOT new.key
    BEGIN INSERT INTO search_dirty(kind, ref) SELECT 'r', new.room_id
        WHERE NOT EXISTS (SELECT 1 FROM search_dirty WHERE kind='r' AND ref=new.room_id AND name=''); END;
CREATE TRIGGER IF NOT EXISTS events_search_ins AFTER INSERT ON events
    BEGIN INSERT INTO search_dirty(kind, ref) SELECT 'e', new.event_id
        WHERE NOT EXISTS (SELECT 1 FROM search_dirty WHERE kind='e' AND ref=new.event_id AND name=''); END;
CREATE TRIGGER IF NOT EXISTS events_search_upd AFTER UPDATE OF name, key ON events WHEN old.name IS NOT new.name OR old.key IS NOT new.key
    BEGIN INSERT INTO search_dirty(kind, ref) SELECT 'e', new.event_id
        WHERE NOT EXISTS (SELECT 1 FROM search_dirty WHERE kind='e' AND ref=new.event_id AND name=''); END;
-- Internet Archive に残っている SHOWROOM のページ (消えたルーム・退会したユーザーの情報を取り戻すため)
CREATE TABLE IF NOT EXISTS archive_pages (
    page TEXT PRIMARY KEY, kind TEXT, event_key TEXT, room_id INTEGER, url TEXT, stamps TEXT,
    tries INTEGER DEFAULT 0, fetched_at REAL, rows INTEGER);
CREATE INDEX IF NOT EXISTS archive_pages_todo ON archive_pages(kind, fetched_at);
-- Internet Archive の貢献ランキング (ユーザー ID は載っていない。突き合わせて分かった人だけ user_id を入れる)
CREATE TABLE IF NOT EXISTS archive_contributions (
    event_id INTEGER, room_id INTEGER, rank INTEGER, name TEXT, point INTEGER, user_id INTEGER, at REAL,
    PRIMARY KEY (event_id, room_id, rank, name)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
-- ルームのアクティブファン (月ごとのファンレベル。見る・コメント・ギフトで上がるので、ポイントに出ない常連も分かる)
CREATE TABLE IF NOT EXISTS active_fans (
    room_id INTEGER, ym TEXT, user_id INTEGER, level INTEGER, title_id INTEGER, at REAL, PRIMARY KEY (room_id, ym, user_id))
    WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS active_fans_user ON active_fans(user_id);
CREATE TABLE IF NOT EXISTS room_fans (
    room_id INTEGER, ym TEXT, fan_power INTEGER, total INTEGER, at REAL, PRIMARY KEY (room_id, ym)) WITHOUT ROWID;
-- ユーザーが持っているルーム (プロフィールでルームを公開している配信者。room_id が NULL ならルームなし・非公開)
CREATE TABLE IF NOT EXISTS user_rooms (user_id INTEGER PRIMARY KEY, room_id INTEGER, checked_at REAL);
CREATE INDEX IF NOT EXISTS user_rooms_room ON user_rooms(room_id);
-- イベントの一覧に入っている、ほかの項目 (説明文・続きもののイベントの親・特典の順位・リーグ・ブロックなど)。
-- data は一覧の 1 件をそのまま (画像の URL を除く) 残したもの
CREATE TABLE IF NOT EXISTS event_info (
    event_id INTEGER PRIMARY KEY, at REAL, parent_id INTEGER, reward_rank INTEGER, block TEXT, leagues TEXT,
    required_level INTEGER, box INTEGER, official INTEGER, data TEXT);
CREATE INDEX IF NOT EXISTS event_info_parent ON event_info(parent_id) WHERE parent_id > 0;
-- イベントのページ (ルール・特典・応募条件) の文章。status は 200 (読めた)・404 (無い)・0 (失敗、あとで読み直す)
CREATE TABLE IF NOT EXISTS event_pages (event_id INTEGER PRIMARY KEY, at REAL, status INTEGER, text TEXT);
-- ルームのプロフィール (SHOWランク・リーグ・グレード・まいにち配信の日数など)。data はプロフィールの 1 件 (画像・長い一覧を除く)
CREATE TABLE IF NOT EXISTS room_info (
    room_id INTEGER PRIMARY KEY, at REAL, show_rank TEXT, prev_show_rank TEXT, league_id INTEGER, prev_league_id INTEGER,
    next_league_id INTEGER, grade TEXT, continuous_days INTEGER, prev_score INTEGER, style INTEGER, group_type TEXT,
    official INTEGER, data TEXT);
CREATE INDEX IF NOT EXISTS room_info_league ON room_info(league_id);
-- 月ごとの SHOWランク・リーグ (その月に読んだ最後の値。ランクの上がり下がりを見るため)
CREATE TABLE IF NOT EXISTS room_rank_month (
    room_id INTEGER, ym TEXT, show_rank TEXT, league_id INTEGER, grade TEXT, at REAL, PRIMARY KEY (room_id, ym)) WITHOUT ROWID;
-- 配信ごとの記録 (配信中のルームの一覧から 5 分ごとに。始まった時刻・最後に見た時刻・盛り上がり度の最大・フォロワーの増減)
CREATE TABLE IF NOT EXISTS room_lives (
    room_id INTEGER, started_at INTEGER, live_id INTEGER, last_seen REAL, view_max INTEGER,
    followers_start INTEGER, followers_end INTEGER, PRIMARY KEY (room_id, started_at)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS room_lives_start ON room_lives(started_at);
-- 記録したギフトの、ルーム × 日 (日本時間) の合計。ギフトの記録のファイル (gifts/<ルーム>.jsonl) を、前に読んだところから先だけ足す
CREATE TABLE IF NOT EXISTS room_gift_daily (
    room_key TEXT, day TEXT, paid_points INTEGER DEFAULT 0, free_points INTEGER DEFAULT 0, gifts INTEGER DEFAULT 0,
    PRIMARY KEY (room_key, day)) WITHOUT ROWID;
-- その日に有料ギフトを送った人 (人数を数えるため)
CREATE TABLE IF NOT EXISTS room_gift_day_users (room_key TEXT, day TEXT, user_id INTEGER, PRIMARY KEY (room_key, day, user_id)) WITHOUT ROWID;
-- ギフトの記録のファイルを、どこまで足したか (バイトの位置と、その時のファイルの大きさ)
CREATE TABLE IF NOT EXISTS room_gift_pos (room_key TEXT PRIMARY KEY, pos INTEGER, size INTEGER, at REAL);
-- 記録したギフトの送り主 (ルームごと)。上の検索欄で、ランキングに出ない少額の人も探すため。
-- 記録のファイルの新しい分だけを、収集の合間に足す (explore.update_gift_senders。ファイル全部を読み直さない)
-- points・free_points (そのうち無料) は、足した時のギフトの一覧で数える。ギフターのページ・一覧・分析は、全員の合計をここから読む
-- unknown = 足した時のギフトの一覧に無かったギフトの回数 (ポイントが分からず 0 で数えた。有料の分が抜けて、無料の割合が高く見える)
CREATE TABLE IF NOT EXISTS gift_senders (
    room_key TEXT, user_id INTEGER, times INTEGER, last_at REAL, name TEXT, points INTEGER DEFAULT 0, free_points INTEGER DEFAULT 0,
    first_at REAL, unknown INTEGER DEFAULT 0, PRIMARY KEY (room_key, user_id)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS gift_senders_user ON gift_senders(user_id);
-- 送り主が使っていた名前と、そろえた形 (explore._fold。探す時にくらべる)
CREATE TABLE IF NOT EXISTS gift_sender_names (
    user_id INTEGER, name TEXT, folded TEXT, at REAL, PRIMARY KEY (user_id, name)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS gift_sender_pos (room_key TEXT PRIMARY KEY, pos INTEGER, size INTEGER, at REAL);
-- ルームごとの、送り主の表に足したギフトの件数と最後の時刻 (ルームの一覧に出す。前は一覧を開くたびに、記録のファイルを全部読んでいた)。
-- explore.update_gift_senders が送り主の表と一緒に足す。前に足した分は catchup が 1 回だけ送り主の表から作る (meta の gift_room_totals)
CREATE TABLE IF NOT EXISTS gift_room_totals (room_key TEXT PRIMARY KEY, records INTEGER, last_at REAL);
-- 記録したギフト 1 件ずつ (人・ルームで引けるように。explore.update_gift_events が、記録のファイルの新しい分だけ足す)。
-- ギフターのページ・分析で、1 人のギフトや、1 つのルームの全員のギフトを、ファイルを読み直さずに引く
-- (前は開くたびにファイルを読み直していた。送る量の多い人のページで 9.2 秒。2026-10-19 に Cursor が測った)
CREATE TABLE IF NOT EXISTS gift_events (user_id INTEGER, at REAL, room_key TEXT, gift_id INTEGER, num INTEGER);
-- 人・ルームと時刻で引く索引 (gift_events_user_at・gift_events_room_at) は、下の Store の初めで作る (前からある research.db では、裏の仕事 catchup が作る)
CREATE TABLE IF NOT EXISTS gift_event_pos (room_key TEXT PRIMARY KEY, pos INTEGER, size INTEGER, at REAL);
-- イベントごとの、貢献ランキングに出たいちばん大きいユーザー ID (名前で推測した分を除く。uid が NULL なら、まだ出た人がいない)。
-- 登録時期の目印に使う。前は分析のたびに貢献ランキングの表を全部読んでいた (サーバーで 27 秒。2026-10-19)
CREATE TABLE IF NOT EXISTS event_max_user (event_id INTEGER PRIMARY KEY, uid INTEGER);
-- 記録しているルームの来場の知らせ (live_events の visit)。ギフターのページの来場の時間帯・関わり方で使う。
-- live_events のファイルの新しい分だけを、収集の合間とページを開く前に足す (explore.update_live_visits。前はファイル全部を読み直していた)
CREATE TABLE IF NOT EXISTS live_visits (
    user_id INTEGER, at INTEGER, room_key TEXT, n INTEGER, PRIMARY KEY (user_id, at, room_key)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS live_visits_room ON live_visits(room_key);
CREATE TABLE IF NOT EXISTS live_visit_pos (room_key TEXT PRIMARY KEY, pos INTEGER, size INTEGER, at REAL);
-- SHOWROOM のお知らせ (ログインが要る)
CREATE TABLE IF NOT EXISTS notices (notice_id INTEGER PRIMARY KEY, label TEXT, title TEXT, created_at REAL, contents TEXT, at REAL);
-- ルームの 1 日ごとの様子 (配信中のルームの一覧から。フォロワー・配信の数・視聴者数)。ROOM_DAILY_KEEP 日で消す
CREATE TABLE IF NOT EXISTS room_daily (
    room_id INTEGER, day TEXT, followers INTEGER, lives INTEGER DEFAULT 0, view_max INTEGER, view_sum INTEGER DEFAULT 0,
    view_n INTEGER DEFAULT 0, last_start REAL, PRIMARY KEY (room_id, day)) WITHOUT ROWID;
-- 終わったイベントの、ルームごとの走り方 (ポイントの推移から。room_analysis が作る)
CREATE TABLE IF NOT EXISTS room_event_style (
    event_id INTEGER, room_id INTEGER, style TEXT, early REAL, mid REAL, late REAL, final REAL, last_day_share REAL,
    climb REAL, lead_share REAL, PRIMARY KEY (event_id, room_id)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS room_event_style_room ON room_event_style(room_id);
-- ルームのタイプ・勢い・戦術 (room_analysis が 1 日 1 回作り直す。ルームの一覧の絞り込みに使う)。
-- momentum は 1 日あたりのポイントの直近 ÷ 前の期間、recent_place・prev_place は戦った相手の中の位置 (勢いには使わない)
CREATE TABLE IF NOT EXISTS room_profile (
    room_id INTEGER PRIMARY KEY, at REAL, events INTEGER, size_class INTEGER, top1 REAL, top3 REAL, contributors REAL,
    capped INTEGER, rel_top10 REAL, rel_per_contrib REAL, rel_point REAL, type TEXT,
    momentum REAL, recent_place REAL, prev_place REAL, style TEXT, style_events INTEGER, style_counts TEXT);
-- ルームのページの重い部品 (リス活・重なるルーム・ギフターごとのルーム数。JSON)。room_parts が暇な時に作り直す。
-- src は作った時の、ルームのイベントの貢献ランキングがいちばん後に入った時刻 (変わったら作り直す)。opened は管理画面で開いた時刻
CREATE TABLE IF NOT EXISTS room_parts (
    room_id INTEGER PRIMARY KEY, version INTEGER, at REAL, src REAL, opened REAL, rounds TEXT, overlap TEXT, rooms TEXT);
-- ルームのヒートマップ: 毎日 3:00 / 15:00 (日本時間) で区切った 12 時間 (slot は heat.slot_of の番号) に、イベントで増えたポイント。
-- events はその区切りに数えたイベントの数。heat.py が区切りの終わったあと 1 回だけ作る
CREATE TABLE IF NOT EXISTS room_point_12h (
    slot INTEGER, room_id INTEGER, point INTEGER, events INTEGER, PRIMARY KEY (slot, room_id)) WITHOUT ROWID;
-- ルームの地力と残弾 (ヒートマップの「地力」「残弾」と、ルームのページの中のギフターのマップ)。heat.py が、ギフターのステータスを
-- 作り終えたあと、サーバーが暇な時に 1 回作り直す。power は主砲・主力・支えの人の「勝負の時にこのルームに出せる量」の合計、
-- left_pt はそのうち今月まだ撃てる分。data は出せる量の多い人から heat.PEOPLE 人 (JSON)
CREATE TABLE IF NOT EXISTS room_power (
    room_id INTEGER PRIMARY KEY, at REAL, power INTEGER, left_pt INTEGER, people INTEGER, guns INTEGER, mains INTEGER, data TEXT);
CREATE INDEX IF NOT EXISTS room_power_power ON room_power(power);
-- イベントの最終の順位のうち、上の方 (上から GifterStatus の ROUND_RANKS 位か、特典の順位の 1 つ下まで) と、隣の順位との差・
-- 勝負になった回か (特典の境目で、差がポイントの 1 割以内か、大会の 2 段階目より後)・目標 (特典の順位) に届いたか。
-- ギフターのステータス (gifter_status.py) が、終わったイベントを 1 回ずつ作る。event_rounds_done は作り終えたイベント
CREATE TABLE IF NOT EXISTS event_rounds (
    event_id INTEGER, room_id INTEGER, end REAL, rank INTEGER, point INTEGER, gap INTEGER, battle INTEGER, reached INTEGER,
    PRIMARY KEY (event_id, room_id)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS event_rounds_room ON event_rounds(room_id, end);
CREATE TABLE IF NOT EXISTS event_rounds_done (event_id INTEGER PRIMARY KEY, end REAL, at REAL);
-- ギフターのステータス (総合力・火力・残弾・忠誠心など。gifter_status.py が 1 日 1 回、暇な時に作る)。data はステータスの全部 (JSON)。
-- 並べ替え・一覧に使う値だけ列にも持つ。gifter_status_queue は、今の回でまだ作っていない人
CREATE TABLE IF NOT EXISTS gifter_status (
    user_id INTEGER PRIMARY KEY, version INTEGER, at REAL, overall REAL, firepower INTEGER, budget INTEGER, left_pt INTEGER,
    state TEXT, top_room INTEGER, top_share REAL, data TEXT);
CREATE INDEX IF NOT EXISTS gifter_status_overall ON gifter_status(overall);
CREATE TABLE IF NOT EXISTS gifter_status_queue (user_id INTEGER PRIMARY KEY);
-- 大会 (段階ごとに別のイベントになっている大きな大会) のまとまり。木の形: 大会 → 道すじ (route。本選権#1・SR枠 など) → 段階。
-- series_id は大会の最初の段階のイベント番号 (大会でないと調べたイベントは NULL)。parents は前の段階のイベント番号 (「,」区切り。
-- 道すじが合流する段階は複数)。brand は名前の【】の印から取った大会の名前。how は auto (自動) か manual (手で直した)。series.py が作る
CREATE TABLE IF NOT EXISTS event_series (
    event_id INTEGER PRIMARY KEY, series_id INTEGER, stage_no INTEGER, stage TEXT, how TEXT, at REAL,
    parents TEXT, route TEXT, brand TEXT);
CREATE INDEX IF NOT EXISTS event_series_series ON event_series(series_id) WHERE series_id IS NOT NULL;
-- 管理画面で手でつないだ・外したイベント (series_id が NULL なら外した)。自動では変えない
CREATE TABLE IF NOT EXISTS event_series_manual (event_id INTEGER PRIMARY KEY, series_id INTEGER, at REAL);
"""

MONTH_SQL = "strftime('%Y-%m', e.end + 9 * 3600, 'unixepoch')"  # イベントが終わった月 (日本時間)
USER_MONTH_STEP = 2000  # user_month を前に集めた分から作るとき、1 回に足すイベントの数
HISTORY_EVERY = 25 * 86400  # 累計ランキングの値を残す間隔 (ルームごと。月ごとの推移に使う)
ROOM_STATS_STEP = 100  # 貢献ランキングの数字 (c_*) を前に集めた分から作るとき、1 回に作るイベントの数
ROOM_DAILY_KEEP = 0  # room_daily を残す日数 (0 = 消さない。フォロワーを日・週・月で見るため、2026-10-18 に利用者が決めた)
DAY_SQL = "strftime('%Y-%m-%d', ? + 9 * 3600, 'unixepoch')"  # 日本時間の日付


CONTRIB_FULL = 100  # 貢献ランキングに載る人数 (この順位の人のポイントを、ファンの層の厚さに使う)


def contribution_stats(points: Iterable[Any]) -> tuple[int, int, int, int, int, int]:
    """貢献ランキングの (人数, 合計, 1 位, 上位 3 人, 上位 10 人, 100 位) のポイント (ルームのタイプ分けに使う)。

    100 位のポイントは、100 人に届かなければ 0 (101 人目以降は誰も送っていないのと同じ扱い)。
    """
    pts = sorted((int(p or 0) for p in points), reverse=True)
    p100 = pts[CONTRIB_FULL - 1] if len(pts) >= CONTRIB_FULL else 0
    return len(pts), sum(pts), sum(pts[:1]), sum(pts[:3]), sum(pts[:10]), p100


def median(values: Iterable[Any]) -> float | None:
    v = sorted(x for x in values if x is not None)
    if not v:
        return None
    m = len(v) // 2
    return float(v[m]) if len(v) % 2 else (v[m - 1] + v[m]) / 2

_ready: set[str] = set()
_ready_lock = threading.Lock()


def db_path(state_dir: Path) -> Path:
    return Path(state_dir) / DB_NAME


JOURNAL_SIZE_LIMIT = 64 * 1024 * 1024  # research.db-wal を縮める大きさ (バイト)
PROGRESS_THIN_GAP = 6 * 3600  # 終わったイベントの推移は、最後の 24 時間を除いてこの間隔に 1 回だけ残す (秒)
PROGRESS_THIN_AFTER = 7 * 86400  # 終わってこれだけたち、走り方を作り終えたイベントの推移を間引く (秒)


SLOW_SQL = 2.0  # これより長くかかった読み込みを、自己点検に知らせる (秒)
slow_hooks: list[Any] = []  # 遅い読み込みを受け取る関数 (sql, 秒)。自己点検が登録する


def _timed(sql: str, started: float) -> None:
    took = time.monotonic() - started
    if took >= SLOW_SQL:
        for hook in slow_hooks:
            hook(sql, took)


# ギフト 1 件ずつの表の索引 (新しい名前, 作る SQL, 前の索引, 前の索引の欄)。どちらも読む欄を全部入れて、表を引き直さずに読む
GIFT_EVENT_INDEXES = [
    ("gift_events_room_at", "CREATE INDEX IF NOT EXISTS gift_events_room_at ON gift_events(room_key, at, gift_id, num, user_id)",
     "gift_events_room", "room_key, at"),
    ("gift_events_user_at", "CREATE INDEX IF NOT EXISTS gift_events_user_at ON gift_events(user_id, at, room_key, gift_id, num)",
     "gift_events_user", "user_id, at"),
]

class Store:
    """research.db への接続 1 本。スレッドごとに作って使う (with 文で閉じる)。"""

    def __init__(self, state_dir: Path):
        self.state_dir = Path(state_dir)
        path = db_path(self.state_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), timeout=60, isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout=60000")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        # 書き込み待ちのファイル (research.db-wal) を、本体に移し終えたらこの大きさまで縮める (接続ごとの設定)。
        # 無いと、長い読み込みの間にたまった分の大きさのまま残る (サーバーで 607MB。2026-10-20 に Cursor が測った)
        self.conn.execute(f"PRAGMA journal_size_limit={JOURNAL_SIZE_LIMIT}")
        with _ready_lock:
            if str(path) not in _ready:
                self.conn.execute("PRAGMA journal_mode=WAL")
                self.conn.executescript(SCHEMA)
                for table, col in (("rooms", "retry_at REAL"), ("rooms", "history_at REAL"), ("events", "retry_at REAL"), ("events", "progress_at REAL"),
                                   ("room_events", "point INTEGER"), ("room_events", "rank INTEGER"),
                                   ("rooms", "gone INTEGER"), ("events", "room_count INTEGER"),
                                   ("events", "median_point INTEGER"), ("events", "style_at REAL"),
                                   ("room_events", "c_count INTEGER"), ("room_events", "c_sum INTEGER"),
                                   ("room_events", "c_top1 INTEGER"), ("room_events", "c_top3 INTEGER"),
                                   ("room_events", "c_top10 INTEGER"), ("contributions", "guessed INTEGER"),
                                   ("room_events", "c_p100 INTEGER"), ("room_profile", "depth_pt REAL"),
                                   ("room_profile", "depth_rel REAL"), ("room_profile", "depth TEXT"),
                                   ("room_profile", "below_pt REAL"), ("room_profile", "below_share REAL"),
                                   ("room_profile", "momentum_label TEXT"), ("room_profile", "momentum_people REAL"),
                                   ("room_profile", "momentum_detail TEXT"), ("events", "thinned_at REAL"),
                                   ("room_event_style", "swaps INTEGER"), ("room_event_style", "early_gap REAL"),
                                   ("room_event_style", "rival_slow REAL"), ("rooms", "fans_at REAL"), ("rooms", "fans_back TEXT"), ("rooms", "profile_at REAL"), ("room_lives", "genre TEXT"),
                                   ("user_rooms", "room_key TEXT"), ("event_series", "parents TEXT"),
                                   ("event_series", "route TEXT"), ("event_series", "brand TEXT"),
                                   ("gift_senders", "points INTEGER DEFAULT 0"), ("gift_senders", "free_points INTEGER DEFAULT 0"),
                                   ("gift_senders", "first_at REAL"), ("gift_senders", "unknown INTEGER DEFAULT 0")):
                    cols = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}
                    if col.split()[0] not in cols:  # この欄が無かったころの research.db
                        self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col}")
                        if (table, col.split()[0]) == ("room_events", "c_p100"):
                            # 100 位のポイントを、前に集めた分からも作り直す (build_room_stats が少しずつ)
                            self.conn.execute("DELETE FROM meta WHERE k='room_stats_upto'")
                        if (table, col.split()[0]) in (("gift_senders", "points"), ("gift_senders", "unknown")):
                            # ポイント・一覧に無いギフトの列が無かった表: 記録のファイルの最初から足し直す
                            self.conn.execute("DELETE FROM gift_senders")
                            self.conn.execute("DELETE FROM gift_sender_pos")
                        if (table, col.split()[0]) == ("event_series", "route"):
                            self.conn.execute("DELETE FROM event_series WHERE how='auto'")  # 木の形でまとめ直す
                self.conn.execute("DROP INDEX IF EXISTS contributions_user")  # 上の contributions_user_pt に替えた (人で引くのは同じ)
                # 名前で推測した分だけの索引 (ルームがあると分かった時に、表を全部読まずに探すため)。guessed の欄を足したあとに作る
                self.conn.execute("""CREATE INDEX IF NOT EXISTS contributions_guessed ON contributions(room_id, event_id)
                                     WHERE guessed IS NOT NULL""")
                # まだ貢献ランキングを取っていないルーム・イベントだけの索引 (次に取るものを選ぶ時に、取り終えた分を読まない)。
                # ポイントが 0 のルーム (取らない) を入れず、選ぶのに読む欄を全部入れて、表を引き直さずに索引だけで選ぶ。
                # 前の room_events_pending (event_id だけ) は、ポイント 0 の取らない分が溜まり、その 1 行ずつ表を探しに行っていた
                # (サーバーで 1 回 平均 5.5 秒・最大 15 秒。2026-10-20 に Cursor が測った)
                self.conn.execute("DROP INDEX IF EXISTS room_events_pending")
                self.conn.execute("""CREATE INDEX IF NOT EXISTS room_events_todo ON room_events(event_id, failures, ranking_at, point, final)
                                     WHERE final=0 AND (point IS NULL OR point > 0)""")
                # 前に作った research.db の user_room は、WITHOUT ROWID になる前の形 (表の本体と、主キーの索引が別) のことがある。
                # その形だと、分析の土台が人の順に全部読む時に、索引から本体を 1 行ずつ飛び飛びに探しに行く (サーバーで 204 万行・70 秒。
                # 2026-10-20 に Cursor が測った)。索引だけで読めるように、読む欄を全部入れた索引を作る (今の形の表には要らない)
                ur = self.conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='user_room'").fetchone()
                if ur and "WITHOUT ROWID" not in (ur[0] or "").upper():
                    self.conn.execute("CREATE INDEX IF NOT EXISTS user_room_scan ON user_room(user_id, room_id, cumulative, events)")
                # 走り方をまだ作っていない・推移をまだ間引いていないイベントだけの索引 (room_analysis.step が収集の仕事のたびに探す。
                # 前は終わった全部のイベント 3 万件を順に見ていて、ほとんど 0 件なのに 1 回 0.1〜0.15 秒かかっていた。2026-10-19 に Cursor が測った)
                self.conn.execute("""CREATE INDEX IF NOT EXISTS events_style_todo ON events(end)
                                     WHERE progress_at IS NOT NULL AND end IS NOT NULL AND style_at IS NULL""")
                self.conn.execute("""CREATE INDEX IF NOT EXISTS events_thin_todo ON events(end)
                                     WHERE style_at IS NOT NULL AND thinned_at IS NULL""")
                # ルームと時刻で引く索引に、読む欄 (gift_id・num・user_id) も入れて、表を引き直さずに読む
                # (大口のギフト・期間を決めたギフターの一覧が、直近の分を 1 件ずつ表に探しに行き、サーバーで 13 秒・55 秒かかっていた。
                # 2026-10-21 に Cursor が測った)。人で引く索引も同じ (ギフターのページ)。表が空の時はここで作る。
                # 前からある表は、起動を止めないように catchup が作る (GIFT_EVENT_INDEXES)
                have = {r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='gift_events'")}
                empty = self.conn.execute("SELECT 1 FROM gift_events LIMIT 1").fetchone() is None
                for new, sql, old, old_cols in GIFT_EVENT_INDEXES:
                    if new in have:
                        continue
                    if empty:
                        self.conn.execute(sql)
                        self.conn.execute(f"DROP INDEX IF EXISTS {old}")
                    elif old not in have:  # 作り替える前に、前の索引も無い (ふつうは無い): 引けるように前の形を作っておく
                        self.conn.execute(f"CREATE INDEX IF NOT EXISTS {old} ON gift_events({old_cols})")
                _import_json(self)
                _mark_guessed(self)
                _ready.add(str(path))

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        self.conn.execute("COMMIT")

    def one(self, sql: str, args: Iterable[Any] = ()) -> sqlite3.Row | None:
        started = time.monotonic()
        row = self.conn.execute(sql, tuple(args)).fetchone()
        _timed(sql, started)
        return row

    def all(self, sql: str, args: Iterable[Any] = ()) -> list[sqlite3.Row]:
        started = time.monotonic()
        rows = self.conn.execute(sql, tuple(args)).fetchall()
        _timed(sql, started)
        return rows

    def scalar(self, sql: str, args: Iterable[Any] = (), default: Any = 0) -> Any:
        row = self.one(sql, args)
        return default if row is None or row[0] is None else row[0]

    # ---------------------------------------------------------------- meta
    def meta(self, k: str, default: Any = None) -> Any:
        row = self.one("SELECT v FROM meta WHERE k=?", (k,))
        return default if row is None else json.loads(row[0])

    def set_meta(self, k: str, v: Any) -> None:
        self.conn.execute("INSERT INTO meta(k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                          (k, json.dumps(v)))

    # ---------------------------------------------------------------- ルーム・イベント
    def upsert_rooms(self, rooms: Iterable[dict[str, Any]], via: str, now: float) -> int:
        """ルームを登録する (分かっている項目だけ上書き)。戻り値は新しく見つかったルームの数。"""
        before = self.scalar("SELECT COUNT(*) FROM rooms")
        with self.tx() as c:
            c.executemany(
                """INSERT INTO rooms(room_id, key, name, followers, found_via, found_at, live_at)
                   VALUES (:room_id, :key, :name, :followers, :via, :now, :live_at)
                   ON CONFLICT(room_id) DO UPDATE SET key=COALESCE(excluded.key, key), name=COALESCE(excluded.name, name),
                     followers=COALESCE(excluded.followers, followers),
                     live_at=CASE WHEN excluded.live_at > COALESCE(live_at, 0) THEN excluded.live_at ELSE live_at END""",
                [{"room_id": int(r["room_id"]), "key": r.get("key"), "name": r.get("name"), "followers": r.get("followers"),
                  "live_at": r.get("live_at"), "via": via, "now": now} for r in rooms if r.get("room_id")])
        return self.scalar("SELECT COUNT(*) FROM rooms") - before

    def room(self, key: str | None = None, room_id: int | None = None) -> sqlite3.Row | None:
        if room_id is not None:
            return self.one("SELECT * FROM rooms WHERE room_id=?", (int(room_id),))
        return self.one("SELECT * FROM rooms WHERE key=? ORDER BY room_id LIMIT 1", (key,))

    def upsert_events(self, events: Iterable[dict[str, Any]]) -> None:
        with self.tx() as c:
            c.executemany(
                """INSERT INTO events(event_id, key, name, start, end, type, show_ranking)
                   VALUES (:event_id, :key, :name, :start, :end, :type, COALESCE(:show_ranking, 1))
                   ON CONFLICT(event_id) DO UPDATE SET key=COALESCE(excluded.key, key), name=COALESCE(excluded.name, name),
                     start=COALESCE(excluded.start, start), end=COALESCE(excluded.end, end),
                     type=COALESCE(excluded.type, type), show_ranking=COALESCE(:show_ranking, show_ranking)""",
                [{"key": None, "name": None, "start": None, "end": None, "type": None, **e,
                  "show_ranking": None if e.get("show_ranking") is None else int(bool(e["show_ranking"]))}
                 for e in events if e.get("event_id")])

    def upsert_event_info(self, raws: Iterable[dict[str, Any]], now: float) -> None:
        """イベントの一覧の 1 件を、そのまま (画像の URL を除く) 残す。前に残した項目は、無い項目で消さない。"""
        rows = []
        for e in raws:
            if not isinstance(e, dict) or not e.get("event_id"):
                continue
            data = {k: v for k, v in e.items() if not str(k).startswith("image")}
            old = self.one("SELECT data FROM event_info WHERE event_id=?", (int(e["event_id"]),))
            if old is not None and old[0]:
                data = {**json.loads(old[0]), **{k: v for k, v in data.items() if v is not None}}
            rows.append((int(e["event_id"]), now, data.get("parent_event_id") or None, data.get("reward_rank"),
                         data.get("event_block_label"), json.dumps(data["league_ids"]) if data.get("league_ids") else None,
                         data.get("required_level"), None if data.get("is_box_event") is None else int(bool(data["is_box_event"])),
                         None if data.get("is_official") is None else int(bool(data["is_official"])),
                         json.dumps(data, ensure_ascii=False)))
        with self.tx() as c:
            c.executemany("""INSERT OR REPLACE INTO event_info(event_id, at, parent_id, reward_rank, block, leagues, required_level,
                             box, official, data) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", rows)

    def set_room_info(self, room_id: int, p: dict[str, Any], now: float, ym: str) -> None:
        """ルームのプロフィールを残す (月ごとのランクも)。"""
        rid = int(room_id)
        keep = {k: v for k, v in p.items() if not isinstance(v, (list, dict)) and "image" not in str(k) and "url" not in str(k)}
        row = (rid, now, p.get("show_rank_subdivided"), p.get("prev_show_rank_subdivided"), p.get("league_id"),
               p.get("prev_league_id"), p.get("next_league_id"), p.get("room_grade_after_aggregation") or p.get("room_grade_name"),
               p.get("live_continuous_days"), p.get("prev_score"), p.get("broadcast_style"), p.get("room_group_config_type"),
               None if p.get("is_official") is None else int(bool(p["is_official"])), json.dumps(keep, ensure_ascii=False))
        with self.tx() as c:
            c.execute("""INSERT OR REPLACE INTO room_info(room_id, at, show_rank, prev_show_rank, league_id, prev_league_id,
                         next_league_id, grade, continuous_days, prev_score, style, group_type, official, data)
                         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", row)
            c.execute("""INSERT OR REPLACE INTO room_rank_month(room_id, ym, show_rank, league_id, grade, at) VALUES (?, ?, ?, ?, ?, ?)""",
                      (rid, ym, row[2], row[4], row[7], now))
            c.execute("UPDATE rooms SET profile_at=?, followers=COALESCE(?, followers) WHERE room_id=?", (now, p.get("follower_num"), rid))

    def set_event_page(self, event_id: int, status: int, text: str | None, now: float) -> None:
        self.conn.execute("""INSERT INTO event_pages(event_id, at, status, text) VALUES (?, ?, ?, ?)
                             ON CONFLICT(event_id) DO UPDATE SET at=excluded.at, status=excluded.status,
                             text=COALESCE(excluded.text, text)""", (int(event_id), now, int(status), text))

    def upsert_notices(self, rows: Iterable[dict[str, Any]], now: float) -> int:
        """お知らせを残す。戻り値は新しく入った数。中身 (contents) が無い行は、前の中身を消さない。"""
        new = 0
        with self.tx() as c:
            for r in rows:
                if not isinstance(r, dict) or not r.get("notice_id"):
                    continue
                new += c.execute("SELECT 1 FROM notices WHERE notice_id=?", (int(r["notice_id"]),)).fetchone() is None
                c.execute("""INSERT INTO notices(notice_id, label, title, created_at, contents, at) VALUES (?, ?, ?, ?, ?, ?)
                             ON CONFLICT(notice_id) DO UPDATE SET label=COALESCE(excluded.label, label),
                             title=COALESCE(excluded.title, title), created_at=COALESCE(excluded.created_at, created_at),
                             contents=COALESCE(excluded.contents, contents), at=excluded.at""",
                          (int(r["notice_id"]), r.get("label"), r.get("title"), r.get("created_at"), r.get("contents"), now))
        return new

    def link(self, pairs: Iterable[tuple[int, int]]) -> None:
        """ルームとイベント (参加したもの) を結び付ける。"""
        with self.tx() as c:
            c.executemany("INSERT OR IGNORE INTO room_events(room_id, event_id) VALUES (?, ?)",
                          [(int(r), int(e)) for r, e in pairs])

    def room_events(self, room_id: int) -> list[sqlite3.Row]:
        return self.all("""SELECT e.*, re.ranking_at, re.final, re.failures FROM room_events re
                           JOIN events e USING(event_id) WHERE re.room_id=? ORDER BY COALESCE(e.end, 0) DESC""",
                        (int(room_id),))

    # ---------------------------------------------------------------- ランキング
    def _names(self, c: sqlite3.Connection, rows: list[dict[str, Any]], now: float | None = None) -> None:
        named = [(r["user_id"], r["name"]) for r in rows if r.get("name")]
        # 名前が変わった (退会して「退会済みユーザー」になった時も) なら、前の名前を残す
        c.executemany("""INSERT OR IGNORE INTO user_names(user_id, name, at, source)
                         SELECT user_id, name, ?, 'showroom' FROM users WHERE user_id=? AND name IS NOT NULL AND name != ?""",
                      [(now, u, n) for u, n in named])
        # 名前が同じ人は書き換えない (貢献ランキング 1 件で 100 人分。書くと、人の番号で並んだ表と索引のページが毎回書き換わる)
        c.executemany("""INSERT INTO users(user_id, name) VALUES (?, ?)
                         ON CONFLICT(user_id) DO UPDATE SET name=excluded.name WHERE users.name IS NOT excluded.name""",
                      named)

    def set_cumulative(self, room_id: int, rows: list[dict[str, Any]], now: float) -> None:
        """ルームの累計ランキング (上位 30 人) を置き換える。user_room の累計は小さくしない (上位から外れても送った分は残る)。"""
        room_id = int(room_id)
        with self.tx() as c:
            last = c.execute("SELECT history_at FROM rooms WHERE room_id=?", (room_id,)).fetchone()
            if rows and (last is None or last[0] is None or now - last[0] >= HISTORY_EVERY):
                c.executemany("INSERT OR IGNORE INTO cumulative_history(room_id, user_id, at, point, visits) VALUES (?, ?, ?, ?, ?)",
                              [(room_id, r["user_id"], now, r["point"], r["visits"]) for r in rows])
                c.execute("UPDATE rooms SET history_at=? WHERE room_id=?", (now, room_id))
            c.execute("DELETE FROM cumulative WHERE room_id=?", (room_id,))
            c.executemany("INSERT OR REPLACE INTO cumulative(room_id, user_id, point, visits, rank) VALUES (?, ?, ?, ?, ?)",
                          [(room_id, r["user_id"], r["point"], r["visits"], r.get("rank")) for r in rows])
            c.executemany(
                """INSERT INTO user_room(user_id, room_id, cumulative, cumulative_rank, visits) VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(user_id, room_id) DO UPDATE SET cumulative=MAX(cumulative, excluded.cumulative),
                     cumulative_rank=excluded.cumulative_rank, visits=MAX(visits, excluded.visits)""",
                [(r["user_id"], room_id, r["point"], r.get("rank"), r["visits"]) for r in rows])
            self._names(c, rows, now)

    def set_contributions(self, event_id: int, room_id: int, rows: list[dict[str, Any]], now: float, final: bool) -> None:
        """イベントの貢献ランキングを置き換える (取り直したときは前の分を user_room から引いてから足す)。"""
        self.set_contributions_many([(event_id, room_id, rows, final)], now)

    def set_contributions_many(self, items: list[tuple[int, int, list[dict[str, Any]], bool]], now: float) -> None:
        """いくつかの貢献ランキング (event_id, room_id, rows, final) を、1 回の書き込み (トランザクション) でまとめて置き換える。

        同じ人・同じ索引のページを何度書いても、書き込み待ちのファイルには 1 回分しか入らないので、1 件ずつより書く量が減る。
        """
        with self.tx() as c:
            for event_id, room_id, rows, final in items:
                self._set_contributions(c, int(event_id), int(room_id), rows, now, final)

    def _set_contributions(self, c: sqlite3.Connection, event_id: int, room_id: int, rows: list[dict[str, Any]],
                           now: float, final: bool) -> None:
        c.execute("""UPDATE user_room SET events = events - (SELECT point FROM contributions o
                       WHERE o.event_id=? AND o.room_id=? AND o.user_id=user_room.user_id)
                     WHERE room_id=? AND user_id IN (SELECT user_id FROM contributions WHERE event_id=? AND room_id=?)""",
                  (event_id, room_id, room_id, event_id, room_id))
        months = self._user_month_ready(c, event_id)
        if months:
            c.execute(f"""INSERT INTO user_month(user_id, month, points)
                           SELECT o.user_id, {MONTH_SQL}, -o.point FROM contributions o JOIN events e USING(event_id)
                           WHERE o.event_id=? AND o.room_id=? AND e.end IS NOT NULL
                           ON CONFLICT(user_id, month) DO UPDATE SET points = points + excluded.points""", (event_id, room_id))
        c.execute("DELETE FROM contributions WHERE event_id=? AND room_id=?", (event_id, room_id))
        c.executemany("""INSERT OR REPLACE INTO contributions(event_id, room_id, user_id, point, rank, guessed)
                         VALUES (?, ?, ?, ?, ?, ?)""",
                      [(event_id, room_id, r["user_id"], r["point"], r.get("rank"), 1 if r.get("guessed") else None) for r in rows])
        c.executemany("""INSERT INTO user_room(user_id, room_id, events) VALUES (?, ?, ?)
                         ON CONFLICT(user_id, room_id) DO UPDATE SET events = events + excluded.events""",
                      [(r["user_id"], room_id, r["point"]) for r in rows])
        if months:
            c.execute(f"""INSERT INTO user_month(user_id, month, points)
                           SELECT o.user_id, {MONTH_SQL}, o.point FROM contributions o JOIN events e USING(event_id)
                           WHERE o.event_id=? AND o.room_id=? AND e.end IS NOT NULL
                           ON CONFLICT(user_id, month) DO UPDATE SET points = points + excluded.points""", (event_id, room_id))
        # 名前で推測した分 (Internet Archive) は、分かった人だけの一部なので、ルームのタイプ分けの数字は作らない
        stats = (None,) * 6 if any(r.get("guessed") for r in rows) else contribution_stats(r["point"] for r in rows)
        c.execute("""INSERT INTO room_events(room_id, event_id, ranking_at, final, c_count, c_sum, c_top1, c_top3, c_top10, c_p100)
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                     ON CONFLICT(room_id, event_id) DO UPDATE SET ranking_at=excluded.ranking_at, final=excluded.final,
                       failures=0, c_count=excluded.c_count, c_sum=excluded.c_sum, c_top1=excluded.c_top1,
                       c_top3=excluded.c_top3, c_top10=excluded.c_top10, c_p100=excluded.c_p100""", (room_id, event_id, now, int(final), *stats))
        self._names(c, rows, now)
        top = max((int(r["user_id"]) for r in rows if not r.get("guessed")), default=None)
        if top is not None:
            c.execute("""INSERT INTO event_max_user(event_id, uid) VALUES (?, ?)
                         ON CONFLICT(event_id) DO UPDATE SET uid=MAX(COALESCE(uid, 0), excluded.uid)""", (event_id, top))

    def build_event_max_user(self, step: int = 500) -> bool:
        """前に集めた貢献ランキングから、event_max_user を少しずつ作る (イベントごとに主キーで引く)。することが無ければ False。"""
        todo = [r[0] for r in self.all("""SELECT e.event_id FROM events e LEFT JOIN event_max_user m USING(event_id)
                                          WHERE m.event_id IS NULL LIMIT ?""", (step,))]
        if not todo:
            return False
        with self.tx() as c:
            for eid in todo:
                top = c.execute("SELECT MAX(user_id) FROM contributions WHERE event_id=? AND guessed IS NULL", (eid,)).fetchone()[0]
                c.execute("INSERT OR IGNORE INTO event_max_user(event_id, uid) VALUES (?, ?)", (eid, top))
        return True

    # ---------------------------------------------------------------- 月ごとの集計 (user_month)
    def _user_month_ready(self, c: sqlite3.Connection, event_id: int) -> bool:
        """このイベントの分が、もう user_month に入っているか (入っていれば、取り直しのたびに足し引きする)。"""
        row = c.execute("SELECT v FROM meta WHERE k='user_month_upto'").fetchone()
        return row is not None and (json.loads(row[0]) is True or event_id <= json.loads(row[0]))

    def build_user_month(self, step: int = USER_MONTH_STEP) -> bool:
        """前に集めた貢献ランキングを、user_month に少しずつ足す (イベント番号の小さい順)。

        戻り値は、まだ残りがあるか。全部足し終えると meta の user_month_upto が true になり、
        そのあとは set_contributions が保存のたびに足す。
        """
        with self.tx() as c:
            row = c.execute("SELECT v FROM meta WHERE k='user_month_upto'").fetchone()
            upto = 0 if row is None else json.loads(row[0])
            if upto is True:
                return False
            ids = [r[0] for r in c.execute("SELECT DISTINCT event_id FROM contributions WHERE event_id > ? ORDER BY event_id LIMIT ?",
                                           (upto, step))]
            if ids:
                c.execute(f"""INSERT INTO user_month(user_id, month, points)
                               SELECT o.user_id, {MONTH_SQL} AS m, SUM(o.point) FROM contributions o JOIN events e USING(event_id)
                               WHERE o.event_id > ? AND o.event_id <= ? AND e.end IS NOT NULL GROUP BY o.user_id, m
                               ON CONFLICT(user_id, month) DO UPDATE SET points = points + excluded.points""", (upto, ids[-1]))
            done = len(ids) < step
            c.execute("INSERT INTO meta(k, v) VALUES ('user_month_upto', ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                      (json.dumps(True if done else ids[-1]),))
            return not done

    def set_event_rooms(self, event_id: int, rows: list[dict[str, Any]], now: float) -> None:
        """イベントの参加ルームと、各ルームのイベントのポイント・順位を入れる。"""
        event_id = int(event_id)
        self.upsert_rooms(rows, "event", now)
        with self.tx() as c:
            if rows:  # 参加ルームの無い番号 (使われていない番号など) は入れない
                c.execute("INSERT OR IGNORE INTO events(event_id) VALUES (?)", (event_id,))
            c.executemany("""INSERT INTO room_events(room_id, event_id, point, rank) VALUES (?, ?, ?, ?)
                             ON CONFLICT(room_id, event_id) DO UPDATE SET point=excluded.point, rank=excluded.rank""",
                          [(int(r["room_id"]), event_id, r.get("point"), r.get("rank")) for r in rows if r.get("room_id")])
            c.execute("UPDATE events SET rooms_at=?, retry_at=NULL WHERE event_id=?", (now, event_id))
            if rows:
                c.execute("UPDATE events SET room_count=?, median_point=? WHERE event_id=?",
                          (len(rows), _median_point(r.get("point") for r in rows), event_id))

    def build_room_stats(self, step: int = ROOM_STATS_STEP) -> bool:
        """前に集めたイベントの参加ルームの数・ポイントの真ん中と、貢献ランキングの数字 (c_*) を、少しずつ作る。

        新しく集めた分は set_event_rooms・set_contributions が作る。戻り値は、まだ残りがあるか。
        """
        upto = self.meta("room_stats_upto", 0)
        if upto is True:
            return False
        ids = [r[0] for r in self.all("SELECT event_id FROM events WHERE event_id > ? ORDER BY event_id LIMIT ?", (upto, step))]
        if ids:
            lo, hi = ids[0], ids[-1]
            points: dict[int, list[Any]] = {}
            for r in self.all("SELECT event_id, point FROM room_events WHERE event_id BETWEEN ? AND ?", (lo, hi)):
                points.setdefault(r[0], []).append(r[1])
            contrib: dict[tuple[int, int], list[int]] = {}
            guessed = set()
            for r in self.all("SELECT event_id, room_id, point, guessed FROM contributions WHERE event_id BETWEEN ? AND ?", (lo, hi)):
                contrib.setdefault((r[0], r[1]), []).append(r[2])
                if r[3]:
                    guessed.add((r[0], r[1]))
            for k in guessed:
                del contrib[k]
            with self.tx() as c:
                c.executemany("UPDATE events SET room_count=?, median_point=? WHERE event_id=?",
                              [(len(p), _median_point(p), e) for e, p in points.items()])
                c.executemany("""UPDATE room_events SET c_count=?, c_sum=?, c_top1=?, c_top3=?, c_top10=?, c_p100=?
                                 WHERE event_id=? AND room_id=?""",
                              [(*contribution_stats(p), e, r) for (e, r), p in contrib.items()])
        done = len(ids) < step
        self.set_meta("room_stats_upto", True if done else ids[-1])
        return not done

    def add_room_lives(self, lives: list[dict[str, Any]], now: float) -> None:
        """配信中のルームの一覧 (5 分ごと) から、配信ごとの記録を足す (同じ配信は、最後に見た時刻・最大・フォロワーを更新)。"""
        rows = [(int(x["room_id"]), int(x["started_at"]), x.get("live_id"), now, x.get("viewers"), x.get("followers"), x.get("genre"))
                for x in lives if x.get("room_id") and x.get("started_at")]
        with self.tx() as c:
            c.executemany("""INSERT INTO room_lives(room_id, started_at, live_id, last_seen, view_max, followers_start, followers_end, genre)
                             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?6, ?7)
                             ON CONFLICT(room_id, started_at) DO UPDATE SET last_seen=MAX(last_seen, excluded.last_seen),
                               genre=COALESCE(genre, excluded.genre),
                               view_max=MAX(COALESCE(view_max, 0), COALESCE(excluded.view_max, 0)),
                               followers_end=COALESCE(excluded.followers_end, followers_end)""", rows)

    def add_room_daily(self, lives: list[dict[str, Any]], now: float) -> None:
        """配信中のルームの一覧 (5 分ごと) から、ルームの 1 日ごとの様子を足す。"""
        rows = [(int(x["room_id"]), now, x.get("followers"), x.get("viewers"), x.get("started_at"))
                for x in lives if x.get("room_id")]
        with self.tx() as c:
            c.executemany(f"""INSERT INTO room_daily(room_id, day, followers, lives, view_max, view_sum, view_n, last_start)
                              VALUES (?1, {DAY_SQL.replace('?', '?2')}, ?3, 1, ?4, COALESCE(?4, 0), ?4 IS NOT NULL, ?5)
                              ON CONFLICT(room_id, day) DO UPDATE SET followers=COALESCE(excluded.followers, followers),
                                lives = lives + (COALESCE(excluded.last_start, 0) != COALESCE(last_start, 0)),
                                last_start=COALESCE(excluded.last_start, last_start),
                                view_max=MAX(COALESCE(view_max, 0), COALESCE(excluded.view_max, 0)),
                                view_sum = view_sum + excluded.view_sum, view_n = view_n + excluded.view_n""", rows)
            day = c.execute(f"SELECT {DAY_SQL}", (now,)).fetchone()[0]
            row = c.execute("SELECT v FROM meta WHERE k='room_daily_cleaned'").fetchone()
            if ROOM_DAILY_KEEP and (row is None or json.loads(row[0]) != day):  # 古い日の分は 1 日 1 回だけ消す
                c.execute(f"DELETE FROM room_daily WHERE day < {DAY_SQL}", (now - ROOM_DAILY_KEEP * 86400,))
                c.execute("INSERT INTO meta(k, v) VALUES ('room_daily_cleaned', ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                          (json.dumps(day),))

    # ---------------------------------------------------------------- 気になるギフター
    def set_watch(self, user_id: int, watched: bool, memo: str | None, now: float) -> None:
        """気になるギフターに入れる (メモつき)・外す。memo が None ならメモは変えない。"""
        uid = int(user_id)
        if not watched:
            self.conn.execute("DELETE FROM watch WHERE user_id=?", (uid,))
            return
        self.conn.execute("""INSERT INTO watch(user_id, memo, added_at, updated_at) VALUES (?, COALESCE(?, ''), ?, ?)
                             ON CONFLICT(user_id) DO UPDATE SET memo=COALESCE(?, memo), updated_at=excluded.updated_at""",
                          (uid, memo, now, now, memo))

    def watch_rows(self) -> list[sqlite3.Row]:
        return self.all("SELECT * FROM watch ORDER BY added_at DESC")

    def add_progress(self, event_id: int, rows: list[dict[str, Any]], now: float) -> int:
        """開催中のイベントの、今のルームごとのポイントと順位を残す (ポイントの推移)。戻り値は残したルームの数。"""
        event_id = int(event_id)
        keep = [(event_id, int(r["room_id"]), now, int(r["point"]), r.get("rank"))
                for r in rows if r.get("room_id") and r.get("point") is not None]
        with self.tx() as c:
            c.executemany("INSERT OR REPLACE INTO event_progress(event_id, room_id, at, point, rank) VALUES (?, ?, ?, ?, ?)", keep)
            c.execute("UPDATE events SET progress_at=? WHERE event_id=?", (now, event_id))
        return len(keep)

    def thin_progress(self, event_id: int, now: float) -> int:
        """終わったイベントの推移を間引く (戻り値は消した行の数)。event_progress は 1 年に数 GB 増えるため。

        残すのは: 最後の 24 時間 (最終日の伸び・終盤の分析に使う)、最初の回、期間の 4 分の 1・半分・4 分の 3 の前後の回
        (走り方を作り直す時に使う)、それ以外は 6 時間に 1 回。
        """
        eid = int(event_id)
        e = self.one("SELECT start, end FROM events WHERE event_id=?", (eid,))
        ats = [r[0] for r in self.all("SELECT DISTINCT at FROM event_progress WHERE event_id=? ORDER BY at", (eid,))]
        if e is None or not e["start"] or not e["end"] or not ats:
            self.conn.execute("UPDATE events SET thinned_at=? WHERE event_id=?", (now, eid))
            return 0
        start, end = e["start"], e["end"]
        keep = {ats[0], ats[-1]}
        targets = [start + f * (end - start) for f in (0.25, 0.5, 0.75)] + [end - 86400]
        for t in targets:
            before = [a for a in ats if a <= t]
            after = [a for a in ats if a > t]
            keep.update(x for x in (before[-1] if before else None, after[0] if after else None) if x is not None)
        buckets: set[int] = set()
        for a in ats:
            if a >= end - 86400 - PROGRESS_THIN_GAP:
                keep.add(a)
            elif int((a - start) // PROGRESS_THIN_GAP) not in buckets:
                buckets.add(int((a - start) // PROGRESS_THIN_GAP))
                keep.add(a)
        drop = [a for a in ats if a not in keep]
        removed = 0
        with self.tx() as c:
            for a in drop:
                removed += c.execute("DELETE FROM event_progress WHERE event_id=? AND at=?", (eid, a)).rowcount
            c.execute("UPDATE events SET thinned_at=? WHERE event_id=?", (now, eid))
        return removed

    def set_active_fans(self, room_id: int, ym: str, fan_power: Any, total: Any, users: list[dict[str, Any]], now: float,
                        current: bool = True) -> None:
        """ルームの、その月のアクティブファン (ファンレベル) を置き換える。current=False は前の月を読み足した分 (読み直す時期は変えない)。"""
        rid = int(room_id)
        with self.tx() as c:
            c.execute("DELETE FROM active_fans WHERE room_id=? AND ym=?", (rid, ym))
            c.executemany("""INSERT OR REPLACE INTO active_fans(room_id, ym, user_id, level, title_id, at) VALUES (?, ?, ?, ?, ?, ?)""",
                          [(rid, ym, int(u["user_id"]), u.get("level"), u.get("title_id"), now) for u in users if u.get("user_id")])
            c.execute("""INSERT INTO room_fans(room_id, ym, fan_power, total, at) VALUES (?, ?, ?, ?, ?)
                         ON CONFLICT(room_id, ym) DO UPDATE SET fan_power=excluded.fan_power, total=excluded.total, at=excluded.at""",
                      (rid, ym, fan_power, total, now))
            if current:
                c.execute("UPDATE rooms SET fans_at=? WHERE room_id=?", (now, rid))
            self._names(c, [{"user_id": int(u["user_id"]), "name": u.get("user_name")} for u in users if u.get("user_id")], now)

    def set_user_room(self, user_id: int, room_id: Any, now: float, key: Any = None) -> None:
        """ユーザーが持っているルーム (無い・非公開なら None) と、そのルームの URL の名前を残す。"""
        self.conn.execute("""INSERT INTO user_rooms(user_id, room_id, checked_at, room_key) VALUES (?, ?, ?, ?)
                             ON CONFLICT(user_id) DO UPDATE SET room_id=excluded.room_id, checked_at=excluded.checked_at,
                             room_key=excluded.room_key""",
                          (int(user_id), None if room_id in (None, "", 0) else int(room_id), now, key or None))

    def add_final(self, event_id: int, rows: list[dict[str, Any]], now: float) -> int:
        """終わる直前の細かい推移を残す。戻り値は残したルームの数。"""
        keep = [(int(event_id), now, int(r["room_id"]), int(r["point"]), r.get("rank"))
                for r in rows if r.get("room_id") and r.get("point") is not None]
        with self.tx() as c:
            c.executemany("INSERT OR REPLACE INTO event_final(event_id, at, room_id, point, rank) VALUES (?, ?, ?, ?, ?)", keep)
        return len(keep)

    def ranking_failed(self, event_id: int, room_id: int) -> None:
        self.conn.execute("""INSERT INTO room_events(room_id, event_id, failures) VALUES (?, ?, 1)
                             ON CONFLICT(room_id, event_id) DO UPDATE SET failures = failures + 1""",
                          (int(room_id), int(event_id)))


def _median_point(points: Iterable[Any]) -> int | None:
    m = median(p for p in points if p and p > 0)
    return None if m is None else int(m)


def _mark_guessed(store: Store) -> None:
    """前の版が Internet Archive から名前だけで決めて足した貢献ランキングに、「名前で推測」の印を 1 回だけ付ける。

    前の版は印を残していないので、貢献ランキングのページを読んだ時刻 (archive_pages.fetched_at) と、
    その直前に保存した時刻 (room_events.ranking_at) が同じ時のものを、名前で決めた分とみなす。
    """
    if store.meta("guessed_marked"):
        return
    with store.tx() as c:
        pairs = c.execute("""SELECT re.event_id, re.room_id FROM archive_pages p JOIN events e ON e.key = p.event_key
                               JOIN room_events re ON re.event_id = e.event_id AND re.room_id = p.room_id
                             WHERE p.kind = 'contribution' AND p.fetched_at BETWEEN re.ranking_at AND re.ranking_at + 120""").fetchall()
        c.executemany("UPDATE contributions SET guessed=1 WHERE event_id=? AND room_id=?", [tuple(x) for x in pairs])
        c.executemany("""UPDATE room_events SET c_count=NULL, c_sum=NULL, c_top1=NULL, c_top3=NULL, c_top10=NULL, c_p100=NULL
                         WHERE event_id=? AND room_id=?""", [tuple(x) for x in pairs])
        c.execute("INSERT INTO meta(k, v) VALUES ('guessed_marked', 'true') ON CONFLICT(k) DO UPDATE SET v=excluded.v")
    if pairs:
        log.info("名前で推測した貢献ランキングに印を付けました (%d ルーム・イベント)", len(pairs))


def _import_json(store: Store) -> None:
    """前の版が state/research/ に JSON で保存した情報を、1 回だけ取り込む。"""
    folder = store.state_dir / "research"
    if store.meta("json_imported") or not folder.is_dir():
        return

    def read(p: Path) -> Any:
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    rooms = events = 0
    for p in sorted((folder / "rooms").glob("*.json")):
        r = read(p)
        if not isinstance(r, dict) or not r.get("room_id"):
            continue
        store.upsert_rooms([{"room_id": r["room_id"], "key": r.get("key"), "name": r.get("name")}], "settings", r.get("at") or 0)
        store.conn.execute("UPDATE rooms SET collected_at=? WHERE room_id=?", (r.get("at"), int(r["room_id"])))
        store.set_cumulative(r["room_id"], [x for x in r.get("cumulative") or [] if x.get("user_id")], r.get("at") or 0)
        store.upsert_events([{k: e.get(k) for k in ("event_id", "key", "name", "start", "end", "type", "show_ranking")}
                             for e in r.get("events") or []])
        store.link((r["room_id"], e["event_id"]) for e in r.get("events") or [] if e.get("event_id"))
        rooms += 1
    for p in sorted((folder / "events").glob("*.json")):
        e = read(p)
        if not isinstance(e, dict) or not e.get("event_id") or not e.get("room_id"):
            continue
        store.set_contributions(e["event_id"], e["room_id"], [x for x in e.get("rows") or [] if x.get("user_id")],
                                e.get("at") or 0, bool(e.get("final")))
        events += 1
    store.set_meta("json_imported", True)
    if rooms or events:
        log.info("前の版で集めた情報を取り込みました (%d ルーム / 貢献ランキング %d 件)", rooms, events)
