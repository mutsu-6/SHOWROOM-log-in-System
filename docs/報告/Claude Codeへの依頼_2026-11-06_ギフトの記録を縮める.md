# Claude Code への依頼: ギフトの記録 (gifts・live_events) の古い分を gzip にしまう (2026-11-06、Cursor から)

利用者の指示 (2026-11-06 22:16「Claude に、ギフトの記録の圧縮から頼む」)。数字と理由は `docs/報告/Cursorの作業_2026-11-06.md`。
読む所 (explore・listing・gifts・stage_ranks・event_analysis・research_store) は Claude Code の受け持ちなので、お願いします。
しまう仕事をどこに置くか (sweep の合間・catchup・毎朝のバックアップのあと など) も決めてください。Cursor が書く方がよければ言ってください。

## なぜ
- `data/gifts/` は 1.27GB で、1 日 約 75MB 増える (ディスクの増え方 1 日 約 230MB のうち 3 分の 1)。`live_events/` は 0.35GB・1 日 約 15MB
- gzip にすると gifts は 11〜13 分の 1、live_events は 9 分の 1 (11/6 に測った)。今あるだけで 約 1.4GB 空き、増え方も 1 日 約 80MB 減る
- 空きは 9.0GB (11/6 22 時)。何もしないと 2〜3 週間で、毎朝の写しがまた作れなくなる

## 今の形 (気をつける所)
- ギフトの記録は、ルームごとに 1 つのファイル `gifts/<ルーム>.jsonl` に、ずっと足していく (gifts.py)。live_events も同じ (`live_events/<ルーム>.jsonl`)
- `explore.update_gift_senders`・`update_gift_events`・`update_live_visits`・research_store の room_gift_daily は、ルームごとに
  「どこまで読んだか (バイトの位置と大きさ)」を覚えていて、**ファイルが前より小さくなると、そのルームの表の中身を最初から作り直す**
  (gift_events なら `DELETE FROM gift_events WHERE room_key=?` のあと読み直す)。そのままファイルを切り詰めると、しまった分が表から消える
- `gifts.load_recorded`・`listing._record_files`・`event_analysis` (297 行目あたり)・`explore` (279 行目あたり)・`stage_ranks` は、`*.jsonl` を全部読む

## お願いしたい形 (案。よりよい形があれば変えてください)
1. 終わった月の分を `gifts/<ルーム>.<YYYYMM>.jsonl.gz` (live_events も同じ) に移し、`<ルーム>.jsonl` には今の月の分だけを残す
   (月が変わって 2 日たったら。配信中に月をまたいだ分を、書き込みとぶつけないため)。書き込み中のファイルを切り詰める時は、
   gifts.py の書き込みと同じ鍵 (ロック) を取るか、`<ルーム>.jsonl` を新しいファイルに入れ替える形に
2. ファイル全部を読む所は、`<ルーム>.*.jsonl.gz` を古い順に読んでから `<ルーム>.jsonl` を読む (1 つの関数にまとめる)
3. 「どこまで読んだか」を覚えている所は、しまった時に位置をしまった分だけ引く (または、しまった月の分を読み終えていることを確かめてからしまう)。
   **ファイルが小さくなっても作り直さない**ように
4. 最初の 1 回は、10 月までの分を少しずつ (1 回に数ルーム、メモリ 2GB・CPU 2 つなので重くしない)。しまう前後で
   gift_events・gift_senders・room_gift_daily の件数が変わらないことを確かめる試験を
5. 管理画面のディスクの見張り (`backup.WATCHED`) は gifts・live_events のフォルダ全体を測るので、そのままでよい

## 守ること
- 集めるデータを減らさない (試験段階)。消すのではなく縮めてしまう
- 動いている research.db に重い処理をしない
- ギフターの名前・ID をファイル・コミットに書かない
