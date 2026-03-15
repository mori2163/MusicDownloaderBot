# MusicDownloaderBot

![DL_command](https://github.com/mori2163/MusicDownloaderBot/blob/main/docs/dl_command.png?raw=true)
![Voice_Player](https://github.com/mori2163/MusicDownloaderBot/blob/main/docs/voice_player.png?raw=true)

MusicDownloaderBot は、Discord のスラッシュコマンドで **Qobuz / YouTube / Spotify** の音楽を扱える、汎用的な **ダウンロード & 音楽配信 Bot** です。  
ダウンロード、キュー管理、メタデータ取得、ファイル配布（直接添付 or ダウンロードリンク）、ボイス再生までを1つの Bot でまとめて運用できます。

## 主な機能

- **マルチソース対応**
  - Qobuz（高音質取得）
  - YouTube
  - Spotify（`spotdl` 経由）
- **Discord から完結する操作性**
  - `/dl` でダウンロード予約
  - `/queue` でダウンロード/再生キューを確認
  - `/join` `/play` `/search` `/stop` `/replay` `/autoplay` `/leave` でボイス再生を制御
- **大容量ファイル配布**
  - 小さいファイルは Discord に直接添付
  - 大きいファイルは有効期限付きダウンロードリンクを発行
- **外部公開オプション**
  - Cloudflare Tunnel（Quick / Named）対応

## 動作要件

- Python **3.11+**
- FFmpeg（PATH が通っていることを推奨）
- Discord Bot Token
- （任意）Qobuz アカウント情報
- （任意）Cloudflare Tunnel を使う場合は `cloudflared`

## セットアップ

### 1) 依存関係をインストール

`uv` を使う場合（推奨）:

```bash
uv sync
```

### 2) 環境変数を設定

```bash
cp .env.example .env
```

最低限、以下は設定してください。

```env
# 必須
DISCORD_TOKEN=your_discord_bot_token

# 出力先（汎用）
DOWNLOAD_PATH=./downloads
LIBRARY_PATH=./library
```

> `LIBRARY_PATH` は「任意の音楽保存先フォルダ」です。特定サービス専用ではありません。

Qobuz を使う場合:

```env
QOBUZ_EMAIL=your_email@example.com
QOBUZ_PASSWORD=your_password
```

### 3) Bot を起動

```bash
uv run python main.py
```

## Discord コマンド

### ダウンロード

- `/dl <url>`  
  Qobuz / YouTube / Spotify の URL を投入し、メタデータ確認後にキュー追加
- `/queue`  
  ダウンロード実行状況と、ボイス再生キュー状況を表示

### ボイス再生（YouTube）

- `/join` : Bot をボイスチャンネルへ接続
- `/play [youtube_url]` : URL を再生（省略時はボイスキュー先頭）
- `/search <query>` : YouTube 検索結果をボタンで選んで再生/キュー追加
- `/stop` : 再生停止（キュー保持）
- `/replay` : 現在/直前の曲を先頭から再生
- `/autoplay <enabled>` : 自動再生 ON/OFF
- `/leave` : ボイスチャンネルから退出

## ファイル配布の挙動

ダウンロード完了後はアルバム（または取得フォルダ）を zip 化して通知します。

- `DOWNLOAD_SIZE_THRESHOLD` 未満: Discord 添付で送信
- `DOWNLOAD_SIZE_THRESHOLD` 以上: ダウンロードリンクを発行（`FILE_SERVER_BASE_URL` または Tunnel 必須）

## 外部公開（ダウンロードリンク）

### A. Quick Tunnel（手軽・一時URL）

```env
CLOUDFLARE_TUNNEL_ENABLED=true
CLOUDFLARE_TUNNEL_MODE=quick
```

### B. Named Tunnel（固定URL運用）

```env
CLOUDFLARE_TUNNEL_ENABLED=true
CLOUDFLARE_TUNNEL_MODE=named
CLOUDFLARE_TUNNEL_NAME=music-bot
CLOUDFLARE_CONFIG_PATH=~/.cloudflared/config.yml
FILE_SERVER_BASE_URL=https://music.example.com
```

### C. 手動公開（トンネル無効）

```env
CLOUDFLARE_TUNNEL_ENABLED=false
FILE_SERVER_BASE_URL=https://your-public-url.example
```

## セキュリティ注意（重要）

`POST /upload` を外部公開する場合、`UPLOAD_TOKEN` を必ず設定してください。

```env
UPLOAD_TOKEN=very_long_random_secret_token
```

- 未設定だと第三者による不正アップロードのリスクがあります。
- 強力なランダム値を使用し、定期ローテーションを推奨します。

## 主な環境変数

| カテゴリ | 変数名 | 既定値 | 説明 |
|---|---|---:|---|
| 基本 | `DISCORD_TOKEN` | - | Discord Bot トークン（必須） |
| パス | `DOWNLOAD_PATH` | `./downloads` | 一時ダウンロード先 |
| パス | `LIBRARY_PATH` | `./library` | 最終保存先（任意の音楽ライブラリ） |
| Qobuz | `QOBUZ_EMAIL` | - | Qobuz メール |
| Qobuz | `QOBUZ_PASSWORD` | - | Qobuz パスワード |
| キュー | `MAX_RETRIES` | `3` | ダウンロード再試行回数 |
| キュー | `QUEUE_MAX_SIZE` | `100` | キュー最大件数 |
| ボイス | `VOICE_TARGET_BITRATE_KBPS` | `128` | ボイス再生目標ビットレート |
| ボイス | `VOICE_STREAM_CACHE_SECONDS` | `180` | 再生URLキャッシュ秒数 |
| ボイス | `VOICE_SEARCH_RESULT_LIMIT` | `5` | `/search` 結果件数 |
| 配布 | `FILE_SERVER_PORT` | `8080` | ファイルサーバーポート |
| 配布 | `FILE_SERVER_BASE_URL` | - | ダウンロードリンク生成のベースURL |
| 配布 | `DOWNLOAD_SIZE_THRESHOLD` | `10485760` | 直接添付→リンク切替サイズ(Byte) |
| 配布 | `DOWNLOAD_LINK_MAX_COUNT` | `3` | リンク利用可能回数 |
| 配布 | `DOWNLOAD_LINK_EXPIRE_HOURS` | `24` | リンク有効期限(時間) |
| アップロード | `UPLOAD_PATH` | `./uploads` | アップロード保存先 |
| アップロード | `UPLOAD_MAX_SIZE` | `1073741824` | アップロード上限(Byte) |
| アップロード | `UPLOAD_TOKEN` | - | アップロード認証トークン |
| Tunnel | `CLOUDFLARE_TUNNEL_ENABLED` | `false` | Tunnel自動管理の有効化 |
| Tunnel | `CLOUDFLARE_TUNNEL_MODE` | `quick` | `quick` / `named` |

## トラブルシューティング

- **起動時に設定エラー**
  - `DISCORD_TOKEN` 未設定、または Qobuz の片側のみ設定が多い原因です。
- **ボイス接続エラー（4017 など）**
  - 依存ライブラリ（`discord.py`, `davey`, `PyNaCl`）を更新し、再起動してください。
- **YouTube再生に失敗する**
  - `yt-dlp` を更新し、必要に応じて `YOUTUBE_PO_TOKEN` を見直してください。
- **大容量ファイルのリンクが出ない**
  - `FILE_SERVER_BASE_URL` 未設定、または Tunnel 起動失敗を確認してください。

## プロジェクト構成

```text
MusicDownloderBot/
├── main.py
├── bot.py
├── queue_manager.py
├── voice_player.py
├── file_server.py
├── tunnel_manager.py
├── metadata_fetcher.py
├── url_parser.py
├── config.py
├── downloaders/
│   ├── base.py
│   ├── qobuz.py
│   ├── youtube.py
│   └── spotify.py
└── ecosystem.config.js  # PM2設定ファイル
```

## 免責事項

このプロジェクトは技術検証・個人利用を目的としています。  
利用時は各サービスの利用規約および著作権法を遵守してください。
