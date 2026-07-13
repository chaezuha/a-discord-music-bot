"""Async tests for the GuildPlayer queue/playback/idle loop, all against fakes."""

from __future__ import annotations

import asyncio
import shlex
from types import SimpleNamespace

import discord
import pytest

from musicbot import player as player_module
from musicbot.notifier import BreakageNotifier
from musicbot.player import QueueFullError
from musicbot.sources import ResolvedStream, SourceError

from .conftest import fake_stream


async def test_plays_enqueued_track(make_player, voice, channel, track_factory, wait_until):
    player, _ = make_player()
    position = player.enqueue(track_factory("Song A"))
    assert position == 1

    await wait_until(lambda: voice.played_sources)
    assert voice.played_sources == ["stream://Song A"]
    assert player.now_playing is not None and player.now_playing.title == "Song A"
    assert any("Now playing" in m and "Song A" in m for m in channel.messages)

    voice.finish_track()
    await wait_until(lambda: player.now_playing is None)


async def test_queue_is_fifo(make_player, voice, track_factory, wait_until):
    player, _ = make_player()
    player.enqueue(track_factory("Song A"))
    await wait_until(lambda: voice.played_sources)

    position = player.enqueue(track_factory("Song B"))
    assert position == 1  # first in the *upcoming* queue while A plays
    assert player.is_active

    voice.finish_track()
    await wait_until(lambda: len(voice.played_sources) == 2)
    assert voice.played_sources == ["stream://Song A", "stream://Song B"]


async def test_skip_advances_to_next_track(make_player, voice, track_factory, wait_until):
    player, _ = make_player()
    player.enqueue(track_factory("Song A"))
    await wait_until(lambda: voice.played_sources)
    player.enqueue(track_factory("Song B"))

    player.skip()
    await wait_until(lambda: len(voice.played_sources) == 2)
    assert voice.stop_calls == 1
    assert player.now_playing.title == "Song B"


async def test_idle_timeout_disconnects(make_player, voice, channel, wait_until):
    player, destroyed = make_player(idle_timeout=0.05)

    await wait_until(lambda: voice.disconnect_calls == 1)
    assert destroyed == [True]
    assert player._destroyed
    assert not player.queue
    assert any("disconnecting" in m.lower() for m in channel.messages)


async def test_resolve_failure_skips_to_next(
    make_player, voice, channel, track_factory, wait_until
):
    async def resolve(track):
        if track.title == "Broken":
            raise SourceError("boom")
        return fake_stream(track)

    player, _ = make_player(resolve=resolve)
    player.enqueue(track_factory("Broken"))
    player.enqueue(track_factory("Working"))

    await wait_until(lambda: voice.played_sources)
    assert voice.played_sources == ["stream://Working"]
    assert any("Skipping" in m and "Broken" in m for m in channel.messages)


async def test_destroy_is_idempotent(make_player, voice, track_factory, wait_until):
    player, destroyed = make_player()
    player.enqueue(track_factory("Song A"))
    await wait_until(lambda: voice.played_sources)
    player.enqueue(track_factory("Song B"))

    await player.destroy()
    assert not player.queue
    assert player.now_playing is None
    assert voice.disconnect_calls == 1
    assert destroyed == [True]

    await player.destroy()
    assert voice.disconnect_calls == 1
    assert destroyed == [True]


async def test_remove_at(make_player, voice, track_factory, wait_until):
    player, _ = make_player()
    player.enqueue(track_factory("Song A"))
    await wait_until(lambda: voice.played_sources)
    player.enqueue(track_factory("Song B"))
    player.enqueue(track_factory("Song C"))

    removed = player.remove_at(0)
    assert removed.title == "Song B"
    assert [t.title for t in player.queue] == ["Song C"]


async def test_enqueue_front_jumps_queue(make_player, voice, track_factory, wait_until):
    player, _ = make_player()
    player.enqueue(track_factory("Song A"))
    await wait_until(lambda: voice.played_sources)
    player.enqueue(track_factory("Song B"))

    position = player.enqueue(track_factory("Song C"), front=True)
    assert position == 1
    assert [t.title for t in player.queue] == ["Song C", "Song B"]

    voice.finish_track()
    await wait_until(lambda: len(voice.played_sources) == 2)
    assert voice.played_sources == ["stream://Song A", "stream://Song C"]


async def test_loop_replays_current_track(make_player, voice, channel, track_factory, wait_until):
    player, _ = make_player()
    player.enqueue(track_factory("Song A"))
    await wait_until(lambda: voice.played_sources)
    player.enqueue(track_factory("Song B"))
    player.song_looping = True

    voice.finish_track()
    await wait_until(lambda: len(voice.played_sources) == 2)
    assert voice.played_sources == ["stream://Song A", "stream://Song A"]
    assert [t.title for t in player.queue] == ["Song B"]
    assert sum("Now playing" in m for m in channel.messages) == 1


async def test_loop_re_resolves_stream(make_player, voice, track_factory, wait_until):
    calls = []

    async def resolve(track):
        calls.append(track.title)
        return fake_stream(track)

    player, _ = make_player(resolve=resolve)
    player.enqueue(track_factory("Song A"))
    await wait_until(lambda: voice.played_sources)
    player.song_looping = True

    voice.finish_track()
    await wait_until(lambda: len(voice.played_sources) == 2)
    assert calls == ["Song A", "Song A"]


