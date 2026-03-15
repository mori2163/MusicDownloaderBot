"""
Discord Bot本体
スラッシュコマンドによるダウンロードリクエストを処理する
"""

import logging
import discord
from discord import app_commands
from discord.ext import commands
from datetime import datetime
from pathlib import Path
from typing import Optional

from config import Config
from queue_manager import QueueManager, DownloadTask, TaskStatus
from url_parser import URLParser, ServiceType
from metadata_fetcher import MetadataFetcher, MediaMetadata
from archive_utils import create_zip_archive, format_file_size
from file_server import DownloadToken, get_file_server
from voice_player import VoicePlaybackManager, SearchResult

logger = logging.getLogger(__name__)

# サービス別の絵文字とカラー
SERVICE_ICONS = {
    ServiceType.QOBUZ: ("🎵", discord.Color.from_rgb(255, 102, 0)),    # Qobuzオレンジ
    ServiceType.YOUTUBE: ("▶️", discord.Color.from_rgb(255, 0, 0)),     # YouTube赤
    ServiceType.SPOTIFY: ("🎧", discord.Color.from_rgb(30, 215, 96)),   # Spotifyグリーン
}

# キュー追加メッセージの自動削除時間（秒）
QUEUE_MESSAGE_DELETE_DELAY = 10


class DownloadConfirmView(discord.ui.View):
    """ダウンロード確認用のボタンを含むView"""
    
    def __init__(
        self,
        metadata: MediaMetadata,
        bot_instance: "MusicDownloaderBot",
        timeout: float = 300.0,  # 5分でタイムアウト
    ):
        super().__init__(timeout=timeout)
        self.metadata = metadata
        self.bot_instance = bot_instance
        self.message: Optional[discord.Message] = None
    
    @discord.ui.button(
        label="ダウンロード",
        style=discord.ButtonStyle.success,
        emoji="⬇️",
    )
    async def download_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        """ダウンロードボタンが押されたときの処理"""
        logger.info(f"ダウンロードボタン押下: user={interaction.user}, url={self.metadata.url}")
        
        # メッセージIDを取得して渡す（進捗更新用）
        message_id = self.message.id if self.message else None
        
        # キューに追加
        success, message, task = await self.bot_instance.queue_manager.add_task(
            url=self.metadata.url,
            requester_id=interaction.user.id,
            channel_id=interaction.channel_id or 0,
            message_id=message_id,
        )
        
        if success:
            if task is None:
                logger.error("キュー追加成功扱いだがタスク情報がありません: url=%s", self.metadata.url)
                await interaction.response.send_message(
                    "キュー追加処理で内部エラーが発生しました。再試行してください。",
                    ephemeral=True,
                    delete_after=QUEUE_MESSAGE_DELETE_DELAY,
                )
                return

            logger.info(f"キュー追加成功: task_id={task.id[:8]}")
            icon, color = SERVICE_ICONS.get(
                self.metadata.service, ("🎵", discord.Color.blue())
            )
            
            # ephemeralメッセージでキュー追加を通知
            embed = discord.Embed(
                title=f"{icon} キューに追加しました",
                description=f"**{self.metadata.title}**\n{message}",
                color=color,
            )
            embed.set_footer(text=f"このメッセージは{QUEUE_MESSAGE_DELETE_DELAY}秒後に消えます")
            
            await interaction.response.send_message(
                embed=embed,
                ephemeral=True,
                delete_after=QUEUE_MESSAGE_DELETE_DELAY,
            )
            
            # 元のメッセージのボタンを無効化して状態を更新
            button.disabled = True
            button.label = "キューに追加済み"
            button.style = discord.ButtonStyle.secondary
            
            # キャンセルボタンも無効化
            for item in self.children:
                if isinstance(item, discord.ui.Button) and item.label == "キャンセル":
                    item.disabled = True
            
            # Embedを更新してダウンロード待機中であることを表示
            if self.message:
                original_embed = self.message.embeds[0] if self.message.embeds else None
                if original_embed:
                    original_embed.set_footer(text="⏳ ダウンロード待機中...")
                    await self.message.edit(embed=original_embed, view=self)
        else:
            logger.warning(f"キュー追加失敗: {message}")
            embed = discord.Embed(
                title="❌ キュー追加失敗",
                description=message,
                color=discord.Color.red(),
            )
            await interaction.response.send_message(
                embed=embed,
                ephemeral=True,
                delete_after=QUEUE_MESSAGE_DELETE_DELAY,
            )
    
    @discord.ui.button(
        label="キャンセル",
        style=discord.ButtonStyle.secondary,
        emoji="✖️",
    )
    async def cancel_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        """キャンセルボタンが押されたときの処理"""
        logger.info(f"キャンセルボタン押下: user={interaction.user}")
        # 元のメッセージを削除
        if self.message:
            await self.message.delete()
        
        await interaction.response.send_message(
            "キャンセルしました",
            ephemeral=True,
            delete_after=5,
        )
    
    async def on_timeout(self) -> None:
        """タイムアウト時の処理"""
        if self.message:
            # ボタンを無効化
            for item in self.children:
                if isinstance(item, discord.ui.Button):
                    item.disabled = True
            
            try:
                await self.message.edit(view=self)
            except discord.NotFound:
                pass


class DownloadLinkView(discord.ui.View):
    """ダウンロードリンクボタンを含むView"""
    
    def __init__(self, download_url: str):
        super().__init__(timeout=None)  # タイムアウトなし
        
        # URLボタンを追加（外部リンク）
        self.add_item(
            discord.ui.Button(
                label="ダウンロード",
                style=discord.ButtonStyle.link,
                url=download_url,
                emoji="⬇️",
            )
        )


