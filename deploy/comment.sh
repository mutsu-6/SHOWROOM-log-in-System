#!/usr/bin/env bash
# サーバー (Docker) で常駐中の keeper から配信にコメントする。
#   ~/SHOWROOM-log-in-System/deploy/comment.sh こんにちは      1 件投稿
#   ~/SHOWROOM-log-in-System/deploy/comment.sh                 会話モード (1 行ずつ入力して投稿)
#   ~/SHOWROOM-log-in-System/deploy/comment.sh --room ルーム名 こんにちは   (複数ルーム監視時)
# `bash deploy/comment.sh --install` で短縮コマンド `c` (コメント) と `s` (配信状況) を登録する。
set -euo pipefail
cd "$(dirname "$0")/.."
if [ "${1:-}" = "--install" ]; then
  for LINE in "alias c='$(pwd)/deploy/comment.sh'" "alias s='$(pwd)/deploy/status.sh'"; do
    grep -qxF "$LINE" ~/.bashrc 2>/dev/null || echo "$LINE" >> ~/.bashrc
  done
  echo "登録しました。次のコマンドで有効にしてください:  source ~/.bashrc"
  exit 0
fi
if [ -z "$(sudo docker compose ps -q --status running showroom-keeper 2>/dev/null)" ]; then
  echo "視聴が止まっているためコメントできません。先に次のコマンドで視聴を開始してください:"
  echo "  cd $(pwd) && sudo docker compose up -d"
  exit 1
fi
# 本文が無ければ会話モード (端末入力が必要なので -T を付けない)
TTY=-T
if [ $# -eq 0 ] || { [ "$1" = "--room" ] && [ $# -eq 2 ]; }; then TTY=; fi
exec sudo docker compose exec $TTY -e PYTHONIOENCODING=utf-8 showroom-keeper python -m showroom_keeper comment "$@"
