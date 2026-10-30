"""FastAPI app: JSON API + audio files + static UI. State and queue rules live in engine.py."""
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware
from pydantic import BaseModel

from . import library
from .engine import Engine, EngineError

WEB = Path(__file__).resolve().parent.parent / "web"
# YouTube ids are exactly 11 chars of [A-Za-z0-9_-]. Checking this before any path
# is built means an id can never contain "/" or "..".
ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
# Only the source and our renders are servable; song.json, peaks.json etc. are not.
AUDIO_RE = re.compile(r"^(audio\.flac|render-[0-9a-f]+\.wav)$")


class AddReq(BaseModel):
    url: str
    genre: str | None = None


class PlayingReq(BaseModel):
    id: str
    plan_id: str | None = None  # the /api/transition plan the player actually started


class GenreReq(BaseModel):
    genre: str


class SongGenreReq(BaseModel):
    genre: str | None = None


class GenresReq(BaseModel):
    genres: list[str]


class QueueReq(BaseModel):
    ids: list[str]
    pin: str | None = None


def _id(sid: str) -> str:
    if not ID_RE.match(sid):
        raise HTTPException(404, f"No such song: {sid}")
    return sid


def create_app(engine: Engine | None = None) -> FastAPI:
    """Factory so tests can inject an Engine built on a temp library."""
    eng = engine or Engine()

    @asynccontextmanager
    async def lifespan(_app):
        # Only runs under a real server (or `with TestClient(...)`), so plain tests
        # never get a background thread.
        eng.worker.start()
        try:
            yield
        finally:
            eng.worker.stop()

    app = FastAPI(lifespan=lifespan)
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

    @app.post("/api/playing")
    def playing(req: PlayingReq):
        eng.set_playing(_id(req.id), req.plan_id)
        return eng.state()

    @app.post("/api/genre")
    def genre(req: GenreReq):
        eng.set_genre(req.genre)
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

    @app.delete("/api/song/{sid}")
    def delete(sid: str):
        eng.remove(_id(sid))
        return eng.state()

    @app.get("/api/deck/{sid}")
    def deck(sid: str):
        return eng.deck(_id(sid))

    @app.get("/api/transition")
    # `from` is a Python keyword, hence the alias.
    def transition(from_: Annotated[str, Query(alias="from")], after: float | None = None):
        return eng.transition(_id(from_), after)

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
        app.mount("/", StaticFiles(directory=WEB, html=True), name="web")
    return app

