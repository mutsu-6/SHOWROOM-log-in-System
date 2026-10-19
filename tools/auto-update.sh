#!/bin/bash
# GitHub の作業ブランチに、動いている版 (showroom_keeper/VERSION) より新しい版があり、自動テスト (GitHub Actions) が全部通っていたら、tools/update.sh で更新する。
# サーバーで 5 分ごとに動かす (tools/install-auto-update.sh で入れる)。Claude Code や Cursor がマージした変更が、
# 誰も操作しなくてもサーバーに入る。
#
# - テストがまだ終わっていない版は、次の回まで待つ。テストが失敗した版は入れない (記録だけ残す)
# - ~/SHOWROOM-log-in-System/.no-auto-update というファイルがあれば何もしない (一時的に止めたい時)
# - 結果は journalctl -u showroom-auto-update で見られる
set -u
cd "$(dirname "$0")/.."
BRANCH=claude/showroom-login-persistence-gfdxzo
REPO=mutsu-6/SHOWROOM-log-in-System
# 変わったら作り直して起動し直すファイル (イメージに入るもの・docker の設定。Dockerfile の COPY と合わせる)
RESTART_FILES='^(showroom_keeper/|requirements\.txt$|Dockerfile$|docker-compose\.yml$|\.dockerignore$)'

if [ -e .no-auto-update ]; then
  exit 0
fi
git fetch -q origin "$BRANCH" || { echo "GitHub から取れませんでした (次の回にもう一度試します)"; exit 0; }
latest=$(git rev-parse "origin/$BRANCH")
# 比べるのは git の HEAD ではなく、動いている版 (tools/update.sh が作り直す時に書く VERSION)。
# git pull だけを先にした時も、作り直していなければ更新する。VERSION が無ければ (版が分からなければ) 更新する
running=$(cut -d' ' -f1 showroom_keeper/VERSION 2>/dev/null)
if [ -n "$running" ] && [ "$(git rev-parse -q --verify "$running^{commit}" 2>/dev/null)" = "$latest" ]; then
  exit 0
fi
short=$(git rev-parse --short "$latest")
# 作り直しに失敗した版は、5 分ごとにやり直さない (サーバーの CPU を使い続けないように)。新しい版が来たらまた試す
if [ "$(cat .auto-update-build-failed 2>/dev/null)" = "$latest" ]; then
  exit 0
fi
# 自動テストの結果 (公開リポジトリなので、ログインなしで読める)
checks=$(curl -fsS -m 30 -H "Accept: application/vnd.github+json" \
  "https://api.github.com/repos/$REPO/commits/$latest/check-runs") || { echo "テストの結果を読めませんでした (次の回にもう一度試します)"; exit 0; }
verdict=$(printf '%s' "$checks" | python3 -c '
import json, sys
runs = json.load(sys.stdin).get("check_runs") or []
if not runs or any(r.get("status") != "completed" for r in runs):
    print("wait")
elif all(r.get("conclusion") in ("success", "skipped", "neutral") for r in runs):
    print("ok")
else:
    print("fail")
')
case "$verdict" in
  wait)
    echo "新しい版 $short の自動テストがまだ終わっていないので、次の回まで待ちます"
    exit 0 ;;
  fail)
    # 同じ版のことを 5 分ごとに書かないように、1 回だけ書く
    if [ "$(cat .auto-update-failed 2>/dev/null)" != "$latest" ]; then
      echo "新しい版 $short は自動テストが通っていないので、入れません"
      echo "$latest" > .auto-update-failed
    fi
    exit 0 ;;
esac
# 動いているプログラムに関わるファイル (イメージに入るもの・docker の設定) が変わっていなければ、起動し直さない。
# 起動し直すと視聴のアバターが 1 分半〜2 分ほどルームから抜ける (2026-10-19 は説明書だけの変更で 10 回ほど起動し直していた)。
# 説明書・テスト・tools/ は取り込むだけにして、版の番号を進める。次にプログラムが変わった時に、まとめて作り直す
if [ -n "$running" ] && changed=$(git -c core.quotepath=off diff --name-only "$running" "$latest" 2>/dev/null) && [ -n "$changed" ] \
   && ! printf '%s\n' "$changed" | grep -qE "$RESTART_FILES"; then
  if ! { git checkout -q "$BRANCH" && git pull -q --ff-only origin "$BRANCH"; }; then
    echo "新しい版 $short を取り込めませんでした (次の回にもう一度試します)"
    exit 0
  fi
  git log -1 --format='%h %ci' > showroom_keeper/VERSION
  # 管理画面の「動いている版」も進める (中のプログラムは同じなので、作り直さない)
  sudo docker compose exec -T showroom-keeper sh -c 'cat > /app/showroom_keeper/VERSION' < showroom_keeper/VERSION 2>/dev/null || true
  echo "新しい版 $short はプログラムの変わらない変更 (説明書・テストなど) だけなので、起動し直さずに取り込みました"
  exit 0
fi
echo "新しい版 $short の自動テストが通ったので、更新します"
if ! bash tools/update.sh; then
  echo "$latest" > .auto-update-build-failed
  echo "版 $short の更新に失敗しました。この版ではもうやり直しません (次の新しい版で、また試します)"
  exit 1
fi
rm -f .auto-update-build-failed
