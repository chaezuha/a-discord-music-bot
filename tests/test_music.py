"""Tests for command-layer logic that doesn't need Discord objects."""

from __future__ import annotations

import asyncio
import re
import time
from collections import deque
from types import SimpleNamespace

import pytest

from musicbot.music import Music, UserError, votes_needed


@pytest.fixture
def queued_player(track_factory):
    return SimpleNamespace(
        queue=deque(
            [
                track_factory("Never Gonna Give You Up"),
                track_factory("Sandstorm"),
                track_factory("One More Time"),
            ]
        )
    )


class StubPlayer:
    def __init__(self, active: bool, position: int):
        self.is_active = active
        self._position = position

    def enqueue(self, track, front: bool = False) -> int:
        return 1 if front else self._position


# -- /remove matching -----------------------------------------------------


def test_remove_by_number(queued_player):
    assert Music._find_queue_index(queued_player, "2") == 1


@pytest.mark.parametrize("target", ["0", "4", "999"])
def test_remove_number_out_of_range(queued_player, target):
    assert Music._find_queue_index(queued_player, target) is None


def test_remove_by_substring_case_insensitive(queued_player):
    assert Music._find_queue_index(queued_player, "SANDstorm") == 1
    assert Music._find_queue_index(queued_player, "one more") == 2


def test_remove_by_fuzzy_match(queued_player):
    assert Music._find_queue_index(queued_player, "nevr gona give you up") == 0


def test_remove_no_match(queued_player):
    assert Music._find_queue_index(queued_player, "zzzzzz nothing zzzzzz") is None


# -- /play reply wording --------------------------------------------------


def test_enqueue_message_when_idle(track_factory):
    message = Music._enqueue(None, StubPlayer(active=False, position=1), track_factory("Song"))
    assert "starting now" in message
    assert "Song" in message


def test_enqueue_message_when_busy(track_factory):
    message = Music._enqueue(None, StubPlayer(active=True, position=3), track_factory("Song"))
    assert "Added to queue (#3)" in message


def test_enqueue_message_front(track_factory):
    message = Music._enqueue(
        None, StubPlayer(active=True, position=1), track_factory("Song"), front=True
    )
    assert "Playing next" in message
    assert "Song" in message


def test_enqueue_message_front_when_idle(track_factory):
    message = Music._enqueue(
        None, StubPlayer(active=False, position=1), track_factory("Song"), front=True
    )
    assert "starting now" in message


def test_enqueue_message_links_title(track_factory):
    message = Music._enqueue(None, StubPlayer(active=True, position=2), track_factory("Song"))
    assert "[**Song**](<https://example.com/Song>)" in message


# -- /queue progress --------------------------------------------------------


def test_fmt_progress_shows_elapsed_and_total(track_factory):
    player = SimpleNamespace(position=61.0)
    assert Music._fmt_progress(player, track_factory(duration=180)) == "1:01 / 3:00"


def test_fmt_progress_without_position_shows_total(track_factory):
    player = SimpleNamespace(position=None)
    assert Music._fmt_progress(player, track_factory(duration=180)) == "3:00"


def test_fmt_progress_without_duration_shows_elapsed(track_factory):
    player = SimpleNamespace(position=61.0)
    assert Music._fmt_progress(player, track_factory(duration=None)) == "1:01"


# -- /help ------------------------------------------------------------------


def test_help_embed_lists_all_commands():
    cog = Music.__new__(Music)
    embed = Music._help_embed(list(cog.get_app_commands()))
    names = [field.name for field in embed.fields]
    for expected in (
        "/help",
        "/loopsong",
        "/loopqueue",
        "/forceskip",
        "/skip",
        "/pause",
        "/playnext <query> [source]",
        "/queue",
    ):
        assert any(name.startswith(expected.split(" ")[0]) for name in names)
    assert "/play <query> [source]" in names
    assert "/playnext <query> [source]" in names
    assert all(field.value for field in embed.fields)


# -- /skip vote threshold -----------------------------------------------------


@pytest.mark.parametrize(
    ("listeners", "needed"),
    [(1, 1), (2, 1), (3, 2), (4, 2), (5, 3), (6, 3), (7, 4)],
)
def test_votes_needed_is_half_rounded_up(listeners, needed):
    assert votes_needed(listeners) == needed


# -- voice channel occupancy ------------------------------------------------


class OccupancyPlayer:
    def __init__(self, members):
        self.voice = SimpleNamespace(channel=SimpleNamespace(members=members))
        self.calls: list[str] = []

    def channel_became_empty(self):
        self.calls.append("empty")

    def channel_became_occupied(self):
        self.calls.append("occupied")


def test_occupancy_with_humans_resumes():
    human = SimpleNamespace(bot=False)
    bot_member = SimpleNamespace(bot=True)
    player = OccupancyPlayer([bot_member, human])
    Music._check_voice_occupancy(player)
    assert player.calls == ["occupied"]


def test_occupancy_only_bots_counts_as_empty():
    player = OccupancyPlayer([SimpleNamespace(bot=True), SimpleNamespace(bot=True)])
    Music._check_voice_occupancy(player)
    assert player.calls == ["empty"]


def test_occupancy_no_channel_is_noop():
    player = OccupancyPlayer([])
    player.voice.channel = None
    Music._check_voice_occupancy(player)
    assert player.calls == []


# -- player bookkeeping -------------------------------------------------------


def test_remove_player_only_removes_matching_instance():
    cog = Music.__new__(Music)
    old_player, new_player = object(), object()
    cog.players = {1: new_player}

    # A stale destroy callback from the old player must not evict the new one.
    cog._remove_player(1, old_player)
    assert cog.players[1] is new_player

    cog._remove_player(1, new_player)
    assert 1 not in cog.players


# -- voice channel gate -------------------------------------------------------


def _gate_fixtures():
    bot_channel = SimpleNamespace(name="music")
    player = SimpleNamespace(voice=SimpleNamespace(channel=bot_channel))
    return bot_channel, player


def test_same_channel_gate_allows_member_in_channel():
    bot_channel, player = _gate_fixtures()
    interaction = SimpleNamespace(user=SimpleNamespace(voice=SimpleNamespace(channel=bot_channel)))
    Music._require_same_channel(interaction, player)  # must not raise


def test_same_channel_gate_rejects_member_elsewhere():
    _, player = _gate_fixtures()
    other = SimpleNamespace(name="afk")
    interaction = SimpleNamespace(user=SimpleNamespace(voice=SimpleNamespace(channel=other)))
    with pytest.raises(UserError):
        Music._require_same_channel(interaction, player)


def test_same_channel_gate_rejects_user_not_in_voice():
    _, player = _gate_fixtures()
    interaction = SimpleNamespace(user=SimpleNamespace(voice=None))
    with pytest.raises(UserError):
        Music._require_same_channel(interaction, player)


