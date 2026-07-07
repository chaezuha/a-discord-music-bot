"""Per-guild queue and playback loop."""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Callable

import discord

from .sources import SourceError, Track, fmt_duration, resolve_stream

log = logging.getLogger(__name__)

FFMPEG_BEFORE_OPTIONS = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
FFMPEG_OPTIONS = "-vn"


class GuildPlayer:
    """Owns the queue, voice client, and playback loop for one guild."""

    def __init__(
        self,
        bot: discord.Client,
        voice: discord.VoiceClient,
        text_channel: discord.abc.Messageable,
        idle_timeout: float,
        on_destroy: Callable[[], None],
    ):
        self.bot = bot
        self.voice = voice
        self.text_channel = text_channel
        self.idle_timeout = idle_timeout
        self.queue: deque[Track] = deque()
        self.now_playing: Track | None = None
        self._on_destroy = on_destroy
        self._track_added = asyncio.Event()
        self._destroyed = False
        self._task = bot.loop.create_task(self._player_loop())

    # -- public API ----------------------------------------------------

    def enqueue(self, track: Track) -> int:
        """Add a track and return its 1-based position in the upcoming queue."""
        self.queue.append(track)
        self._track_added.set()
        return len(self.queue)

    @property
    def is_active(self) -> bool:
        """True while a track is playing, paused, or being resolved."""
        return self.now_playing is not None

    def skip(self) -> None:
        self.voice.stop()

    def pause(self) -> None:
        self.voice.pause()

    def resume(self) -> None:
        self.voice.resume()

    def remove_at(self, index: int) -> Track:
        track = self.queue[index]
        del self.queue[index]
        return track

    async def destroy(self) -> None:
        """Tear everything down: queue, playback, voice connection."""
        if self._destroyed:
            return
        self._destroyed = True
        self.queue.clear()
        self.now_playing = None
        if self._task is not asyncio.current_task():
            self._task.cancel()
        try:
            self.voice.stop()
            if self.voice.is_connected():
                await self.voice.disconnect(force=True)
        except Exception:
            log.exception("Error while disconnecting voice")
        self._on_destroy()

    # -- internals -----------------------------------------------------

    async def _player_loop(self) -> None:
        try:
            while not self._destroyed:
                self._track_added.clear()
                if not self.queue:
                    try:
                        await asyncio.wait_for(
                            self._track_added.wait(), timeout=self.idle_timeout
                        )
                    except TimeoutError:
                        await self._say(
                            "\N{WAVING HAND SIGN} Nothing has played for a while — disconnecting."
                        )
                        break
                    continue

                track = self.queue.popleft()
                self.now_playing = track
                try:
                    stream_url = await resolve_stream(track)
                except SourceError as exc:
                    await self._say(f"\N{WARNING SIGN} Skipping **{track.title}**: {exc}")
                    self.now_playing = None
                    continue

                if self._destroyed or not self.voice.is_connected():
                    break

                finished = asyncio.Event()

                def _after(error: Exception | None, finished: asyncio.Event = finished) -> None:
                    if error:
                        log.error("Playback error: %s", error)
                    self.bot.loop.call_soon_threadsafe(finished.set)

                source = discord.FFmpegPCMAudio(
                    stream_url,
                    before_options=FFMPEG_BEFORE_OPTIONS,
                    options=FFMPEG_OPTIONS,
                )
                self.voice.play(source, after=_after)
                await self._say(
                    f"\N{MULTIPLE MUSICAL NOTES} Now playing: **{track.title}** "
                    f"({fmt_duration(track.duration)}) — requested by {track.requested_by}"
                )
                await finished.wait()
                self.now_playing = None
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Player loop crashed")
        finally:
            if not self._destroyed:
                await self.destroy()

    async def _say(self, message: str) -> None:
        try:
            await self.text_channel.send(message)
        except discord.HTTPException:
            log.warning("Could not send message to text channel")
