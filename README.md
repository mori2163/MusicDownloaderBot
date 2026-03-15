
# DLSyncBot

DLSyncBotは、Discordを通じて **Qobuz**、**YouTube**、**Spotify** から音楽をダウンロードし、メタデータ整理から **Navidrome** への配信準備までを自動化する多機能Discord Botです。

## 概要

音楽収集のワークフロー（ダウンロード、タグ付け、フォルダ整理、ライブラリ追加）をDiscordのコマンド一つで完結させます。

- **マルチプラットフォーム対応**: Qobuz (FLAC/Hi-Res)、YouTube、Spotifyに対応。
- **Navidrome連携**: ダウンロードファイルをNavidromeのライブラリフォルダへ自動配置。
- **メタデータ自動管理**: ジャケット画像、アルバム名、アーティスト名などのタグ情報（ID3）を自動で埋め込みます。
- **ダウンロードリンク発行**: Botがファイルをサーバーへ送信できないサイズの場合、一時的なダウンロードリンクを発行します。

## 主な特徴

### 音源ソースの識別
保存されるフォルダ名で音源の取得元を判別可能です：
- **Qobuz**: 接頭辞なし (例: `Artist - Title`) - 高品質(FLAC)
- **YouTube**: `[YT]` 接頭辞 (例: `[YT] Artist - Title`)
- **Spotify**: `[SP]` 接頭辞 (例: `[SP] Artist - Title`) - 音源はYouTubeから補完

## 前提条件

- **Python 3.11** 以上
- **FFmpeg**: 音声変換・タグ編集に必須です。システムパスに通してください。
- **PyNaCl**: Discordボイス再生に必須です（`uv sync` で自動インストール）。
- **davey**: Discord音声E2EE対応に必要です（`uv sync` で自動インストール）。
- **Node.js**: `yt-dlp` の一部機能で必要となる場合があります。
- **Discord Bot Token**: Discord Developer Portal で取得したもの。
- **Qobuz アカウント**: Qobuzを利用する場合に必要です（サブスクリプション有効なもの）。

## インストール

### 1. リポジトリのクローン

```bash
git clone <repository-url>
cd MusicDownloaderBot
```

### 2. 環境構築

