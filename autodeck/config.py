"""Paths and spec constants. Everything tunable lives here."""
import json
import os
import tempfile
from pathlib import Path

# Spec values (see docs/superpowers/specs/2026-10-30-autodeck-design.md)
WINDOW_SIZE = 10          # songs analysed/rendered ahead in the rolling window
FIXED_NEXT = 2            # upcoming songs whose order is locked
BEATMATCH_MAX_PCT = 0.08  # max tempo stretch to beatmatch (8%)
EASE_BARS = 16            # bars over which tempo eases back to the song's own
PHRASE_BARS = 8           # a phrase boundary = any multiple of 8 bars from the first downbeat; prefer 16

DEFAULT_GENRES = ["Pop", "Hip-hop", "Romanian hip-hop", "Romanian sârbe"]
ANY_GENRE = "Any"  # pseudo-genre: no filtering


def library_root() -> Path:
    # Read the env var on every call (not at import) so tests can monkeypatch it.
    # The cache lives outside the repo on purpose: audio must never be committed.
    return Path(os.environ.get("AUTODECK_LIBRARY") or Path.home() / "autodeck-library")


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
