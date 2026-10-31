"""Mix planner: song ordering, transition choice, genre-switch entry. Pure, no I/O.

All Transition times are in ORIGINAL (unstretched) song time. The outgoing song
is assumed to be at its native tempo at out_at (render ease-back finishes before
the exit), so tempo_ratio only describes the incoming song.
"""
from dataclasses import dataclass, replace

from autodeck.config import ANY_GENRE, EASE_BARS
from autodeck.library import Section, Song
from autodeck.music import camelot_score, next_boundary, phrase_boundaries, tempo_relation

FADE_LEN = 4.0        # s; brief wants 3..6
FADE_WINDOW = 20.0    # s before play_end searched for the quietest second
DROPSWAP_MAX_BARS = 32  # b's first body must start within this many of b's bars
ECHO_BARS = 2         # echo / dropswap overlap, in a's bars (also min outro for non-cut)
BLEND_BARS = (8, 4)     # beat-matched overlap lengths, preferred first
MAX_BLEND_SHARE = 0.25  # a blend never takes more than this share of either song
LOCAL_BAR_TOL = 0.06  # a bar in a blend window may differ this much from the window's median bar
LOCAL_MIN_BARS = 4    # tempo is measured over at least this many bars around a blend


@dataclass
class Transition:
    kind: str          # "blend" | "half_double" | "cut" | "echo" | "dropswap" | "fade"
    out_id: str
    in_id: str
    out_at: float      # outgoing ORIGINAL time (phrase boundary when grid reliable)
    in_at: float       # incoming ORIGINAL time where its audio starts
    length: float      # seconds of overlap/effect (0 for cut)
    tempo_ratio: float # incoming speed during blend (1.0 unless blend/half_double)


def _energy(s: Song) -> float:
    e = s.analysis.energy
    return sum(e) / len(e) if e else 0.0


def score_pair(a: Song, b: Song) -> float:
    kind, _ = tempo_relation(a.analysis.bpm, b.analysis.bpm)
    closeness = {"match": 1.0, "half_double": 0.8}.get(kind, 0.0)
    # ponytail: hand-tuned weights (tempo matters most, then key, then energy); retune by ear.
    return (0.5 * closeness
            + 0.3 * camelot_score(a.analysis.camelot, b.analysis.camelot)
            + 0.2 * (1 - abs(_energy(a) - _energy(b))))


# Pair memory (owner's 🔥/👎 on a hand-over, <library>/pairs.json, design xWVJy): pair_key(a, b) ->
# {effect, loved: n, avoided: 0/1, why: None | "songs" | "effect", last}. A loved a->b adds
# LOVED_BONUS to the mix score, LOVED_STEP more per extra 🔥 up to LOVED_MAX; b->a gets half of it
# (songs that blend one way usually blend the other way too). score_pair's range is 0..1, so a
# loved pair wins against all but a much better tempo/key match.
LOVED_BONUS, LOVED_STEP, LOVED_MAX = 0.6, 0.1, 0.9


def pair_key(a: str, b: str) -> str:
    """Directional key of a hand-over a -> b in pairs.json. Kept as "a>b" (the file's format since
    the first pair memory) so existing files and every client lookup stay valid."""
    return f"{a}>{b}"


def _credit(entry: dict | None) -> float:
    n = (entry or {}).get("loved", 0)
    return min(LOVED_BONUS + LOVED_STEP * (n - 1), LOVED_MAX) if n > 0 else 0.0


def memory_bonus(prev: Song, s: Song, pairs: dict[str, dict]) -> float:
    """What the owner's pair memory adds to playing s right after prev."""
    return _credit(pairs.get(pair_key(prev.id, s.id))) or _credit(pairs.get(pair_key(s.id, prev.id))) / 2


def avoids(pairs: dict[str, dict], a: str, b: str) -> bool:
    """True when b must not follow a. A pair 👎'd for its effect only (why="effect") can still
    meet with another effect, so it isn't banned here (the transition just skips that effect)."""
    p = pairs.get(pair_key(a, b))
    return bool(p and p.get("avoided") and p.get("why") != "effect")


