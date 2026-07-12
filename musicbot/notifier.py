"""DM the owner when consecutive extraction failures suggest yt-dlp is broken."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

import discord

log = logging.getLogger(__name__)

FAILURE_THRESHOLD = 3
DM_COOLDOWN_SECONDS = 24 * 60 * 60

BREAKAGE_MESSAGE = (
    "\N{WARNING SIGN} Your music bot has hit several extraction failures in a row. "
    "yt-dlp is probably out of date. Update it (`docker compose up -d` to pull a "
    "fresh image — images are rebuilt weekly — or `pip install -U yt-dlp`) and "
    "restart the bot."
)


class BreakageNotifier:
    """Counts consecutive source failures across all guilds and DMs the owner."""

    def __init__(
        self,
        owner_id: int | None,
        threshold: int = FAILURE_THRESHOLD,
        cooldown: float = DM_COOLDOWN_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.owner_id = owner_id
        self.threshold = threshold
        self.cooldown = cooldown
        self._clock = clock
        self.failures = 0
        self._last_dm: float | None = None

    def record_success(self) -> None:
        self.failures = 0

    async def record_failure(self, bot: discord.Client) -> None:
        self.failures += 1
        if self.owner_id is None or self.failures < self.threshold:
            return
        now = self._clock()
        if self._last_dm is not None and now - self._last_dm < self.cooldown:
            return
        # Reserve the cooldown slot before awaiting so concurrent failures
        # can't double-DM; roll it back if the DM never went out.
        previous = self._last_dm
        self._last_dm = now
        try:
            user = await bot.fetch_user(self.owner_id)
            await user.send(BREAKAGE_MESSAGE)
        except discord.HTTPException:
            self._last_dm = previous
            log.warning("Could not DM owner %s about breakage", self.owner_id)
