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
    # genre wiring (Task 5): a click track has no genre; language is whisper's noise guess, must just not crash
    assert a.genre_guess is None


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


def test_tempo_fallback_is_not_constant(click_track):
    import librosa
    from autodeck import analyze as A

    y, _ = librosa.load(click_track(100, 30), sr=A.SR)
    assert A._tempo_fallback(y) == pytest.approx(100, rel=0.05)  # was always FALLBACK_BPM (120)


def test_regular_grid_rebuilds_bars_from_steady_beats():
    # Real-song failure modes (Task 11 smoke): beat_this beats steady at 0.5 s but
    # (a) an extra downbeat every half bar, (b) a half-time passage reporting every
    # other beat, (c) a spurious beat 0.1 s after a real one.
    from autodeck import analyze as A

    true = np.arange(0, 64, 0.5)
    beats = np.concatenate([true[:40], true[40:60:2], true[60:], [true[70] + 0.1]])
    beats.sort()
    downbeats = np.sort(np.concatenate([true[::4], true[2:40:8]]))  # half-bar extras early on
    b, d = A._regular_grid(beats, downbeats)
    assert np.allclose(b, true)
    assert np.allclose(np.diff(d), 2.0) and d[0] == 0.0


def test_spoken_intro_trimmed_even_when_grid_unreliable(tmp_path):
    # Music video shape: 10 s of noise ("dialogue"), steady song, then a tempo change
    # that fails the global grid check. The intro must still be skipped.
    import soundfile as sf
    from conftest import SR, make_click_signal

    noise = (0.3 * np.random.default_rng(3).standard_normal(10 * SR)).astype(np.float32)
    y = np.concatenate([noise, make_click_signal(120, 50), make_click_signal(96, 40)])
    p = tmp_path / "mv.flac"
    sf.write(p, y, SR)
    a = analyze(p, "t", "c")
    assert a.grid_reliable is False
    assert a.play_start == pytest.approx(10, abs=1)


def test_outro_capped_when_clustering_cuts_early(click_track, monkeypatch):
    # HUMBLE.: clustering put the last cut at 58 s of 182 s, and the planner exits at the
    # outro's first boundary, so two thirds of the song were skipped.
    import librosa
    from autodeck import analyze as A

    monkeypatch.setattr(librosa.segment, "agglomerative", lambda data, k: np.array([0, 50, 100]))
    a = analyze(click_track(120, 180), "t", "c")
    out = a.sections[-1]
    assert a.grid_reliable and out.label == "outro"
    assert out.end - out.start <= A.MAX_OUTRO_BARS * 2.0 + 1e-6  # 2 s bars at 120 bpm
    assert out.start in a.downbeats


@pytest.mark.parametrize("slip", ["missed_then_late", "extra_then_missed"])
def test_beat_slip_never_leaves_a_shifted_reliable_grid(tmp_path, monkeypatch, slip):
    # Reviewer repro: steady 0.5 s beats, true downbeats every 2 s, then one beat slip.
    # A single global phase vote put every bar after the slip on beat 2 while the bar
    # spacing still looked even (CV 0.031 -> reliable). Must be on beat 1 or unreliable.
    import soundfile as sf
    from autodeck import analyze as A
    from conftest import SR, make_click_signal

    p = tmp_path / "t.flac"
    sf.write(p, make_click_signal(120, 64), SR)
    true = np.arange(0, 64, 0.5)
    if slip == "missed_then_late":  # beat at 20.5 missed, next one 0.12 s late
        beats = np.concatenate([true[:41], [21.12], true[43:]])
    else:  # spurious beat at 20.3 replaces the real 20.5: one beat lost
        beats = np.concatenate([true[:41], [20.3], true[42:]])
    monkeypatch.setattr(A, "_beats", lambda y: (beats.copy(), true[::4].copy()))
    a = analyze(p, "t", "c")
    # All bar lines, both sides of the slip (a global vote fixes one side, shifts the other).
    d = np.array(a.downbeats)
    on_beat_one = np.allclose(d / 2.0, np.round(d / 2.0), atol=0.05)
    assert on_beat_one or not a.grid_reliable
    assert a.grid_reliable  # with the per-segment vote the grid is recovered, not just rejected