def order(current: Song, candidates: list[Song], pinned: dict[int, str], genre: str,
          bonus=None, pairs: dict[str, dict] | None = None, moved: dict | None = None) -> list[Song]:
    """Greedy set order: each next song is the best mix from the previous one.

    `bonus(prev, s) -> float` is added to the mix score (party mode: popularity and rising
    energy), so it only reorders songs that mix about equally well.
    `pairs` is the owner's pair memory: memory_bonus is added, and an avoided pair is never
    placed back to back unless nothing else is left. Pins still win over both.
    `moved`, when given, collects songs an avoid pushed back: id -> (the song it would have
    followed, the index it would have had), for Up next's "moved 3 later" note.
    """
    pairs = pairs or {}

    def score(prev: Song, s: Song) -> float:
        return score_pair(prev, s) + (bonus(prev, s) if bonus else 0.0) + memory_bonus(prev, s, pairs)

    ready = [s for s in candidates if s.status == "ready" and s.analysis and s.id != current.id]
    pool = [s for s in ready if genre == ANY_GENRE or s.effective_genre() == genre]
    by_id = {s.id: s for s in ready}
    # Pinned songs are the owner's explicit choice: they ignore the genre filter.
    pins = {i: by_id[sid] for i, sid in pinned.items() if sid in by_id}
    pin_ids = {s.id for s in pins.values()}
    rest = [s for s in pool if s.id not in pin_ids]
    n = len(rest) + len(pins)
    # A pin past the end can't hold its slot; treat it as a normal song instead of dropping it.
    late = {i for i in pins if i >= n}
    rest += [pins.pop(i) for i in late]
    out: list[Song] = []
    prev = current
    for i in range(len(rest) + len(pins)):
        if i in pins:
            nxt = pins[i]
        else:
            # Avoided successors only when every remaining song is one (never go silent).
            ok = [s for s in rest if not avoids(pairs, prev.id, s.id)] or rest
            nxt = max(ok, key=lambda s: score(prev, s))
            if moved is not None and len(ok) < len(rest):
                best = max(rest, key=lambda s: score(prev, s))
                if best is not nxt:
                    moved.setdefault(best.id, (prev.id, i))
            rest.remove(nxt)
        out.append(nxt)
        prev = nxt
    return out


def _bar(bpm: float) -> float:
    return 60.0 / bpm * 4


