"""yt-dlp integration: searching and resolving audio streams."""

from __future__ import annotations

import asyncio
import ipaddress
import os
import threading
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse

import discord
import yt_dlp

SEARCH_PREFIXES = {
    "youtube": "ytsearch10",
    "soundcloud": "scsearch10",
}

# Direct-URL playback is limited to these sites unless ALLOWED_URL_DOMAINS
# overrides them. Subdomains are included automatically.
DEFAULT_ALLOWED_DOMAINS = frozenset({"youtube.com", "youtu.be", "soundcloud.com", "bandcamp.com"})

TITLE_DISPLAY_LIMIT = 200
ERROR_DISPLAY_LIMIT = 500

# Bounds concurrent yt-dlp extractions across all guilds. A thread-level gate
# (rather than an asyncio one) so it holds even for extractions whose awaiting
# coroutine was cancelled.
MAX_CONCURRENT_EXTRACTIONS = 4
_extract_gate = threading.BoundedSemaphore(MAX_CONCURRENT_EXTRACTIONS)


def _parse_allowed_domains(raw: str | None) -> frozenset[str] | None:
    """ALLOWED_URL_DOMAINS: unset -> defaults, a list -> replaces them, * -> no limits.

    Returns None when protection is disabled.
    """
    if raw is None or not raw.strip():
        return DEFAULT_ALLOWED_DOMAINS
    if raw.strip() == "*":
        return None
    domains = {d.strip().lower().lstrip(".") for d in raw.split(",")}
    domains.discard("")
    if not domains:
        raise ValueError(
            "ALLOWED_URL_DOMAINS must be a comma-separated list of domains, "
            "'*' to allow every site, or unset for the built-in defaults."
        )
    return frozenset(domains)


ALLOWED_DOMAINS = _parse_allowed_domains(os.getenv("ALLOWED_URL_DOMAINS"))

_BASE_OPTS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
    "socket_timeout": 15,
}
if ALLOWED_DOMAINS is not None:
    # The generic extractor will fetch any URL; keep it off while the domain
    # allowlist is active (ALLOWED_URL_DOMAINS=* restores it).
    _BASE_OPTS["allowed_extractors"] = ["default", "-generic"]

# Resolved stream URLs are reused within this window. YouTube URLs last ~6h;
# staying well under that keeps loops and prefetches safe.
STREAM_TTL_SECONDS = 15 * 60


class SourceError(Exception):
    """Extraction failed in a way worth showing to the user."""


@dataclass
class ResolvedStream:
    url: str
    acodec: str | None
    resolved_at: float


@dataclass
class Track:
    title: str
    webpage_url: str
    duration: int | None
    uploader: str
    requested_by: str
    stream: ResolvedStream | None = field(default=None, compare=False)


def is_url(text: str) -> bool:
    return text.startswith(("http://", "https://"))


def check_url_allowed(url: str) -> None:
    """Reject URLs outside the domain allowlist (SSRF guard).

    This limits which sites yt-dlp is pointed at; redirects and extractor
    sub-requests are still up to yt-dlp itself.
    """
    if ALLOWED_DOMAINS is None:
        return
    parsed = urlparse(url)
    if parsed.username or parsed.password:
        raise SourceError("URLs with embedded credentials aren't allowed.")
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        raise SourceError("that URL has no hostname.")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        if any(host == d or host.endswith("." + d) for d in ALLOWED_DOMAINS):
            return
    else:
        # Literal IPs are only playable if explicitly allowlisted, and never
        # private/loopback/link-local/metadata ranges.
        if ip.is_global and host in ALLOWED_DOMAINS:
            return
        raise SourceError("URLs pointing at IP addresses aren't allowed.")
    allowed = ", ".join(sorted(ALLOWED_DOMAINS))
    raise SourceError(
        f"that site isn't on the allowed list ({allowed}). "
        "The bot owner can change this with the ALLOWED_URL_DOMAINS setting."
    )


