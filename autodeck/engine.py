"""In-memory mix state shared by the HTTP layer and the background worker.

Split out of server.py so the queue rules can be read (and tested) without the
HTTP plumbing. One lock for everything: library.LOCK (an RLock). The worker
calls get_state() while already holding it, so any second lock here could
deadlock; reusing the same RLock can't.
"""
import json
import os
import re
import logging
import threading
import time
import uuid
from dataclasses import asdict
from pathlib import Path

import soundfile as sf

from . import config, fetch, jobs, lastfm, library, mcp_activity, moods, party, standalone, stems
from .library import Song
from .planner import FORCEABLE, Transition, avoids, genre_entry, order, pair_key, pick_transition, score_pair
from .render import TimeMap, render, time_map_for

LOCK = library.LOCK
PLANS_PER_SONG = 4
# rejected.json bucket that applies to every party flavor: where the single global list from
# before per-source exclusions (2026-10-31) went. Those were all party rejections.
PARTY_BUCKET = "party"
# ponytail: a mood/genre draft lists at most this many songs; paging if moods grow past it.
DRAFT_MAX = 60
# Saved playlists (playlists.json, design HJGfo): how each one plays and when it re-reads YouTube.
PL_ORDERS = ("playlist", "mix")
PL_AT_END = ("keep_going", "repeat", "stop")
PL_STALE = 24 * 3600  # picking a playlist re-reads it when the last sync is older than this
LIST_RE = re.compile(r"[A-Za-z0-9_-]{2,64}")  # a YouTube list id (PL…, LM, OLAK5uy_…, RD…)
ONE_SONG = ("That’s one song playing inside a playlist. Add just the song, or open the playlist "
            "and copy the link from its page.")


def sync_diff(old: list[str], new: list[str]) -> tuple[list[int], list[str]]:
    """(indexes in `new` of ids that weren't in `old`, ids of `old` gone from `new`)."""
    o, n = set(old), set(new)
    return [k for k, i in enumerate(new) if i not in o], [i for i in old if i not in n]


def _fail_reason(failed: list[Song]) -> str | None:
    """One short reason shared by a playlist's songs that can't play, for "can't play (…)"."""
    kinds = set()
    for s in failed:
        e = (s.error or "").lower()
        kinds.add("unavailable in your country" if "country" in e else "private" if "private" in e
                  else "unavailable" if ("unavailable" in e or "removed" in e or "deleted" in e) else None)
    return kinds.pop() if len(kinds) == 1 else None


def _fit_reason(prev: Song | None, s: Song, rest: list[Song]) -> str | None:
    """Why the mix order put s after prev: same key, or the closest tempo of what was left."""
    if prev is None or not prev.analysis or not s.analysis:
        return None
    if prev.analysis.camelot and prev.analysis.camelot == s.analysis.camelot:
        return "same key"
    gap = lambda x: abs(x.analysis.bpm - prev.analysis.bpm) if x.analysis else 1e9  # noqa: E731
    return "closest tempo" if len(rest) > 1 and gap(s) <= min(gap(x) for x in rest) else None


PAIR_WHY = ("songs", "effect")  # the 👎 question: "These songs don't go together" / "Wrong effect for them"


def pair_effect(kind: str | None) -> str | None:
    """A logged transition kind as the effect picker knows it: half_double is the picker's "blend"
    (forcing "blend" plays it); anything the picker can't force is None."""
    kind = "blend" if kind == "half_double" else kind
    return kind if kind in FORCEABLE else None


def migrate_pair(v) -> dict | None:
    """One pairs.json value in the current shape. The first pair memory stored just "loved" /
    "avoided"; those become one 🔥 / a 👎 with no effect, reason or time (nothing is lost: that
    was all it knew). Anything unreadable is dropped rather than crash the load."""
    if v in ("loved", "avoided"):
        return {"effect": None, "loved": int(v == "loved"), "avoided": int(v == "avoided"), "why": None, "last": None}
    if not isinstance(v, dict):
        return None
    try:
        loved, avoided = max(int(v.get("loved") or 0), 0), int(bool(v.get("avoided")))
    except (TypeError, ValueError):
        return None
    return {"effect": pair_effect(v.get("effect")), "loved": loved, "avoided": avoided,
            "why": v.get("why") if v.get("why") in PAIR_WHY else None, "last": v.get("last")}


def rate_entry(entry: dict | None, verdict: str, effect: str | None, when: str) -> dict:
    """The pair after one rating: 🔥 adds one (and clears an old 👎), 👎 resets the pair to avoided.
    effect/last describe this latest hand-over; a 🔥 without a known effect keeps the old one."""
    old = entry if entry and not entry.get("avoided") else None
    if verdict == "loved":
        return {"effect": effect or (old or {}).get("effect"), "loved": (old or {}).get("loved", 0) + 1,
                "avoided": 0, "why": None, "last": when}
    return {"effect": effect, "loved": 0, "avoided": 1, "why": None, "last": when}


def _shown_sections(an) -> list:
    """The sections a deck shows (sections_rendered), in order: the ones inside the played span.
    The cued deck's section clicks send an index into this list (Engine.transition's `enter`)."""
    return [x for x in an.sections if x.end > an.play_start and x.start < an.play_end]


class EngineError(Exception):
    """Owner-facing error; the server maps it to an HTTP status + {error}."""

    def __init__(self, status: int, msg: str):
        super().__init__(msg)
        self.status = status


def _known_genre(g: str | None, allow_none=False, allow_any=False) -> None:
    if g is None and allow_none:
        return
    if g in config.genres() or (allow_any and g == config.ANY_GENRE):
        return
    raise EngineError(400, f"Unknown genre: {g}")


