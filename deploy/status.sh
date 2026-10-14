#!/usr/bin/env bash
# サーバー (Docker) で監視中ルームの配信状況を表示する。
#   ~/SHOWROOM-log-in-System/deploy/status.sh            1 回表示
#   ~/SHOWROOM-log-in-System/deploy/status.sh -w 60      60 秒ごとに更新 (Ctrl+C で終了)
set -euo pipefail
cd "$(dirname "$0")/.."
if [ -n "$(sudo docker compose ps -q --status running showroom-keeper 2>/dev/null)" ]; then
  exec sudo docker compose exec -e PYTHONIOENCODING=utf-8 showroom-keeper python -m showroom_keeper status "$@"
fi
# 視聴を止めていても確認できるよう、一時的なコンテナで実行する
exec sudo docker compose run --rm --no-deps showroom-keeper status "$@"
