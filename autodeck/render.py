"""Tempo-mapped rendering with Rubber Band (pitch is always preserved by Rubber Band).

Time map: points are (original_s, rendered_s). Rendered time 0 == original
play_start; the render only covers [play_start, play_end].
"""
import bisect
import hashlib
import json
import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pyrubberband
import soundfile as sf

from autodeck.config import EASE_BARS

log = logging.getLogger(__name__)
from autodeck.library import ANALYSIS_VERSION, Song, audio_path, song_dir
from autodeck.planner import Transition

_EPS = 1e-6


@dataclass
class TimeMap:
    points: list[tuple[float, float]]  # (original_s, rendered_s), strictly increasing in both

    def _seg(self, t: float) -> int:
        """Index of the segment [points[i], points[i+1]] that t falls in. t outside the
        points uses the first/last segment, i.e. the edge speed is extrapolated."""
        i = bisect.bisect_right([p[0] for p in self.points], t) - 1
        return min(max(i, 0), len(self.points) - 2)

    def to_rendered(self, t: float) -> float:
        # Piecewise-linear; before the first / after the last point we extrapolate
        # with the edge segment's slope (so out-of-range times stay monotonic).
        if len(self.points) < 2:
            return t - self.points[0][0] + self.points[0][1]
        (o0, r0), (o1, r1) = self.points[self._seg(t)], self.points[self._seg(t) + 1]
        return r0 + (t - o0) * (r1 - r0) / (o1 - o0)

    def to_original(self, r: float) -> float:
        """Inverse of to_rendered (the map is strictly increasing, so it's well defined).
        The player reports positions in rendered seconds; the planner wants original time."""
        if len(self.points) < 2:
            return r - self.points[0][1] + self.points[0][0]
        i = bisect.bisect_right([p[1] for p in self.points], r) - 1
        i = min(max(i, 0), len(self.points) - 2)  # edge segments extrapolate, like to_rendered
        (o0, r0), (o1, r1) = self.points[i], self.points[i + 1]
        return o0 + (r - r0) * (o1 - o0) / (r1 - r0)

    def speed_at(self, t: float) -> float:
        """d(original)/d(rendered) = playback-speed factor (right-continuous at breakpoints)."""
        if len(self.points) < 2:
            return 1.0
        (o0, r0), (o1, r1) = self.points[self._seg(t)], self.points[self._seg(t) + 1]
        return (o1 - o0) / (r1 - r0)


def time_map_for(song: Song, incoming: Transition | None, outgoing: Transition | None = None) -> TimeMap:
    an = song.analysis
    ps, pe = an.play_start, an.play_end
    # segs: (original_start, original_end, speed)
    segs: list[tuple[float, float, float]] = []
    ratio = incoming.tempo_ratio if incoming else 1.0
    if incoming and outgoing:
        # The planner normally guarantees the exit is after the overlap. The only known
        # exception is a fade (not beat-matched, so harmless); warn, never crash playback.
        # Capped at play_end: an unsatisfiable floor degrades (song ends sped up).
        ov = min(incoming.in_at + incoming.length * ratio, an.play_end)
        if outgoing.out_at < ov - 1e-6:
            log.warning("exit %.2f before overlap end %.2f for %s", outgoing.out_at, ov, song.id)
    if incoming and abs(ratio - 1.0) > _EPS:
        # The overlap lasts `length` s of rendered time, which covers length*ratio s of
        # this song's original audio. Audio before in_at plays at the ratio too.
        ov_end = incoming.in_at + incoming.length * ratio
        segs.append((ps, ov_end, ratio))
        # Ease back to 1.0 over EASE_BARS of the song's own bars. ponytail: stepped per bar
        # (each step at its midpoint speed), make it finer if it sounds steppy.
        bar = 4 * 60 / an.bpm
        ease = EASE_BARS * bar
        if outgoing:
            # Compress so the ease ends exactly at the planned exit (0 -> jump to 1.0 there).
            ease = min(ease, max(outgoing.out_at - ov_end, 0.0))
        n = max(1, round(ease / bar)) if ease > _EPS else 0
        for i in range(n):
            segs.append((ov_end + i * ease / n, ov_end + (i + 1) * ease / n,
                         ratio + (1.0 - ratio) * (i + 0.5) / n))
        segs.append((ov_end + ease, pe, 1.0))
    else:
        segs.append((ps, pe, 1.0))
    # Clip to [play_start, play_end], drop empty segments, integrate rendered time.
    pts, r = [(ps, 0.0)], 0.0
    for a, b, s in segs:
        a, b = max(a, ps), min(b, pe)
        if b - a <= _EPS:
            continue
        r += (b - a) / s
        pts.append((b, r))
    if pts[-1][0] < pe - _EPS:  # defensive: segments ended before play_end; keep the final point at play_end
        pts.append((pe, r + (pe - pts[-1][0])))
    return TimeMap(pts)


def _is_identity(tm: TimeMap) -> bool:
    return all(abs((o1 - o0) - (r1 - r0)) < 1e-4 for (o0, r0), (o1, r1) in zip(tm.points, tm.points[1:]))


RB_ARGS = {"--centre-focus": ""}  # see render()


def _hash(tm: TimeMap) -> str:
    # Rounded so float noise doesn't bust the cache; version so re-analysis re-renders;
    # stretch options so a quality change never serves an old render.
    key = json.dumps([[round(o, 3), round(r, 3)] for o, r in tm.points] + [ANALYSIS_VERSION, RB_ARGS])
    return hashlib.sha1(key.encode()).hexdigest()[:16]


def render(song: Song, tm: TimeMap) -> Path:
    out = song_dir(song.id) / f"render-{_hash(tm)}.wav"
    if out.exists():
        return out
    y, sr = sf.read(audio_path(song.id), dtype="float32", always_2d=False)
    o_start, o_end = tm.points[0][0], tm.points[-1][0]
    y = y[int(round(o_start * sr)): int(round(o_end * sr))]
    if not _is_identity(tm):
        # Sample-index pairs relative to the trimmed start; the final (n_src, n_dst)
        # pair is required or rubberband stops mapping early.
        pairs = [(int(round((o - o_start) * sr)), int(round(r * sr))) for o, r in tm.points]
        pairs[-1] = (len(y), pairs[-1][1])
        # Owner wants high sound quality. centre-focus keeps the stereo image (and mono
        # compatibility) intact. NOT the R3 '--fine' engine: in rubberband 4.0 it ignores
        # the --timemap keyframes (stretches evenly instead of blend-then-ease), which would
        # break beat alignment; the onset test in test_render catches that.
        # A copy: pyrubberband adds --time/--timemap into the dict it is given.
        y = pyrubberband.timemap_stretch(y, sr, pairs, rbargs=dict(RB_ARGS))
    # Atomic write: the player may be reading/serving while the worker renders.
    # Unique temp name: /api/deck and /api/transition can render the same map at once,
    # and a shared "<name>.tmp" would let them clobber each other (last os.replace wins, whole).
    fd, tmp = tempfile.mkstemp(dir=out.parent, prefix=out.name + ".", suffix=".tmp")
    os.close(fd)
    try:
        sf.write(tmp, y, sr, format="WAV", subtype="PCM_16")
        os.replace(tmp, out)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return out