# -- env validation -----------------------------------------------------------


def test_env_id_parses_and_rejects(monkeypatch):
    from musicbot.music import env_id

    monkeypatch.delenv("OWNER_ID", raising=False)
    assert env_id("OWNER_ID") is None
    monkeypatch.setenv("OWNER_ID", "  12345 ")
    assert env_id("OWNER_ID") == 12345
    monkeypatch.setenv("OWNER_ID", "not-a-number")
    with pytest.raises(ValueError, match="OWNER_ID"):
        env_id("OWNER_ID")


@pytest.mark.parametrize("bad", ["abc", "-5", "0", "inf", "nan"])
def test_env_idle_timeout_rejects_bad_values(monkeypatch, bad):
    from musicbot.music import env_idle_timeout

    monkeypatch.setenv("IDLE_TIMEOUT_SECONDS", bad)
    with pytest.raises(ValueError, match="IDLE_TIMEOUT_SECONDS"):
        env_idle_timeout()


def test_env_idle_timeout_default_and_valid(monkeypatch):
    from musicbot.music import env_idle_timeout

    monkeypatch.delenv("IDLE_TIMEOUT_SECONDS", raising=False)
    assert env_idle_timeout() == 180.0
    monkeypatch.setenv("IDLE_TIMEOUT_SECONDS", "42.5")
    assert env_idle_timeout() == 42.5


# -- /remove empty needle ------------------------------------------------------


def test_remove_empty_needle_matches_nothing(queued_player):
    assert Music._find_queue_index(queued_player, "") is None


# -- queue embed clamping -------------------------------------------------------


def test_queue_description_within_limit_keeps_everything():
    description = Music._queue_description(["**Now playing:** X\n"], ["`1.` A", "`2.` B"], 2)
    assert description == "**Now playing:** X\n\n`1.` A\n`2.` B"


def test_queue_description_reports_hidden_tracks():
    description = Music._queue_description([], ["`1.` A"], 5)
    assert description.endswith("…and 4 more")


def test_queue_description_drops_lines_to_fit():
    from musicbot.music import EMBED_DESCRIPTION_LIMIT

    lines = [f"`{i}.` " + "x" * 400 for i in range(1, 16)]
    description = Music._queue_description([], lines, 20)
    assert len(description) <= EMBED_DESCRIPTION_LIMIT
    assert "…and" in description  # dropped lines are accounted for


# -- skip vote tallying ---------------------------------------------------------


def _vote_player():
    return SimpleNamespace(skip_votes=set())


def test_tally_skip_vote_passes_at_threshold():
    player = _vote_player()
    passed, already, needed = Music._tally_skip_vote(player, 1, {1, 2})
    assert (passed, already, needed) == (True, False, 1)


def test_tally_skip_vote_below_threshold():
    player = _vote_player()
    passed, already, needed = Music._tally_skip_vote(player, 1, {1, 2, 3, 4, 5})
    assert (passed, already, needed) == (False, False, 3)


def test_tally_skip_vote_repeat_vote_reports_duplicate():
    player = _vote_player()
    Music._tally_skip_vote(player, 1, {1, 2, 3, 4, 5})
    passed, already, _ = Music._tally_skip_vote(player, 1, {1, 2, 3, 4, 5})
    assert (passed, already) == (False, True)


def test_tally_skip_vote_passes_after_listeners_leave():
    """2/5 votes, then two non-voters leave: the existing votes now carry."""
    player = _vote_player()
    Music._tally_skip_vote(player, 1, {1, 2, 3, 4, 5})
    Music._tally_skip_vote(player, 2, {1, 2, 3, 4, 5})
    # Only listeners 1-3 remain; a repeat vote from 1 must pass, not bounce.
    passed, already, needed = Music._tally_skip_vote(player, 1, {1, 2, 3})
    assert (passed, needed) == (True, 2)


def test_tally_skip_vote_prunes_departed_voters():
    player = _vote_player()
    Music._tally_skip_vote(player, 9, {9, 1, 2, 3, 4})  # voter 9 then leaves
    passed, _, needed = Music._tally_skip_vote(player, 1, {1, 2, 3, 4})
    assert not passed
    assert player.skip_votes == {1}


# -- interaction-level checks ----------------------------------------------------


class FakeResponse:
    def __init__(self):
        self.deferred = False
        self.messages: list[str] = []

    async def defer(self):
        self.deferred = True

    def is_done(self):
        return self.deferred or bool(self.messages)

    async def send_message(self, content=None, **kwargs):
        self.messages.append(content or "")


def make_interaction(*, voice_channel=None, guild_voice_client=None, text_channel=None):
    edits: list[dict] = []
    followups: list[str] = []

    async def edit_original_response(**kwargs):
        edits.append(kwargs)

    async def followup_send(content=None, **kwargs):
        followups.append(content or "")

    interaction = SimpleNamespace(
        guild_id=1,
        guild=SimpleNamespace(voice_client=guild_voice_client),
        channel=text_channel,
        user=SimpleNamespace(
            id=7,
            display_name="alice",
            voice=SimpleNamespace(channel=voice_channel) if voice_channel else None,
        ),
        response=FakeResponse(),
        followup=SimpleNamespace(send=followup_send),
        edit_original_response=edit_original_response,
    )
    return interaction, edits, followups


def make_cog():
    cog = Music.__new__(Music)
    cog.players = {}
    cog._player_locks = {}
    return cog


async def test_play_requires_voice_before_any_extraction(monkeypatch):
    from musicbot import music as music_module

    calls = []

    async def spy_fetch(url, requested_by):
        calls.append(url)

    monkeypatch.setattr(music_module.sources, "fetch_track", spy_fetch)
    cog = make_cog()
    interaction, _, _ = make_interaction(voice_channel=None)

    with pytest.raises(UserError, match="voice channel"):
        await cog._play_impl(interaction, "https://youtube.com/watch?v=x", None, front=False)
    assert calls == []
    assert not interaction.response.deferred


@pytest.mark.parametrize("query", ["", "   ", "x" * 501])
async def test_play_rejects_bad_queries(query):
    channel = SimpleNamespace(name="music")
    cog = make_cog()
    interaction, _, _ = make_interaction(voice_channel=channel)

    with pytest.raises(UserError):
        await cog._play_impl(interaction, query, None, front=False)
    assert not interaction.response.deferred


