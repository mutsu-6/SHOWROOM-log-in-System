#!/bin/bash
# GitHub の作業ブランチに新しい版があり、自動テスト (GitHub Actions) が全部通っていたら、tools/update.sh で更新する。
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

if [ -e .no-auto-update ]; then
  exit 0
fi
git fetch -q origin "$BRANCH" || { echo "GitHub から取れませんでした (次の回にもう一度試します)"; exit 0; }
current=$(git rev-parse HEAD)
latest=$(git rev-parse "origin/$BRANCH")
if [ "$current" = "$latest" ]; then
  exit 0
fi
short=$(git rev-parse --short "$latest")
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
echo "新しい版 $short の自動テストが通ったので、更新します"
bash tools/update.sh
