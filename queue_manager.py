"""
ダウンロードキュー管理モジュール
非同期キューでダウンロードを順番に処理する
"""

import asyncio
import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import Enum, auto
from typing import Awaitable, Callable, Optional

from url_parser import ServiceType, URLParser
from downloaders import (
    BaseDownloader,
    DownloadResult,
    QobuzDownloader,
    YouTubeDownloader,
    SpotifyDownloader,
)
from config import Config


class TaskStatus(Enum):
    """タスク状態"""
    PENDING = auto()   # 待機中
    RUNNING = auto()   # 実行中
    COMPLETED = auto() # 完了
    FAILED = auto()    # 失敗


@dataclass
class DownloadTask:
    """ダウンロードタスク"""
    id: str
    url: str
    service: ServiceType
    requester_id: int
    channel_id: int
    status: TaskStatus = TaskStatus.PENDING
    result: Optional[DownloadResult] = None
    created_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    message_id: Optional[int] = None  # プレビューメッセージのID（更新用）
    
    def __post_init__(self):
        if self.created_at is None:
            self.created_at = datetime.now()


# 進捗通知用のコールバック型
ProgressCallback = Callable[[DownloadTask], Awaitable[None]]


class QueueManager:
    """ダウンロードキュー管理"""
    
    def __init__(self):
        self._queue: asyncio.Queue[DownloadTask] = asyncio.Queue(
            maxsize=Config.QUEUE_MAX_SIZE
        )
        self._current_task: Optional[DownloadTask] = None
        self._pending_tasks: list[DownloadTask] = []
        self._history: list[DownloadTask] = []
        self._worker_task: Optional[asyncio.Task] = None
        self._progress_callback: Optional[ProgressCallback] = None
        
        # ダウンローダーの初期化
        Config.ensure_directories()
        self._downloaders: dict[ServiceType, BaseDownloader] = {
            ServiceType.QOBUZ: QobuzDownloader(
                Config.DOWNLOAD_PATH, Config.LIBRARY_PATH
            ),
            ServiceType.YOUTUBE: YouTubeDownloader(
                Config.DOWNLOAD_PATH, Config.LIBRARY_PATH
            ),
            ServiceType.SPOTIFY: SpotifyDownloader(
                Config.DOWNLOAD_PATH, Config.LIBRARY_PATH
            ),
        }
    
    def set_progress_callback(self, callback: ProgressCallback) -> None:
        """進捗通知コールバックを設定"""
        self._progress_callback = callback
    
    async def add_task(
        self,
        url: str,
        requester_id: int,
        channel_id: int,
        message_id: Optional[int] = None,
    ) -> tuple[bool, str, Optional[DownloadTask]]:
        """
        ダウンロードタスクをキューに追加
        
        Returns:
            tuple: (成功したか, メッセージ, タスク)
        """
        service = URLParser.parse(url)
        
        if service == ServiceType.UNKNOWN:
            return False, "対応していないURLです", None
        
        if self._queue.full():
            return False, "キューが満杯です。しばらく待ってから再試行してください", None
        
        task = DownloadTask(
            id=str(uuid.uuid4()),
            url=url,
            service=service,
            requester_id=requester_id,
            channel_id=channel_id,
            message_id=message_id,
        )
        
        self._pending_tasks.append(task)
        await self._queue.put(task)
        position = self._queue.qsize()
        
        service_name = URLParser.get_service_name(service)
        return True, f"キューに追加しました（{service_name}、順番: {position}）", task
    
    async def start_worker(self) -> None:
        """ワーカータスクを開始"""
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.create_task(self._worker())
    
    async def stop_worker(self) -> None:
        """ワーカータスクを停止"""
        if self._worker_task and not self._worker_task.done():
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
    
    async def _worker(self) -> None:
        """キューからタスクを取り出して処理するワーカー"""
        while True:
            task = await self._queue.get()
            self._current_task = task
            
            # 待機リストから削除
            if task in self._pending_tasks:
                self._pending_tasks.remove(task)
            
            task.status = TaskStatus.RUNNING
            task.started_at = datetime.now()
            
            # 開始通知
            if self._progress_callback:
                await self._progress_callback(task)
            
            try:
                downloader = self._downloaders.get(task.service)
                if downloader:
                    task.result = await downloader.download(task.url)
                    task.status = (
                        TaskStatus.COMPLETED if task.result.success
                        else TaskStatus.FAILED
                    )
                else:
                    task.result = DownloadResult(
                        success=False,
                        message="対応するダウンローダーがありません",
                    )
                    task.status = TaskStatus.FAILED
            except Exception as e:
                task.result = DownloadResult(
                    success=False,
                    message="エラーが発生しました",
                    error=str(e),
                )
                task.status = TaskStatus.FAILED
            
            task.completed_at = datetime.now()
            self._current_task = None
            self._history.append(task)
            
            # 完了通知
            if self._progress_callback:
                await self._progress_callback(task)
            
            self._queue.task_done()
    
    @property
    def queue_size(self) -> int:
        """キュー内のタスク数"""
        return self._queue.qsize()
    
    @property
    def current_task(self) -> Optional[DownloadTask]:
        """現在実行中のタスク"""
        return self._current_task
    
    def get_queue_status(self) -> str:
        """キュー状態の文字列を返す"""
        lines = []
        
        if self._current_task:
            service = URLParser.get_service_name(self._current_task.service)
            lines.append(f"🔄 実行中: {service} (ID: {self._current_task.id[:8]})")
        
        lines.append(f"📋 待機中: {self.queue_size}件")
        
        return "\n".join(lines)
    
    def get_queue_info(self) -> tuple[list[DownloadTask], Optional[DownloadTask]]:
        """キュー情報を取得（待機中タスクリスト、現在実行中タスク）"""
        return self._pending_tasks.copy(), self._current_task
