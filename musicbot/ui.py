"""Interactive components: the search-result picker."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

import discord

from .sources import Track, fmt_duration

log = logging.getLogger(__name__)

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
