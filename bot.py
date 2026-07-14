"""Entry point: load config from .env and run the bot."""

import asyncio
import faulthandler
import logging
import logging.handlers
import math
import os
import sys
import time

import discord
from discord.ext import commands
from dotenv import load_dotenv

from musicbot.music import Music, env_id, env_idle_timeout

log = logging.getLogger("bot")

WATCHDOG_CHECK_INTERVAL_SECONDS = 30

# Held open for the life of the process: faulthandler writes to this fd on a
# hard crash, so it must never be garbage-collected and closed.
_faulthandler_file = None


def env_watchdog_seconds(default: float = 0.0) -> float:
    """Parse WATCHDOG_DISCONNECT_SECONDS: finite, non-negative; 0 disables."""
    raw = (os.getenv("WATCHDOG_DISCONNECT_SECONDS") or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(
            f"WATCHDOG_DISCONNECT_SECONDS must be a number of seconds, got {raw!r}."
        ) from None
    if not math.isfinite(value) or value < 0:
        raise ValueError(
            "WATCHDOG_DISCONNECT_SECONDS must be a finite, non-negative number of "
            f"seconds (0 disables the watchdog), got {raw!r}."
        )
    return value


def setup_logging() -> None:
    """Log to stderr (as before) plus a rotating file under LOG_DIR."""
    discord.utils.setup_logging(root=True)

    global _faulthandler_file
    log_dir = os.getenv("LOG_DIR") or "logs"
    try:
        os.makedirs(log_dir, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            os.path.join(log_dir, "bot.log"),
            maxBytes=2 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        # Same format discord.utils.setup_logging puts on stderr, so the two
        # outputs correlate line for line.
        file_handler.setFormatter(
            logging.Formatter(
                "[{asctime}] [{levelname:<8}] {name}: {message}",
                "%Y-%m-%d %H:%M:%S",
                style="{",
            )
        )
        logging.getLogger().addHandler(file_handler)
        _faulthandler_file = open(os.path.join(log_dir, "faulthandler.log"), "a", encoding="utf-8")
        faulthandler.enable(file=_faulthandler_file, all_threads=True)
    except OSError as exc:
        log.warning(
            "Cannot write log files under %r (%s); continuing with console logging only",
            log_dir,
            exc,
        )
        faulthandler.enable()


def _terminate(code: int) -> None:
    # os._exit rather than close(): a graceful shutdown can hang on the very
    # network problem the watchdog just detected, and Discord drops the
    # session server-side anyway.
    os._exit(code)


class MusicBot(commands.Bot):
    def __init__(self, dev_guild_id: int | None, watchdog_seconds: float = 0.0):
        intents = discord.Intents.default()
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
            # User-supplied text (queries, titles, names) is echoed back;
            # never let it ping anyone.
            allowed_mentions=discord.AllowedMentions.none(),
        )
        self.dev_guild_id = dev_guild_id
        self.watchdog_seconds = watchdog_seconds
        self._disconnected_since: float | None = None
        self._watchdog_task: asyncio.Task | None = None

    async def setup_hook(self) -> None:
        await self.add_cog(Music(self))
        self._start_watchdog()
        if self.dev_guild_id:
            guild = discord.Object(id=self.dev_guild_id)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            log.info("Synced %d commands to dev guild %d", len(synced), self.dev_guild_id)
        else:
            synced = await self.tree.sync()
            log.info(
                "Synced %d global commands (new commands can take up to an hour to appear)",
                len(synced),
            )

    def _start_watchdog(self) -> None:
        if self.watchdog_seconds:
            self._watchdog_task = asyncio.get_running_loop().create_task(self._watchdog())

    async def on_connect(self) -> None:
        # Deliberately does NOT clear _disconnected_since: a broken reconnect
        # loop can get this far over and over without the session ever
        # becoming functional. Only on_resumed/on_ready mean "recovered".
        log.info("Gateway connected")

    async def on_disconnect(self) -> None:
        # Fires again on every failed reconnect attempt; keep the first
        # timestamp so outage duration is measured from when it began.
        if self._disconnected_since is None:
            self._disconnected_since = time.monotonic()
            log.warning("Gateway disconnected")

    async def on_resumed(self) -> None:
        self._note_recovery("resumed")

    async def on_ready(self) -> None:
        self._note_recovery("re-established")
        log.info("Logged in as %s (id: %s)", self.user, self.user.id)

    def _note_recovery(self, how: str) -> None:
        if self._disconnected_since is not None:
            offline = time.monotonic() - self._disconnected_since
            self._disconnected_since = None
            log.info("Gateway session %s after %.0fs offline", how, offline)

    async def _watchdog(self) -> None:
        """Exit when stuck offline so a supervisor can restart us fresh.

        This is a maximum-outage policy, not stall detection: discord.py's
        exponential backoff between reconnect attempts is legitimate, but
        past the threshold a guaranteed-fresh process beats waiting on a
        session that may never come back (e.g. after a router restart).
        Runs on the event loop, so it cannot catch a fully blocked loop.
        """
        while not self.is_closed():
            await asyncio.sleep(WATCHDOG_CHECK_INTERVAL_SECONDS)
            if (
                self._disconnected_since is not None
                and time.monotonic() - self._disconnected_since > self.watchdog_seconds
            ):
                log.critical(
                    "Gateway disconnected for over %.0fs (WATCHDOG_DISCONNECT_SECONDS=%.0f); "
                    "exiting so the supervisor can restart the bot",
                    time.monotonic() - self._disconnected_since,
                    self.watchdog_seconds,
                )
                logging.shutdown()  # flush bot.log before the hard exit
                _terminate(70)


def main() -> None:
    load_dotenv()
    setup_logging()
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        sys.exit(
            "DISCORD_TOKEN is not set.\n"
            "Copy .env.example to .env and fill in your bot token from "
            "https://discord.com/developers/applications"
        )

    try:
        dev_guild_id = env_id("DEV_GUILD_ID")
        env_id("OWNER_ID")  # validated here for a clear startup error
        env_idle_timeout()
        watchdog_seconds = env_watchdog_seconds()
    except ValueError as exc:
        sys.exit(f"Configuration error: {exc}")

    bot = MusicBot(dev_guild_id=dev_guild_id, watchdog_seconds=watchdog_seconds)
    try:
        bot.run(token, log_handler=None)
    except Exception:
        log.critical("Bot crashed with an unhandled exception", exc_info=True)
        logging.shutdown()
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