class Engine:
    def __init__(self, pool: list[Song] | None = None):
        # The worker keeps references to these Song objects (identity checks), so the
        # list is mutated in place for its whole life and Songs are never replaced.
        self.pool: list[Song] = library.load_pool() if pool is None else pool
        self.playing: str | None = None
        self.played: set[str] = set()
        self.upcoming: list[str] = []
        self.genre = config.ANY_GENRE
        self.notice: str | None = None
        self.version = 0
        # Ruling B: the map each song was last rendered with, and the transition that
        # brought it in. Persisted with playing/played in session.json (see _save_session):
        # after a restart the playing song is still on its stretched render, and planning its
        # exit on the identity map put the cut more than a bar off the grid.
        self.maps: dict[str, TimeMap] = {}
        self.entered: dict[str, Transition] = {}
        # Candidate plans from /api/transition, keyed by plan_id: (in_id, TimeMap, Transition).
        # They only become maps/entered when the player reports it actually started that
        # render (POST /api/playing with plan_id). A response the player dropped (superseded
        # or too late) must never describe the song that is really playing, or the next
        # transition would be computed on the wrong time map.
        self.plans: dict[str, tuple[str, TimeMap, object]] = {}
        # The owner's Up next (decision A, 2026-10-31): songs kept from the draft (✓ / Add all),
        # "play next" from search or the assistant, and anything dragged into place, in the
        # owner's order. Not-ready ones go to the front of the worker's list (prepped before
        # anything, cap or not); ready ones play after the locked-in rows, whatever the source.
        # Dropped once they start playing. Saved in session.json with their `kept` notes.
        self.requested: list[str] = []
        # "Play next on deck B" (play_next) picked a song that isn't ready yet: it waits at the
        # top of `requested`, and the planned song stays planned until _recompute sees it ready,
        # then it jumps ahead of the locked-in rows once. Not saved: after a restart it is simply
        # the first kept song.
        self.next_pick: str | None = None
        # Moods built from public playlists (moods.py): name -> song ids. A mood is selected like
        # a genre (its name is in the genres list), but membership is this list, so a song keeps
        # its own genre label and can be in several moods.
        self.moods: dict[str, list[str]] = self._load_moods()
        # Playlist votes per mood, for ranking its draft: name -> {"of": votes possible,
        # "votes": {song id: votes}}. Separate file so moods.json keeps its simple shape.
        self.mood_votes: dict[str, dict] = self._load_json_dict("mood-votes.json")
        # Party mode: a flavor (party.FLAVORS) replaces the genre filter while on; None = off.
        self.party: str | None = None
        # The owner's ✕ per source: source key ("mood:Balkan party", "genre:Pop", "party:Hits",
        # or PARTY_BUCKET for every flavor) -> party.key()s never to suggest there again.
        self.rejected: dict[str, set[str]] = self._load_rejected()
        # Songs kept (✓ / Add all) from a draft -> the source's label, shown in Up next as
        # "kept from Balkan party". Saved in session.json; dropped once the song plays.
        self.kept: dict[str, str] = {}
        # Pair memory (design xWVJy): the owner's ratings of a hand-over, pair_key(a, b) -> {effect,
        # loved, avoided, why, last} (migrate_pair). Directional and kept for removed songs too
        # (re-adding the link brings the memory back).
        self.pairs: dict[str, dict] = self._load_pairs()
        # This session's last rating per pair and the entry before it, so tapping it again undoes
        # it (Played rows show it pressed). ponytail: memory only, a restart forgets what can be
        # undone (the ratings themselves are in pairs.json); persist in session.json if missed.
        self.pair_undo: dict[str, tuple[str, dict | None]] = {}
        # Songs an avoid pushed back in the last _recompute: id -> (song it would have followed,
        # index it would have had in the auto order, index it got). Up next's "moved 3 later" note.
        self.moved: dict[str, tuple[str, int, int]] = {}
        # Saved playlists: list id -> entry (_load_playlists has the fields). Dict order = save order.
        self.playlists: dict[str, dict] = self._load_playlists()
        # The active playlist source (a list id) or None. One source at a time, like genre / party.
        self.playlist: str | None = None
        # This run through the active playlist: song id -> "HH:MM" it started. Separate from
        # `played` (the whole session) so picking a playlist plays all of it, even songs heard
        # earlier tonight, and "Repeat" can start over without touching the Played log.
        self.pl_run: dict[str, str] = {}
        self._sugg: tuple[tuple, float, list] | None = None  # ((playing, source key), time, list)
        self._enrich_stop = threading.Event()
        # Effect forced for the playing song's exit (dropdown, MCP). Server-side so every
        # client agrees; cleared when the next song starts. mix_now_seq: bumped by "mix now"
        # requests from anywhere (MCP); the player mixes when it sees a new value.
        self.next_effect: str | None = None
        self.mix_now_seq = 0
        # Owner's "beat to beat" mode: blends skip a's outro and b's intro (planner._main_view).
        self.beat_to_beat = False
        # What an assistant changed (design m6p10 "via MCP" tags): "song:<id>" (a song it put in
        # Up next), "source" (vibe / party / playlist), "effect" (the next effect). A tag goes when
        # the song plays or the owner changes that thing (_mark with by=None). Memory only: after
        # a restart nothing is "just changed" any more.
        self.by_mcp: set[str] = set()
        # The MCP button, toast and Activity log (mcp_activity.py); in /api/state as `mcp`.
        self.mcp = mcp_activity.Activity()
        # Created here, started by the server lifespan (tests never start it).
        self.worker = jobs.Worker(self.get_state, on_change=self._on_worker_change, stems_wanted=self.stems_wanted)
        with LOCK:
            self._load_session()
            self._recompute()

    # --- worker interface ----------------------------------------------------
    def get_state(self):
        with LOCK:  # reentrant: the worker calls this while holding LOCK in _set
            first = [i for i in self.requested if i not in self.upcoming]  # requested, not ready yet
            # Playlist songs are prepared from the top: the active playlist's, then the newest
            # saved one's (so a playlist saved a minute ago is playable soon, the rest keep coming).
            # They ride in the worker's "upcoming" slot, which is prepped before anything else.
            by = {s.id: s for s in self.pool}
            newest = max(self.playlists.values(), key=lambda p: p["saved_at"], default=None)
            prep = []
            for pl in dict.fromkeys(x["list_id"] for x in (self._pl(), newest) if x):
                prep += [i for i in self.playlists[pl]["song_ids"]
                         if i in by and by[i].status not in ("ready", "failed")]
            ids = list(dict.fromkeys(first + list(self.upcoming) + prep))
            return list(self.pool), ids, self.genre, set(self.played)

    def stems_wanted(self) -> list[str]:
        """Songs that should have stems: the ON AIR one, then the next few. Only songs about to play,
        so separation (the heaviest job per song) is never spent on the whole library."""
        with LOCK:
            by = {s.id: s for s in self.pool}
            ids = ([self.playing] if self.playing else []) + self.upcoming[: config.STEMS_AHEAD]
            return [i for i in dict.fromkeys(ids) if i in by and by[i].status == "ready"]

    def _stems_status(self, sid: str, wanted: list[str]) -> str | None:
        """Up next's quiet stems icon: "ready" | "preparing" | "failed" | None (not near enough)."""
        if sid not in wanted:
            return None
        if stems.has(sid):
            return "ready"
        return "failed" if sid in self.worker.stems_failed else "preparing"

    def _on_worker_change(self) -> None:
        # A song changed status (e.g. became ready): it may belong in the queue now.
        with LOCK:
            self._recompute()
            self.version += 1

    # --- helpers (call with LOCK held) ----------------------------------------
    def _session_file(self):
        return config.library_root() / "session.json"

    def _save_session(self) -> None:
        # Plans stay in memory: a plan the player hasn't committed is worthless after a restart.
        config.write_json_atomic(self._session_file(), {
            "playing": self.playing, "played": sorted(self.played),
            "maps": {k: tm.points for k, tm in self.maps.items()},
            "entered": {k: asdict(t) for k, t in self.entered.items()},
            "beat_to_beat": self.beat_to_beat, "party": self.party, "kept": self.kept,
            # The active source (genre / mood / playlist; party above), so a restart doesn't reset
            # it to Any. pl_run: how far the playlist got.
            "genre": self.genre, "playlist": self.playlist, "pl_run": self.pl_run,
            "requested": self.requested})

    def _load_session(self) -> None:
        try:
            d = json.loads(self._session_file().read_text(encoding="utf-8"))
            ids = {s.id for s in self.pool}  # songs removed meanwhile are dropped
            playing = d.get("playing") if d.get("playing") in ids else None
            played = {i for i in d.get("played", []) if i in ids}
            maps = {k: TimeMap([tuple(p) for p in v]) for k, v in d.get("maps", {}).items() if k in ids}
            entered = {k: Transition(**v) for k, v in d.get("entered", {}).items() if k in ids}
        except FileNotFoundError:
            return
        except (ValueError, TypeError, AttributeError):
            return  # corrupt session.json: start a fresh session rather than crash
        self.playing, self.played, self.maps, self.entered = playing, played, maps, entered
        self.beat_to_beat = bool(d.get("beat_to_beat", False))
        self.party = d.get("party") if d.get("party") in party.FLAVORS else None
        # A genre or mood deleted meanwhile falls back to Any. Party wins if both were saved
        # (one source at a time; older session files had no genre at all).
        g = d.get("genre")
        self.genre = g if g in config.genres() and not self.party else config.ANY_GENRE
        pl = d.get("playlist")
        if pl in self.playlists and not self.party:
            self.playlist, self.genre = pl, config.ANY_GENRE
            run = d.get("pl_run")
            self.pl_run = {str(k): str(v) for k, v in run.items()} if isinstance(run, dict) else {}
        kept = d.get("kept")
        self.kept = {k: str(v) for k, v in kept.items() if k in ids} if isinstance(kept, dict) else {}
        req = d.get("requested")
        self.requested = [i for i in dict.fromkeys(req) if i in ids and i != playing] if isinstance(req, list) else []

    def _mark(self, thing: str, by: str | None) -> None:
        """Who last changed `thing`: an assistant (by="mcp") tags it, anyone else clears the tag."""
        if by == "mcp":
            self.by_mcp.add(thing)
        else:
            self.by_mcp.discard(thing)

    def by_id(self, sid: str) -> Song | None:
        return next((s for s in self.pool if s.id == sid), None)

    def _song(self, sid: str) -> Song:
        s = self.by_id(sid)
        if s is None:
            raise EngineError(404, f"No such song: {sid}")
        return s

    def _changed(self) -> None:
        self.version += 1
        self.worker.poke()

    def _eligible(self, s: Song) -> bool:
        return s.status == "ready" and s.analysis is not None and s.id not in self.played and s.id != self.playing

    def _recompute(self, keep: list[str] | None = None) -> None:
        """Rebuild `upcoming` = the locked-in rows, then the owner's kept songs (self.requested,
        their order), then auto picks from the active source only while fewer than MIN_UPCOMING
        ready songs are lined up. The first FIXED_NEXT of the old list stay put (the next
        transition may be planned already), unless `keep` overrides them (source switch)."""
        g, anyg = self.genre, config.ANY_GENRE
        cands = [s for s in self.pool if self._eligible(s)]
        pl = self._pl()
        pl_ready: list[Song] = []
        pl_waiting = False
        end_note = None
        if pl is not None:
            left = self._pl_left(pl)
            if not left and pl["at_end"] == "repeat" and self._pl_songs(pl):
                self.pl_run = {}  # Repeat: start again from #1 (the playing song plays on)
                left = self._pl_left(pl)
            pl_ready = [s for s in left if s.status == "ready" and s.analysis]
            # Not ready yet = skipped for now, never waited for (they join once ready).
            pl_waiting = len(pl_ready) < len(left)
            in_src, what = pl_ready, pl["name"]
            end_note = self._pl_end_note(pl, left)
        elif self.party:
            # Party flavor replaces the genre filter; songs not looked up on Last.fm yet don't fit.
            in_src = [s for s in cands if party.fits(s, self.party)]
            what = f"{self.party} party"
        elif g in self.moods:
            members = set(self.moods[g])
            in_src = [s for s in cands if s.id in members or s.effective_genre() == g]
            what = g
        else:
            in_src = [s for s in cands if g == anyg or s.effective_genre() == g]
            what = g
        # Review Focus 3: the source ran dry -> auto picks come from any genre (with a notice),
        # never go silent. A playlist that simply ended is handled by its at_end, not here.
        fell_back = bool(cands) and not in_src and (pl is None or pl_waiting)
        self.notice = end_note or (f"No ready {what} songs yet — playing from any genre" if fell_back else None)
        # order() filters by genre itself; for a party or mood the candidates arrive pre-filtered.
        prefiltered = bool(self.party) or g in self.moods
        use_genre = anyg if (fell_back or prefiltered) else g
        src = cands if fell_back else in_src
        cur = self.by_id(self.playing) if self.playing else None
        # A playlist song played earlier tonight is still playable in the playlist.
        ok = {s.id for s in cands} | {s.id for s in pl_ready}
        if keep is None:
            keep = self.upcoming[: config.FIXED_NEXT] if cur else []
        if self.next_pick in ok:
            # The play_next pick is playable: it takes the idle deck, and what was planned there
            # stays locked right behind it (back at the top of Up next, not lost). The player
            # sees the next song change and replans the mix (shouldReplan); a mix already under
            # way never replans, and the server's `playing` is its incoming song by then, so
            # the pick still lands after it.
            keep = [self.next_pick, *keep]
            self.next_pick = None
        locked = [i for i in dict.fromkeys(keep) if i in ok]
        # A locked auto pick whose hand-over the owner marked avoided loses its slot (and so does
        # everything after it), so a fresh 👎 takes effect right away. Kept songs always stay.
        for j, sid in enumerate(locked):
            before = locked[j - 1] if j else (cur.id if cur else None)
            if before and sid not in self.requested and avoids(self.pairs, before, sid):
                locked = locked[:j]
                break
        head = locked + [i for i in self.requested if i in ok and i not in locked]
        auto: list[str] = []
        self.moved = {}
        need = config.MIN_UPCOMING - len(head)
        if need > 0:
            prev = self.by_id(head[-1]) if head else cur
            if pl is not None:
                auto = self._pl_auto(pl, prev, head, pl_ready, pl_waiting, cands, need)
            else:
                rest = [s for s in src if s.id not in head]
                moved: dict = {}
                # Nothing playing and nothing kept: no "previous song" to score against, pool order.
                ranked = order(prev, rest, {}, use_genre, bonus=party.bonus if self.party else None,
                               pairs=self.pairs, moved=moved) if prev else rest
                auto = [s.id for s in ranked[:need]]
                ids = [s.id for s in ranked]
                self.moved = {sid: (after, i, ids.index(sid)) for sid, (after, i) in moved.items() if sid in ids}
        self.upcoming = head + auto

    def _pl_auto(self, pl: dict, prev: Song | None, head: list[str], pl_ready: list[Song], waiting: bool,
                 cands: list[Song], need: int) -> list[str]:
        """Auto picks while a playlist plays: its next ready songs in playlist order (or the
        planner's mix order), then, when it runs short, whatever its at_end says."""
        rest = [s for s in pl_ready if s.id not in head]
        if pl["order"] == "mix" and prev is not None:
            rest = order(prev, rest, {}, config.ANY_GENRE, pairs=self.pairs)
        auto = [s.id for s in rest[:need]]
        more = need - len(auto)
        if more <= 0:
            return auto
        taken = set(head) | set(auto) | {self.playing}
        if pl["at_end"] == "repeat" and not waiting:
            # Round again from #1 (played songs included: that's the point of Repeat).
            wrap = [s for s in self._pl_songs(pl) if s.id not in taken and s.status == "ready" and s.analysis]
            return auto + [s.id for s in wrap[:more]]
        if waiting or pl["at_end"] == "keep_going":
            # Songs still downloading (never waited for), or the end with "Keep going": similar
            # library songs, seeded by the last playlist song so the hand-over stays smooth.
            tail = self.by_id(auto[-1]) if auto else prev
            lib = [s for s in cands if s.id not in taken]
            ranked = order(tail, lib, {}, config.ANY_GENRE, pairs=self.pairs) if tail else lib
            return auto + [s.id for s in ranked[:more]]
        return auto  # "stop": Up next simply ends after the last song

    def view(self, s: Song) -> dict:
        an = s.analysis
        return {"id": s.id, "title": s.title, "channel": s.channel, "status": s.status, "error": s.error,
                "genre": s.effective_genre(), "bpm": an.bpm if an else None, "camelot": an.camelot if an else None,
                "danceability": (s.meta or {}).get("danceability")}

    def _queued(self, s: Song) -> dict:
        """An Up next row: the song, whether the owner kept it (else it's an auto pick that keeps
        the music going), and where it was kept from (None = no note, e.g. dragged into place)."""
        pl = self._pl()
        # in_playlist: an auto pick from the playing playlist (else a library song filling in around it).
        return {**self.view(s), "kept": s.id in self.requested, "kept_from": self.kept.get(s.id),
                "in_playlist": pl is not None and s.id in pl["song_ids"],
                "by": "mcp" if f"song:{s.id}" in self.by_mcp else None}

    # --- API operations ---------------------------------------------------------
    def state(self) -> dict:
        with LOCK:
            by = {s.id: s for s in self.pool}  # one pass; the pool can be a whole Liked Music import
            counts: dict[str, int] = {}
            for s in self.pool:
                counts[s.status] = counts.get(s.status, 0) + 1
            src = self._source()
            if src is not None:
                src["by"] = "mcp" if "source" in self.by_mcp else None
            wanted = self.stems_wanted()
            # Up next rows and the ON AIR deck's Voice/Beat switch: id -> stems status (_stems_status).
            stems_by = {i: self._stems_status(i, wanted) for i in wanted}
            return {"version": self.version, "genre": self.genre, "genres": config.genres(),
                    "notice": self.notice, "playing": self.playing, "beat_to_beat": self.beat_to_beat,
                    "party": self.party, "flavors": list(party.FLAVORS), "lastfm": bool(lastfm.api_key()),
                    "next_effect": self.next_effect, "effects": list(FORCEABLE), "mix_now_seq": self.mix_now_seq,
                    "moods": {k: len(v) for k, v in self.moods.items()},
                    "source": src,
                    "next_effect_by": "mcp" if "effect" in self.by_mcp else None,
                    "mcp": self.mcp.state(),
                    # Saved playlists, newest first, and the active one (or None).
                    "playlists": [self._pl_summary(p, by) for p in reversed(self.playlists.values())],
                    "playlist": self._pl_summary(self._pl(), by) if self._pl() else None,
                    "queue": [self._queued(by[i]) for i in self.upcoming if i in by],
                    # What the queue panel marks: the first `queue_locked` songs keep their order
                    # (_recompute's FIXED_NEXT, only while something plays); `requested` = kept
                    # songs still downloading/analysing (they join `queue` once ready).
                    "queue_locked": config.FIXED_NEXT if self.playing else 0,
                    "requested": [self._queued(by[i]) for i in self.requested if i in by and i not in self.upcoming],
                    "pairs": dict(self.pairs),
                    "moved": self._moved_notes(by),
                    # Whole pool too: the UI shows per-song status/errors and lets the owner
                    # relabel or delete songs that aren't (or can't be) in the queue.
                    "pool": [self.view(s) for s in self.pool],
                    "pool_counts": counts,
                    # The beat-match limit, so the web UI's beat-match icon uses the planner's
                    # number instead of a copy that could drift (music.tempo_relation).
                    "beatmatch_max_pct": config.BEATMATCH_MAX_PCT,
                    "stems": stems_by}

    def add(self, url: str, genre: str | None) -> int:
        _known_genre(genre, allow_none=True)
        try:
            # Network call, deliberately outside the lock; a bad URL raises ValueError first.
            songs = fetch.expand(url)
        except (ValueError, fetch.FetchError) as e:
            raise EngineError(400, str(e)) from e
        with LOCK:
            added, retried = self._import_songs(songs, genre)
        return added + retried  # the UI's "Added N songs": a retry is (re)queued too

    def _import_songs(self, songs: list[Song], genre: str | None) -> tuple[int, int]:
        """Put fetched songs in the library pool (call with LOCK held). (new, retried)."""
        added = retried = 0
        have = {s.id: s for s in self.pool}
        for new in songs:
            if new.id in have:
                # Re-adding is the owner's only retry: the worker never touches a failed
                # song, so put it back in the queue (in place: the worker holds it).
                old = have[new.id]
                if old.status == "failed":
                    old.status, old.error = "queued", None
                    library.save_song(old)
                    retried += 1
                continue
            # A song removed earlier keeps its cache on disk; reuse it, because saving
            # the fresh (analysis-less) record would delete analysis.json and the
            # owner's genre fix. A reused failed song gets a fresh try, same as above.
            s = library.load_song(new.id) or new
            if s.status == "failed":
                s.status, s.error = "queued", None
            if genre:
                s.genre_hint = genre
            library.save_song(s)
            self.pool.append(s)
            have[s.id] = s
            added += 1
        if added or retried:  # a retried song changed status: wake the worker
            library.save_pool([s.id for s in self.pool])
            self._recompute()
            self._changed()
        return added, retried

    def set_playing(self, sid: str, plan_id: str | None = None) -> None:
        with LOCK:
            self._song(sid)
            plan = self.plans.get(plan_id) if plan_id else None
            if plan and plan[0] == sid:
                self.maps[sid], self.entered[sid] = plan[1], plan[2]
            # No / unknown plan_id (first song, or a plan pruned long ago): maps stay as they
            # are, i.e. identity via deck() for a fresh song.
            if sid in self.requested:
                self.requested.remove(sid)
            self.kept.pop(sid, None)  # "kept from …" is an Up next note; it has played now
            self.by_mcp.discard(f"song:{sid}")  # so is "via MCP"
            if sid != self.playing:  # a pulled-back fader re-reports the same song: one entry
                self.next_effect = None  # the forced effect was for the previous song's exit
                self.by_mcp.discard("effect")
                self._log_played(sid, plan[2] if plan and plan[0] == sid else None)
            prev, self.playing = self.playing, sid
            self.played = self.played | {sid}  # new set: get_state copies never see a resize
            pl = self._pl()
            if pl is not None and sid != prev:
                if sid in pl["song_ids"]:
                    self.pl_run[sid] = time.strftime("%H:%M")
                elif pl["at_end"] == "keep_going" and not self._pl_left(pl):
                    # The playlist is over and a library song took over ("Keep going"): the
                    # source hands over to Vibe: Any, seeded by what's playing.
                    self.playlist, self.pl_run = None, {}
                    self.by_mcp.discard("source")
            self._save_session()
        if prev and prev != sid:
            # The song that just handed over is decoded in the browser already, and its renders
            # (~40 MB WAVs each) are never served again: delete them, or they pile up (1.3 GB in
            # one evening). Outside the lock: plain file deletes. A replay re-renders on demand.
            for r in library.song_dir(prev).glob("render-*.wav"):
                r.unlink(missing_ok=True)
        with LOCK:
            self._recompute()
            self._changed()

    def search(self, q: str) -> list[dict]:
        if not q.strip():
            raise EngineError(400, "Type something to search for")
        try:
            hits = fetch.search(q.strip())  # network, outside the lock
        except fetch.FetchError as e:
            raise EngineError(400, str(e)) from e
        with LOCK:
            by = {s.id: s for s in self.pool}
            # status tells the UI whether the song is already in the library (and ready).
            return [{"id": h.id, "title": h.title, "channel": h.channel, "duration": h.duration,
                     "status": by[h.id].status if h.id in by else None} for h in hits]

    def _named(self, sid: str, title: str, channel: str, duration: float) -> tuple[str, str, float]:
        """(title, channel, duration) for a song about to be added. The assistant's play_next can
        send a bare video id; added as is, the song had no name anywhere (Up next, the decks) and
        analysis had no artist to go on. So a new song with no title is looked up on YouTube
        first. Outside LOCK: it's a network call."""
        if title or self.by_id(sid) or library.load_song(sid):
            return title, channel, duration
        try:
            songs = fetch.expand(f"https://www.youtube.com/watch?v={sid}")
        except fetch.FetchError as e:
            raise EngineError(404, f"Couldn't find video {sid} on YouTube: {e}") from e
        if not songs:
            raise EngineError(404, f"Couldn't find video {sid} on YouTube")
        return songs[0].title, songs[0].channel, songs[0].duration

    def request(self, sid: str, title: str, channel: str, duration: float, kept_from: str | None = None,
                first: bool = False, by: str | None = None) -> None:
        """Owner kept a song (the draft, the assistant, a pair): add it if new, prep it first, and
        put it in Up next after the songs already kept (`first`: before them, see play_next).
        `kept_from` is the note ("Balkan party", "search", "assistant")."""
        title, channel, duration = self._named(sid, title, channel, duration)
        with LOCK:
            s = self.by_id(sid)
            if s is None:
                # Same reuse rule as add(): a removed song's cache (analysis, owner genre) survives.
                s = library.load_song(sid) or Song(sid, title, channel, float(duration or 0.0), "queued")
                self.pool.append(s)
                library.save_pool([x.id for x in self.pool])
            if s.status == "failed":  # picking it again is a retry
                s.status, s.error = "queued", None
            library.save_song(s)
            if first:
                self.requested = [sid] + [i for i in self.requested if i != sid]
                self.next_pick = sid
            elif sid not in self.requested:
                self.requested.append(sid)
            if kept_from:
                self.kept[sid] = kept_from
            self._mark(f"song:{sid}", by)
            # Asking for a song that already played tonight means "play it again": without this
            # it isn't eligible, so it sat in the requested list forever ("plays next once ready").
            self.played = self.played - {sid}  # new set: get_state copies never see a resize
            self._recompute()
            self._save_session()
            self._changed()

    def play_next(self, sid: str, title: str = "", channel: str = "", duration: float = 0.0,
                  kept_from: str | None = None, by: str | None = None) -> dict:
        """The very next song, on the idle deck (design yA49d). A ready pick goes first and the
        planned song moves back to second; one still downloading/analysing waits at the top of
        the requests while the planned song stays, then swaps in once ready (_recompute). The
        playing song is never touched. Returns what the UI's one-line notice needs."""
        title, channel, duration = self._named(sid, title, channel, duration)  # before LOCK: network
        with LOCK:
            if sid == self.playing:
                raise EngineError(409, "That song is playing now")
            planned = self.upcoming[0] if self.upcoming else None
            self.request(sid, title, channel, duration, kept_from, first=True, by=by)
            # The version bump (request's _changed) is what makes a player holding a plan for
            # `planned` fetch a new one; the old plan_ids stay valid on purpose: if that mix is
            # already audible the player keeps it, and /api/playing must still commit its map.
            return {"id": sid, "ready": bool(self.upcoming) and self.upcoming[0] == sid,
                    "planned": planned if planned != sid else None}

    # --- played log ------------------------------------------------------------------
    def _history_file(self):
        return config.library_root() / "history.jsonl"

    def _log_played(self, sid: str, came_in: Transition | None) -> None:
        """Append one line per song that started playing (owner asked for a log). JSON Lines:
        append-only, survives restarts, a crash loses at most the line being written."""
        s = self.by_id(sid)
        # "from": the song it was mixed in from, so a Played row can rate that hand-over. A song
        # started from scratch (Play, resume, recovery) has no hand-over: None.
        line = {"at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "id": sid, "title": s.title, "channel": s.channel,
                "from": self.playing if came_in else None,
                "bpm": round(s.analysis.bpm, 1) if s.analysis else None, "genre": s.effective_genre(),
                "effect": came_in.kind if came_in else None, "party": self.party}
        with open(self._history_file(), "a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")

    def history(self, limit: int = 200) -> list[dict]:
        """Most recent first. Each entry carries `from` (the song it was mixed in from, or None),
        `verdict` (this session's 🔥/👎 on that hand-over, which a second tap undoes; None when not
        rated since the app started, so an old 🔥 never looks pressed and a tap adds one more) and
        `why` (the 👎's answer to "What was off?", None until answered)."""
        try:
            lines = self._history_file().read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        rows = []
        for ln in lines[-(limit + 1):]:  # one extra: the oldest shown row's predecessor
            try:
                rows.append(json.loads(ln))
            except ValueError:
                continue  # a half-written last line after a crash
        with LOCK:
            pairs, undo = dict(self.pairs), dict(self.pair_undo)
        for prev, h in zip([None, *rows], rows):
            if "from" not in h:
                # Lines written before "from" existed: a song that came in via an effect was
                # mixed in from the line before it (the log is in play order).
                h["from"] = prev["id"] if prev and h.get("effect") else None
            k = pair_key(h["from"], h["id"]) if h["from"] else None
            h["verdict"] = undo[k][0] if k in undo else None
            h["why"] = (pairs.get(k) or {}).get("why") if h["verdict"] == "avoided" else None
        return list(reversed(rows[-limit:]))

    # --- pair memory --------------------------------------------------------------------
    def _pairs_file(self):
        return config.library_root() / "pairs.json"

    def _load_pairs(self) -> dict[str, dict]:
        try:
            d = json.loads(self._pairs_file().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(d, dict):
            return {}
        out = {str(k): migrate_pair(v) for k, v in d.items()}
        return {k: v for k, v in out.items() if v is not None}

    def _pairs_saved(self) -> None:
        """Every change is written at once (atomic): a crash or restart loses no rating. Then the
        queue is re-ordered with it."""
        config.write_json_atomic(self._pairs_file(), self.pairs)
        self._recompute()
        self._changed()

    def rate_pair(self, a: str, b: str, verdict: str, effect: str | None = None, why: str | None = None) -> None:
        """Owner's 🔥 (loved) / 👎 (avoided) on the hand-over a -> b, mixed with `effect` (the
        Played row's / the sign's; None = unknown). `why` answers "What was off?" right away
        (the MCP's avoid_pair); see rate_entry for what a rating does."""
        if verdict not in ("loved", "avoided"):
            raise EngineError(400, f"Unknown verdict: {verdict}")
        if why is not None and (verdict != "avoided" or why not in PAIR_WHY):
            raise EngineError(400, f"Unknown reason: {why}")
        if a == b:
            raise EngineError(400, "A pair needs two different songs")
        k = pair_key(a, b)
        with LOCK:
            prev = self.pairs.get(k)
            self.pairs[k] = {**rate_entry(prev, verdict, pair_effect(effect),
                                          time.strftime("%Y-%m-%dT%H:%M:%S%z")), "why": why}
            # Undo restores the entry from before THIS rating ("tapping again undoes your last").
            self.pair_undo[k] = (verdict, prev)
            self._pairs_saved()

    def pair_why(self, a: str, b: str, why: str) -> None:
        """The optional "What was off?" after a 👎: "songs" bans the pair, "effect" only its effect."""
        if why not in PAIR_WHY:
            raise EngineError(400, f"Unknown reason: {why}")
        with LOCK:
            p = self.pairs.get(pair_key(a, b))
            if not p or not p["avoided"]:
                raise EngineError(409, "Only a pair you 👎 has a reason")
            p["why"] = why
            self._pairs_saved()

    def undo_pair(self, a: str, b: str) -> None:
        """Tapping the pressed 🔥/👎 again: the pair goes back to what it was before that rating."""
        k = pair_key(a, b)
        with LOCK:
            if k not in self.pair_undo:
                raise EngineError(409, "Nothing to undo for that pair")
            _, prev = self.pair_undo.pop(k)
            if prev is None:
                self.pairs.pop(k, None)
            else:
                self.pairs[k] = prev
            self._pairs_saved()

    def forget_pair(self, a: str, b: str) -> None:
        """Pairs tab ✕ / MCP forget_pair: drop the memory of a -> b (b -> a is its own pair)."""
        k = pair_key(a, b)
        with LOCK:
            if self.pairs.pop(k, None) is None:
                raise EngineError(404, "No memory of that pair")
            self.pair_undo.pop(k, None)
            self._pairs_saved()

    def play_pair(self, a: str, b: str) -> None:
        """Pairs tab "Play pair": a then b are the very next songs, in that order. Reuses
        play_next (b first, then a in front of it); a song that's playing now is skipped, so
        "Play pair" on the playing song's pair just puts its partner next."""
        with LOCK:
            sa, sb = self.by_id(a) or library.load_song(a), self.by_id(b) or library.load_song(b)
            if sa is None or sb is None:
                raise EngineError(404, "That pair's songs aren't in the library any more")
            for s in (sb, sa):
                if s.id != self.playing:
                    self.play_next(s.id, s.title, s.channel, s.duration, kept_from="a pair you loved")

    def _moved_notes(self, by: dict[str, Song]) -> list[dict]:
        """Up next's "X moved 3 later: you 👎 it right after Y" (after = Y's id, by = places)."""
        return [{"id": sid, "title": by[sid].title, "after": after, "after_title": by[after].title if after in by else after,
                 "by": got - want} for sid, (after, want, got) in self.moved.items() if got > want and sid in by]

    def pairs_list(self) -> list[dict]:
        """Every rated pair with titles (songs no longer in the pool: from their cache), most
        loved first, then avoided ones; most recently rated first within each."""
        with LOCK:
            items = [(k, dict(v)) for k, v in self.pairs.items()]
            by = {s.id: s for s in self.pool}

        def title(sid: str) -> str:
            s = by.get(sid) or library.load_song(sid)
            return s.title if s else sid

        out = []
        for k, v in items:
            a, _, b = k.partition(">")
            out.append({"from": a, "to": b, "from_title": title(a), "to_title": title(b), **v})
        out.sort(key=lambda p: p["last"] or "", reverse=True)
        return sorted(out, key=lambda p: (p["avoided"], -p["loved"]))

    # --- moods ----------------------------------------------------------------------
    def _moods_file(self):
        return config.library_root() / "moods.json"

    def _load_moods(self) -> dict[str, list[str]]:
        try:
            d = json.loads(self._moods_file().read_text(encoding="utf-8"))
            return {str(k): [str(i) for i in v] for k, v in d.items()} if isinstance(d, dict) else {}
        except (OSError, ValueError, TypeError, AttributeError):
            return {}

    def _load_json_dict(self, name: str) -> dict:
        try:
            d = json.loads((config.library_root() / name).read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def build_mood(self, theme: str, n: int = 30, by: str | None = None) -> dict:
        """Collect the crowd's picks for a theme from public playlists, import them, and switch
        to the mood. Returns {"mood", "found", "added", "songs": [{title, votes}]}."""
        theme = " ".join(theme.split())[:60]
        if not theme:
            raise EngineError(400, "Type a mood, e.g. 'Balkan party'")
        name = theme[:1].upper() + theme[1:]
        if name == config.ANY_GENRE:
            raise EngineError(400, f"'{name}' is reserved")
        top, of = moods.collect(theme, max(5, min(n, 60)))  # network: seconds, outside the lock
        if not top:
            raise EngineError(404, f"No playlists found for '{theme}'")
        added = 0
        with LOCK:
            have = {s.id: s for s in self.pool}
            ids = []
            for new, _votes in top:
                s = have.get(new.id) or library.load_song(new.id)
                if s is None or s.id not in have:
                    s = s or new
                    if s.genre is None and s.genre_hint is None:
                        s.genre_hint = name  # lets the worker fetch mood songs first
                    if s.status == "failed":
                        s.status, s.error = "queued", None
                    library.save_song(s)
                    self.pool.append(s)
                    have[s.id] = s
                    added += 1
                ids.append(s.id)
            # Building an existing mood again refreshes it: new picks are added, none removed
            # (the owner may be relying on songs already in it).
            self.moods[name] = list(dict.fromkeys([*self.moods.get(name, []), *ids]))
            config.write_json_atomic(self._moods_file(), self.moods)
            # Votes rank the mood's draft. A refresh keeps each song's best count so far.
            mv = self.mood_votes.setdefault(name, {"of": 0, "votes": {}})
            mv["of"] = max(int(mv.get("of") or 0), of)
            for sid, (_s, v) in zip(ids, top):
                mv["votes"][sid] = max(int(mv["votes"].get(sid) or 0), v)
            config.write_json_atomic(config.library_root() / "mood-votes.json", self.mood_votes)
            if name not in config.genres():
                config.save_genres([*config.genres(), name])
            library.save_pool([s.id for s in self.pool])
        self.set_genre(name, by)  # switch to it now; also replans the next transition
        return {"mood": name, "found": len(top), "added": added,
                "songs": [{"title": s.title, "votes": v} for s, v in top[:10]]}

    def delete_mood(self, name: str) -> None:
        """Forget a mood (songs stay in the library). Back to Any if it was active."""
        with LOCK:
            if name not in self.moods:
                raise EngineError(404, f"No mood called {name}")
            del self.moods[name]
            config.write_json_atomic(self._moods_file(), self.moods)
            if self.mood_votes.pop(name, None) is not None:
                config.write_json_atomic(config.library_root() / "mood-votes.json", self.mood_votes)
            config.save_genres([g for g in config.genres() if g != name] or list(config.DEFAULT_GENRES))
            if self.genre == name:
                self.genre = config.ANY_GENRE
                self.by_mcp.discard("source")
            self._recompute()
            self._changed()

    # --- party mode ------------------------------------------------------------------
    def _rejected_file(self):
        return config.library_root() / "rejected.json"

    def _load_rejected(self) -> dict[str, set[str]]:
        try:
            d = json.loads(self._rejected_file().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if isinstance(d, list):
            # Before per-source exclusions it was one flat list, and only Party could reject:
            # it becomes the bucket every party flavor reads, so nothing comes back. Written in
            # the new shape right away, so the migration runs once.
            out = {PARTY_BUCKET: {str(k) for k in d}}
            self._save_rejected(out)
            return out
        if not isinstance(d, dict):
            return {}
        return {str(k): {str(x) for x in v} for k, v in d.items() if isinstance(v, list)}

    def _save_rejected(self, rejected: dict[str, set[str]] | None = None) -> None:
        r = self.rejected if rejected is None else rejected
        config.write_json_atomic(self._rejected_file(), {k: sorted(v) for k, v in r.items() if v})

    def _source(self) -> dict | None:
        """Where the next songs come from: one of party / mood / genre, or None (Any, party off).
        `key` names its exclusions in rejected.json; `label` is what the owner reads. A playlist's
        exclusions live in its own playlists.json entry instead."""
        pl = self._pl()
        if pl is not None:
            return {"kind": "playlist", "name": pl["name"], "key": f"playlist:{pl['list_id']}",
                    "label": pl["name"], "id": pl["list_id"]}
        if self.party:
            name = self.party.title()
            return {"kind": "party", "name": self.party, "key": f"party:{name}", "label": name}
        if self.genre == config.ANY_GENRE:
            return None
        kind = "mood" if self.genre in self.moods else "genre"
        return {"kind": kind, "name": self.genre, "key": f"{kind}:{self.genre}", "label": self.genre}

    def _excluded(self, key: str) -> set[str]:
        out = set(self.rejected.get(key, ()))
        if key.startswith("party:"):
            out |= self.rejected.get(PARTY_BUCKET, set())
        return out

    def set_party(self, flavor: str | None, by: str | None = None) -> None:
        if flavor is not None and flavor not in party.FLAVORS:
            raise EngineError(400, f"Unknown party flavor: {flavor}")
        with LOCK:
            self.party = flavor
            self._mark("source", by)
            if flavor:
                # One source at a time: a party flavor resets the vibe to Any and the playlist to
                # None. Off doesn't bring the old vibe back (owner's rule).
                self.genre = config.ANY_GENRE
                self.playlist, self.pl_run = None, {}
            self._sugg = None
            cur = self.by_id(self.playing) if self.playing else None
            keep = None
            if cur is not None and flavor:
                # Like a genre switch: the best-fitting ready song becomes next, so it's audible soon.
                fit = [s for s in self.pool if self._eligible(s) and party.fits(s, flavor)]
                if fit:
                    keep = [max(fit, key=lambda s: score_pair(cur, s) + party.bonus(cur, s)).id]
            self._save_session()
            self._recompute(keep)
            self._changed()

    def _library_draft(self, src: dict) -> list[dict]:
        """Draft for a mood or genre (call with LOCK held): library songs of that source that
        aren't in Up next (queued, requested or playing) or excluded for it. A mood's are its
        members ranked by playlist votes; a genre's are its ready songs."""
        # Up next and tonight's played songs are never drafted (played ones belong in Played).
        skip = set(self.upcoming) | set(self.requested) | {self.playing} | self.played
        ex = self._excluded(src["key"])
        name = src["name"]
        if src["kind"] == "mood":
            mv = self.mood_votes.get(name) or {}
            votes, of = mv.get("votes") or {}, mv.get("of")
            by = {s.id: s for s in self.pool}
            # sorted() is stable: equal votes keep the mood's own (first-seen) order.
            songs = [by[i] for i in sorted((i for i in self.moods.get(name, []) if i in by),
                                           key=lambda i: -int(votes.get(i) or 0))]
        else:
            # Ready songs ranked by how well they mix from the end of Up next, then the ones
            # still being prepared (✓ on those requests them: prepped first, played once ready).
            mine = [s for s in self.pool if s.status != "failed" and s.effective_genre() == name]
            last = self.by_id((self.upcoming or [self.playing])[-1]) if (self.upcoming or self.playing) else None
            ready = [s for s in mine if s.status == "ready" and s.analysis]
            if last is not None and last.analysis:
                ready.sort(key=lambda s: -score_pair(last, s))
            songs = ready + [s for s in mine if not (s.status == "ready" and s.analysis)]
        out = []
        for s in songs:
            if s.id in skip:
                continue
            a, t = party.artist_title(s)
            if party.key(a, t) in ex:
                continue
            if src["kind"] == "mood":
                v = votes.get(s.id)
                why = f"in {v} of {of} playlists" if v and of else f"from the {name} playlists"
            else:
                why = s.effective_genre() if s.status == "ready" else f"{s.effective_genre()} · preparing"
            out.append({"id": s.id, "artist": a, "title": t, "why": why, "status": s.status,
                        "bpm": s.analysis.bpm if s.analysis else None})
            if len(out) >= DRAFT_MAX:
                break
        return out

    def draft(self) -> dict:
        """The Suggestions tab: {"source", "suggestions"} for the active source. Party drafts
        songs you don't have (Last.fm, cached 5 min per playing song); moods and genres draft
        from the library (cheap, never cached). No source (Any, party off) = no draft."""
        with LOCK:
            src = self._source()
            if src is None:
                return {"source": None, "suggestions": []}
            if src["kind"] == "playlist":
                pl = self._pl()
                return {"source": src, "playlist": self._pl_summary(pl, {s.id: s for s in self.pool}),
                        "suggestions": self._playlist_draft(pl)}
            if src["kind"] != "party":
                return {"source": src, "suggestions": self._library_draft(src)}
            k = (self.playing, src["key"])
            if self._sugg and self._sugg[0] == k and time.time() - self._sugg[1] < 300:
                return {"source": src, "suggestions": self._sugg[2]}
            cur = self.by_id(self.playing) if self.playing else None
            pool, rejected = list(self.pool), self._excluded(src["key"])
        res = party.suggestions(cur, self.party, pool, rejected)  # network: outside the lock
        for x in res:
            # Not in the library (that's the point): no id until accepted and found on YouTube.
            x.setdefault("id", None)
            x.setdefault("why", x.get("reason"))
        with LOCK:
            self._sugg = (k, time.time(), res)
        return {"source": src, "suggestions": res}

    def suggestions(self) -> list[dict]:
        return self.draft()["suggestions"]

    def accept(self, artist: str, title: str, sid: str | None = None, by: str | None = None) -> None:
        """Owner kept a draft song: it plays next (request()), noted as kept from the source.
        With an id (a library song) that's all; without one (a party pick) it's found on
        YouTube first."""
        if sid is None:
            try:
                hits = fetch.search(f"{artist} {title}", 3)
            except fetch.FetchError as e:
                raise EngineError(400, str(e)) from e
            if not hits:
                raise EngineError(404, f"Couldn't find {artist} – {title} on YouTube")
            h = hits[0]
            sid, title, channel, duration = h.id, h.title, h.channel, h.duration
        else:
            channel, duration = "", 0.0  # request() only uses these for a song not in the pool
        with LOCK:
            src = self._source()
            self._sugg = None
        self.request(sid, title, channel, duration, src["label"] if src else None, by=by)

    def accept_all(self, items: list[dict]) -> None:
        """"Add all N": keep each in the order shown. request() appends, so Up next keeps it."""
        for x in items:
            self.accept(x.get("artist", ""), x.get("title", ""), x.get("id"))

    def _reject_key(self, source: str | None) -> str:
        if source is None:
            with LOCK:
                src = self._source()
            if src is None:
                raise EngineError(409, "Pick a mood, genre or party first")
            if src["kind"] == "playlist":
                raise EngineError(409, "A playlist skips songs by id: use the playlist's exclude")
            return src["key"]
        if not re.fullmatch(r"(mood|genre|party):.{1,80}", source):
            raise EngineError(400, f"Unknown source: {source}")
        return source

    def reject(self, artist: str, title: str, source: str | None = None) -> None:
        """✕: never suggest this song for that source again (default: the active one). The UI
        passes the source it drafted from, so an Undo after a switch still hits the right one."""
        key, k = self._reject_key(source), party.key(artist, title)
        with LOCK:
            self.rejected.setdefault(key, set()).add(k)
            self._save_rejected()
            if self._sugg and self._sugg[0][1] == key:  # drop it from the cached list right away
                c, t, lst = self._sugg
                self._sugg = (c, t, [x for x in lst if party.key(x["artist"], x["title"]) != k])

    def unreject(self, artist: str, title: str, source: str | None = None) -> None:
        """Undo a ✕ (the few seconds the row shows Undo)."""
        key, k = self._reject_key(source), party.key(artist, title)
        with LOCK:
            self.rejected.get(key, set()).discard(k)
            if key.startswith("party:"):
                self.rejected.get(PARTY_BUCKET, set()).discard(k)
            self._save_rejected()
            self._sugg = None

    def enrich_once(self) -> bool:
        """Fill one song's party facts: Last.fm first (ready songs first), then danceability
        measured from the audio of ready songs. False when nothing is left to do."""
        with LOCK:
            todo = sorted((s for s in self.pool if "listeners" not in (s.meta or {}) and s.status != "failed"),
                          key=lambda s: s.status != "ready") if lastfm.api_key() else []
            s = todo[0] if todo else None
            if s is None:
                dance = next((x for x in self.pool if x.status == "ready"
                              and "danceability" not in (x.meta or {})), None)
        if s is None:
            if dance is None:
                return False
            from .analyze import danceability
            try:
                d = danceability(library.audio_path(dance.id))  # ~1-2 s of CPU, outside the lock
            except Exception:
                logging.getLogger(__name__).exception("danceability failed for %s", dance.id)
                d = None
            with LOCK:
                if any(x is dance for x in self.pool):
                    dance.meta = {**(dance.meta or {}), "danceability": d}
                    library.save_song(dance)
            return True
        a, t = lastfm.split_title(s.title, s.channel)
        info = lastfm.track_info(a, t)  # network: outside the lock
        tags = list(dict.fromkeys(info.get("tags", []) + lastfm.artist_tags(a)))
        with LOCK:
            if any(x is s for x in self.pool):
                s.meta = {**(s.meta or {}), "artist": a, "title": t, "listeners": info.get("listeners", 0), "tags": tags}
                library.save_song(s)
                if self.party and s.status == "ready":
                    self._recompute()
                    self.version += 1
        return True

    def start_enrichment(self) -> None:
        """Background thread filling Song.meta from Last.fm, one song at a time.
        ponytail: one slow thread (rate-limited in lastfm); a 500-song library takes ~10 min."""
        def run():
            while not self._enrich_stop.is_set():
                try:
                    busy = self.enrich_once()
                except Exception:
                    logging.getLogger(__name__).exception("last.fm enrichment failed")
                    busy = False
                self._enrich_stop.wait(0.2 if busy else 60)
        threading.Thread(target=run, daemon=True, name="autodeck-lastfm").start()

    def stop_enrichment(self) -> None:
        self._enrich_stop.set()

    def set_next_effect(self, effect: str | None, by: str | None = None) -> None:
        if effect is not None and effect not in FORCEABLE:
            raise EngineError(400, f"Unknown transition effect: {effect}")
        with LOCK:
            self.next_effect = effect
            self._mark("effect", by)
            self._changed()  # the player replans the not-yet-started mix with it

    def mix_now(self) -> None:
        with LOCK:
            if not self.playing:
                raise EngineError(409, "Nothing is playing")
            self.mix_now_seq += 1
            self._changed()

    # --- settings -------------------------------------------------------------------
    def settings(self) -> dict:
        """Never returns the key itself, only whether one is set and its last 4 characters."""
        key = lastfm.api_key()
        # Where the library lives, for the Settings note; home shortened to ~ (it's shown, not used).
        lib = str(config.library_root()).replace(str(Path.home()), "~", 1)
        return {"lastfm": {"set": bool(key), "hint": key[-4:] if key else None, "source": lastfm.key_source()},
                "library": lib,
                # from_env: AUTODECK_BROWSER overrides the saved choice, so the page says so.
                "youtube": {"browser": config.youtube_browser(), "from_env": bool(os.environ.get("AUTODECK_BROWSER"))},
                "runtime": standalone.status()}

    def repair_install(self) -> dict:
        """Settings -> Repair installation (standalone app only): flag it, then relaunch the
        app, whose setup reinstalls the packages and re-checks the models."""
        if not standalone.ACTIVE:
            raise EngineError(409, "Repair is for the standalone app; in the repo run `uv sync`")
        standalone.request_repair()
        return {"relaunch": True}

    def set_youtube_browser(self, browser: str) -> dict:
        if browser not in config.BROWSERS:
            raise EngineError(400, "Pick one of: " + ", ".join(config.BROWSERS.values()))
        config.save_setting("youtube_browser", browser)  # fetch reads it on every call: no restart
        return self.settings()

    def set_lastfm_key(self, key: str | None) -> dict:
        key = (key or "").strip()
        if key and not re.fullmatch(r"[0-9a-fA-F]{32}", key):
            raise EngineError(400, "A Last.fm API key is 32 letters and digits (0-9, a-f)")
        if key and not lastfm.check_key(key):  # network, outside the lock
            raise EngineError(400, "Last.fm didn't accept that key")
        lastfm.save_key(key or None)
        with LOCK:
            self._sugg = None  # suggestions may now work (or stop)
            self._changed()
        return self.settings()

    def set_beat_to_beat(self, on: bool) -> None:
        with LOCK:
            self.beat_to_beat = bool(on)
            self._save_session()
            self._changed()  # version bump: the player replans a not-yet-started transition

    def set_genre(self, g: str, by: str | None = None) -> None:
        _known_genre(g, allow_any=True)
        with LOCK:
            self.genre = g
            self._mark("source", by)
            self.playlist, self.pl_run = None, {}  # one source at a time: Playlist back to None
            # One source at a time: a genre or mood turns party off (moods come through here too).
            if self.party:
                self.party = None
                self._sugg = None
            self._save_session()  # the source survives a restart
            cur = self.by_id(self.playing) if self.playing else None
            keep = None
            if cur is not None:
                elig = [s for s in self.pool if self._eligible(s)]
                if g in self.moods:  # mood membership, not genre labels: entry from its members
                    members = set(self.moods[g])
                    elig = [s for s in elig if s.id in members or s.effective_genre() == g]
                    song = max(elig, key=lambda s: score_pair(cur, s)) if elig else None
                else:
                    song, _ = genre_entry(cur, elig, g)
                # The best entry song replaces the fixed slot so the switch is audible next.
                keep = [song.id] if song else []
            self._recompute(keep)  # also sets/clears the fall-back notice
            self._changed()

    def set_queue(self, ids: list[str], pin: str | None) -> None:
        """Owner reordered Up next (drag, ↑ ↓; `pin` = the song moved). The moved song and
        everything above the lowest kept song become kept, in this order: kept songs always
        come before auto picks, so a song the owner placed among them is theirs now."""
        with LOCK:
            known = {s.id for s in self.pool}
            if len(set(ids)) != len(ids) or not set(ids) <= known:
                raise EngineError(400, "Queue ids must be unique songs from the pool")
            if pin is not None and pin not in ids:
                raise EngineError(400, "Pinned song must be in the queue")
            mine = set(self.requested) | ({pin} if pin else set())
            if pin:
                self.by_mcp.discard(f"song:{pin}")  # the owner placed it: it's theirs now
            last = max((n for n, i in enumerate(ids) if i in mine), default=-1)
            self.requested = ids[: last + 1] + [i for i in self.requested if i not in ids]
            self._recompute(keep=ids[: config.FIXED_NEXT] if self.playing else [])
            self._save_session()
            self._changed()

    def set_song_genre(self, sid: str, g: str | None) -> None:
        _known_genre(g, allow_none=True)
        with LOCK:
            s = self._song(sid)
            s.genre = g  # in place: the worker holds this object
            library.save_song(s)
            self._recompute()
            self._changed()

    def retry(self, sid: str) -> None:
        """Library Retry on a failed song: back to queued, so the worker tries it again (same
        as re-adding its link, without the network round trip to read the link again)."""
        with LOCK:
            s = self._song(sid)
            if s.status != "failed":
                raise EngineError(409, f"{s.title} hasn't failed")
            s.status, s.error = "queued", None  # in place: the worker holds this object
            library.save_song(s)
            self._recompute()
            self._changed()

    def remove(self, sid: str) -> None:
        with LOCK:
            self.pool.remove(self._song(sid))  # list.remove: the worker's identity check sees it gone
            self.upcoming = [i for i in self.upcoming if i != sid]
            self.requested = [i for i in self.requested if i != sid]
            self.kept.pop(sid, None)
            self.by_mcp.discard(f"song:{sid}")
            if self.next_pick == sid:
                self.next_pick = None  # re-adding it later must not make it jump the queue
            self.maps.pop(sid, None)
            self.entered.pop(sid, None)
            self.plans = {k: v for k, v in self.plans.items() if v[0] != sid}
            self._save_session()  # drop its map/entry on disk too
            # Files stay on disk: re-adding the song later reuses the download + analysis.
            library.save_pool([s.id for s in self.pool])
            self._recompute()
            self._changed()

    def set_genres(self, items: list[str]) -> None:
        if (not items or len(set(items)) != len(items) or config.ANY_GENRE in items
                or any(not isinstance(x, str) or not x.strip() for x in items)):
            raise EngineError(400, f"Genres must be unique, non-empty names (not {config.ANY_GENRE!r})")
        with LOCK:
            config.save_genres(items)
            if self.genre not in items:
                self.genre = config.ANY_GENRE  # the active genre was deleted
            self._recompute()
            self._changed()

    # --- saved playlists (design HJGfo) ---------------------------------------------------
    def _playlists_file(self):
        return config.library_root() / "playlists.json"

    def _load_playlists(self) -> dict[str, dict]:
        """playlists.json: {list_id: entry}. Corrupt file or entries are skipped, never fatal."""
        out = {}
        for lid, p in self._load_json_dict("playlists.json").items():
            try:
                if not isinstance(p, dict) or not LIST_RE.fullmatch(lid):
                    continue
                ids = p.get("song_ids") if isinstance(p.get("song_ids"), list) else []
                ex = p.get("excluded") if isinstance(p.get("excluded"), list) else []
                out[lid] = {
                    "list_id": lid, "name": str(p.get("name") or lid)[:100],
                    "song_ids": list(dict.fromkeys(str(i) for i in ids)),  # YouTube order = the playlist
                    "genre": p["genre"] if isinstance(p.get("genre"), str) else None,  # "treat songs as"
                    "order": p.get("order") if p.get("order") in PL_ORDERS else "playlist",
                    "at_end": p.get("at_end") if p.get("at_end") in PL_AT_END else "keep_going",
                    "excluded": [str(i) for i in ex],  # ✕'d here: skipped, not deleted
                    "synced_at": float(p.get("synced_at") or 0), "saved_at": float(p.get("saved_at") or 0)}
            except (TypeError, ValueError):
                continue
        return out

    def _save_playlists(self) -> None:
        config.write_json_atomic(self._playlists_file(), self.playlists)

    def _pl(self) -> dict | None:
        return self.playlists.get(self.playlist) if self.playlist else None

    def _playlist(self, lid: str) -> dict:
        pl = self.playlists.get(lid)
        if pl is None:
            raise EngineError(404, "No saved playlist with that id")
        return pl

    def _pl_songs(self, pl: dict) -> list[Song]:
        """The playlist's playable songs in its order: in the library, not excluded, not failed."""
        by, ex = {s.id: s for s in self.pool}, set(pl["excluded"])
        return [by[i] for i in pl["song_ids"] if i in by and i not in ex and by[i].status != "failed"]

    def _pl_left(self, pl: dict) -> list[Song]:
        """What this run hasn't played yet (the playing song excluded), playlist order."""
        return [s for s in self._pl_songs(pl) if s.id not in self.pl_run and s.id != self.playing]

    def _pl_end_note(self, pl: dict, left: list[Song]) -> str | None:
        """The heads-up two songs before the end: the last one is next (or playing)."""
        if len(left) > 1 or not self.pl_run:
            return None
        last = left[-1] if left else (self.by_id(self.playing) if self.playing in pl["song_ids"] else None)
        if last is None:
            return None
        then = {"keep_going": "similar songs from the library.", "repeat": f"{pl['name']} again from #1.",
                "stop": "the music fades out and ON AIR turns off."}[pl["at_end"]]
        return f"{pl['name']} ends after {party.artist_title(last)[1]}. Then: {then}"

    def _pl_summary(self, pl: dict, by: dict[str, Song]) -> dict:
        songs = [by[i] for i in pl["song_ids"] if i in by]
        failed = [s for s in songs if s.status == "failed"]
        return {"id": pl["list_id"], "name": pl["name"], "count": len(pl["song_ids"]),
                "ready": sum(1 for s in songs if s.status == "ready"), "failed": len(failed),
                "fail_reason": _fail_reason(failed), "covers": pl["song_ids"][:4], "genre": pl["genre"],
                "order": pl["order"], "at_end": pl["at_end"], "excluded": len(pl["excluded"]),
                "synced_at": pl["synced_at"], "stale": time.time() - pl["synced_at"] > PL_STALE,
                # Songs left in this run, the playing one included ("48 songs · 2 left").
                "left": (len(self._pl_left(pl)) + (self.playing in pl["song_ids"] and self.playing not in pl["excluded"])
                         if pl is self._pl() else None),
                "url": f"https://www.youtube.com/playlist?list={pl['list_id']}"}

    def _playlist_draft(self, pl: dict) -> list[dict]:
        """The Suggestions tab while a playlist plays: the playlist itself. Playlist order: its own
        order. Mix order: played, playing, then the planner's order (`plan` = 0 for next, 1 for
        the 3rd song …, `why` = what makes it fit), then the songs not ready yet."""
        by = {s.id: s for s in self.pool}
        ex = set(pl["excluded"])
        members = [i for i in pl["song_ids"] if i in by and i not in ex]
        pos = {i: n + 1 for n, i in enumerate(pl["song_ids"])}
        plan: dict[str, tuple[int, str | None]] = {}
        ids = members
        if pl["order"] == "mix":
            mine = set(members)
            left = [s for s in self._pl_left(pl) if s.status == "ready" and s.analysis]
            head = [i for i in self.upcoming if i in mine]
            rest = [s for s in left if s.id not in head]
            prev = by.get(head[-1]) if head else by.get(self.playing)
            if prev is not None and prev.analysis:
                rest = order(prev, rest, {}, config.ANY_GENRE, pairs=self.pairs)
            seq = [by[i] for i in head] + rest
            before = by.get(self.playing)
            for k, s in enumerate(seq):
                plan[s.id] = (k, _fit_reason(before, s, seq[k:]))
                before = s
            played = [i for i in self.pl_run if i in mine and i != self.playing]
            now = [self.playing] if self.playing in mine else []
            ids = list(dict.fromkeys(played + now + [s.id for s in seq] + members))
        left_ids = [s.id for s in self._pl_left(pl)]
        last = left_ids[-1] if left_ids and pl["at_end"] != "repeat" else None
        if last and plan and len(plan) == len(left_ids):  # mix order, all ready: its own last song
            last = max(plan, key=lambda i: plan[i][0])
        nxt = self.upcoming[0] if self.upcoming else None
        rows = []
        for i in ids:
            s = by[i]
            a, t = party.artist_title(s)
            ready = s.status == "ready" and s.analysis is not None
            state = ("playing" if i == self.playing else "played" if i in self.pl_run else "next" if i == nxt
                     else "failed" if s.status == "failed" else "waiting" if not ready
                     else "kept" if i in self.requested else "")
            row = {"id": i, "artist": a, "title": t, "pos": pos[i], "state": state, "status": s.status,
                   "played_at": self.pl_run.get(i), "bpm": s.analysis.bpm if s.analysis else None,
                   "last": i == last, "error": s.error}
            if i in plan and state not in ("played", "playing"):
                row["plan"], row["why"] = plan[i]
            rows.append(row)
        return rows

    def playlists_list(self) -> list[dict]:
        with LOCK:
            by = {s.id: s for s in self.pool}
            return [self._pl_summary(p, by) for p in reversed(self.playlists.values())]

    def save_playlist(self, url: str, genre: str | None) -> dict:
        """Save a YouTube playlist link: read it (name + songs, one request), import its songs into
        the library like Add link, and remember it. Saving one that's saved already opens it
        (existing=True, nothing is read). Doesn't switch the source: picking it does."""
        _known_genre(genre, allow_none=True)
        try:
            kind, lid = fetch.classify_url(url)
        except ValueError as e:
            raise EngineError(400, "Paste a playlist link: youtube.com/playlist?list=…") from e
        if kind != "playlist":
            # A watch link carrying list= is one song playing inside a playlist (design 10).
            raise EngineError(400, ONE_SONG if "list=" in url else
                              "That’s a link to one song. Paste a playlist link: youtube.com/playlist?list=…")
        if not LIST_RE.fullmatch(lid):
            raise EngineError(400, "That playlist id doesn't look right")
        with LOCK:
            if lid in self.playlists:
                return {"existing": True, "playlist": self._pl_summary(self.playlists[lid], {s.id: s for s in self.pool})}
        try:
            name, songs = fetch.read_playlist(url)  # network, outside the lock
        except fetch.FetchError as e:
            raise EngineError(400, str(e)) from e
        if not songs:
            raise EngineError(404, "That playlist has no songs autodeck can read")
        with LOCK:
            if lid in self.playlists:  # saved meanwhile (double click)
                return {"existing": True, "playlist": self._pl_summary(self.playlists[lid], {s.id: s for s in self.pool})}
            added, _ = self._import_songs(songs, genre)
            ids = list(dict.fromkeys(x.id for x in songs))
            now = time.time()
            self.playlists[lid] = {"list_id": lid, "name": (name or "YouTube playlist")[:100], "song_ids": ids,
                                   "genre": genre, "order": "playlist", "at_end": "keep_going", "excluded": [],
                                   "synced_at": now, "saved_at": now}
            if genre:
                self._apply_pl_genre(self.playlists[lid], None)
            self._save_playlists()
            self._changed()  # the worker starts on it from the top
            return {"existing": False, "found": len(ids), "added": added,
                    "playlist": self._pl_summary(self.playlists[lid], {s.id: s for s in self.pool})}

    def _apply_pl_genre(self, pl: dict, old: str | None) -> None:
        """"Treat songs as" = the genre_hint of every song in it (import as, for the whole list).
        Owner-set per-song genres still win (Song.effective_genre). Back to "their own genres"
        clears only the hint this playlist set."""
        ids = set(pl["song_ids"])
        for s in self.pool:
            if s.id in ids and (pl["genre"] or s.genre_hint == old):
                s.genre_hint = pl["genre"]  # in place: the worker holds this object
                library.save_song(s)

    def sync_playlist(self, lid: str) -> dict:
        """↻: read the playlist again. New songs are imported and sit at their YouTube position;
        songs removed on YouTube leave the playlist only (never the library).
        Returns {"added": [#positions], "new_ids", "removed": n, "playlist"}."""
        with LOCK:
            self._playlist(lid)
        try:
            _name, songs = fetch.read_playlist(f"https://www.youtube.com/playlist?list={lid}")  # network
        except fetch.FetchError as e:
            raise EngineError(400, str(e)) from e
        with LOCK:
            pl = self._playlist(lid)  # removed meanwhile -> 404
            new = list(dict.fromkeys(x.id for x in songs))
            added, removed = sync_diff(pl["song_ids"], new)
            self._import_songs(songs, pl["genre"])
            pl["song_ids"], pl["synced_at"] = new, time.time()
            pl["excluded"] = [i for i in pl["excluded"] if i in set(new)]
            if pl["genre"]:
                self._apply_pl_genre(pl, None)
            self._save_playlists()
            self._recompute()
            self._changed()
            return {"added": [n + 1 for n in added], "new_ids": [new[n] for n in added], "removed": len(removed),
                    "playlist": self._pl_summary(pl, {s.id: s for s in self.pool})}

    def pick_playlist(self, lid: str | None, by: str | None = None) -> None:
        """Playlist dropdown: play from this saved playlist (None = Playlist: None). Like a genre
        or party it is exclusive: Vibe goes to Any and Party to Off. A run starts from the top."""
        with LOCK:
            if lid is None:
                self.playlist, self.pl_run = None, {}
                keep = None
            else:
                pl = self._playlist(lid)
                if self.playlist == lid:
                    return
                self.playlist, self.genre, self.party, self._sugg = lid, config.ANY_GENRE, None, None
                self.pl_run = {self.playing: time.strftime("%H:%M")} if self.playing in pl["song_ids"] else {}
                # Like a genre switch: the playlist's own next song replaces the fixed slot.
                keep = [] if self.playing else None
            self._mark("source", by)
            self._save_session()
            self._recompute(keep)
            self._changed()

    def update_playlist(self, lid: str, **changes) -> dict:
        """Rename / treat songs as / playlist or mix order / at the end. Only the given keys change."""
        with LOCK:
            pl = self._playlist(lid)
            if "name" in changes:
                name = " ".join(str(changes["name"] or "").split())[:100]
                if not name:
                    raise EngineError(400, "Give the playlist a name")
                pl["name"] = name
            if "order" in changes and changes["order"] not in PL_ORDERS:
                raise EngineError(400, "Order is playlist or mix")
            if "at_end" in changes and changes["at_end"] not in PL_AT_END:
                raise EngineError(400, "At the end is keep_going, repeat or stop")
            pl.update({k: changes[k] for k in ("order", "at_end") if k in changes})
            if "genre" in changes:
                _known_genre(changes["genre"], allow_none=True)
                old, pl["genre"] = pl["genre"], changes["genre"]
                self._apply_pl_genre(pl, old)
            self._save_playlists()
            self._recompute([] if "order" in changes and pl is self._pl() and self.playing else None)
            self._changed()
            return self._pl_summary(pl, {s.id: s for s in self.pool})

    def exclude_playlist_song(self, lid: str, sid: str, on: bool = True) -> None:
        """✕ in a playlist: skip this song in it from now on (it stays in the playlist and the
        library). on=False is the Undo."""
        with LOCK:
            pl = self._playlist(lid)
            if sid not in pl["song_ids"]:
                raise EngineError(404, "That song isn't in this playlist")
            ex = [i for i in pl["excluded"] if i != sid]
            pl["excluded"] = ex + [sid] if on else ex
            self._save_playlists()
            # A ✕ on the next song takes effect now, even if it was locked in.
            self._recompute([i for i in self.upcoming[: config.FIXED_NEXT] if i != sid] if self.playing else None)
            self._changed()

    def remove_playlist(self, lid: str) -> dict:
        """Forget the playlist (playlists.json only: songs, analysis and genres stay). If it was
        playing, the source falls back to Vibe: Any; the locked next songs keep their slots, so
        the deck plays on without a gap."""
        with LOCK:
            pl = self._playlist(lid)
            del self.playlists[lid]
            self._save_playlists()
            was = self.playlist == lid
            if was:
                self.playlist, self.pl_run = None, {}
                self.by_mcp.discard("source")
                self._save_session()
            self._recompute()
            self._changed()
            return {"name": pl["name"], "count": len(pl["song_ids"]), "was_playing": was}

    # --- decks & transitions ----------------------------------------------------
    def _ready(self, sid: str) -> Song:
        s = self._song(sid)
        if s.status != "ready" or s.analysis is None:
            raise EngineError(409, f"Song not ready: {sid}")
        return s

    def _deck(self, s: Song, tm: TimeMap, path) -> dict:
        an = s.analysis
        ps, pe = an.play_start, an.play_end
        peaks_file = library.song_dir(s.id) / "peaks.json"
        try:
            peaks = json.loads(peaks_file.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            peaks = []
        if peaks:
            # Peaks span the whole source but the deck draws them over the render, which only
            # covers [play_start, play_end]: keep that slice. Duration from the file header
            # (Song.duration is YouTube metadata, 0 for some playlist entries).
            try:
                dur = sf.info(str(library.audio_path(s.id))).duration
            except (OSError, RuntimeError):
                dur = 0.0
            if dur > 0:
                n = len(peaks)
                peaks = peaks[int(ps / dur * n): max(int(round(pe / dur * n)), int(ps / dur * n) + 1)]
        r = tm.to_rendered
        return {"id": s.id, "title": s.title, "channel": s.channel, "genre": s.effective_genre(),
                "key": an.key, "camelot": an.camelot, "bpm_original": an.bpm,
                # speed_at = original s per rendered s, so the heard tempo is bpm * speed.
                "speed_at_start": tm.speed_at(ps), "bpm_playing": an.bpm * tm.speed_at(pe),
                "url": f"/audio/{s.id}/{path.name}", "peaks": peaks,
                "duration_rendered": tm.points[-1][1],
                "downbeats_rendered": [r(d) for d in an.downbeats if ps <= d <= pe],
                "sections_rendered": [{"start": r(max(x.start, ps)), "end": r(min(x.end, pe)), "label": x.label}
                                      for x in _shown_sections(an)]}

    def deck(self, sid: str) -> dict:
        with LOCK:
            s = self._ready(sid)
            tm = self.maps.get(sid) or time_map_for(s, None)
        path = render(s, tm)  # blocks (Rubber Band); never under the lock
        with LOCK:
            self.maps.setdefault(sid, tm)
            return self._deck(s, tm, path)

    def stem(self, sid: str, name: str) -> dict:
        """The Voice/Beat switch: song sid's stem `name` rendered with the map it's playing on, so it
        lines up sample for sample with the main render the deck is already playing."""
        if name not in stems.NAMES:
            raise EngineError(400, f"Unknown stem: {name}")
        with LOCK:
            s = self._ready(sid)
            tm = self.maps.get(sid) or time_map_for(s, None)
        if not stems.has(sid):
            raise EngineError(409, "No stems for this song yet")
        path = render(s, tm, stem=name)  # one Rubber Band pass; never under the lock
        return {"id": sid, "stem": name, "url": f"/audio/{sid}/{path.name}"}

    def transition(self, from_id: str, after: float | None, kind: str | None = None,
                   enter: int | None = None, to: str | None = None) -> dict:
        """`enter` = index into the cued song's sections_rendered: the owner clicked that section on
        the cued deck, so the mix brings it in there. Only applied while `to` is still the next
        song (the queue may have changed while the click was on its way)."""
        if kind is not None and kind not in FORCEABLE:
            raise EngineError(400, f"Unknown transition effect: {kind}")
        with LOCK:
            kind = kind or self.next_effect
            a = self._ready(from_id)
            nxt = next((i for i in self.upcoming if i != from_id), None)
            b = self.by_id(nxt) if nxt else None
            if b is None or b.status != "ready" or b.analysis is None:
                return {"transition": None}
            # No map yet (e.g. after a restart): a is playing its identity render.
            tm_a = self.maps.setdefault(a.id, time_map_for(a, None))
            # The player reports rendered seconds; the planner works in original time.
            after_orig = tm_a.to_original(after) if after is not None else None
            shown = _shown_sections(b.analysis)
            enter_at = shown[enter].start if to == b.id and enter is not None and 0 < enter < len(shown) else None
            # Vocal activity of the songs that have stems: Vocal swap needs a's (planner).
            vocals = {s.id: v for s in (a, b) if (v := stems.activity(s.id)) is not None}
            plan = lambda force: pick_transition(  # noqa: E731
                a, b, after=after_orig, entered=self.entered.get(a.id),
                beat_to_beat=self.beat_to_beat, force=force, enter=enter_at, vocals=vocals)
            # The dropdown lists only effects that actually work for this pair (pure, cheap:
            # the planner is a handful of list scans). "blend" covers half_double too.
            options = [k for k in FORCEABLE
                       if plan(k).kind in (("blend", "half_double") if k == "blend" else (k,))]
            # Pair memory (design xWVJy) only steers Auto; an effect the owner picked always wins.
            # A loved pair replays the effect it was loved with (when it still works here); an
            # effect 👎'd as wrong for this pair is never Auto's pick (it stays forceable, dimmed).
            mem = self.pairs.get(pair_key(a.id, b.id)) or {}
            loved_fx = mem["effect"] if mem.get("loved") and mem.get("effect") in options else None
            avoid_fx = mem["effect"] if mem.get("avoided") and mem.get("why") == "effect" else None
            tr = plan(kind or loved_fx)
            if not kind and avoid_fx and pair_effect(tr.kind) == avoid_fx:
                alt = next((k for k in options if k != avoid_fx), None)
                tr = plan(alt) if alt else tr
            # Only claim "you loved it" when that is what was planned: the label must name the
            # effect that will actually play, never the remembered one.
            loved_fx = None if kind or pair_effect(tr.kind) != loved_fx else loved_fx
            # ponytail: outgoing=None, b's own exit isn't planned yet; the planner keeps
            # exits after the ease when it can, so the ease is rarely cut short.
            tm_b = time_map_for(b, tr)
        path = render(b, tm_b)  # blocks a few seconds; endpoint is a threadpool `def`
        # Vocal swap: a drops to its instrumental, rendered with the map a is playing on (aligned).
        # A failed render degrades to a plain blend in the player (out_stem missing), never silence.
        out_stem = None
        if tr.kind == "vocal_swap":
            try:
                out_stem = f"/audio/{a.id}/{render(a, tm_a, stem='instrumental').name}"
            except Exception:
                logging.getLogger(__name__).exception("instrumental render failed for %s", a.id)
        with LOCK:
            plan_id = uuid.uuid4().hex
            self.plans[plan_id] = (b.id, tm_b, tr)
            # Keep the last PLANS_PER_SONG candidates per incoming song: enough for a few
            # overlapping replans, bounded for a long session.
            mine = [k for k, v in self.plans.items() if v[0] == b.id]
            for k in mine[:-PLANS_PER_SONG]:
                del self.plans[k]
            return {"transition": asdict(tr), "plan_id": plan_id, "in_deck": self._deck(b, tm_b, path),
                    "options": options, "forced": kind,
                    # Why Auto sounds the way it does: "Auto · Bass swap (you loved it)", and the
                    # effect the picker dims as "you 👎 it here".
                    "loved_effect": loved_fx, "avoid_effect": avoid_fx,
                    "out_at_rendered": tm_a.to_rendered(tr.out_at),
                    "out_stem": out_stem,
                    "in_at_rendered": tm_b.to_rendered(tr.in_at)}