async def test_skip_while_looping_advances_and_keeps_loop(
    make_player, voice, track_factory, wait_until
):
    player, _ = make_player()
    player.enqueue(track_factory("Song A"))
    await wait_until(lambda: voice.played_sources)
    player.enqueue(track_factory("Song B"))
    player.song_looping = True

    player.skip()
    await wait_until(lambda: len(voice.played_sources) == 2)
    assert voice.played_sources == ["stream://Song A", "stream://Song B"]
    assert player.song_looping


async def test_loop_off_advances_normally(make_player, voice, track_factory, wait_until):
    player, _ = make_player()
    player.enqueue(track_factory("Song A"))
    await wait_until(lambda: voice.played_sources)
    player.enqueue(track_factory("Song B"))
    player.song_looping = True
    player.song_looping = False

    voice.finish_track()
    await wait_until(lambda: len(voice.played_sources) == 2)
    assert voice.played_sources == ["stream://Song A", "stream://Song B"]


async def test_queue_loop_reappends_finished_track(make_player, voice, track_factory, wait_until):
    player, _ = make_player()
    player.enqueue(track_factory("Song A"))
    await wait_until(lambda: voice.played_sources)
    player.enqueue(track_factory("Song B"))
    player.queue_looping = True

    voice.finish_track()
    await wait_until(lambda: len(voice.played_sources) == 2)
    assert voice.played_sources == ["stream://Song A", "stream://Song B"]
    assert [t.title for t in player.queue] == ["Song A"]


async def test_queue_loop_reappends_skipped_track(make_player, voice, track_factory, wait_until):
    player, _ = make_player()
    player.enqueue(track_factory("Song A"))
    await wait_until(lambda: voice.played_sources)
    player.enqueue(track_factory("Song B"))
    player.queue_looping = True

    player.skip()
    await wait_until(lambda: len(voice.played_sources) == 2)
    assert voice.played_sources == ["stream://Song A", "stream://Song B"]
    assert [t.title for t in player.queue] == ["Song A"]


async def test_queue_loop_single_track_cycles(make_player, voice, track_factory, wait_until):
    player, _ = make_player()
    player.enqueue(track_factory("Song A"))
    await wait_until(lambda: voice.played_sources)
    player.queue_looping = True

    voice.finish_track()
    await wait_until(lambda: len(voice.played_sources) == 2)
    assert voice.played_sources == ["stream://Song A", "stream://Song A"]


async def test_queue_loop_does_not_reappend_failed_track(
    make_player, voice, track_factory, wait_until
):
    async def resolve(track):
        if track.title == "Broken":
            raise SourceError("boom")
        return fake_stream(track)

    player, _ = make_player(resolve=resolve)
    player.queue_looping = True
    player.enqueue(track_factory("Broken"))
    player.enqueue(track_factory("Working"))

    await wait_until(lambda: voice.played_sources)
    assert voice.played_sources == ["stream://Working"]
    assert [t.title for t in player.queue] == []


async def test_song_loop_wins_over_queue_loop(make_player, voice, track_factory, wait_until):
    player, _ = make_player()
    player.enqueue(track_factory("Song A"))
    await wait_until(lambda: voice.played_sources)
    player.enqueue(track_factory("Song B"))
    player.song_looping = True
    player.queue_looping = True

    voice.finish_track()
    await wait_until(lambda: len(voice.played_sources) == 2)
    assert voice.played_sources == ["stream://Song A", "stream://Song A"]
    assert [t.title for t in player.queue] == ["Song B"]


async def test_skip_votes_reset_between_tracks(make_player, voice, track_factory, wait_until):
    player, _ = make_player()
    player.enqueue(track_factory("Song A"))
    await wait_until(lambda: voice.played_sources)
    player.skip_votes.add(123)
    player.enqueue(track_factory("Song B"))

    voice.finish_track()
    await wait_until(lambda: len(voice.played_sources) == 2)
    assert player.skip_votes == set()


async def test_channel_empty_auto_pauses_and_resumes(make_player, voice, track_factory, wait_until):
    player, _ = make_player()
    player.enqueue(track_factory("Song A"))
    await wait_until(lambda: voice.played_sources)

    player.channel_became_empty()
    assert voice.paused
    assert player.auto_paused

    player.channel_became_occupied()
    assert not voice.paused
    assert not player.auto_paused


async def test_manual_pause_survives_empty_and_rejoin(
    make_player, voice, track_factory, wait_until
):
    player, _ = make_player()
    player.enqueue(track_factory("Song A"))
    await wait_until(lambda: voice.played_sources)

    player.pause()
    player.channel_became_empty()
    assert not player.auto_paused

    player.channel_became_occupied()
    assert voice.paused  # a manual pause is not undone by someone rejoining


async def test_empty_channel_timeout_destroys(
    make_player, voice, channel, track_factory, wait_until
):
    player, destroyed = make_player(idle_timeout=0.05)
    player.enqueue(track_factory("Song A"))
    await wait_until(lambda: voice.played_sources)  # playing, so the queue-idle timer is off

    player.channel_became_empty()
    await wait_until(lambda: voice.disconnect_calls == 1)
    assert destroyed == [True]
    assert any("Everyone left" in m for m in channel.messages)


async def test_rejoin_cancels_empty_timer(make_player, voice, track_factory, wait_until):
    player, destroyed = make_player(idle_timeout=0.05)
    player.enqueue(track_factory("Song A"))
    await wait_until(lambda: voice.played_sources)

    player.channel_became_empty()
    player.channel_became_occupied()
    await asyncio.sleep(0.1)
    assert voice.disconnect_calls == 0
    assert destroyed == []


