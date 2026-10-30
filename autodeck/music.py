"""Pure music helpers: Camelot keys, tempo relation, phrase grid. No I/O."""
from autodeck.config import BEATMATCH_MAX_PCT, PHRASE_BARS

# Pitch class (semitones above C) -> Camelot number, per mode. Keyed by pitch
# class so sharp and flat spellings (F# / Gb) share one entry.
_MINOR = {8: 1, 3: 2, 10: 3, 5: 4, 0: 5, 7: 6, 2: 7, 9: 8, 4: 9, 11: 10, 6: 11, 1: 12}
_MAJOR = {11: 1, 6: 2, 1: 3, 8: 4, 3: 5, 10: 6, 5: 7, 0: 8, 7: 9, 2: 10, 9: 11, 4: 12}
_NOTE = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}


def to_camelot(key: str) -> str:
    """"A minor" -> "8A", "C major" -> "8B". Accepts # and b spellings."""
    try:
        tonic, scale = key.split()
        pc = _NOTE[tonic[0].upper()] + sum({"#": 1, "b": -1}[c] for c in tonic[1:])
        table, letter = {"minor": (_MINOR, "A"), "major": (_MAJOR, "B")}[scale.lower()]
        return f"{table[pc % 12]}{letter}"
    except (ValueError, KeyError, AttributeError, IndexError):
        raise ValueError(f"unknown key: {key!r}") from None


def camelot_score(a: str | None, b: str | None) -> float:
    """1.0 same code / relative major-minor / adjacent number; 0.5 for +-2; else 0."""
    if a is None or b is None:
        return 0.5  # unknown key: neutral, neither a bonus nor a veto
    # Distance on the 12-step wheel (wraps: 12 -> 1). Letter is ignored: the
    # same-number other-letter pair is the relative key, distance 0.
    d = abs(int(a[:-1]) - int(b[:-1]))
    d = min(d, 12 - d)
    if d <= 1 and (d == 0 or a[-1] == b[-1]):
        return 1.0
    if d == 2 and a[-1] == b[-1]:
        return 0.5
    return 0.0


def tempo_relation(out_bpm: float, in_bpm: float) -> tuple[str, float]:
    """How the incoming song can be sped up/down to match the outgoing one.

    Returns (kind, ratio); ratio = out_bpm / effective in_bpm, i.e. the speed
    factor applied to the incoming song. Half/double time covers beat trackers
    that report 85 as 170. "match" wins over "half_double" when both qualify.
    """
    # Epsilon: 100*1.08 / 100 - 1 is 0.08000000000000007 in floats, and the
    # limit is meant to be inclusive.
    ok = lambda r: abs(r - 1) <= BEATMATCH_MAX_PCT + 1e-9  # noqa: E731
    ratio = out_bpm / in_bpm
    if ok(ratio):
        return "match", ratio
    best = min((out_bpm / (in_bpm * 2), out_bpm / (in_bpm / 2)), key=lambda r: abs(r - 1))
    if ok(best):
        return "half_double", best
    return "none", 1.0


def phrase_boundaries(downbeats: list[float], bars: int = PHRASE_BARS) -> list[float]:
    """Every `bars`-th downbeat counted from the first one (one downbeat = one bar)."""
    return downbeats[::bars]


def next_boundary(downbeats: list[float], t: float, bars: int = PHRASE_BARS) -> float | None:
    """First phrase boundary at or after t, or None if the song has none left."""
    return next((b for b in phrase_boundaries(downbeats, bars) if b >= t), None)
