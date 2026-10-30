"""MCP server so an assistant can DJ: read what's playing, pick songs, steer genre/party/effects.

Served by autodeck itself at http://127.0.0.1:8765/mcp (streamable HTTP, mounted in server.py),
or standalone over stdio: `uv run python -m autodeck.dj_mcp`. Either way it's a thin client of
the autodeck JSON API (AUTODECK_URL, default http://127.0.0.1:8765): the browser stays the player,
the tools call the same endpoints the page uses, so the screen, the API and the MCP always agree.
(Mounted in-process, sync tools run on worker threads, so calling our own API can't deadlock.)

Tool rules (owner's MCP conventions):
- every tool declares readOnlyHint, destructiveHint and openWorldHint explicitly, plus a title;
  the defaults live in one place (`tool()` below), each tool states its own values;
- destructiveHint is true for anything not purely additive (it changes or removes state);
- openWorldHint is true when the call reaches outside this machine (YouTube, Last.fm, Deezer);
- one tool per operation, no action/mode switch parameters.
"""
import json
import os
import urllib.error
import urllib.parse
import urllib.request

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

BASE = os.environ.get("AUTODECK_URL", "http://127.0.0.1:8765").rstrip("/")

server = MCPServer(
    name="autodeck",
    title="autodeck DJ",
    instructions=(
        "You are DJing the owner's autodeck session. The browser plays the music; these tools "
        "read and steer it. Start with now_playing. Prefer play_next for 'play X' requests "
        "(search_songs first if you only have a name), set_genre / set_party to change the vibe, "
        "set_next_effect to choose how the next transition sounds, mix_now to move on early. "
        "Songs must be 'ready' (downloaded + analysed) to play; play_next preps them first."
    ),
)


def tool(title: str, *, read_only: bool = False, destructive: bool = True, open_world: bool = False):
    """Register a tool with ALL three hints set explicitly (+ title). Defaults are the
    cautious ones (a write that changes state, local only); read tools override them."""
    return server.tool(title=title, annotations=ToolAnnotations(
        title=title, read_only_hint=read_only, destructive_hint=destructive, open_world_hint=open_world))


def _api(path: str, method: str = "GET", body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read()).get("error") or str(e)
        except ValueError:
            msg = str(e)
        # ToolError: an anticipated failure whose text reaches the assistant (any other
        # exception is reported as an opaque crash by the MCP SDK).
        raise ToolError(msg) from None
    except urllib.error.URLError as e:
        raise ToolError(f"autodeck isn't running at {BASE} ({e.reason}). Start it with "
                           "`uv run python -m autodeck` in ~/gits/bvdr/autodeck.") from None


def _song(v: dict) -> dict:
    keep = ("id", "title", "channel", "status", "genre", "bpm", "camelot", "danceability", "error")
    out = {k: v.get(k) for k in keep if v.get(k) is not None}
    if isinstance(out.get("bpm"), float):
        out["bpm"] = round(out["bpm"], 1)
    return out


def _summary(s: dict) -> dict:
    pool = {p["id"]: p for p in s.get("pool", [])}
    return {
        "playing": _song(pool[s["playing"]]) if s.get("playing") in pool else None,
        "up_next": [_song(q) for q in s.get("queue", [])[:10]],
        "genre": s.get("genre"), "genres": s.get("genres"),
        "party": s.get("party"), "party_flavors": s.get("flavors"),
        "beat_to_beat": s.get("beat_to_beat"),
        "next_effect": s.get("next_effect") or "auto", "effects": s.get("effects"),
        "notice": s.get("notice"), "library_counts": s.get("pool_counts"),
    }


# --- reading --------------------------------------------------------------------
@tool("Now playing", read_only=True, destructive=False)
def now_playing() -> dict:
    """What's playing, the next 10 songs (BPM, key, genre, danceability), and the active
    genre / party flavor / beat-to-beat mode / forced next effect."""
    return _summary(_api("/api/state"))


@tool("Browse library", read_only=True, destructive=False)
def list_library(query: str = "", status: str = "", limit: int = 30) -> list[dict]:
    """Songs in the library. `query` filters title/channel (case-insensitive); `status` one of
    queued, downloading, analyzing, ready, failed."""
    q = query.lower()
    out = [_song(p) for p in _api("/api/state")["pool"]
           if (not q or q in f"{p['title']} {p['channel']}".lower()) and (not status or p["status"] == status)]
    return out[: max(1, min(limit, 200))]