async def test_skip_with_empty_queue_stops_but_stays_connected(
    make_player, voice, track_factory, wait_until
):
    player, destroyed = make_player()
    player.enqueue(track_factory("Song A"))
    await wait_until(lambda: voice.played_sources)

    player.skip()
    await wait_until(lambda: player.now_playing is None)
    await asyncio.sleep(0.05)
    assert voice.disconnect_calls == 0
    assert destroyed == []

    # Still alive: a new track starts playback again.
    player.enqueue(track_factory("Song B"))
    await wait_until(lambda: len(voice.played_sources) == 2)
    assert player.now_playing.title == "Song B"


async def test_position_tracks_elapsed_time(make_player, voice, track_factory, wait_until):
    player, _ = make_player()
    assert player.position is None

    player.enqueue(track_factory("Song A"))
    await wait_until(lambda: voice.played_sources)
    assert player.position is not None and player.position >= 0

    await asyncio.sleep(0.05)
    assert player.position >= 0.05

    voice.finish_track()
    await wait_until(lambda: player.now_playing is None)
    assert player.position is None


async def test_position_freezes_while_paused(make_player, voice, track_factory, wait_until):
    player, _ = make_player()
    player.enqueue(track_factory("Song A"))
    await wait_until(lambda: voice.played_sources)

    player.pause()
    frozen = player.position
    await asyncio.sleep(0.2)
    assert player.position == frozen

    player.resume()
    await asyncio.sleep(0.05)
    resumed = player.position
    # The 0.2s spent paused must not count toward the position.
    assert frozen < resumed < frozen + 0.2


async def test_position_freezes_during_auto_pause(make_player, voice, track_factory, wait_until):
    player, _ = make_player()
    player.enqueue(track_factory("Song A"))
    await wait_until(lambda: voice.played_sources)

    player.channel_became_empty()
    frozen = player.position
    await asyncio.sleep(0.05)
    assert player.position == frozen

    player.channel_became_occupied()
    await asyncio.sleep(0.05)
    assert player.position > frozen


async def test_resolve_failure_notifies_owner(make_player, voice, track_factory, wait_until):
    async def resolve(track):
        raise SourceError("boom")

    notifier = BreakageNotifier(owner_id=1, threshold=1)
    player, _ = make_player(resolve=resolve, notifier=notifier)
    player.enqueue(track_factory("Broken"))

    await wait_until(lambda: player.bot.owner.dms)
    assert "yt-dlp" in player.bot.owner.dms[0]


async def test_skip_during_resolution_skips_track(make_player, voice, track_factory, wait_until):
    gate = asyncio.Event()
    resolving = asyncio.Event()

    async def resolve(track):
        if track.title == "Song A" and not gate.is_set():
            resolving.set()
            await gate.wait()
        return fake_stream(track)

    player, _ = make_player(resolve=resolve)
    player.enqueue(track_factory("Song A"))
    player.enqueue(track_factory("Song B"))

    await resolving.wait()
    player.skip()  # voice.stop() is a no-op here: nothing is playing yet
    gate.set()

    await wait_until(lambda: voice.played_sources)
    assert voice.played_sources == ["stream://Song B"]


async def test_skip_during_resolution_requeues_when_queue_looping(
    make_player, voice, track_factory, wait_until
):
    gate = asyncio.Event()
    resolving = asyncio.Event()

    async def resolve(track):
        if track.title == "Song A" and not gate.is_set():
            resolving.set()
            await gate.wait()
        return fake_stream(track)

    player, _ = make_player(resolve=resolve)
    player.queue_looping = True
    player.enqueue(track_factory("Song A"))
    player.enqueue(track_factory("Song B"))

    await resolving.wait()
    player.skip()
    gate.set()

    await wait_until(lambda: voice.played_sources)
    assert voice.played_sources == ["stream://Song B"]
    assert [t.title for t in player.queue] == ["Song A"]


async def test_playback_error_notifies_and_does_not_requeue(
    make_player, voice, channel, track_factory, wait_until
):
    player, _ = make_player()
    player.queue_looping = True
    player.enqueue(track_factory("Song A"))
    await wait_until(lambda: voice.played_sources)
    player.enqueue(track_factory("Song B"))

    voice.finish_track(error=RuntimeError("ffmpeg exploded"))
    # The first failure retries the same track on a fresh extraction.
    await wait_until(lambda: len(voice.played_sources) == 2)
    assert voice.played_sources[1] == "stream://Song A"

    voice.finish_track(error=RuntimeError("ffmpeg exploded"))
    await wait_until(lambda: len(voice.played_sources) == 3)
    assert voice.played_sources[2] == "stream://Song B"
    assert any("failed" in m and "ffmpeg exploded" in m for m in channel.messages)
    # The broken track must not come back around via the queue loop.
    assert [t.title for t in player.queue] == []


async def test_playback_error_stops_song_loop(
    make_player, voice, channel, track_factory, wait_until
):
    player, _ = make_player()
    player.enqueue(track_factory("Song A"))
    await wait_until(lambda: voice.played_sources)
    player.song_looping = True

    voice.finish_track(error=RuntimeError("boom"))
    await wait_until(lambda: len(voice.played_sources) == 2)  # the one retry
    voice.finish_track(error=RuntimeError("boom"))
    await wait_until(lambda: player.now_playing is None)
    assert voice.played_sources == ["stream://Song A", "stream://Song A"]
    assert any("failed" in m for m in channel.messages)


