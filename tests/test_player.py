"""Async tests for the GuildPlayer queue/playback/idle loop, all against fakes."""

from __future__ import annotations

import asyncio

from musicbot.notifier import BreakageNotifier
from musicbot.sources import SourceError


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
        return f"stream://{track.title}"

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
    player.looping = True

    voice.finish_track()
    await wait_until(lambda: len(voice.played_sources) == 2)
    assert voice.played_sources == ["stream://Song A", "stream://Song A"]
    assert [t.title for t in player.queue] == ["Song B"]
    assert sum("Now playing" in m for m in channel.messages) == 1


async def test_loop_re_resolves_stream(make_player, voice, track_factory, wait_until):
    calls = []

    async def resolve(track):
        calls.append(track.title)
        return f"stream://{track.title}"

    player, _ = make_player(resolve=resolve)
    player.enqueue(track_factory("Song A"))
    await wait_until(lambda: voice.played_sources)
    player.looping = True

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
    player.looping = True

    player.skip()
    await wait_until(lambda: len(voice.played_sources) == 2)
    assert voice.played_sources == ["stream://Song A", "stream://Song B"]
    assert player.looping


async def test_loop_off_advances_normally(make_player, voice, track_factory, wait_until):
    player, _ = make_player()
    player.enqueue(track_factory("Song A"))
    await wait_until(lambda: voice.played_sources)
    player.enqueue(track_factory("Song B"))
    player.looping = True
    player.looping = False

    voice.finish_track()
    await wait_until(lambda: len(voice.played_sources) == 2)
    assert voice.played_sources == ["stream://Song A", "stream://Song B"]


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
