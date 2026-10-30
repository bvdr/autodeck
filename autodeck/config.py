"""Paths and spec constants. Everything tunable lives here."""
import json
import os
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


def _genres_file() -> Path:
    return library_root() / "genres.json"


def genres() -> list[str]:
    """Owner-editable genre list (without "Any"); falls back to the defaults."""
    try:
        return json.loads(_genres_file().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return list(DEFAULT_GENRES)


def save_genres(items: list[str]) -> None:
    f = _genres_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
