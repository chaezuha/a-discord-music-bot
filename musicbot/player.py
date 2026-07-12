"""Per-guild queue and playback loop."""

from __future__ import annotations

import asyncio
import logging
import random
import shlex
import time
from collections import deque
from collections.abc import Callable

import discord

from .notifier import BreakageNotifier
from .sources import ResolvedStream, SourceError, Track, fmt_duration, fmt_title, resolve_stream

log = logging.getLogger(__name__)

FFMPEG_BEFORE_OPTIONS = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
FFMPEG_OPTIONS = "-vn"


def _before_options(resolved: ResolvedStream) -> str:
    """ffmpeg input options, including the headers the stream URL was signed for.

    Some YouTube clients 403 requests whose User-Agent doesn't match the one
    yt-dlp extracted with. discord.py shlex.split()s this string, so the
    CRLF-joined header blob is quoted to survive as one argument.
    """
    if not resolved.http_headers:
        return FFMPEG_BEFORE_OPTIONS
    blob = "".join(f"{k}: {v}\r\n" for k, v in resolved.http_headers.items())
    return f"{FFMPEG_BEFORE_OPTIONS} -headers {shlex.quote(blob)}"

# A track that "finishes" implausibly fast almost certainly hit a dead stream
# URL: ffmpeg exits on HTTP errors without reporting a playback error, so we
# detect it by elapsed time relative to the advertised duration.
SUSPICIOUS_FINISH_SECONDS = 2.0

# Hard cap on queued tracks per guild.
MAX_QUEUE_SIZE = 500


class QueueFullError(Exception):
    """The guild's queue is at MAX_QUEUE_SIZE."""


