"""
設定管理モジュール
環境変数から各種設定を読み込む
"""

import os
from pathlib import Path
from typing import Optional
from dotenv import load_dotenv

load_dotenv()


class Config:
    """アプリケーション設定"""
    
    # Discord Bot設定
    DISCORD_TOKEN: str = os.getenv("DISCORD_TOKEN", "")
    
    # Qobuz認証情報
    QOBUZ_EMAIL: str = os.getenv("QOBUZ_EMAIL", "")
    QOBUZ_PASSWORD: str = os.getenv("QOBUZ_PASSWORD", "")
    
    # ダウンロード先パス
    DOWNLOAD_PATH: Path = Path(os.getenv("DOWNLOAD_PATH", "./downloads"))
    LIBRARY_PATH: Path = Path(os.getenv("LIBRARY_PATH", "./library"))
    
    # 外部ツールパス
    FFMPEG_PATH: Optional[str] = os.getenv("FFMPEG_PATH")
    
    # ダウンロード設定
    MAX_RETRIES: int = int(os.getenv("MAX_RETRIES", "3"))
    QUEUE_MAX_SIZE: int = int(os.getenv("QUEUE_MAX_SIZE", "100"))
    
    # YouTube設定
    YOUTUBE_FORMAT: str = "opus"  # opus固定
    YOUTUBE_PO_TOKEN: Optional[str] = os.getenv("YOUTUBE_PO_TOKEN")
    VOICE_TARGET_BITRATE_KBPS: int = int(os.getenv("VOICE_TARGET_BITRATE_KBPS", "128"))
    VOICE_STREAM_CACHE_SECONDS: int = int(os.getenv("VOICE_STREAM_CACHE_SECONDS", "180"))
    VOICE_SEARCH_RESULT_LIMIT: int = int(os.getenv("VOICE_SEARCH_RESULT_LIMIT", "5"))
    
    # 音声形式設定
    SPOTIFY_FORMAT: str = "opus"  # opus固定
    
    # フォルダ接頭辞
    YOUTUBE_PREFIX: str = "[YT] "
    SPOTIFY_PREFIX: str = "[SP] "
    
    # ファイル配信サーバー設定
    FILE_SERVER_PORT: int = int(os.getenv("FILE_SERVER_PORT", "8080"))
    FILE_SERVER_BASE_URL: str = os.getenv("FILE_SERVER_BASE_URL", "")
    DOWNLOAD_SIZE_THRESHOLD: int = int(os.getenv("DOWNLOAD_SIZE_THRESHOLD", "10485760"))  # 10MB
    DOWNLOAD_LINK_MAX_COUNT: int = int(os.getenv("DOWNLOAD_LINK_MAX_COUNT", "3"))
    DOWNLOAD_LINK_EXPIRE_HOURS: int = int(os.getenv("DOWNLOAD_LINK_EXPIRE_HOURS", "24"))

    # アップロード設定
    UPLOAD_PATH: Path = Path(os.getenv("UPLOAD_PATH", "./uploads"))
    UPLOAD_MAX_SIZE: int = int(os.getenv("UPLOAD_MAX_SIZE", "1073741824"))  # 1GB
    UPLOAD_TOKEN: str = os.getenv("UPLOAD_TOKEN", "")

    # Cloudflare Tunnel設定
    CLOUDFLARE_TUNNEL_ENABLED: bool = os.getenv("CLOUDFLARE_TUNNEL_ENABLED", "").lower() in ("true", "1", "yes")
    CLOUDFLARE_TUNNEL_MODE: str = os.getenv("CLOUDFLARE_TUNNEL_MODE", "quick")  # quick or named
    CLOUDFLARE_TUNNEL_NAME: str = os.getenv("CLOUDFLARE_TUNNEL_NAME", "")
    CLOUDFLARE_CONFIG_PATH: str = os.getenv("CLOUDFLARE_CONFIG_PATH", "")
    CLOUDFLARED_PATH: str = os.getenv("CLOUDFLARED_PATH", "")
    
    @classmethod
    def validate(cls) -> list[str]:
        """設定の検証を行い、エラーメッセージのリストを返す"""
        errors = []
        
        if not cls.DISCORD_TOKEN:
            errors.append("DISCORD_TOKENが設定されていない")
        
        if not cls.QOBUZ_EMAIL or not cls.QOBUZ_PASSWORD:
            errors.append("Qobuz認証情報が不完全（QOBUZ_EMAIL, QOBUZ_PASSWORD）")
        
        return errors
    
    @classmethod
    def ensure_directories(cls) -> None:
        """必要なディレクトリを作成"""
        cls.DOWNLOAD_PATH.mkdir(parents=True, exist_ok=True)
        cls.LIBRARY_PATH.mkdir(parents=True, exist_ok=True)
        cls.UPLOAD_PATH.mkdir(parents=True, exist_ok=True)
