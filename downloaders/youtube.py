"""
YouTubeダウンローダー
yt-dlpを使用して音声をダウンロードする
"""

import logging
import re
from pathlib import Path

from config import Config

from .base import BaseDownloader, DownloadResult

logger = logging.getLogger(__name__)


class YouTubeDownloader(BaseDownloader):
    """YouTube用ダウンローダー"""
    
    @property
    def service_name(self) -> str:
        return "YouTube"
    
    @property
    def folder_prefix(self) -> str:
        return Config.YOUTUBE_PREFIX
    
    async def download(self, url: str) -> DownloadResult:
        """
        YouTubeから音声をダウンロードする
        """
        logger.info(f"YouTubeダウンロード開始: {url}")

        existing_folders: set[Path] = set()
        if self.download_path.exists():
            existing_folders = {
                folder.resolve()
                for folder in self._safe_iterdir(self.download_path)
                if folder.is_dir()
            }
        
        # 出力テンプレート: アーティスト - タイトル/タイトル.拡張子
        # プレイリストの場合はプレイリスト名をフォルダ名に使用
        output_template = str(
            self.download_path
            / "%(playlist_title,channel)s - %(title)s"
            / "%(title)s.%(ext)s"
        )
        
        # クライアント設定とPO Tokenの設定
        if Config.YOUTUBE_PO_TOKEN:
            # Tokenがある場合は制限の回避を試みるためにiosを優先
            client_arg = "ios,web"
            extractor_args = [
                f"youtube:player_client={client_arg}",
                f"youtube:po_token=ios.gvs+{Config.YOUTUBE_PO_TOKEN}",
            ]
        else:
            # Tokenがない場合は、警告を減らすためにwebを試みる
            # (ejs:githubによりnシグネチャ問題を回避)
            client_arg = "web"
            extractor_args = [f"youtube:player_client={client_arg}"]
        
        cmd = [
            "yt-dlp",
            "--js-runtimes",
            "node",
            "--remote-components",
            "ejs:github",
            "--extractor-args",
            "; ".join(extractor_args),
            "--extract-audio",
            "--audio-format",
            "opus",
            "--audio-quality",
            "0",  # 最高品質
            "--embed-thumbnail",
            "--embed-metadata",
            "--output",
            output_template,
            "--no-playlist" if "list=" not in url else "--yes-playlist",
        ]
        
        # ffmpegのパスが設定されている場合は追加
        ffmpeg_path = self._get_ffmpeg_path()
        if ffmpeg_path:
            cmd.extend(["--ffmpeg-location", str(ffmpeg_path)])
        
        cmd.append(url)
        
        logger.info("yt-dlpコマンド実行中...")
        returncode, stdout, stderr = await self.run_command(cmd)
        logger.info(f"yt-dlp完了: returncode={returncode}")
        
        if returncode != 0:
            logger.error(f"yt-dlp失敗: {stderr or stdout}")
            return DownloadResult(
                success=False,
                message="ダウンロード失敗",
                error=stderr or stdout,
            )
        
        # ダウンロードされたフォルダを特定
        # yt-dlpの出力からファイルパスを抽出
        logger.info("ダウンロードフォルダ特定中...")
        combined_output = "\n".join(part for part in (stdout, stderr) if part)
        downloaded_folder = self._find_downloaded_folder(
            combined_output,
            existing_folders=existing_folders,
        )
        logger.info(f"特定されたフォルダ: {downloaded_folder}")
        
        if downloaded_folder and downloaded_folder.exists():
            # cover.jpgを生成
            logger.info(f"cover.jpg生成開始: {downloaded_folder}")
            await self._generate_cover(url, downloaded_folder)
            
            logger.info("オーディオファイルカウント中...")
            file_count = self.count_audio_files(downloaded_folder)
            logger.info(f"ファイル数: {file_count}")
            
            logger.info(f"ライブラリへ移動中: {downloaded_folder} -> {self.library_path}")
            dest = self.move_to_library(downloaded_folder, add_prefix=True)
            logger.info(f"移動完了: {dest}")
            
            return DownloadResult(
                success=True,
                message=f"ダウンロード完了: {dest.name}",
                folder_path=dest,
                file_count=file_count,
            )
        
        logger.warning(f"フォルダ特定不可: downloaded_folder={downloaded_folder}")
        return DownloadResult(
            success=True,
            message="ダウンロード完了（フォルダ特定不可）",
        )
    
    async def _generate_cover(self, url: str, folder: Path) -> None:
        """サムネイルをcover.jpgとして保存"""
        cover_path = folder / "cover.jpg"
        
        # 既にcover.jpgがあればスキップ
        if cover_path.exists():
            logger.info(f"cover.jpg既存、スキップ: {cover_path}")
            return
        
        try:
            # yt-dlpでサムネイルのみを取得
            if Config.YOUTUBE_PO_TOKEN:
                client_arg = "ios,web"
                extractor_args = [
                    f"youtube:player_client={client_arg}",
                    f"youtube:po_token=ios.gvs+{Config.YOUTUBE_PO_TOKEN}",
                ]
            else:
                client_arg = "web"
                extractor_args = [f"youtube:player_client={client_arg}"]

            cmd = [
                "yt-dlp",
                "--js-runtimes",
                "node",
                "--remote-components",
                "ejs:github",
                "--extractor-args",
                "; ".join(extractor_args),
                "--skip-download",
                "--write-thumbnail",
                "--convert-thumbnails",
                "jpg",
                "--output",
                str(folder / "cover"),
                "--no-playlist",
            ]

            ffmpeg_path = self._get_ffmpeg_path()
            if ffmpeg_path:
                cmd.extend(["--ffmpeg-location", str(ffmpeg_path)])
            
            cmd.append(url)
            
            logger.info("サムネイル取得コマンド実行中...")
            returncode, stdout, stderr = await self.run_command(cmd)
            
            if returncode == 0:
                # yt-dlpは cover.jpg.jpg のようなファイル名になることがある
                for file in folder.glob("cover*.jpg"):
                    if file.name != "cover.jpg":
                        file.rename(cover_path)
                        break
                # webp等が生成された場合のリネーム
                for file in folder.glob("cover*.webp"):
                    file.rename(cover_path)
                    break
                    
                logger.info(f"cover.jpg生成完了: {cover_path}")
            else:
                logger.warning(f"サムネイル取得失敗: {stderr}")
        except Exception as e:
            logger.exception(f"cover.jpg生成中にエラー: {e}")
    
    def _find_downloaded_folder(
        self,
        output: str,
        existing_folders: set[Path] | None = None,
    ) -> Path | None:
        """yt-dlpの出力からダウンロードされたフォルダを特定"""
        # [download]/[ExtractAudio] Destination: パス形式の行を探す
        pattern = r"\[(?:download|ExtractAudio)\] Destination: (.+)"
        matches = re.findall(pattern, output)
        
        if matches:
            for raw_path in matches:
                file_path = Path(raw_path.strip().strip('"'))
                folder = file_path.parent if file_path.suffix else file_path
                logger.info(f"出力から検出したパス: {file_path}")
                if folder.exists() and folder.is_dir():
                    return folder

        if existing_folders is not None:
            current_folders = [
                folder
                for folder in self._safe_iterdir(self.download_path)
                if folder.is_dir()
            ]
            new_folders = [
                folder
                for folder in current_folders
                if folder.resolve() not in existing_folders
            ]
            if new_folders:
                return max(new_folders, key=self._folder_mtime)
        
        return None

    @staticmethod
    def _folder_mtime(folder: Path) -> float:
        try:
            return folder.stat().st_mtime
        except OSError:
            return 0.0