async def test_stop_rejects_caller_outside_orphaned_voice_channel():
    """No player, but a lingering voice client: /stop still checks the channel."""
    bot_channel = SimpleNamespace(name="music")
    disconnects = []

    async def disconnect(force=False):
        disconnects.append(force)

    voice_client = SimpleNamespace(channel=bot_channel, disconnect=disconnect)
    cog = make_cog()
    interaction, _, _ = make_interaction(voice_channel=None, guild_voice_client=voice_client)

    with pytest.raises(UserError, match="Join my voice channel"):
        await Music.stop.callback(cog, interaction)
    assert disconnects == []


async def test_stop_disconnects_orphaned_voice_client_for_member_in_channel():
    bot_channel = SimpleNamespace(name="music")
    disconnects = []

    async def disconnect(force=False):
        disconnects.append(force)

    voice_client = SimpleNamespace(channel=bot_channel, disconnect=disconnect)
    cog = make_cog()
    interaction, _, followups = make_interaction(
        voice_channel=bot_channel, guild_voice_client=voice_client
    )

    await Music.stop.callback(cog, interaction)
    assert disconnects == [True]
    assert any("Stopped" in m for m in followups)


async def test_on_pick_user_error_edits_response():
    cog = make_cog()

    async def ensure(interaction):
        raise UserError("Join a voice channel first, then try again.")

    cog._ensure_player = ensure
    interaction, edits, _ = make_interaction()

    await cog._on_pick(interaction, track=None)
    assert edits and "voice channel" in edits[0]["content"]
    assert edits[0]["view"] is None


async def test_on_pick_generic_error_edits_response():
    cog = make_cog()

    async def ensure(interaction):
        raise RuntimeError("voice handshake exploded")

    cog._ensure_player = ensure
    interaction, edits, _ = make_interaction()

    await cog._on_pick(interaction, track=None)
    assert edits and "went wrong" in edits[0]["content"]
    assert "exploded" not in edits[0]["content"]  # internals stay out of chat


async def test_ensure_player_waits_for_inflight_teardown():
    closed = asyncio.Event()
    waited = []

    async def wait_closed():
        waited.append(True)
        await closed.wait()

    old = SimpleNamespace(destroyed=True, wait_closed=wait_closed)
    connected = []

    async def connect(self_deaf=False):
        connected.append(True)
        return SimpleNamespace(channel=channel, stop=lambda: None, is_connected=lambda: False)

    channel = SimpleNamespace(name="music", connect=connect)
    cog = make_cog()
    cog.players = {1: old}
    cog.idle_timeout = 60
    cog.notifier = None
    cog.bot = SimpleNamespace(loop=asyncio.get_event_loop())
    interaction, _, _ = make_interaction(voice_channel=channel)

    task = asyncio.ensure_future(cog._ensure_player(interaction))
    await asyncio.sleep(0.02)
    assert waited and not connected  # blocked on the old player's teardown

    closed.set()
    player = await asyncio.wait_for(task, timeout=1)
    assert connected == [True]
    await player.destroy()


async def test_ensure_player_reuse_updates_announcement_channel():
    bot_channel = SimpleNamespace(name="music")
    existing = SimpleNamespace(
        destroyed=False,
        voice=SimpleNamespace(channel=bot_channel),
        is_active=True,
        text_channel="old-text-channel",
    )
    cog = make_cog()
    cog.players = {1: existing}
    interaction, _, _ = make_interaction(voice_channel=bot_channel, text_channel="new-text-channel")

    player = await cog._ensure_player(interaction)
    assert player is existing
    assert player.text_channel == "new-text-channel"


def test_enqueue_full_queue_is_user_error(track_factory):
    from musicbot.player import QueueFullError

    class FullPlayer:
        is_active = True

        def enqueue(self, track, front=False):
            raise QueueFullError

    with pytest.raises(UserError, match="full"):
        Music._enqueue(None, FullPlayer(), track_factory("Song"))


# -- search picker -------------------------------------------------------------


async def test_search_picker_on_error_edits_message(track_factory):
    from musicbot.ui import SearchPicker

    picker = SearchPicker([track_factory("Song")], SimpleNamespace(id=1), on_pick=None)
    edits = []

    class Resp:
        def is_done(self):
            return False

        async def edit_message(self, **kwargs):
            edits.append(kwargs)

    interaction = SimpleNamespace(response=Resp())
    await picker.on_error(interaction, RuntimeError("boom"), item=None)
    assert edits and "went wrong" in edits[0]["content"]
    assert edits[0]["view"] is None


async def test_search_picker_on_error_after_defer_edits_original(track_factory):
    from musicbot.ui import SearchPicker

    picker = SearchPicker([track_factory("Song")], SimpleNamespace(id=1), on_pick=None)
    edits = []

    async def edit_original_response(**kwargs):
        edits.append(kwargs)

    class Resp:
        def is_done(self):
            return True

    interaction = SimpleNamespace(response=Resp(), edit_original_response=edit_original_response)
    await picker.on_error(interaction, RuntimeError("boom"), item=None)
    assert edits and "went wrong" in edits[0]["content"]


# -- autocomplete values in /remove and /move -----------------------------------


def test_find_queue_index_autocomplete_value_fresh(queued_player):
    assert Music._find_queue_index(queued_player, "2:Sandstorm") == 1


def test_find_queue_index_autocomplete_value_with_truncated_prefix(queued_player):
    assert Music._find_queue_index(queued_player, "3:One More") == 2


def test_find_queue_index_autocomplete_value_stale_falls_back_to_title(queued_player):
    # The suggestion pointed at position 1 but the queue shifted since;
    # the title prefix must win over the stale index.
    assert Music._find_queue_index(queued_player, "1:Sandstorm") == 1


def test_find_queue_index_autocomplete_value_out_of_range_falls_back(queued_player):
    assert Music._find_queue_index(queued_player, "9:Sandstorm") == 1


def test_find_queue_index_autocomplete_value_no_match(queued_player):
    assert Music._find_queue_index(queued_player, "2:Zzz Nothing") is None


def test_find_queue_index_plain_digits_still_work(queued_player):
    assert Music._find_queue_index(queued_player, "2") == 1


async def test_queue_autocomplete_without_player_is_empty():
    cog = make_cog()
    interaction = SimpleNamespace(guild_id=1)
    assert await cog._queue_autocomplete(interaction, "anything") == []


async def test_queue_autocomplete_filters_by_substring_and_index(queued_player):
    cog = make_cog()
    cog.players = {1: queued_player}
    interaction = SimpleNamespace(guild_id=1)

    all_choices = await cog._queue_autocomplete(interaction, "")
    assert [c.name for c in all_choices] == [
        "1. Never Gonna Give You Up",
        "2. Sandstorm",
        "3. One More Time",
    ]
    assert all_choices[1].value == "2:Sandstorm"

    by_title = await cog._queue_autocomplete(interaction, "sand")
    assert [c.value for c in by_title] == ["2:Sandstorm"]

    by_index = await cog._queue_autocomplete(interaction, "3")
    assert [c.name for c in by_index] == ["3. One More Time"]


