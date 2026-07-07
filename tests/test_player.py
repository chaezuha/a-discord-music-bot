"""Async tests for the GuildPlayer queue/playback/idle loop, all against fakes."""

from __future__ import annotations

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
