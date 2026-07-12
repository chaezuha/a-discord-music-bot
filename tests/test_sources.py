"""Unit tests for the yt-dlp wrapper. All extraction is mocked — no network."""

from __future__ import annotations

import pytest

from musicbot import sources
from musicbot.sources import (
    DEFAULT_ALLOWED_DOMAINS,
    ERROR_DISPLAY_LIMIT,
    TITLE_DISPLAY_LIMIT,
    SourceError,
    _clean_error,
    _first_entry,
    _parse_allowed_domains,
    check_url_allowed,
    fmt_duration,
    fmt_title,
    is_url,
    truncate,
)


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


def test_fmt_title_links_to_webpage(track_factory):
    track = track_factory("Song", webpage_url="https://example.com/song")
    assert fmt_title(track) == "[**Song**](<https://example.com/song>)"


def test_fmt_title_without_url_is_plain_bold(track_factory):
    track = track_factory("Song", webpage_url="")
    assert fmt_title(track) == "**Song**"


def test_fmt_title_escapes_markdown(track_factory):
    track = track_factory("**evil** [link](x)", webpage_url="")
    assert fmt_title(track) == r"**\*\*evil\*\* \[link](x)**"


def test_clean_error_strips_prefix():
    assert _clean_error(Exception("ERROR: video unavailable")) == "video unavailable"
    assert _clean_error(Exception("plain message")) == "plain message"
    assert _clean_error(Exception("")) == "unknown extraction error"


def test_clean_error_truncates_long_messages():
    cleaned = _clean_error(Exception("x" * (ERROR_DISPLAY_LIMIT + 100)))
    assert len(cleaned) == ERROR_DISPLAY_LIMIT
    assert cleaned.endswith("…")


def test_truncate_passthrough_and_ellipsis():
    assert truncate("short", 10) == "short"
    assert truncate("x" * 10, 10) == "x" * 10
    clipped = truncate("word " * 20, 12)
    assert len(clipped) <= 12
    assert clipped.endswith("…")


def test_fmt_title_truncates_before_escaping(track_factory):
    # A pile of Markdown characters right at the cut point: escaping after
    # truncation means no escape sequence can be sliced in half.
    title = "a" * (TITLE_DISPLAY_LIMIT - 1) + "*" * 50
    track = track_factory(title, webpage_url="")
    rendered = fmt_title(track)
    assert rendered.startswith("**")
    assert "*" * 50 not in rendered
    assert "\\" + "…" not in rendered  # the ellipsis is never escaped
    assert "…" in rendered


def test_first_entry_unwraps_nested_playlists():
    info = {"entries": [{"entries": [{"title": "deep"}, {"title": "ignored"}]}]}
    assert _first_entry(info) == {"title": "deep"}


def test_first_entry_rejects_empty_playlists():
    with pytest.raises(SourceError):
        _first_entry({"entries": []})
    with pytest.raises(SourceError):
        _first_entry({"entries": [None]})


# -- URL allowlist ----------------------------------------------------------


def test_parse_allowed_domains_semantics():
    assert _parse_allowed_domains(None) == DEFAULT_ALLOWED_DOMAINS
    assert _parse_allowed_domains("") == DEFAULT_ALLOWED_DOMAINS
    assert _parse_allowed_domains("*") is None
    # A list replaces the defaults entirely.
    assert _parse_allowed_domains("Vimeo.com, .archive.org") == {"vimeo.com", "archive.org"}
    with pytest.raises(ValueError):
        _parse_allowed_domains(" , ,")


@pytest.mark.parametrize(
    "url",
    [
        "https://youtube.com/watch?v=abc",
        "https://www.youtube.com/watch?v=abc",
        "https://music.youtube.com/watch?v=abc",
        "https://youtu.be/abc",
        "https://soundcloud.com/artist/track",
        "https://artist.bandcamp.com/track/x",
    ],
)
def test_default_allowlist_accepts_known_sites(url):
    check_url_allowed(url)  # must not raise


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/video",
        "https://notyoutube.com/watch?v=abc",  # suffix match must not be fooled
        "https://youtube.com.evil.example/watch",
        "https://user:pass@youtube.com/watch?v=abc",  # embedded credentials
        "http://127.0.0.1:8080/admin",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/",
        "http://10.0.0.5/stream",
        "https:///nohost",
    ],
)
def test_default_allowlist_rejects(url):
    with pytest.raises(SourceError):
        check_url_allowed(url)


def test_wildcard_disables_allowlist(monkeypatch):
    monkeypatch.setattr(sources, "ALLOWED_DOMAINS", None)
    check_url_allowed("http://127.0.0.1/anything")  # must not raise


def test_custom_allowlist_replaces_defaults(monkeypatch):
    monkeypatch.setattr(sources, "ALLOWED_DOMAINS", frozenset({"vimeo.com"}))
    check_url_allowed("https://vimeo.com/12345")
    with pytest.raises(SourceError):
        check_url_allowed("https://youtube.com/watch?v=abc")


async def test_fetch_track_enforces_allowlist(monkeypatch):
    calls = patch_extract(monkeypatch, {"title": "Song"})
    with pytest.raises(SourceError):
        await sources.fetch_track("https://evil.example/x", requested_by="me")
    assert calls == []  # rejected before any extraction


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


