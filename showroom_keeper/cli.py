"""コマンドラインエントリポイント。"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import signal
from contextlib import suppress

from . import __version__
from .config import Config, load_dotenv


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="showroom-keeper", description="SHOWROOM の視聴状態を維持し続けます")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("-c", "--config", default="config.yaml", help="設定ファイル (既定: config.yaml)")
    sub = p.add_subparsers(dest="command")

    run = sub.add_parser("run", help="視聴状態の維持を開始する (既定)")
    run.add_argument("rooms", nargs="*", help="ルームの URL または room_url_key (設定ファイルより優先)")
    run.add_argument("--headed", action="store_true", help="ブラウザ画面を表示する")
    run.add_argument("--no-web", action="store_true", help="管理画面 (既定 http://localhost:8080/) を起動しない")

    sub.add_parser("login", help="ブラウザを開いて手動ログインし、セッションを保存する")

    com = sub.add_parser("comment", help="常駐中の keeper から配信にコメントを 1 件投稿する")
    com.add_argument("text", nargs="*", help="コメント本文 (省略すると 1 行ずつ入力して送る会話モード)")
    com.add_argument("--room", help="投稿先のルーム (複数ルームを監視している場合に指定)")

    st = sub.add_parser("status", help="監視中ルームの配信状況を表示する")
    st.add_argument("rooms", nargs="*", help="ルーム (省略時は設定の rooms)")
    st.add_argument("-w", "--watch", type=int, metavar="秒", help="指定秒ごとに更新し続ける (Ctrl+C で終了)")

    gf = sub.add_parser("gifts", help="ルームのギフト履歴 (リスナー別ランキングと最近のギフト) を表示する")
    gf.add_argument("rooms", nargs="*", help="ルーム (省略時は設定の rooms)")
    gf.add_argument("--hours", type=float, help="直近の時間で絞り込む (省略時は今回の配信)")
    gf.add_argument("--top", type=int, default=20, help="ランキングの表示人数 (既定 20)")
    gf.add_argument("--log", type=int, default=10, metavar="件数", help="最近のギフトの表示件数 (既定 10、0 で非表示)")
    gf.add_argument("--user", metavar="名前かID", help="特定のリスナーの履歴だけを表示 (名前は部分一致、数字はユーザー ID)")
    gf.add_argument("--all", action="store_true", help="期間で絞り込まず、記録のすべてを対象にする")
    gf.add_argument("--event", metavar="イベントURL", help="イベント参加ルーム全体を集計する (イベント期間のみ)")
    gf.add_argument("--no-official", action="store_true",
                    help="--event で SHOWROOM 公式の貢献ランキングを取得しない (記録だけを速く表示)")

    diag = sub.add_parser("diagnose", help="ページの読み込み速度を計測し、遅い通信を表示する")
    diag.add_argument("url", nargs="?", default="https://www.showroom-live.com/", help="計測する URL")
    diag.add_argument("--headed", action="store_true", help="ブラウザ画面を表示する")
    return p


def enable_utf8_line_editing() -> None:
    """端末の行編集を UTF-8 対応にする (Backspace で日本語 1 文字を丸ごと消せるように)。

    Docker の端末などでは既定で無効なため、日本語を消すと 1 バイトだけ消えて文字が壊れる。
    """
    try:
        import termios

        fd = sys.stdin.fileno()
        if os.isatty(fd) and hasattr(termios, "IUTF8"):
            attrs = termios.tcgetattr(fd)
            attrs[0] |= termios.IUTF8
            termios.tcsetattr(fd, termios.TCSANOW, attrs)
    except Exception:  # noqa: BLE001 - 対応していない環境ではそのまま
        pass


def send_comment(cfg: Config, text: str, room: str | None) -> int:
    from .comments import enqueue, queue_dir, validate, wait_result

    try:
        text = validate(text)
    except ValueError as e:
        print(f"❌ {e}")
        return 2
    req_id = enqueue(queue_dir(cfg), text, room)
    print(f"投稿中: {text}", flush=True)
    result = asyncio.run(wait_result(queue_dir(cfg), req_id))
    if result is None:
        print("❌ 応答がありません。視聴 (run) が動いているか確認してください")
        return 1
    print(("✅ " if result["ok"] else "❌ ") + result["message"])
    return 0 if result["ok"] else 1


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    load_dotenv()
    cfg = Config.load(args.config)
    logging.basicConfig(
        level=getattr(logging, str(cfg.log_level).upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    if args.command == "login":
        from .keeper import interactive_login

        asyncio.run(interactive_login(cfg))
        return 0

    if args.command == "comment":
        from .config import normalize_room_key

        room = normalize_room_key(args.room) if args.room else None
        if args.text:
            return send_comment(cfg, " ".join(args.text), room)
        print("コメントを入力して Enter で投稿します。終了するには /q (または Ctrl+D)")
        enable_utf8_line_editing()
        while True:
            print("コメント> ", end="", flush=True)
            try:
                raw = sys.stdin.buffer.readline()
            except KeyboardInterrupt:
                print("\n会話モードを終了しました")
                return 0
            if not raw:  # Ctrl+D
                print("\n会話モードを終了しました")
                return 0
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            if "\ufffd" in line:
                print("❌ 文字が崩れて受け取りました。お手数ですがもう一度入力してください")
                continue
            if line.strip() == "/q":
                print("会話モードを終了しました")
                return 0
            if not line.strip():  # 日本語入力の確定などで空の Enter が来ても終了しない
                continue
            send_comment(cfg, line, room)

    if args.command == "status":
        import time

        from .config import normalize_room_key
        from .status import print_status

        rooms = [normalize_room_key(r) for r in args.rooms] or cfg.rooms
        if not rooms:
            print("ルームを指定してください (引数 / config.yaml の rooms / 環境変数 SHOWROOM_ROOMS)")
            return 2
        from pathlib import Path

        state_dir = Path(cfg.state_file).parent
        if not args.watch:
            return print_status(rooms, state_dir)
        try:
            while True:
                print_status(rooms, state_dir)
                time.sleep(max(10, args.watch))
                print()
        except KeyboardInterrupt:
            return 0

    if args.command == "gifts":
        from pathlib import Path

        from .config import normalize_room_key
        from .gifts import show, show_event

        if args.event:
            from .config import normalize_event_key

            try:
                return show_event(Path(cfg.state_file).parent, normalize_event_key(args.event), args.top, args.user,
                                  official=not args.no_official)
            except Exception as e:  # noqa: BLE001
                print(f"❌ イベントを取得できませんでした ({e})")
                return 1
        rooms = [normalize_room_key(r) for r in args.rooms] or cfg.rooms
        if not rooms:
            print("ルームを指定してください")
            return 2
        code = 0
        for i, key in enumerate(rooms):
            if i:
                print()
            try:
                hours = 1e9 if args.all else args.hours
                code |= show(Path(cfg.state_file).parent, key, hours, args.top, args.log, args.user)
            except Exception as e:  # noqa: BLE001
                print(f"❌ {key}: 取得できませんでした ({e})")
                code = 1
        return code

    if args.command == "diagnose":
        from .keeper import diagnose

        cfg.headless = not args.headed
        asyncio.run(diagnose(cfg, args.url))
        return 0

    if getattr(args, "rooms", None):
        from .config import normalize_room_key

        cfg.rooms = list(dict.fromkeys(normalize_room_key(r) for r in args.rooms))
    if getattr(args, "headed", False):
        cfg.headless = False

    from .keeper import Keeper

    if getattr(args, "no_web", False):
        cfg.web_port = 0
    if not (cfg.rooms or cfg.enter_rooms or cfg.record_rooms or cfg.events or cfg.web_port):
        logging.error("監視するルームを指定してください (引数 / config.yaml の rooms / 環境変数 SHOWROOM_ROOMS。"
                      "ギフト記録だけなら SHOWROOM_RECORD_ROOMS / SHOWROOM_EVENTS)")
        return 2
    keeper = Keeper(cfg)

    async def _main() -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with suppress(NotImplementedError):  # Windows
                loop.add_signal_handler(sig, keeper.stop)
        await keeper.run()

    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass
    return 0