async def test_queue_autocomplete_caps_at_discord_limit(track_factory):
    from musicbot.music import AUTOCOMPLETE_MAX_CHOICES

    cog = make_cog()
    player = SimpleNamespace(queue=deque(track_factory(f"Song {i}") for i in range(60)))
    cog.players = {1: player}
    interaction = SimpleNamespace(guild_id=1)

    choices = await cog._queue_autocomplete(interaction, "")
    assert len(choices) == AUTOCOMPLETE_MAX_CHOICES


# -- queue management commands ----------------------------------------------------


def _wire_player(cog, player, voice):
    """Register a real GuildPlayer with the cog and give its voice a channel."""
    bot_channel = SimpleNamespace(name="music")
    voice.channel = bot_channel
    cog.players = {1: player}
    return bot_channel


async def test_clear_command_keeps_current_track(make_player, voice, track_factory, wait_until):
    player, _ = make_player()
    player.enqueue(track_factory("Song A"))
    await wait_until(lambda: voice.played_sources)
    player.enqueue(track_factory("Song B"))
    player.enqueue(track_factory("Song C"))

    cog = make_cog()
    bot_channel = _wire_player(cog, player, voice)
    interaction, _, _ = make_interaction(voice_channel=bot_channel)

    await Music.clear.callback(cog, interaction)
    assert not player.queue
    assert player.now_playing is not None
    assert any("Cleared 2 tracks" in m for m in interaction.response.messages)


async def test_clear_command_requires_same_channel(make_player, voice, track_factory, wait_until):
    player, _ = make_player()
    player.enqueue(track_factory("Song A"))
    await wait_until(lambda: voice.played_sources)
    player.enqueue(track_factory("Song B"))

    cog = make_cog()
    _wire_player(cog, player, voice)
    elsewhere = SimpleNamespace(name="afk")
    interaction, _, _ = make_interaction(voice_channel=elsewhere)

    with pytest.raises(UserError, match="Join my voice channel"):
        await Music.clear.callback(cog, interaction)
    assert [t.title for t in player.queue] == ["Song B"]


async def test_move_command_moves_track(make_player, voice, track_factory, wait_until):
    player, _ = make_player()
    player.enqueue(track_factory("Song A"))
    await wait_until(lambda: voice.played_sources)
    for title in ("Song B", "Song C", "Song D"):
        player.enqueue(track_factory(title))

    cog = make_cog()
    bot_channel = _wire_player(cog, player, voice)
    interaction, _, _ = make_interaction(voice_channel=bot_channel)

    await Music.move.callback(cog, interaction, track="song d", position=1)
    assert [t.title for t in player.queue] == ["Song D", "Song B", "Song C"]
    assert any("Moved" in m and "#1" in m for m in interaction.response.messages)


async def test_move_command_clamps_position_to_queue_length(
    make_player, voice, track_factory, wait_until
):
    player, _ = make_player()
    player.enqueue(track_factory("Song A"))
    await wait_until(lambda: voice.played_sources)
    player.enqueue(track_factory("Song B"))
    player.enqueue(track_factory("Song C"))

    cog = make_cog()
    bot_channel = _wire_player(cog, player, voice)
    interaction, _, _ = make_interaction(voice_channel=bot_channel)

    await Music.move.callback(cog, interaction, track="1", position=99)
    assert [t.title for t in player.queue] == ["Song C", "Song B"]


async def test_move_command_unknown_track_is_user_error(
    make_player, voice, track_factory, wait_until
):
    player, _ = make_player()
    player.enqueue(track_factory("Song A"))
    await wait_until(lambda: voice.played_sources)
    player.enqueue(track_factory("Song B"))

    cog = make_cog()
    bot_channel = _wire_player(cog, player, voice)
    interaction, _, _ = make_interaction(voice_channel=bot_channel)

    with pytest.raises(UserError, match="Couldn't find"):
        await Music.move.callback(cog, interaction, track="zzz nothing", position=1)


async def test_shuffle_command_needs_two_tracks(make_player, voice, track_factory, wait_until):
    player, _ = make_player()
    player.enqueue(track_factory("Song A"))
    await wait_until(lambda: voice.played_sources)
    player.enqueue(track_factory("Song B"))

    cog = make_cog()
    bot_channel = _wire_player(cog, player, voice)
    interaction, _, _ = make_interaction(voice_channel=bot_channel)

    with pytest.raises(UserError, match="at least two"):
        await Music.shuffle.callback(cog, interaction)

    player.enqueue(track_factory("Song C"))
    await Music.shuffle.callback(cog, interaction)
    assert any("Shuffled 2 tracks" in m for m in interaction.response.messages)


async def test_remove_mine_matches_requester_id(make_player, voice, track_factory, wait_until):
    player, _ = make_player()
    player.enqueue(track_factory("Song A"))
    await wait_until(lambda: voice.played_sources)
    # Ownership is by Discord ID (the caller in make_interaction is id=7):
    # display names collide across users and change with nicknames.
    player.enqueue(track_factory("Song B", requested_by="old_nick", requested_by_id=7))
    player.enqueue(track_factory("Song C", requested_by="alice", requested_by_id=42))
    player.enqueue(track_factory("Song D", requested_by="old_nick", requested_by_id=7))

    cog = make_cog()
    bot_channel = _wire_player(cog, player, voice)
    interaction, _, _ = make_interaction(voice_channel=bot_channel)
    # The caller renamed themselves to the same display name as Song C's
    # requester: their own tracks must still go, the impostor-named one stays.
    interaction.user.display_name = "alice"

    await Music.remove_mine.callback(cog, interaction)
    assert [t.title for t in player.queue] == ["Song C"]
    assert any("Removed your 2 tracks" in m for m in interaction.response.messages)


async def test_remove_mine_without_own_tracks_is_user_error(
    make_player, voice, track_factory, wait_until
):
    player, _ = make_player()
    player.enqueue(track_factory("Song A"))
    await wait_until(lambda: voice.played_sources)
    player.enqueue(track_factory("Song B", requested_by="bob"))

    cog = make_cog()
    bot_channel = _wire_player(cog, player, voice)
    interaction, _, _ = make_interaction(voice_channel=bot_channel)

    with pytest.raises(UserError, match="no tracks"):
        await Music.remove_mine.callback(cog, interaction)


