"""Tests for command-layer logic that doesn't need Discord objects."""

from __future__ import annotations

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
