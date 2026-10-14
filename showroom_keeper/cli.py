"""コマンドラインエントリポイント。"""

from __future__ import annotations

import argparse
import asyncio
import logging
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

    sub.add_parser("login", help="ブラウザを開いて手動ログインし、セッションを保存する")

    diag = sub.add_parser("diagnose", help="ページの読み込み速度を計測し、遅い通信を表示する")
    diag.add_argument("url", nargs="?", default="https://www.showroom-live.com/", help="計測する URL")
    diag.add_argument("--headed", action="store_true", help="ブラウザ画面を表示する")
    return p


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

    if not cfg.rooms:
        logging.error("監視するルームを指定してください (引数 / config.yaml の rooms / 環境変数 SHOWROOM_ROOMS)")
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
