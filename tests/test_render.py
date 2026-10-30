import os

import numpy as np
import pytest
import soundfile as sf

from autodeck.config import EASE_BARS
from autodeck.library import Analysis, Song, song_dir
from autodeck.planner import Transition
from autodeck.render import TimeMap, render, time_map_for


def make_song(bpm=120.0, ps=2.0, pe=62.0, downbeats=None):
    bar = 240 / bpm
    db = downbeats or [ps + i * bar for i in range(int((pe - ps) / bar))]
    an = Analysis(1, bpm, db, db, True, None, None, [0.5] * 60, [], ps, pe, None, None)
    return Song("sid", "t", "c", pe + 2, "ready", analysis=an)


def tr(song, ratio=1.05, length=8.0, in_at=None, out_at=40.0):
    return Transition("blend", "x", song.id, out_at, in_at if in_at is not None else song.analysis.play_start,
                      length, ratio)


def test_identity_map():
    s = make_song()
    tm = time_map_for(s, None)
    assert tm.to_rendered(2.0) == 0 and tm.to_rendered(32.0) == pytest.approx(30.0)
    assert tm.speed_at(10) == 1.0


def test_speed_profile():
    s = make_song()
    t = tr(s)
    tm = time_map_for(s, t)
    ov_end = 2.0 + 8.0 * 1.05
    bar = 2.0
    assert tm.speed_at(2.0) == 1.05 and tm.speed_at(ov_end - 0.01) == 1.05
    # linear-ish ramp: strictly between, decreasing
    sp = [tm.speed_at(ov_end + (i + 0.5) * bar) for i in range(EASE_BARS)]
    assert all(1.0 < a <= 1.05 for a in sp) and sp == sorted(sp, reverse=True)
    assert tm.speed_at(ov_end + EASE_BARS * bar + 0.1) == 1.0
    # whole overlap is `length` seconds of rendered time
    assert tm.to_rendered(ov_end) == pytest.approx(8.0)


def test_ease_compressed_by_outgoing():
    s = make_song()
    t = tr(s)
    ov_end = 2.0 + 8.4
    tm = time_map_for(s, t, tr(s, out_at=ov_end + 10.0))  # 10 s room < 32 s ease
    assert tm.speed_at(ov_end + 10.01) == 1.0
    assert 1.0 < tm.speed_at(ov_end + 9.0) < 1.05
    # overlap reaches the exit: jump straight to 1.0
    tm2 = time_map_for(s, t, tr(s, out_at=ov_end))
    assert tm2.speed_at(ov_end + 0.1) == 1.0 and tm2.speed_at(ov_end - 0.1) == 1.05


def test_downbeats_strictly_increasing():
    s = make_song()
    tm = time_map_for(s, tr(s))
    r = [tm.to_rendered(d) for d in s.analysis.downbeats]
    assert all(b > a for a, b in zip(r, r[1:])) and r[0] == pytest.approx(0)
    assert tm.to_rendered(0.0) < 0 < tm.to_rendered(100.0)  # extrapolates, stays monotonic


def _put_audio(tmp_library, song, sr=22050, secs=70, freq=100.0, clicks=True):
    t = np.arange(int(secs * sr)) / sr
    y = 0.3 * np.sin(2 * np.pi * freq * t)
    if clicks:  # 2 clicks/s (120 BPM)
        for k in range(int(secs * 2)):
            y[int(k * 0.5 * sr): int(k * 0.5 * sr) + 200] += 0.6
    d = song_dir(song.id)
    d.mkdir(parents=True, exist_ok=True)
    sf.write(d / "audio.flac", np.stack([y, y], 1).astype(np.float32), sr)


def test_render_identity_length_and_cache(tmp_library):
    s = make_song()
    _put_audio(tmp_library, s)
    p = render(s, time_map_for(s, None))
    info = sf.info(p)
    assert abs(info.duration - 60.0) < 0.01 and info.channels == 2
    m = os.stat(p).st_mtime_ns
    assert render(s, time_map_for(s, None)) == p and os.stat(p).st_mtime_ns == m


@pytest.mark.slow
def test_render_stretch_keeps_pitch(tmp_library):
    s = make_song(ps=0.0, pe=60.0)
    _put_audio(tmp_library, s)
    t = tr(s, ratio=1.05, length=10.0, in_at=0.0, out_at=60.0)
    p = render(s, time_map_for(s, t))
    y, sr = sf.read(p)
    y = y[:, 0]
    # onsets in the first 10 s: spacing 0.5 s / 1.05
    env = np.abs(np.diff(y, prepend=0))
    on = np.flatnonzero((env > 0.3) & (np.arange(len(y)) % 1 == 0))
    on = on[np.insert(np.diff(on) > sr * 0.1, 0, True)] / sr
    on = on[on < 9]
    assert np.median(np.diff(on)) == pytest.approx(0.5 / 1.05, rel=0.02)
    seg = y[sr * 12: sr * 20]  # steady-ish part; the tone must still be ~100 Hz
    f = np.fft.rfftfreq(len(seg), 1 / sr)
    assert f[np.argmax(np.abs(np.fft.rfft(seg * np.hanning(len(seg)))))] == pytest.approx(100.0, rel=0.01)


def test_time_map_for_warns_on_exit_inside_overlap(caplog):
    from autodeck.planner import Transition
    from autodeck.render import time_map_for
    from tests.test_planner import make_song
    s = make_song(122, duration=70)
    inc = Transition("blend", "x", s.id, 0, s.analysis.play_start, 20.0, 0.95)
    out = Transition("cut", s.id, "y", s.analysis.play_start + 5, 1.0, 0.0, 1.0)
    time_map_for(s, inc, out)  # must not raise
    assert "before overlap end" in caplog.text


def test_time_map_for_degrades_when_overlap_passes_play_end():
    from autodeck.planner import Transition
    from autodeck.render import time_map_for
    from tests.test_planner import make_song
    s = make_song(122, duration=70)
    inc = Transition("blend", "x", s.id, 0, s.analysis.play_start, 80.0, 0.95)
    out = Transition("cut", s.id, "y", s.analysis.play_end, 1.0, 0.0, 1.0)
    time_map_for(s, inc, out)  # must not raise