def truncate(text: str, limit: int) -> str:
    """Cap text at `limit` characters, ellipsizing when it was longer."""
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "\N{HORIZONTAL ELLIPSIS}"


def fmt_duration(seconds: float | None) -> str:
    if not seconds:
        return "?:??"
    seconds = int(seconds)
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def fmt_title(track: Track) -> str:
    """Bold title, hyperlinked to the track's page when we have one.

    The <> around the URL stops Discord from unfurling a preview embed.
    Truncation happens before escaping so Markdown can't be cut in half.
    """
    title = discord.utils.escape_markdown(truncate(track.title, TITLE_DISPLAY_LIMIT))
    if track.webpage_url:
        return f"[**{title}**](<{track.webpage_url}>)"
    return f"**{title}**"


def _clean_error(exc: Exception) -> str:
    message = str(exc)
    if message.startswith("ERROR:"):
        message = message[len("ERROR:") :].strip()
    return truncate(message, ERROR_DISPLAY_LIMIT) or "unknown extraction error"


def _extract(url_or_query: str, *, flat: bool) -> dict:
    opts = dict(_BASE_OPTS)
    if flat:
        opts["extract_flat"] = True
    try:
        with _extract_gate, yt_dlp.YoutubeDL(opts) as ydl:
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


def _track_from_entry(entry: dict, requested_by: str, *, flat: bool = False) -> Track:
    # In a flat search entry "url" is the track's page; in a full extraction
    # it can be a signed media URL, which must never become the display link.
    webpage_url = entry.get("webpage_url") or ""
    if not webpage_url and flat:
        webpage_url = entry.get("url") or ""
    return Track(
        title=entry.get("title") or "Unknown title",
        webpage_url=webpage_url,
        duration=entry.get("duration"),
        uploader=entry.get("uploader") or entry.get("channel") or "",
        requested_by=requested_by,
    )


def _stream_from_info(info: dict) -> ResolvedStream:
    """Pick the playable audio URL (and its codec) out of a full extraction."""
    url = info.get("url")
    acodec = info.get("acodec")
    if not url:
        for fmt in reversed(info.get("formats") or []):
            if fmt.get("url") and fmt.get("acodec") not in (None, "none"):
                url = fmt["url"]
                acodec = fmt.get("acodec")
                break
    if not url:
        raise SourceError("no playable audio stream found")
    return ResolvedStream(url=url, acodec=acodec, resolved_at=time.monotonic())


async def search(query: str, source: str, requested_by: str) -> list[Track]:
    """Return up to 10 metadata-only results for a search term."""
    prefix = SEARCH_PREFIXES[source]
    info = await asyncio.to_thread(_extract, f"{prefix}:{query}", flat=True)
    entries = [e for e in (info.get("entries") or []) if e]
    return [_track_from_entry(e, requested_by, flat=True) for e in entries]


async def fetch_track(url: str, requested_by: str) -> Track:
    """Fully extract a direct URL into a Track (validates it up front)."""
    check_url_allowed(url)
    info = await asyncio.to_thread(_extract, url, flat=False)
    entry = _first_entry(info)
    track = _track_from_entry(entry, requested_by)
    # We already paid for a full extraction; keep the stream so playback
    # doesn't have to extract again.
    try:
        track.stream = _stream_from_info(entry)
    except SourceError:
        pass  # playback's own resolve will surface the error
    return track


async def resolve_stream(track: Track) -> ResolvedStream:
    """Resolve the audio stream for a track, reusing a fresh cached result.

    Stream URLs expire, so cached results are only reused within
    STREAM_TTL_SECONDS of being resolved.
    """
    cached = track.stream
    if cached is not None and time.monotonic() - cached.resolved_at < STREAM_TTL_SECONDS:
        return cached
    info = await asyncio.to_thread(_extract, track.webpage_url, flat=False)
    track.stream = _stream_from_info(_first_entry(info))
    return track.stream