Pythonパッケージマネージャー [uv](https://docs.astral.sh/uv/) の使用を推奨しています。

**uv のインストール:**

```bash
# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh

# Windows (PowerShell)
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
```

**依存関係のインストール:**

```bash
# 仮想環境の作成とライブラリのインストール
uv sync

# または明示的にPythonバージョンを指定
uv sync --python 3.11
```

### 3. 環境変数の設定

`.env.example` をコピーして `.env` ファイルを作成し、設定を書き込みます。

```bash
cp .env.example .env
```

**`.env` の主要設定項目:**

```env
# Discord設定 (必須)
DISCORD_TOKEN=your_discord_bot_token_here

# Qobuz設定 (利用する場合)
QOBUZ_EMAIL=your_email@example.com
QOBUZ_PASSWORD=your_password_here

# パス設定
## Navidromeが監視しているライブラリフォルダ
LIBRARY_PATH=C:/path/to/your/navidrome/music/library
## Botの一時作業用フォルダ
DOWNLOAD_PATH=./downloads
```

## 使用方法

### Botの起動

```bash
# uv環境で起動
uv run python main.py
```

### 常駐化・永続化 (推奨)

サーバーで運用する場合は [PM2](https://pm2.keymetrics.io/) の使用を推奨します。

```bash
# 起動
pm2 start ecosystem.config.js

# ログ確認
pm2 logs music-downloader-bot
```

### Discord コマンド

Bot参加中のサーバーで以下のスラッシュコマンドを使用できます。

#### ダウンロード系
#### `/dl <url>`
指定したURLから音楽をダウンロードします。

**対応URL例:**
- **Qobuz**: `https://open.qobuz.com/album/...` (アルバム/トラック)
- **YouTube**: `https://youtu.be/...` (動画/音楽)
- **Spotify**: `https://open.spotify.com/track/...` (トラック/アルバム/プレイリスト)

#### `/queue`
現在のキュー状態を表示します。  
ダウンロード（実行中/待機中）とボイス再生（状態/待機キュー）をまとめて確認できます。

#### ボイス再生系（YouTubeのみ）
#### `/join`
実行者が参加しているボイスチャンネルへBotを接続します。

#### `/play [youtube_url]`
YouTube URL指定時はその曲を再生（再生中ならキュー追加）します。  
URL省略時は、ボイスキュー先頭の曲を再生します。

再生時には `/dl` と同様にEmbed形式のプレイヤーUIを表示し、以下のボタンを使えます。
- 音量調整（`-25%` / `+25%`）
- 再生/一時停止
- 停止
- 10秒巻き戻し
- 秒数入力ジャンプ（モーダルで任意秒へ移動）
- 再再生（現在または直前の曲を先頭から再生）

#### `/search <query>`
YouTube検索結果（最大5件）をEmbedで表示し、ボタンで選択した曲を即時再生またはキューに追加します。

#### `/replay`
現在再生中、または直前に再生していた曲を先頭から再生します。

#### `/stop`
現在の再生を停止します。キューは保持されます。

#### `/autoplay <enabled>`
キュー再生を継続する自動再生をON/OFFします（キューがある場合はキューを優先して次曲へ進みます）。

#### `/leave`
Botをボイスチャンネルから退出させます。

> ボイス再生は通常用途向けに **128kbps** をデフォルト設定にしています。  
> 再生開始を速くするため、YouTubeストリームURLを短時間キャッシュします。
> さらに、再生URL解決では軽量プロファイル（`android,web`）を優先し、失敗時のみ `node/ejs` や PO Token プロファイルへフォールバックします。
> PO Token設定時も自動で複数プロファイルへフォールバックし、再生失敗時は明示的なエラーを返します。

## ファイル配信機能（ダウンロードリンク）

Discordのファイル添付制限（通常25MB等）を超えるファイルの場合、Botは**一時的なダウンロードリンク**を生成します。

| ファイルサイズ | 提供方法 |
|--------------|----------|
| **制限未満** | Discordチャットにzipファイルを直接添付 |
| **制限超過** | 一時ダウンロードリンクを生成して通知 |

### 外部公開設定 (Cloudflare Tunnel)

ダウンロードリンク機能を自宅サーバー等から外部に提供するには、**Cloudflare Tunnel** の利用が便利です。BotにはCloudflare Tunnelの管理機能が内蔵されています。

以下の3つのモードから選択してください。

#### 【モードA】Quick Tunnel (推奨・手軽)
アカウント不要で、Bot起動時に自動的に一時的な公開URLを発行します。URLは起動ごとに変わります。

1. [cloudflared](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/) をインストールし、パスを通す。
2. `.env` を設定：
    ```env
    CLOUDFLARE_TUNNEL_ENABLED=true
    CLOUDFLARE_TUNNEL_MODE=quick
    ```
3. Botを起動すると、自動的にトンネルが確立されます。

#### 【モードB】Named Tunnel (固定URL)
固定ドメインで運用したい場合に使用します。Cloudflareアカウントが必要です。

1. `cloudflared` でトンネルを作成し、configファイルを生成（[公式ドキュメント参照](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/get-started/)）。
2. `.env` を設定：
    ```env
    CLOUDFLARE_TUNNEL_ENABLED=true
    CLOUDFLARE_TUNNEL_MODE=named
    CLOUDFLARE_TUNNEL_NAME=music-bot  # 作成したトンネル名
    CLOUDFLARE_CONFIG_PATH=~/.cloudflared/config.yml
    FILE_SERVER_BASE_URL=https://music.your-domain.com
    ```

#### 【モードC】手動運用
Botとは別に手動でトンネルやリバースプロキシを立ち上げる場合です。公開URLを直接指定します。

1. `.env` を設定：
    ```env
    CLOUDFLARE_TUNNEL_ENABLED=false
    FILE_SERVER_BASE_URL=https://your-public-url.com
    ```

---

### ⚠️ アップロード機能のセキュリティ警告

Quick Tunnel等を使用する場合、`POST /upload` エンドポイントが外部に公開される可能性があります。

- **`UPLOAD_TOKEN` は必ず設定してください。** 未設定の場合、**誰でもファイルをアップロードできる状態**になり大変危険です。
- 強力なランダム文字列を設定し、定期的に変更することを推奨します。

```env
# .env設定例
UPLOAD_TOKEN=very_secret_complex_token_here_12345
```

## 設定ガイド (`.env`)

| カテゴリ | 変数名 | デフォルト | 説明 |
|:---:|---|---|---|
| **基本** | `DISCORD_TOKEN` | - | **必須**: Botトークン |
| | `LIBRARY_PATH` | `./library` | Navidrome管理下の保存先 |
| | `DOWNLOAD_PATH` | `./downloads` | 一時保存フォルダ |
| **Qobuz** | `QOBUZ_EMAIL` | - | ログインメールアドレス |
| | `QOBUZ_PASSWORD` | - | ログインパスワード |
| **制限** | `MAX_RETRIES` | `3` | ダウンロード失敗時の再試行回数 |
| | `QUEUE_MAX_SIZE` | `100` | キューの最大タスク保持数 |
| **ボイス** | `VOICE_TARGET_BITRATE_KBPS` | `128` | Discordボイス再生の目標ビットレート(kbps) |
| | `VOICE_STREAM_CACHE_SECONDS` | `180` | ストリームURLキャッシュ秒数（短いほど鮮度重視） |
| | `VOICE_SEARCH_RESULT_LIMIT` | `5` | `/search` の候補数（ボタンUIでは最大5件を表示） |
| **配信** | `DOWNLOAD_SIZE_THRESHOLD` | `10485760` | リンク生成に切り替えるサイズ閾値(Byte) <br>※デフォルト約10MB |
| | `DOWNLOAD_LINK_EXPIRE_HOURS` | `24` | リンクの有効期限(時間) |
| | `DOWNLOAD_LINK_MAX_COUNT` | `3` | リンクからの最大ダウンロード回数 |
| **公開** | `CLOUDFLARE_TUNNEL_ENABLED` | `false` | Cloudflare Tunnelの自動管理有効化 |
| | `UPLOAD_TOKEN` | - | **重要**: アップロード認証用トークン |

## トラブルシューティング

**Q. Botが起動しない**
- `.env` の `DISCORD_TOKEN` が正しいか確認してください。
- 必要なライブラリがインストールされているか (`uv sync`) 確認してください。

**Q. `/join` で `4017` や `Failed to connect to voice` が出る**
- `discord.py` / `davey` / `PyNaCl` の不足または古いバージョンが原因です。
- 次を実行後、Botを再起動してください：
  ```bash
  uv sync
  # もしくは
  python -m pip install -U "discord.py>=2.7.1" davey PyNaCl
  ```

**Q. `/play` で「再生開始」と出るのに音が出ない**
- Botは再生前にストリーム先頭フレームを検証し、取得できない場合はエラー通知します。
- YouTube制限の回避として、PO Token付きプロファイル → `android,web` プロファイルへ自動フォールバックします。
- 続く場合は `YOUTUBE_PO_TOKEN` を更新し、`uv sync` 後にBotを再起動してください。

**Q. ダウンロードに失敗する (YouTube/Spotify)**
- `yt-dlp` は頻繁に更新が必要です。エラーが続く場合は更新を試してください：
  ```bash
  uv run pip install --upgrade yt-dlp spotdl
  ```
- `ffmpeg` がインストールされていない、またはPATHが通っていない可能性があります。

**Q. 曲がLibraryに移動されない**
- `LIBRARY_PATH` のパス指定が正しいか（特にWindowsのドライブレターや区切り文字）確認してください。
- Botを実行しているユーザーに、そのフォルダへの書き込み権限があるか確認してください。

## 開発者向け

コードの修正や機能追加を行う場合：

```bash
# 開発用ツールのインストール
uv sync --all-extras

# コード整形 (Black)
uv run black .

# インポート順序整理 (isort)
uv run isort .

# 静的解析 (Flake8)
uv run flake8 .
```

## ディレクトリ構成

```
MusicDownloaderBot/
├── main.py              # アプリケーションのエントリーポイント
├── bot.py               # Discord Bot イベントハンドラ
├── voice_player.py      # Discordボイス再生（YouTube/キュー/自動再生）
├── config.py            # 設定値の読み込み・管理
├── queue_manager.py     # ダウンロードキューの制御
├── url_parser.py        # URL解析と振り分け
├── metadata_fetcher.py  # メタデータ取得・加工処理
├── file_server.py       # ダウンロードリンク提供用HTTPサーバー
├── downloaders/         # 各サービスのダウンロードロジック
│   ├── qobuz.py
│   ├── youtube.py
│   └── spotify.py
└── ecosystem.config.js  # PM2設定ファイル
```

## 免責事項

本ツールは技術検証および私的利用を目的として開発されています。

- 本ツールを利用して著作権法に違反する行為（権利者の許可なきアップロード、配布、販売など）を行わないでください。
- 各配信サービスの利用規約を遵守してください。アカウントの停止等の不利益について、開発者は一切の責任を負いません。