async def test_pause_and_resume_commands_refresh_the_card(
    make_player, voice, track_factory, wait_until
):
    player, _ = make_player()
    player.enqueue(track_factory("Song A"))
    await wait_until(lambda: voice.played_sources)

    refreshes = []

    def fake_refresh():
        refreshes.append(True)

    player.request_np_refresh = fake_refresh
    cog = make_cog()
    bot_channel = _wire_player(cog, player, voice)

    interaction, _, _ = make_interaction(voice_channel=bot_channel)
    await Music.pause.callback(cog, interaction)
    assert voice.is_paused()
    assert len(refreshes) == 1  # the card's Pause button must flip to Resume

    interaction, _, _ = make_interaction(voice_channel=bot_channel)
    await Music.resume.callback(cog, interaction)
    assert not voice.is_paused()
    assert len(refreshes) == 2


async def test_loop_commands_refresh_the_card(make_player, voice, track_factory, wait_until):
    player, _ = make_player()
    player.enqueue(track_factory("Song A"))
    await wait_until(lambda: voice.played_sources)

    refreshes = []

    def fake_refresh():
        refreshes.append(True)

    player.request_np_refresh = fake_refresh
    cog = make_cog()
    bot_channel = _wire_player(cog, player, voice)

    interaction, _, _ = make_interaction(voice_channel=bot_channel)
    await Music.loopsong.callback(cog, interaction)
    assert player.song_looping
    assert len(refreshes) == 1  # the card shows the loop state

    interaction, _, _ = make_interaction(voice_channel=bot_channel)
    await Music.loopqueue.callback(cog, interaction)
    assert player.queue_looping and not player.song_looping
    assert len(refreshes) == 2


# -- playlist import ---------------------------------------------------------


class RecordingPlayer:
    """Enqueue recorder with an optional capacity cap, mimicking GuildPlayer."""

    def __init__(self, capacity=None):
        from musicbot.player import MAX_QUEUE_SIZE

        self.is_active = True
        self.queue: list = []
        self.capacity = MAX_QUEUE_SIZE if capacity is None else capacity

    def enqueue(self, track, front=False):
        from musicbot.player import QueueFullError

        if len(self.queue) >= self.capacity:
            raise QueueFullError
        if front:
            self.queue.insert(0, track)
            return 1
        self.queue.append(track)
        return len(self.queue)


def make_playlist_cog(playlist_max=10):
    cog = make_cog()
    cog.playlist_max = playlist_max
    return cog


def playlist_result(track_factory, count, total=None, title="My Mix"):
    from musicbot.sources import FetchResult

    return FetchResult(
        tracks=[track_factory(f"Song {i}") for i in range(count)],
        playlist_title=title,
        playlist_total=total if total is not None else count,
    )


def test_enqueue_playlist_reports_added_of_total(track_factory):
    cog = make_playlist_cog()
    player = RecordingPlayer()
    message = cog._enqueue_playlist(player, playlist_result(track_factory, 3, total=3))
    assert "Added 3 of 3 tracks" in message and "My Mix" in message
    assert message.endswith(".")
    assert [t.title for t in player.queue] == ["Song 0", "Song 1", "Song 2"]


def test_enqueue_playlist_mentions_import_cap(track_factory):
    cog = make_playlist_cog(playlist_max=10)
    message = cog._enqueue_playlist(RecordingPlayer(), playlist_result(track_factory, 10, total=40))
    assert "Added 10 of 40 tracks" in message
    assert "capped at 10" in message


def test_enqueue_playlist_front_preserves_playlist_order(track_factory):
    cog = make_playlist_cog()
    player = RecordingPlayer()
    cog._enqueue_playlist(player, playlist_result(track_factory, 3), front=True)
    assert [t.title for t in player.queue] == ["Song 0", "Song 1", "Song 2"]


def test_enqueue_playlist_stops_when_queue_fills(track_factory):
    cog = make_playlist_cog()
    player = RecordingPlayer(capacity=2)
    message = cog._enqueue_playlist(player, playlist_result(track_factory, 5, total=5))
    assert "Added 2 of 5 tracks" in message
    assert "queue filled up" in message
    assert [t.title for t in player.queue] == ["Song 0", "Song 1"]


def test_enqueue_playlist_front_with_full_queue_keeps_first_tracks(track_factory, monkeypatch):
    from musicbot import music as music_module

    cog = make_playlist_cog()
    player = RecordingPlayer(capacity=2)
    monkeypatch.setattr(music_module, "MAX_QUEUE_SIZE", 2)
    message = cog._enqueue_playlist(player, playlist_result(track_factory, 5, total=5), front=True)
    # The playlist's FIRST tracks make it in, in order — not its last.
    assert [t.title for t in player.queue] == ["Song 0", "Song 1"]
    assert "Added 2 of 5" in message


def test_enqueue_playlist_totally_full_queue_is_user_error(track_factory):
    cog = make_playlist_cog()
    with pytest.raises(UserError, match="full"):
        cog._enqueue_playlist(RecordingPlayer(capacity=0), playlist_result(track_factory, 3))


async def test_play_impl_enqueues_playlist_and_reports(monkeypatch, track_factory):
    from musicbot import music as music_module

    cog = make_playlist_cog()
    player = RecordingPlayer()

    async def fake_fetch_tracks(url, requested_by, playlist_limit, requested_by_id=None):
        assert playlist_limit == 10
        return playlist_result(track_factory, 3, total=20)

    async def ensure(interaction):
        return player

    monkeypatch.setattr(music_module.sources, "fetch_tracks", fake_fetch_tracks)
    cog._ensure_player = ensure
    channel = SimpleNamespace(name="music")
    interaction, _, followups = make_interaction(voice_channel=channel)

    await cog._play_impl(interaction, "https://youtube.com/playlist?list=x", None, front=False)
    assert len(player.queue) == 3
    assert any("Added 3 of 20 tracks" in m for m in followups)


async def test_play_impl_single_url_keeps_plain_confirmation(monkeypatch, track_factory):
    from musicbot import music as music_module
    from musicbot.sources import FetchResult

    cog = make_playlist_cog()
    player = RecordingPlayer()

    async def fake_fetch_tracks(url, requested_by, playlist_limit, requested_by_id=None):
        return FetchResult(tracks=[track_factory("Solo")])

    async def ensure(interaction):
        return player

    monkeypatch.setattr(music_module.sources, "fetch_tracks", fake_fetch_tracks)
    cog._ensure_player = ensure
    channel = SimpleNamespace(name="music")
    interaction, _, followups = make_interaction(voice_channel=channel)

    await cog._play_impl(interaction, "https://youtube.com/watch?v=x", None, front=False)
    assert [t.title for t in player.queue] == ["Solo"]
    assert any("Added to queue" in m for m in followups)


# -- PLAYLIST_MAX_TRACKS parsing ---------------------------------------------


def test_env_playlist_max_default_and_valid(monkeypatch):
    from musicbot.music import env_playlist_max

    monkeypatch.delenv("PLAYLIST_MAX_TRACKS", raising=False)
    assert env_playlist_max() == 10
    monkeypatch.setenv("PLAYLIST_MAX_TRACKS", " 25 ")
    assert env_playlist_max() == 25