async def test_playback_failure_retries_once_silently(
    make_player, voice, channel, track_factory, wait_until
):
    calls = []

    async def resolve(track):
        calls.append(track.title)
        return fake_stream(track)

    player, _ = make_player(resolve=resolve)
    player.enqueue(track_factory("Song A"))
    await wait_until(lambda: voice.played_sources)

    voice.finish_track(error=RuntimeError("403"))
    await wait_until(lambda: len(voice.played_sources) == 2)
    # The retry re-resolved (the bad cached URL was dropped) and stayed quiet.
    assert calls == ["Song A", "Song A"]
    assert not any("failed" in m for m in channel.messages)
    assert sum("Now playing" in m for m in channel.messages) == 1

    voice.finish_track()  # the retry plays through to the end
    await wait_until(lambda: player.now_playing is None)
    assert not any("failed" in m for m in channel.messages)


async def test_double_playback_failure_notifies_owner(
    make_player, voice, track_factory, wait_until
):
    notifier = BreakageNotifier(owner_id=1, threshold=1)
    player, _ = make_player(notifier=notifier)
    player.enqueue(track_factory("Song A"))
    await wait_until(lambda: voice.played_sources)

    voice.finish_track(error=RuntimeError("403"))
    await wait_until(lambda: len(voice.played_sources) == 2)
    assert not player.bot.owner.dms  # a single failure is not breakage

    voice.finish_track(error=RuntimeError("403"))
    await wait_until(lambda: player.bot.owner.dms)
    assert "yt-dlp" in player.bot.owner.dms[0]


async def test_successful_play_resets_retry_budget(
    make_player, voice, channel, track_factory, wait_until
):
    player, _ = make_player()
    player.enqueue(track_factory("Song A"))
    await wait_until(lambda: voice.played_sources)
    player.song_looping = True

    voice.finish_track(error=RuntimeError("boom"))  # failure -> retry
    await wait_until(lambda: len(voice.played_sources) == 2)
    voice.finish_track()  # retry plays fully; song loop replays
    await wait_until(lambda: len(voice.played_sources) == 3)
    voice.finish_track(error=RuntimeError("boom"))  # a fresh retry, not a skip
    await wait_until(lambda: len(voice.played_sources) == 4)
    assert not any("failed" in m for m in channel.messages)


def test_before_options_without_headers_is_unchanged():
    resolved = ResolvedStream(url="https://s", acodec="opus", resolved_at=0.0)
    assert player_module._before_options(resolved) == player_module.FFMPEG_BEFORE_OPTIONS


def test_before_options_quotes_headers_as_one_ffmpeg_argument():
    resolved = ResolvedStream(
        url="https://s",
        acodec="opus",
        resolved_at=0.0,
        http_headers={"User-Agent": "com.google.android VR/1.61", "Accept": "*/*"},
    )
    # discord.py shlex.split()s before_options; the blob must survive as one arg.
    args = shlex.split(player_module._before_options(resolved))
    base = shlex.split(player_module.FFMPEG_BEFORE_OPTIONS)
    assert args[: len(base)] == base
    assert args[len(base) :] == [
        "-headers",
        "User-Agent: com.google.android VR/1.61\r\nAccept: */*\r\n",
    ]


async def test_play_exception_skips_track_but_keeps_player(
    make_player, voice, channel, track_factory, wait_until
):
    voice.play_error = RuntimeError("ffmpeg binary missing")
    player, destroyed = make_player()
    player.enqueue(track_factory("Song A"))
    player.enqueue(track_factory("Song B"))

    await wait_until(lambda: voice.played_sources)
    assert voice.played_sources == ["stream://Song B"]
    assert any("Couldn't play" in m and "Song A" in m for m in channel.messages)
    assert destroyed == []


async def test_instant_finish_of_long_track_counts_as_failure(
    make_player, voice, channel, track_factory, wait_until
):
    player, _ = make_player()
    player.queue_looping = True
    player.enqueue(track_factory("Song A", duration=180))
    await wait_until(lambda: voice.played_sources)

    # Dead stream URL: ffmpeg exits almost instantly, no error.
    voice.finish_track(elapsed=0.1)
    await wait_until(lambda: len(voice.played_sources) == 2)  # the one retry
    voice.finish_track(elapsed=0.1)
    await wait_until(lambda: player.now_playing is None)
    assert any("failed" in m for m in channel.messages)
    assert [t.title for t in player.queue] == []


async def test_prefetch_resolves_next_track_while_playing(
    make_player, voice, track_factory, wait_until
):
    calls = []

    async def resolve(track):
        calls.append(track.title)
        return fake_stream(track)

    player, _ = make_player(resolve=resolve)
    player.enqueue(track_factory("Song A"))
    await wait_until(lambda: voice.played_sources)

    player.enqueue(track_factory("Song B"))
    await wait_until(lambda: "Song B" in calls)
    # Song B was resolved while Song A is still playing.
    assert voice.played_sources == ["stream://Song A"]
    assert player.now_playing.title == "Song A"


async def test_queue_cap_rejects_overflow(make_player, track_factory, monkeypatch):
    monkeypatch.setattr(player_module, "MAX_QUEUE_SIZE", 2)
    player, _ = make_player()
    player.enqueue(track_factory("Song A"))
    player.enqueue(track_factory("Song B"))
    with pytest.raises(QueueFullError):
        player.enqueue(track_factory("Song C"))
    assert len(player.queue) == 2


