"""Genre guess = owner-visible title/channel keywords > Discogs style model + lyric language.

Never raises into analyze(): a missing model or no network just means "no guess".
"""
import logging
import os
import re
import shutil
import unicodedata
import urllib.request
from pathlib import Path

import librosa
import numpy as np

from . import config

log = logging.getLogger(__name__)

SR = 16000                # both the Discogs-EffNet card and Whisper want 16 kHz mono
MODEL_URL = "https://essentia.upf.edu/models/feature-extractors/discogs-effnet/discogs-effnet-bsdynamic-1"
SNIPPET_S = 30            # seconds from the middle of the song, for both models (speed: a 4 min song stays ~seconds)
PATCH, PATCH_HOP = 128, 62  # model card: input (n, 128 frames, 96 mel bands); 62 = essentia's default patch hop
TOP_N = 3


def _norm(s: str) -> str:
    """Lowercase + strip diacritics so 'Sârba' == 'sarba'."""
    return "".join(c for c in unicodedata.normalize("NFKD", s.lower()) if not unicodedata.combining(c))


# Keyword -> genre. Word boundaries matter: bare "sarb" would match "sarbatori" (holidays).
HINTS = [
    # s[ai]rb: "sîrba" (old spelling) folds to "sirba", not "sarba". lautar*: lăutari = Romanian folk band.
    (re.compile(r"\b(s[ai]rb(a|e|ele|ei)?|hora|manele|manea|lautar\w*)\b"), "Romanian sârbe"),
]


def metadata_hint(title: str, channel: str, genres: list[str]) -> str | None:
    text = _norm(f"{title} {channel}")
    for rx, genre in HINTS:
        if rx.search(text) and genre in genres:
            return genre
    return None


# ponytail: hand-written rule table with a known ceiling (4 default genres, "ro" vs not).
# Refine once owner corrections pile up (they are stored per song and could train this later).
# Each rule: (style-prefix in Discogs "Genre---Style" form, genre if lyrics are Romanian, genre otherwise).
# Lyrics decide the Romanian variant because the style model can't tell a Romanian trap track from any trap track.
RULES = [
    ("Hip Hop---",                    "Romanian hip-hop", "Hip-hop"),
    ("Pop---",                        "Pop",              "Pop"),
    ("Electronic---Dance-pop",        "Pop",              "Pop"),
    ("Folk, World, & Country---",     "Romanian sârbe",   None),  # folk only maps to sârbe when sung in Romanian
]


def combine(style_top: list[str], language: str | None, hint: str | None, genres: list[str] | None = None) -> str | None:
    """Owner-visible hint wins; else first (highest-ranked) style that has a rule. Only returns genres in `genres`."""
    genres = config.DEFAULT_GENRES if genres is None else genres
    if hint is not None:
        return hint if hint in genres else None
    for style in style_top:
        for prefix, ro, other in RULES:
            if style.startswith(prefix):
                g = ro if language == "ro" else other
                if g in genres:
                    return g
                # A matching rule that yields nothing usable stops here; a lower-ranked style shouldn't override the top pick.
                return None
    return None


# --- models -------------------------------------------------------------------
_session = None
_classes: list[str] = []
_whisper = None
# Set when a model can't be fetched/loaded. Offline first run would otherwise retry the
# download (up to the timeout) for every single song; one failure per process is enough.
_unavailable: set[str] = set()
MIN_LANG_PROB = 0.5  # tiny whisper picks an arbitrary language on instrumentals; below this, say "unknown"


def _fetch(url: str, dest: Path) -> None:
    if dest.exists():
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")  # atomic: a killed download never leaves a truncated model
    # urlopen with a timeout (urlretrieve has none): a stalled connection must not hang analyze() forever.
    with urllib.request.urlopen(url, timeout=30) as r, open(tmp, "wb") as f:
        shutil.copyfileobj(r, f)
    os.replace(tmp, dest)


