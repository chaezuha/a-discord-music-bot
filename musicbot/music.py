"""The Music cog: all slash commands."""

from __future__ import annotations

import asyncio
import difflib
import logging
import math
import os

import discord
from discord import app_commands
from discord.ext import commands

from . import sources
from .notifier import BreakageNotifier
from .player import MAX_QUEUE_SIZE, GuildPlayer, QueueFullError
from .sources import (
    TITLE_DISPLAY_LIMIT,
    SourceError,
    Track,
    fmt_duration,
    fmt_title,
    is_url,
    truncate,
)
from .ui import SearchPicker

log = logging.getLogger(__name__)

QUEUE_PAGE_SIZE = 15
MAX_QUERY_LENGTH = 500
EMBED_DESCRIPTION_LIMIT = 4096
DESTROY_WAIT_SECONDS = 10

esc = discord.utils.escape_markdown

SOURCE_CHOICES = [
    app_commands.Choice(name="YouTube", value="youtube"),
    app_commands.Choice(name="SoundCloud", value="soundcloud"),
]


class UserError(Exception):
    """A problem the user can fix; shown as-is in chat."""


def votes_needed(listener_count: int) -> int:
    """Votes required to pass a skip: half the listeners, rounded up (minimum 1)."""
    return max(1, (listener_count + 1) // 2)


def env_id(name: str) -> int | None:
    """Parse an optional Discord-ID environment variable with a clear error."""
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        raise ValueError(f"{name} must be a numeric Discord ID, got {raw!r}.") from None


def env_idle_timeout(default: float = 180.0) -> float:
    """Parse IDLE_TIMEOUT_SECONDS: a finite, positive number of seconds."""
    raw = (os.getenv("IDLE_TIMEOUT_SECONDS") or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(
            f"IDLE_TIMEOUT_SECONDS must be a number of seconds, got {raw!r}."
        ) from None
    if not math.isfinite(value) or value <= 0:
        raise ValueError(
            f"IDLE_TIMEOUT_SECONDS must be a finite, positive number of seconds, got {raw!r}."
        )
    return value


class Music(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.players: dict[int, GuildPlayer] = {}
        self._player_locks: dict[int, asyncio.Lock] = {}
        self.idle_timeout = env_idle_timeout()
        self.notifier = BreakageNotifier(env_id("OWNER_ID"))

    async def cog_unload(self) -> None:
        await asyncio.gather(
            *(player.destroy() for player in list(self.players.values())),
            return_exceptions=True,
        )

    # -- helpers ---------------------------------------------------------

    @staticmethod
    def _require_voice(interaction: discord.Interaction) -> discord.abc.Connectable:
        """The caller's current voice channel, or a UserError."""
        voice = getattr(interaction.user, "voice", None)
        if voice is None or voice.channel is None:
            raise UserError("Join a voice channel first, then try again.")
        return voice.channel

    async def _ensure_player(self, interaction: discord.Interaction) -> GuildPlayer:
        """Get or create the guild's player, joining the caller's voice channel."""
        channel = self._require_voice(interaction)
        guild_id = interaction.guild_id

        # Serialized per guild: concurrent /play calls must not double-connect,
        # and a player mid-/stop must not receive new tracks.
        lock = self._player_locks.setdefault(guild_id, asyncio.Lock())
        async with lock:
            player = self.players.get(guild_id)
            if player is not None and player.destroyed:
                # destroy() is in flight; wait for its disconnect to finish so
                # the new voice connection can't race it.
                try:
                    await asyncio.wait_for(player.wait_closed(), timeout=DESTROY_WAIT_SECONDS)
                except asyncio.TimeoutError:
                    log.warning("Timed out waiting for player teardown in guild %s", guild_id)
                self._remove_player(guild_id, player)
                player = None
            if player is not None:
                if player.voice.channel != channel:
                    if player.is_active:
                        raise UserError(
                            f"I'm already playing in {player.voice.channel.mention} — join me there."
                        )
                    await player.voice.move_to(channel)
                # Announcements follow the channel of the latest command.
                player.text_channel = interaction.channel
                return player

            voice = await channel.connect(self_deaf=True)
            player = GuildPlayer(
                bot=self.bot,
                voice=voice,
                text_channel=interaction.channel,
                idle_timeout=self.idle_timeout,
                on_destroy=lambda: self._remove_player(guild_id, player),
                notifier=self.notifier,
            )
            self.players[guild_id] = player
            return player

    def _remove_player(self, guild_id: int, player: GuildPlayer) -> None:
        """Drop the guild's player, but only if it's still this instance."""
        if self.players.get(guild_id) is player:
            del self.players[guild_id]

    def _player_or_error(self, interaction: discord.Interaction) -> GuildPlayer:
        player = self.players.get(interaction.guild_id)
        if player is None:
            raise UserError("I'm not connected to a voice channel right now.")
        return player

    @staticmethod
    def _require_same_channel(interaction: discord.Interaction, player: GuildPlayer) -> None:
        """Control commands are limited to people in the bot's voice channel."""
        voice = getattr(interaction.user, "voice", None)
        if voice is None or voice.channel != player.voice.channel:
            raise UserError("Join my voice channel to use that command.")

    def _enqueue(self, player: GuildPlayer, track: Track, front: bool = False) -> str:
        was_active = player.is_active
        try:
            position = player.enqueue(track, front=front)
        except QueueFullError:
            raise UserError(f"The queue is full (max {MAX_QUEUE_SIZE} tracks).") from None
        if was_active or position > 1:
            if front:
                return (
                    f"\N{BLACK RIGHT-POINTING DOUBLE TRIANGLE WITH VERTICAL BAR} Playing next: "
                    f"{fmt_title(track)} ({fmt_duration(track.duration)})"
                )
            return (
                f"\N{HEAVY PLUS SIGN} Added to queue (#{position}): "
                f"{fmt_title(track)} ({fmt_duration(track.duration)})"
            )
        return f"\N{BLACK RIGHT-POINTING TRIANGLE} Queued {fmt_title(track)} ({fmt_duration(track.duration)}) — starting now"

    async def _on_pick(
        self, interaction: discord.Interaction, track: Track, front: bool = False
    ) -> None:
        """Called by SearchPicker when the user selects a result."""
        # Joining voice can outlast Discord's 3s ack deadline; ack the click first.
        await interaction.response.defer()
        try:
            player = await self._ensure_player(interaction)
            message = self._enqueue(player, track, front=front)
        except UserError as exc:
            message = str(exc)
        except Exception:
            # View callbacks don't reach cog_app_command_error; without this
            # the deferred response would spin forever.
            log.exception("Failed to queue picked track")
            message = "Something went wrong queueing that track."
        await interaction.edit_original_response(content=message, view=None)

    async def _play_impl(
        self,
        interaction: discord.Interaction,
        query: str,
        source: app_commands.Choice[str] | None,
        *,
        front: bool,
    ) -> None:
        # Cheap gates before any yt-dlp work: the caller must be in voice
        # (re-checked by _ensure_player after extraction) and the query sane.
        self._require_voice(interaction)
        query = query.strip()
        if not query:
            raise UserError("Give me something to play — a URL or words to search for.")
        if len(query) > MAX_QUERY_LENGTH:
            raise UserError(f"That query is too long (max {MAX_QUERY_LENGTH} characters).")

        await interaction.response.defer()
        requested_by = esc(interaction.user.display_name)

        if is_url(query):
            # Failures here don't feed the breakage notifier: user-typed URLs
            # are typo-prone and deliberately triggerable.
            track = await sources.fetch_track(query, requested_by=requested_by)
            player = await self._ensure_player(interaction)
            await interaction.followup.send(self._enqueue(player, track, front=front))
            return

        source_key = source.value if source else "youtube"
        try:
            tracks = await sources.search(query, source_key, requested_by=requested_by)
        except SourceError:
            # A bot-formed search against a known-good site failing is a real
            # breakage signal, unlike an arbitrary user-typed URL.
            await self.notifier.record_failure(self.bot)
            raise
        self.notifier.record_success()
        if not tracks:
            await interaction.followup.send(f"No results found for **{esc(query)}**.")
            return

        async def on_pick(pick_interaction: discord.Interaction, track: Track) -> None:
            await self._on_pick(pick_interaction, track, front=front)

        view = SearchPicker(tracks, interaction.user, on_pick)
        view.message = await interaction.followup.send(
            f"\N{LEFT-POINTING MAGNIFYING GLASS} Top results for **{esc(query)}** — pick one:",
            view=view,
        )

    # -- commands ----------------------------------------------------------

    @app_commands.command(
        description="Play a URL or search for a song (queues if something is already playing)"
    )
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
        await self._play_impl(interaction, query, source, front=False)

    @app_commands.command(description="Like /play, but the track jumps to the front of the queue")
    @app_commands.describe(
        query="A URL, or words to search for",
        source="Where to search when the query isn't a URL (default: YouTube)",
    )
    @app_commands.choices(source=SOURCE_CHOICES)
    @app_commands.guild_only()
    async def playnext(
        self,
        interaction: discord.Interaction,
        query: str,
        source: app_commands.Choice[str] | None = None,
    ) -> None:
        await self._play_impl(interaction, query, source, front=True)

    @app_commands.command(description="Stop playback, clear the queue, and disconnect")
    @app_commands.guild_only()
    async def stop(self, interaction: discord.Interaction) -> None:
        if (
            self.players.get(interaction.guild_id) is None
            and interaction.guild.voice_client is None
        ):
            raise UserError("I'm not connected to a voice channel.")
        # Disconnecting can outlast Discord's 3s ack deadline; ack first.
        await interaction.response.defer()
        # Same lock as _ensure_player: a concurrent /play must not connect or
        # enqueue into a player that is mid-teardown. State is re-read under
        # the lock because it may have changed while deferring.
        lock = self._player_locks.setdefault(interaction.guild_id, asyncio.Lock())
        async with lock:
            player = self.players.get(interaction.guild_id)
            voice_client = interaction.guild.voice_client
            if player is None and voice_client is None:
                raise UserError("I'm not connected to a voice channel.")
            bot_channel = player.voice.channel if player is not None else voice_client.channel
            caller_voice = getattr(interaction.user, "voice", None)
            if caller_voice is None or caller_voice.channel != bot_channel:
                raise UserError("Join my voice channel to use that command.")
            if player is not None:
                await player.destroy()
            else:
                # Orphaned voice client (no player): disconnect it directly.
                await voice_client.disconnect(force=True)
        await interaction.followup.send(
            "\N{BLACK SQUARE FOR STOP} Stopped playback and cleared the queue. Bye!"
        )

    @app_commands.command(description="Pause the current track (stays connected)")
    @app_commands.guild_only()
    async def pause(self, interaction: discord.Interaction) -> None:
        player = self._player_or_error(interaction)
        self._require_same_channel(interaction, player)
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
        self._require_same_channel(interaction, player)
        if not player.voice.is_paused():
            raise UserError("Nothing is paused right now.")
        player.resume()
        await interaction.response.send_message("\N{BLACK RIGHT-POINTING TRIANGLE} Resumed.")

    @app_commands.command(
        description="Vote to skip the current track (majority of the voice channel)"
    )
    @app_commands.guild_only()
    async def skip(self, interaction: discord.Interaction) -> None:
        player = self._player_or_error(interaction)
        if not player.is_active and not player.queue:
            raise UserError("Nothing is playing right now.")
        user = interaction.user
        channel = player.voice.channel
        if (
            not isinstance(user, discord.Member)
            or user.voice is None
            or user.voice.channel != channel
        ):
            raise UserError("Join my voice channel to vote to skip.")

        listener_ids = {m.id for m in channel.members if not m.bot}
        passed, already_voted, needed = self._tally_skip_vote(player, user.id, listener_ids)
        if passed:
            verb = "Vote passed — skipped" if needed > 1 else "Skipped"
            await interaction.response.send_message(self._do_skip(player, verb))
        elif already_voted:
            raise UserError(f"You already voted — {len(player.skip_votes)}/{needed} votes to skip.")
        else:
            await interaction.response.send_message(
                f"Vote to skip: **{len(player.skip_votes)}/{needed}** — `/skip` to add your vote."
            )

    @staticmethod
    def _tally_skip_vote(
        player: GuildPlayer, voter_id: int, listener_ids: set[int]
    ) -> tuple[bool, bool, int]:
        """Prune departed voters and register this vote.

        Returns (passed, already_voted, needed). The threshold is evaluated
        even for a repeat vote: listeners leaving can turn the existing votes
        into a majority.
        """
        needed = votes_needed(len(listener_ids))
        player.skip_votes &= listener_ids
        already_voted = voter_id in player.skip_votes
        player.skip_votes.add(voter_id)
        return len(player.skip_votes) >= needed, already_voted, needed

    @app_commands.command(description="Skip the current track immediately, no vote")
    @app_commands.guild_only()
    async def forceskip(self, interaction: discord.Interaction) -> None:
        player = self._player_or_error(interaction)
        self._require_same_channel(interaction, player)
        if not player.is_active and not player.queue:
            raise UserError("Nothing is playing right now.")
        await interaction.response.send_message(self._do_skip(player, "Force-skipped"))

    @staticmethod
    def _do_skip(player: GuildPlayer, verb: str) -> str:
        """Skip the current track; `verb` leads the confirmation message."""
        player.skip()
        prefix = f"\N{BLACK RIGHT-POINTING DOUBLE TRIANGLE} {verb}"
        if player.queue:
            if player.song_looping:
                return f"{prefix}. (song loop is still on — the next track will repeat)"
            return f"{prefix}."
        if player.queue_looping:
            return f"{prefix} — queue loop is on, so it will come back around."
        return f"{prefix} — the queue is empty, so playback stopped."

    @app_commands.command(description="Show the current queue")
    @app_commands.guild_only()
    async def queue(self, interaction: discord.Interaction) -> None:
        player = self.players.get(interaction.guild_id)
        if player is None or (player.now_playing is None and not player.queue):
            raise UserError("The queue is empty and nothing is playing.")

        lines = []
        if player.now_playing is not None:
            track = player.now_playing
            marker = (
                " \N{CLOCKWISE RIGHTWARDS AND LEFTWARDS OPEN CIRCLE ARROWS} (looping)"
                if player.song_looping
                else ""
            )
            if player.voice.is_paused():
                marker += " \N{DOUBLE VERTICAL BAR} (paused)"
            lines.append(
                f"**Now playing:** {fmt_title(track)} ({self._fmt_progress(player, track)})"
                f"{marker} — requested by {track.requested_by}\n"
            )
        queue_lines = [
            f"`{i}.` {fmt_title(track)} ({fmt_duration(track.duration)}) — {track.requested_by}"
            for i, track in enumerate(list(player.queue)[:QUEUE_PAGE_SIZE], start=1)
        ]

        embed = discord.Embed(
            title="\N{MULTIPLE MUSICAL NOTES} Queue",
            description=self._queue_description(lines, queue_lines, len(player.queue)),
            color=discord.Color.blurple(),
        )
        if player.queue_looping:
            embed.set_footer(
                text="\N{CLOCKWISE RIGHTWARDS AND LEFTWARDS OPEN CIRCLE ARROWS} "
                "Queue loop is on — finished tracks return to the end."
            )
        await interaction.response.send_message(embed=embed)

    @staticmethod
    def _queue_description(head_lines: list[str], queue_lines: list[str], queue_size: int) -> str:
        """Assemble the queue embed body, dropping whole lines to fit the limit."""
        hidden = queue_size - len(queue_lines)

        def build() -> str:
            parts = head_lines + queue_lines
            if hidden > 0:
                parts.append(f"…and {hidden} more")
            return "\n".join(parts)

        description = build()
        while queue_lines and len(description) > EMBED_DESCRIPTION_LIMIT:
            queue_lines.pop()
            hidden += 1
            description = build()
        return truncate(description, EMBED_DESCRIPTION_LIMIT)

    @staticmethod
    def _fmt_progress(player: GuildPlayer, track: Track) -> str:
        """`elapsed / total`, degrading to whichever half is known."""
        position = player.position
        if position is None:
            return fmt_duration(track.duration)
        elapsed = fmt_duration(position)
        if track.duration:
            return f"{elapsed} / {fmt_duration(track.duration)}"
        return elapsed

    @app_commands.command(description="Toggle looping the current track (repeats until turned off)")
    @app_commands.guild_only()
    async def loopsong(self, interaction: discord.Interaction) -> None:
        player = self._player_or_error(interaction)
        self._require_same_channel(interaction, player)
        if not player.is_active:
            raise UserError("Nothing is playing right now — start something with `/play` first.")
        player.song_looping = not player.song_looping
        if player.song_looping:
            player.queue_looping = False
            await interaction.response.send_message(
                f"\N{CLOCKWISE RIGHTWARDS AND LEFTWARDS OPEN CIRCLE ARROWS} Looping "
                f"**{esc(truncate(player.now_playing.title, TITLE_DISPLAY_LIMIT))}** — "
                "run `/loopsong` again to turn it off."
            )
        else:
            await interaction.response.send_message(
                "\N{CLOCKWISE RIGHTWARDS AND LEFTWARDS OPEN CIRCLE ARROWS} Song loop off — "
                "the queue will advance normally."
            )

    @app_commands.command(
        description="Toggle looping the whole queue (finished tracks return to the end)"
    )
    @app_commands.guild_only()
    async def loopqueue(self, interaction: discord.Interaction) -> None:
        player = self._player_or_error(interaction)
        self._require_same_channel(interaction, player)
        if not player.is_active:
            raise UserError("Nothing is playing right now — start something with `/play` first.")
        player.queue_looping = not player.queue_looping
        if player.queue_looping:
            player.song_looping = False
            await interaction.response.send_message(
                "\N{CLOCKWISE RIGHTWARDS AND LEFTWARDS OPEN CIRCLE ARROWS} Looping the queue — "
                "finished tracks go back to the end. Run `/loopqueue` again to turn it off."
            )
        else:
            await interaction.response.send_message(
                "\N{CLOCKWISE RIGHTWARDS AND LEFTWARDS OPEN CIRCLE ARROWS} Queue loop off — "
                "the queue will advance normally."
            )

    @app_commands.command(description="Show all commands and what they do")
    async def help(self, interaction: discord.Interaction) -> None:
        cmds = [c for c in self.bot.tree.get_commands() if isinstance(c, app_commands.Command)]
        await interaction.response.send_message(embed=self._help_embed(cmds), ephemeral=True)

    @staticmethod
    def _help_embed(cmds: list[app_commands.Command]) -> discord.Embed:
        embed = discord.Embed(
            title="\N{MULTIPLE MUSICAL NOTES} Commands", color=discord.Color.blurple()
        )
        for cmd in sorted(cmds, key=lambda c: c.name):
            params = " ".join(
                f"<{p.display_name}>" if p.required else f"[{p.display_name}]"
                for p in cmd.parameters
            )
            embed.add_field(
                name=f"/{cmd.name} {params}".strip(), value=cmd.description or "—", inline=False
            )
        return embed

    @app_commands.command(description="Remove a track from the queue by its number or name")
    @app_commands.describe(target="Queue number (from /queue) or part of the track's name")
    @app_commands.guild_only()
    async def remove(self, interaction: discord.Interaction, target: str) -> None:
        player = self.players.get(interaction.guild_id)
        if player is None or not player.queue:
            raise UserError("The queue is empty — nothing to remove.")
        self._require_same_channel(interaction, player)

        target = target.strip()
        if not target:
            raise UserError("Tell me which track to remove — a queue number or part of its name.")
        index = self._find_queue_index(player, target)
        if index is None:
            raise UserError(
                f"Couldn't find anything in the queue matching "
                f"**{esc(truncate(target, TITLE_DISPLAY_LIMIT))}**."
            )
        track = player.remove_at(index)
        await interaction.response.send_message(
            f"\N{WASTEBASKET} Removed #{index + 1}: "
            f"**{esc(truncate(track.title, TITLE_DISPLAY_LIMIT))}**"
        )

    @staticmethod
    def _find_queue_index(player: GuildPlayer, target: str) -> int | None:
        if not target:
            return None  # an empty needle would match every title
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
        """Track the bot's own connection plus who is in its voice channel."""
        player = self.players.get(member.guild.id)
        if player is None:
            return

        if member.id == self.bot.user.id:
            if after.channel is None:
                # Disconnected externally (kicked, channel deleted): clean up.
                await player.destroy()
            else:
                # Moved to another channel: re-check who is listening there.
                self._check_voice_occupancy(player)
            return

        if member.bot:
            return

        bot_channel = player.voice.channel
        if bot_channel is None:
            return
        if before.channel != bot_channel and after.channel != bot_channel:
            return
        self._check_voice_occupancy(player)

    @staticmethod
    def _check_voice_occupancy(player: GuildPlayer) -> None:
        """Pause + start the leave timer when no humans are left; resume otherwise."""
        channel = player.voice.channel
        if channel is None:
            return
        if any(not m.bot for m in channel.members):
            player.channel_became_occupied()
        else:
            player.channel_became_empty()

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
