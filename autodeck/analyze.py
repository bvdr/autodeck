"""Audio analysis: beats/downbeats/BPM, key, energy, sections, trim, grid reliability, peaks."""
from pathlib import Path

import librosa
import numpy as np
import soundfile as sf

from . import config
from .library import ANALYSIS_VERSION, Analysis, Section
from .music import phrase_boundaries, to_camelot

# One sample rate for everything. beat_this resamples to 22050 internally anyway,
# so decoding once at 22050 means exactly one resample for the whole pipeline
# (essentia's key detector is fine below 11 kHz, where the harmony lives).
SR = 22050

STEADY_BARS = 16        # a "steady" run must span this many bars (Ruling D)
STEADY_TOL = 0.06       # per-bar tolerance and grid CV limit, relative to the median bar
PULSE_CONTRAST = 1.5    # beat onset strength vs bar average, for a bar to count as rhythmic
TRIM_THRESHOLD = 0.1    # energy fraction of max that counts as "music is playing"
SNAP_SLACK = 0.25       # s: downbeat detected slightly before the energy edge still counts as the edge
MIN_BEATS = 8           # fewer beats than this -> don't trust the median interval
FALLBACK_BPM = 120.0    # last resort: the planner divides by bpm, so it must never be 0/NaN

_beat_model = None


