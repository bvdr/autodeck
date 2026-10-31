"""Paths and spec constants. Everything tunable lives here."""
import json
import os
import tempfile
from pathlib import Path

# Spec values (see docs/superpowers/specs/2026-10-30-autodeck-design.md)
WINDOW_SIZE = 10          # songs analysed/rendered ahead in the rolling window
# Hard ceiling on ready-but-unplayed songs of ANY genre. WINDOW_SIZE counts only the active
# genre, and a song's genre is unknown until it's downloaded and analysed, so a rare genre
# made the worker download the whole pool hunting for matches (140 songs / 9 GB in real use).
MAX_READY_AHEAD = 20
FIXED_NEXT = 2            # upcoming songs whose order is locked
BEATMATCH_MAX_PCT = 0.08  # max tempo stretch to beatmatch (8%)
EASE_BARS = 16            # bars over which tempo eases back to the song's own
PHRASE_BARS = 8           # a phrase boundary = any multiple of 8 bars from the first downbeat; prefer 16

DEFAULT_GENRES = ["Pop", "Hip-hop", "Romanian hip-hop", "Romanian sârbe"]
ANY_GENRE = "Any"  # pseudo-genre: no filtering


# Where a Mac app keeps its data (Apple's convention), not a loose folder in the home dir:
#   ~/Library/Application Support/autodeck/library  songs, analyses, moods, session, history, models
#   ~/Library/Caches/autodeck                         Last.fm / Deezer lookups (safe to delete)
# Outside the repo on purpose: audio must never be committed.
APP_SUPPORT = Path.home() / "Library" / "Application Support" / "autodeck"
CACHES = Path.home() / "Library" / "Caches" / "autodeck"
LEGACY_LIBRARY = Path.home() / "autodeck-library"  # before 2026-10-31


def library_root() -> Path:
    # Read the env var on every call (not at import) so tests can monkeypatch it.
    return Path(os.environ.get("AUTODECK_LIBRARY") or APP_SUPPORT / "library")


def cache_root() -> Path:
    """Re-fetchable lookups. With AUTODECK_LIBRARY set (tests, experiments) they stay beside
    that library instead of mixing into the real ~/Library/Caches."""
    if os.environ.get("AUTODECK_LIBRARY"):
        return library_root() / "cache"
    return CACHES


def migrate_legacy_library() -> str | None:
    """Move a pre-2026-10-31 ~/autodeck-library to the Mac location, once, at startup.
    A rename on the same disk: instant, no copy, nothing duplicated. The lookup caches go to
    ~/Library/Caches. Returns a message when something moved."""
    if os.environ.get("AUTODECK_LIBRARY") or not LEGACY_LIBRARY.is_dir():
        return None
    new = library_root()
    if new.exists():
        return None  # never merge two libraries silently; the new one wins
    new.parent.mkdir(parents=True, exist_ok=True)
    os.rename(LEGACY_LIBRARY, new)
    for name in ("lastfm-cache", "deezer-cache"):
        old = new / name
        if old.is_dir() and not (CACHES / name).exists():
            CACHES.mkdir(parents=True, exist_ok=True)
            os.rename(old, CACHES / name)
    return f"Moved your library from {LEGACY_LIBRARY} to {new}"


# Module-level alias requested by the spec; resolved at import time, so code that
# must honor AUTODECK_LIBRARY overrides at runtime should call library_root().
LIBRARY: Path = library_root()


def write_json_atomic(path: Path, data) -> None:
    """Write JSON via temp file + os.replace so a crash never leaves truncated JSON.

    The background worker writes these files constantly; readers must only ever
    see the old or the new complete file. Temp lives in the same dir so the
    replace stays on one filesystem (atomic).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    # Unique temp name per call: the worker thread and the server thread can write
    # the same song.json at once, and a shared "<name>.tmp" would let them clobber
    # each other's half-written file. os.replace makes the last writer win, whole.
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _genres_file() -> Path:
    return library_root() / "genres.json"


def genres() -> list[str]:
    """Owner-editable genre list (without "Any"); falls back to the defaults."""
    try:
        return json.loads(_genres_file().read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):  # missing or corrupt -> defaults
        return list(DEFAULT_GENRES)


def save_genres(items: list[str]) -> None:
    write_json_atomic(_genres_file(), items)
