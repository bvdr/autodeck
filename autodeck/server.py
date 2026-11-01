"""FastAPI app: JSON API + audio files + static UI. State and queue rules live in engine.py."""
import logging
import re
import urllib.parse
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware
from pydantic import BaseModel, Field

from . import dj_mcp, library, mcp_activity
from .engine import Engine, EngineError

WEB = Path(__file__).resolve().parent.parent / "web"
# YouTube ids are exactly 11 chars of [A-Za-z0-9_-]. Checking this before any path
# is built means an id can never contain "/" or "..".
ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
# Only the source and our renders are servable; song.json, peaks.json etc. are not.
AUDIO_RE = re.compile(r"^(audio\.(opus|flac)|render-[0-9a-f]+(-vocals|-instrumental)?\.wav)$")


log = logging.getLogger(__name__)


class FreshStaticFiles(StaticFiles):
    """The web app, always revalidated. Without Cache-Control, WebKit (the desktop window keeps a
    disk cache) heuristically reuses old copies of modules: after an update the window ran a new
    app.js against a stale deck.js (2026-11-01: no stems, no section strip). no-cache still lets it
    use its copy when the ETag matches, so a reload costs one 304 per file on localhost."""

    def file_response(self, *args, **kwargs):
        resp = super().file_response(*args, **kwargs)
        resp.headers["Cache-Control"] = "no-cache"
        return resp


class LogReq(BaseModel):
    msg: str = Field(max_length=4000)


class AddReq(BaseModel):
    url: str
    genre: str | None = None


class PlayingReq(BaseModel):
    id: str
    plan_id: str | None = None  # the /api/transition plan the player actually started


class RequestReq(BaseModel):
    id: str
    title: str = ""
    channel: str = ""
    duration: float = 0.0
    source: str | None = None  # "kept from" note in Up next: "search", "assistant", …


class PartyReq(BaseModel):
    flavor: str | None = None  # None = party off


class SuggestionReq(BaseModel):
    artist: str = ""
    title: str = ""
    id: str | None = None      # a library song (mood/genre draft): no YouTube search needed
    source: str | None = None  # reject/unreject: the source key it was drafted for (default: active)


class AcceptAllReq(BaseModel):
    items: list[SuggestionReq]  # in the order shown; Up next keeps it


class MoodReq(BaseModel):
    theme: str
    songs: int = 30


class EffectReq(BaseModel):
    effect: str | None = None  # None = let the app pick


class PairReq(BaseModel):
    # `from` is a Python keyword, hence the alias.
    from_: str = Field(alias="from")
    to: str


class RateReq(PairReq):
    verdict: str                # "loved" (🔥) | "avoided" (👎)
    effect: str | None = None   # the effect that hand-over used, when known
    why: str | None = None      # with "avoided": "songs" | "effect"


class WhyReq(PairReq):
    why: str


class LastfmKeyReq(BaseModel):
    key: str | None = None  # empty/None = remove


class BrowserReq(BaseModel):
    browser: str


class ModeReq(BaseModel):
    beat_to_beat: bool


class GenreReq(BaseModel):
    genre: str


class SongGenreReq(BaseModel):
    genre: str | None = None


class GenresReq(BaseModel):
    genres: list[str]


class PlaylistSaveReq(BaseModel):
    url: str
    genre: str | None = None  # "treat songs as"; None = their own genres


class PlaylistPickReq(BaseModel):
    id: str | None = None  # None = Playlist: None


class PlaylistPatchReq(BaseModel):
    # Only the fields sent change (genre: null = back to their own genres).
    name: str | None = None
    genre: str | None = None
    order: str | None = None
    at_end: str | None = None


class PlaylistSongReq(BaseModel):
    id: str


class QueueReq(BaseModel):
    ids: list[str]
    pin: str | None = None


class McpSettingsReq(BaseModel):
    # Only the fields sent change.
    enabled: bool | None = None
    mode: str | None = None  # "steer" | "read"


class McpActivityReq(BaseModel):
    # dj_mcp's report when a tool call is done (dj_mcp._report).
    call: str
    tool: str
    client: str | None = None
    ok: bool
    text: str | None = None
    song: str | None = None


def _by(x_autodeck_client: Annotated[str | None, Header()] = None) -> str | None:
    """"mcp" when an assistant made this call: dj_mcp sends X-Autodeck-Client: mcp on every API
    call of a tool. The engine tags what it changes ("via MCP"); anything else clears the tag."""
    return "mcp" if x_autodeck_client == "mcp" else None


