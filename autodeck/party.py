"""Party mode: flavors (hits / oldies / latino), song fit, popularity, suggestions.

A song's party facts live in Song.meta, filled from Last.fm by the engine's enrichment
thread: {"artist", "title", "listeners", "tags"} (tags = the track's own + its artist's,
lower-case). Pure functions here; the only network use is suggestions(), via lastfm.
"""
import math
import unicodedata

from . import deezer, lastfm
from .music import tempo_relation
from .library import Song

FLAVORS = ("hits", "oldies", "latino")
# A track with this many Last.fm listeners is a crowd-pleaser (Africa ~2M, a niche track ~10k).
# ponytail: one global threshold; a per-genre one if Romanian hits (smaller audience) miss it.
HIT_LISTENERS = 300_000
TAGS = {
    "oldies": {"oldies", "60s", "70s", "80s", "90s", "classic rock", "disco", "motown",
               "soul", "funk", "rock and roll", "new wave", "classic", "retro", "golden oldies"},
    "latino": {"latin", "latino", "latin pop", "reggaeton", "salsa", "bachata", "cumbia",
               "merengue", "dembow", "spanish", "tropical", "urbano latino", "latin trap",
               "musica latina", "reggaeton latino", "brazilian", "samba"},
}
# Where suggestions for a flavor come from besides "similar to what's playing".
SOURCES = {"hits": [("chart", None)], "oldies": [("tag", "oldies"), ("tag", "80s")],
           "latino": [("tag", "latin"), ("tag", "reggaeton")]}


def listeners(s: Song) -> int:
    return int((s.meta or {}).get("listeners") or 0)


def fits(s: Song, flavor: str) -> bool:
    if flavor == "hits":
        return listeners(s) >= HIT_LISTENERS
    return bool(TAGS.get(flavor, set()) & set((s.meta or {}).get("tags") or []))


def popularity(s: Song) -> float:
    """0..1 from listener count, log-scaled: 10k -> 0.4, 300k -> ~0.7, 3M -> 0.9."""
    n = listeners(s)
    return min(1.0, math.log10(n) / 7) if n > 0 else 0.0


def key(artist: str, title: str) -> str:
    """Matching key for "is this suggestion already in the library / rejected"."""
    t = unicodedata.normalize("NFKD", f"{artist} {title}").encode("ascii", "ignore").decode().lower()
    return " ".join("".join(c if c.isalnum() else " " for c in t).split())


def suggestions(current: Song | None, flavor: str | None, pool: list[Song], rejected: set[str],
                n: int = 8) -> list[dict]:
    """Songs the owner doesn't have that fit what's playing and the flavor.

    "Similar to the current song" first (Last.fm listening sessions: what people play
    together), then the flavor's own chart. Excludes library songs and rejected ones.
    """
    have = set()
    for s in pool:
        a, t = (s.meta or {}).get("artist"), (s.meta or {}).get("title")
        if not a:
            a, t = lastfm.split_title(s.title, s.channel)
        have.add(key(a, t))
    seen = set(have) | set(rejected)
    out: list[dict] = []

    def take(pairs, reason):
        for a, t in pairs:
            k = key(a, t)
            if k and k not in seen:
                seen.add(k)
                out.append({"artist": a, "title": t, "reason": reason})
            if len(out) >= n:
                return

    if current is not None:
        a, t = (current.meta or {}).get("artist"), (current.meta or {}).get("title")
        if not a:
            a, t = lastfm.split_title(current.title, current.channel)
        take(lastfm.similar_tracks(a, t, 25), f"people play it with {t}")
    for kind, tag in SOURCES.get(flavor or "", [("chart", None)]):
        if len(out) >= n:
            break
        take(lastfm.chart_top_tracks() if kind == "chart" else lastfm.tag_top_tracks(tag),
             "top hit" if kind == "chart" else f"top {tag}")
    out = out[:n]
    # Tempo for songs we have no audio for comes from Deezer (often, not always, known).
    # "mixes": beat-matchable with what's playing (within 8 %, or half/double time), which is
    # what the owner wants to see; those go first, otherwise the source order is kept.
    cur_bpm = current.analysis.bpm if current is not None and current.analysis else None
    for x in out:
        x.update(deezer.lookup(x["artist"], x["title"]))
        x["mixes"] = bool(cur_bpm and x["bpm"]) and tempo_relation(cur_bpm, x["bpm"])[0] != "none"
    out.sort(key=lambda x: not x["mixes"])
    return out


def bonus(prev: Song, s: Song) -> float:
    """Party ordering bonus on top of the mix score: crowd-pleasers first, energy building.
    Small weights on purpose: a bonus reorders songs that mix about equally well, it never
    trades a clean beatmatch for a famous song."""
    def energy(x: Song) -> float:
        e = x.analysis.energy if x.analysis else []
        return sum(e) / len(e) if e else 0.5
    dance = float((s.meta or {}).get("danceability") or 0.5)  # 0..1, measured from the audio
    return 0.15 * popularity(s) + 0.1 * dance + 0.1 * max(0.0, energy(s) - energy(prev))
