"""The Music cog: all slash commands."""

from __future__ import annotations

import difflib
import logging
import os

import discord
from discord import app_commands
from discord.ext import commands

from . import sources
from .player import GuildPlayer
from .sources import SourceError, Track, fmt_duration, is_url
from .ui import SearchPicker

log = logging.getLogger(__name__)

QUEUE_PAGE_SIZE = 15

SOURCE_CHOICES = [
    app_commands.Choice(name="YouTube", value="youtube"),
    app_commands.Choice(name="SoundCloud", value="soundcloud"),
]


class UserError(Exception):
    """A problem the user can fix; shown as-is in chat."""


class Music(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.players: dict[int, GuildPlayer] = {}
        self.idle_timeout = float(os.getenv("IDLE_TIMEOUT_SECONDS") or 180)

    # -- helpers ---------------------------------------------------------

    async def _ensure_player(self, interaction: discord.Interaction) -> GuildPlayer:
        """Get or create the guild's player, joining the caller's voice channel."""
        user = interaction.user
        if not isinstance(user, discord.Member) or user.voice is None or user.voice.channel is None:
            raise UserError("Join a voice channel first, then try again.")
        channel = user.voice.channel

        player = self.players.get(interaction.guild_id)
        if player is not None:
            if player.voice.channel != channel:
                if player.is_active:
                    raise UserError(
                        f"I'm already playing in {player.voice.channel.mention} — join me there."
                    )
                await player.voice.move_to(channel)
            return player

        voice = await channel.connect(self_deaf=True)
        guild_id = interaction.guild_id
        player = GuildPlayer(
            bot=self.bot,
            voice=voice,
            text_channel=interaction.channel,
            idle_timeout=self.idle_timeout,
            on_destroy=lambda: self.players.pop(guild_id, None),
        )
        self.players[guild_id] = player
        return player

    def _player_or_error(self, interaction: discord.Interaction) -> GuildPlayer:
        player = self.players.get(interaction.guild_id)
        if player is None:
            raise UserError("I'm not connected to a voice channel right now.")
        return player

    def _enqueue(self, player: GuildPlayer, track: Track) -> str:
        was_active = player.is_active
        position = player.enqueue(track)
        if was_active or position > 1:
            return (
                f"\N{HEAVY PLUS SIGN} Added to queue (#{position}): "
                f"**{track.title}** ({fmt_duration(track.duration)})"
            )
        return f"\N{BLACK RIGHT-POINTING TRIANGLE} Queued **{track.title}** ({fmt_duration(track.duration)}) — starting now"

    async def _on_pick(self, interaction: discord.Interaction, track: Track) -> None:
        """Called by SearchPicker when the user selects a result."""
        try:
            player = await self._ensure_player(interaction)
        except UserError as exc:
            await interaction.response.edit_message(content=str(exc), view=None)
            return
        message = self._enqueue(player, track)
        await interaction.response.edit_message(content=message, view=None)

    # -- commands ----------------------------------------------------------

    @app_commands.command(description="Play a URL or search for a song (queues if something is already playing)")
    @app_commands.describe(
        query="A URL, or words to search for",
        source="Where to search when the query isn't a URL (default: YouTube)",
    )
    @app_commands.choices(source=SOURCE_CHOICES)
    @app_commands.guild_only()
    async def play(
        self,
        interaction: discord.Interaction,
        query: str,
        source: app_commands.Choice[str] | None = None,
    ) -> None:
        await interaction.response.defer()
        requested_by = interaction.user.display_name

        if is_url(query):
            track = await sources.fetch_track(query, requested_by=requested_by)
            player = await self._ensure_player(interaction)
            await interaction.followup.send(self._enqueue(player, track))
            return

        source_key = source.value if source else "youtube"
        tracks = await sources.search(query, source_key, requested_by=requested_by)
        if not tracks:
            await interaction.followup.send(f"No results found for **{query}**.")
            return
        view = SearchPicker(tracks, interaction.user, self._on_pick)
        view.message = await interaction.followup.send(
            f"\N{LEFT-POINTING MAGNIFYING GLASS} Top results for **{query}** — pick one:",
            view=view,
        )

    @app_commands.command(description="Stop playback, clear the queue, and disconnect")
    @app_commands.guild_only()
    async def stop(self, interaction: discord.Interaction) -> None:
        player = self.players.get(interaction.guild_id)
        if player is not None:
            await player.destroy()
        elif interaction.guild.voice_client is not None:
            await interaction.guild.voice_client.disconnect(force=True)
        else:
            raise UserError("I'm not connected to a voice channel.")
        await interaction.response.send_message(
            "\N{BLACK SQUARE FOR STOP} Stopped playback and cleared the queue. Bye!"
        )

    @app_commands.command(description="Pause the current track (stays connected)")
    @app_commands.guild_only()
    async def pause(self, interaction: discord.Interaction) -> None:
        player = self._player_or_error(interaction)
        if player.voice.is_paused():
            raise UserError("Playback is already paused. Use `/resume` to continue.")
        if not player.voice.is_playing():
            raise UserError("Nothing is playing right now.")
        player.pause()
        await interaction.response.send_message("\N{DOUBLE VERTICAL BAR} Paused.")

    @app_commands.command(description="Resume paused playback")
    @app_commands.guild_only()
    async def resume(self, interaction: discord.Interaction) -> None:
        player = self._player_or_error(interaction)
        if not player.voice.is_paused():
            raise UserError("Nothing is paused right now.")
        player.resume()
        await interaction.response.send_message("\N{BLACK RIGHT-POINTING TRIANGLE} Resumed.")

    @app_commands.command(description="Skip the current track (disconnects if the queue is empty)")
    @app_commands.guild_only()
    async def skip(self, interaction: discord.Interaction) -> None:
        player = self._player_or_error(interaction)
        if not player.is_active and not player.queue:
            raise UserError("Nothing is playing right now.")
        if player.queue:
            player.skip()
            await interaction.response.send_message(
                "\N{BLACK RIGHT-POINTING DOUBLE TRIANGLE} Skipped."
            )
        else:
            await player.destroy()
            await interaction.response.send_message(
                "\N{BLACK RIGHT-POINTING DOUBLE TRIANGLE} Skipped — queue is empty, disconnecting."
            )

    @app_commands.command(description="Show the current queue")
    @app_commands.guild_only()
    async def queue(self, interaction: discord.Interaction) -> None:
        player = self.players.get(interaction.guild_id)
        if player is None or (player.now_playing is None and not player.queue):
            raise UserError("The queue is empty and nothing is playing.")

        lines = []
        if player.now_playing is not None:
            track = player.now_playing
            lines.append(
                f"**Now playing:** {track.title} ({fmt_duration(track.duration)}) "
                f"— requested by {track.requested_by}\n"
            )
        for i, track in enumerate(list(player.queue)[:QUEUE_PAGE_SIZE], start=1):
            lines.append(
                f"`{i}.` **{track.title}** ({fmt_duration(track.duration)}) "
                f"— {track.requested_by}"
            )
        remaining = len(player.queue) - QUEUE_PAGE_SIZE
        if remaining > 0:
            lines.append(f"…and {remaining} more")

        embed = discord.Embed(
            title="\N{MULTIPLE MUSICAL NOTES} Queue",
            description="\n".join(lines),
            color=discord.Color.blurple(),
        )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(description="Remove a track from the queue by its number or name")
    @app_commands.describe(target="Queue number (from /queue) or part of the track's name")
    @app_commands.guild_only()
    async def remove(self, interaction: discord.Interaction, target: str) -> None:
        player = self.players.get(interaction.guild_id)
        if player is None or not player.queue:
            raise UserError("The queue is empty — nothing to remove.")

        index = self._find_queue_index(player, target.strip())
        if index is None:
            raise UserError(f"Couldn't find anything in the queue matching **{target}**.")
        track = player.remove_at(index)
        await interaction.response.send_message(
            f"\N{WASTEBASKET} Removed #{index + 1}: **{track.title}**"
        )

    @staticmethod
    def _find_queue_index(player: GuildPlayer, target: str) -> int | None:
        if target.isdigit():
            position = int(target)
            if 1 <= position <= len(player.queue):
                return position - 1
            return None
        titles = [track.title.lower() for track in player.queue]
        needle = target.lower()
        for i, title in enumerate(titles):
            if needle in title:
                return i
        close = difflib.get_close_matches(needle, titles, n=1, cutoff=0.4)
        if close:
            return titles.index(close[0])
        return None

    # -- events ------------------------------------------------------------

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        """Clean up if the bot is disconnected externally (kicked, channel deleted)."""
        if member.id != self.bot.user.id:
            return
        if after.channel is None:
            player = self.players.get(member.guild.id)
            if player is not None:
                await player.destroy()

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        original = getattr(error, "original", error)
        if isinstance(original, (UserError, SourceError)):
            message = str(original)
        else:
            log.error("Command error", exc_info=original)
            message = "Something went wrong running that command."
        if interaction.response.is_done():
            await interaction.followup.send(message)
        else:
            await interaction.response.send_message(message, ephemeral=True)
