"""Rolling-window background worker: downloads + analyses the songs the mix needs next."""
import logging
import random
import threading
from collections.abc import Callable

from . import analyze, config, fetch, genre, library
from .library import Song

log = logging.getLogger(__name__)


class Worker:
    # ponytail: one thread, one song at a time. If prep falls behind playback,
    # use 2 threads (download in parallel with analyze).
    def __init__(self, get_state: Callable[[], tuple[list[Song], list[str], str, set[str]]],
                 on_change: Callable[[], None] = lambda: None, seed: int = 0):
        # get_state -> (pool, upcoming ids, active genre, played ids); the server owns all four.
        self.get_state = get_state
        self.on_change = on_change
        # Fixed seed per instance: the "rest of the queue" order is random but reproducible in tests.
        self._rng = random.Random(seed)
        self._wake = threading.Event()
        self._stop = False
        self._thread: threading.Thread | None = None

    # --- selection (pure apart from the seeded RNG) ---------------------------
    def wanted(self, pool: list[Song], upcoming: list[str], genre_name: str,
               played: set[str] = frozenset()) -> list[str]:
        """Ids to prepare, in priority order: upcoming, genre matches, the rest. Max WINDOW_SIZE.

        Look-ahead cap: once WINDOW_SIZE ready, unplayed songs of the active genre exist,
        only not-yet-ready upcoming songs are prepared (the spec says ~10 ahead, not the pool).
        """
        by_id = {s.id: s for s in pool}
        # downloading/analyzing count as todo: after a crash they'd otherwise be stuck
        # forever; both steps are resumable (download is idempotent, analysis re-runs).
        todo = ("queued", "downloading", "analyzing")  # ready/failed never need work
        out = [i for i in upcoming if i in by_id and by_id[i].status in todo]
        ahead = sum(1 for s in pool if s.status == "ready" and s.id not in played
                    and (genre_name == config.ANY_GENRE or s.effective_genre() == genre_name))
        total = sum(1 for s in pool if s.status == "ready" and s.id not in played)
        if ahead >= config.WINDOW_SIZE or total >= config.MAX_READY_AHEAD:
            return out[: config.WINDOW_SIZE]
        seen = set(out)
        queued = [s for s in pool if s.status in todo and s.id not in seen]
        names = config.genres()

        def matches(s: Song) -> bool:
            # Metadata hint covers songs we haven't analysed yet (no genre_guess).
            return genre_name in (s.effective_genre(), genre.metadata_hint(s.title, s.channel, names))

        hit = [s.id for s in queued if matches(s)]
        rest = [s.id for s in queued if not matches(s)]
        self._rng.shuffle(rest)
        return (out + hit + rest)[: config.WINDOW_SIZE]

    # --- thread control --------------------------------------------------------
    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True, name="autodeck-worker")
        self._thread.start()

    def poke(self) -> None:
        """Server calls this on add / genre change so the worker re-reads state now."""
        self._wake.set()

    def stop(self) -> None:
        self._stop = True
        self._wake.set()
        if self._thread:
            # Bounded: a download/analyze in flight can't be interrupted. The thread is a
            # daemon, so giving up here never blocks interpreter exit.
            self._thread.join(timeout=5)

    def _run(self) -> None:
        while not self._stop:
            try:
                pool, upcoming, active, played = self.get_state()
                # Selection is purely status-based: every _prep ends ready/failed, so no
                # "done" set is needed and retry/re-add (status back to queued) just works.
                ids = self.wanted(pool, upcoming, active, played)
                if not ids:
                    # Idle: sleep until poked; the timeout is a safety net for missed pokes.
                    self._wake.wait(2)
                    self._wake.clear()
                    continue
                self._prep(next(s for s in pool if s.id == ids[0]))
            except Exception:  # never let the thread die
                log.exception("worker loop error")
                self._wake.wait(2)

    # --- one song --------------------------------------------------------------
    def _set(self, song: Song, status: str, error: str | None = None, analysis=None) -> None:
        with library.LOCK:  # short: only field updates + save
            song.status, song.error = status, error
            if analysis is not None:
                song.analysis = analysis
            # The server may have removed this song during a long download; saving now
            # would resurrect its song.json. Identity check against the live pool.
            if any(s is song for s in self.get_state()[0]):
                library.save_song(song)
        self.on_change()

    def _peaks(self, audio) -> None:
        out = library.song_dir(audio.parent.name) / "peaks.json"
        if not out.exists():
            analyze.write_peaks(audio, out)

    def _prep(self, song: Song) -> None:
        audio = library.song_dir(song.id) / "audio.flac"
        saved = library.load_song(song.id) if audio.exists() else None
        # load_song drops analyses with a stale version, so a hit here is current.
        if saved and saved.analysis:
            try:
                self._peaks(audio)
            except Exception:
                log.exception("peaks failed for %s", song.id)
            self._set(song, "ready", analysis=saved.analysis)
            return
        try:
            self._set(song, "downloading")
            audio = fetch.download(song)
            self._set(song, "analyzing")
            result = analyze.analyze(audio, song.title, song.channel)
            self._peaks(audio)
        except fetch.FetchError as e:
            self._set(song, "failed", str(e))
            return
        except Exception as e:
            log.exception("prep failed for %s", song.id)
            self._set(song, "failed", f"analysis failed: {e}"[:200])
            return
        self._set(song, "ready", analysis=result)
