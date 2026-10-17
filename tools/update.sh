#!/bin/bash
# サーバーで SHOWROOM keeper を最新の版にする (サーバーの中で実行する)
#   cd ~/SHOWROOM-log-in-System && bash tools/update.sh
# Mac からは tools/サーバーを更新する.command をダブルクリックすると、これをサーバーで実行する。
#
# 1. GitHub の作業ブランチから最新を取る (git pull)
# 2. 動いている版の番号を showroom_keeper/VERSION に書く (管理画面の「収集」と上の表示に出る)
# 3. 作り直して起動し直す (docker compose up -d --build)。視聴は 1 分ほど途切れる
# 4. 起動したか確かめる
set -e
cd "$(dirname "$0")/.."
BRANCH=claude/showroom-login-persistence-gfdxzo

echo "== 1. 最新を取ります"
before=$(git rev-parse --short HEAD)
git checkout -q "$BRANCH"
git pull --ff-only origin "$BRANCH"
after=$(git rev-parse --short HEAD)
if [ "$before" = "$after" ]; then
  echo "   すでに最新でした ($after)"
else
  echo "   $before → $after に更新しました。入った変更:"
  git log --format='   - %s' "$before..$after" | head -30
fi

echo "== 2. 版の番号を書きます"
git log -1 --format='%h %ci' > showroom_keeper/VERSION
cat showroom_keeper/VERSION

echo "== 3. 作り直して起動し直します (数分かかることがあります)"
sudo docker compose up -d --build

echo "== 4. 起動したか確かめます"
for _ in $(seq 1 30); do
  # パスワードを設定していると 401 が返る (それも管理画面が動いている印)
  code=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8080/ || true)
  if [ "$code" = "200" ] || [ "$code" = "401" ]; then
    echo "   管理画面が動いています"
    sudo docker compose logs --tail 15 showroom-keeper
    echo "== 終わりました ($after)"
    exit 0
  fi
  sleep 2
done
echo "   60 秒たっても管理画面が動きません。ログを見てください:"
sudo docker compose logs --tail 40 showroom-keeper
exit 1
