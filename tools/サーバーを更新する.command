#!/bin/bash
# SHOWROOM keeper のサーバーを最新の版にする (Mac でダブルクリックして使う)
# サーバーにつないで tools/update.sh を実行する (git pull → 作り直して起動し直す → 確かめる)。
# 視聴は 1 分ほど途切れる。終わったら、このウィンドウは閉じてよい。

INSTANCE=instance-20261014-163017
ZONE=asia-northeast1-c
PROJECT=officea-prod
SERVER_USER=mutsukuchiki

export PATH="$HOME/google-cloud-sdk/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
GCLOUD=$(command -v gcloud || zsh -ic 'command -v gcloud' 2>/dev/null | tail -n 1)
if [ -z "$GCLOUD" ] || [ ! -x "$GCLOUD" ]; then
  echo "gcloud が見つかりません。ふだんのターミナルで gcloud が使えるか確かめてください。"
  read -n 1 -s -r -p "何かキーを押すと閉じます"
  exit 1
fi

echo "サーバーを最新の版にします…"
# 最初の 1 回は、サーバーにまだ tools/update.sh が無いので git pull してから実行する
"$GCLOUD" compute ssh "$SERVER_USER@$INSTANCE" --zone "$ZONE" --project "$PROJECT" --command \
  'cd ~/SHOWROOM-log-in-System && git pull -q --ff-only origin claude/showroom-login-persistence-gfdxzo && bash tools/update.sh'
code=$?
echo
if [ $code -eq 0 ]; then
  echo "更新が終わりました。管理画面を開いている時は、読み直してください。"
else
  echo "途中で失敗しました (上のメッセージを確かめてください)。"
fi
read -n 1 -s -r -p "何かキーを押すと閉じます"