async def build_voice_control_embed(
    bot_instance: "MusicDownloaderBot",
    guild_id: int,
    headline: Optional[str] = None,
) -> discord.Embed:
    """ボイスコントロール用のEmbedを生成"""

    snapshot = await bot_instance.voice_manager.get_snapshot(guild_id)
    status_text = "再生中" if snapshot.playing else "一時停止中" if snapshot.paused else "停止中"
    current_title = snapshot.current_title or "なし"
    elapsed_text = _format_duration(snapshot.current_elapsed) if snapshot.current_elapsed is not None else "--:--"
    duration_text = _format_duration(snapshot.current_duration) if snapshot.current_duration is not None else "--:--"

    embed = discord.Embed(
        title="🎛️ ボイスプレイヤー",
        description=headline or "ボタンで再生を操作できます。",
        color=discord.Color.blurple(),
        timestamp=datetime.now(),
    )
    embed.add_field(name="状態", value=status_text, inline=True)
    embed.add_field(name="音量", value=f"{snapshot.volume_percent}%", inline=True)
    embed.add_field(name="自動再生", value="ON" if snapshot.autoplay else "OFF", inline=True)
    embed.add_field(name="現在再生", value=current_title, inline=False)
    embed.add_field(name="再生位置", value=f"{elapsed_text} / {duration_text}", inline=True)
    embed.add_field(name="キュー", value=f"{snapshot.queue_length}件", inline=True)
    if snapshot.current_thumbnail_url:
        embed.set_thumbnail(url=snapshot.current_thumbnail_url)
    embed.set_footer(text="⏯ 再生/一時停止 | ⏹ 停止 | ⏪ 10秒戻し | ⏩ 秒数ジャンプ | 🔁 再再生")
    return embed


def build_search_results_embed(query: str, results: list[SearchResult]) -> discord.Embed:
    """検索結果一覧Embedを生成"""

    embed = discord.Embed(
        title="🔎 YouTube検索結果",
        description=f"キーワード: **{query}**\n下のボタンで再生/キュー追加します。",
        color=discord.Color.red(),
        timestamp=datetime.now(),
    )
    for index, result in enumerate(results, 1):
        duration_text = _format_duration(result.duration) if result.duration else "--:--"
        embed.add_field(
            name=f"{index}. {result.title}",
            value=f"⏱️ {duration_text} ｜ [リンク]({result.webpage_url})",
            inline=False,
        )
    if results and results[0].thumbnail_url:
        embed.set_thumbnail(url=results[0].thumbnail_url)
    embed.set_footer(text="ボタン押下で即時再生またはキューに追加")
    return embed


