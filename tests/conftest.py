import pytest


@pytest.fixture
def tmp_library(tmp_path, monkeypatch):
    """Point the library cache at a temp dir so tests never touch ~/autodeck-library."""
    monkeypatch.setenv("AUTODECK_LIBRARY", str(tmp_path))
    return tmp_path


# --- synthetic audio fixtures -------------------------------------------------
import numpy as np  # noqa: E402
import soundfile as sf  # noqa: E402

SR = 22050


def _hit(freq0, freq1, dur, amp, decay):
    """Pitch-dropping sine burst (kick-like when freq0 >> freq1)."""
    t = np.arange(int(dur * SR)) / SR
    f = freq1 + (freq0 - freq1) * np.exp(-t * 30)
    return amp * np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t * decay)


def make_click_signal(bpm, seconds, accent_every=4, lead_silence=0.0):
    """Music-like 4/4 groove: kick on beats 1 and 3 (bar start loudest), snare on
    2 and 4, hats on off-beats, a crash at each bar start, and a bass line that
    changes note every bar. Bare clicks gave beat_this no bar cue (it put a
    downbeat on every beat), so the fixture carries real bar-level cues."""
    rng = np.random.default_rng(0)
    n = int(seconds * SR)
    y = np.zeros(n)
    beat = 60.0 / bpm
    noise = lambda d, tau: rng.standard_normal(int(d * SR)) * np.exp(-np.arange(int(d * SR)) / (tau * SR))  # noqa: E731
    hat, snare, crash = 0.2 * noise(0.04, 0.008), 0.5 * noise(0.15, 0.04), 0.5 * noise(0.6, 0.15)
    bass_notes = [55.0, 55.0, 65.4, 73.4]
    total = int((seconds - lead_silence) / beat)
    for i in range(total):
        s = int((lead_silence + i * beat) * SR)
        pos = i % accent_every
        hits = [(s + int(beat / 2 * SR), hat)]
        if pos == 0:
            hits += [(s, _hit(160, 45, 0.2, 1.0, 15)), (s, crash)]
        elif pos % 2 == 0:
            hits.append((s, _hit(160, 45, 0.2, 0.6, 15)))
        else:
            hits.append((s, snare))
        for off, w in hits:
            seg = w[: max(0, n - off)]
            y[off : off + len(seg)] += seg
    # Bass: one note per bar, so the harmony itself marks bar starts.
    bar = beat * accent_every
    t = np.arange(n) / SR
    bar_idx = np.maximum(((t - lead_silence) // bar).astype(int), 0)
    y += 0.15 * np.sin(2 * np.pi * np.array(bass_notes)[(bar_idx // 1) % len(bass_notes)] * t)
    y[: int(lead_silence * SR)] = 0
    return np.clip(y, -1, 1).astype(np.float32)


@pytest.fixture
def click_track(tmp_path):
    def make(bpm=120, seconds=90, accent_every=4, lead_silence=0.0, name="click.flac"):
        p = tmp_path / name
        sf.write(p, make_click_signal(bpm, seconds, accent_every, lead_silence), SR)
        return p

    return make


@pytest.fixture
def silence_then_track(tmp_path):
    """Click track with 8 s of digital silence at the start and at the end."""

    def make(bpm=120, seconds=90, pad=8.0):
        y = make_click_signal(bpm, seconds)
        z = np.zeros(int(pad * SR), dtype=np.float32)
        p = tmp_path / "padded.flac"
        sf.write(p, np.concatenate([z, y, z]), SR)
        return p

    return make


@pytest.fixture
def noise_file(tmp_path):
    def make(seconds=60):
        p = tmp_path / "noise.flac"
        sf.write(p, (0.3 * np.random.default_rng(1).standard_normal(seconds * SR)).astype(np.float32), SR)
        return p

    return make


@pytest.fixture
def noise_then_track(tmp_path):
    """Beatless noise intro (stands in for a spoken video intro) then a click track."""

    def make(noise_s=10, bpm=120, seconds=80):
        rng = np.random.default_rng(2)
        noise = (0.3 * rng.standard_normal(noise_s * SR)).astype(np.float32)
        p = tmp_path / "noise_track.flac"
        sf.write(p, np.concatenate([noise, make_click_signal(bpm, seconds)]), SR)
        return p

    return make


@pytest.fixture
def a_minor_triad(tmp_path):
    p = tmp_path / "triad.flac"
    t = np.arange(20 * SR) / SR
    y = sum(np.sin(2 * np.pi * f * t) for f in (220.0, 261.63, 329.63)) / 3
    sf.write(p, (0.5 * y).astype(np.float32), SR)
    return p
