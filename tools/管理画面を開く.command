#!/bin/bash
# SHOWROOM Analytics の管理画面を開く (Mac でダブルクリックして使う)
#
# 1. 前のつなぎ道 (8080 番) が残っていれば閉じる
# 2. サーバーへのつなぎ道を開く。切れたら (Mac のスリープなど) 5 秒後に自動でつなぎ直す
# 3. つながったら、ブラウザで管理画面を開く
# このウィンドウを開いている間は Mac を自動でスリープさせない (caffeinate)。
# やめる時は、このウィンドウで Ctrl+C を押すか、ウィンドウを閉じる。

INSTANCE=instance-20261014-163017
ZONE=asia-northeast1-c
PROJECT=officea-prod
SERVER_USER=mutsukuchiki
PORT=8080
RETRY=${RETRY:-5}

# ダブルクリックで開いた時は、ふだんのターミナルの設定 (.zshrc) が読まれないので gcloud の場所を探す
export PATH="$HOME/google-cloud-sdk/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
GCLOUD=$(command -v gcloud || zsh -ic 'command -v gcloud' 2>/dev/null | tail -n 1)
if [ -z "$GCLOUD" ] || [ ! -x "$GCLOUD" ]; then
  echo "gcloud が見つかりません。ふだんのターミナルで gcloud が使えるか確かめてください。"
  read -n 1 -s -r -p "何かキーを押すと閉じます"
  exit 1
fi

echo "管理画面へのつなぎ道を開きます。このウィンドウは開いたままにしてください (閉じると管理画面も開けなくなります)。"
lsof -ti "tcp:$PORT" | xargs kill 2>/dev/null

# つながったらブラウザで開く (最大 90 秒待つ)
(
  for _ in $(seq 1 90); do
    if nc -z localhost "$PORT" 2>/dev/null; then
      echo "つながりました。ブラウザで管理画面を開きます。"
      open "http://localhost:$PORT/"
      exit 0
    fi
    sleep 1
  done
  echo "90 秒たってもつながりません。このウィンドウに出ているエラーを確かめてください。"
) &

export GCLOUD SERVER_USER INSTANCE ZONE PROJECT PORT RETRY
caffeinate -is bash -c '
  while true; do
    "$GCLOUD" compute ssh "$SERVER_USER@$INSTANCE" --zone "$ZONE" --project "$PROJECT" -- \
      -L "$PORT:localhost:$PORT" -N -o ServerAliveInterval=30 -o ServerAliveCountMax=6 -o ExitOnForwardFailure=yes
    echo "$(date "+%H:%M:%S") 切れました。${RETRY} 秒後につなぎ直します (やめる時は Ctrl+C)"
    sleep "$RETRY"
  done
'
