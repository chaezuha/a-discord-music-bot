"""Tests for command-layer logic that doesn't need Discord objects."""

from __future__ import annotations

from collections import deque
from types import SimpleNamespace

import pytest

from musicbot.music import Music


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

    def enqueue(self, track) -> int:
        return self._position


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
