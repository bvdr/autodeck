import pytest

from autodeck.library import Analysis, Section, Song
from autodeck.music import next_boundary, phrase_boundaries
from autodeck.planner import genre_entry, order, pick_transition, score_pair


def make_song(bpm=122, camelot="8A", energy=0.5, duration=240.0, grid=True, genre="Pop", id=None,
              body_at=None, energy_list=None):
    bar = 60 / bpm * 4
    n = int(duration // bar)
    start = 1.0
    downbeats = [start + i * bar for i in range(n)]
    end = downbeats[-1]
    intro_end = downbeats[min(8, n - 2)]
    outro_start = downbeats[max((n - 1) // 8 * 8 - 8, 1)]  # on an 8-bar boundary, like real phrase structure
    if body_at is not None:
        intro_end = body_at
    secs = [Section(start, intro_end, "intro"), Section(intro_end, outro_start, "body"),
            Section(outro_start, end, "outro")]
    if grid:
        beats = downbeats
    else:
        downbeats, beats = [], []
    a = Analysis(1, bpm, beats, downbeats, grid, None, camelot, energy_list or [energy] * int(duration),
                 secs, start, end, genre, None)
    return Song(id or f"{genre}-{bpm}-{camelot}", "t", "c", duration, "ready", analysis=a)


def test_order_prefers_close_song_and_filters_genre():
    cur = make_song(122, "8A")
    near, far = make_song(124, "9A", id="near"), make_song(150, "3B", id="far")
    other = make_song(122, "8A", genre="Hip-hop", id="other")
    assert [s.id for s in order(cur, [far, other, near], {}, "Pop")] == ["near", "far"]
    assert {s.id for s in order(cur, [far, other, near], {}, "Any")} == {"near", "far", "other"}


def test_order_keeps_pins_even_off_genre():
    cur = make_song(122, "8A")
    a, b, c = (make_song(124, "9A", id="a"), make_song(123, "8A", id="b"), make_song(150, "3B", id="c"))
    other = make_song(100, "1B", genre="Hip-hop", id="other")
    res = order(cur, [a, b, c, other], {1: "other"}, "Pop")
    assert [s.id for s in res][1] == "other" and len(res) == 4
    assert order(cur, [a, b, c], {0: "c"}, "Pop")[0].id == "c"


def test_blend_122_to_124():
    a, b = make_song(122), make_song(124)
    t = pick_transition(a, b)
    bar = 60 / 122 * 4
    assert t.kind == "blend"
    assert t.out_at in phrase_boundaries(a.analysis.downbeats)
    assert t.out_at >= a.analysis.sections[-1].start
    assert t.in_at == b.analysis.play_start
    assert t.length / bar == pytest.approx(round(t.length / bar))
    assert 0 < t.length <= a.analysis.sections[-1].end - a.analysis.sections[-1].start
    assert t.out_at + t.length <= a.analysis.play_end + 1e-9
    assert t.tempo_ratio == pytest.approx(122 / 124)


def test_half_double():
    t = pick_transition(make_song(170), make_song(86))
    assert t.kind == "half_double"
    assert t.tempo_ratio == pytest.approx(170 / 172)


@pytest.mark.parametrize("body_at,kind", [(None, "dropswap"), (1 + 60 / 90 * 4 * 40, "echo")])
def test_unmatched_dropswap_or_echo(body_at, kind):
    a, b = make_song(150), make_song(90, body_at=body_at)
    t = pick_transition(a, b)
    assert t.kind == kind and t.tempo_ratio == 1.0
    assert t.out_at in phrase_boundaries(a.analysis.downbeats)
    if kind == "dropswap":
        assert t.in_at == b.analysis.sections[1].start
    else:
        assert t.in_at == b.analysis.play_start


def test_cut_when_outro_under_two_bars():
    a, b = make_song(150), make_song(90)
    a.analysis.sections[-1].start = a.analysis.play_end - 60 / 150 * 4 * 1.5
    t = pick_transition(a, b)
    assert t.kind == "cut" and t.length == 0 and t.in_at == b.analysis.play_start


def test_fade_without_grid_picks_quietest_second():
    e = [0.8] * 240
    e[230] = 0.01
    a, b = make_song(grid=False, energy_list=e), make_song()
    t = pick_transition(a, b)
    assert t.kind == "fade" and 3 <= t.length <= 6 and t.out_at == 230.0
    assert pick_transition(b, a).kind == "fade"


def test_after_uses_next_boundary():
    a, b = make_song(122), make_song(124)
    t = pick_transition(a, b, after=60.0)
    assert t.out_at == next_boundary(a.analysis.downbeats, 60.0)


@pytest.mark.parametrize("other_bpm", [122, 150, 86])
@pytest.mark.parametrize("after", [None, 20.0])
def test_short_song_clamp(other_bpm, after):
    a, b = make_song(122, duration=70), make_song(other_bpm)
    for x, y in ((a, b), (b, a)):
        t = pick_transition(x, y, after=after if x is a else None)
        assert x.analysis.play_start < t.out_at <= x.analysis.play_end - t.length + 1e-9
        assert t.in_at >= y.analysis.play_start and t.length >= 0


def test_genre_entry():
    cur = make_song(122, "8A", id="cur")
    pop = make_song(124, "9A", id="pop")
    hh = make_song(122, "8A", genre="Hip-hop", id="hh")
    assert genre_entry(cur, [cur, pop, hh], "Hip-hop") == (hh, False)
    assert genre_entry(cur, [cur, pop], "Hip-hop") == (pop, True)
    assert genre_entry(cur, [cur], "Hip-hop") == (None, True)
    assert score_pair(cur, hh) > score_pair(cur, make_song(150, "3B"))


# --- fix round 1 ---
from autodeck.planner import Transition  # noqa: E402


def _invariants(a, b, t):
    assert a.analysis.play_start < t.out_at <= a.analysis.play_end - t.length + 1e-9
    assert t.in_at >= b.analysis.play_start and t.length >= 0


def test_entered_exit_after_ease_back():
    a, b = make_song(122, duration=70), make_song(122)
    bar = 60 / 122 * 4
    ent = Transition("blend", "x", a.id, 0, a.analysis.play_start, 16 * bar, 1.0)
    ease_end = ent.in_at + ent.length + 16 * bar
    t = pick_transition(a, b, entered=ent)
    assert t.out_at >= ease_end - 1e-9 and t.out_at in a.analysis.downbeats
    _invariants(a, b, t)
    assert pick_transition(a, b).out_at < ease_end  # without `entered` it would exit mid-ease


def test_entered_nothing_fits_still_valid():
    a, b = make_song(122, duration=70), make_song(124)
    ent = Transition("blend", "x", a.id, 0, a.analysis.play_start, 500.0, 1.0)
    _invariants(a, b, pick_transition(a, b, entered=ent))


def test_mix_now_near_end_is_on_a_downbeat():
    a, b = make_song(122), make_song(124)
    last = phrase_boundaries(a.analysis.downbeats)[-1]
    for other in (b, make_song(150)):
        t = pick_transition(a, other, after=last + 0.37)
        assert t.out_at in a.analysis.downbeats
        _invariants(a, other, t)


def test_long_blend_does_not_leave_the_outro():
    a, b = make_song(122, duration=55.2), make_song(124)
    d = a.analysis.downbeats
    assert len(d) == 28
    a.analysis.sections = [Section(d[0], d[8], "intro"), Section(d[8], d[17], "body"), Section(d[17], d[27], "outro")]
    b.analysis.sections[0].end = b.analysis.downbeats[16]
    t = pick_transition(a, b)
    assert t.kind == "blend" and t.out_at >= d[17] and t.out_at == d[24]


def test_fade_when_only_b_lacks_grid_uses_a_boundary():
    a, b = make_song(122), make_song(grid=False)
    t = pick_transition(a, b)
    assert t.kind == "fade" and t.out_at in phrase_boundaries(a.analysis.downbeats)
    assert t.out_at + t.length <= a.analysis.play_end


def test_exit_never_before_overlap_end():
    a, b = make_song(122, duration=70), make_song(124)
    bar = 60 / 122 * 4
    ent = Transition("blend", "x", a.id, 0, a.analysis.play_start, 16 * bar, 0.95)
    ov = ent.in_at + ent.length * ent.tempo_ratio
    for after in (None, ov - 5, 3.0):
        for other in (b, make_song(150), make_song(grid=False)):
            t = pick_transition(a, other, after=after, entered=ent)
            assert t.out_at >= ov - 1e-6 and t.kind in {"blend", "half_double", "cut", "echo", "dropswap", "fade"}
            _invariants(a, other, t)
    # nothing fits: overlap end beyond the last boundary -> cut on a downbeat
    ent.length = 80.0
    t = pick_transition(a, b, entered=ent)
    assert t.kind == "cut" and t.out_at == a.analysis.play_end


@pytest.mark.parametrize("a_grid,b_grid", [(True, False), (False, True)])
def test_fade_fallback_respects_overlap_floor(a_grid, b_grid):
    a, b = make_song(122, duration=70, grid=a_grid), make_song(grid=b_grid)
    ent = Transition("blend", "x", a.id, 0, 0.0, 63.5, 1.0)
    t = pick_transition(a, b, entered=ent)
    assert t.kind == "fade" and t.out_at >= 63.5 - 1e-6
    assert t.out_at + t.length <= a.analysis.play_end + 1e-9


def _retime_from(s, t0, bpm):
    """Make s's bars after t0 run at `bpm` (a song that speeds up, like a live sârba)."""
    an = s.analysis
    db = an.downbeats
    i = next(k for k, d in enumerate(db) if d >= t0)
    bar = 240 / bpm
    new = db[: i + 1] + [db[i] + bar * (k - i) for k in range(i + 1, len(db))]
    shift = new[-1] - db[-1]
    an.downbeats = an.beats = new
    an.sections[-1].end = an.play_end = new[-1]
    return shift


def test_blend_matches_local_tempo_of_a_song_that_speeds_up():
    # Regression: the ratio came from each song's single global bpm. A live sârba that
    # speeds up 3-6 % drifted ~0.7-0.8 s over a 16-bar blend. Match the tempo where the
    # blend actually happens.
    a, b = make_song(122), make_song(126)
    _retime_from(a, a.analysis.sections[-1].start - 60, 126)  # a ends at 126 bpm
    t = pick_transition(a, b)
    assert t.kind == "blend"
    assert t.tempo_ratio == pytest.approx(1.0, abs=0.002)  # not 122/126 = 0.968


def test_irregular_bars_in_blend_window_never_beatmatch():
    # Junk beats in a breakdown (Telephone, B.U.G. Mafia in real use): the bars there say
    # nothing about the tempo, so beat-matching on them is a train-wreck. Phrase cut instead.
    a, b = make_song(122), make_song(124)
    db = a.analysis.downbeats
    k = len(db) - 6  # one bar in the closing stretch 30 % too long, the next shortened to fit
    db[k] += 0.6
    t = pick_transition(a, b)
    assert t.kind in ("echo", "dropswap", "cut") or t.out_at + t.length < db[k - 1]


def test_beat_to_beat_skips_outro_and_intro():
    # Owner's mode: no outro-into-intro blend; go from a's main part straight into b's beat.
    a, b = make_song(122), make_song(124)
    a_body = [x for x in a.analysis.sections if x.label == "body"][-1]
    b_body = [x for x in b.analysis.sections if x.label == "body"][0]
    t = pick_transition(a, b, beat_to_beat=True)
    assert t.kind == "blend"
    assert t.in_at == b_body.start                       # b enters on its first full-beat bar
    assert t.out_at + t.length <= a_body.end + 1e-6     # a leaves before its outro
    assert t.out_at >= a_body.start
    assert t.out_at in a.analysis.downbeats
    # it ends as late as possible: right where the outro would start
    assert a_body.end - (t.out_at + t.length) < 16 * 240 / 122


def test_beat_to_beat_falls_back_without_body_sections():
    a, b = make_song(122), make_song(124)
    for s in (a, b):  # only intro + outro, like a short track
        an = s.analysis
        an.sections = [an.sections[0], an.sections[-1]]
        an.sections[0].end = an.sections[1].start
    assert pick_transition(a, b, beat_to_beat=True) == pick_transition(a, b)
