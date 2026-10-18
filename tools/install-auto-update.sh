#!/bin/bash
# サーバーに、5 分ごとの自動の更新 (tools/auto-update.sh) を入れる。最初に 1 回だけ実行する:
#   cd ~/SHOWROOM-log-in-System && git pull && bash tools/install-auto-update.sh
# 止める:     sudo systemctl disable --now showroom-auto-update.timer
# 一時停止:   touch ~/SHOWROOM-log-in-System/.no-auto-update   (消すと再開)
# 記録を見る: journalctl -u showroom-auto-update --since today
set -e
cd "$(dirname "$0")/.."
DIR=$(pwd)
USER_NAME=$(id -un)
sudo tee /etc/systemd/system/showroom-auto-update.service >/dev/null <<UNIT
[Unit]
Description=SHOWROOM Analytics: GitHub の新しい版を確かめて更新する
After=network-online.target docker.service

[Service]
Type=oneshot
User=$USER_NAME
WorkingDirectory=$DIR
ExecStart=/bin/bash $DIR/tools/auto-update.sh
UNIT
sudo tee /etc/systemd/system/showroom-auto-update.timer >/dev/null <<UNIT
[Unit]
Description=SHOWROOM Analytics: 5 分ごとに新しい版を確かめる

[Timer]
OnBootSec=3min
OnUnitActiveSec=5min

[Install]
WantedBy=timers.target
UNIT
sudo systemctl daemon-reload
sudo systemctl enable --now showroom-auto-update.timer
echo "自動の更新を入れました。5 分ごとに GitHub の新しい版を確かめ、自動テストが通っていれば更新します。"
systemctl list-timers showroom-auto-update.timer --no-pager | head -3