async def test_track_enqueued_during_idle_goodbye_still_plays(
    make_player, voice, channel, track_factory, wait_until
):
    """The idle timer's goodbye message must not eat a just-enqueued track."""
    gate = asyncio.Event()
    saying_goodbye = asyncio.Event()
    original_send = channel.send

    async def slow_send(content=None, **kwargs):
        if content and "disconnecting" in content.lower():
            saying_goodbye.set()
            await gate.wait()
        await original_send(content, **kwargs)

    channel.send = slow_send
    player, destroyed = make_player(idle_timeout=0.05)

    await saying_goodbye.wait()
    player.enqueue(track_factory("Song A"))  # arrives mid-goodbye
    gate.set()

    await wait_until(lambda: voice.played_sources)
    assert voice.played_sources == ["stream://Song A"]
    assert destroyed == []


async def test_skip_before_loop_picks_up_track_removes_it(make_player, voice, track_factory):
    player, _ = make_player()
    player.enqueue(track_factory("Song A"))
    # Nothing is current yet: the loop hasn't run. A skip must take the
    # queued head with it instead of arming a flag the loop resets.
    player.skip()
    assert not player.queue

    await asyncio.sleep(0.05)
    assert voice.played_sources == []


async def test_skip_before_loop_requeues_when_queue_looping(make_player, track_factory):
    player, _ = make_player()
    player.queue_looping = True
    player.enqueue(track_factory("Song A"))
    player.enqueue(track_factory("Song B"))

    player.skip()
    assert [t.title for t in player.queue] == ["Song B", "Song A"]


async def test_instant_finish_of_unknown_duration_counts_as_failure(
    make_player, voice, channel, track_factory, wait_until
):
    player, _ = make_player()
    player.enqueue(track_factory("Live Stream", duration=None))
    await wait_until(lambda: voice.played_sources)

    voice.finish_track(elapsed=0.3)  # dead live stream: exits immediately
    await wait_until(lambda: len(voice.played_sources) == 2)  # the one retry
    voice.finish_track(elapsed=0.3)
    await wait_until(lambda: player.now_playing is None)
    assert any("failed" in m for m in channel.messages)


async def test_unknown_duration_long_play_is_not_failure(
    make_player, voice, channel, track_factory, wait_until
):
    player, _ = make_player()
    player.enqueue(track_factory("Live Stream", duration=None))
    await wait_until(lambda: voice.played_sources)

    voice.finish_track(elapsed=30.0)
    await wait_until(lambda: player.now_playing is None)
    assert not any("failed" in m for m in channel.messages)


async def test_short_track_early_exit_is_failure(
    make_player, voice, channel, track_factory, wait_until
):
    player, _ = make_player()
    player.enqueue(track_factory("Short", duration=5))
    await wait_until(lambda: voice.played_sources)

    # Threshold is duration-relative: min(2s, 5 * 0.5) = 2s.
    voice.finish_track(elapsed=0.7)
    await wait_until(lambda: len(voice.played_sources) == 2)  # the one retry
    voice.finish_track(elapsed=0.7)
    await wait_until(lambda: player.now_playing is None)
    assert any("failed" in m for m in channel.messages)


async def test_very_short_clip_full_play_is_not_failure(
    make_player, voice, channel, track_factory, wait_until
):
    player, _ = make_player()
    player.enqueue(track_factory("Blip", duration=1))
    await wait_until(lambda: voice.played_sources)

    voice.finish_track(elapsed=1.0)  # legit: played its full 1s
    await wait_until(lambda: player.now_playing is None)
    assert not any("failed" in m for m in channel.messages)


async def test_position_is_cleared_while_next_track_resolves(
    make_player, voice, track_factory, wait_until
):
    gate = asyncio.Event()

    async def resolve(track):
        if track.title == "Song B" and not gate.is_set():
            await gate.wait()
        return fake_stream(track)

    player, _ = make_player(resolve=resolve)
    player.enqueue(track_factory("Song A"))
    await wait_until(lambda: voice.played_sources)
    player.enqueue(track_factory("Song B"))

    voice.finish_track()
    # Song B becomes current but is stuck resolving: no stale elapsed time
    # left over from Song A.
    await wait_until(
        lambda: player.now_playing is not None and player.now_playing.title == "Song B"
    )
    assert player.position is None
    gate.set()


async def test_wait_closed_completes_after_destroy(make_player, track_factory):
    player, _ = make_player()
    waiter = asyncio.ensure_future(player.wait_closed())
    await asyncio.sleep(0.01)
    assert not waiter.done()

    await player.destroy()
    await asyncio.wait_for(waiter, timeout=1)


async def test_removing_queue_head_retargets_prefetch(
    make_player, voice, track_factory, wait_until
):
    calls = []

    async def resolve(track):
        calls.append(track.title)
        return fake_stream(track)

    player, _ = make_player(resolve=resolve)
    player.enqueue(track_factory("Song A"))
    await wait_until(lambda: voice.played_sources)
    player.enqueue(track_factory("Song B"))
    player.enqueue(track_factory("Song C"))
    await wait_until(lambda: "Song B" in calls)

    removed = player.remove_at(0)
    assert removed.title == "Song B"
    # The prefetch now aims at the new head.
    await wait_until(lambda: "Song C" in calls)
    assert player._prefetch_track.title == "Song C"


async def test_foreground_resolve_waits_for_inflight_prefetch(
    make_player, voice, track_factory, wait_until
):
    active = 0
    max_active = 0

    async def resolve(track):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        try:
            if track.title == "Song B":
                await asyncio.sleep(0.05)
            return fake_stream(track)
        finally:
            active -= 1

    player, _ = make_player(resolve=resolve)
    player.enqueue(track_factory("Song A"))
    await wait_until(lambda: voice.played_sources)
    player.enqueue(track_factory("Song B"))  # prefetch of B starts (slow)

    voice.finish_track()  # loop reaches B while its prefetch is in flight
    await wait_until(lambda: len(voice.played_sources) == 2)
    assert max_active == 1  # never two concurrent resolutions