VIDEO_URL = "https://youtube.com/watch?v=abc"
PLAYLIST_URL = "https://youtube.com/playlist?list=xyz"


async def test_fetch_track_single_video(monkeypatch):
    calls = patch_extract(
        monkeypatch,
        {"title": "Song", "webpage_url": "https://w", "duration": 42, "uploader": "U"},
    )
    track = await sources.fetch_track(VIDEO_URL, requested_by="me")
    assert calls == [(VIDEO_URL, False)]
    assert (track.title, track.webpage_url, track.duration) == ("Song", "https://w", 42)


async def test_fetch_track_unwraps_playlist_url(monkeypatch):
    patch_extract(
        monkeypatch,
        {"entries": [{"title": "First", "webpage_url": "https://first"}, {"title": "Second"}]},
    )
    track = await sources.fetch_track(PLAYLIST_URL, requested_by="me")
    assert track.title == "First"


async def test_fetch_track_empty_playlist_raises(monkeypatch):
    patch_extract(monkeypatch, {"entries": []})
    with pytest.raises(SourceError):
        await sources.fetch_track(PLAYLIST_URL, requested_by="me")


async def test_fetch_track_never_uses_media_url_as_display_link(monkeypatch):
    # A full extraction's "url" can be a signed media URL; it must not leak
    # into the track's webpage_url (which gets embedded in chat).
    patch_extract(
        monkeypatch,
        {"title": "Song", "url": "https://cdn.example/signed?token=secret", "acodec": "opus"},
    )
    track = await sources.fetch_track(VIDEO_URL, requested_by="me")
    assert track.webpage_url == ""


# -- resolve_stream -------------------------------------------------------


async def test_resolve_stream_uses_top_level_url(monkeypatch, track_factory):
    patch_extract(monkeypatch, {"url": "https://stream", "acodec": "opus", "formats": []})
    resolved = await sources.resolve_stream(track_factory())
    assert resolved.url == "https://stream"
    assert resolved.acodec == "opus"


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
    resolved = await sources.resolve_stream(track_factory())
    assert resolved.url == "https://f2"
    assert resolved.acodec == "mp4a"


async def test_resolve_stream_captures_top_level_headers(monkeypatch, track_factory):
    patch_extract(
        monkeypatch,
        {"url": "https://stream", "acodec": "opus", "http_headers": {"User-Agent": "yt-ua"}},
    )
    resolved = await sources.resolve_stream(track_factory())
    assert resolved.http_headers == {"User-Agent": "yt-ua"}


async def test_resolve_stream_format_headers_win_over_top_level(monkeypatch, track_factory):
    patch_extract(
        monkeypatch,
        {
            "http_headers": {"User-Agent": "top-ua"},
            "formats": [
                {"url": "https://f1", "acodec": "opus", "http_headers": {"User-Agent": "fmt-ua"}}
            ],
        },
    )
    resolved = await sources.resolve_stream(track_factory())
    assert resolved.http_headers == {"User-Agent": "fmt-ua"}


async def test_resolve_stream_format_without_headers_falls_back_to_top_level(
    monkeypatch, track_factory
):
    patch_extract(
        monkeypatch,
        {
            "http_headers": {"User-Agent": "top-ua"},
            "formats": [{"url": "https://f1", "acodec": "opus"}],
        },
    )
    resolved = await sources.resolve_stream(track_factory())
    assert resolved.http_headers == {"User-Agent": "top-ua"}


async def test_resolve_stream_missing_headers_is_empty_dict(monkeypatch, track_factory):
    patch_extract(monkeypatch, {"url": "https://stream", "acodec": "opus"})
    resolved = await sources.resolve_stream(track_factory())
    assert resolved.http_headers == {}


async def test_resolve_stream_no_audio_raises(monkeypatch, track_factory):
    patch_extract(monkeypatch, {"formats": [{"url": "https://video", "acodec": "none"}]})
    with pytest.raises(SourceError):
        await sources.resolve_stream(track_factory())


async def test_resolve_stream_reuses_fresh_cache(monkeypatch, track_factory):
    calls = patch_extract(monkeypatch, {"url": "https://stream", "acodec": "opus"})
    track = track_factory()
    first = await sources.resolve_stream(track)
    second = await sources.resolve_stream(track)
    assert len(calls) == 1
    assert second is first


async def test_resolve_stream_re_extracts_stale_cache(monkeypatch, track_factory):
    calls = patch_extract(monkeypatch, {"url": "https://stream", "acodec": "opus"})
    track = track_factory()
    first = await sources.resolve_stream(track)
    track.stream.resolved_at -= sources.STREAM_TTL_SECONDS + 1
    second = await sources.resolve_stream(track)
    assert len(calls) == 2
    assert second is not first


async def test_fetch_track_prepopulates_stream_cache(monkeypatch):
    calls = patch_extract(
        monkeypatch,
        {
            "title": "Song",
            "webpage_url": "https://w",
            "duration": 42,
            "uploader": "U",
            "url": "https://stream",
            "acodec": "opus",
        },
    )
    track = await sources.fetch_track(VIDEO_URL, requested_by="me")
    assert track.stream is not None
    assert track.stream.url == "https://stream"
    # Playback's resolve should now be free.
    resolved = await sources.resolve_stream(track)
    assert resolved is track.stream
    assert len(calls) == 1