@pytest.mark.parametrize("bad", ["abc", "0", "-3", "501", "2.5"])
def test_env_playlist_max_rejects_bad_values(monkeypatch, bad):
    from musicbot.music import env_playlist_max

    monkeypatch.setenv("PLAYLIST_MAX_TRACKS", bad)
    with pytest.raises(ValueError, match="PLAYLIST_MAX_TRACKS"):
        env_playlist_max()


# -- queue embed builder / QueueView ---------------------------------------------


import discord  # noqa: E402

from musicbot import ui  # noqa: E402


class ViewPlayer:
    """Just enough player surface for build_queue_embed and QueueView."""

    def __init__(self, tracks, now=None, position=None):
        self.queue = deque(tracks)
        self.now_playing = now
        self.song_looping = False
        self.queue_looping = False
        self.position = position
        self.destroyed = False
        self.voice = SimpleNamespace(channel=SimpleNamespace(name="music"), is_paused=lambda: False)

    def remove_at(self, index):
        track = self.queue[index]
        del self.queue[index]
        return track


def make_view_interaction(voice_channel=None):
    edits: list[dict] = []
    sends: list[dict] = []
    followups: list[dict] = []

    class Resp:
        def __init__(self):
            self.done = False

        def is_done(self):
            return self.done

        async def edit_message(self, **kwargs):
            self.done = True
            edits.append(kwargs)

        async def send_message(self, content=None, **kwargs):
            self.done = True
            sends.append({"content": content, **kwargs})

    async def followup_send(content=None, **kwargs):
        followups.append({"content": content, **kwargs})

    interaction = SimpleNamespace(
        response=Resp(),
        followup=SimpleNamespace(send=followup_send),
        user=SimpleNamespace(
            voice=SimpleNamespace(channel=voice_channel) if voice_channel else None
        ),
    )
    return interaction, edits, sends, followups


def test_build_queue_embed_pages_with_absolute_numbers(track_factory):
    player = ViewPlayer([track_factory(f"Song {i:02d}", duration=60) for i in range(20)])
    page = ui.build_queue_embed(player, 1)
    assert (page.page, page.page_count) == (1, 2)
    assert [t.title for t in page.tracks] == [f"Song {i:02d}" for i in range(15, 20)]
    assert "`16.`" in page.embed.description
    assert "`1.`" not in page.embed.description
    assert "Page 2/2" in page.embed.footer.text
    assert "20 tracks — 20:00 queued" in page.embed.footer.text


def test_build_queue_embed_clamps_page_into_range(track_factory):
    player = ViewPlayer([track_factory("Song", duration=60)])
    page = ui.build_queue_embed(player, 99)
    assert (page.page, page.page_count) == (0, 1)


def test_build_queue_embed_marks_unknown_durations_in_total(track_factory):
    player = ViewPlayer([track_factory("A", duration=90), track_factory("Live", duration=None)])
    page = ui.build_queue_embed(player, 0)
    assert "1:30+ queued" in page.embed.footer.text


def test_build_queue_embed_shows_loop_state(track_factory):
    player = ViewPlayer([track_factory("A", duration=60)])
    player.queue_looping = True
    page = ui.build_queue_embed(player, 0)
    assert "queue loop on" in page.embed.footer.text


def test_build_queue_embed_includes_now_playing_head(track_factory):
    player = ViewPlayer(
        [track_factory("Queued", duration=60)],
        now=track_factory("Current", duration=180),
        position=61.0,
    )
    page = ui.build_queue_embed(player, 0)
    assert "**Now playing:**" in page.embed.description
    assert "1:01 / 3:00" in page.embed.description


def test_queue_wait_seconds_sums_remainder_and_prefix(track_factory):
    player = ViewPlayer(
        [track_factory("A", duration=60), track_factory("B", duration=30)],
        now=track_factory("Current", duration=100),
        position=40.0,
    )
    assert ui.queue_wait_seconds(player, 0) == 60.0
    assert ui.queue_wait_seconds(player, 1) == 120.0


def test_queue_wait_seconds_unknown_duration_or_song_loop_is_none(track_factory):
    player = ViewPlayer(
        [track_factory("Live", duration=None), track_factory("B", duration=30)],
        now=track_factory("Current", duration=100),
        position=40.0,
    )
    assert ui.queue_wait_seconds(player, 0) == 60.0  # before the unknown track
    assert ui.queue_wait_seconds(player, 1) is None  # the unknown is in the way

    player.song_looping = True
    assert ui.queue_wait_seconds(player, 0) is None


def test_queue_wait_seconds_paused_is_none(track_factory):
    # A pause can last indefinitely; "starts in ~…" would be a lie.
    player = ViewPlayer(
        [track_factory("A", duration=60)],
        now=track_factory("Current", duration=100),
        position=40.0,
    )
    player.voice = SimpleNamespace(channel=player.voice.channel, is_paused=lambda: True)
    assert ui.queue_wait_seconds(player, 0) is None


def test_queue_view_refresh_syncs_buttons_and_select(track_factory):
    player = ViewPlayer([track_factory(f"Song {i}", duration=60) for i in range(20)])
    view = ui.QueueView(player, make_cog())

    view.refresh()
    assert view.prev_button.disabled and not view.next_button.disabled
    assert [o.label for o in view.select.options][0] == "1. Song 0"
    assert len(view.select.options) == 15

    view.page = 1
    view.refresh()
    assert not view.prev_button.disabled and view.next_button.disabled
    assert [o.label for o in view.select.options][0] == "16. Song 15"


def test_queue_view_page_clamps_after_queue_shrinks(track_factory):
    player = ViewPlayer([track_factory(f"Song {i}", duration=60) for i in range(20)])
    view = ui.QueueView(player, make_cog(), page=1)
    view.refresh()

    while len(player.queue) > 3:
        player.queue.pop()
    view.refresh()
    assert view.page == 0
    assert view.prev_button.disabled and view.next_button.disabled


def test_queue_view_empty_page_has_no_select(track_factory):
    player = ViewPlayer([], now=track_factory("Current", duration=60), position=10.0)
    view = ui.QueueView(player, make_cog())
    view.refresh()
    assert view.select not in view.children


