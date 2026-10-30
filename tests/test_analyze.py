import json

import numpy as np
import pytest

from autodeck.analyze import analyze, write_peaks

pytestmark = pytest.mark.slow


@pytest.fixture
def clicked(click_track):
    return analyze(click_track(120, 90), "t", "c")


def test_tempo_and_downbeats(clicked):
    a = clicked
    assert a.bpm == pytest.approx(120, abs=1)
    gaps = np.diff(a.downbeats)
    assert np.allclose(gaps, 2.0, atol=0.05)
    assert a.grid_reliable is True
    assert a.genre_guess is None and a.language is None


def test_trim_silence(silence_then_track):
    a = analyze(silence_then_track(), "t", "c")
    dur = 90 + 16
    assert a.play_start == pytest.approx(8, abs=0.5)
    assert a.play_end == pytest.approx(dur - 8, abs=0.5)


def test_noise_has_no_reliable_grid(noise_file):
    a = analyze(noise_file(60), "t", "c")
    assert a.grid_reliable is False
    assert a.bpm > 0 and np.isfinite(a.bpm)  # planner divides by it


def test_beatless_intro_skipped(noise_then_track):
    a = analyze(noise_then_track(10), "t", "c")
    assert a.grid_reliable
    assert a.play_start == pytest.approx(10, abs=1)


def test_sections(clicked):
    a = clicked
    s = a.sections
    assert s[0].label == "intro" and s[-1].label == "outro"
    assert s[0].start == pytest.approx(a.play_start)
    assert s[-1].end == pytest.approx(a.play_end)
    for x, y in zip(s, s[1:]):
        assert x.end == y.start
    assert a.grid_reliable
    for edge in [s[0].start, *[x.end for x in s]]:
        assert min(abs(edge - d) for d in a.downbeats) < 1e-6


def test_energy(clicked):
    assert len(clicked.energy) == pytest.approx(90, abs=1)
    assert max(clicked.energy) == 1.0


def test_key(a_minor_triad):
    a = analyze(a_minor_triad, "t", "c")
    assert a.camelot == "8A"


def test_peaks(click_track, tmp_library):
    out = tmp_library / "peaks.json"
    write_peaks(click_track(120, 20), out)
    p = json.loads(out.read_text())
    assert len(p) == 2000 and max(p) > 0 and min(p) >= 0


def test_short_track_has_intro_and_outro(click_track):
    a = analyze(click_track(120, 34), "t", "c")
    s = a.sections
    assert a.grid_reliable and len(s) >= 2
    assert s[0].label == "intro" and s[-1].label == "outro"


def test_dropout_bar_keeps_reliable_and_start(tmp_path):
    import soundfile as sf
    from conftest import SR, make_click_signal

    y = make_click_signal(120, 80)
    y[int(9.7 * SR) : int(11.99 * SR)] = 0  # one bar fails the pulse gate (checked: pulse array has a 0)
    p = tmp_path / "drop.flac"
    sf.write(p, y, SR)
    from autodeck import analyze as A

    # Guard against a vacuous test: the dropout must really fail the gate.
    b, d = A._beats(y)
    assert not A._bar_has_pulse(y, d, b)[:16].all()
    a = analyze(p, "t", "c")
    assert a.grid_reliable is True
    assert a.play_start == pytest.approx(0, abs=1)


def test_unsnappable_trim_marks_grid_unreliable(tmp_path, monkeypatch):
    import soundfile as sf
    from autodeck import analyze as A
    from conftest import SR

    # Loud audio only after the last downbeat -> start can't snap to the grid.
    y = np.zeros(44 * SR, dtype=np.float32)
    y[42 * SR :] = 0.3 * np.random.default_rng(3).standard_normal(2 * SR)
    p = tmp_path / "late.flac"
    sf.write(p, y, SR)
    monkeypatch.setattr(A, "_beats", lambda _y: (np.arange(0, 38, 0.5), np.arange(0, 38, 2.0)))
    monkeypatch.setattr(A, "_bar_has_pulse", lambda *_: np.ones(18, dtype=bool))
    a = analyze(p, "t", "c")
    assert a.grid_reliable is False
    assert a.play_start == pytest.approx(42, abs=1)
