#!/usr/bin/env bash
# サーバー (Docker) でルームのギフト履歴を表示する。
#   ~/SHOWROOM-log-in-System/deploy/gifts.sh                   今回の配信のギフト
#   ~/SHOWROOM-log-in-System/deploy/gifts.sh --hours 24 --top 50   直近 24 時間・上位 50 人
#   ~/SHOWROOM-log-in-System/deploy/gifts.sh --all --user 10089835   特定リスナーの記録すべて
set -euo pipefail
cd "$(dirname "$0")/.."
if [ -n "$(sudo docker compose ps -q --status running showroom-keeper 2>/dev/null)" ]; then
  exec sudo docker compose exec -e PYTHONIOENCODING=utf-8 showroom-keeper python -m showroom_keeper gifts "$@"
fi
# 視聴を止めていても確認できるよう、一時的なコンテナで実行する
exec sudo docker compose run --rm --no-deps showroom-keeper gifts "$@"
