"""Interactive components: search picker, queue browser, and embed builders."""

from __future__ import annotations

import logging
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import discord

from .errors import UserError
from .sources import (
    TITLE_DISPLAY_LIMIT,
    Track,
    fmt_duration,
    fmt_title,
    progress_bar,
    truncate,
)

log = logging.getLogger(__name__)

esc = discord.utils.escape_markdown

QUEUE_PAGE_SIZE = 15
EMBED_DESCRIPTION_LIMIT = 4096

OnPick = Callable[[discord.Interaction, Track], Awaitable[None]]


class SearchPicker(discord.ui.View):
    """Dropdown of up to 10 search results; only the requester may pick."""

    def __init__(self, tracks: list[Track], requester: discord.abc.User, on_pick: OnPick):
        super().__init__(timeout=60)
        self.tracks = tracks[:10]
        self.requester = requester
        self.on_pick = on_pick
        self.message: discord.Message | None = None

        options = [
            discord.SelectOption(
                label=(track.title or "Unknown title")[:100],
                description=f"{track.uploader} ({fmt_duration(track.duration)})"[:100],
                value=str(i),
            )
            for i, track in enumerate(self.tracks)
        ]
        self.select: discord.ui.Select = discord.ui.Select(
            placeholder="Pick a track…", options=options
        )
        self.select.callback = self._on_select
        self.add_item(self.select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester.id:
            await interaction.response.send_message(
                "Only the person who searched can pick a result.", ephemeral=True
            )
            return False
        return True

    async def _on_select(self, interaction: discord.Interaction) -> None:
        track = self.tracks[int(self.select.values[0])]
        self.stop()
        await self.on_pick(interaction, track)

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item,
    ) -> None:
        """Backstop: never leave the picker (or a deferred pick) hanging."""
        log.error("Search picker failed", exc_info=error)
        content = "\N{WARNING SIGN} Something went wrong with that pick."
        try:
            if interaction.response.is_done():
                await interaction.edit_original_response(content=content, view=None)
            else:
                await interaction.response.edit_message(content=content, view=None)
        except discord.HTTPException:
            pass

    async def on_timeout(self) -> None:
        if self.message:
            try:
                await self.message.edit(
                    content="\N{TIMER CLOCK} Search timed out — nothing was picked.",
                    view=None,
                )
            except discord.HTTPException:
                pass


# -- queue embed -----------------------------------------------------------


def clamp_description(head_lines: list[str], queue_lines: list[str], queue_size: int) -> str:
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


def fmt_progress(player, track: Track) -> str:
    """`elapsed / total`, degrading to whichever half is known."""
    position = player.position
    if position is None:
        return fmt_duration(track.duration)
    elapsed = fmt_duration(position)
    if track.duration:
        return f"{elapsed} / {fmt_duration(track.duration)}"
    return elapsed


def queue_totals(tracks: list[Track]) -> tuple[float, bool]:
    """(sum of the known durations, whether every duration was known)."""
    total = 0.0
    all_known = True
    for track in tracks:
        if track.duration:
            total += track.duration
        else:
            all_known = False
    return total, all_known


def queue_wait_seconds(player, index: int) -> float | None:
    """Seconds until queue[index] starts, or None when it can't be estimated
    (an unknown duration on the way there, or the current song is looping)."""
    if player.song_looping:
        return None
    wait = 0.0
    now = player.now_playing
    if now is not None:
        if not now.duration:
            return None
        wait += max(0.0, now.duration - (player.position or 0.0))
    for track in list(player.queue)[:index]:
        if not track.duration:
            return None
        wait += track.duration
    return wait


def _now_playing_line(player) -> str:
    track = player.now_playing
    marker = (
        " \N{CLOCKWISE RIGHTWARDS AND LEFTWARDS OPEN CIRCLE ARROWS} (looping)"
        if player.song_looping
        else ""
    )
    if player.voice.is_paused():
        marker += " \N{DOUBLE VERTICAL BAR} (paused)"
    return (
        f"**Now playing:** {fmt_title(track)} ({fmt_progress(player, track)})"
        f"{marker} — requested by {track.requested_by}\n"
    )


@dataclass
class QueuePage:
    embed: discord.Embed
    page: int  # clamped to the queue's current size
    page_count: int
    tracks: list[Track]  # the tracks shown on this page, by identity