def _load_model():
    global _session, _classes
    if _session is None:
        import json

        import onnxruntime as ort

        d = config.library_root() / "models"
        # The card's separate genre_discogs400 head is only published as .pb (no ONNX), and the
        # bsdynamic EffNet ONNX already outputs the same 400 sigmoid style activations (+ 1280-d
        # embeddings) with the same class list, so one model does the whole job.
        _fetch(MODEL_URL + ".onnx", d / "discogs-effnet-bsdynamic-1.onnx")
        _fetch(MODEL_URL + ".json", d / "discogs-effnet-bsdynamic-1.json")
        _classes = json.loads((d / "discogs-effnet-bsdynamic-1.json").read_text())["classes"]
        _session = ort.InferenceSession(str(d / "discogs-effnet-bsdynamic-1.onnx"), providers=["CPUExecutionProvider"])
    return _session


def _middle(y: np.ndarray, seconds: int) -> np.ndarray:
    n = seconds * SR
    s = max((len(y) - n) // 2, 0)
    return y[s : s + n]


def _styles(y: np.ndarray) -> list[str]:
    import essentia.standard as es

    if "styles" in _unavailable:
        raise RuntimeError("style model unavailable (earlier failure)")
    try:
        sess = _load_model()
    except Exception:
        _unavailable.add("styles")
        raise
    y = _middle(y, SNIPPET_S).astype(np.float32)
    # Plain essentia (no TF) ships the exact mel front-end these models were trained with.
    frame = es.FrameGenerator
    mel = es.TensorflowInputMusiCNN()
    frames = np.array([mel(f) for f in frame(y, frameSize=512, hopSize=256, startFromZero=True, validFrameThresholdRatio=1)], dtype=np.float32)
    if len(frames) < PATCH:  # very short audio: pad with the floor value rather than fail
        frames = np.vstack([frames, np.zeros((PATCH - len(frames), frames.shape[1]), np.float32)]) if len(frames) else np.zeros((PATCH, 96), np.float32)
    patches = np.stack([frames[i : i + PATCH] for i in range(0, len(frames) - PATCH + 1, PATCH_HOP)])
    # Tensor names read from the loaded session, not hard-coded (the card's names differ from the ONNX file's).
    out = sess.run([sess.get_outputs()[0].name], {sess.get_inputs()[0].name: patches})[0]
    top = np.argsort(out.mean(axis=0))[::-1][:TOP_N]
    return [_classes[i] for i in top]


def _language(y: np.ndarray) -> str | None:
    global _whisper
    if _whisper is None:  # load once per process; first call downloads ~75 MB from the HF hub
        if "whisper" in _unavailable:
            raise RuntimeError("whisper unavailable (earlier failure)")
        try:
            from faster_whisper import WhisperModel

            _whisper = WhisperModel("tiny", device="cpu", compute_type="int8")
        except Exception:
            _unavailable.add("whisper")
            raise
    lang, prob, _ = _whisper.detect_language(audio=_middle(y, SNIPPET_S).astype(np.float32))
    return lang if prob >= MIN_LANG_PROB else None


def guess_genre(audio_path: Path, title: str, channel: str, genres: list[str]) -> tuple[str | None, str | None]:
    """Returns (genre, language). Each model failing degrades independently; never raises."""
    hint = metadata_hint(title, channel, genres)
    try:
        y, _ = librosa.load(audio_path, sr=SR, mono=True)
    except Exception:
        log.warning("genre: cannot decode %s", audio_path, exc_info=True)
        return hint, None
    language = styles = None
    try:
        language = _language(y)
    except Exception:
        log.warning("genre: language ID unavailable", exc_info=True)
    if hint is None:  # a hint wins anyway, so skip the (slower) style model
        try:
            styles = _styles(y)
        except Exception:
            log.warning("genre: style model unavailable", exc_info=True)
    return combine(styles or [], language, hint, genres), language
