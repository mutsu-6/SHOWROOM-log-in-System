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

# メモリが少ないサーバー (例: Google Cloud の e2-micro = 1GB、e2-small = 2GB) ではスワップを追加して
# Chrome の強制終了や、メモリ不足で全体が止まるのを防ぐ
MEM_MB=$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)
if [ "$MEM_MB" -lt 3500 ] && [ "$(swapon --show | wc -l)" -eq 0 ]; then
  echo "==> メモリが ${MEM_MB}MB と少ないため、2GB のスワップを追加します"
  fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile >/dev/null && swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
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

# 視聴ルーム・ギフト記録のルーム・イベントのどれも設定されていなければ尋ねる
if [ ! -f .env ] || ! grep -qE "^SHOWROOM_(ROOMS|RECORD_ROOMS|EVENTS)=..*" .env; then
  echo "==> 設定 (.env) に視聴するルームを追加します"
  read -r -p "視聴するルームの URL (複数はカンマ区切り): " ROOMS < /dev/tty
  umask 077
  # ログイン情報など既存の設定は残し、空の SHOWROOM_ROOMS= の行だけ除いて追記する
  if [ -f .env ]; then sed -i '/^SHOWROOM_ROOMS=$/d' .env; fi
  # 値に $ などが含まれても展開されないよう単一引用符で囲む
  echo "SHOWROOM_ROOMS='$ROOMS'" >> .env
fi
chmod 600 .env

echo "==> 起動します (初回はイメージ作成に数分かかります)"
docker compose up -d --build

cat <<MSG

セットアップ完了。サーバーを再起動しても自動で再開します。
  ログを見る:  cd $DIR && sudo docker compose logs -f
  止める:      cd $DIR && sudo docker compose down
  ルーム変更:  $DIR/.env の SHOWROOM_ROOMS (ギフト記録は SHOWROOM_RECORD_ROOMS / SHOWROOM_EVENTS) を編集して sudo docker compose up -d
MSG