def build_queue_embed(player, page: int) -> QueuePage:
    """Render one page of the live queue (page is clamped into range)."""
    tracks = list(player.queue)
    page_count = max(1, math.ceil(len(tracks) / QUEUE_PAGE_SIZE))
    page = max(0, min(page, page_count - 1))
    start = page * QUEUE_PAGE_SIZE
    page_tracks = tracks[start : start + QUEUE_PAGE_SIZE]

    head_lines = []
    if player.now_playing is not None:
        head_lines.append(_now_playing_line(player))
    queue_lines = [
        f"`{i}.` {fmt_title(track)} ({fmt_duration(track.duration)}) — {track.requested_by}"
        for i, track in enumerate(page_tracks, start=start + 1)
    ]
    description = clamp_description(head_lines, queue_lines, len(tracks) - start)
    embed = discord.Embed(
        title="\N{MULTIPLE MUSICAL NOTES} Queue",
        description=description or "The queue is empty.",
        color=discord.Color.blurple(),
    )

    footer = [f"Page {page + 1}/{page_count}"]
    if tracks:
        total, all_known = queue_totals(tracks)
        plural = "s" if len(tracks) != 1 else ""
        footer.append(f"{len(tracks)} track{plural} — {fmt_duration(total)}{'' if all_known else '+'} queued")
    if player.song_looping:
        footer.append("\N{CLOCKWISE RIGHTWARDS AND LEFTWARDS OPEN CIRCLE ARROWS} song loop on")
    if player.queue_looping:
        footer.append(
            "\N{CLOCKWISE RIGHTWARDS AND LEFTWARDS OPEN CIRCLE ARROWS} queue loop on — "
            "finished tracks return to the end"
        )
    embed.set_footer(text=" • ".join(footer))
    return QueuePage(embed=embed, page=page, page_count=page_count, tracks=page_tracks)


class QueueView(discord.ui.View):
    """Paginated queue browser: Prev/Next buttons plus a remove-a-track menu.

    Every render rebuilds from the live queue, so pages follow along as tracks
    are added, removed, or finish playing. Removal resolves the picked track by
    object identity, never by its (possibly shifted) index.
    """

    def __init__(self, player, controller, *, page: int = 0, timeout: float = 180):
        super().__init__(timeout=timeout)
        self.player = player
        self.controller = controller
        self.page = page
        self.page_tracks: list[Track] = []
        self.message: discord.Message | None = None

        self.prev_button: discord.ui.Button = discord.ui.Button(
            label="\N{BLACK LEFT-POINTING TRIANGLE} Prev", style=discord.ButtonStyle.secondary
        )
        self.next_button: discord.ui.Button = discord.ui.Button(
            label="Next \N{BLACK RIGHT-POINTING TRIANGLE}", style=discord.ButtonStyle.secondary
        )
        self.prev_button.callback = self._on_prev
        self.next_button.callback = self._on_next
        self.add_item(self.prev_button)
        self.add_item(self.next_button)
        self.select: discord.ui.Select = discord.ui.Select(placeholder="Remove a track…")
        self.select.callback = self._on_remove
        # refresh() adds/removes the select depending on whether the page has tracks.

    def refresh(self) -> discord.Embed:
        """Re-render from the live queue; sync page, buttons, and menu options."""
        rendered = build_queue_embed(self.player, self.page)
        self.page = rendered.page
        self.page_tracks = rendered.tracks
        self.prev_button.disabled = rendered.page == 0
        self.next_button.disabled = rendered.page >= rendered.page_count - 1
        self._rebuild_select()
        return rendered.embed

    def _rebuild_select(self) -> None:
        self.remove_item(self.select)
        if not self.page_tracks:
            return  # a Select needs 1-25 options; an empty page gets none
        start = self.page * QUEUE_PAGE_SIZE
        options = []
        for row, track in enumerate(self.page_tracks):
            duration = fmt_duration(track.duration)
            wait = queue_wait_seconds(self.player, start + row)
            if wait is None:
                description = duration
            elif wait < 1:
                description = f"up next — {duration}"
            else:
                description = f"starts in ~{fmt_duration(wait)} — {duration}"
            options.append(
                discord.SelectOption(
                    label=f"{start + row + 1}. {track.title}"[:100],
                    description=description[:100],
                    value=str(row),
                )
            )
        self.select.options = options
        self.add_item(self.select)

    async def _update(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(embed=self.refresh(), view=self)

    async def _on_prev(self, interaction: discord.Interaction) -> None:
        self.page = max(0, self.page - 1)
        await self._update(interaction)

    async def _on_next(self, interaction: discord.Interaction) -> None:
        self.page += 1  # refresh() clamps against the live queue
        await self._update(interaction)

    async def _on_remove(self, interaction: discord.Interaction) -> None:
        await self._remove_row(interaction, int(self.select.values[0]))

    async def _remove_row(self, interaction: discord.Interaction, row: int) -> None:
        try:
            self.controller._require_same_channel(interaction, self.player)
        except UserError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        target = self.page_tracks[row]
        # The queue may have shifted since this page rendered; find the picked
        # track itself rather than trusting its old position.
        index = next((i for i, t in enumerate(self.player.queue) if t is target), None)
        if index is None:
            await self._update(interaction)
            await interaction.followup.send(
                "That track already left the queue — here's the current one.", ephemeral=True
            )
            return
        self.player.remove_at(index)
        await self._update(interaction)
        await interaction.followup.send(
            f"\N{WASTEBASKET} Removed #{index + 1}: "
            f"**{esc(truncate(target.title, TITLE_DISPLAY_LIMIT))}**",
            ephemeral=True,
        )

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item,
    ) -> None:
        """Backstop: view callbacks never reach the cog's error handler."""
        log.error("Queue view failed", exc_info=error)
        content = "\N{WARNING SIGN} Something went wrong with the queue controls."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(content, ephemeral=True)
            else:
                await interaction.response.send_message(content, ephemeral=True)
        except discord.HTTPException:
            pass

    async def on_timeout(self) -> None:
        if self.message:
            try:
                await self.message.edit(view=None)
            except discord.HTTPException:
                pass


