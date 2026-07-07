"""Entry point: load config from .env and run the bot."""

import logging
import os
import sys

import discord
from discord.ext import commands
from dotenv import load_dotenv

from musicbot.music import Music

log = logging.getLogger("bot")


class MusicBot(commands.Bot):
    def __init__(self, dev_guild_id: int | None):
        intents = discord.Intents.default()
        super().__init__(command_prefix=commands.when_mentioned, intents=intents)
        self.dev_guild_id = dev_guild_id

    async def setup_hook(self) -> None:
        await self.add_cog(Music(self))
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

    async def on_ready(self) -> None:
        log.info("Logged in as %s (id: %s)", self.user, self.user.id)


def main() -> None:
    load_dotenv()
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        sys.exit(
            "DISCORD_TOKEN is not set.\n"
            "Copy .env.example to .env and fill in your bot token from "
            "https://discord.com/developers/applications"
        )

    dev_guild_raw = os.getenv("DEV_GUILD_ID", "").strip()
    dev_guild_id = int(dev_guild_raw) if dev_guild_raw else None

    bot = MusicBot(dev_guild_id=dev_guild_id)
    bot.run(token, log_level=logging.INFO, root_logger=True)


if __name__ == "__main__":
    main()