class SeekSecondsModal(discord.ui.Modal, title="秒数ジャンプ"):
    """再生位置ジャンプ入力モーダル"""

    def __init__(
        self,
        bot_instance: "MusicDownloaderBot",
        guild_id: int,
        control_message: discord.Message,
    ):
        super().__init__(timeout=180)
        self.bot_instance = bot_instance
        self.guild_id = guild_id
        self.control_message = control_message
        self.seconds_input = discord.ui.TextInput(
            label="移動先の秒数",
            placeholder="例: 75",
            required=True,
            max_length=8,
        )
        self.add_item(self.seconds_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or interaction.guild.id != self.guild_id:
            await interaction.response.send_message(
                "このコントロールは元のサーバー内でのみ使えます。",
                ephemeral=True,
            )
            return

        raw_value = str(self.seconds_input.value).strip()
        if not raw_value.isdigit():
            await interaction.response.send_message(
                "秒数は0以上の整数で入力してください。",
                ephemeral=True,
            )
            return

        seconds = int(raw_value)
        try:
            position = await self.bot_instance.voice_manager.seek(self.guild_id, seconds)
        except RuntimeError as error:
            await interaction.response.send_message(
                _build_voice_failure_message("シークに失敗しました", error),
                ephemeral=True,
            )
            return

        embed = await build_voice_control_embed(
            self.bot_instance,
            self.guild_id,
            headline=f"⏩ 再生位置を {position} 秒へ移動しました。",
        )
        try:
            await self.control_message.edit(embed=embed)
        except discord.HTTPException:
            logger.warning("コントロールEmbed更新に失敗しました (modal)")

        await interaction.response.send_message(
            f"⏩ {position}秒へジャンプしました。",
            ephemeral=True,
        )


class VoiceControlView(discord.ui.View):
    """再生コントロールボタンView"""

    def __init__(self, bot_instance: "MusicDownloaderBot", guild_id: int, timeout: float = 1800.0):
        super().__init__(timeout=timeout)
        self.bot_instance = bot_instance
        self.guild_id = guild_id

    async def _refresh_control_message(
        self,
        interaction: discord.Interaction,
        headline: Optional[str] = None,
    ) -> None:
        if interaction.message is None:
            return
        embed = await build_voice_control_embed(
            self.bot_instance,
            self.guild_id,
            headline=headline,
        )
        try:
            await interaction.message.edit(embed=embed, view=self)
        except discord.HTTPException:
            logger.warning("コントロールEmbed更新に失敗しました")

    def _validate_interaction(self, interaction: discord.Interaction) -> Optional[str]:
        if interaction.guild is None or interaction.guild.id != self.guild_id:
            return "このコントロールは元のサーバー内でのみ使えます。"
        return None

    @discord.ui.button(label="🔉 -25%", style=discord.ButtonStyle.secondary, row=0)
    async def volume_down_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        error_message = self._validate_interaction(interaction)
        if error_message:
            await interaction.response.send_message(error_message, ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        try:
            current = await self.bot_instance.voice_manager.adjust_volume(self.guild_id, -25)
        except RuntimeError as error:
            await interaction.followup.send(
                _build_voice_failure_message("音量変更に失敗しました", error),
                ephemeral=True,
            )
            return

        await self._refresh_control_message(interaction, headline=f"🔉 音量を {current}% に変更しました。")
        await interaction.followup.send(f"🔉 音量: {current}%", ephemeral=True)

    @discord.ui.button(label="🔊 +25%", style=discord.ButtonStyle.secondary, row=0)
    async def volume_up_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        error_message = self._validate_interaction(interaction)
        if error_message:
            await interaction.response.send_message(error_message, ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        try:
            current = await self.bot_instance.voice_manager.adjust_volume(self.guild_id, 25)
        except RuntimeError as error:
            await interaction.followup.send(
                _build_voice_failure_message("音量変更に失敗しました", error),
                ephemeral=True,
            )
            return

        await self._refresh_control_message(interaction, headline=f"🔊 音量を {current}% に変更しました。")
        await interaction.followup.send(f"🔊 音量: {current}%", ephemeral=True)

    @discord.ui.button(label="⏯ 再生/一時停止", style=discord.ButtonStyle.primary, row=0)
    async def toggle_play_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        error_message = self._validate_interaction(interaction)
        if error_message:
            await interaction.response.send_message(error_message, ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        try:
            result = await self.bot_instance.voice_manager.toggle_pause_resume(self.guild_id)
        except RuntimeError as error:
            await interaction.followup.send(
                _build_voice_failure_message("再生操作に失敗しました", error),
                ephemeral=True,
            )
            return

        status_map = {
            "paused": "⏸ 一時停止しました。",
            "resumed": "▶️ 再生を再開しました。",
            "idle": "再生中の曲がありません。",
        }
        message = status_map.get(result, "操作を実行しました。")
        await self._refresh_control_message(interaction, headline=message)
        await interaction.followup.send(message, ephemeral=True)

    @discord.ui.button(label="⏹ 停止", style=discord.ButtonStyle.danger, row=0)
    async def stop_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        error_message = self._validate_interaction(interaction)
        if error_message:
            await interaction.response.send_message(error_message, ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        stopped, _ = await self.bot_instance.voice_manager.stop(self.guild_id, clear_queue=False)
        snapshot = await self.bot_instance.voice_manager.get_snapshot(self.guild_id)
        queue_length = snapshot.queue_length
        if stopped:
            message = f"⏹ 停止しました（キュー {queue_length} 件を保持）。"
        elif queue_length > 0:
            message = f"⏹ 再生中はありません（キュー {queue_length} 件を保持中）。"
        else:
            message = "再生中の曲はありません。"
        await self._refresh_control_message(interaction, headline=message)
        await interaction.followup.send(message, ephemeral=True)

    @discord.ui.button(label="⏪ 10秒戻し", style=discord.ButtonStyle.secondary, row=0)
    async def rewind_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        error_message = self._validate_interaction(interaction)
        if error_message:
            await interaction.response.send_message(error_message, ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        try:
            position = await self.bot_instance.voice_manager.rewind(self.guild_id, seconds=10)
        except RuntimeError as error:
            await interaction.followup.send(
                _build_voice_failure_message("巻き戻しに失敗しました", error),
                ephemeral=True,
            )
            return

        message = f"⏪ {position}秒位置へ巻き戻しました。"
        await self._refresh_control_message(interaction, headline=message)
        await interaction.followup.send(message, ephemeral=True)

    @discord.ui.button(label="⏩ 秒数ジャンプ", style=discord.ButtonStyle.secondary, row=1)
    async def seek_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        error_message = self._validate_interaction(interaction)
        if error_message:
            await interaction.response.send_message(error_message, ephemeral=True)
            return
        if interaction.message is None:
            await interaction.response.send_message("コントロールメッセージを取得できません。", ephemeral=True)
            return

        modal = SeekSecondsModal(
            bot_instance=self.bot_instance,
            guild_id=self.guild_id,
            control_message=interaction.message,
        )
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="🔁 再再生", style=discord.ButtonStyle.secondary, row=1)
    async def replay_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        error_message = self._validate_interaction(interaction)
        if error_message:
            await interaction.response.send_message(error_message, ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        try:
            track = await self.bot_instance.voice_manager.replay(self.guild_id)
        except RuntimeError as error:
            await interaction.followup.send(
                _build_voice_failure_message("再再生に失敗しました", error),
                ephemeral=True,
            )
            return

        message = f"🔁 **{track.title}** を先頭から再生しました。"
        await self._refresh_control_message(interaction, headline=message)
        await interaction.followup.send(message, ephemeral=True)


class SearchResultView(discord.ui.View):
    """検索結果選択ボタンView"""

    def __init__(
        self,
        bot_instance: "MusicDownloaderBot",
        guild_id: int,
        requester_id: int,
        results: list[SearchResult],
        timeout: float = 300.0,
    ):
        super().__init__(timeout=timeout)
        self.bot_instance = bot_instance
        self.guild_id = guild_id
        self.requester_id = requester_id
        self.results = results[:5]
        self._is_processing = False

        for index, _result in enumerate(self.results):
            style = discord.ButtonStyle.primary if index == 0 else discord.ButtonStyle.secondary
            button = discord.ui.Button(
                label=f"{index + 1} を再生/追加",
                style=style,
                row=index // 3,
            )

            async def on_click(interaction: discord.Interaction, item_index: int = index) -> None:
                await self._select_result(interaction, item_index)

            button.callback = on_click
            self.add_item(button)

    def _validate_interaction(self, interaction: discord.Interaction) -> Optional[str]:
        if interaction.guild is None or interaction.guild.id != self.guild_id:
            return "この検索結果は元のサーバー内でのみ使えます。"
        if interaction.user.id != self.requester_id:
            return "この検索結果はコマンド実行者のみ操作できます。"
        return None

    def _set_buttons_processing(self, selected_index: int) -> None:
        for index, child in enumerate(self.children):
            if not isinstance(child, discord.ui.Button):
                continue
            child.disabled = True
            if index == selected_index:
                child.label = f"{index + 1} 処理中..."
                child.style = discord.ButtonStyle.success
            else:
                child.label = f"{index + 1} を再生/追加"
                child.style = discord.ButtonStyle.secondary

    def _reset_buttons(self) -> None:
        for index, child in enumerate(self.children):
            if not isinstance(child, discord.ui.Button):
                continue
            child.disabled = False
            child.label = f"{index + 1} を再生/追加"
            child.style = (
                discord.ButtonStyle.primary
                if index == 0
                else discord.ButtonStyle.secondary
            )

    async def _edit_search_message_view(self, interaction: discord.Interaction) -> None:
        if interaction.message is None:
            return
        try:
            await interaction.message.edit(view=self)
        except discord.HTTPException:
            logger.warning("検索結果メッセージのView更新に失敗しました")

    async def _select_result(self, interaction: discord.Interaction, index: int) -> None:
        error_message = self._validate_interaction(interaction)
        if error_message:
            await interaction.response.send_message(error_message, ephemeral=True)
            return

        if index < 0 or index >= len(self.results):
            await interaction.response.send_message("選択された候補が見つかりません。", ephemeral=True)
            return

        if interaction.guild is None:
            await interaction.response.send_message("このコマンドはサーバー内でのみ使用できます。", ephemeral=True)
            return

        if self._is_processing:
            await interaction.response.send_message(
                "⏳ すでに処理中です。完了までお待ちください。",
                ephemeral=True,
            )
            return

        selected = self.results[index]
        self._is_processing = True
        try:
            await interaction.response.send_message(
                f"⏳ **{selected.title}** を処理中です。しばらくお待ちください...",
                ephemeral=True,
            )
            self._set_buttons_processing(index)
            await self._edit_search_message_view(interaction)

            try:
                result = await self.bot_instance.voice_manager.enqueue_url(
                    guild=interaction.guild,
                    url=selected.webpage_url,
                    requester_id=interaction.user.id,
                    announcement_channel_id=interaction.channel_id,
                )
            except (RuntimeError, discord.ClientException, discord.HTTPException) as error:
                self._reset_buttons()
                await self._edit_search_message_view(interaction)
                await interaction.edit_original_response(
                    content=_build_voice_failure_message("再生できませんでした", error),
                )
                return

            duration_text = _format_duration(result.track.duration or selected.duration)
            duration_suffix = f" [{duration_text}]" if duration_text else ""
            if result.started:
                headline = f"▶️ 再生開始: **{result.track.title}**{duration_suffix}"
            else:
                headline = (
                    f"📥 キューに追加: **{result.track.title}**"
                    f" (位置: {result.position}){duration_suffix}"
                )

            control_embed = await build_voice_control_embed(
                self.bot_instance,
                self.guild_id,
                headline=headline,
            )
            control_view = VoiceControlView(self.bot_instance, self.guild_id)
            if interaction.message is not None:
                try:
                    await interaction.message.edit(embed=control_embed, view=control_view)
                except discord.HTTPException:
                    logger.warning("検索結果メッセージの更新に失敗しました")
                    self._reset_buttons()
                    await self._edit_search_message_view(interaction)

            await interaction.edit_original_response(content=headline)
        finally:
            self._is_processing = False


class MusicDownloaderBot(commands.Bot):
    """音楽ダウンロードBot"""
    
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        
        super().__init__(
            command_prefix="!",
            intents=intents,
        )
        
        self.queue_manager = QueueManager()
        self.voice_manager = VoicePlaybackManager(self)
    
    async def setup_hook(self) -> None:
        """Bot起動時の初期化処理"""
        logger.info("setup_hook: コマンド登録開始")
        # コマンド登録
        self.tree.add_command(dl_command)
        self.tree.add_command(queue_command)
        self.tree.add_command(join_command)
        self.tree.add_command(play_command)
        self.tree.add_command(search_command)
        self.tree.add_command(stop_command)
        self.tree.add_command(replay_command)
        self.tree.add_command(autoplay_command)
        self.tree.add_command(leave_command)
        
        # ダウンロード回数更新用のコールバックを登録
        get_file_server().set_download_callback(self._on_download_link_used)
        
        # キューワーカーを開始
        self.queue_manager.set_progress_callback(self._on_task_progress)
        await self.queue_manager.start_worker()
        logger.info("setup_hook: キューワーカー開始")
        
        # コマンドを同期
        await self.tree.sync()
        logger.info("setup_hook: コマンド同期完了")
    
    async def on_ready(self) -> None:
        """Bot準備完了時"""
        logger.info(f"ログイン完了: {self.user}")
        logger.info(f"接続サーバー数: {len(self.guilds)}")

    async def close(self) -> None:
        """終了時クリーンアップ"""
        await self.voice_manager.shutdown()
        await super().close()
    
    async def _update_preview_message(
        self,
        task: DownloadTask,
        footer_text: str,
    ) -> None:
        """プレビューメッセージのフッターを更新"""
        if not task.message_id:
            return
        
        channel = self.get_channel(task.channel_id)
        if not channel:
            try:
                channel = await self.fetch_channel(task.channel_id)
            except (discord.NotFound, discord.Forbidden):
                logger.warning(f"チャンネル取得失敗: {task.channel_id}")
                return
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return
        
        try:
            message = await channel.fetch_message(task.message_id)
            if message.embeds:
                embed = message.embeds[0]
                embed.set_footer(text=footer_text)
                await message.edit(embed=embed, view=None)  # ボタンを削除
        except discord.NotFound:
            pass
        except discord.HTTPException:
            pass

    async def _on_download_link_used(self, token: DownloadToken) -> None:
        """ダウンロードリンク使用後に残り回数を更新"""
        if token.channel_id is None or token.message_id is None:
            return

        channel = self.get_channel(token.channel_id)
        if not channel:
            try:
                channel = await self.fetch_channel(token.channel_id)
            except (discord.NotFound, discord.Forbidden):
                return
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return

        try:
            message = await channel.fetch_message(token.message_id)
        except discord.NotFound:
            return
        except discord.HTTPException:
            return

        if not message.embeds:
            return

        embed = message.embeds[0]
        updated = False
        for index, field in enumerate(embed.fields):
            if field.name == "📦 ダウンロード":
                field_value = field.value if isinstance(field.value, str) else str(field.value or "")
                value_lines = field_value.splitlines()
                new_lines = []
                for line in value_lines:
                    if line.startswith("残り回数:"):
                        new_lines.append(f"残り回数: **{token.remaining_downloads}回**")
                        updated = True
                    else:
                        new_lines.append(line)
                if updated:
                    embed.set_field_at(
                        index,
                        name=field.name,
                        value="\n".join(new_lines),
                        inline=field.inline,
                    )
                break

        if updated:
            try:
                await message.edit(embed=embed)
            except discord.HTTPException:
                pass
    
    async def _on_task_progress(self, task: DownloadTask) -> None:
        """タスク進捗通知"""
        logger.info(f"タスク進捗通知: task_id={task.id[:8]}, status={task.status.name}")
        
        channel = self.get_channel(task.channel_id)
        if not channel:
            # チャンネルキャッシュなし、fetchする
            try:
                channel = await self.fetch_channel(task.channel_id)
                
            except (discord.NotFound, discord.Forbidden) as e:
                logger.error(f"チャンネル取得失敗: {task.channel_id}, error={e}")
                return
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            logger.warning(f"サポート外のチャンネル: type={type(channel)}")
            return
        
        user_mention = f"<@{task.requester_id}>"
        service_name = URLParser.get_service_name(task.service)
        icon, color = SERVICE_ICONS.get(task.service, ("🎵", discord.Color.blue()))
        
        if task.status == TaskStatus.RUNNING:
            logger.info(f"タスク実行中: {task.id[:8]}")
            # プレビューメッセージを更新
            await self._update_preview_message(
                task,
                f"🔄 ダウンロード中... (タスクID: {task.id[:8]})",
            )
            
            # 新しい通知メッセージは送信しない（プレビューメッセージで状態がわかるため）
        
        elif task.status == TaskStatus.COMPLETED:
            logger.info(f"タスク完了: {task.id[:8]}, result={task.result}")
            # プレビューメッセージを更新
            await self._update_preview_message(
                task,
                f"✅ ダウンロード完了! (タスクID: {task.id[:8]})",
            )
            
            # 完了通知を送信
            embed = discord.Embed(
                title=f"{icon} ダウンロード完了!",
                color=color,
                timestamp=datetime.now(),
            )
            
            # アルバム/フォルダ名を表示
            folder_name = None
            if task.result and task.result.folder_path:
                logger.info(f"フォルダパス: {task.result.folder_path}")
                # 接頭辞を除去して表示
                folder_name = task.result.folder_path.name
                for prefix in [Config.YOUTUBE_PREFIX, Config.SPOTIFY_PREFIX]:
                    if folder_name.startswith(prefix):
                        folder_name = folder_name[len(prefix):]
                        break
                embed.add_field(
                    name="📁 アルバム",
                    value=f"{folder_name}",
                    inline=False,
                )
            
            # 詳細情報
            details = []
            if task.result and task.result.file_count > 0:
                details.append(f"🎵 **{task.result.file_count}** 曲")
            details.append(f"📀 **{service_name}**")
            
            if details:
                embed.add_field(
                    name="詳細",
                    value=" │ ".join(details),
                    inline=False,
                )
            
            embed.set_footer(text=f"タスクID: {task.id[:8]}")
            
            # ダウンロードファイルの準備
            file_attachment = None
            download_view = None
            token: Optional[DownloadToken] = None
            zip_to_cleanup: Optional[Path] = None
            
            try:
                if task.result and task.result.folder_path and task.result.folder_path.exists():
                    logger.info(f"zipアーカイブ作成開始: {task.result.folder_path}")
                    # zipアーカイブを作成（ライブラリではなく一時フォルダに出力）
                    zip_output_path = Config.DOWNLOAD_PATH / f"{task.result.folder_path.name}.zip"
                    zip_path, zip_size = await create_zip_archive(
                        task.result.folder_path,
                        output_path=zip_output_path,
                    )
                    logger.info(f"zipアーカイブ作成完了: path={zip_path}, size={zip_size}")
                    
                    if zip_path and zip_size > 0:
                        size_str = format_file_size(zip_size)
                        
                        # 一旦クリーンアップ対象にする
                        zip_to_cleanup = zip_path
                        
                        if zip_size < Config.DOWNLOAD_SIZE_THRESHOLD:
                            # 10MB以下: Discordに直接添付
                            logger.info(f"Discord直接添付: {zip_path}")
                            try:
                                file_attachment = discord.File(
                                    zip_path,
                                    filename=f"{folder_name or 'download'}.zip",
                                )
                                embed.add_field(
                                    name="📦 ダウンロード",
                                    value=f"ファイルサイズ: {size_str}",
                                    inline=False,
                                )
                            except Exception as e:
                                logger.error(f"ファイル添付準備失敗: {e}")
                                embed.add_field(
                                    name="⚠️ 添付エラー",
                                    value=f"ファイルの添付準備に失敗しました: {e}",
                                    inline=False,
                                )
                        else:
                            # 10MB以上: ダウンロードリンクを生成
                            logger.info(f"ダウンロードリンク生成: {zip_path}")
                            try:
                                file_server = get_file_server()
                                if Config.FILE_SERVER_BASE_URL:
                                    download_url, token = file_server.create_download_link(
                                        file_path=zip_path,
                                        file_name=f"{folder_name or 'download'}.zip",
                                    )
                                    
                                    # ファイルサーバーに正常に登録された場合は、今すぐ削除しない
                                    zip_to_cleanup = None
                                    
                                    embed.add_field(
                                        name="📦 ダウンロード",
                                        value=(
                                            f"ファイルサイズ: {size_str}\n"
                                            f"残り回数: **{token.remaining_downloads}回**\n"
                                            f"有効期限: {Config.DOWNLOAD_LINK_EXPIRE_HOURS}時間"
                                        ),
                                        inline=False,
                                    )
                                    
                                    # ダウンロードボタン付きView
                                    download_view = DownloadLinkView(download_url)
                                else:
                                    embed.add_field(
                                        name="⚠️ ダウンロードリンク",
                                        value=(
                                            f"ファイルサイズ: {size_str}\n"
                                            "サーバー設定がないためリンクを生成できません"
                                        ),
                                        inline=False,
                                    )
                            except Exception as e:
                                logger.error(f"リンク生成失敗: {e}")
                                embed.add_field(
                                    name="⚠️ リンク生成エラー",
                                    value=f"ダウンロードリンクの生成に失敗しました: {e}",
                                    inline=False,
                                )
                else:
                    logger.warning(f"フォルダが存在しない: {task.result.folder_path if task.result else 'None'}")
                
                # メッセージを送信
                logger.info("完了通知メッセージ送信開始")
                send_kwargs = {
                    "content": user_mention,
                    "embed": embed,
                }
                if file_attachment:
                    send_kwargs["file"] = file_attachment
                if download_view:
                    send_kwargs["view"] = download_view
                
                sent_message = await channel.send(**send_kwargs)
                logger.info("完了通知メッセージ送信成功")
                
                if download_view and token:
                    token.channel_id = channel.id
                    token.message_id = sent_message.id
                    if token.download_count > 0:
                        await self._on_download_link_used(token)
            except Exception as e:
                # 送信エラー時の処理
                logger.exception(f"通知送信エラー: {e}")
                try:
                    # 簡潔なメッセージで再試行
                    await channel.send(f"{user_mention} 通知の送信に失敗しましたが、ダウンロードは完了しています。")
                except Exception:
                    # チャンネル送信が壊滅的な場合はDMを試みる
                    try:
                        user = self.get_user(task.requester_id) or await self.fetch_user(task.requester_id)
                        if user:
                            await user.send(f"通知の送信に失敗しましたが、ダウンロードは完了しました。タスクID: {task.id[:8]}")
                    except Exception as dm_e:
                        logger.error(f"DM送信失敗: {dm_e}")
            finally:
                # 添付ファイルを閉じて一時ファイルを削除
                if file_attachment:
                    file_attachment.close()
                if zip_to_cleanup and zip_to_cleanup.exists():
                    zip_to_cleanup.unlink()
                    logger.info(f"一時zipファイル削除: {zip_to_cleanup}")
        
        elif task.status == TaskStatus.FAILED:
            logger.warning(f"タスク失敗: {task.id[:8]}, error={task.result.error if task.result else 'Unknown'}")
            # プレビューメッセージを更新
            await self._update_preview_message(
                task,
                f"❌ ダウンロード失敗 (タスクID: {task.id[:8]})",
            )
            
            # エラー通知を送信
            embed = discord.Embed(
                title="❌ ダウンロード失敗",
                description=task.result.message if task.result else "不明なエラー",
                color=discord.Color.red(),
                timestamp=datetime.now(),
            )
            embed.add_field(name="📀 サービス", value=service_name, inline=True)
            
            if task.result and task.result.error:
                # エラーメッセージが長すぎる場合は切り詰め
                error_text = task.result.error[:400]
                if len(task.result.error) > 400:
                    error_text += "..."
                embed.add_field(
                    name="⚠️ エラー詳細",
                    value=f"`\n{error_text}\n`",
                    inline=False,
                )
            embed.set_footer(text=f"タスクID: {task.id[:8]}")
            await channel.send(content=user_mention, embed=embed)


# Botインスタンス（コマンドから参照するため）
bot: Optional[MusicDownloaderBot] = None


def get_bot() -> MusicDownloaderBot:
    """Botインスタンスを取得"""
    global bot
    if bot is None:
        bot = MusicDownloaderBot()
    return bot


# スラッシュコマンド定義
@app_commands.command(name="dl", description="URLから音楽をダウンロードする")
@app_commands.describe(url="ダウンロード対象のURL（Qobuz、YouTube、Spotify）")
async def dl_command(interaction: discord.Interaction, url: str) -> None:
    """ダウンロードコマンド"""
    logger.info(f"/dl コマンド実行: user={interaction.user}, url={url}")
    bot_instance = get_bot()
    
    # URL検証
    service = URLParser.detect_service(url)
    if service == ServiceType.UNKNOWN:
        logger.warning(f"非対応URL: {url}")
        embed = discord.Embed(
            title="❌ 非対応のURL",
            description="Qobuz、YouTube、Spotifyのリンクを指定してください。",
            color=discord.Color.red(),
        )
        embed.add_field(
            name="対応サービス",
            value="🎵 Qobuz\n▶️ YouTube\n🎧 Spotify",
            inline=False,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    
    # インタラクションを保留（3秒ルール回避）
    await interaction.response.defer()
    
    # メタデータ取得中のメッセージを表示
    icon, color = SERVICE_ICONS.get(service, ("🎵", discord.Color.blue()))
    service_name = URLParser.get_service_name(service)
    
    logger.info(f"メタデータ取得開始: service={service_name}")
    loading_embed = discord.Embed(
        title=f"{icon} メタデータ取得中...",
        description=f"**{service_name}** から情報を取得しています",
        color=color,
    )
    await interaction.edit_original_response(embed=loading_embed)
    
    # メタデータを取得
    metadata = await MetadataFetcher.fetch(url)
    
    if metadata is None:
        logger.warning(f"メタデータ取得失敗、フォールバック使用: {url}")
        # メタデータ取得失敗時はフォールバック
        metadata = MediaMetadata(
            title=f"{service_name} コンテンツ",
            artist="不明",
            service=service,
            url=url,
        )
    else:
        logger.info(f"メタデータ取得成功: title={metadata.title}, artist={metadata.artist}")
    
    # プレビュー用Embedを作成
    embed = discord.Embed(
        title=f"{icon} {metadata.title}",
        description=f"**{metadata.artist}**",
        color=color,
        timestamp=datetime.now(),
    )
    
    # サムネイルがあれば設定
    if metadata.thumbnail_url:
        embed.set_thumbnail(url=metadata.thumbnail_url)
    
    # 詳細情報を追加
    info_parts = [f"📀 **{service_name}**"]
    if metadata.duration:
        minutes, seconds = divmod(metadata.duration, 60)
        info_parts.append(f"⏱️ {minutes}:{seconds:02d}")
    if metadata.track_count and metadata.track_count > 1:
        info_parts.append(f"🎵 {metadata.track_count}曲")
    if metadata.album:
        info_parts.append(f"💿 {metadata.album}")
    
    embed.add_field(name="詳細", value=" │ ".join(info_parts), inline=False)
    embed.add_field(name="🔗 URL", value=f"[リンク]({url})", inline=False)
    embed.set_footer(text="ダウンロードボタンを押してキューに追加")
    
    # ボタン付きViewを作成
    view = DownloadConfirmView(metadata, bot_instance)
    
    # メッセージを更新
    message = await interaction.edit_original_response(embed=embed, view=view)
    view.message = message
    logger.info(f"プレビュー表示完了: message_id={message.id}")


@app_commands.command(name="queue", description="ダウンロード/ボイスキューの状態を表示")
async def queue_command(interaction: discord.Interaction) -> None:
    """キュー状態表示コマンド"""
    logger.info(f"/queue コマンド実行: user={interaction.user}")
    bot_instance = get_bot()
    pending, current = bot_instance.queue_manager.get_queue_info()

    def _trim_text(text: str, max_length: int = 70) -> str:
        if len(text) <= max_length:
            return text
        return f"{text[: max_length - 1]}…"

    embed = discord.Embed(
        title="📋 キュー状態",
        color=discord.Color.blue(),
        timestamp=datetime.now(),
    )

    # ダウンロードキュー: 現在実行中
    if current:
        service_name = URLParser.get_service_name(current.service)
        icon, _ = SERVICE_ICONS.get(current.service, ("🎵", discord.Color.blue()))
        running_url = _trim_text(current.url, max_length=80)
        embed.add_field(
            name="⬇️ ダウンロード実行中",
            value=f"{icon} {service_name}\n{running_url}",
            inline=False,
        )
    else:
        embed.add_field(name="⬇️ ダウンロード実行中", value="なし", inline=False)

    # ダウンロードキュー: 待機中
    if pending:
        queue_lines: list[str] = []
        for i, task in enumerate(pending[:5], 1):
            service_name = URLParser.get_service_name(task.service)
            icon, _ = SERVICE_ICONS.get(task.service, ("🎵", discord.Color.blue()))
            queue_lines.append(f"{i}. {icon} {service_name} - {_trim_text(task.url, max_length=60)}")
        if len(pending) > 5:
            queue_lines.append(f"... 他 {len(pending) - 5} 件")
        embed.add_field(
            name=f"📥 ダウンロード待機中 ({len(pending)}件)",
            value="\n".join(queue_lines),
            inline=False,
        )
    else:
        embed.add_field(name="📥 ダウンロード待機中", value="なし", inline=False)

    # ボイスキュー（サーバー内のみ）
    if interaction.guild is not None:
        snapshot = await bot_instance.voice_manager.get_snapshot(interaction.guild.id)
        current_track, queued_tracks = await bot_instance.voice_manager.get_queue_tracks(
            interaction.guild.id,
            limit=5,
        )

        if not snapshot.connected:
            voice_status = "未接続"
        elif snapshot.playing:
            voice_status = "再生中"
        elif snapshot.paused:
            voice_status = "一時停止中"
        else:
            voice_status = "停止中"

        if current_track is not None:
            current_voice_text = _trim_text(current_track.title, max_length=70)
        else:
            current_voice_text = "なし"

        embed.add_field(
            name="🔊 ボイス状態",
            value=f"状態: {voice_status}\n現在: {current_voice_text}",
            inline=False,
        )

        if queued_tracks:
            voice_lines = [
                f"{i}. {_trim_text(track.title, max_length=70)}"
                for i, track in enumerate(queued_tracks, 1)
            ]
            remaining = max(0, snapshot.queue_length - len(queued_tracks))
            if remaining > 0:
                voice_lines.append(f"... 他 {remaining} 件")
            embed.add_field(
                name=f"🎶 ボイス待機中 ({snapshot.queue_length}件)",
                value="\n".join(voice_lines),
                inline=False,
            )
        else:
            embed.add_field(name="🎶 ボイス待機中", value="なし", inline=False)

    await interaction.response.send_message(embed=embed)


VoiceChannelType = discord.VoiceChannel | discord.StageChannel


def _get_requester_voice_channel(
    interaction: discord.Interaction,
) -> tuple[Optional[VoiceChannelType], Optional[str]]:
    """実行者のボイスチャンネルを取得"""
    if interaction.guild is None:
        return None, "このコマンドはサーバー内でのみ使用できます。"

    member = interaction.user
    if not isinstance(member, discord.Member):
        member = interaction.guild.get_member(interaction.user.id)
    if member is None or member.voice is None or member.voice.channel is None:
        return None, "先にボイスチャンネルへ参加してください。"

    channel = member.voice.channel
    if isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
        return channel, None
    return None, "このチャンネル種別はサポートされていません。"


def _format_duration(seconds: Optional[int]) -> str:
    if seconds is None:
        return ""
    minutes, remain = divmod(seconds, 60)
    return f"{minutes}:{remain:02d}"


def _build_voice_failure_message(action: str, error: Exception) -> str:
    detail = str(error)
    lowered = detail.lower()
    code = getattr(error, "code", None)

    if code == 4017 or "4017" in detail:
        return (
            "❌ Discord側でボイス接続が拒否されました（code: 4017）。\n"
            "`discord.py>=2.7.1` と `davey` が必要です。"
            "`.venv` で `python -m pip install -U \"discord.py>=2.7.1\" davey PyNaCl` "
            "または `uv sync` を実行し、Botを再起動してください。"
        )
    if "PyNaCl" in detail:
        return (
            "❌ PyNaCl が未インストールのためボイス機能を利用できません。\n"
            "`.venv` を有効化して `python -m pip install PyNaCl` または "
            "`uv sync` を実行後、Botを再起動してください。"
        )
    if "davey" in lowered:
        return (
            "❌ davey が未インストールのためボイス機能を利用できません。\n"
            "`discord.py>=2.7.1` と `davey` を導入してください。\n"
            "`.venv` を有効化して `python -m pip install -U \"discord.py>=2.7.1\" davey` "
            "または `uv sync` を実行後、Botを再起動してください。"
        )
    if "requested format is not available" in lowered:
        return (
            "❌ YouTube側フォーマットの取得に失敗しました。\n"
            "内部でフォールバックを試行しましたが再生可能な音声が見つかりませんでした。\n"
            "少し時間を置いて再試行するか、`YOUTUBE_PO_TOKEN` を更新してください。"
        )
    if "音声ストリームの取得に失敗しました" in detail:
        return (
            "❌ 音声ストリームを取得できませんでした。\n"
            "YouTube側制限または PO Token 不整合の可能性があります。"
            "時間を置いて再試行し、必要なら `YOUTUBE_PO_TOKEN` を更新してください。"
        )
    if isinstance(error, TimeoutError) or "timed out connecting to voice" in lowered:
        return (
            "❌ ボイス接続がタイムアウトしました。\n"
            "依存関係を最新化（`discord.py>=2.7.1`, `davey`, `PyNaCl`）して Bot を再起動後、"
            "もう一度 `/join` を試してください。"
        )
    return f"❌ {action}: {detail}"


@app_commands.command(name="join", description="Botをボイスチャンネルに呼び出す")
async def join_command(interaction: discord.Interaction) -> None:
    """ボイスチャンネル参加コマンド"""
    logger.info(f"/join コマンド実行: user={interaction.user}")
    bot_instance = get_bot()

    if interaction.guild is None:
        await interaction.response.send_message(
            "このコマンドはサーバー内でのみ使用できます。",
            ephemeral=True,
        )
        return

    voice_channel, error_message = _get_requester_voice_channel(interaction)
    if voice_channel is None:
        await interaction.response.send_message(error_message or "接続先を特定できません。", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    try:
        await bot_instance.voice_manager.connect(interaction.guild, voice_channel)
    except (RuntimeError, discord.ClientException, discord.HTTPException) as error:
        await interaction.followup.send(
            _build_voice_failure_message("接続に失敗しました", error),
            ephemeral=True,
        )
        return

    await interaction.followup.send(f"🔊 {voice_channel.mention} に接続しました。", ephemeral=True)


@app_commands.command(name="play", description="YouTube URLまたはキュー先頭を再生する")
@app_commands.describe(url="YouTube動画のURL（省略時はキュー先頭を再生）")
async def play_command(interaction: discord.Interaction, url: Optional[str] = None) -> None:
    """YouTube再生コマンド（URL省略時はキュー先頭）"""
    raw_url = (url or "").strip()
    logger.info(
        "/play コマンド実行: user=%s, url=%s",
        interaction.user,
        raw_url or "(queue)",
    )
    bot_instance = get_bot()

    if interaction.guild is None:
        await interaction.response.send_message(
            "このコマンドはサーバー内でのみ使用できます。",
            ephemeral=True,
        )
        return

    if raw_url:
        service = URLParser.detect_service(raw_url)
        if service != ServiceType.YOUTUBE:
            await interaction.response.send_message(
                "音声再生はYouTube URLのみ対応しています。",
                ephemeral=True,
            )
            return

    voice_channel, error_message = _get_requester_voice_channel(interaction)
    if voice_channel is None:
        await interaction.response.send_message(error_message or "接続先を特定できません。", ephemeral=True)
        return

    await interaction.response.defer()
    try:
        await bot_instance.voice_manager.connect(interaction.guild, voice_channel)
        if raw_url:
            result = await bot_instance.voice_manager.enqueue_url(
                guild=interaction.guild,
                url=raw_url,
                requester_id=interaction.user.id,
                announcement_channel_id=interaction.channel_id,
            )
            duration_text = _format_duration(result.track.duration)
            duration_suffix = f" [{duration_text}]" if duration_text else ""
            if result.started:
                headline = f"▶️ 再生開始: **{result.track.title}**{duration_suffix}"
            else:
                headline = (
                    f"📥 キューに追加: **{result.track.title}**"
                    f" (位置: {result.position}){duration_suffix}"
                )
        else:
            track = await bot_instance.voice_manager.play_queued_track(interaction.guild.id)
            duration_text = _format_duration(track.duration)
            duration_suffix = f" [{duration_text}]" if duration_text else ""
            headline = f"▶️ キュー先頭を再生: **{track.title}**{duration_suffix}"
    except (RuntimeError, discord.ClientException, discord.HTTPException) as error:
        await interaction.followup.send(
            _build_voice_failure_message("再生できませんでした", error),
            ephemeral=True,
        )
        return

    embed = await build_voice_control_embed(
        bot_instance,
        interaction.guild.id,
        headline=headline,
    )
    view = VoiceControlView(bot_instance, interaction.guild.id)
    await interaction.followup.send(embed=embed, view=view)


@app_commands.command(name="search", description="YouTubeを検索して再生する")
@app_commands.describe(
    query="検索キーワード",
)
async def search_command(
    interaction: discord.Interaction,
    query: str,
) -> None:
    """YouTube検索コマンド（ボタン選択で再生/キュー追加）"""
    logger.info(f"/search コマンド実行: user={interaction.user}, query={query}")
    bot_instance = get_bot()

    if interaction.guild is None:
        await interaction.response.send_message(
            "このコマンドはサーバー内でのみ使用できます。",
            ephemeral=True,
        )
        return

    voice_channel, error_message = _get_requester_voice_channel(interaction)
    if voice_channel is None:
        await interaction.response.send_message(error_message or "接続先を特定できません。", ephemeral=True)
        return

    await interaction.response.defer()
    search_limit = max(1, min(5, Config.VOICE_SEARCH_RESULT_LIMIT))
    try:
        await bot_instance.voice_manager.connect(interaction.guild, voice_channel)
        results = await bot_instance.voice_manager.search(query, limit=search_limit)
    except (RuntimeError, discord.ClientException, discord.HTTPException) as error:
        await interaction.followup.send(
            _build_voice_failure_message("検索に失敗しました", error),
            ephemeral=True,
        )
        return

    if not results:
        await interaction.followup.send("該当する動画が見つかりませんでした。", ephemeral=True)
        return

    listed_results = results[:5]
    embed = build_search_results_embed(query, listed_results)
    view = SearchResultView(
        bot_instance=bot_instance,
        guild_id=interaction.guild.id,
        requester_id=interaction.user.id,
        results=listed_results,
    )
    await interaction.followup.send(embed=embed, view=view)


@app_commands.command(name="stop", description="再生を停止する（キューは保持）")
async def stop_command(interaction: discord.Interaction) -> None:
    """再生停止コマンド"""
    logger.info(f"/stop コマンド実行: user={interaction.user}")
    bot_instance = get_bot()

    if interaction.guild is None:
        await interaction.response.send_message(
            "このコマンドはサーバー内でのみ使用できます。",
            ephemeral=True,
        )
        return

    stopped, _ = await bot_instance.voice_manager.stop(
        interaction.guild.id,
        clear_queue=False,
    )
    snapshot = await bot_instance.voice_manager.get_snapshot(interaction.guild.id)
    queue_length = snapshot.queue_length

    if stopped:
        await interaction.response.send_message(
            f"⏹️ 再生を停止しました（キュー {queue_length} 件を保持）。"
        )
    elif queue_length > 0:
        await interaction.response.send_message(
            f"⏹️ 再生中の曲はありません（キュー {queue_length} 件を保持中）。"
        )
    else:
        await interaction.response.send_message(
            "再生中の曲はありません。",
            ephemeral=True,
        )


@app_commands.command(name="replay", description="現在または直前の曲を先頭から再生する")
async def replay_command(interaction: discord.Interaction) -> None:
    """再再生コマンド"""
    logger.info(f"/replay コマンド実行: user={interaction.user}")
    bot_instance = get_bot()

    if interaction.guild is None:
        await interaction.response.send_message(
            "このコマンドはサーバー内でのみ使用できます。",
            ephemeral=True,
        )
        return

    await interaction.response.defer()
    try:
        track = await bot_instance.voice_manager.replay(interaction.guild.id)
    except (RuntimeError, discord.ClientException, discord.HTTPException) as error:
        await interaction.followup.send(
            _build_voice_failure_message("再再生に失敗しました", error),
            ephemeral=True,
        )
        return

    duration_text = _format_duration(track.duration)
    duration_suffix = f" [{duration_text}]" if duration_text else ""
    embed = await build_voice_control_embed(
        bot_instance,
        interaction.guild.id,
        headline=f"🔁 再再生: **{track.title}**{duration_suffix}",
    )
    view = VoiceControlView(bot_instance, interaction.guild.id)
    await interaction.followup.send(embed=embed, view=view)


@app_commands.command(name="autoplay", description="自動再生をON/OFFする")
@app_commands.describe(enabled="trueでON、falseでOFF")
async def autoplay_command(interaction: discord.Interaction, enabled: bool) -> None:
    """自動再生切替コマンド"""
    logger.info(f"/autoplay コマンド実行: user={interaction.user}, enabled={enabled}")
    bot_instance = get_bot()

    if interaction.guild is None:
        await interaction.response.send_message(
            "このコマンドはサーバー内でのみ使用できます。",
            ephemeral=True,
        )
        return

    await bot_instance.voice_manager.set_autoplay(interaction.guild.id, enabled)
    status_text = "ON" if enabled else "OFF"
    await interaction.response.send_message(f"🔁 自動再生を **{status_text}** にしました。")


@app_commands.command(name="leave", description="Botをボイスチャンネルから退出させる")
async def leave_command(interaction: discord.Interaction) -> None:
    """ボイス退出コマンド"""
    logger.info(f"/leave コマンド実行: user={interaction.user}")
    bot_instance = get_bot()

    if interaction.guild is None:
        await interaction.response.send_message(
            "このコマンドはサーバー内でのみ使用できます。",
            ephemeral=True,
        )
        return

    disconnected = await bot_instance.voice_manager.leave(interaction.guild.id)
    if disconnected:
        await interaction.response.send_message("👋 ボイスチャンネルから退出しました。")
    else:
        await interaction.response.send_message("接続中のボイスチャンネルはありません。", ephemeral=True)