def test_junk_lead_in_downbeats_do_not_vote(tmp_path, monkeypatch):
    # beat_this extrapolates beats through a noise/dialogue intro and drops downbeats
    # there at an arbitrary phase. They must not outvote the song's own downbeats.
    import soundfile as sf
    from autodeck import analyze as A
    from conftest import SR, make_click_signal

    noise = (0.3 * np.random.default_rng(4).standard_normal(48 * SR)).astype(np.float32)
    p = tmp_path / "t.flac"
    sf.write(p, np.concatenate([noise, make_click_signal(120, 40)]), SR)
    beats = np.arange(0, 88, 0.5)
    junk = np.arange(0.5, 48, 2.0)           # 24 lead-in votes, all on beat 2
    song = np.arange(48, 88, 2.0)            # 20 true downbeats
    monkeypatch.setattr(A, "_beats", lambda y: (beats.copy(), np.concatenate([junk, song])))
    a = analyze(p, "t", "c")
    d = np.array(a.downbeats)
    assert a.grid_reliable
    assert a.play_start == pytest.approx(48, abs=0.6)
    assert np.allclose(d / 2.0, np.round(d / 2.0), atol=0.05)


def test_ambiguous_pause_is_a_slip(tmp_path, monkeypatch):
    # Reviewer probe: steady 0.5 s beats, a 2.6-beat pause, music restarts on beat 1.
    # Rounding the pause to 3 beats and never cutting there left 28 % of bars a beat off
    # while CV and agreement (0.72) still passed. The pause must re-phase the grid.
    import soundfile as sf
    from autodeck import analyze as A
    from conftest import SR, make_click_signal

    p = tmp_path / "t.flac"
    sf.write(p, np.concatenate([make_click_signal(120, 40.5), np.zeros(int(0.8 * SR), np.float32),
                                make_click_signal(120, 100)]), SR)
    pre, post = np.arange(0, 40.01, 0.5), 41.3 + np.arange(0, 100, 0.5)
    monkeypatch.setattr(A, "_beats", lambda y: (np.concatenate([pre, post]), np.concatenate([pre[::4], post[::4]])))
    a = analyze(p, "t", "c")
    true = np.concatenate([pre[::4], post[::4]])
    d = np.array(a.downbeats[:-1])  # last one may be the extrapolated play_end bar line
    on_beat_one = all(np.min(np.abs(true - x)) < 0.05 for x in d)
    assert on_beat_one or not a.grid_reliable
    assert a.grid_reliable


def test_bpm_from_raw_beats_not_filled_grid(click_track, monkeypatch):
    # Every 3rd gap of a 120 bpm song is 1.6 beats (irregular tracker output). Filling that
    # to 2 beats packs 0.4 s beats in; the filled median read ~133 bpm (Sarba De La Bucuresti
    # went 181.8 -> 214.3 that way). bpm must follow the tracker's own beat spacing.
    from autodeck import analyze as A

    gaps = np.tile([0.5, 0.5, 0.8], 48)  # 86 s, inside the 90 s audio
    beats = np.concatenate([[0.0], np.cumsum(gaps)])
    monkeypatch.setattr(A, "_beats", lambda y: (beats, beats[::4]))
    a = analyze(click_track(120, 90), "t", "c")
    assert a.bpm == pytest.approx(120, rel=0.02)


@pytest.mark.parametrize("true_bpm", [124, 128, 122])
def test_bpm_not_snapped_to_tracker_frames(click_track, monkeypatch, true_bpm):
    # beat_this reports beat times on 20 ms frames (frame / 50), so every raw gap is a whole
    # number of frames and their median snaps to 120 / 125 / 130.43 bpm. That 0.8-2 % error
    # becomes a 16-bar blend drifting 260-760 ms. A reliable grid's tempo is its slope.
    from autodeck import analyze as A

    true = 0.3 + np.arange(int(85 * true_bpm / 60)) * 60 / true_bpm
    beats = np.round(true * 50) / 50
    monkeypatch.setattr(A, "_beats", lambda y: (beats.copy(), beats[::4].copy()))
    a = analyze(click_track(true_bpm, 90), "t", "c")
    assert a.grid_reliable
    assert a.bpm == pytest.approx(true_bpm, rel=0.001)


def test_bpm_ignores_a_passage_of_extra_beats(click_track, monkeypatch):
    # Real songs have short passages where the tracker runs fast (Telephone's bridge: 32-beat
    # windows read 127-135 bpm in a 122 bpm song). A single least-squares line through the
    # whole grid leaned on them (123.37); the median 16-bar slope doesn't (122.06).
    from autodeck import analyze as A

    beat = 60 / 124
    pre = 0.3 + np.arange(140) * beat
    fast = pre[-1] + np.arange(1, 13) * beat * 0.8  # 12 beats (3 bars) in 9.6 beats' time
    post = fast[-1] + np.arange(1, 140) * beat
    beats = np.round(np.concatenate([pre, fast, post]) * 50) / 50
    monkeypatch.setattr(A, "_beats", lambda y: (beats.copy(), beats[::4].copy()))
    a = analyze(click_track(124, 150), "t", "c")
    assert a.grid_reliable
    assert a.bpm == pytest.approx(124, rel=0.001)
