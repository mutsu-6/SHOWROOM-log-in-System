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
previous=$(cat showroom_keeper/VERSION 2>/dev/null || true)
git log -1 --format='%h %ci' > showroom_keeper/VERSION
cat showroom_keeper/VERSION

echo "== 3. 作り直して起動し直します (数分かかることがあります)"
# 起動の確かめに失敗した時に戻せるよう、いま動いている版のイメージを覚えておく
old_image=$(sudo docker compose images -q showroom-keeper 2>/dev/null | head -1 || true)
image_name=$(sudo docker compose config --images 2>/dev/null | head -1 || true)
if ! sudo docker compose up -d --build; then
  # 作り直しに失敗した時は、前の版が動いたままなので、版の番号も前に戻す
  # (戻さないと、新しい版が動いていると思い込み、自動の更新もやり直さない)
  if [ -n "$previous" ]; then echo "$previous" > showroom_keeper/VERSION; else rm -f showroom_keeper/VERSION; fi
  echo "   作り直しに失敗しました。前の版のまま動いています。上のメッセージを確かめてください"
  exit 1
fi

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
echo "   60 秒たっても管理画面が動きません。ログ:"
sudo docker compose logs --tail 40 showroom-keeper
# 新しい版が動かないので、前の版のイメージで起動し直す (版の番号も前に戻す)
if [ -n "$old_image" ] && [ -n "$image_name" ]; then
  echo "== 前の版に戻します"
  if sudo docker tag "$old_image" "$image_name" && sudo docker compose up -d --no-build; then
    if [ -n "$previous" ]; then echo "$previous" > showroom_keeper/VERSION; else rm -f showroom_keeper/VERSION; fi
    echo "   前の版に戻しました (${previous:-版の番号は不明})。新しい版のどこが悪いかを、上のログで確かめてください"
  else
    echo "   前の版に戻せませんでした。上のメッセージを確かめてください"
  fi
fi
exit 1
