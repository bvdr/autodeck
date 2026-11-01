import os

import numpy as np
import soundfile as sf

from autodeck import config, jobs, library, stems
from autodeck.engine import Engine
from autodeck.library import Analysis, Song

SR = 48000


def _audio(sid, seconds=3.0):
    """A stereo 48 kHz Opus 'song': a tone on the left, a different one on the right."""
    t = np.arange(int(SR * seconds)) / SR
    y = np.stack([0.3 * np.sin(2 * np.pi * 220 * t), 0.3 * np.sin(2 * np.pi * 330 * t)], 1).astype(np.float32)
    p = library.song_dir(sid) / "audio.opus"
    p.parent.mkdir(parents=True, exist_ok=True)
    sf.write(p, y, SR, format="OGG", subtype="OPUS")
    return p


def _fake_split(monkeypatch, k=0.5):
    # No model in unit tests: "vocals" = half the mix.
    monkeypatch.setattr(stems, "_split", lambda mix: (k * mix).astype(np.float32))


def test_separate_writes_aligned_stems_next_to_the_song(tmp_library, monkeypatch):
    _fake_split(monkeypatch)
    src = _audio("aaaaaaaaaaa")
    assert not stems.has("aaaaaaaaaaa") and stems.activity("aaaaaaaaaaa") is None
    stems.separate("aaaaaaaaaaa")
    assert stems.has("aaaaaaaaaaa")
    mix, _ = sf.read(src, dtype="float32")
    v, sr_v = sf.read(stems.path("aaaaaaaaaaa", "vocals"), dtype="float32")
    inst, _ = sf.read(stems.path("aaaaaaaaaaa", "instrumental"), dtype="float32")
    assert stems.path("aaaaaaaaaaa", "vocals").parent == src.parent
    # Same length as the song, sample for sample: the stems follow the main render's time map.
    assert sr_v == SR and len(v) == len(inst) == len(mix)
    # vocals + instrumental == the song (up to Opus coding noise).
    mid = slice(SR // 2, -SR // 2)
    assert np.sqrt(np.mean((v[mid] + inst[mid] - mix[mid]) ** 2)) < 0.02
    act = stems.activity("aaaaaaaaaaa")
    assert len(act) == 3 and all(0.4 < a < 0.6 for a in act)


def test_prune_keeps_wanted_and_newest(tmp_library, monkeypatch):
    _fake_split(monkeypatch)
    ids = [f"song{i:07d}" for i in range(5)]
    for k, sid in enumerate(ids):
        _audio(sid, 1.0)
        stems.separate(sid)
        os.utime(library.song_dir(sid) / "stems.json", (1000 + k, 1000 + k))  # song0 oldest
    gone = stems.prune(keep={ids[0]}, max_songs=3)
    assert gone == 2
    # The wanted one survives though it's the oldest; then the two newest.
    assert [stems.has(s) for s in ids] == [True, False, False, True, True]
    assert (library.song_dir(ids[1]) / "audio.opus").exists()  # only stems are pruned


def _ready(i):
    return Song(id=i, title=i, channel="c", duration=100, status="ready")


def test_worker_runs_stems_only_after_the_next_songs_are_prepared(tmp_library, monkeypatch):
    pool = [_ready("on_air"), Song(id="next", title="n", channel="c", duration=100, status="queued"),
            Song(id="far", title="f", channel="c", duration=100, status="queued")]
    w = jobs.Worker(lambda: (pool, ["next"], "Any", set()), stems_wanted=lambda: ["on_air"])
    # The next song isn't ready: its download/analysis goes first, stems wait.
    assert w.next_job(pool, ["next"], "Any") == ("prep", "next")
    # Next song ready: stems for the ON AIR song beat filling the rest of the window.
    pool[1].status = "ready"
    assert w.next_job(pool, ["next"], "Any") == ("stems", "on_air")
    # Once they exist, the window carries on.
    monkeypatch.setattr(stems, "has", lambda sid: True)
    assert w.next_job(pool, ["next"], "Any") == ("prep", "far")


def test_stems_failure_is_remembered_and_never_breaks_the_worker(tmp_library, monkeypatch):
    calls = []

    def boom(sid):
        calls.append(sid)
        raise RuntimeError("model download failed")

    monkeypatch.setattr(stems, "separate", boom)
    changed = []
    w = jobs.Worker(lambda: ([], [], "Any", set()), on_change=lambda: changed.append(1),
                    stems_wanted=lambda: ["x"])
    assert w.next_job([], [], "Any") == ("stems", "x")
    w._stems("x")  # logs, doesn't raise
    assert calls == ["x"] and changed and "x" in w.stems_failed
    assert w.next_job([], [], "Any") is None  # not retried in a loop


def test_engine_wants_stems_only_for_on_air_and_the_next_few(tmp_library, monkeypatch):
    an = Analysis(version=library.ANALYSIS_VERSION, bpm=120, beats=[], downbeats=[], grid_reliable=False,
                  key=None, camelot=None, energy=[0.5] * 10, sections=[], play_start=0, play_end=10,
                  genre_guess=None, language=None)
    pool = [Song(id=f"s{i:010d}", title=f"t{i}", channel="c", duration=10, status="ready", analysis=an)
            for i in range(8)]
    eng = Engine(pool)
    eng.playing = pool[0].id
    eng.upcoming = [s.id for s in pool[1:]]
    want = eng.stems_wanted()
    assert want == [pool[0].id] + [s.id for s in pool[1:1 + config.STEMS_AHEAD]]
    st = eng.state()["stems"]
    assert set(st) == set(want) and set(st.values()) == {"preparing"}
