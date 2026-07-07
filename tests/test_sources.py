"""Unit tests for the yt-dlp wrapper. All extraction is mocked — no network."""

from __future__ import annotations

import pytest

from musicbot import sources
from musicbot.sources import SourceError, _clean_error, _first_entry, fmt_duration, is_url


def patch_extract(monkeypatch, result: dict):
    """Replace sources._extract with a canned result; returns the call log."""
    calls: list[tuple[str, bool]] = []

    def fake_extract(url_or_query: str, *, flat: bool) -> dict:
        calls.append((url_or_query, flat))
        return result

    monkeypatch.setattr(sources, "_extract", fake_extract)
    return calls


# -- pure helpers ---------------------------------------------------------


def test_is_url():
    assert is_url("https://youtube.com/watch?v=abc")
    assert is_url("http://example.com")
    assert not is_url("never gonna give you up")
    assert not is_url("youtube.com/watch?v=abc")


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (None, "?:??"),
        (0, "?:??"),
        (5, "0:05"),
        (65, "1:05"),
        (3725, "1:02:05"),
        (213.7, "3:33"),
    ],
)
def test_fmt_duration(seconds, expected):
    assert fmt_duration(seconds) == expected


def test_clean_error_strips_prefix():
    assert _clean_error(Exception("ERROR: video unavailable")) == "video unavailable"
    assert _clean_error(Exception("plain message")) == "plain message"
    assert _clean_error(Exception("")) == "unknown extraction error"


def test_first_entry_unwraps_nested_playlists():
    info = {"entries": [{"entries": [{"title": "deep"}, {"title": "ignored"}]}]}
    assert _first_entry(info) == {"title": "deep"}


def test_first_entry_rejects_empty_playlists():
    with pytest.raises(SourceError):
        _first_entry({"entries": []})
    with pytest.raises(SourceError):
        _first_entry({"entries": [None]})


# -- search ---------------------------------------------------------------


async def test_search_uses_source_prefix_and_flat(monkeypatch):
    calls = patch_extract(monkeypatch, {"entries": []})
    await sources.search("hello world", "youtube", requested_by="me")
    await sources.search("hello world", "soundcloud", requested_by="me")
    assert calls == [
        ("ytsearch10:hello world", True),
        ("scsearch10:hello world", True),
    ]


async def test_search_maps_entries_with_fallbacks(monkeypatch):
    patch_extract(
        monkeypatch,
        {
            "entries": [
                {"title": "A", "url": "https://a", "duration": 100, "channel": "Chan"},
                None,
                {"webpage_url": "https://b", "uploader": "Up"},
            ]
        },
    )
    tracks = await sources.search("query", "youtube", requested_by="me")
    assert len(tracks) == 2

    first, second = tracks
    assert (first.title, first.webpage_url, first.uploader) == ("A", "https://a", "Chan")
    assert first.duration == 100
    assert first.requested_by == "me"
    assert (second.title, second.webpage_url, second.uploader) == (
        "Unknown title",
        "https://b",
        "Up",
    )


# -- fetch_track ----------------------------------------------------------


async def test_fetch_track_single_video(monkeypatch):
    calls = patch_extract(
        monkeypatch,
        {"title": "Song", "webpage_url": "https://w", "duration": 42, "uploader": "U"},
    )
    track = await sources.fetch_track("https://w", requested_by="me")
    assert calls == [("https://w", False)]
    assert (track.title, track.webpage_url, track.duration) == ("Song", "https://w", 42)


async def test_fetch_track_unwraps_playlist_url(monkeypatch):
    patch_extract(
        monkeypatch,
        {"entries": [{"title": "First", "webpage_url": "https://first"}, {"title": "Second"}]},
    )
    track = await sources.fetch_track("https://playlist", requested_by="me")
    assert track.title == "First"


async def test_fetch_track_empty_playlist_raises(monkeypatch):
    patch_extract(monkeypatch, {"entries": []})
    with pytest.raises(SourceError):
        await sources.fetch_track("https://playlist", requested_by="me")


# -- resolve_stream -------------------------------------------------------


async def test_resolve_stream_uses_top_level_url(monkeypatch, track_factory):
    patch_extract(monkeypatch, {"url": "https://stream", "formats": []})
    assert await sources.resolve_stream(track_factory()) == "https://stream"


async def test_resolve_stream_falls_back_to_last_audio_format(monkeypatch, track_factory):
    patch_extract(
        monkeypatch,
        {
            "formats": [
                {"url": "https://f1", "acodec": "opus"},
                {"url": "https://f2", "acodec": "mp4a"},
                {"url": "https://f3", "acodec": "none"},
                {"url": None, "acodec": "opus"},
            ]
        },
    )
    assert await sources.resolve_stream(track_factory()) == "https://f2"


async def test_resolve_stream_no_audio_raises(monkeypatch, track_factory):
    patch_extract(monkeypatch, {"formats": [{"url": "https://video", "acodec": "none"}]})
    with pytest.raises(SourceError):
        await sources.resolve_stream(track_factory())
