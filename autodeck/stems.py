"""Vocal / instrumental stems, made ahead of time like the analysis (never live in the browser).

htdemucs (demucs 4, MIT, ~84 MB checkpoint) splits a song into drums/bass/other/vocals; we keep
vocals and instrumental = mix - vocals, so vocals + instrumental is exactly the song again (the
"Both" position never sounds different from the plain render). Chosen over python-audio-separator
(MDX / Roformer): 13 s vs 96 s of model time per 4-min song on an M1 Max (MPS vs CoreML), torch is already
installed for beat_this, and five small pure packages instead of a broken extra-heavy tree.

Files next to the song's audio (library.audio_path): vocals.opus, instrumental.opus (96 kbps,
~2.9 MB each for 4 min) and stems.json (per-second vocal activity, for the
planner's Vocal swap). A separate optional artefact: not part of ANALYSIS_VERSION, so adding or
pruning stems never re-analyses anything, and a song without them behaves exactly as before.
"""
import hashlib
import json
import logging
import os
import subprocess
import tempfile
import threading
from pathlib import Path

import numpy as np
import soundfile as sf

from . import config, library
from .standalone import HTDEMUCS

log = logging.getLogger(__name__)

NAMES = ("vocals", "instrumental")
# The file the standalone setup installs (standalone.MODELS); dev fetches it on first use.
MODEL_FILE, MODEL_URL, MODEL_SHA256, _ = HTDEMUCS
MODEL_SR = 44100       # htdemucs was trained at 44.1 kHz
OPUS_SR = 48000        # the only rate (of the usual ones) Opus stores
OPUS_BITRATE = "96k"   # ~2.9 MB per 4-min stem; transparent enough for a stem that's mostly mixed back in
# Stems are kept for at most this many songs (~7 MB each); the oldest go first, never one that's
# wanted right now. Same idea as render pruning: re-made on demand, so dropping them is safe.
MAX_SONGS = 40
# Per-second vocal activity = vocal RMS / mix RMS. demucs leaks a little; above this counts as singing.
# ponytail: one hand-picked threshold; tune by ear if Auto picks Vocal swap over spoken intros.
SINGING = 0.35

_model = None
_lock = threading.Lock()  # one model load; separate() itself only runs on the worker thread


def path(song_id: str, name: str) -> Path:
    return library.song_dir(song_id) / f"{name}.opus"


def _meta(song_id: str) -> Path:
    return library.song_dir(song_id) / "stems.json"


def has(song_id: str) -> bool:
    """stems.json is written last, so its presence means both stems are complete."""
    return _meta(song_id).exists() and all(path(song_id, n).exists() for n in NAMES)


def activity(song_id: str) -> list[float] | None:
    """Vocal activity per original second (0..1), None without stems."""
    if not has(song_id):
        return None
    try:
        return json.loads(_meta(song_id).read_text(encoding="utf-8"))["vocal"]
    except (OSError, ValueError, KeyError):
        return None


def _model_file() -> Path:
    installed = config.installed_model(MODEL_FILE)
    if installed:
        return installed
    dest = config.library_root() / "models" / MODEL_FILE
    if not dest.exists():
        from .genre import _fetch  # the same atomic, timed-out download the style model uses

        _fetch(MODEL_URL, dest)
        # torch.load unpickles: only ever load the exact file we pinned.
        if hashlib.sha256(dest.read_bytes()).hexdigest() != MODEL_SHA256:
            dest.unlink()
            raise RuntimeError("stem model download corrupt")
    return dest


def _load():
    global _model
    with _lock:
        if _model is None:
            from demucs.states import load_model

            m = load_model(str(_model_file()))
            m.eval()
            _model = m
    return _model


def _device() -> str:
    import torch

    # MPS (Apple GPU): ~13 s of model time per 4-min song, and it leaves the CPU to analysis and Rubber Band.
    # CPU fallback (Intel Macs): ~80 s, still fine for a background job a few songs ahead.
    return "mps" if torch.backends.mps.is_available() else "cpu"


