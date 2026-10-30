"""In-memory mix state shared by the HTTP layer and the background worker.

Split out of server.py so the queue rules can be read (and tested) without the
HTTP plumbing. One lock for everything: library.LOCK (an RLock). The worker
calls get_state() while already holding it, so any second lock here could
deadlock; reusing the same RLock can't.
"""
import json
import uuid
from dataclasses import asdict

import soundfile as sf

from . import config, fetch, jobs, library
from .library import Song
from .planner import Transition, genre_entry, order, pick_transition
from .render import TimeMap, render, time_map_for

LOCK = library.LOCK
PLANS_PER_SONG = 4


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
        self.pinned: dict[int, str] = {}  # upcoming index -> song id the owner dragged there
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
        # Created here, started by the server lifespan (tests never start it).
        self.worker = jobs.Worker(self.get_state, on_change=self._on_worker_change)
        with LOCK:
            self._load_session()
            self._recompute()

    # --- worker interface ----------------------------------------------------
    def get_state(self):
        with LOCK:  # reentrant: the worker calls this while holding LOCK in _set
            return list(self.pool), list(self.upcoming), self.genre, set(self.played)

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
            "entered": {k: asdict(t) for k, t in self.entered.items()}})

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
        """Rebuild `upcoming`. The first FIXED_NEXT of the old queue stay put (the owner
        may already be looking at them / the next render is planned), unless `keep`
        overrides them (genre switch)."""
        g, anyg = self.genre, config.ANY_GENRE
        cands = [s for s in self.pool if self._eligible(s)]
        in_genre = [s for s in cands if g == anyg or s.effective_genre() == g]
        # Review Focus 3: the genre ran dry -> play from any genre (with a notice), never go silent.
        fell_back = bool(cands) and not in_genre
        self.notice = f"No ready {g} songs yet — playing from any genre" if fell_back else None
        use_genre = anyg if fell_back else g
        cur = self.by_id(self.playing) if self.playing else None
        if cur is None:
            # Nothing playing yet: no "previous song" to score against, so pool order.
            self.upcoming = [s.id for s in (cands if fell_back else in_genre)]
            return
        ok = {s.id for s in cands}
        if keep is None:
            keep = self.upcoming[: config.FIXED_NEXT]
        keep = [i for i in keep if i in ok]
        # Pins are absolute upcoming indices; order() sees the list after the kept prefix.
        n = len(keep)
        pins = {i - n: sid for i, sid in self.pinned.items() if i >= n and sid not in keep}
        prev = self.by_id(keep[-1]) if keep else cur
        rest = order(prev, [s for s in cands if s.id not in keep], pins, use_genre)
        self.upcoming = keep + [s.id for s in rest]

    def view(self, s: Song) -> dict:
        an = s.analysis
        return {"id": s.id, "title": s.title, "channel": s.channel, "status": s.status, "error": s.error,
                "genre": s.effective_genre(), "bpm": an.bpm if an else None, "camelot": an.camelot if an else None}

    # --- API operations ---------------------------------------------------------
    def state(self) -> dict:
        with LOCK:
            by = {s.id: s for s in self.pool}  # one pass; the pool can be a whole Liked Music import
            counts: dict[str, int] = {}
            for s in self.pool:
                counts[s.status] = counts.get(s.status, 0) + 1
            return {"version": self.version, "genre": self.genre, "genres": config.genres(),
                    "notice": self.notice, "playing": self.playing,
                    "queue": [self.view(by[i]) for i in self.upcoming if i in by],
                    # Whole pool too: the UI shows per-song status/errors and lets the owner
                    # relabel or delete songs that aren't (or can't be) in the queue.
                    "pool": [self.view(s) for s in self.pool],
                    "pool_counts": counts}

    def add(self, url: str, genre: str | None) -> int:
        _known_genre(genre, allow_none=True)
        try:
            # Network call, deliberately outside the lock; a bad URL raises ValueError first.
            songs = fetch.expand(url)
        except (ValueError, fetch.FetchError) as e:
            raise EngineError(400, str(e)) from e
        added = retried = 0
        with LOCK:
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
        return added + retried  # the UI's "Added N songs": a retry is (re)queued too

    def set_playing(self, sid: str, plan_id: str | None = None) -> None:
        with LOCK:
            self._song(sid)
            plan = self.plans.get(plan_id) if plan_id else None
            if plan and plan[0] == sid:
                self.maps[sid], self.entered[sid] = plan[1], plan[2]
            # No / unknown plan_id (first song, or a plan pruned long ago): maps stay as they
            # are, i.e. identity via deck() for a fresh song.
            pos = self.upcoming.index(sid) if sid in self.upcoming else None
            # Everything after the song that just started moves up one slot; pins follow.
            # ponytail: pins only track the common case (playing a queued song); jumping to
            # a song outside the queue leaves pin indices as they were.
            self.pinned = {i - (1 if pos is not None and pos < i else 0): p
                           for i, p in self.pinned.items() if p != sid}
            self.playing = sid
            self.played = self.played | {sid}  # new set: get_state copies never see a resize
            self._save_session()
            self._recompute()
            self._changed()

    def set_genre(self, g: str) -> None:
        _known_genre(g, allow_any=True)
        with LOCK:
            self.genre = g
            cur = self.by_id(self.playing) if self.playing else None
            keep = None
            if cur is not None:
                song, _ = genre_entry(cur, [s for s in self.pool if self._eligible(s)], g)
                # The best entry song replaces the fixed slot so the switch is audible next.
                keep = [song.id] if song else []
            self._recompute(keep)  # also sets/clears the fall-back notice
            self._changed()

    def set_queue(self, ids: list[str], pin: str | None) -> None:
        with LOCK:
            known = {s.id for s in self.pool}
            if len(set(ids)) != len(ids) or not set(ids) <= known:
                raise EngineError(400, "Queue ids must be unique songs from the pool")
            if pin is not None and pin not in ids:
                raise EngineError(400, "Pinned song must be in the queue")
            self.upcoming = list(ids)
            pins = {i: p for i, p in self.pinned.items() if p in known and p != pin}
            if pin is not None:
                pins = {i: p for i, p in pins.items() if i != ids.index(pin)}
                pins[ids.index(pin)] = pin
            self.pinned = pins
            self._changed()

    def set_song_genre(self, sid: str, g: str | None) -> None:
        _known_genre(g, allow_none=True)
        with LOCK:
            s = self._song(sid)
            s.genre = g  # in place: the worker holds this object
            library.save_song(s)
            self._recompute()
            self._changed()

    def remove(self, sid: str) -> None:
        with LOCK:
            self.pool.remove(self._song(sid))  # list.remove: the worker's identity check sees it gone
            self.upcoming = [i for i in self.upcoming if i != sid]
            self.pinned = {i: p for i, p in self.pinned.items() if p != sid}
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
                dur = sf.info(str(library.song_dir(s.id) / "audio.flac")).duration
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
                                      for x in an.sections if x.end > ps and x.start < pe]}

    def deck(self, sid: str) -> dict:
        with LOCK:
            s = self._ready(sid)
            tm = self.maps.get(sid) or time_map_for(s, None)
        path = render(s, tm)  # blocks (Rubber Band); never under the lock
        with LOCK:
            self.maps.setdefault(sid, tm)
            return self._deck(s, tm, path)

    def transition(self, from_id: str, after: float | None) -> dict:
        with LOCK:
            a = self._ready(from_id)
            nxt = next((i for i in self.upcoming if i != from_id), None)
            b = self.by_id(nxt) if nxt else None
            if b is None or b.status != "ready" or b.analysis is None:
                return {"transition": None}
            # No map yet (e.g. after a restart): a is playing its identity render.
            tm_a = self.maps.setdefault(a.id, time_map_for(a, None))
            # The player reports rendered seconds; the planner works in original time.
            after_orig = tm_a.to_original(after) if after is not None else None
            tr = pick_transition(a, b, after=after_orig, entered=self.entered.get(a.id))
            # ponytail: outgoing=None, b's own exit isn't planned yet; the planner keeps
            # exits after the ease when it can, so the ease is rarely cut short.
            tm_b = time_map_for(b, tr)
        path = render(b, tm_b)  # blocks a few seconds; endpoint is a threadpool `def`
        with LOCK:
            plan_id = uuid.uuid4().hex
            self.plans[plan_id] = (b.id, tm_b, tr)
            # Keep the last PLANS_PER_SONG candidates per incoming song: enough for a few
            # overlapping replans, bounded for a long session.
            mine = [k for k, v in self.plans.items() if v[0] == b.id]
            for k in mine[:-PLANS_PER_SONG]:
                del self.plans[k]
            return {"transition": asdict(tr), "plan_id": plan_id, "in_deck": self._deck(b, tm_b, path),
                    "out_at_rendered": tm_a.to_rendered(tr.out_at),
                    "in_at_rendered": tm_b.to_rendered(tr.in_at)}