async def test_channel_emptying_during_resolve_pauses_playback(
    make_player, voice, track_factory, wait_until
):
    gate = asyncio.Event()
    resolving = asyncio.Event()

    async def resolve(track):
        if not gate.is_set():
            resolving.set()
            await gate.wait()
        return fake_stream(track)

    player, _ = make_player(idle_timeout=60, resolve=resolve)
    player.enqueue(track_factory("Song A"))

    await resolving.wait()
    player.channel_became_empty()  # everyone left while resolving
    gate.set()

    await wait_until(lambda: voice.played_sources)
    await wait_until(lambda: voice.paused)
    assert player.auto_paused

    player.channel_became_occupied()
    assert not voice.paused
    assert not player.auto_paused


# -- queue management: move / shuffle / clear / remove_where --------------------


async def test_move_reorders_queue(make_player, voice, track_factory, wait_until):
    player, _ = make_player()
    player.enqueue(track_factory("Song A"))
    await wait_until(lambda: voice.played_sources)
    for title in ("Song B", "Song C", "Song D"):
        player.enqueue(track_factory(title))

    moved = player.move(2, 0)
    assert moved.title == "Song D"
    assert [t.title for t in player.queue] == ["Song D", "Song B", "Song C"]


async def test_move_to_head_retargets_prefetch(make_player, voice, track_factory, wait_until):
    calls = []

    async def resolve(track):
        calls.append(track.title)
        return fake_stream(track)

    player, _ = make_player(resolve=resolve)
    player.enqueue(track_factory("Song A"))
    await wait_until(lambda: voice.played_sources)
    player.enqueue(track_factory("Song B"))
    player.enqueue(track_factory("Song C"))
    await wait_until(lambda: "Song B" in calls)

    player.move(1, 0)
    await wait_until(lambda: "Song C" in calls)
    assert player._prefetch_track.title == "Song C"

    voice.finish_track()
    await wait_until(lambda: len(voice.played_sources) == 2)
    assert voice.played_sources[1] == "stream://Song C"


async def test_move_clamps_past_the_end(make_player, voice, track_factory, wait_until):
    player, _ = make_player()
    player.enqueue(track_factory("Song A"))
    await wait_until(lambda: voice.played_sources)
    player.enqueue(track_factory("Song B"))
    player.enqueue(track_factory("Song C"))

    player.move(0, 99)  # deque.insert clamps to the end
    assert [t.title for t in player.queue] == ["Song C", "Song B"]


async def test_shuffle_preserves_tracks_and_retargets_prefetch(
    make_player, voice, track_factory, wait_until, monkeypatch
):
    calls = []

    async def resolve(track):
        calls.append(track.title)
        return fake_stream(track)

    player, _ = make_player(resolve=resolve)
    player.enqueue(track_factory("Song A"))
    await wait_until(lambda: voice.played_sources)
    for title in ("Song B", "Song C", "Song D"):
        player.enqueue(track_factory(title))
    await wait_until(lambda: "Song B" in calls)

    def reverse_shuffle(seq):
        seq.reverse()

    monkeypatch.setattr(player_module.random, "shuffle", reverse_shuffle)
    player.shuffle()
    assert [t.title for t in player.queue] == ["Song D", "Song C", "Song B"]
    await wait_until(lambda: "Song D" in calls)
    assert player._prefetch_track.title == "Song D"

    voice.finish_track()
    await wait_until(lambda: len(voice.played_sources) == 2)
    assert voice.played_sources[1] == "stream://Song D"


async def test_shuffle_keeping_head_skips_reprefetch(
    make_player, voice, track_factory, wait_until, monkeypatch
):
    player, _ = make_player()
    player.enqueue(track_factory("Song A"))
    await wait_until(lambda: voice.played_sources)
    player.enqueue(track_factory("Song B"))
    await wait_until(lambda: player._prefetch_track is not None)
    prefetch_task = player._prefetch_task

    monkeypatch.setattr(player_module.random, "shuffle", lambda seq: None)
    player.shuffle()
    assert player._prefetch_task is prefetch_task  # untouched: head didn't change


async def test_clear_queue_keeps_current_track_playing(
    make_player, voice, track_factory, wait_until
):
    player, _ = make_player()
    player.enqueue(track_factory("Song A"))
    await wait_until(lambda: voice.played_sources)
    player.enqueue(track_factory("Song B"))
    player.enqueue(track_factory("Song C"))

    assert player.clear_queue() == 2
    assert not player.queue
    assert player.now_playing is not None
    assert voice.connected
    # The old head's prefetch was cancelled along with the queue.
    assert player._prefetch_track is None


async def test_remove_where_removes_matching_and_retargets_prefetch(
    make_player, voice, track_factory, wait_until
):
    calls = []

    async def resolve(track):
        calls.append(track.title)
        return fake_stream(track)

    player, _ = make_player(resolve=resolve)
    player.enqueue(track_factory("Song A"))
    await wait_until(lambda: voice.played_sources)
    player.enqueue(track_factory("Song B", requested_by="alice"))
    player.enqueue(track_factory("Song C", requested_by="bob"))
    player.enqueue(track_factory("Song D", requested_by="alice"))
    await wait_until(lambda: "Song B" in calls)

    removed = player.remove_where(lambda t: t.requested_by == "alice")
    assert [t.title for t in removed] == ["Song B", "Song D"]
    assert [t.title for t in player.queue] == ["Song C"]
    await wait_until(lambda: "Song C" in calls)
    assert player._prefetch_track.title == "Song C"


