"""Song record + on-disk cache (<library>/<youtube-id>/song.json, analysis.json)."""
import json
import threading
from dataclasses import asdict, dataclass
from pathlib import Path

from . import config

# Bump when analysis output changes; stale analyses are dropped on load (song is kept).
ANALYSIS_VERSION = 5  # 2: real-song grid/trim/outro/language fixes (Task 11); 3: per-segment bar phase + agreement; 4: raw-gap slips, bpm from raw beats; 5: reliable-grid bpm from the grid slope (no 20 ms frame snapping)

# Held by the worker thread and the server thread while they mutate a Song's fields
# and save it, so an owner genre edit can't interleave with a status update.
# Keep sections short; never hold it during download/analyze.
LOCK = threading.RLock()


@dataclass
class Section:
    start: float
    end: float
    label: str  # "intro" | "body" | "outro"


@dataclass
class Analysis:
    version: int
    bpm: float  # original tempo, octave as detected
    beats: list[float]  # seconds, original time
    downbeats: list[float]
    grid_reliable: bool
    key: str | None  # e.g. "A minor"
    camelot: str | None  # e.g. "8A"
    energy: list[float]  # one value per second, normalized 0..1
    sections: list[Section]
    play_start: float  # after trimming silence or spoken intro
    play_end: float
    genre_guess: str | None
    language: str | None  # ISO code from lyric language ID, e.g. "ro"


@dataclass
class Song:
    id: str  # YouTube video id
    title: str
    channel: str
    duration: float
    status: str  # "queued"|"downloading"|"analyzing"|"ready"|"failed"
    error: str | None = None
    genre: str | None = None  # owner-set; wins over genre_guess
    genre_hint: str | None = None  # from playlist import ("import as Sârbe")
    analysis: Analysis | None = None

    def effective_genre(self) -> str | None:
        return self.genre or (self.analysis.genre_guess if self.analysis else None) or self.genre_hint


def song_dir(song_id: str) -> Path:
    return config.library_root() / song_id


def save_song(song: Song) -> None:
    d = song_dir(song.id)
    d.mkdir(parents=True, exist_ok=True)
    meta = asdict(song)
    analysis = meta.pop("analysis")
    # Analysis lives in its own file so it can be invalidated/replaced without
    # touching owner-set data (genre) in song.json.
    config.write_json_atomic(d / "song.json", meta)
    a_path = d / "analysis.json"
    if analysis is None:
        a_path.unlink(missing_ok=True)
    else:
        config.write_json_atomic(a_path, analysis)


def load_song(song_id: str) -> Song | None:
    d = song_dir(song_id)
    try:
        meta = json.loads((d / "song.json").read_text(encoding="utf-8"))
        Song(**meta)  # validate shape too
    except (FileNotFoundError, ValueError, TypeError):
        return None  # missing or corrupt song.json == no such song
    analysis = None
    try:
        raw = json.loads((d / "analysis.json").read_text(encoding="utf-8"))
        # Version mismatch: drop the analysis (forces re-analysis), keep the owner's genre.
        if raw.get("version") == ANALYSIS_VERSION:
            raw["sections"] = [Section(**s) for s in raw["sections"]]
            analysis = Analysis(**raw)
    except FileNotFoundError:
        pass
    except (ValueError, TypeError, KeyError):
        pass  # corrupt analysis.json: same as a version mismatch, drop it and keep the song
    if analysis is None and meta.get("status") == "ready":
        # "ready" means "has a current analysis". Without one, the worker (which only
        # prepares queued/downloading/analyzing) would never redo it and the planner
        # would never play it. Requeue; audio is cached, so this re-analyzes only.
        meta["status"] = "queued"
    return Song(**meta, analysis=analysis)


def _pool_file() -> Path:
    return config.library_root() / "pool.json"


def save_pool(ids: list[str]) -> None:
    config.write_json_atomic(_pool_file(), ids)


def load_pool() -> list[Song]:
    try:
        ids = json.loads(_pool_file().read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return []  # missing or corrupt pool.json: start empty rather than crash
    return [s for s in (load_song(i) for i in ids) if s]