# -- now playing -------------------------------------------------------------


async def _reply_ephemeral(interaction: discord.Interaction, content: str) -> None:
    try:
        if interaction.response.is_done():
            await interaction.followup.send(content, ephemeral=True)
        else:
            await interaction.response.send_message(content, ephemeral=True)
    except discord.HTTPException:
        pass


def build_now_playing_embed(player) -> discord.Embed:
    """The rich now-playing card; progress is a snapshot as of this render."""
    track = player.now_playing
    lines = [fmt_title(track)]
    bar = progress_bar(player.position, track.duration)
    if bar:
        lines.append(bar)
    lines.append(f"`{fmt_progress(player, track)}`")
    if player.voice.is_paused():
        lines.append("\N{DOUBLE VERTICAL BAR} Paused")
    embed = discord.Embed(
        title="\N{MULTIPLE MUSICAL NOTES} Now Playing",
        description="\n".join(lines),
        color=discord.Color.blurple(),
    )
    embed.add_field(name="Requested by", value=track.requested_by or "—", inline=True)
    if track.uploader:
        embed.add_field(name="Uploader", value=esc(truncate(track.uploader, 100)), inline=True)
    queue_size = len(player.queue)
    up_next = f"{queue_size} track{'s' if queue_size != 1 else ''}"
    if queue_size:
        total, all_known = queue_totals(list(player.queue))
        up_next += f" ({fmt_duration(total)}{'' if all_known else '+'})"
    embed.add_field(name="Up next", value=up_next, inline=True)
    loop_state = "Song" if player.song_looping else "Queue" if player.queue_looping else "Off"
    embed.add_field(name="Loop", value=loop_state, inline=True)
    if track.thumbnail:
        embed.set_thumbnail(url=track.thumbnail)
    return embed


class NowPlayingView(discord.ui.View):
    """Playback controls under the now-playing card.

    The player owns this view's lifecycle (timeout=None): it strips the view
    when the track changes or the player is destroyed, and interaction_check
    catches any stale message that slips through. All real work happens in the
    controller (the Music cog); callbacks here only route and report errors,
    because view callbacks never reach cog_app_command_error.
    """

    def __init__(self, player, controller):
        super().__init__(timeout=None)
        self.player = player
        self.controller = controller
        self._sync_pause_button()

    def _sync_pause_button(self) -> None:
        self.pause_button.label = "Resume" if self.player.voice.is_paused() else "Pause"

    async def refresh(self, interaction: discord.Interaction) -> None:
        """Re-render the card in place — the on-interaction progress update."""
        self._sync_pause_button()
        await interaction.response.edit_message(
            embed=build_now_playing_embed(self.player), view=self
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.player.destroyed or self.player.now_playing is None:
            await _reply_ephemeral(interaction, "That track is over — these controls are stale.")
            self.stop()
            message = getattr(interaction, "message", None)
            if message is not None:
                try:
                    await message.edit(view=None)
                except discord.HTTPException:
                    pass
            return False
        try:
            self.controller._require_same_channel(interaction, self.player)
        except UserError as exc:
            await _reply_ephemeral(interaction, str(exc))
            return False
        return True

    async def _run(self, interaction: discord.Interaction, action) -> None:
        try:
            await action
        except UserError as exc:
            await _reply_ephemeral(interaction, str(exc))

    @discord.ui.button(label="Pause", style=discord.ButtonStyle.primary, row=0)
    async def pause_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self._run(interaction, self.controller.np_pause_resume(interaction, self.player, self))

    @discord.ui.button(label="Skip", style=discord.ButtonStyle.secondary, row=0)
    async def skip_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self._run(interaction, self.controller.np_skip(interaction, self.player))

    @discord.ui.button(label="Loop", style=discord.ButtonStyle.secondary, row=0)
    async def loop_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self._run(interaction, self.controller.np_loop(interaction, self.player, self))

    @discord.ui.button(label="Queue", style=discord.ButtonStyle.secondary, row=0)
    async def queue_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self._run(interaction, self.controller.np_queue(interaction, self.player))

    @discord.ui.button(label="Stop", style=discord.ButtonStyle.danger, row=0)
    async def stop_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self._run(interaction, self.controller.np_stop(interaction, self.player))

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item,
    ) -> None:
        """Backstop: view callbacks never reach the cog's error handler."""
        log.error("Now-playing controls failed", exc_info=error)
        await _reply_ephemeral(
            interaction, "\N{WARNING SIGN} Something went wrong with that control."
        )