async def test_remove_where_no_match_leaves_queue_alone(
    make_player, voice, track_factory, wait_until
):
    player, _ = make_player()
    player.enqueue(track_factory("Song A"))
    await wait_until(lambda: voice.played_sources)
    player.enqueue(track_factory("Song B"))
    await wait_until(lambda: player._prefetch_task is not None)
    prefetch_task = player._prefetch_task

    assert player.remove_where(lambda t: t.requested_by == "nobody") == []
    assert [t.title for t in player.queue] == ["Song B"]
    assert player._prefetch_task is prefetch_task


# -- now-playing message lifecycle ---------------------------------------------


class StubNowView:
    def __init__(self):
        self.stopped = False

    def stop(self):
        self.stopped = True


def np_factory(record):
    """A now_playing_factory that records the views it hands out."""

    def factory(player):
        view = StubNowView()
        record.append(view)
        return {"card": player.now_playing.title}, view

    return factory


async def test_now_playing_factory_sends_embed_with_view(
    make_player, voice, channel, track_factory, wait_until
):
    views = []
    player, _ = make_player(now_playing_factory=np_factory(views))
    player.enqueue(track_factory("Song A"))
    await wait_until(lambda: channel.sent)

    message = channel.sent[0]
    assert message.kwargs["embed"] == {"card": "Song A"}
    assert message.kwargs["view"] is views[0]
    assert player._now_message is message


async def test_track_change_strips_previous_controls(
    make_player, voice, channel, track_factory, wait_until
):
    views = []
    player, _ = make_player(now_playing_factory=np_factory(views))
    player.enqueue(track_factory("Song A"))
    await wait_until(lambda: voice.played_sources)
    player.enqueue(track_factory("Song B"))

    voice.finish_track()
    await wait_until(lambda: len(channel.sent) == 2)
    first = channel.sent[0]
    await wait_until(lambda: first.edits)
    assert first.edits == [{"view": None}]
    assert views[0].stopped
    assert not views[1].stopped


async def test_queue_drained_strips_controls(
    make_player, voice, channel, track_factory, wait_until
):
    views = []
    player, _ = make_player(now_playing_factory=np_factory(views))
    player.enqueue(track_factory("Song A"))
    await wait_until(lambda: voice.played_sources)

    voice.finish_track()
    await wait_until(lambda: channel.sent[0].edits)
    assert channel.sent[0].edits == [{"view": None}]
    assert views[0].stopped
    assert player._now_message is None


async def test_destroy_strips_controls(make_player, voice, channel, track_factory, wait_until):
    views = []
    player, _ = make_player(now_playing_factory=np_factory(views))
    player.enqueue(track_factory("Song A"))
    await wait_until(lambda: channel.sent)

    await player.destroy()
    assert channel.sent[0].edits == [{"view": None}]
    assert views[0].stopped


async def test_song_loop_replay_does_not_resend_card(
    make_player, voice, channel, track_factory, wait_until
):
    views = []
    player, _ = make_player(now_playing_factory=np_factory(views))
    player.enqueue(track_factory("Song A"))
    await wait_until(lambda: voice.played_sources)
    player.song_looping = True

    voice.finish_track()
    await wait_until(lambda: len(voice.played_sources) == 2)
    assert len(channel.sent) == 1  # same card keeps serving the replay
    assert not channel.sent[0].edits
    assert not views[0].stopped


async def test_retry_after_failure_does_not_resend_card(
    make_player, voice, channel, track_factory, wait_until
):
    views = []
    player, _ = make_player(now_playing_factory=np_factory(views))
    player.enqueue(track_factory("Song A"))
    await wait_until(lambda: voice.played_sources)

    voice.finish_track(error=RuntimeError("403"))
    await wait_until(lambda: len(voice.played_sources) == 2)  # the silent retry
    assert len(channel.sent) == 1
    assert not views[0].stopped


# -- live card refreshes and the finished state ---------------------------------


class RenderStubView(StubNowView):
    """StubNowView that also supports GuildPlayer.refresh_now_message."""

    def __init__(self):
        super().__init__()
        self.renders = 0

    def render(self):
        self.renders += 1
        return {"render": self.renders}


def rendering_np_factory(record):
    def factory(player):
        view = RenderStubView()
        record.append(view)
        return {"card": player.now_playing.title}, view

    return factory


def _recording_finished_factory(track, reason):
    return {"finished": track.title, "reason": reason}


async def test_finished_factory_flips_card_when_track_ends(
    make_player, voice, channel, track_factory, wait_until
):
    views = []
    player, _ = make_player(
        now_playing_factory=np_factory(views),
        finished_factory=_recording_finished_factory,
    )
    player.enqueue(track_factory("Song A"))
    await wait_until(lambda: voice.played_sources)

    voice.finish_track()
    await wait_until(lambda: channel.sent[0].edits)
    assert channel.sent[0].edits == [
        {"view": None, "embed": {"finished": "Song A", "reason": "finished"}}
    ]
    assert views[0].stopped