By = Annotated[str | None, Depends(_by)]


LIST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{2,64}$")


def _list(lid: str) -> str:
    if not LIST_ID_RE.match(lid):
        raise HTTPException(404, "No saved playlist with that id")
    return lid


def _id(sid: str) -> str:
    if not ID_RE.match(sid):
        raise HTTPException(404, f"No such song: {sid}")
    return sid


def relaunch() -> None:
    """Quit and reopen the standalone app (used by Repair): a detached `open` after we're gone,
    then close the window, which stops the engine (desktop.main)."""
    import subprocess
    import threading

    from . import standalone
    app_path = standalone.resources().parent.parent  # .../autodeck.app
    subprocess.Popen(["/bin/sh", "-c", f'sleep 2; /usr/bin/open "{app_path}"'], start_new_session=True)

    def close():
        import webview
        for w in list(webview.windows):
            w.destroy()
    threading.Timer(0.5, close).start()


def create_app(engine: Engine | None = None) -> FastAPI:
    """Factory so tests can inject an Engine built on a temp library."""
    eng = engine or Engine()

    @asynccontextmanager
    async def lifespan(_app):
        # Only runs under a real server (or `with TestClient(...)`), so plain tests
        # never get a background thread.
        eng.worker.start()
        eng.start_enrichment()
        try:
            # The mounted MCP endpoint needs its session manager running for the app's lifetime.
            async with dj_mcp.server.session_manager.run():
                yield
        finally:
            eng.stop_enrichment()
            eng.worker.stop()

    async def mcp_call(request: Request) -> None:
        # Every API call an assistant's tool makes starts (or continues) its Activity log entry,
        # so the MCP button shows "working" while the tool runs. Async: no threadpool hop.
        h = request.headers
        call, tool = h.get("x-autodeck-call"), h.get("x-autodeck-tool", "")
        if h.get("x-autodeck-client") == "mcp" and call:
            doing, read = dj_mcp.PLAIN.get(tool, (tool, True))
            eng.mcp.begin(call[:64], tool[:64], urllib.parse.unquote(h.get("x-autodeck-client-name", ""))[:80] or None,
                          doing, read)

    app = FastAPI(lifespan=lifespan, dependencies=[Depends(mcp_call)])
    # DJ assistant MCP at /mcp (streamable HTTP). Its route is added straight to our router
    # (not app.mount) so the URL is exactly /mcp, no /mcp/ redirect that MCP clients may not
    # follow for POST. host=127.0.0.1 turns on the SDK's DNS-rebinding protection too.
    mcp_app = dj_mcp.server.streamable_http_app(streamable_http_path="/mcp", host="127.0.0.1")
    app.router.routes.extend(mcp_app.routes)  # before the static "/" mount below, which catches all
    app.state.engine = eng
    # DNS rebinding guard: binding 127.0.0.1 isn't enough, a malicious page can point its
    # own hostname at 127.0.0.1 and drive /api/add or DELETE. Its Host header gives it away.
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])

    # Every error body is {"error": msg}, which is what the UI shows the owner.
    @app.exception_handler(EngineError)
    async def _engine_err(_r: Request, e: EngineError):
        return JSONResponse({"error": str(e)}, status_code=e.status)

    @app.exception_handler(HTTPException)
    async def _http_err(_r: Request, e: HTTPException):
        return JSONResponse({"error": str(e.detail)}, status_code=e.status_code)

    @app.exception_handler(RequestValidationError)
    async def _bad_body(_r: Request, e: RequestValidationError):
        return JSONResponse({"error": "Bad request: " + "; ".join(str(x.get("msg")) for x in e.errors())},
                            status_code=400)

    # Endpoints that touch the network or render are plain `def`: FastAPI runs them in
    # its threadpool, so a multi-second render doesn't stall /api/state polling.
    @app.get("/api/state")
    def state():
        return eng.state()

    @app.post("/api/add")
    def add(req: AddReq):
        return {"added": eng.add(req.url, req.genre)}

    @app.post("/api/log")
    def client_log(req: LogReq):
        # The desktop window (WKWebView) has no dev tools: page errors and "why no mix" snapshots
        # land in autodeck.log, the one place the owner and a debugging session can read.
        log.warning("web: %s", req.msg)
        return {"ok": True}

    @app.post("/api/playing")
    def playing(req: PlayingReq):
        eng.set_playing(_id(req.id), req.plan_id)
        return eng.state()

    @app.get("/api/search")
    def search(q: str = ""):
        return {"results": eng.search(q)}

    @app.post("/api/request")
    def request_song(req: RequestReq):
        eng.request(_id(req.id), req.title[:300], req.channel[:300], req.duration,
                    req.source[:80] if req.source else None)
        return eng.state()

    @app.post("/api/play_next")
    def play_next(req: RequestReq, by: By):
        # The deck button / action sheet: the very next song (not "after the kept ones").
        # `play_next` tells the page what happened, for its one-line notice.
        done = eng.play_next(_id(req.id), req.title[:300], req.channel[:300], req.duration,
                             req.source[:80] if req.source else None, by)
        return {**eng.state(), "play_next": done}

    @app.post("/api/party")
    def set_party(req: PartyReq, by: By):
        eng.set_party(req.flavor, by)
        return eng.state()

    @app.get("/api/history")
    def history(limit: int = 200):
        return {"history": eng.history(max(1, min(limit, 2000)))}

    @app.get("/api/suggestions")
    def suggestions():
        # {"source": {kind, name, key, label} | None, "suggestions": [{id, artist, title, why, bpm, …}]}
        return eng.draft()

    @app.post("/api/suggestions/accept")
    def accept(req: SuggestionReq, by: By):
        eng.accept(req.artist[:200], req.title[:300], _id(req.id) if req.id else None, by)
        return eng.state()

    @app.post("/api/suggestions/accept_all")
    def accept_all(req: AcceptAllReq):
        eng.accept_all([{"artist": x.artist[:200], "title": x.title[:300], "id": _id(x.id) if x.id else None}
                        for x in req.items[:100]])
        return eng.state()

    @app.post("/api/suggestions/reject")
    def reject(req: SuggestionReq):
        eng.reject(req.artist[:200], req.title[:300], req.source)
        return eng.draft()

    @app.post("/api/suggestions/unreject")
    def unreject(req: SuggestionReq):
        eng.unreject(req.artist[:200], req.title[:300], req.source)
        return eng.draft()

    @app.post("/api/mood")
    def build_mood(req: MoodReq, by: By):
        # Plain def: playlist searches take seconds, FastAPI runs this in its threadpool.
        return eng.build_mood(req.theme, req.songs, by)

    @app.delete("/api/mood/{name}")
    def delete_mood(name: str):
        eng.delete_mood(name)
        return eng.state()

    # --- saved playlists ---------------------------------------------------------------
    @app.get("/api/playlists")
    def playlists():
        return {"playlists": eng.playlists_list()}

    @app.post("/api/playlists")
    def save_playlist(req: PlaylistSaveReq):
        # Plain def: reading the playlist on YouTube takes seconds (threadpool).
        return eng.save_playlist(req.url.strip()[:2000], req.genre)

    @app.post("/api/playlist")
    def pick_playlist(req: PlaylistPickReq, by: By):
        eng.pick_playlist(_list(req.id) if req.id is not None else None, by)
        return eng.state()

    @app.post("/api/playlists/{lid}/sync")
    def sync_playlist(lid: str):
        return eng.sync_playlist(_list(lid))

    @app.patch("/api/playlists/{lid}")
    def patch_playlist(lid: str, req: PlaylistPatchReq):
        return eng.update_playlist(_list(lid), **{k: getattr(req, k) for k in req.model_fields_set})

    @app.post("/api/playlists/{lid}/exclude")
    def exclude_playlist_song(lid: str, req: PlaylistSongReq):
        eng.exclude_playlist_song(_list(lid), _id(req.id))
        return eng.draft()

    @app.post("/api/playlists/{lid}/unexclude")
    def unexclude_playlist_song(lid: str, req: PlaylistSongReq):
        eng.exclude_playlist_song(_list(lid), _id(req.id), on=False)
        return eng.draft()

    @app.delete("/api/playlists/{lid}")
    def remove_playlist(lid: str):
        return {**eng.remove_playlist(_list(lid)), "state": eng.state()}

    @app.post("/api/next_effect")
    def next_effect(req: EffectReq, by: By):
        eng.set_next_effect(req.effect, by)
        return eng.state()

    @app.post("/api/mix_now")
    def mix_now():
        eng.mix_now()
        return eng.state()

    # Pair memory (design xWVJy). One endpoint per operation, like the MCP tools.
    @app.post("/api/pair")
    def pair(req: RateReq):
        eng.rate_pair(_id(req.from_), _id(req.to), req.verdict, req.effect, req.why)
        return eng.state()

    @app.post("/api/pair/why")
    def pair_why(req: WhyReq):
        eng.pair_why(_id(req.from_), _id(req.to), req.why)
        return eng.state()

    @app.post("/api/pair/undo")
    def pair_undo(req: PairReq):
        eng.undo_pair(_id(req.from_), _id(req.to))
        return eng.state()

    @app.post("/api/pair/forget")
    def pair_forget(req: PairReq):
        eng.forget_pair(_id(req.from_), _id(req.to))
        return eng.state()

    @app.post("/api/pair/play")
    def pair_play(req: PairReq):
        eng.play_pair(_id(req.from_), _id(req.to))
        return eng.state()

    @app.get("/api/pairs")
    def pairs():
        return {"pairs": eng.pairs_list()}

    @app.get("/api/settings")
    def get_settings():
        return eng.settings()

    @app.put("/api/settings/lastfm")
    def put_lastfm(req: LastfmKeyReq):
        return eng.set_lastfm_key(req.key)

    @app.put("/api/settings/youtube")
    def put_youtube(req: BrowserReq):
        return eng.set_youtube_browser(req.browser)

    # --- MCP (design m6p10): the owner's switches and the assistants' Activity log -----------
    @app.put("/api/mcp/settings")
    def mcp_settings(req: McpSettingsReq):
        try:
            mcp_activity.set_switches(req.enabled, req.mode)
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        return {"mcp": eng.mcp.state()}

    @app.post("/api/mcp/seen")
    def mcp_seen():
        eng.mcp.seen()  # the owner opened the MCP settings: the red error has been seen
        return {"mcp": eng.mcp.state()}

    @app.post("/api/mcp/activity")
    def mcp_activity_done(req: McpActivityReq):
        doing, read = dj_mcp.PLAIN.get(req.tool, (req.tool, True))
        eng.mcp.finish(req.call[:64], req.tool[:64], (req.client or "")[:80] or None, doing, read, req.ok,
                       (req.text or "")[:300] or None, req.song if req.song and ID_RE.match(req.song) else None)
        return {"ok": True}

    @app.post("/api/setup/repair")
    def repair():
        r = eng.repair_install()
        relaunch()
        return r

    @app.post("/api/mode")
    def mode(req: ModeReq):
        eng.set_beat_to_beat(req.beat_to_beat)
        return eng.state()

    @app.post("/api/genre")
    def genre(req: GenreReq, by: By):
        eng.set_genre(req.genre, by)
        return eng.state()

    @app.put("/api/genres")
    def genres(req: GenresReq):
        eng.set_genres(req.genres)
        return eng.state()

    @app.post("/api/queue")
    def queue(req: QueueReq):
        eng.set_queue(req.ids, req.pin)
        return eng.state()

    @app.post("/api/song/{sid}")
    def song_genre(sid: str, req: SongGenreReq):
        eng.set_song_genre(_id(sid), req.genre)
        return eng.state()

    @app.post("/api/song/{sid}/retry")
    def retry(sid: str):
        eng.retry(_id(sid))
        return eng.state()

    @app.delete("/api/song/{sid}")
    def delete(sid: str):
        eng.remove(_id(sid))
        return eng.state()

    @app.get("/api/deck/{sid}")
    def deck(sid: str):
        return eng.deck(_id(sid))

    @app.get("/api/stem/{sid}/{name}")
    def stem(sid: str, name: str):
        # The ON AIR deck's Voice/Beat switch: that stem, rendered with the song's current time map.
        return eng.stem(_id(sid), name)

    @app.get("/api/transition")
    # `from` is a Python keyword, hence the alias.
    def transition(from_: Annotated[str, Query(alias="from")], after: float | None = None,
                   kind: str | None = None, enter: int | None = None, to: str | None = None):
        # kind: the owner's forced effect from the dropdown (planner.FORCEABLE), else auto.
        # enter/to: the cued song (`to`) comes in at its section number `enter` (cued-deck click).
        return eng.transition(_id(from_), after, kind, enter, to)

    @app.get("/audio/{sid}/{name}")
    def audio(sid: str, name: str):
        if not AUDIO_RE.match(name):
            raise HTTPException(404, "Not found")
        d = library.song_dir(_id(sid)).resolve()
        p = (d / name).resolve()
        # Belt and braces on top of both regexes: the file must really sit in song_dir.
        if p.parent != d or not p.is_file():
            raise HTTPException(404, "Not found")
        return FileResponse(p)

    # Mounted last so it never shadows /api or /audio.
    if WEB.is_dir():
        app.mount("/", FreshStaticFiles(directory=WEB, html=True), name="web")
    return app