def _local_bpm(s: Song, t0: float, t1: float) -> float | None:
    """Tempo of s over [t0, t1] from its own bar lines; None if the bars there are irregular.

    A song's single bpm is a whole-song median. Real songs drift (a live sârba speeds up
    180 -> 196 bpm) and the tracker produces junk bars in breakdowns (Telephone, B.U.G.
    Mafia): a blend matched on the global bpm drifted ~0.7-0.8 s over 16 bars. So blends
    are matched on the tempo where they actually happen, and refused where the bars are junk.
    Bar lines are on 20 ms frames: over >= 4 bars that's <= 0.5 % tempo error, and blends
    that short are short in time too, so the drift stays a few ms.
    """
    db = [d for d in s.analysis.downbeats if t0 - 1e-6 <= d <= t1 + 1e-6]
    if len(db) < 3:
        return None
    bars = [y - x for x, y in zip(db, db[1:])]
    med = sorted(bars)[len(bars) // 2]
    if any(abs(x / med - 1) > LOCAL_BAR_TOL for x in bars):
        return None
    return 240.0 * (len(db) - 1) / (db[-1] - db[0])


def _main_view(s: Song, side: str) -> Song | None:
    """Planning view of s without its intro (side="in") or outro (side="out"), for
    beat-to-beat mode. The neighbouring body section plays the intro/outro role, so the
    normal planner places the blend inside the main parts. None if s has no body section
    (nothing to skip to). Times stay the song's real original times."""
    an = s.analysis
    body = [x for x in an.sections if x.label == "body"]
    if not body or not an.grid_reliable:
        return None
    if side == "in":
        i = an.sections.index(body[0])
        secs = [Section(body[0].start, body[0].end, "intro"), *an.sections[i + 1:]]
        view = replace(an, play_start=body[0].start, sections=secs)
    else:
        i = an.sections.index(body[-1])
        secs = [*an.sections[:i], Section(body[-1].start, body[-1].end, "outro")]
        view = replace(an, play_end=body[-1].end, sections=secs)
    return replace(s, analysis=view)


def _section_len(s: Song, label: str, first: bool) -> float:
    secs = [x for x in s.analysis.sections if x.label == label]
    if not secs:
        return 0.0
    x = secs[0] if first else secs[-1]
    return x.end - x.start


def _pick_out(a: Song, length: float, after: float | None, min_out: float = 0.0, wide: bool = True,
              late: bool = False) -> float | None:
    """Phrase boundary of a to leave at, with out_at + length <= play_end and out_at >= min_out.

    wide=False only accepts boundaries inside the outro (or right after `after`);
    wide=True also accepts earlier ones when the outro has none. None if nothing fits.
    """
    an = a.analysis
    db, end = an.downbeats, an.play_end - length
    if after is not None:
        t = next_boundary(db, max(after, min_out - 1e-9))
        return t if t is not None and t <= end else None
    outro_start = an.sections[-1].start if an.sections else an.play_start
    lo = max(outro_start, an.play_start, min_out - 1e-9)
    # Prefer a 16-bar boundary, then 8, inside the outro; out_at must be after play_start
    # so a short song never exits before (or at) its own entry.
    for bars in (16, 8):
        c = [t for t in phrase_boundaries(db, bars) if lo <= t <= end and t > an.play_start]
        if c:
            # Normally the first: the blend covers the outro. late=True (beat-to-beat): the
            # last, so the blend ends right where the skipped outro would have started.
            return c[-1] if late else c[0]
    if not wide:
        return None
    c = [t for t in phrase_boundaries(db) if an.play_start < t <= end and t >= min_out - 1e-9]
    return c[-1] if c else None


def _search(a: Song, lengths: list[float], after: float | None, floors: list[float], late: bool = False):
    """First (length, out_at) that fits, trying `floors` (min out_at) in order. Order matters (see comments): exits that
    respect the incoming ease-back come first, then plain ones; within each, a
    boundary inside the outro for ANY candidate length beats an earlier one, so a
    long blend can't drag the exit out of the outro when a shorter one fits."""
    for floor in floors:
        for wide in (False, True):
            for n in lengths:
                out_at = _pick_out(a, n, after, floor, wide, late)
                if out_at is not None:
                    return n, out_at
    return None


def _fade_out(a: Song, after: float | None) -> float:
    an = a.analysis
    end = max(an.play_end - FADE_LEN, an.play_start)
    if after is not None:
        return min(max(after, an.play_start), end)
    lo = int(max(an.play_start, an.play_end - FADE_WINDOW))
    hi = int(end)
    e = an.energy
    cand = [s for s in range(lo, hi + 1) if s < len(e)]
    # Quietest second in the closing window (no grid to respect); ties -> latest.
    return float(min(cand, key=lambda s: (e[s], -s))) if cand else end


# Effects the owner can force from the dropdown. "blend" also covers half_double.
# Beat-matched (overlap, both songs at one tempo): blend, bass_swap, filter_sweep.
# Exit effects (outgoing does its thing for N bars on a phrase, then the incoming lands):
EXIT_FX_BARS = {"echo": 2, "loop_roll": 2, "brake": 1, "backspin": 1, "reverb": 2, "gate": 2, "riser": 4}
# These land the incoming on its drop (first full-beat bar) when it's near the start: a
# stop/stutter/riser into a quiet intro would fall flat.
INTO_DROP = {"loop_roll", "brake", "backspin", "gate", "riser"}
MATCHED_FX = ("blend", "bass_swap", "filter_sweep")
# Effects that still work when one song has no trustworthy beat grid (owner: the dropdown
# showed only "fade" for ~1/4 of pairs). Roll/gate need the outgoing's bar grid to sound right.
GRIDLESS_OK = ("echo", "brake", "backspin", "reverb", "riser", "cut")
FORCEABLE = (*MATCHED_FX, *EXIT_FX_BARS, "dropswap", "cut", "fade")


def pick_transition(a: Song, b: Song, after: float | None = None, entered: Transition | None = None,
                    beat_to_beat: bool = False, force: str | None = None) -> Transition:
    """Plan a -> b. `after` = mix now: leave at the first phrase boundary >= after.

    `entered` is the transition that brought `a` in. When given, a's exit is chosen
    at or after the end of its tempo ease-back (in a's original time:
    entered.in_at + entered.length * entered.tempo_ratio + EASE_BARS bars) if a
    fitting boundary exists. Otherwise the normal choice is used and the renderer
    has to compress the ease (short songs). out_at >= entered overlap end
    (in_at + length * tempo_ratio) is a hard floor: if no boundary satisfies it, the
    result is a cut at the first downbeat >= that point (else play_end).

    `force` (one of FORCEABLE) is the owner's pick from the transition dropdown. It only
    narrows the choice: a forced kind that can't work for this pair (e.g. a beat blend
    between unrelated tempos) yields the normal choice, so the result's kind may differ.
    """
    late = False
    if beat_to_beat:
        # Owner's "beat to beat" mode: skip a's outro and b's intro, blending main part into
        # main part. Falls back to the normal plan for a song without a body section.
        va, vb = _main_view(a, "out"), _main_view(b, "in")
        if va and vb:
            a, b, late = va, vb, True
    an, bn = a.analysis, b.analysis
    mk = lambda kind, out_at, in_at, length, ratio=1.0: Transition(  # noqa: E731
        kind, a.id, b.id, out_at, in_at, length, ratio)
    bar_a = _bar(an.bpm)
    # overlap_end: the renderer plays a at entered.tempo_ratio until here, so exiting earlier
    # would leave a sped up at out_at and misalign the next blend. HARD floor.
    # ease_end: where the ease-back finishes; best-effort floor above it.
    overlap_end = (entered.in_at + entered.length * entered.tempo_ratio) if entered else 0.0
    floors = [overlap_end + EASE_BARS * bar_a, overlap_end] if entered else [0.0]

    both = an.grid_reliable and bn.grid_reliable
    if not both and force in GRIDLESS_OK:
        # Exit effects don't beat-match: they play the outgoing out, then the incoming starts.
        # Only the START needs care: a's phrase boundary when a has a grid, else its quietest
        # second near the end (like a fade). b's grid doesn't matter, so these stay available.
        bars = EXIT_FX_BARS.get(force, 0)
        length = bars * bar_a
        hit = _search(a, [length], after, floors) if an.grid_reliable else None
        out_at = hit[1] if hit else min(_fade_out(a, after), max(an.play_end - length, an.play_start))
        out_at = max(out_at, min(overlap_end, an.play_end - length))  # hard overlap_end floor
        return mk(force, out_at, bn.play_start, length)
    if force == "fade" or not both:
        length = min(FADE_LEN, max(an.play_end - an.play_start, 0.0))
        hit = _search(a, [length], after, floors) if an.grid_reliable else None
        out_at = hit[1] if hit else _fade_out(a, after)
        if not hit:  # unfloored fallback: apply the hard overlap_end floor here too
            out_at = max(out_at, min(overlap_end, an.play_end - length))
        return mk("fade", out_at, bn.play_start, length)

    kind, ratio = tempo_relation(an.bpm, bn.bpm)
    outro = _section_len(a, "outro", first=False)
    if force is not None and force not in MATCHED_FX:
        kind = "none"  # owner chose a non-beatmatched effect: skip the blend

    if kind != "none":
        # b's intro lasts longer in outgoing time when b is slowed (ratio < 1).
        # Owner: 16-bar blends (~38 s at 100 BPM) felt like the next song "starts, then blends
        # later", and on a 57 s song the blend was most of the song. So at most 8 bars, and never
        # more than MAX_BLEND_SHARE of either song's played length.
        share = MAX_BLEND_SHARE * min(an.play_end - an.play_start, bn.play_end - bn.play_start)
        cap = min(outro, _section_len(b, "intro", first=True) / ratio, share)
        whole = int(cap // bar_a)
        # Prefer 8, then 4 bars; below 4 use whatever whole bars fit; none -> cut.
        bars = [n for n in BLEND_BARS if n <= whole] or ([whole] if whole >= 1 else [])
        bars += [m for m in range(min(whole, 3), 0, -1) if m not in bars]
        hit = _search(a, [n * bar_a for n in bars], after, floors, late)
        if hit:
            n_len, out_at = hit
            la = _local_bpm(a, out_at, out_at + max(n_len, LOCAL_MIN_BARS * bar_a))
            lb = _local_bpm(b, bn.play_start, bn.play_start + max(n_len * ratio, LOCAL_MIN_BARS * _bar(bn.bpm)))
            local = tempo_relation(la, lb) if la and lb else ("none", 1.0)
            if local[0] != "none":
                label = force if force in ("bass_swap", "filter_sweep") else (
                    "blend" if local[0] == "match" else "half_double")
                return mk(label, out_at, bn.play_start, n_len, local[1])
            # ponytail: one exit tried; junk/drifting bars there -> phrase cut/echo, not another exit.
        kind = "none"  # no fitting overlap, or the bars at it can't be trusted: cut/echo below

    # Non-matched (or blend that can't fit): cut / echo / dropswap on a phrase boundary.
    if force in EXIT_FX_BARS and force != "echo":
        length = EXIT_FX_BARS[force] * bar_a
        hit = _search(a, [length], after, floors, late) if outro >= length else None
        if hit:
            body = next((x for x in bn.sections if x.label == "body"), None)
            drop = (force in INTO_DROP and body is not None
                    and body.start - bn.play_start <= DROPSWAP_MAX_BARS * _bar(bn.bpm))
            return mk(force, hit[1], body.start if drop else bn.play_start, length)
    length = ECHO_BARS * bar_a
    if kind == "none" and outro >= length and force != "cut":
        hit = _search(a, [length], after, floors, late)
        if hit:
            body = next((x for x in bn.sections if x.label == "body"), None)
            # Drop swap only when the owner forces it: they disliked it as an automatic pick
            # (2026-10-30), so unrelated-tempo pairs get an echo out instead.
            if (force == "dropswap" and body
                    and body.start - bn.play_start <= DROPSWAP_MAX_BARS * _bar(bn.bpm)):
                return mk("dropswap", hit[1], body.start, length)
            return mk("echo", hit[1], bn.play_start, length)
    hit = _search(a, [0.0], after, floors)
    if hit:
        out_at = hit[1]
    else:
        # No phrase boundary left after `after` / overlap_end: take the next downbeat (never mid-bar), else play_end.
        t = max(overlap_end, after if after is not None else an.play_start)
        out_at = next((d for d in an.downbeats if d >= t), an.play_end)
        out_at = min(max(out_at, an.play_start), an.play_end)
    return mk("cut", out_at, bn.play_start, 0.0)


def genre_entry(current: Song, pool: list[Song], genre: str) -> tuple[Song | None, bool]:
    """Best next song for a genre switch. (song, fell_back_to_any)."""
    ready = [s for s in pool if s.status == "ready" and s.analysis and s.id != current.id]
    if not ready:
        return None, True
    hit = [s for s in ready if genre == ANY_GENRE or s.effective_genre() == genre]
    # Genre ran dry: fall back to the whole pool so the mix never goes silent.
    return max(hit or ready, key=lambda s: score_pair(current, s)), not hit
