"""In-memory mix state shared by the HTTP layer and the background worker.

Split out of server.py so the queue rules can be read (and tested) without the
HTTP plumbing. One lock for everything: library.LOCK (an RLock). The worker
calls get_state() while already holding it, so any second lock here could
deadlock; reusing the same RLock can't.
"""
import json
import re
import logging
import threading
import time
import uuid
from dataclasses import asdict
from pathlib import Path

import soundfile as sf

from . import config, fetch, jobs, lastfm, library, moods, party
from .library import Song
from .planner import FORCEABLE, Transition, genre_entry, order, pair_key, pick_transition, score_pair
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
        # Songs the owner picked from search ("play next"), oldest first. Not-ready ones go to
        # the front of the worker's list (prepped before anything, cap or not); ready ones go to
        # the front of the queue, whatever the active genre. Dropped once they start playing.
        self.requested: list[str] = []
        # Moods built from public playlists (moods.py): name -> song ids. A mood is selected like
        # a genre (its name is in the genres list), but membership is this list, so a song keeps
        # its own genre label and can be in several moods.
        self.moods: dict[str, list[str]] = self._load_moods()
        # Party mode: a flavor (party.FLAVORS) replaces the genre filter while on; None = off.
        self.party: str | None = None
        self.rejected: set[str] = self._load_rejected()  # party.key()s the owner turned down
        # Pair memory: the owner's verdict on a hand-over, pair_key(a, b) -> "loved" | "avoided".
        # Directional and kept for removed songs too (re-adding the link brings the memory back).
        self.pairs: dict[str, str] = self._load_pairs()
        self._sugg: tuple[tuple, float, list] | None = None  # ((playing, party), time, list)
        self._enrich_stop = threading.Event()
        # Effect forced for the playing song's exit (dropdown, MCP). Server-side so every
        # client agrees; cleared when the next song starts. mix_now_seq: bumped by "mix now"
        # requests from anywhere (MCP); the player mixes when it sees a new value.
        self.next_effect: str | None = None
        self.mix_now_seq = 0
        # Owner's "beat to beat" mode: blends skip a's outro and b's intro (planner._main_view).
        self.beat_to_beat = False
        # Created here, started by the server lifespan (tests never start it).
        self.worker = jobs.Worker(self.get_state, on_change=self._on_worker_change)
        with LOCK:
            self._load_session()
            self._recompute()

    # --- worker interface ----------------------------------------------------
    def get_state(self):
        with LOCK:  # reentrant: the worker calls this while holding LOCK in _set
            first = [i for i in self.requested if i not in self.upcoming]  # requested, not ready yet
            return list(self.pool), first + list(self.upcoming), self.genre, set(self.played)

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
            "beat_to_beat": self.beat_to_beat, "party": self.party})

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
        if self.party:
            # Party flavor replaces the genre filter; songs not looked up on Last.fm yet don't fit.
            in_genre = [s for s in cands if party.fits(s, self.party)]
            what = f"{self.party} party"
        elif g in self.moods:
            members = set(self.moods[g])
            in_genre = [s for s in cands if s.id in members or s.effective_genre() == g]
            what = g
        else:
            in_genre = [s for s in cands if g == anyg or s.effective_genre() == g]
            what = g
        # Review Focus 3: the genre ran dry -> play from any genre (with a notice), never go silent.
        fell_back = bool(cands) and not in_genre
        self.notice = f"No ready {what} songs yet — playing from any genre" if fell_back else None
        # order() filters by genre itself; for a party the candidates arrive pre-filtered.
        prefiltered = bool(self.party) or g in self.moods
        use_genre = anyg if (fell_back or prefiltered) else g
        group = {s.id for s in (cands if fell_back else in_genre)}
        cur = self.by_id(self.playing) if self.playing else None
        ok = {s.id for s in cands}
        asked = [i for i in self.requested if i in ok]  # owner's picks play next, any genre
        if cur is None:
            # Nothing playing yet: no "previous song" to score against, so pool order.
            self.upcoming = asked + [s.id for s in (cands if fell_back else in_genre) if s.id not in asked]
            return
        if keep is None:
            keep = self.upcoming[: config.FIXED_NEXT]
        keep = asked + [i for i in keep if i in ok and i not in asked]
        # A kept song whose hand-over the owner marked avoided loses its fixed slot (and so does
        # everything after it), so a fresh 👎 takes effect right away; order() still puts it
        # back if nothing else is left. The owner's own "play next" picks always stay.
        for j in range(len(asked), len(keep)):
            before = keep[j - 1] if j else cur.id
            if self.pairs.get(pair_key(before, keep[j])) == "avoided":
                keep = keep[:j]
                break
        # Pins are absolute upcoming indices; order() sees the list after the kept prefix.
        n = len(keep)
        pins = {i - n: sid for i, sid in self.pinned.items() if i >= n and sid not in keep}
        prev = self.by_id(keep[-1]) if keep else cur
        pin_ids = set(pins.values())  # pinned songs are the owner's choice: they stay whatever the filter
        pool = [s for s in cands if s.id not in keep and (s.id in group or s.id in pin_ids or not prefiltered)]
        rest = order(prev, pool, pins, use_genre, bonus=party.bonus if self.party else None, pairs=self.pairs)
        self.upcoming = keep + [s.id for s in rest]

    def view(self, s: Song) -> dict:
        an = s.analysis
        return {"id": s.id, "title": s.title, "channel": s.channel, "status": s.status, "error": s.error,
                "genre": s.effective_genre(), "bpm": an.bpm if an else None, "camelot": an.camelot if an else None,
                "danceability": (s.meta or {}).get("danceability")}

    # --- API operations ---------------------------------------------------------
    def state(self) -> dict:
        with LOCK:
            by = {s.id: s for s in self.pool}  # one pass; the pool can be a whole Liked Music import
            counts: dict[str, int] = {}
            for s in self.pool:
                counts[s.status] = counts.get(s.status, 0) + 1
            return {"version": self.version, "genre": self.genre, "genres": config.genres(),
                    "notice": self.notice, "playing": self.playing, "beat_to_beat": self.beat_to_beat,
                    "party": self.party, "flavors": list(party.FLAVORS), "lastfm": bool(lastfm.api_key()),
                    "next_effect": self.next_effect, "effects": list(FORCEABLE), "mix_now_seq": self.mix_now_seq,
                    "moods": {k: len(v) for k, v in self.moods.items()},
                    "queue": [self.view(by[i]) for i in self.upcoming if i in by],
                    # What the queue panel marks: the first `queue_locked` songs keep their order
                    # (_recompute's FIXED_NEXT, only while something plays), `pinned` = songs the
                    # owner dragged, `requested` = "play next" picks still downloading/analysing.
                    "queue_locked": config.FIXED_NEXT if self.playing else 0,
                    "pinned": sorted(set(self.pinned.values())),
                    "requested": [self.view(by[i]) for i in self.requested if i in by and i not in self.upcoming],
                    "pairs": dict(self.pairs),
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
            if sid in self.requested:
                self.requested.remove(sid)
            if sid != self.playing:  # a pulled-back fader re-reports the same song: one entry
                self.next_effect = None  # the forced effect was for the previous song's exit
                self._log_played(sid, plan[2] if plan and plan[0] == sid else None)
            prev, self.playing = self.playing, sid
            self.played = self.played | {sid}  # new set: get_state copies never see a resize
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

    def request(self, sid: str, title: str, channel: str, duration: float) -> None:
        """Owner picked a song (search): add it if new, prep it first, play it next."""
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
            if sid not in self.requested:
                self.requested.append(sid)
            self._recompute()
            self._changed()

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
        """Most recent first. Each entry carries `from` (the song it was mixed in from, or None)
        and `verdict` (the owner's pair rating of that hand-over, or None)."""
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
            pairs = dict(self.pairs)
        for prev, h in zip([None, *rows], rows):
            if "from" not in h:
                # Lines written before "from" existed: a song that came in via an effect was
                # mixed in from the line before it (the log is in play order).
                h["from"] = prev["id"] if prev and h.get("effect") else None
            h["verdict"] = pairs.get(pair_key(h["from"], h["id"])) if h["from"] else None
        return list(reversed(rows[-limit:]))

    # --- pair memory --------------------------------------------------------------------
    def _pairs_file(self):
        return config.library_root() / "pairs.json"

    def _load_pairs(self) -> dict[str, str]:
        try:
            d = json.loads(self._pairs_file().read_text(encoding="utf-8"))
            return {str(k): v for k, v in d.items() if v in ("loved", "avoided")} if isinstance(d, dict) else {}
        except (OSError, ValueError, TypeError, AttributeError):
            return {}

    def set_pair(self, a: str, b: str, verdict: str | None) -> None:
        """Owner's 🔥 (loved) / 👎 (avoided) on the hand-over a -> b; None forgets it.
        Saved on every rating (atomic), then the queue is re-ordered with it."""
        if verdict not in ("loved", "avoided", None):
            raise EngineError(400, f"Unknown verdict: {verdict}")
        if a == b:
            raise EngineError(400, "A pair needs two different songs")
        with LOCK:
            if verdict is None:
                self.pairs.pop(pair_key(a, b), None)
            else:
                self.pairs[pair_key(a, b)] = verdict
            config.write_json_atomic(self._pairs_file(), self.pairs)
            self._recompute()
            self._changed()

    def pairs_list(self) -> list[dict]:
        """Every rated pair with titles (songs no longer in the pool: from their cache)."""
        with LOCK:
            items = list(self.pairs.items())
            by = {s.id: s for s in self.pool}

        def title(sid: str) -> str:
            s = by.get(sid) or library.load_song(sid)
            return s.title if s else sid

        out = []
        for k, v in items:
            a, _, b = k.partition(">")
            out.append({"from": a, "to": b, "from_title": title(a), "to_title": title(b), "verdict": v})
        # Loved first, then avoided; most recently rated first within each (dict order = insertion).
        return sorted(reversed(out), key=lambda p: p["verdict"] != "loved")

    # --- moods ----------------------------------------------------------------------
    def _moods_file(self):
        return config.library_root() / "moods.json"

    def _load_moods(self) -> dict[str, list[str]]:
        try:
            d = json.loads(self._moods_file().read_text(encoding="utf-8"))
            return {str(k): [str(i) for i in v] for k, v in d.items()} if isinstance(d, dict) else {}
        except (OSError, ValueError, TypeError, AttributeError):
            return {}

    def build_mood(self, theme: str, n: int = 30) -> dict:
        """Collect the crowd's picks for a theme from public playlists, import them, and switch
        to the mood. Returns {"mood", "found", "added", "songs": [{title, votes}]}."""
        theme = " ".join(theme.split())[:60]
        if not theme:
            raise EngineError(400, "Type a mood, e.g. 'Balkan party'")
        name = theme[:1].upper() + theme[1:]
        if name == config.ANY_GENRE:
            raise EngineError(400, f"'{name}' is reserved")
        top = moods.collect(theme, max(5, min(n, 60)))  # network: seconds, outside the lock
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
            if name not in config.genres():
                config.save_genres([*config.genres(), name])
            library.save_pool([s.id for s in self.pool])
        self.set_genre(name)  # switch to it now; also replans the next transition
        return {"mood": name, "found": len(top), "added": added,
                "songs": [{"title": s.title, "votes": v} for s, v in top[:10]]}

    def delete_mood(self, name: str) -> None:
        """Forget a mood (songs stay in the library). Back to Any if it was active."""
        with LOCK:
            if name not in self.moods:
                raise EngineError(404, f"No mood called {name}")
            del self.moods[name]
            config.write_json_atomic(self._moods_file(), self.moods)
            config.save_genres([g for g in config.genres() if g != name] or list(config.DEFAULT_GENRES))
            if self.genre == name:
                self.genre = config.ANY_GENRE
            self._recompute()
            self._changed()

    # --- party mode ------------------------------------------------------------------
    def _rejected_file(self):
        return config.library_root() / "rejected.json"

    def _load_rejected(self) -> set[str]:
        try:
            return set(json.loads(self._rejected_file().read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError):
            return set()

    def set_party(self, flavor: str | None) -> None:
        if flavor is not None and flavor not in party.FLAVORS:
            raise EngineError(400, f"Unknown party flavor: {flavor}")
        with LOCK:
            self.party = flavor
            self._sugg = None
            cur = self.by_id(self.playing) if self.playing else None
            keep = None
            if cur is not None and flavor:
                # Like a genre switch: the best-fitting ready song becomes next, so it's audible soon.
                fit = [s for s in self.pool if self._eligible(s) and party.fits(s, flavor)]
                if fit:
                    from .planner import score_pair
                    keep = [max(fit, key=lambda s: score_pair(cur, s) + party.bonus(cur, s)).id]
            self._save_session()
            self._recompute(keep)
            self._changed()

    def suggestions(self) -> list[dict]:
        with LOCK:
            k = (self.playing, self.party)
            if self._sugg and self._sugg[0] == k and time.time() - self._sugg[1] < 300:
                return self._sugg[2]
            cur = self.by_id(self.playing) if self.playing else None
            pool, rejected = list(self.pool), set(self.rejected)
        res = party.suggestions(cur, self.party, pool, rejected)  # network: outside the lock
        with LOCK:
            self._sugg = (k, time.time(), res)
        return res

    def accept(self, artist: str, title: str) -> None:
        """Owner accepted a suggestion: find it on YouTube, then prep it and play it next."""
        try:
            hits = fetch.search(f"{artist} {title}", 3)
        except fetch.FetchError as e:
            raise EngineError(400, str(e)) from e
        if not hits:
            raise EngineError(404, f"Couldn't find {artist} – {title} on YouTube")
        h = hits[0]
        with LOCK:
            self._sugg = None
        self.request(h.id, h.title, h.channel, h.duration)

    def reject(self, artist: str, title: str) -> None:
        with LOCK:
            self.rejected.add(party.key(artist, title))
            config.write_json_atomic(self._rejected_file(), sorted(self.rejected))
            if self._sugg:  # drop it from the cached list right away
                k, t, lst = self._sugg
                self._sugg = (k, t, [x for x in lst if party.key(x["artist"], x["title"]) != party.key(artist, title)])

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

    def set_next_effect(self, effect: str | None) -> None:
        if effect is not None and effect not in FORCEABLE:
            raise EngineError(400, f"Unknown transition effect: {effect}")
        with LOCK:
            self.next_effect = effect
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
                "library": lib}

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

    def set_genre(self, g: str) -> None:
        _known_genre(g, allow_any=True)
        with LOCK:
            self.genre = g
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
                                      for x in an.sections if x.end > ps and x.start < pe]}

    def deck(self, sid: str) -> dict:
        with LOCK:
            s = self._ready(sid)
            tm = self.maps.get(sid) or time_map_for(s, None)
        path = render(s, tm)  # blocks (Rubber Band); never under the lock
        with LOCK:
            self.maps.setdefault(sid, tm)
            return self._deck(s, tm, path)

    def transition(self, from_id: str, after: float | None, kind: str | None = None) -> dict:
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
            plan = lambda force: pick_transition(  # noqa: E731
                a, b, after=after_orig, entered=self.entered.get(a.id),
                beat_to_beat=self.beat_to_beat, force=force)
            tr = plan(kind)
            # The dropdown lists only effects that actually work for this pair (pure, cheap:
            # the planner is a handful of list scans). "blend" covers half_double too.
            options = [k for k in FORCEABLE
                       if plan(k).kind in (("blend", "half_double") if k == "blend" else (k,))]
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
                    "options": options, "forced": kind,
                    "out_at_rendered": tm_a.to_rendered(tr.out_at),
                    "in_at_rendered": tm_b.to_rendered(tr.in_at)}

