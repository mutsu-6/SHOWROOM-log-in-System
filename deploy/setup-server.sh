#!/usr/bin/env bash
# サーバー (Ubuntu 22.04 / 24.04, x86_64) で SHOWROOM keeper を Docker で常駐させる。
# 手元の PC からこのフォルダをサーバーにコピーしたうえで、サーバー上で実行する:
#   sudo bash ~/SHOWROOM-log-in-System/deploy/setup-server.sh
# 何度実行してもよい (更新時は再コピー、または git clone した場合は git pull 後に再実行)。
set -euo pipefail

DIR=$(cd "$(dirname "$0")/.." && pwd)
cd "$DIR"

if [ "$(id -u)" -ne 0 ]; then echo "sudo を付けて実行してください"; exit 1; fi
if [ "$(uname -m)" != "x86_64" ]; then
  echo "このサーバーは $(uname -m) です。Google Chrome は x86_64 (amd64) のサーバーでのみ動作します"; exit 1
fi

echo "==> Docker を準備します"
if ! command -v docker >/dev/null; then
  curl -fsSL https://get.docker.com | sh
fi
systemctl enable --now docker >/dev/null

mkdir -p data && chmod 700 data
# 手元で login して作ったログイン状態を、コピーされていれば data/ に移す
if [ -f state/storage_state.json ] && [ ! -f data/storage_state.json ]; then
  mv state/storage_state.json data/storage_state.json
fi
[ -f data/storage_state.json ] && chmod 600 data/storage_state.json || \
  echo "注意: ログイン状態 (data/storage_state.json) がありません。未ログインで視聴します"

if [ ! -f .env ] || ! grep -q "^SHOWROOM_ROOMS=..*" .env; then
  echo "==> 設定 (.env) を作成します"
  read -r -p "視聴するルームの URL (複数はカンマ区切り): " ROOMS < /dev/tty
  umask 077
  # 値に $ などが含まれても展開されないよう単一引用符で囲む
  echo "SHOWROOM_ROOMS='$ROOMS'" > .env
fi
chmod 600 .env

echo "==> 起動します (初回はイメージ作成に数分かかります)"
docker compose up -d --build

cat <<MSG

セットアップ完了。サーバーを再起動しても自動で再開します。
  ログを見る:  cd $DIR && sudo docker compose logs -f
  止める:      cd $DIR && sudo docker compose down
  ルーム変更:  $DIR/.env の SHOWROOM_ROOMS を編集して sudo docker compose up -d
MSG