def _beats(y: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    global _beat_model
    if _beat_model is None:  # model load is slow; do it once per process
        from beat_this.inference import Audio2Beats

        # dbn=False: the DBN post-processor needs madmom; plain peak picking is enough.
        _beat_model = Audio2Beats(dbn=False)
    b, d = _beat_model(y, SR)
    return np.asarray(b, dtype=float), np.asarray(d, dtype=float)


def _bar_has_pulse(y: np.ndarray, downbeats: np.ndarray, beats: np.ndarray) -> np.ndarray:
    """Per bar: do onsets actually peak on the beats? beat_this happily extrapolates a
    perfect grid through noise or speech (seen in testing), so evenly spaced
    downbeats alone prove nothing. Real pulse = onset strength at the beats is
    clearly above the bar's average onset strength.
    ponytail: single contrast threshold; fails for very soft/legato music."""
    hop = 512
    env = librosa.onset.onset_strength(y=y, sr=SR, hop_length=hop)
    at = np.minimum((beats * SR / hop).round().astype(int), len(env) - 1)
    # +-2 frames: the tracker's beat time can sit a frame or two off the onset peak.
    peak = np.array([env[max(i - 2, 0) : i + 3].max() for i in at])
    out = np.zeros(max(len(downbeats) - 1, 0), dtype=bool)
    for k in range(len(out)):
        m = (beats >= downbeats[k] - 1e-6) & (beats < downbeats[k + 1] - 1e-6)
        f0, f1 = int(downbeats[k] * SR / hop), max(int(downbeats[k + 1] * SR / hop), int(downbeats[k] * SR / hop) + 1)
        base = env[f0:f1].mean() + 1e-9
        out[k] = m.any() and peak[m].mean() / base >= PULSE_CONTRAST
    return out


def _steady_run_start(downbeats: np.ndarray, pulse: np.ndarray) -> int | None:
    """Index of the first downbeat starting STEADY_BARS consecutive evenly spaced bars with real pulse."""
    if len(downbeats) <= STEADY_BARS:
        return None
    gaps = np.diff(downbeats)
    med = np.median(gaps)
    ok = (np.abs(gaps - med) <= STEADY_TOL * med) & pulse
    # Tolerate one weak bar per 16 (fill, dropout, breakdown): a hard "16 in a row"
    # would mistake a real song for a beatless one. The run starts at the first
    # passing bar of the first window with >= 15 passing.
    hits = np.convolve(ok, np.ones(STEADY_BARS, dtype=int), mode="valid")
    for w in np.flatnonzero(hits >= STEADY_BARS - 1):
        return int(w + np.flatnonzero(ok[w : w + STEADY_BARS])[0])
    return None


def _tempo_fallback(y: np.ndarray) -> float:
    try:
        t = float(np.atleast_1d(librosa.feature.rhythm.tempo(y=y, sr=SR))[0])
        return t if np.isfinite(t) and t > 0 else FALLBACK_BPM
    except Exception:
        return FALLBACK_BPM


def _key(y: np.ndarray) -> tuple[str | None, str | None]:
    try:
        import essentia.standard as es

        k, scale, _ = es.KeyExtractor(profileType="edma", sampleRate=SR)(y.astype(np.float32))
        key = f"{k} {scale}"
        return key, to_camelot(key)
    except Exception:
        return None, None


def _sections(y, start, end, downbeats, reliable) -> list[Section]:
    # Boundaries only land on phrase boundaries (8 bars) when we have a grid, so a
    # transition never starts mid-phrase. Without a grid we keep raw timestamps.
    hop = 2048
    feat = np.vstack([
        librosa.util.normalize(librosa.feature.chroma_stft(y=y, sr=SR, hop_length=hop), axis=0),
        librosa.util.normalize(librosa.feature.mfcc(y=y, sr=SR, hop_length=hop, n_mfcc=13), axis=0),
    ])
    f0, f1 = int(start * SR / hop), int(end * SR / hop)
    seg = feat[:, f0:f1]
    cuts: list[float] = []
    k = int(np.clip(round((end - start) / 40), 2, 8))  # ~one section per 40 s
    if seg.shape[1] > k:
        idx = librosa.segment.agglomerative(seg, k)
        cuts = [start + i * hop / SR for i in idx if i > 0]
    if reliable:
        grid = [b for b in phrase_boundaries(list(downbeats)) if start < b < end]
        cuts = [min(grid, key=lambda g: abs(g - c)) for c in cuts] if grid else []
    cuts = sorted({c for c in cuts if start < c < end})
    if not cuts:
        # Contract: always intro + outro. Split at the bar line nearest the middle
        # when we have a grid (so the cut is still a downbeat), else the raw middle.
        mid = (start + end) / 2
        inner = [d for d in downbeats if start < d < end] if reliable else []
        cuts = [min(inner, key=lambda d: abs(d - mid)) if inner else mid]
    edges = [start, *cuts, end]
    # ponytail: label by position only (first=intro, last=outro); real structure
    # labelling (verse/chorus) isn't needed by the planner. Upgrade if it ever is.
    labels = ["body"] * (len(edges) - 1)
    labels[0], labels[-1] = "intro", "outro"
    return [Section(a, b, l) for a, b, l in zip(edges, edges[1:], labels)]


def analyze(audio_path: Path, title: str, channel: str) -> Analysis:
    # title/channel are accepted for the Task 5 genre wiring; genre_guess and
    # language stay None here.
    y, _ = librosa.load(audio_path, sr=SR, mono=True)
    dur = len(y) / SR

    beats, downbeats = _beats(y)
    run = _steady_run_start(downbeats, _bar_has_pulse(y, downbeats, beats))
    reliable = False
    if run is not None:
        gaps = np.diff(downbeats[run:])
        # ponytail: one global CV threshold; per-section reliability once live-drum tracks need it.
        reliable = bool(np.std(gaps) / np.mean(gaps) < STEADY_TOL)
    if reliable:
        # Drop the beatless/irregular lead-in: the phrase grid counts from the
        # first downbeat, so junk downbeats would shift every phrase boundary.
        downbeats = downbeats[run:]
        beats = beats[beats >= downbeats[0] - 1e-6]

    if len(beats) >= MIN_BEATS:
        bpm = 60.0 / float(np.median(np.diff(beats)))
    else:
        bpm = _tempo_fallback(y)
        reliable = False
    if not np.isfinite(bpm) or bpm <= 0:
        bpm = FALLBACK_BPM

    # Energy: RMS of each whole second, normalized so the loudest second is 1.0.
    n = int(dur)
    rms = librosa.feature.rms(y=y[: n * SR], frame_length=SR, hop_length=SR, center=False)[0] if n else np.zeros(0)
    peak = rms.max() if len(rms) else 0.0
    energy = (rms / peak).tolist() if peak > 0 else [0.0] * len(rms)

    loud = np.flatnonzero(np.array(energy) > TRIM_THRESHOLD)
    t_start, t_end = (float(loud[0]), float(loud[-1] + 1)) if len(loud) else (0.0, dur)
    if reliable:
        # Snap inward to bar lines. The last bar's end has no click of its own, so
        # one extrapolated bar after the final downbeat is a legal end.
        bar = float(np.median(np.diff(downbeats)))
        starts = [d for d in downbeats if d >= t_start - SNAP_SLACK]
        ends = [d for d in [*downbeats, downbeats[-1] + bar] if d <= t_end + SNAP_SLACK]
        if starts and ends and ends[-1] > starts[0]:
            play_start, play_end = float(starts[0]), float(ends[-1])  # starts[0] >= downbeats[0]: Ruling D
            if play_end > downbeats[-1] + 1e-6:
                # Store the extrapolated bar line so every section boundary is a stored downbeat.
                downbeats = np.append(downbeats, play_end)
        else:
            # Can't snap (energy edges outside the grid): no usable grid for the planner.
            play_start, play_end, reliable = t_start, t_end, False
    else:
        play_start, play_end = t_start, t_end

    key, camelot = _key(y)
    sections = _sections(y, play_start, play_end, downbeats, reliable)
    return Analysis(
        version=ANALYSIS_VERSION, bpm=float(bpm), beats=beats.tolist(), downbeats=downbeats.tolist(),
        grid_reliable=reliable, key=key, camelot=camelot, energy=energy, sections=sections,
        play_start=float(play_start), play_end=float(play_end), genre_guess=None, language=None,
    )


def write_peaks(audio_path: Path, out: Path, points: int = 2000) -> None:
    """Waveform overview for the UI: max |sample| per bucket, `points` buckets."""
    y, _ = sf.read(audio_path, always_2d=True)
    a = np.abs(y).max(axis=1)
    edges = np.linspace(0, len(a), points + 1).astype(int)
    edges[-1] = len(a)
    # reduceat needs strictly increasing starts; very short clips get repeated bins.
    edges = np.minimum(edges[:-1], max(len(a) - 1, 0))
    peaks = np.maximum.reduceat(a, edges) if len(a) else np.zeros(points)
    config.write_json_atomic(out, [float(p) for p in peaks])