async def test_finished_factory_flips_card_on_destroy(
    make_player, voice, channel, track_factory, wait_until
):
    views = []
    player, _ = make_player(
        now_playing_factory=np_factory(views),
        finished_factory=_recording_finished_factory,
    )
    player.enqueue(track_factory("Song A"))
    await wait_until(lambda: channel.sent)

    await player.destroy()
    assert channel.sent[0].edits == [
        {"view": None, "embed": {"finished": "Song A", "reason": "stopped"}}
    ]


async def test_finished_factory_reports_skip_reason(
    make_player, voice, channel, track_factory, wait_until
):
    views = []
    player, _ = make_player(
        now_playing_factory=np_factory(views),
        finished_factory=_recording_finished_factory,
    )
    player.enqueue(track_factory("Song A"))
    await wait_until(lambda: voice.played_sources)

    player.skip()
    await wait_until(lambda: channel.sent[0].edits)
    assert channel.sent[0].edits == [
        {"view": None, "embed": {"finished": "Song A", "reason": "skipped"}}
    ]


async def test_np_updater_ticks_the_card_while_playing(
    make_player, voice, channel, track_factory, wait_until
):
    views = []
    player, _ = make_player(now_playing_factory=rendering_np_factory(views))
    player.np_update_interval = 0.01
    player.enqueue(track_factory("Song A"))
    await wait_until(lambda: channel.sent)

    message = channel.sent[0]
    await wait_until(lambda: len(message.edits) >= 2)
    assert message.edits[0]["embed"] == {"render": 1}
    assert message.edits[0]["view"] is views[0]


async def test_np_updater_skips_ticks_while_paused(
    make_player, voice, channel, track_factory, wait_until
):
    views = []
    player, _ = make_player(now_playing_factory=rendering_np_factory(views))
    player.np_update_interval = 0.01
    player.enqueue(track_factory("Song A"))
    await wait_until(lambda: channel.sent)

    player.pause()
    await asyncio.sleep(0.03)  # let any in-flight tick land
    edits_before = len(channel.sent[0].edits)
    await asyncio.sleep(0.05)
    assert len(channel.sent[0].edits) == edits_before


async def test_queue_changes_coalesce_into_one_card_refresh(
    make_player, voice, channel, track_factory, wait_until, monkeypatch
):
    monkeypatch.setattr("musicbot.player.NP_REFRESH_COALESCE_SECONDS", 0.01)
    views = []
    player, _ = make_player(now_playing_factory=rendering_np_factory(views))
    player.np_update_interval = 60  # keep the periodic ticker out of the way
    player.enqueue(track_factory("Song A"))
    await wait_until(lambda: channel.sent)
    message = channel.sent[0]

    for title in ("Song B", "Song C", "Song D"):
        player.enqueue(track_factory(title))
    await wait_until(lambda: message.edits)
    await asyncio.sleep(0.05)
    assert len(message.edits) == 1  # the burst collapsed into a single edit


async def test_refresh_requested_during_edit_is_not_lost(
    make_player, voice, channel, track_factory, wait_until, monkeypatch
):
    monkeypatch.setattr("musicbot.player.NP_REFRESH_COALESCE_SECONDS", 0.01)
    views = []
    player, _ = make_player(now_playing_factory=rendering_np_factory(views))
    player.np_update_interval = 60
    player.enqueue(track_factory("Song A"))
    await wait_until(lambda: channel.sent)
    message = channel.sent[0]

    gate = asyncio.Event()
    edits: list[dict] = []

    async def slow_edit(**kwargs):
        edits.append(kwargs)
        await gate.wait()

    message.edit = slow_edit
    player.request_np_refresh()
    await wait_until(lambda: len(edits) == 1)  # first edit is in flight

    player.request_np_refresh()  # arrives mid-edit; must not be dropped
    gate.set()
    await wait_until(lambda: len(edits) == 2)
    assert edits[1]["embed"] == {"render": 2}  # a fresh render, not a replay


@pytest.mark.parametrize("error_cls", [discord.NotFound, discord.Forbidden])
async def test_dead_card_stops_the_updater(
    make_player, voice, channel, track_factory, wait_until, error_cls
):
    views = []
    player, _ = make_player(now_playing_factory=rendering_np_factory(views))
    player.np_update_interval = 0.01
    player.enqueue(track_factory("Song A"))
    await wait_until(lambda: channel.sent)
    message = channel.sent[0]

    attempts: list[dict] = []

    async def edit_gone(**kwargs):
        attempts.append(kwargs)
        raise error_cls(SimpleNamespace(status=404, reason="Not Found"), "unknown message")

    message.edit = edit_gone
    await wait_until(lambda: attempts)
    await asyncio.sleep(0.05)
    assert len(attempts) == 1  # the updater retired the card instead of retrying
    assert views[0].stopped
    assert player._now_message is None

    voice.finish_track()  # track-end teardown must not touch the dead message
    await asyncio.sleep(0.03)
    assert len(attempts) == 1


async def test_auto_pause_and_resume_refresh_the_card(
    make_player, voice, channel, track_factory, wait_until, monkeypatch
):
    monkeypatch.setattr("musicbot.player.NP_REFRESH_COALESCE_SECONDS", 0.01)
    views = []
    player, _ = make_player(now_playing_factory=rendering_np_factory(views), idle_timeout=60)
    player.np_update_interval = 60
    player.enqueue(track_factory("Song A"))
    await wait_until(lambda: channel.sent)
    message = channel.sent[0]

    player.channel_became_empty()
    assert voice.is_paused()
    await wait_until(lambda: len(message.edits) >= 1)

    player.channel_became_occupied()
    assert not voice.is_paused()
    await wait_until(lambda: len(message.edits) >= 2)
