#!/usr/bin/env bash
# サーバー (Docker) で常駐中の keeper から配信にコメントを 1 件投稿する。
#   ~/SHOWROOM-log-in-System/deploy/comment.sh こんにちは
#   ~/SHOWROOM-log-in-System/deploy/comment.sh --room ルーム名 こんにちは   (複数ルーム監視時)
set -euo pipefail
cd "$(dirname "$0")/.."
if [ $# -eq 0 ]; then echo "使い方: $0 [--room ルーム名] コメント"; exit 2; fi
exec sudo docker compose exec -T showroom-keeper python -m showroom_keeper comment "$@"
