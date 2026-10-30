import pytest

from autodeck.config import BEATMATCH_MAX_PCT
from autodeck.music import camelot_score, next_boundary, phrase_boundaries, tempo_relation, to_camelot

MINOR = {"A": 8, "E": 9, "B": 10, "F#": 11, "C#": 12, "G#": 1, "D#": 2, "A#": 3, "F": 4, "C": 5, "G": 6, "D": 7}
MAJOR = {"C": 8, "G": 9, "D": 10, "A": 11, "E": 12, "B": 1, "F#": 2, "C#": 3, "G#": 4, "D#": 5, "A#": 6, "F": 7}


def test_to_camelot_all_24_sharp_spellings():
    for t, n in MINOR.items():
        assert to_camelot(f"{t} minor") == f"{n}A"
    for t, n in MAJOR.items():
        assert to_camelot(f"{t} major") == f"{n}B"


def test_to_camelot_flat_spellings_and_errors():
    assert to_camelot("Gb minor") == "11A"
    assert to_camelot("Bb major") == "6B"
    assert to_camelot("Ab minor") == "1A"
    assert to_camelot("Db major") == "3B"
    for bad in ("H minor", "A dorian", "A", ""):
        with pytest.raises(ValueError):
            to_camelot(bad)


def test_camelot_score():
    assert camelot_score("8A", "8A") == 1.0
    assert camelot_score("8A", "9A") == 1.0
    assert camelot_score("12A", "1A") == 1.0
    assert camelot_score("1B", "12B") == 1.0
    assert camelot_score("8A", "8B") == 1.0
    assert camelot_score("8A", "10A") == 0.5
    assert camelot_score("11A", "1A") == 0.5
    assert camelot_score("8A", "3B") == 0
    assert camelot_score(None, "8A") == 0.5
    assert camelot_score("8A", None) == 0.5


def test_tempo_relation():
    assert tempo_relation(124, 120) == ("match", pytest.approx(124 / 120))
    kind, ratio = tempo_relation(170, 86)
    assert kind == "half_double" and ratio == pytest.approx(170 / 172)
    assert tempo_relation(150, 90) == ("none", 1.0)


def test_tempo_relation_limit_is_inclusive():
    assert tempo_relation(100 * (1 + BEATMATCH_MAX_PCT), 100)[0] == "match"
    assert tempo_relation(100 * (1 - BEATMATCH_MAX_PCT), 100)[0] == "match"
    assert tempo_relation(108.1, 100)[0] == "none"
    # same limit applies to half/double time
    assert tempo_relation(108, 50)[0] == "half_double"
    assert tempo_relation(108.1, 50)[0] == "none"


def test_phrase_grid():
    downbeats = [i * 2.0 for i in range(33)]
    assert phrase_boundaries(downbeats) == [0, 16, 32, 48, 64]
    assert phrase_boundaries(downbeats, bars=16) == [0, 32, 64]
    assert next_boundary(downbeats, 17.0) == 32
    assert next_boundary(downbeats, 16.0) == 16
    assert next_boundary(downbeats, 64.5) is None
    assert phrase_boundaries([]) == []