async def test_queue_view_remove_matches_by_identity(track_factory):
    player = ViewPlayer([track_factory(f"Song {i}", duration=60) for i in range(4)])
    view = ui.QueueView(player, make_cog())
    view.refresh()
    target = view.page_tracks[2]  # "Song 2"

    # Someone else removes an earlier track after the page rendered: every
    # index shifts, but the selection must still remove "Song 2".
    player.remove_at(0)

    interaction, edits, _, followups = make_view_interaction(voice_channel=player.voice.channel)
    await view._remove_row(interaction, 2)
    assert [t.title for t in player.queue] == ["Song 1", "Song 3"]
    assert target.title == "Song 2"
    assert edits, "the queue page must re-render"
    assert followups and "Removed" in followups[0]["content"]
    assert followups[0]["ephemeral"] is True


async def test_queue_view_remove_vanished_track_reports_and_rerenders(track_factory):
    player = ViewPlayer(
        [track_factory("Song 0", duration=60), track_factory("Song 1", duration=60)]
    )
    view = ui.QueueView(player, make_cog())
    view.refresh()

    player.remove_at(0)  # the pick target disappears entirely
    interaction, edits, _, followups = make_view_interaction(voice_channel=player.voice.channel)
    await view._remove_row(interaction, 0)
    assert [t.title for t in player.queue] == ["Song 1"]  # nothing else removed
    assert edits
    assert followups and "already left" in followups[0]["content"]


async def test_queue_view_remove_requires_same_channel(track_factory):
    player = ViewPlayer([track_factory("Song 0", duration=60)])
    view = ui.QueueView(player, make_cog())
    view.refresh()

    elsewhere = SimpleNamespace(name="afk")
    interaction, edits, sends, _ = make_view_interaction(voice_channel=elsewhere)
    await view._remove_row(interaction, 0)
    assert len(player.queue) == 1
    assert not edits
    assert sends and "Join my voice channel" in sends[0]["content"]
    assert sends[0]["ephemeral"] is True


async def test_queue_view_pagination_updates_message(track_factory):
    player = ViewPlayer([track_factory(f"Song {i}", duration=60) for i in range(20)])
    view = ui.QueueView(player, make_cog())
    view.refresh()

    interaction, edits, _, _ = make_view_interaction()
    await view._on_next(interaction)
    assert view.page == 1
    assert edits and "`16.`" in edits[0]["embed"].description

    interaction2, edits2, _, _ = make_view_interaction()
    await view._on_prev(interaction2)
    assert view.page == 0
    assert edits2


# -- now-playing embed --------------------------------------------------------


def test_build_now_playing_embed_fields_and_thumbnail(track_factory):
    track = track_factory(
        "Current", duration=180, thumbnail="https://thumb", uploader="Chan", requested_by="alice"
    )
    player = ViewPlayer([track_factory("Next", duration=60)], now=track, position=90.0)
    embed = ui.build_now_playing_embed(player)

    assert "Current" in embed.description
    assert "1:30 / 3:00" in embed.description
    assert "\N{RADIO BUTTON}" in embed.description  # the progress bar
    fields = {f.name: f.value for f in embed.fields}
    assert fields["Requested by"] == "alice"
    assert fields["Uploader"] == "Chan"
    assert fields["Up next"] == "1 track (1:00)"
    assert fields["Loop"] == "Off"
    assert embed.thumbnail.url == "https://thumb"


def test_build_now_playing_embed_paused_and_loop_markers(track_factory):
    track = track_factory("Current", duration=180)
    player = ViewPlayer([], now=track, position=10.0)
    player.voice = SimpleNamespace(channel=None, is_paused=lambda: True)
    player.song_looping = True
    embed = ui.build_now_playing_embed(player)

    assert "Paused" in embed.description
    fields = {f.name: f.value for f in embed.fields}
    assert fields["Loop"] == "Song"
    assert fields["Up next"] == "0 tracks"
    assert embed.thumbnail.url is None


def test_build_now_playing_embed_unknown_duration_has_no_bar(track_factory):
    track = track_factory("Live", duration=None)
    player = ViewPlayer([], now=track, position=90.0)
    embed = ui.build_now_playing_embed(player)
    assert "\N{RADIO BUTTON}" not in embed.description
    assert "`1:30`" in embed.description


def test_build_now_playing_embed_shows_live_end_timestamp(track_factory):
    track = track_factory("Current", duration=180)
    player = ViewPlayer([], now=track, position=60.0)
    before = time.time()
    embed = ui.build_now_playing_embed(player)
    after = time.time()

    match = re.search(r"Ends <t:(\d+):R>", embed.description)
    assert match is not None
    remaining = 180 - 60
    assert int(before) + remaining - 1 <= int(match.group(1)) <= int(after) + remaining + 1


def test_build_now_playing_embed_hides_end_timestamp_when_unreliable(track_factory):
    track = track_factory("Current", duration=180)

    paused = ViewPlayer([], now=track, position=60.0)
    paused.voice = SimpleNamespace(channel=None, is_paused=lambda: True)
    assert "Ends <t:" not in ui.build_now_playing_embed(paused).description

    looping = ViewPlayer([], now=track, position=60.0)
    looping.song_looping = True
    assert "Ends <t:" not in ui.build_now_playing_embed(looping).description

    unknown = ViewPlayer([], now=track_factory("Live", duration=None), position=60.0)
    assert "Ends <t:" not in ui.build_now_playing_embed(unknown).description


def test_build_finished_embed_titles_reflect_the_end_reason(track_factory):
    track = track_factory("Done", duration=180)
    for reason, title in [
        ("finished", "Finished Playing"),
        ("skipped", "Skipped"),
        ("stopped", "Stopped"),
        ("failed", "Playback Failed"),
        ("someday-new-reason", "Playback Ended"),
    ]:
        embed = ui.build_finished_embed(track, reason)
        assert title in embed.title, reason
    assert ui.build_finished_embed(track).title.endswith("Finished Playing")
    assert ui.build_finished_embed(track, "failed").color == discord.Color.red()
    assert ui.build_finished_embed(track, "stopped").color == discord.Color.dark_grey()


# -- NowPlayingView -----------------------------------------------------------


def make_np_interaction(voice_channel=None, message=None):
    interaction, edits, sends, followups = make_view_interaction(voice_channel=voice_channel)
    interaction.message = message
    return interaction, edits, sends, followups


async def test_now_playing_view_rejects_stale_controls(track_factory):
    player = ViewPlayer([], now=track_factory("Current"))
    view = ui.NowPlayingView(player, make_cog())
    player.now_playing = None  # the track ended

    stripped = []

    class Msg:
        async def edit(self, **kwargs):
            stripped.append(kwargs)

    interaction, _, sends, _ = make_np_interaction(message=Msg())
    assert await view.interaction_check(interaction) is False
    assert sends and "stale" in sends[0]["content"]
    assert stripped == [{"view": None}]
    assert view.is_finished()


