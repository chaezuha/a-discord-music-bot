"""yt-dlp integration: searching and resolving audio streams."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import yt_dlp

SEARCH_PREFIXES = {
    "youtube": "ytsearch10",
    "soundcloud": "scsearch10",
}

_BASE_OPTS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
    "socket_timeout": 15,
}


class SourceError(Exception):
    """Extraction failed in a way worth showing to the user."""


@dataclass
class Track:
    title: str
    webpage_url: str
    duration: int | None
    uploader: str
    requested_by: str


def is_url(text: str) -> bool:
    return text.startswith(("http://", "https://"))


def fmt_duration(seconds: float | None) -> str:
    if not seconds:
        return "?:??"
    seconds = int(seconds)
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _clean_error(exc: Exception) -> str:
    message = str(exc)
    if message.startswith("ERROR:"):
        message = message[len("ERROR:") :].strip()
    return message or "unknown extraction error"


def _extract(url_or_query: str, *, flat: bool) -> dict:
    opts = dict(_BASE_OPTS)
    if flat:
        opts["extract_flat"] = True
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url_or_query, download=False)
    except yt_dlp.utils.DownloadError as exc:
        raise SourceError(_clean_error(exc)) from exc
    if info is None:
        raise SourceError("could not extract any information")
    return info


def _first_entry(info: dict) -> dict:
    """Unwrap a playlist-shaped result down to its first playable entry."""
    while "entries" in info:
        entries = [e for e in (info.get("entries") or []) if e]
        if not entries:
            raise SourceError("no playable entries found at that URL")
        info = entries[0]
    return info


def _track_from_entry(entry: dict, requested_by: str) -> Track:
    return Track(
        title=entry.get("title") or "Unknown title",
        webpage_url=entry.get("webpage_url") or entry.get("url") or "",
        duration=entry.get("duration"),
        uploader=entry.get("uploader") or entry.get("channel") or "",
        requested_by=requested_by,
    )


async def search(query: str, source: str, requested_by: str) -> list[Track]:
    """Return up to 10 metadata-only results for a search term."""
    prefix = SEARCH_PREFIXES[source]
    info = await asyncio.to_thread(_extract, f"{prefix}:{query}", flat=True)
    entries = [e for e in (info.get("entries") or []) if e]
    return [_track_from_entry(e, requested_by) for e in entries]


async def fetch_track(url: str, requested_by: str) -> Track:
    """Fully extract a direct URL into a Track (validates it up front)."""
    info = await asyncio.to_thread(_extract, url, flat=False)
    return _track_from_entry(_first_entry(info), requested_by)


async def resolve_stream(track: Track) -> str:
    """Resolve the audio stream URL right before playback (stream URLs expire)."""
    info = await asyncio.to_thread(_extract, track.webpage_url, flat=False)
    info = _first_entry(info)
    url = info.get("url")
    if not url:
        for fmt in reversed(info.get("formats") or []):
            if fmt.get("url") and fmt.get("acodec") not in (None, "none"):
                url = fmt["url"]
                break
    if not url:
        raise SourceError("no playable audio stream found")
    return url