@tool("Played history", read_only=True, destructive=False)
def get_history(limit: int = 20) -> list[dict]:
    """Songs played so far, newest first, with time, BPM, genre and the effect they came in with."""
    return _api(f"/api/history?limit={max(1, min(limit, 500))}")["history"]


@tool("Search YouTube", read_only=True, destructive=False, open_world=True)
def search_songs(query: str) -> list[dict]:
    """Search YouTube for songs (title, channel, duration, and status if already in the library).
    Use the returned id with play_next."""
    return _api("/api/search?" + urllib.parse.urlencode({"q": query}))["results"]


@tool("Party suggestions", read_only=True, destructive=False, open_world=True)
def get_suggestions() -> list[dict]:
    """Songs not in the library that fit what's playing and the party flavor (Last.fm), with
    BPM from Deezer and whether they beat-match the current song ('mixes')."""
    return _api("/api/suggestions")["suggestions"]


# --- steering (change state: destructiveHint=True) -------------------------------
@tool("Play next", open_world=True)
def play_next(song_id: str, title: str = "", channel: str = "", duration: float = 0.0) -> dict:
    """Queue a song to play next (adds it to the library if new, downloads and analyses it
    first). `song_id` is a YouTube video id from search_songs or list_library."""
    return _summary(_api("/api/request", "POST", {"id": song_id, "title": title,
                                                  "channel": channel, "duration": duration}))


@tool("Mix now")
def mix_now() -> dict:
    """Move to the next song at the next phrase boundary instead of waiting for the outro."""
    return _summary(_api("/api/mix_now", "POST", {}))


@tool("Set next transition effect")
def set_next_effect(effect: str = "") -> dict:
    """Force how the next transition sounds; empty = let the app choose. Effects: blend,
    bass_swap, filter_sweep (beat-matched), echo, loop_roll, brake, backspin, reverb, gate,
    riser, dropswap, cut, fade. An effect that can't work for the song pair falls back to the
    app's choice. Applies only to the current song's exit."""
    return _summary(_api("/api/next_effect", "POST", {"effect": effect or None}))


@tool("Set genre")
def set_genre(genre: str) -> dict:
    """Play only this genre from now on ('Any' for everything). See now_playing for the list."""
    return _summary(_api("/api/genre", "POST", {"genre": genre}))


@tool("Start party mode")
def start_party(flavor: str) -> dict:
    """Party flavor: hits, oldies or latino. Plays fitting library songs, crowd-pleasers first,
    and replaces the genre filter while on."""
    return _summary(_api("/api/party", "POST", {"flavor": flavor}))


@tool("Stop party mode")
def stop_party() -> dict:
    """Turn party mode off (back to the genre filter)."""
    return _summary(_api("/api/party", "POST", {"flavor": None}))


@tool("Set beat-to-beat mode")
def set_beat_to_beat(enabled: bool) -> dict:
    """On: transitions skip the outgoing outro and incoming intro (main part into main part)."""
    return _summary(_api("/api/mode", "POST", {"beat_to_beat": enabled}))


@tool("Accept suggestion", open_world=True)
def accept_suggestion(artist: str, title: str) -> dict:
    """Add a suggested song (from get_suggestions): finds it on YouTube, preps it, plays it next."""
    return _summary(_api("/api/suggestions/accept", "POST", {"artist": artist, "title": title}))


@tool("Reject suggestion")
def reject_suggestion(artist: str, title: str) -> list[dict]:
    """Never suggest this song again. Returns the remaining suggestions."""
    return _api("/api/suggestions/reject", "POST", {"artist": artist, "title": title})["suggestions"]


@tool("Set a song's genre")
def set_song_genre(song_id: str, genre: str = "") -> dict:
    """Fix a song's genre label (empty = back to the app's guess)."""
    return _summary(_api(f"/api/song/{song_id}", "POST", {"genre": genre or None}))


@tool("Remove song")
def remove_song(song_id: str) -> dict:
    """Remove a song from the library/queue (its cached files stay on disk; re-adding restores it)."""
    return _summary(_api(f"/api/song/{song_id}", "DELETE"))


@tool("Add songs by URL", destructive=False, open_world=True)
def add_songs(url: str, genre: str = "") -> dict:
    """Add a YouTube / YouTube Music song or playlist URL to the library (optionally 'import as' a
    genre). Purely additive: songs are queued for download; nothing playing changes."""
    return _api("/api/add", "POST", {"url": url, "genre": genre or None})


def main() -> None:
    server.run("stdio")


if __name__ == "__main__":
    main()
