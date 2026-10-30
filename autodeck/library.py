"""Song record + on-disk cache (<library>/<youtube-id>/song.json, analysis.json)."""
import json
from dataclasses import asdict, dataclass
from pathlib import Path

from . import config

# Bump when analysis output changes; stale analyses are dropped on load (song is kept).
ANALYSIS_VERSION = 1


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
    (d / "song.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    a_path = d / "analysis.json"
    if analysis is None:
        a_path.unlink(missing_ok=True)
    else:
        a_path.write_text(json.dumps(analysis, ensure_ascii=False), encoding="utf-8")


def load_song(song_id: str) -> Song | None:
    d = song_dir(song_id)
    try:
        meta = json.loads((d / "song.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    analysis = None
    try:
        raw = json.loads((d / "analysis.json").read_text(encoding="utf-8"))
        # Version mismatch: drop the analysis (forces re-analysis), keep the owner's genre.
        if raw.get("version") == ANALYSIS_VERSION:
            raw["sections"] = [Section(**s) for s in raw["sections"]]
            analysis = Analysis(**raw)
    except FileNotFoundError:
        pass
    return Song(**meta, analysis=analysis)


def _pool_file() -> Path:
    return config.library_root() / "pool.json"


def save_pool(ids: list[str]) -> None:
    f = _pool_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(ids), encoding="utf-8")


def load_pool() -> list[Song]:
    try:
        ids = json.loads(_pool_file().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    return [s for s in (load_song(i) for i in ids) if s]
