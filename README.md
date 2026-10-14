# SHOWROOM log in System

[SHOWROOM](https://www.showroom-live.com/) にログインした状態で、指定したルームの **視聴状態を維持し続ける** 常駐システムです。
ヘッドレス Chromium (Playwright) で実際に配信ページを開き、止まったら自動で復旧します。

## 主な機能

| 機能 | 内容 |
| --- | --- |
| ログイン維持 | ID/パスワードで自動ログイン、または手動ログインしたセッション (Cookie) を保存して再利用。定期的にログイン状態を確認し、切れていれば再ログイン |
| 配信待機 | 配信状態を定期確認し、配信が始まったら自動で視聴ページを開く。終了したらページを閉じて次の配信を待つ |
| 再生監視 | 動画の再生位置が進んでいるかを定期確認。止まっていれば再生を再開し、一定回数続けばページをリロード |
| 定期リロード | 長時間視聴でのメモリ増加やセッション劣化を避けるため、一定間隔でリロード |
| 障害復旧 | ネットワーク断・ページクラッシュ・ブラウザ終了を検知し、指数バックオフで自動再起動 |
| 複数ルーム | 複数ルームを同時に監視 (ルームごとにタブを分離) |
| 視聴時間ログ | ルームごとの累計視聴時間をログ出力 |

## セットアップ

Python 3.9 以上と **Google Chrome** が必要です。
Playwright 付属の Chromium は H.264 に非対応で SHOWROOM の映像を再生できないため、インストール済みの Chrome を使います
(Chrome が無い場合は警告を出して付属 Chromium で起動します)。

```bash
git clone https://github.com/mutsu-6/SHOWROOM-log-in-System.git
cd SHOWROOM-log-in-System
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
playwright install chromium   # Chrome が無い環境向けの予備

cp config.example.yaml config.yaml   # rooms を編集
cp .env.example .env                 # 必要ならログイン情報を記入
```

## ログイン

次のどちらかを使います。

1. **手動ログイン (推奨・CAPTCHA にも対応)**

   ```bash
   python -m showroom_keeper login
   ```

   ブラウザが開くので SHOWROOM にログインし、ターミナルで Enter を押します。
   セッションは `state/storage_state.json` に保存され、以後の起動で自動的に使われます。

2. **自動ログイン**

   `.env` (または環境変数) に `SHOWROOM_ACCOUNT_ID` / `SHOWROOM_PASSWORD` を設定しておくと、未ログイン時やセッション切れ時に自動でログインします。
   CAPTCHA を要求された場合は自動ログインできないため、1 の手動ログインを行ってください。

ログインしなくても匿名で視聴状態は維持されます。

## 起動

```bash
# config.yaml の rooms を監視
python -m showroom_keeper run

# ルームを引数で指定 (URL / room_url_key どちらでも可)
python -m showroom_keeper run https://www.showroom-live.com/r/ROOM_URL_KEY other_room

# ブラウザ画面を表示して動作確認
python -m showroom_keeper run --headed
```

`Ctrl+C` (SIGINT) / SIGTERM で、セッションを保存してから安全に終了します。

### 常駐させる

- **Docker**: `.env` と `config.yaml` を用意し `docker compose up -d`。
  セッションは `./data/storage_state.json` に保存されます。手動ログインはコンテナ内ではできないため、
  ローカルで `login` を実行して作った `state/storage_state.json` を `./data/` にコピーしてください。
- **systemd**: `deploy/showroom-keeper.service` を参照。

## 設定

`config.example.yaml` に全項目と説明があります。カレントディレクトリの `.env` は自動で読み込まれ、環境変数は設定ファイルより優先されます。

| 環境変数 | 内容 |
| --- | --- |
| `SHOWROOM_ACCOUNT_ID` / `SHOWROOM_PASSWORD` | ログイン情報 |
| `SHOWROOM_ROOMS` | 監視ルーム (カンマ区切り) |
| `SHOWROOM_HEADLESS` | `false` で画面表示 |
| `SHOWROOM_STATE_FILE` | セッション保存先 |
| `SHOWROOM_BROWSER_CHANNEL` | 使用ブラウザ (`chrome` / `msedge` / 空で付属 Chromium) |
| `SHOWROOM_BROWSER_EXECUTABLE` | 使用するブラウザ実行ファイルのパス |
| `SHOWROOM_PROXY` (または `HTTPS_PROXY`) | ブラウザが使うプロキシ |

## トラブルシューティング

| ログ | 原因と対処 |
| --- | --- |
| `このルームは視聴できません` | 年齢制限付きルームなど、未ログインでは見られないルームです。`login` でログインしてください |
| `再生が進んでいません` が続きリロードを繰り返す | Chrome ではなく付属 Chromium で起動している (起動時の警告を確認)、または回線が WebRTC (UDP) を通さない可能性があります |
| ページの読み込みが異常に遅い | `python -m showroom_keeper diagnose` で、どの通信が遅いかを計測できます |
| `未ログインです` | 保存したセッションの期限切れです。`login` をやり直すか、ID/パスワードを設定してください |

## 構成

```
showroom_keeper/
  cli.py      コマンドライン (run / login)
  config.py   設定ファイル・環境変数の読み込み
  api.py      SHOWROOM API (配信状態・ログイン状態・ログイン)
  watcher.py  1 ルーム分の監視ループ (配信待機・再生監視・リロード・バックオフ)
  keeper.py   ブラウザ管理・セッション保存・全ウォッチャーの統括
tests/        pytest によるテスト
```

## 注意事項

- `.env`、`config.yaml`、`state/` (セッション Cookie) には認証情報が含まれます。`.gitignore` 済みですが、共有しないでください。保存したセッションファイルは権限 600 で書き出します。
- 映像は WebRTC (UDP) で受信します。UDP を遮断するネットワークやプロキシ経由では映像を受信できません。
- SHOWROOM の非公式 API・ページ構造に依存しているため、サイト側の仕様変更で動かなくなる可能性があります。
- SHOWROOM の利用規約を確認のうえ、自己責任で利用してください。
