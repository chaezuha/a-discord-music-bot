"""Shared fakes and fixtures. No network, no real ffmpeg, no Discord connection."""

from __future__ import annotations

import asyncio
import time

import pytest

from musicbot.player import GuildPlayer
from musicbot.sources import ResolvedStream, Track


def fake_stream(track: Track) -> ResolvedStream:
    return ResolvedStream(
        url=f"stream://{track.title}", acodec="opus", resolved_at=time.monotonic()
    )


class FakeUser:
    def __init__(self) -> None:
        self.dms: list[str] = []

    async def send(self, content: str) -> None:
        self.dms.append(content)


class FakeBot:
    """Just enough of discord.Client for GuildPlayer: an event loop and DMs."""

    def __init__(self) -> None:
        self.owner = FakeUser()

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        return asyncio.get_running_loop()

    async def fetch_user(self, user_id: int) -> FakeUser:
        return self.owner


class FakeMessage:
    def __init__(self, content: str | None, kwargs: dict) -> None:
        self.content = content
        self.kwargs = kwargs  # embed=, view=, … as passed to send()
        self.edits: list[dict] = []

    async def edit(self, **kwargs) -> None:
        self.edits.append(kwargs)


class FakeChannel:
    def __init__(self) -> None:
        self.messages: list[str] = []
        self.sent: list[FakeMessage] = []

    async def send(self, content: str | None = None, **kwargs) -> FakeMessage:
        self.messages.append(content or "")
        message = FakeMessage(content, kwargs)
        self.sent.append(message)
        return message


class FakeVoiceClient:
    """Mimics discord.VoiceClient: play() stores the after-callback, stop() fires it."""

    def __init__(self) -> None:
        self.connected = True
        self.playing = False
        self.paused = False
        self.played_sources: list[str] = []
        self.stop_calls = 0
        self.disconnect_calls = 0
        self.play_error: Exception | None = None
        self._after = None
        # Set by make_player so finish_track can fake elapsed playback time.
        self.player = None

    def is_connected(self) -> bool:
        return self.connected

    def is_playing(self) -> bool:
        return self.playing and not self.paused

    def is_paused(self) -> bool:
        return self.paused

    def play(self, source, *, after=None) -> None:
        if self.play_error is not None:
            error, self.play_error = self.play_error, None
            raise error
        self.playing = True
        self._after = after
        self.played_sources.append(source)

    def stop(self) -> None:
        self.stop_calls += 1
        self.finish_track()

    def finish_track(self, error: Exception | None = None, elapsed: float | None = None) -> None:
        """Simulate the current track ending (or being stopped).

        By default the track's full advertised duration counts as elapsed (a
        legitimate finish); pass `elapsed` to fake an implausibly early exit
        (see GuildPlayer._playback_failed).
        """
        if self.player is not None and self.player._started_at is not None:
            track = self.player.now_playing
            if elapsed is None:
                elapsed = track.duration if track is not None and track.duration else 10
            self.player._started_at = time.monotonic() - elapsed
        self.playing = False
        self.paused = False
        after, self._after = self._after, None
        if after is not None:
            after(error)

    def pause(self) -> None:
        self.paused = True

    def resume(self) -> None:
        self.paused = False

    async def disconnect(self, *, force: bool = False) -> None:
        self.disconnect_calls += 1
        self.connected = False


@pytest.fixture
def track_factory():
    def make(title: str = "Test Song", **overrides) -> Track:
        fields = {
            "webpage_url": f"https://example.com/{title.replace(' ', '-')}",
            "duration": 5,
            "uploader": "Test Uploader",
            "requested_by": "tester",
            "requested_by_id": 1000,
        }
        fields.update(overrides)
        return Track(title=title, **fields)

    return make


@pytest.fixture
def voice() -> FakeVoiceClient:
    return FakeVoiceClient()


@pytest.fixture
def channel() -> FakeChannel:
    return FakeChannel()


@pytest.fixture
async def make_player(voice, channel, monkeypatch):
    """Factory for a GuildPlayer wired to fakes; resolve/ffmpeg are stubbed out."""
    players: list[GuildPlayer] = []

    def factory(
        idle_timeout: float = 5.0,
        resolve=None,
        on_destroy=None,
        notifier=None,
        now_playing_factory=None,
        finished_factory=None,
    ) -> tuple[GuildPlayer, list]:
        async def default_resolve(track: Track) -> ResolvedStream:
            return fake_stream(track)

        monkeypatch.setattr("musicbot.player.resolve_stream", resolve or default_resolve)
        monkeypatch.setattr(
            "musicbot.player.discord.FFmpegOpusAudio",
            lambda url, **kwargs: url,
        )
        destroyed: list[bool] = []
        player = GuildPlayer(
            bot=FakeBot(),
            voice=voice,
            text_channel=channel,
            idle_timeout=idle_timeout,
            on_destroy=on_destroy or (lambda: destroyed.append(True)),
            notifier=notifier,
            now_playing_factory=now_playing_factory,
            finished_factory=finished_factory,
        )
        voice.player = player
        players.append(player)
        return player, destroyed

    yield factory

    for player in players:
        await player.destroy()


@pytest.fixture
def wait_until():
    """Poll a predicate until true or fail the test after a timeout."""

    async def _wait(predicate, timeout: float = 2.0) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while not predicate():
            if loop.time() > deadline:
                raise AssertionError("condition not met within timeout")
            await asyncio.sleep(0.005)

    return _wait
