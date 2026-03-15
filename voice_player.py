"""
Discordボイス再生管理モジュール
YouTubeストリーミング再生、キュー、自動再生を管理する
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import deque
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Optional

import discord
from discord.ext import commands

from command_utils import resolve_command
from config import Config
from url_parser import ServiceType, URLParser

logger = logging.getLogger(__name__)

MAX_SEARCH_RESULTS = 10


@dataclass(slots=True)
class StreamTrack:
    """再生対象トラック情報"""

    title: str
    webpage_url: str
    stream_url: str
    requester_id: int
    duration: Optional[int] = None
    video_id: Optional[str] = None
    thumbnail_url: Optional[str] = None


@dataclass(slots=True)
class EnqueueResult:
    """キュー投入結果"""

    track: StreamTrack
    started: bool
    position: int = 0


@dataclass(slots=True)
class SearchResult:
    """YouTube検索結果"""

    title: str
    webpage_url: str
    duration: Optional[int] = None
    video_id: Optional[str] = None
    thumbnail_url: Optional[str] = None


@dataclass(slots=True)
class StreamCacheEntry:
    """ストリームURLキャッシュ"""

    track: StreamTrack
    expires_at: float


@dataclass(slots=True)
class PlaybackSnapshot:
    """プレイヤー状態のスナップショット"""

    connected: bool
    playing: bool
    paused: bool
    autoplay: bool
    volume_percent: int
    queue_length: int
    current_title: Optional[str] = None
    current_duration: Optional[int] = None
    current_elapsed: Optional[int] = None
    current_thumbnail_url: Optional[str] = None


@dataclass
class GuildVoiceState:
    """ギルド単位のボイス状態"""

    voice_client: Optional[discord.VoiceClient] = None
    queue: deque[StreamTrack] = field(default_factory=deque)
    current: Optional[StreamTrack] = None
    autoplay: bool = False
    volume: float = 1.0
    announcement_channel_id: Optional[int] = None
    suppress_after_once: bool = False
    played_video_ids: deque[str] = field(default_factory=lambda: deque(maxlen=100))
    current_source: Optional[discord.PCMVolumeTransformer] = None
    current_seek_seconds: float = 0.0
    current_started_at_monotonic: Optional[float] = None
    last_finished_track: Optional[StreamTrack] = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class PrefetchedAudioSource(discord.AudioSource):
    """先読み済みフレームを返すAudioSourceラッパー"""

    def __init__(self, source: discord.AudioSource, first_frame: bytes):
        self._source = source
        self._first_frame = first_frame
        self._first_served = False

    def read(self) -> bytes:
        if not self._first_served:
            self._first_served = True
            return self._first_frame
        return self._source.read()

    def is_opus(self) -> bool:
        return self._source.is_opus()

    def cleanup(self) -> None:
        self._source.cleanup()


class VoicePlaybackManager:
    """YouTube音声再生を管理するクラス"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._states: dict[int, GuildVoiceState] = {}
        self._stream_cache: dict[str, StreamCacheEntry] = {}

    def _get_state(self, guild_id: int) -> GuildVoiceState:
        state = self._states.get(guild_id)
        if state is None:
            state = GuildVoiceState()
            self._states[guild_id] = state
        return state

    async def shutdown(self) -> None:
        """全ギルドの再生を停止して切断する"""

        guild_ids = list(self._states.keys())
        for guild_id in guild_ids:
            await self.leave(guild_id)

    async def connect(
        self,
        guild: discord.Guild,
        channel: discord.VoiceChannel | discord.StageChannel,
    ) -> discord.VoiceClient:
        """指定チャンネルへ接続（必要なら移動）"""

        state = self._get_state(guild.id)
        async with state.lock:
            voice_client = guild.voice_client
            if voice_client and voice_client.is_connected():
                if voice_client.channel != channel:
                    await voice_client.move_to(channel)
            else:
                try:
                    voice_client = await channel.connect(reconnect=True, self_deaf=True)
                except RuntimeError as error:
                    detail = str(error)
                    if "PyNaCl library needed" in detail:
                        raise RuntimeError(
                            "PyNaCl が未インストールです。`uv sync` または "
                            "`python -m pip install PyNaCl` を実行後、Botを再起動してください。"
                        ) from error
                    if "davey library needed" in detail:
                        raise RuntimeError(
                            "davey が未インストールです。`uv sync` または "
                            "`python -m pip install -U \"discord.py>=2.7.1\" davey` "
                            "を実行後、Botを再起動してください。"
                        ) from error
                    raise
                except TimeoutError as error:
                    raise RuntimeError(
                        "ボイス接続がタイムアウトしました。`discord.py>=2.7.1` と "
                        "`davey` が導入されているか確認してください。"
                    ) from error
                except discord.ConnectionClosed as error:
                    if error.code == 4017:
                        raise RuntimeError(
                            "Discord側でボイス接続が拒否されました (code: 4017)。"
                            "`discord.py>=2.7.1` と `davey` を導入してBotを再起動してください。"
                        ) from error
                    raise RuntimeError(
                        f"Discordボイス接続に失敗しました (code: {error.code})"
                    ) from error

            state.voice_client = voice_client
            return voice_client

    async def enqueue_url(
        self,
        guild: discord.Guild,
        url: str,
        requester_id: int,
        announcement_channel_id: Optional[int],
    ) -> EnqueueResult:
        """YouTube URLを再生またはキュー投入する"""

        track = await self._resolve_stream_track(url, requester_id=requester_id, use_cache=True)
        return await self._enqueue_track(
            guild=guild,
            track=track,
            announcement_channel_id=announcement_channel_id,
        )

    async def play_queued_track(self, guild_id: int) -> StreamTrack:
        """待機中キュー先頭を再生する（URL指定なし用）"""

        state = self._states.get(guild_id)
        if state is None:
            raise RuntimeError("キューに曲がありません。")

        async with state.lock:
            voice_client = state.voice_client
            if voice_client is None or not voice_client.is_connected():
                raise RuntimeError("ボイス接続がありません。先に `/join` を実行してください。")

            if voice_client.is_playing() or voice_client.is_paused() or state.current is not None:
                raise RuntimeError("すでに再生中です。")

            if not state.queue:
                raise RuntimeError("キューに曲がありません。")

            next_track = state.queue.popleft()
            await self._play_track_locked(guild_id, state, next_track)
            return next_track

    async def get_queue_tracks(
        self,
        guild_id: int,
        limit: int = 5,
    ) -> tuple[Optional[StreamTrack], list[StreamTrack]]:
        """現在曲と待機キュー（先頭から）を取得"""

        state = self._states.get(guild_id)
        if state is None:
            return None, []

        normalized_limit = max(0, min(limit, 20))
        async with state.lock:
            current = state.current
            queued = list(state.queue)
            if normalized_limit:
                queued = queued[:normalized_limit]
            else:
                queued = []
            return current, queued

    async def _enqueue_track(
        self,
        guild: discord.Guild,
        track: StreamTrack,
        announcement_channel_id: Optional[int],
    ) -> EnqueueResult:
        state = self._get_state(guild.id)

        if announcement_channel_id:
            state.announcement_channel_id = announcement_channel_id

        async with state.lock:
            voice_client = guild.voice_client or state.voice_client
            if not voice_client or not voice_client.is_connected():
                raise RuntimeError("先に `/join` でボイスチャンネルへ接続してください。")

            state.voice_client = voice_client
            is_busy = (
                voice_client.is_playing()
                or voice_client.is_paused()
                or state.current is not None
            )
            if is_busy:
                state.queue.append(track)
                return EnqueueResult(track=track, started=False, position=len(state.queue))

            await self._play_track_locked(guild.id, state, track)
            return EnqueueResult(track=track, started=True)

    async def search(self, query: str, limit: int = 5) -> list[SearchResult]:
        """YouTube検索結果を返す"""

        limit = max(1, min(limit, MAX_SEARCH_RESULTS))
        data: Optional[dict] = None
        last_error = "yt-dlp search failed"
        for profile_name, base_cmd in self._build_ytdlp_base_commands():
            cmd = base_cmd + [
                "--dump-single-json",
                "--skip-download",
                "--no-warnings",
                "--extractor-retries",
                "1",
                f"ytsearch{limit}:{query}",
            ]
            timeout = self._resolve_profile_timeout(profile_name, search_mode=True)
            returncode, stdout, stderr = await self._run_command(cmd, timeout=timeout)
            if returncode != 0:
                last_error = stderr or stdout or "yt-dlp search failed"
                logger.debug(
                    "YouTube検索失敗: profile=%s, timeout=%ss, error=%s",
                    profile_name,
                    timeout,
                    last_error,
                )
                continue
            try:
                data = self._parse_json_output(stdout)
                break
            except RuntimeError as parse_error:
                last_error = str(parse_error)
                logger.debug(
                    "YouTube検索JSON解析失敗: profile=%s, error=%s",
                    profile_name,
                    parse_error,
                )
                continue

        if data is None:
            raise RuntimeError(f"YouTube検索に失敗しました: {last_error}")

        entries: list[dict] = []
        if isinstance(data, dict) and isinstance(data.get("entries"), list):
            entries = [entry for entry in data["entries"] if isinstance(entry, dict)]
        elif isinstance(data, dict):
            entries = [data]

        results: list[SearchResult] = []
        for entry in entries:
            title = entry.get("title")
            if not title:
                continue

            webpage_url = entry.get("webpage_url")
            video_id = entry.get("id")
            if not webpage_url and video_id:
                webpage_url = f"https://www.youtube.com/watch?v={video_id}"
            if not webpage_url:
                continue

            results.append(
                SearchResult(
                    title=title,
                    webpage_url=webpage_url,
                    duration=entry.get("duration"),
                    video_id=video_id,
                    thumbnail_url=entry.get("thumbnail"),
                )
            )
        return results

    async def stop(self, guild_id: int, clear_queue: bool = True) -> tuple[bool, int]:
        """再生停止。必要に応じてキューもクリア"""

        state = self._states.get(guild_id)
        if state is None:
            return False, 0

        async with state.lock:
            cleared = len(state.queue) if clear_queue else 0
            if clear_queue:
                state.queue.clear()

            voice_client = state.voice_client
            if not voice_client:
                return False, cleared

            state.current = None
            state.current_source = None
            state.current_seek_seconds = 0.0
            state.current_started_at_monotonic = None
            if voice_client.is_playing() or voice_client.is_paused():
                state.suppress_after_once = True
                voice_client.stop()
                return True, cleared
            return False, cleared

    async def leave(self, guild_id: int) -> bool:
        """ボイスチャンネルから切断し、状態をクリア"""

        state = self._states.get(guild_id)
        if state is None:
            return False

        async with state.lock:
            state.queue.clear()
            state.current = None
            state.current_source = None
            state.current_seek_seconds = 0.0
            state.current_started_at_monotonic = None
            voice_client = state.voice_client
            state.voice_client = None
            state.announcement_channel_id = None

            if not voice_client or not voice_client.is_connected():
                return False

            if voice_client.is_playing() or voice_client.is_paused():
                state.suppress_after_once = True
                voice_client.stop()
            await voice_client.disconnect(force=True)
            return True

    async def set_autoplay(self, guild_id: int, enabled: bool) -> None:
        """自動再生ON/OFFを設定"""

        state = self._get_state(guild_id)
        async with state.lock:
            state.autoplay = enabled
            if not enabled:
                return

            voice_client = state.voice_client
            if (
                voice_client
                and voice_client.is_connected()
                and state.current is None
                and state.queue
                and not voice_client.is_playing()
                and not voice_client.is_paused()
            ):
                next_track = state.queue.popleft()
                await self._play_track_locked(guild_id, state, next_track)

    def is_autoplay_enabled(self, guild_id: int) -> bool:
        """自動再生状態を取得"""

        state = self._states.get(guild_id)
        return state.autoplay if state else False

    async def get_snapshot(self, guild_id: int) -> PlaybackSnapshot:
        """現在のプレイヤー状態を取得"""

        state = self._states.get(guild_id)
        if state is None:
            return PlaybackSnapshot(
                connected=False,
                playing=False,
                paused=False,
                autoplay=False,
                volume_percent=100,
                queue_length=0,
            )

        async with state.lock:
            voice_client = state.voice_client
            connected = bool(voice_client and voice_client.is_connected())
            playing = bool(voice_client and voice_client.is_playing())
            paused = bool(voice_client and voice_client.is_paused())
            current_elapsed = (
                int(self._current_elapsed_seconds(state))
                if state.current is not None
                else None
            )
            current_duration = state.current.duration if state.current else None
            if current_duration is not None and current_elapsed is not None:
                current_elapsed = min(current_elapsed, current_duration)

            return PlaybackSnapshot(
                connected=connected,
                playing=playing,
                paused=paused,
                autoplay=state.autoplay,
                volume_percent=int(round(state.volume * 100)),
                queue_length=len(state.queue),
                current_title=state.current.title if state.current else None,
                current_duration=current_duration,
                current_elapsed=current_elapsed,
                current_thumbnail_url=state.current.thumbnail_url if state.current else None,
            )

    async def set_volume(self, guild_id: int, volume_percent: int) -> int:
        """音量を設定して現在値(%)を返す"""

        state = self._states.get(guild_id)
        if state is None:
            raise RuntimeError("プレイヤーが初期化されていません。先に `/join` を実行してください。")

        async with state.lock:
            return self._set_volume_no_lock(state, volume_percent)

    async def adjust_volume(self, guild_id: int, delta_percent: int) -> int:
        """音量を増減して現在値(%)を返す"""

        state = self._states.get(guild_id)
        if state is None:
            raise RuntimeError("プレイヤーが初期化されていません。先に `/join` を実行してください。")

        async with state.lock:
            new_percent = int(round(state.volume * 100)) + delta_percent
            return self._set_volume_no_lock(state, new_percent)

    @staticmethod
    def _set_volume_no_lock(state: GuildVoiceState, volume_percent: int) -> int:
        """ロック取得済み前提で音量を設定し、現在値(%)を返す"""

        clamped = max(0, min(200, volume_percent))
        volume_ratio = clamped / 100.0
        state.volume = volume_ratio
        if state.current_source is not None:
            state.current_source.volume = volume_ratio
        voice_client = state.voice_client
        if (
            voice_client is not None
            and isinstance(voice_client.source, discord.PCMVolumeTransformer)
        ):
            voice_client.source.volume = volume_ratio
            state.current_source = voice_client.source
        return clamped

    async def toggle_pause_resume(self, guild_id: int) -> str:
        """再生/一時停止を切り替え（paused/resumed/idle を返す）"""

        state = self._states.get(guild_id)
        if state is None:
            raise RuntimeError("プレイヤーが初期化されていません。先に `/join` を実行してください。")

        async with state.lock:
            voice_client = state.voice_client
            if voice_client is None or not voice_client.is_connected():
                raise RuntimeError("ボイス接続がありません。先に `/join` を実行してください。")

            if voice_client.is_paused():
                voice_client.resume()
                state.current_started_at_monotonic = time.monotonic()
                return "resumed"

            if voice_client.is_playing():
                voice_client.pause()
                state.current_seek_seconds = self._current_elapsed_seconds(state)
                state.current_started_at_monotonic = None
                return "paused"

            if state.current is None:
                return "idle"

            await self._play_track_locked(
                guild_id,
                state,
                state.current,
                seek_seconds=state.current_seek_seconds,
            )
            return "resumed"

    async def rewind(self, guild_id: int, seconds: int = 10) -> int:
        """現在曲を巻き戻す（秒）"""
        target = 0
        state = self._states.get(guild_id)
        if state is None:
            raise RuntimeError("再生中の曲がありません。")

        async with state.lock:
            if state.current is None:
                raise RuntimeError("再生中の曲がありません。")
            current_elapsed = self._current_elapsed_seconds(state)
            target = max(0, int(current_elapsed) - max(1, seconds))
            await self._seek_locked(guild_id, state, target)
        return target

    async def seek(self, guild_id: int, seconds: int) -> int:
        """現在曲を指定秒へシークする"""
        target = max(0, int(seconds))
        state = self._states.get(guild_id)
        if state is None:
            raise RuntimeError("再生中の曲がありません。")

        async with state.lock:
            if state.current is None:
                raise RuntimeError("再生中の曲がありません。")
            if state.current.duration is not None:
                target = min(target, max(0, state.current.duration - 1))
            await self._seek_locked(guild_id, state, target)
        return target

    async def replay(self, guild_id: int) -> StreamTrack:
        """現在または直前の曲を先頭から再生"""

        state = self._states.get(guild_id)
        if state is None:
            raise RuntimeError("再生履歴がありません。")

        async with state.lock:
            voice_client = state.voice_client
            if voice_client is None or not voice_client.is_connected():
                raise RuntimeError("ボイス接続がありません。先に `/join` を実行してください。")

            target_track = state.current or state.last_finished_track
            if target_track is None:
                raise RuntimeError("再再生できる曲がありません。")

            state.suppress_after_once = True
            if voice_client.is_playing() or voice_client.is_paused():
                voice_client.stop()

            await self._play_track_locked(
                guild_id,
                state,
                target_track,
                seek_seconds=0.0,
            )
            return target_track

    async def _play_track_locked(
        self,
        guild_id: int,
        state: GuildVoiceState,
        track: StreamTrack,
        seek_seconds: float = 0.0,
    ) -> None:
        """ロック内でトラック再生を開始"""

        voice_client = state.voice_client
        if voice_client is None or not voice_client.is_connected():
            raise RuntimeError("ボイス接続が切断されています。`/join` を実行してください。")

        prepared_track = track
        source: discord.AudioSource
        try:
            source = self._create_audio_source(prepared_track, seek_seconds=seek_seconds)
        except Exception as source_error:
            logger.warning(
                "音源初期化に失敗したためURLを再取得します: %s",
                source_error,
            )
            prepared_track = await self._resolve_stream_track(
                prepared_track.webpage_url,
                requester_id=prepared_track.requester_id,
                use_cache=False,
            )
            source = self._create_audio_source(prepared_track, seek_seconds=seek_seconds)

        transformed_source = discord.PCMVolumeTransformer(
            source,
            volume=state.volume,
        )

        state.current = prepared_track
        state.current_source = transformed_source
        state.current_seek_seconds = max(0.0, float(seek_seconds))
        state.current_started_at_monotonic = time.monotonic()
        if prepared_track.video_id:
            state.played_video_ids.append(prepared_track.video_id)

        voice_client.play(
            transformed_source,
            after=lambda error: self._schedule_after_play(guild_id, error),
        )

    def _create_audio_source(
        self,
        track: StreamTrack,
        seek_seconds: float = 0.0,
    ) -> discord.AudioSource:
        """FFmpeg音源を作成"""
        self._ensure_opus_loaded()
        executable = self._get_ffmpeg_executable()
        before_parts = [
            "-nostdin",
            "-reconnect 1",
            "-reconnect_streamed 1",
            "-reconnect_delay_max 5",
        ]
        if seek_seconds > 0:
            before_parts.append(f"-ss {seek_seconds:.3f}")
        before_options = " ".join(before_parts)
        options = "-vn -loglevel warning"

        source = discord.FFmpegPCMAudio(
            track.stream_url,
            executable=executable,
            before_options=before_options,
            options=options,
        )
        first_frame = source.read()
        if not first_frame:
            source.cleanup()
            raise RuntimeError(
                "音声ストリームの取得に失敗しました。"
                "YouTube側制限または PO Token 不整合の可能性があります。"
            )
        return PrefetchedAudioSource(source, first_frame)

    @staticmethod
    def _ensure_opus_loaded() -> None:
        if discord.opus.is_loaded():
            return

        try:
            discord.opus._load_default()
        except Exception as error:
            raise RuntimeError(
                "Opusライブラリを読み込めませんでした。"
                "voice機能を使うにはOpusが必要です。"
            ) from error

        if not discord.opus.is_loaded():
            raise RuntimeError(
                "Opusライブラリを読み込めませんでした。"
                "環境にOpus DLLがあるか確認してください。"
            )

    def _schedule_after_play(self, guild_id: int, error: Optional[Exception]) -> None:
        future = asyncio.run_coroutine_threadsafe(
            self._on_track_end(guild_id, error),
            self.bot.loop,
        )
        future.add_done_callback(self._handle_after_future)

    @staticmethod
    def _handle_after_future(future: asyncio.Future) -> None:
        try:
            future.result()
        except Exception:
            logger.exception("再生終了後処理でエラーが発生しました")

    async def _on_track_end(self, guild_id: int, error: Optional[Exception]) -> None:
        state = self._states.get(guild_id)
        if state is None:
            return

        announce_now_playing = False
        next_track: Optional[StreamTrack] = None
        post_error_message: Optional[str] = None
        autoplay_started = False

        async with state.lock:
            if state.suppress_after_once:
                state.suppress_after_once = False
                return

            previous_track = state.current
            previous_started_at = state.current_started_at_monotonic
            if previous_track is not None:
                state.last_finished_track = previous_track
            state.current = None
            state.current_source = None
            state.current_started_at_monotonic = None
            state.current_seek_seconds = 0.0

            if error:
                post_error_message = f"⚠️ 再生中にエラーが発生しました: `{error}`"

            if state.autoplay:
                if state.queue:
                    next_track = state.queue.popleft()
                elif previous_track is not None:
                    next_track = await self._resolve_autoplay_track(previous_track, state)
                    autoplay_started = next_track is not None

            if next_track is not None:
                try:
                    await self._play_track_locked(guild_id, state, next_track)
                    announce_now_playing = True
                except (RuntimeError, discord.ClientException) as play_error:
                    post_error_message = f"⚠️ 次の曲の再生に失敗しました: {play_error}"
                    next_track = None
            elif (
                previous_track is not None
                and previous_started_at is not None
                and previous_track.duration
                and previous_track.duration >= 10
            ):
                elapsed = time.monotonic() - previous_started_at
                if elapsed < 2.5:
                    post_error_message = (
                        "⚠️ 再生を開始しましたが、音声ストリームがすぐ終了しました。"
                        "YouTube側制限や PO Token 不整合の可能性があります。"
                    )

        if post_error_message:
            await self._announce(state, post_error_message)

        if announce_now_playing and next_track:
            prefix = "🔁 自動再生" if autoplay_started else "▶️ 次を再生"
            await self._announce(state, f"{prefix}: **{next_track.title}**")

    @staticmethod
    def _current_elapsed_seconds(state: GuildVoiceState) -> float:
        elapsed = state.current_seek_seconds
        if state.current_started_at_monotonic is not None:
            elapsed += max(0.0, time.monotonic() - state.current_started_at_monotonic)
        return max(0.0, elapsed)

    async def _seek_locked(
        self,
        guild_id: int,
        state: GuildVoiceState,
        target_seconds: int,
    ) -> None:
        """ロック内で現在曲をシークして再生し直す"""

        voice_client = state.voice_client
        if voice_client is None or not voice_client.is_connected():
            raise RuntimeError("ボイス接続がありません。先に `/join` を実行してください。")
        if state.current is None:
            raise RuntimeError("再生中の曲がありません。")

        state.suppress_after_once = True
        if voice_client.is_playing() or voice_client.is_paused():
            voice_client.stop()

        await self._play_track_locked(
            guild_id,
            state,
            state.current,
            seek_seconds=float(target_seconds),
        )

    async def _resolve_autoplay_track(
        self,
        base_track: StreamTrack,
        state: GuildVoiceState,
    ) -> Optional[StreamTrack]:
        """再生履歴を避けながら自動再生候補を解決"""

        search_limit = max(3, min(MAX_SEARCH_RESULTS, Config.VOICE_SEARCH_RESULT_LIMIT + 2))
        query = f"{base_track.title} audio"
        try:
            candidates = await self.search(query, limit=search_limit)
        except RuntimeError as search_error:
            await self._announce(
                state,
                f"⚠️ 自動再生候補の検索に失敗しました: {search_error}",
            )
            return None

        for candidate in candidates:
            if candidate.video_id and candidate.video_id in state.played_video_ids:
                continue
            try:
                return await self._resolve_stream_track(
                    candidate.webpage_url,
                    requester_id=0,
                    use_cache=True,
                )
            except RuntimeError:
                continue
        return None

    async def _announce(self, state: GuildVoiceState, message: str) -> None:
        """通知チャンネルへメッセージを送信"""

        channel_id = state.announcement_channel_id
        if channel_id is None:
            return

        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except (discord.NotFound, discord.Forbidden):
                return
            except discord.HTTPException:
                return

        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return

        try:
            await channel.send(message)
        except discord.HTTPException:
            logger.warning("通知メッセージ送信に失敗しました: channel_id=%s", channel_id)

    async def _resolve_stream_track(
        self,
        url: str,
        requester_id: int,
        use_cache: bool,
    ) -> StreamTrack:
        service = URLParser.detect_service(url)
        if service != ServiceType.YOUTUBE:
            raise RuntimeError("音声再生はYouTube URLのみ対応しています。")

        normalized_url = url.strip()
        now = time.monotonic()
        self._prune_stream_cache(now)

        if use_cache:
            cached = self._stream_cache.get(normalized_url)
            if cached and cached.expires_at > now:
                return replace(cached.track, requester_id=requester_id)
            if cached:
                del self._stream_cache[normalized_url]

        target_bitrate = max(64, Config.VOICE_TARGET_BITRATE_KBPS)
        format_candidates: list[str] = [
            "bestaudio/best",
            "best",
            f"bestaudio[abr<={target_bitrate}]/bestaudio",
        ]

        data: Optional[dict] = None
        last_error = "yt-dlp failed"
        for profile_name, base_cmd in self._build_ytdlp_base_commands():
            timeout = self._resolve_profile_timeout(profile_name, search_mode=False)
            for format_selector in format_candidates:
                cmd = base_cmd + [
                    "--dump-single-json",
                    "--skip-download",
                    "--no-playlist",
                    "--no-warnings",
                    "--extractor-retries",
                    "1",
                ]
                cmd.extend(["--format", format_selector])
                cmd.append(normalized_url)

                returncode, stdout, stderr = await self._run_command(cmd, timeout=timeout)
                if returncode != 0:
                    last_error = stderr or stdout or "yt-dlp failed"
                    logger.debug(
                        (
                            "YouTubeフォーマット解決失敗: profile=%s, selector=%s, "
                            "timeout=%ss, error=%s"
                        ),
                        profile_name,
                        format_selector,
                        timeout,
                        last_error,
                    )
                    continue

                try:
                    data = self._parse_json_output(stdout)
                    break
                except RuntimeError as parse_error:
                    last_error = str(parse_error)
                    logger.debug(
                        "YouTube JSON解析失敗: profile=%s, selector=%s, error=%s",
                        profile_name,
                        format_selector,
                        parse_error,
                    )
                    continue
            if data is not None:
                break

        if data is None:
            raise RuntimeError(f"YouTube情報取得に失敗しました: {last_error}")

        if isinstance(data, dict) and isinstance(data.get("entries"), list):
            entries = [entry for entry in data["entries"] if isinstance(entry, dict)]
            if not entries:
                raise RuntimeError("再生可能な動画情報を取得できませんでした。")
            data = entries[0]

        if not isinstance(data, dict):
            raise RuntimeError("動画情報の解析に失敗しました。")

        stream_url = data.get("url")
        webpage_url = data.get("webpage_url") or normalized_url
        video_id = data.get("id")

        if not stream_url:
            if video_id:
                fallback_url = f"https://www.youtube.com/watch?v={video_id}"
                if fallback_url != normalized_url:
                    return await self._resolve_stream_track(
                        fallback_url,
                        requester_id=requester_id,
                        use_cache=False,
                    )
            raise RuntimeError("ストリームURLを取得できませんでした。")

        track = StreamTrack(
            title=data.get("title", "不明なタイトル"),
            webpage_url=webpage_url,
            stream_url=stream_url,
            requester_id=requester_id,
            duration=data.get("duration"),
            video_id=video_id,
            thumbnail_url=data.get("thumbnail"),
        )

        cache_ttl = max(30, Config.VOICE_STREAM_CACHE_SECONDS)
        cache_entry = StreamCacheEntry(
            track=replace(track, requester_id=0),
            expires_at=now + cache_ttl,
        )
        self._stream_cache[normalized_url] = cache_entry
        self._stream_cache[webpage_url] = cache_entry

        return track

    def _prune_stream_cache(self, now: Optional[float] = None) -> None:
        current = now if now is not None else time.monotonic()
        expired_keys = [
            key
            for key, entry in self._stream_cache.items()
            if entry.expires_at <= current
        ]
        for key in expired_keys:
            self._stream_cache.pop(key, None)

    @staticmethod
    def _parse_json_output(output: str) -> dict:
        """yt-dlpのJSON出力を安全に解析"""

        try:
            return json.loads(output)
        except json.JSONDecodeError:
            lines = [line.strip() for line in output.splitlines() if line.strip()]
            for line in reversed(lines):
                if not line.startswith("{"):
                    continue
                try:
                    return json.loads(line)
                except json.JSONDecodeError:
                    continue
        raise RuntimeError("yt-dlpのJSON解析に失敗しました。")

    def _build_ytdlp_base_commands(self) -> list[tuple[str, list[str]]]:
        commands: list[tuple[str, list[str]]] = []
        # まずは最小構成（node/ejs不要）を優先し、解決できない場合のみ高コスト系へフォールバック
        commands.append(
            (
                "android-web-basic",
                [
                    "yt-dlp",
                    "--extractor-args",
                    "youtube:player_client=android,web",
                ],
            )
        )
        commands.append(
            (
                "android-web-ejs",
                [
                    "yt-dlp",
                    "--js-runtimes",
                    "node",
                    "--remote-components",
                    "ejs:github",
                    "--extractor-args",
                    "youtube:player_client=android,web",
                ],
            )
        )
        if Config.YOUTUBE_PO_TOKEN:
            commands.append(
                (
                    "ios-po-token-ejs",
                    [
                        "yt-dlp",
                        "--js-runtimes",
                        "node",
                        "--remote-components",
                        "ejs:github",
                        "--extractor-args",
                        (
                            "youtube:player_client=ios,web; "
                            f"youtube:po_token=ios.gvs+{Config.YOUTUBE_PO_TOKEN}"
                        ),
                    ],
                )
            )
        return commands

    @staticmethod
    def _resolve_profile_timeout(profile_name: str, *, search_mode: bool) -> int:
        if profile_name == "android-web-basic":
            return 15 if search_mode else 20
        return 30 if search_mode else 35

    async def _run_command(
        self,
        cmd: list[str],
        timeout: int,
    ) -> tuple[int, str, str]:
        resolved_cmd, command_error = resolve_command(cmd)
        if command_error:
            return -1, "", command_error

        env = os.environ.copy()
        ffmpeg_path = self._resolve_ffmpeg_path()
        if ffmpeg_path:
            ffmpeg_dir = ffmpeg_path.parent if ffmpeg_path.is_file() else ffmpeg_path
            path_sep = ";" if os.name == "nt" else ":"
            env["PATH"] = f"{ffmpeg_dir}{path_sep}{env.get('PATH', '')}"

        process = await asyncio.create_subprocess_exec(
            *resolved_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            return -1, "", f"コマンドがタイムアウトしました（{timeout}秒）"

        return (
            process.returncode or 0,
            stdout.decode("utf-8", errors="replace"),
            stderr.decode("utf-8", errors="replace"),
        )

    def _resolve_ffmpeg_path(self) -> Optional[Path]:
        if not Config.FFMPEG_PATH:
            return None

        ffmpeg_path = Path(Config.FFMPEG_PATH)
        if not ffmpeg_path.exists():
            logger.warning("FFMPEG_PATHが存在しません: %s", ffmpeg_path)
            return None
        return ffmpeg_path

    def _get_ffmpeg_executable(self) -> str:
        ffmpeg_path = self._resolve_ffmpeg_path()
        if ffmpeg_path is None:
            return "ffmpeg"

        if ffmpeg_path.is_file():
            return str(ffmpeg_path)

        executable_name = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
        executable_path = ffmpeg_path / executable_name
        if executable_path.exists():
            return str(executable_path)

        return "ffmpeg"
