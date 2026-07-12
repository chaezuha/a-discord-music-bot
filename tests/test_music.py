"""Tests for command-layer logic that doesn't need Discord objects."""

from __future__ import annotations

import asyncio
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
