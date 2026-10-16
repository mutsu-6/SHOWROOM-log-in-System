# 報告

データ分析戦略部隊が、毎週月曜日 8:45 (日本時間) に見直しをして、ここに `YYYY-MM-DD.md` の報告書を置きます。
報告ができると、スマートフォンに知らせが届きます (見直しは、このリポジトリに書き込める Claude Code のセッションの中で動きます)。

## サーバーの数字を部隊に渡す (任意)
部隊はサーバーのデータを直接は見られません。数字を渡すと、報告がくわしくなります。
サーバーの画面 (`mutsukuchiki@instance-...`) で次を実行し、出た文字を `docs/報告/データ_YYYY-MM-DD.md` として置いてください
(Claude Code か Cursor に「この結果を docs/報告/データ_今日の日付.md に入れて」と頼めば置いてくれます)。
データは読むだけで、何も変えません。ギフターの名前・ID は出しません。

```
cd ~/SHOWROOM-log-in-System && sudo docker compose exec -T showroom-keeper python -c "
from showroom_keeper.research import store_status
import time
s = store_status('/data')
print('# サーバーの数字', time.strftime('%Y-%m-%d %H:%M'))
for k, label in [('rooms','見つけたルーム'),('collected','集めたルーム'),('live_rooms','30日以内に配信'),('events','イベント'),
                 ('rankings','貢献ランキング'),('rankings_waiting','取る予定のランキング'),('gifters','分かったギフター'),('db_mb','保存先の大きさ (MB)')]:
    print(f'- {label}: {s.get(k)}')
print('- ルーム番号の調べ:', s.get('scan_next'), '/', s.get('scan_end'))
print('- 過去のイベント (次に読む番号):', s.get('history_next'), '/', s.get('event_max'))
" && df -h / | tail -1 && sudo docker stats --no-stream --format 'CPU {{.CPUPerc}} / メモリ {{.MemUsage}}'
```