def votes_needed(listener_count: int) -> int:
    """Votes required to pass a skip: half the listeners, rounded up (minimum 1)."""
    return max(1, (listener_count + 1) // 2)


def tally_skip_vote(
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
        now_playing_factory: Callable[["GuildPlayer"], tuple[discord.Embed, discord.ui.View]]
        | None = None,
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
        self._np_factory = now_playing_factory
        self._now_message: discord.Message | None = None
        self._now_view: discord.ui.View | None = None
        self._track_added = asyncio.Event()
        self._skip_requested = False
        self._started_at: float | None = None
        self._paused_at: float | None = None
        self._empty_task: asyncio.Task | None = None
        self._prefetch_task: asyncio.Task | None = None
        self._prefetch_track: Track | None = None
        self._destroyed = False
        self._closed = asyncio.Event()
        self._task = bot.loop.create_task(self._player_loop())

    # -- public API ----------------------------------------------------

    def enqueue(self, track: Track, front: bool = False) -> int:
        """Add a track and return its 1-based position in the upcoming queue."""
        if len(self.queue) >= MAX_QUEUE_SIZE:
            raise QueueFullError
        if front:
            self.queue.appendleft(track)
        else:
            self.queue.append(track)
        self._track_added.set()
        if self.now_playing is not None and self.queue[0] is track:
            self._prefetch_next()
        return 1 if front else len(self.queue)

    @property
    def is_active(self) -> bool:
        """True while a track is playing, paused, or being resolved."""
        return self.now_playing is not None

    @property
    def destroyed(self) -> bool:
        return self._destroyed

    @property
    def position(self) -> float | None:
        """Seconds into the current track, or None when nothing is playing."""
        if self.now_playing is None or self._started_at is None:
            return None
        if self._paused_at is not None:
            return self._paused_at - self._started_at
        return time.monotonic() - self._started_at

    def skip(self) -> None:
        if self.now_playing is None:
            # Nothing current: the head of the queue is what "skip" means, and
            # _skip_requested would be reset by the loop before it could act.
            if self.queue:
                track = self.queue.popleft()
                if self.queue_looping:
                    self.queue.append(track)
                self._prefetch_next()
            return
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
        if index == 0:
            self._prefetch_next()  # the old head's prefetch is now stale
        return track

    def move(self, from_index: int, to_index: int) -> Track:
        """Move a queued track to a new index (clamped to the queue's ends)."""
        track = self.queue[from_index]
        del self.queue[from_index]
        self.queue.insert(to_index, track)
        if from_index == 0 or to_index == 0:
            self._prefetch_next()  # the head changed in one direction or the other
        return track

    def shuffle(self) -> None:
        old_head = self.queue[0] if self.queue else None
        random.shuffle(self.queue)
        if self.queue and self.queue[0] is not old_head:
            self._prefetch_next()

    def clear_queue(self) -> int:
        """Drop every queued track (the current one keeps playing)."""
        count = len(self.queue)
        self.queue.clear()
        self._prefetch_next()  # cancels the now-stale head prefetch
        return count

    def remove_where(self, predicate: Callable[[Track], bool]) -> list[Track]:
        """Remove every queued track matching the predicate; returns them in order."""
        removed = [t for t in self.queue if predicate(t)]
        if removed:
            old_head = self.queue[0]
            self.queue = deque(t for t in self.queue if not predicate(t))
            if not self.queue or self.queue[0] is not old_head:
                self._prefetch_next()
        return removed

    async def wait_closed(self) -> None:
        """Block until destroy() has fully finished (voice disconnected)."""
        await self._closed.wait()

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
        if self._prefetch_task is not None:
            self._prefetch_task.cancel()
        await self._clear_now_message()
        try:
            self.voice.stop()
            if self.voice.is_connected():
                await self.voice.disconnect(force=True)
        except Exception:
            log.exception("Error while disconnecting voice")
        self._on_destroy()
        self._closed.set()

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
                        # A track may have arrived while that message was in
                        # flight; it must play rather than be destroyed.
                        if self.queue or self._track_added.is_set():
                            continue
                        break
                    continue

                track = self.queue.popleft()
                replay = False
                retried = False
                while not self._destroyed:
                    self.now_playing = track
                    self._started_at = None
                    self._paused_at = None
                    self._skip_requested = False
                    self.skip_votes.clear()
                    if self._prefetch_task is not None and self._prefetch_track is track:
                        # Let an in-flight prefetch finish instead of racing it
                        # with a duplicate extraction; resolve_stream then hits
                        # its cache (or reports the failure properly).
                        await asyncio.wait({self._prefetch_task})
                    try:
                        # Re-checked on every replay too; cached results are
                        # reused while fresh, re-extracted once they expire.
                        resolved = await resolve_stream(track)
                    except SourceError as exc:
                        if self._notifier is not None:
                            await self._notifier.record_failure(self.bot)
                        await self._say(f"\N{WARNING SIGN} Skipping {fmt_title(track)}: {exc}")
                        self.now_playing = None
                        break

                    if self._skip_requested:
                        # Skipped while resolving: voice.stop() had nothing to
                        # stop, so honor the skip here instead of playing.
                        if self.queue_looping:
                            self.queue.append(track)
                        self.now_playing = None
                        break

                    if self._destroyed or not self.voice.is_connected():
                        break

                    finished = asyncio.Event()
                    error_slot: list[Exception | None] = [None]

                    def _after(
                        error: Exception | None,
                        finished: asyncio.Event = finished,
                        error_slot: list[Exception | None] = error_slot,
                    ) -> None:
                        if error:
                            error_slot[0] = error
                            log.error("Playback error: %s", error)
                        self.bot.loop.call_soon_threadsafe(finished.set)

                    try:
                        # Opus streams are remuxed as-is; anything else is
                        # transcoded by ffmpeg instead of the PCM pipeline.
                        source = discord.FFmpegOpusAudio(
                            resolved.url,
                            codec="copy" if resolved.acodec == "opus" else "libopus",
                            before_options=_before_options(resolved),
                            options=FFMPEG_OPTIONS,
                        )
                        self.voice.play(source, after=_after)
                    except Exception as exc:
                        log.exception("Could not start playback")
                        track.stream = None
                        await self._say(f"\N{WARNING SIGN} Couldn't play {fmt_title(track)}: {exc}")
                        self.now_playing = None
                        break
                    self._started_at = time.monotonic()
                    self._paused_at = None
                    if self._empty_task is not None and not self._empty_task.done():
                        # Everyone left while this track was resolving: don't
                        # play to an empty room. Occupancy events resume it.
                        self.voice.pause()
                        self.auto_paused = True
                        self._mark_paused()
                    self._prefetch_next()
                    if not replay:
                        await self._announce_now_playing(track)
                    await finished.wait()
                    if self._playback_failed(track, error_slot[0]):
                        track.stream = None
                        if not retried:
                            # One shot at a fresh extraction: transient 403s
                            # and dead CDN nodes usually clear on a new URL.
                            retried = True
                            replay = True
                            log.warning(
                                "Playback of %r failed (%s); retrying with a fresh extraction",
                                track.title,
                                error_slot[0] or "early finish",
                            )
                            continue
                        if self._notifier is not None:
                            await self._notifier.record_failure(self.bot)
                        reason = str(error_slot[0]) if error_slot[0] else "the stream cut out"
                        await self._say(
                            f"\N{WARNING SIGN} Playback of {fmt_title(track)} failed: {reason}"
                        )
                        self.now_playing = None
                        break
                    if self._notifier is not None:
                        # Success means audio actually played, not merely that
                        # extraction returned a URL — a 403'd stream would
                        # otherwise reset the breakage counter every track.
                        self._notifier.record_success()
                    retried = False
                    if self.song_looping and not self._skip_requested:
                        replay = True
                        continue
                    if self.queue_looping:
                        self.queue.append(track)
                    self.now_playing = None
                    break
                # The track is over one way or another; its controls must not
                # outlive it, even if nothing else gets announced.
                await self._clear_now_message()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Player loop crashed")
        finally:
            if not self._destroyed:
                await self.destroy()

    def _playback_failed(self, track: Track, error: Exception | None) -> bool:
        """Whether the track's end should be treated as a playback failure.

        ffmpeg exits silently (no error) on dead/expired stream URLs, so an
        implausibly early finish — relative to the advertised duration, when
        known — counts as a failure too.
        """
        if self._destroyed or self._skip_requested:
            return False
        if error is not None:
            return True
        position = self.position
        if position is None:
            return False
        if track.duration:
            threshold = min(SUSPICIOUS_FINISH_SECONDS, track.duration * 0.5)
        else:
            threshold = SUSPICIOUS_FINISH_SECONDS
        return position < threshold

    def _prefetch_next(self) -> None:
        """Resolve the upcoming track's stream while the current one plays."""
        if self._prefetch_task is not None:
            self._prefetch_task.cancel()
            self._prefetch_task = None
            self._prefetch_track = None
        if not self.queue:
            return
        next_track = self.queue[0]

        async def prefetch() -> None:
            try:
                await resolve_stream(next_track)
            except SourceError:
                pass  # the resolve at play time reports the failure
            except Exception:
                log.exception("Prefetch failed")

        self._prefetch_track = next_track
        self._prefetch_task = self.bot.loop.create_task(prefetch())

    async def _empty_channel_timer(self) -> None:
        try:
            await asyncio.sleep(self.idle_timeout)
        except asyncio.CancelledError:
            return
        await self._say(
            "\N{WAVING HAND SIGN} Everyone left the voice channel — disconnecting. Bye!"
        )
        await self.destroy()

    async def _announce_now_playing(self, track: Track) -> None:
        await self._clear_now_message()
        if self._np_factory is None:
            await self._say(
                f"\N{MULTIPLE MUSICAL NOTES} Now playing: {fmt_title(track)} "
                f"({fmt_duration(track.duration)}) — requested by {track.requested_by}"
            )
            return
        embed, view = self._np_factory(self)
        try:
            self._now_message = await self.text_channel.send(embed=embed, view=view)
            self._now_view = view
        except discord.HTTPException:
            view.stop()
            log.warning("Could not send now-playing message")

    async def _clear_now_message(self) -> None:
        """Strip the previous now-playing message's controls, if any."""
        message, view = self._now_message, self._now_view
        self._now_message = self._now_view = None
        if view is not None:
            view.stop()
        if message is not None:
            try:
                await message.edit(view=None)
            except discord.HTTPException:
                pass

    async def _say(self, message: str) -> None:
        try:
            await self.text_channel.send(message)
        except discord.HTTPException:
            log.warning("Could not send message to text channel")
