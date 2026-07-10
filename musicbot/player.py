"""Per-guild queue and playback loop."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Callable

import discord

from .notifier import BreakageNotifier
from .sources import SourceError, Track, fmt_duration, fmt_title, resolve_stream

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
        notifier: BreakageNotifier | None = None,
    ):
        self.bot = bot
        self.voice = voice
        self.text_channel = text_channel
        self.idle_timeout = idle_timeout
        self.queue: deque[Track] = deque()
        self.now_playing: Track | None = None
        self.song_looping = False
        self.queue_looping = False
        self.skip_votes: set[int] = set()
        self.auto_paused = False
        self._on_destroy = on_destroy
        self._notifier = notifier
        self._track_added = asyncio.Event()
        self._skip_requested = False
        self._started_at: float | None = None
        self._paused_at: float | None = None
        self._empty_task: asyncio.Task | None = None
        self._destroyed = False
        self._task = bot.loop.create_task(self._player_loop())

    # -- public API ----------------------------------------------------

    def enqueue(self, track: Track, front: bool = False) -> int:
        """Add a track and return its 1-based position in the upcoming queue."""
        if front:
            self.queue.appendleft(track)
        else:
            self.queue.append(track)
        self._track_added.set()
        return 1 if front else len(self.queue)

    @property
    def is_active(self) -> bool:
        """True while a track is playing, paused, or being resolved."""
        return self.now_playing is not None

    @property
    def position(self) -> float | None:
        """Seconds into the current track, or None when nothing is playing."""
        if self.now_playing is None or self._started_at is None:
            return None
        if self._paused_at is not None:
            return self._paused_at - self._started_at
        return time.monotonic() - self._started_at

    def skip(self) -> None:
        self._skip_requested = True
        self.voice.stop()

    def pause(self) -> None:
        self.auto_paused = False
        self.voice.pause()
        self._mark_paused()

    def resume(self) -> None:
        self.auto_paused = False
        self.voice.resume()
        self._mark_resumed()

    def channel_became_empty(self) -> None:
        """All humans left the voice channel: pause and start the leave timer."""
        if self.voice.is_playing():
            self.voice.pause()
            self.auto_paused = True
            self._mark_paused()
        if self._empty_task is None or self._empty_task.done():
            self._empty_task = self.bot.loop.create_task(self._empty_channel_timer())

    def channel_became_occupied(self) -> None:
        """A human is in the voice channel: cancel the timer, undo any auto-pause."""
        if self._empty_task is not None:
            self._empty_task.cancel()
            self._empty_task = None
        if self.auto_paused:
            self.auto_paused = False
            if self.voice.is_paused():
                self.voice.resume()
                self._mark_resumed()

    def _mark_paused(self) -> None:
        if self._paused_at is None:
            self._paused_at = time.monotonic()

    def _mark_resumed(self) -> None:
        if self._paused_at is not None and self._started_at is not None:
            self._started_at += time.monotonic() - self._paused_at
        self._paused_at = None

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
        if self._empty_task is not None and self._empty_task is not asyncio.current_task():
            self._empty_task.cancel()
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
                        await asyncio.wait_for(self._track_added.wait(), timeout=self.idle_timeout)
                    except asyncio.TimeoutError:
                        await self._say(
                            "\N{WAVING HAND SIGN} Nothing has played for a while — disconnecting."
                        )
                        break
                    continue

                track = self.queue.popleft()
                replay = False
                while not self._destroyed:
                    self.now_playing = track
                    self._skip_requested = False
                    self.skip_votes.clear()
                    try:
                        # Resolved on every replay too: stream URLs expire.
                        stream_url = await resolve_stream(track)
                    except SourceError as exc:
                        if self._notifier is not None:
                            await self._notifier.record_failure(self.bot)
                        await self._say(f"\N{WARNING SIGN} Skipping **{track.title}**: {exc}")
                        self.now_playing = None
                        break
                    if self._notifier is not None:
                        self._notifier.record_success()

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
                    self._started_at = time.monotonic()
                    self._paused_at = None
                    if not replay:
                        await self._say(
                            f"\N{MULTIPLE MUSICAL NOTES} Now playing: {fmt_title(track)} "
                            f"({fmt_duration(track.duration)}) — requested by {track.requested_by}"
                        )
                    await finished.wait()
                    if self.song_looping and not self._skip_requested:
                        replay = True
                        continue
                    if self.queue_looping:
                        self.queue.append(track)
                    self.now_playing = None
                    break
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Player loop crashed")
        finally:
            if not self._destroyed:
                await self.destroy()

    async def _empty_channel_timer(self) -> None:
        try:
            await asyncio.sleep(self.idle_timeout)
        except asyncio.CancelledError:
            return
        await self._say(
            "\N{WAVING HAND SIGN} Everyone left the voice channel — disconnecting. Bye!"
        )
        await self.destroy()

    async def _say(self, message: str) -> None:
        try:
            await self.text_channel.send(message)
        except discord.HTTPException:
            log.warning("Could not send message to text channel")
