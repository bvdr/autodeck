import json
import shutil

from autodeck import library
from autodeck.library import Analysis, Section, Song


def test_heavy_deps_import():
    # Smoke test: these are the packages most likely to break on arm64 / py3.12.
    import beat_this.inference  # noqa: F401
    import essentia.standard  # noqa: F401
    import faster_whisper  # noqa: F401
    import onnxruntime  # noqa: F401
    import pyrubberband  # noqa: F401
    import yt_dlp  # noqa: F401

    assert shutil.which("rubberband")


def _analysis(version=library.ANALYSIS_VERSION, guess=None):
    return Analysis(
        version=version, bpm=120.0, beats=[0.5, 1.0], downbeats=[0.5],
        grid_reliable=True, key="A minor", camelot="8A", energy=[0.1, 0.9],
        sections=[Section(0.0, 1.0, "intro")], play_start=0.0, play_end=2.0,
        genre_guess=guess, language="ro",
    )


def test_roundtrip(tmp_library):
    song = Song(id="abc", title="T", channel="C", duration=2.0, status="ready", analysis=_analysis())
    library.save_song(song)
    assert library.load_song("abc") == song
    assert library.load_song("missing") is None


def test_version_mismatch_drops_analysis_keeps_genre(tmp_library):
    song = Song(id="x", title="T", channel="C", duration=2.0, status="ready",
                genre="Romanian sârbe", analysis=_analysis(version=0))
    library.save_song(song)
    loaded = library.load_song("x")
    assert loaded.analysis is None
    assert loaded.genre == "Romanian sârbe"


def test_effective_genre_priority():
    s = Song(id="x", title="T", channel="C", duration=1.0, status="ready",
             genre_hint="Pop", analysis=_analysis(guess="Hip-hop"))
    # The owner's "import as" genre beats the model's guess (spec: assign a genre to a
    # whole playlist at import); the owner's per-song fix beats both.
    assert s.effective_genre() == "Pop"
    s.genre = "Romanian sârbe"
    assert s.effective_genre() == "Romanian sârbe"
    s.genre, s.genre_hint = None, None
    assert s.effective_genre() == "Hip-hop"


def test_library_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTODECK_LIBRARY", str(tmp_path))
    assert library.song_dir("x") == tmp_path / "x"


def test_pool_and_genres_roundtrip(tmp_library):
    from autodeck import config
    library.save_pool(["a", "b"])
    for i in "ab":
        library.save_song(Song(id=i, title=i, channel="c", duration=1.0, status="queued"))
    assert [s.id for s in library.load_pool()] == ["a", "b"]
    assert config.genres() == config.DEFAULT_GENRES
    config.save_genres(["Jazz"])
    assert config.genres() == ["Jazz"]


def _song(**kw):
    return Song(id="c", title="T", channel="C", duration=2.0, status="ready", **kw)


def test_corrupt_analysis_drops_analysis_keeps_song(tmp_library):
    library.save_song(_song(genre="Pop", analysis=_analysis()))
    (library.song_dir("c") / "analysis.json").write_text('{"version": 1, "bp')
    loaded = library.load_song("c")
    assert loaded.analysis is None and loaded.genre == "Pop"
    assert loaded.status == "queued"  # a "ready" song without analysis would never be re-prepared


def test_stale_analysis_version_requeues_ready_song(tmp_library):
    from autodeck.jobs import Worker
    library.save_song(_song(genre="Pop", analysis=_analysis(version=library.ANALYSIS_VERSION - 1)))
    loaded = library.load_song("c")
    assert (loaded.analysis, loaded.status, loaded.genre) == (None, "queued", "Pop")
    w = Worker(lambda: ([loaded], [], "Any", set()), lambda: None)
    assert w.wanted([loaded], [], "Any") == ["c"]


def test_corrupt_song_json_is_missing_song(tmp_library):
    library.save_song(_song())
    (library.song_dir("c") / "song.json").write_text("")
    assert library.load_song("c") is None
    library.save_pool(["c"])
    assert library.load_pool() == []  # pool survives a bad entry


def test_corrupt_genres_falls_back_to_defaults(tmp_library):
    from autodeck import config
    (tmp_library / "genres.json").write_text("[\"Po")
    assert config.genres() == config.DEFAULT_GENRES


def test_legacy_library_moves_to_application_support_once(tmp_path, monkeypatch):
    # Owner: data belongs in the normal Mac place, not ~/autodeck-library. One-time rename.
    from autodeck import config
    home = tmp_path / "home"
    legacy = home / "autodeck-library"
    (legacy / "abc").mkdir(parents=True)
    (legacy / "abc" / "song.json").write_text("{}")
    (legacy / "lastfm-cache").mkdir()
    monkeypatch.delenv("AUTODECK_LIBRARY", raising=False)
    monkeypatch.setattr(config, "LEGACY_LIBRARY", legacy)
    monkeypatch.setattr(config, "APP_SUPPORT", home / "Library/Application Support/autodeck")
    monkeypatch.setattr(config, "CACHES", home / "Library/Caches/autodeck")
    assert "Moved" in config.migrate_legacy_library()
    new = home / "Library/Application Support/autodeck/library"
    assert (new / "abc" / "song.json").exists() and not legacy.exists()
    assert (home / "Library/Caches/autodeck/lastfm-cache").is_dir()
    assert config.migrate_legacy_library() is None  # nothing left to move


def test_no_migration_when_library_is_overridden(tmp_path, monkeypatch):
    from autodeck import config
    monkeypatch.setenv("AUTODECK_LIBRARY", str(tmp_path / "x"))
    monkeypatch.setattr(config, "LEGACY_LIBRARY", tmp_path)
    assert config.migrate_legacy_library() is None
    assert config.cache_root() == tmp_path / "x" / "cache"
