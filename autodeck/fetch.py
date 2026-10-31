"""YouTube URL classification, playlist expansion and audio download (yt-dlp)."""
import glob
import os
import re
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import yt_dlp
from yt_dlp.cookies import CookieLoadError
from yt_dlp.utils import DownloadError

from . import config
from .library import Song, audio_path, song_dir

# Cookie-READ failures. Markers are deliberately narrow: yt-dlp's sign-in / age-gate /
# bot-check errors end with "Use --cookies-from-browser or --cookies ...", so bare
# "cookie"/"browser" would mislabel them and hide the real reason. The primary signal is
# yt-dlp's own CookieLoadError type; text markers cover the FileNotFoundError /
# keyring / decrypt failures it lets through untyped.
_COOKIE_MARKERS = ("cookies database", "keyring", "decrypt", "failed to load cookies")
# Placeholder titles YouTube gives playlist entries we can't fetch.
_UNAVAILABLE_TITLES = {"[private video]", "[deleted video]"}
_YT_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}


class FetchError(Exception):
    """Human-readable download/expansion failure."""


def classify_url(url: str) -> tuple[str, str]:
    """("video", id) or ("playlist", list_id); ValueError for anything else.

    A watch link wins over a trailing list=: the owner pasted one song, not the
    50-song radio mix YouTube attaches (Review Focus 1). Only /playlist imports a list.
    """
    u = urlparse(url.strip())
    host = (u.hostname or "").lower()
    q = parse_qs(u.query)
    if host == "youtu.be":
        vid = u.path.strip("/")
        if vid:
            return "video", vid
    elif host in _YT_HOSTS:
        if u.path == "/watch" and q.get("v"):
            return "video", q["v"][0]
        if u.path == "/playlist" and q.get("list"):
            return "playlist", q["list"][0]
    raise ValueError(f"Not a YouTube video or playlist URL: {url}")


def _browser() -> str:
    # Settings > "YouTube login from"; the AUTODECK_BROWSER env var still overrides it.
    return config.youtube_browser()


def _cookie_error() -> str:
    b = config.BROWSERS.get(_browser(), _browser())
    return f"Couldn't read YouTube login from {b}. Is {b} logged in to YouTube?"


def _cookie_opts() -> dict:
    # Passed by default (Liked Music needs login; public content ignores it). Read live
    # from the browser by yt-dlp; we never write or log cookie values.
    # AUTODECK_BROWSER=none skips cookies entirely (public content only): reading Chrome
    # cookies blocks on a macOS Keychain prompt, which hangs unattended runs (tests, CI).
    if _browser().lower() == "none":
        return {}
    return {"cookiesfrombrowser": (_browser(),)}


def _short_reason(exc: Exception) -> str:
    msg = re.sub(r"\x1b\[[0-9;]*m", "", str(exc))  # strip ANSI colours
    msg = msg.removeprefix("ERROR: ").strip()
    return msg.splitlines()[0][:200] if msg else "unknown error"


def _is_cookie_error(exc: Exception) -> bool:
    return isinstance(exc, CookieLoadError) or any(m in str(exc).lower() for m in _COOKIE_MARKERS)


def _song_from_entry(e: dict) -> Song | None:
    vid, title = e.get("id"), e.get("title")
    if not vid or not title or title.strip().lower() in _UNAVAILABLE_TITLES:
        return None  # private/deleted entry: skip, don't let it poison the pool
    return Song(
        id=vid,
        title=title,
        channel=e.get("channel") or e.get("uploader") or "",
        duration=float(e.get("duration") or 0.0),  # flat playlist entries may lack it
        status="queued",
    )


def read_playlist(url: str) -> tuple[str | None, list[Song]]:
    """(title, songs) for a video or playlist URL, songs status "queued". No audio is downloaded.
    The title is the playlist's name on YouTube (saved playlists start with it); for a video
    it's the video title."""
    kind, _ = classify_url(url)
    opts = {"quiet": True, "skip_download": True, "noplaylist": kind == "video", **_cookie_opts()}
    if kind == "playlist":
        opts["extract_flat"] = "in_playlist"  # titles/ids only: one request, not one per song
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:
        raise FetchError(_cookie_error() if _is_cookie_error(exc) else f"Couldn't read {url}: {_short_reason(exc)}") from exc
    entries = info.get("entries") if kind == "playlist" else [info]
    return info.get("title"), [s for s in (_song_from_entry(e) for e in entries or [] if e) if s]


def expand(url: str) -> list[Song]:
    """Songs for a video or playlist URL, status "queued". No audio is downloaded."""
    return read_playlist(url)[1]


def search(query: str, n: int = 10) -> list[Song]:
    """Top YouTube search results for a song search box, status "queued". Titles/ids only."""
    opts = {"quiet": True, "skip_download": True, "extract_flat": True, **_cookie_opts()}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"ytsearch{n}:{query}", download=False)
    except Exception as exc:
        raise FetchError(_cookie_error() if _is_cookie_error(exc) else f"Search failed: {_short_reason(exc)}") from exc
    return [s for s in (_song_from_entry(e) for e in (info or {}).get("entries") or [] if e) if s]


def download(song: Song) -> Path:
    """Download the song's audio to library.audio_path (Opus, idempotent). Raises FetchError."""
    d = song_dir(song.id)
    out = audio_path(song.id)
    if out.exists():
        return out  # never re-download a cached song
    opts = {
        # Prefer YouTube's Opus stream: FFmpegExtractAudio then only remuxes it into audio.opus,
        # bit-for-bit the audio YouTube sent (no re-encode, no quality loss). Only when no
        # Opus stream exists (rare) does it transcode, at 192 kbps (transparent for Opus).
        "format": "bestaudio[acodec=opus]/bestaudio",
        "quiet": True,
        "noprogress": True,  # quiet=True still prints progress bars
        "noplaylist": True,
        # Work under a scratch name and rename only on success: a crash mid-download or
        # mid-conversion must never leave a truncated audio file that looks cached.
        # %(ext)s becomes opus after the post-processor.
        "outtmpl": str(d / "dl.%(ext)s"),
        "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "opus", "preferredquality": "192"}],
        **_cookie_opts(),
    }
    scratch = d / "dl.opus"
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([f"https://www.youtube.com/watch?v={song.id}"])
        if not scratch.exists():
            raise FetchError(f"Couldn't download '{song.title}': no audio file was produced")
        os.replace(scratch, out)  # atomic: the audio file is either absent or complete
    except FetchError:
        raise
    except Exception as exc:
        if _is_cookie_error(exc):
            raise FetchError(_cookie_error()) from exc
        if isinstance(exc, DownloadError):
            raise FetchError(f"Couldn't download '{song.title}': {_short_reason(exc)}") from exc
        raise
    finally:
        # Leftovers (dl.*, *.part, *.ytdl) from failures; no-op after a successful rename.
        for f in glob.glob(glob.escape(str(d)) + "/dl.*"):
            Path(f).unlink(missing_ok=True)
    return out
