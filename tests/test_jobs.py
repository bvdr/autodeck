import json
import threading

import pytest

from autodeck import analyze, config, fetch, jobs, library
from autodeck.library import Analysis, Song


def _song(i, status="queued", **kw):
    return Song(id=i, title=f"t{i}", channel="c", duration=100, status=status, **kw)


def _analysis():
    return Analysis(version=library.ANALYSIS_VERSION, bpm=120, beats=[], downbeats=[], grid_reliable=True,
                    key=None, camelot=None, energy=[], sections=[], play_start=0, play_end=1,
                    genre_guess=None, language=None)


def _worker(pool, upcoming=(), genre="Any", on_change=lambda: None):
    return jobs.Worker(lambda: (pool, list(upcoming), genre), on_change)


def test_wanted_order_and_limits(tmp_library):
    pool = [_song(f"q{i}") for i in range(15)]
    pool[9].genre_hint = "Pop"
    pool += [_song("r", "ready"), _song("f", "failed")]
    w = _worker(pool)
    ids = w.wanted(pool, ["q3", "r"], "Pop")
    assert len(ids) == config.WINDOW_SIZE
    assert ids[0] == "q3" and ids[1] == "q9"
    assert "r" not in ids and "f" not in ids
    # seeded: a second worker picks the same remainder
    assert _worker(pool).wanted(pool, ["q3", "r"], "Pop") == ids


class _Stopper:
    """Context manager: start the worker, always stop it (no leaked threads)."""
    def __init__(self, w): self.w = w

    def __enter__(self):
        self.w.start(); return self

    def __exit__(self, *a):
        self.w.stop()


def test_fetch_error_marks_failed_and_continues(tmp_library, monkeypatch):
    pool = [_song("bad"), _song("good")]
    ev = threading.Event()

    def dl(song):
        if song.id == "bad":
            raise fetch.FetchError("video removed")
        p = library.song_dir(song.id) / "audio.flac"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x")
        return p

    monkeypatch.setattr(fetch, "download", dl)
    monkeypatch.setattr(analyze, "analyze", lambda *a: _analysis())
    monkeypatch.setattr(analyze, "write_peaks", lambda a, o, *r: o.write_text("[]"))
    w = _worker(pool, on_change=lambda: ev.set() if pool[1].status == "ready" else None)
    with _Stopper(w):
        assert ev.wait(10)
    assert pool[0].status == "failed" and pool[0].error == "video removed"
    assert pool[1].status == "ready" and pool[1].analysis
    assert (library.song_dir("good") / "peaks.json").exists()


def test_analyze_exception_marks_failed(tmp_library, monkeypatch):
    pool = [_song("a")]
    ev = threading.Event()
    monkeypatch.setattr(fetch, "download", lambda s: library.song_dir(s.id) / "audio.flac")

    def boom(*a):
        raise RuntimeError("nope")

    monkeypatch.setattr(analyze, "analyze", boom)
    w = _worker(pool, on_change=lambda: ev.set() if pool[0].status == "failed" else None)
    with _Stopper(w):
        assert ev.wait(10)
    assert "nope" in pool[0].error


def test_cached_song_skips_download_and_analyze(tmp_library, monkeypatch):
    s = _song("c", analysis=_analysis())
    library.save_song(s)
    (library.song_dir("c") / "audio.flac").write_bytes(b"x")
    pool = [_song("c")]
    ev = threading.Event()
    called = []
    monkeypatch.setattr(fetch, "download", lambda s: called.append("d"))
    monkeypatch.setattr(analyze, "analyze", lambda *a: called.append("a"))
    monkeypatch.setattr(analyze, "write_peaks", lambda a, o, *r: o.write_text("[]"))
    w = _worker(pool, on_change=lambda: ev.set() if pool[0].status == "ready" else None)
    with _Stopper(w):
        assert ev.wait(10)
    assert called == [] and pool[0].analysis
    assert (library.song_dir("c") / "peaks.json").exists()


def test_concurrent_atomic_writes(tmp_path):
    path = tmp_path / "x.json"
    def writer(n):
        for _ in range(50):
            config.write_json_atomic(path, {"w": n, "pad": "x" * 5000})
    ts = [threading.Thread(target=writer, args=(n,)) for n in range(4)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert json.loads(path.read_text())["w"] in range(4)
    assert [p.name for p in tmp_path.iterdir()] == ["x.json"]  # no temp litter