def _split(mix: np.ndarray) -> np.ndarray:
    """(n, 2) float32 at MODEL_SR -> vocals, same shape. The only part that needs the model."""
    import torch
    from demucs.apply import apply_model

    model = _load()
    x = torch.from_numpy(np.ascontiguousarray(mix.T))
    ref = x.mean(0)
    mean, std = ref.mean(), ref.std() + 1e-8  # demucs' own normalisation (separate.py)
    with torch.no_grad():
        out = apply_model(model, ((x - mean) / std)[None], device=_device(), shifts=0, split=True,
                          overlap=0.25, progress=False)[0]
    v = out[model.sources.index("vocals")] * std + mean
    return v.cpu().numpy().T.astype(np.float32)


def _resample(y: np.ndarray, sr: int, to: int) -> np.ndarray:
    if sr == to:
        return y
    import librosa

    return librosa.resample(y.T, orig_sr=sr, target_sr=to, res_type="soxr_hq").T.astype(np.float32)


def _rms_per_second(y: np.ndarray, sr: int) -> np.ndarray:
    n = len(y) // sr
    if n == 0:
        return np.zeros(0)
    return np.sqrt(np.mean(y[: n * sr].reshape(n, sr, -1) ** 2, axis=(1, 2)))


def _write(dest: Path, y: np.ndarray, sr: int) -> None:
    """Opus via ffmpeg (the imageio-ffmpeg binary yt-dlp already uses): 2 s for a 4-min stem, where
    libsndfile's own Opus encoder took 8.5 s. ffmpeg keeps the exact sample count (pre-skip set)."""
    import imageio_ffmpeg

    fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=dest.name + ".", suffix=".opus")
    os.close(fd)
    try:
        subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-v", "error", "-y", "-f", "f32le", "-ar", str(sr),
                        "-ac", str(y.shape[1]), "-i", "-", "-c:a", "libopus", "-b:a", OPUS_BITRATE, tmp],
                       input=np.ascontiguousarray(y, dtype=np.float32).tobytes(), check=True, capture_output=True)
        os.replace(tmp, dest)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def separate(song_id: str) -> None:
    """Make vocals.opus, instrumental.opus and stems.json for a downloaded song. Raises on failure
    (the worker logs it and moves on; nothing else depends on stems existing)."""
    y, sr = sf.read(library.audio_path(song_id), dtype="float32", always_2d=True)
    if y.shape[1] == 1:
        y = np.repeat(y, 2, axis=1)
    y = y[:, :2]
    # Stems are stored at Opus' 48 kHz; audio.opus already is, so this only resamples legacy FLACs.
    y = _resample(y, sr, OPUS_SR)
    v = _resample(_split(_resample(y, OPUS_SR, MODEL_SR)), MODEL_SR, OPUS_SR)
    # Resampling can be off by a sample or two: the stems must be exactly as long as the song,
    # or they drift against the main render's sample positions.
    v = np.pad(v, ((0, max(0, len(y) - len(v))), (0, 0)))[: len(y)]
    inst = y - v  # vocals + instrumental == the song, bit for bit before encoding
    act = np.clip(_rms_per_second(v, OPUS_SR) / (_rms_per_second(y, OPUS_SR) + 1e-6), 0.0, 1.0)
    for name, data in (("vocals", v), ("instrumental", inst)):
        _write(path(song_id, name), data, OPUS_SR)
    config.write_json_atomic(_meta(song_id), {"vocal": [round(float(a), 3) for a in act]})


def prune(keep: set[str], max_songs: int = MAX_SONGS) -> int:
    """Delete the stems of the songs separated longest ago beyond max_songs, never one in `keep`."""
    metas = sorted(config.library_root().glob("*/stems.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    gone = 0
    for m in [p for p in metas if p.parent.name not in keep][max(0, max_songs - len(keep)):]:
        for name in NAMES:
            path(m.parent.name, name).unlink(missing_ok=True)
        m.unlink(missing_ok=True)
        gone += 1
    return gone