async def test_now_playing_view_rejects_wrong_channel(track_factory):
    player = ViewPlayer([], now=track_factory("Current"))
    view = ui.NowPlayingView(player, make_cog())

    elsewhere = SimpleNamespace(name="afk")
    interaction, _, sends, _ = make_np_interaction(voice_channel=elsewhere)
    assert await view.interaction_check(interaction) is False
    assert sends and "Join my voice channel" in sends[0]["content"]


async def test_now_playing_view_allows_member_in_channel(track_factory):
    player = ViewPlayer([], now=track_factory("Current"))
    view = ui.NowPlayingView(player, make_cog())

    interaction, _, sends, _ = make_np_interaction(voice_channel=player.voice.channel)
    assert await view.interaction_check(interaction) is True
    assert not sends


async def test_now_playing_view_refresh_updates_embed_and_pause_label(track_factory):
    player = ViewPlayer([], now=track_factory("Current", duration=100), position=10.0)
    view = ui.NowPlayingView(player, make_cog())
    assert view.pause_button.label == "Pause"

    player.voice = SimpleNamespace(channel=player.voice.channel, is_paused=lambda: True)
    interaction, edits, _, _ = make_np_interaction()
    await view.refresh(interaction)
    assert view.pause_button.label == "Resume"
    assert edits and "Paused" in edits[0]["embed"].description
    assert edits[0]["view"] is view


async def test_now_playing_view_run_reports_user_error_ephemerally(track_factory):
    player = ViewPlayer([], now=track_factory("Current"))
    view = ui.NowPlayingView(player, make_cog())

    async def failing():
        raise UserError("You already voted")

    interaction, _, sends, _ = make_np_interaction()
    await view._run(interaction, failing())
    assert sends and sends[0]["content"] == "You already voted"
    assert sends[0]["ephemeral"] is True


# -- np_* controller methods ----------------------------------------------------


async def test_np_pause_resume_toggles_then_refreshes():
    calls = []

    class V:
        async def refresh(self, interaction):
            calls.append("refresh")

    player = SimpleNamespace(
        voice=SimpleNamespace(is_paused=lambda: False, is_playing=lambda: True),
        pause=lambda: calls.append("pause"),
        resume=lambda: calls.append("resume"),
    )
    await make_cog().np_pause_resume(None, player, V())
    assert calls == ["pause", "refresh"]

    calls.clear()
    player.voice = SimpleNamespace(is_paused=lambda: True, is_playing=lambda: False)
    await make_cog().np_pause_resume(None, player, V())
    assert calls == ["resume", "refresh"]


async def test_np_skip_votes_like_slash_skip():
    calls = []
    listener = SimpleNamespace(id=7, bot=False)
    player = SimpleNamespace(
        skip_votes=set(),
        voice=SimpleNamespace(channel=SimpleNamespace(members=[listener])),
        skip=lambda: calls.append("skip"),
        queue=[],
        song_looping=False,
        queue_looping=False,
    )
    interaction, _, sends, _ = make_view_interaction()
    interaction.user = SimpleNamespace(id=7)

    await make_cog().np_skip(interaction, player)
    assert calls == ["skip"]
    assert sends and "Skipped" in sends[0]["content"]
    assert "ephemeral" not in sends[0] or not sends[0]["ephemeral"]  # public reply


async def test_np_skip_partial_vote_reports_count():
    listeners = [SimpleNamespace(id=i, bot=False) for i in (1, 2, 3)]
    player = SimpleNamespace(
        skip_votes=set(),
        voice=SimpleNamespace(channel=SimpleNamespace(members=listeners)),
        skip=lambda: (_ for _ in ()).throw(AssertionError("must not skip")),
        queue=[],
        song_looping=False,
        queue_looping=False,
    )
    interaction, _, sends, _ = make_view_interaction()
    interaction.user = SimpleNamespace(id=1)

    await make_cog().np_skip(interaction, player)
    assert sends and "1/2" in sends[0]["content"]


async def test_np_loop_toggles_song_loop_exclusively():
    refreshes = []

    class V:
        async def refresh(self, interaction):
            refreshes.append(True)

    player = SimpleNamespace(song_looping=False, queue_looping=True)
    await make_cog().np_loop(None, player, V())
    assert player.song_looping and not player.queue_looping
    assert refreshes


async def test_np_queue_is_ephemeral(track_factory):
    player = ViewPlayer(
        [track_factory("Song", duration=60)], now=track_factory("Current", duration=60)
    )
    player.position = 10.0

    sends = []

    class Resp:
        def is_done(self):
            return bool(sends)

        async def send_message(self, content=None, **kwargs):
            sends.append(kwargs)

    async def original_response():
        return "the-ephemeral-message"

    interaction = SimpleNamespace(response=Resp(), original_response=original_response)
    await make_cog().np_queue(interaction, player)
    assert sends and sends[0]["ephemeral"] is True
    assert sends[0]["view"].message == "the-ephemeral-message"


async def test_np_stop_responds_before_destroy():
    order = []

    class Resp:
        def is_done(self):
            return bool(order)

        async def send_message(self, content=None, **kwargs):
            order.append("respond")

    async def destroy():
        order.append("destroy")

    interaction = SimpleNamespace(response=Resp())
    player = SimpleNamespace(destroy=destroy)
    await make_cog().np_stop(interaction, player)
    assert order == ["respond", "destroy"]


# -- ephemeral search picker ----------------------------------------------------


async def test_on_pick_success_edits_picker_and_announces_publicly(track_factory):
    cog = make_cog()
    player = RecordingPlayer()

    async def ensure(interaction):
        return player

    cog._ensure_player = ensure
    interaction, edits, followups = make_interaction()

    await cog._on_pick(interaction, track_factory("Song"))
    assert [t.title for t in player.queue] == ["Song"]
    assert edits and "Queued." in edits[0]["content"]
    assert edits[0]["view"] is None
    # The channel-visible confirmation goes out as a fresh public message.
    assert followups and "Song" in followups[0]


async def test_play_impl_search_picker_is_ephemeral(monkeypatch, track_factory):
    from musicbot import music as music_module
    from musicbot.notifier import BreakageNotifier

    cog = make_cog()
    cog.notifier = BreakageNotifier(owner_id=None)

    async def fake_search(query, source, requested_by, requested_by_id=None):
        return [track_factory("Result")]

    monkeypatch.setattr(music_module.sources, "search", fake_search)

    sent = []

    async def followup_send(content=None, **kwargs):
        sent.append({"content": content, **kwargs})
        return "picker-message"

    channel = SimpleNamespace(name="music")
    interaction, _, _ = make_interaction(voice_channel=channel)
    interaction.followup = SimpleNamespace(send=followup_send)

    await cog._play_impl(interaction, "hello world", None, front=False)
    assert sent and sent[0]["ephemeral"] is True
    assert sent[0]["view"].message == "picker-message"
