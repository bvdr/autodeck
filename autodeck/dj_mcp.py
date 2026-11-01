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

The owner sees what assistants do (design m6p10): every call goes through `_DJ.call_tool`, the
one place that checks the owner's switches (Off refuses everything, Only read refuses every tool
whose readOnlyHint is false) and reports the call to the app's Activity log (mcp_activity.py).
The tool's API calls carry X-Autodeck-* headers (`_headers`), so the app tags what an assistant
changed ("via MCP"). Each steering tool `say`s one plain sentence, built from its result.
"""
import contextvars
import json
import os
import urllib.error
import urllib.parse
import urllib.request
import uuid

import anyio
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from .mcp_activity import switches

BASE = os.environ.get("AUTODECK_URL", "http://127.0.0.1:8765").rstrip("/")

# Per tool: (what it does in plain words, read-only). The button says "Claude · playing next",
# never a tool name; a read's words name it in the log's folded "Show N reads (…)" line.
PLAIN: dict[str, tuple[str, bool]] = {}
# The tool call in progress: {id, tool, client, text, song}. Set in call_tool; the sync tool body
# runs on a worker thread that gets a copy of this context (anyio), and mutates the same dict.
_call: contextvars.ContextVar[dict | None] = contextvars.ContextVar("autodeck_mcp_call", default=None)

OFF = ("MCP is turned off in autodeck. The owner can turn it back on with “Let assistants DJ” "
       "in autodeck's MCP settings.")
ONLY_READ = ("autodeck is set to Only read: assistants can see what's playing, the library, history "
             "and suggestions, but not change anything (this was {doing}). Ask the owner to switch "
             "MCP to “Steer the session” in autodeck's MCP settings.")


def _client(ctx) -> str:
    """The assistant's name from the MCP session's clientInfo: its title, else the name made
    readable ("claude-code" -> "Claude Code"). In-process calls (tests) have no session."""
    try:
        info = ctx.session.client_params.client_info
    except (AttributeError, ValueError):  # no context / request context / handshake info
        return "An assistant"
    return info.title or info.name.replace("-", " ").replace("_", " ").title()


class _DJ(MCPServer):
    async def call_tool(self, name, arguments, context=None):
        if name not in PLAIN:
            return await super().call_tool(name, arguments, context)  # the SDK's unknown-tool error
        doing, read = PLAIN[name]
        enabled, mode = switches()
        if not enabled:
            raise ToolError(OFF)  # and nothing else happens: not even a log entry
        c = {"id": uuid.uuid4().hex, "tool": name, "client": _client(context), "text": None, "song": None}
        token = _call.set(c)
        ok = False
        try:
            if mode == "read" and not read:
                c["text"] = f"Refused {doing}: MCP is set to Only read"
                raise ToolError(ONLY_READ.format(doing=doing))
            try:
                res = await super().call_tool(name, arguments, context)
            except ToolError as e:
                # The SDK wraps the tool's own ToolError ("Error executing tool …"); the owner reads
                # the reason itself. Anything else (bad arguments, a crash) gets a plain line.
                c["text"] = str(e.__cause__) if isinstance(e.__cause__, ToolError) else f"Couldn't finish {doing}"
                raise
            ok = True
            return res
        finally:
            await anyio.to_thread.run_sync(_report, c, ok)
            _call.reset(token)


def _report(c: dict, ok: bool) -> None:
    try:
        _api("/api/mcp/activity", "POST", {"call": c["id"], "tool": c["tool"], "client": c["client"],
                                           "ok": ok, "text": c["text"], "song": c["song"]})
    except ToolError:
        pass  # the log is for the owner; failing to write it must never fail the assistant's call


def say(text: str, song: str | None = None) -> None:
    """A steering tool's one plain sentence for the owner's log and toast ("Put X next"), plus
    the song it's about (the toast's thumbnail). Built from the result, so it says what happened."""
    c = _call.get()
    if c is not None:
        c["text"], c["song"] = text, song


def _headers() -> dict:
    """Marks the app's API calls as an assistant's: who (client), which call (one log entry per
    tool call) and which tool. The app tags what these calls change with by: "mcp"."""
    c = _call.get()
    if c is None:
        return {}
    return {"X-Autodeck-Client": "mcp", "X-Autodeck-Call": c["id"], "X-Autodeck-Tool": c["tool"],
            # Headers are latin-1: a client name is free text, so it travels percent-encoded.
            "X-Autodeck-Client-Name": urllib.parse.quote(c["client"])}


server = _DJ(
    name="autodeck",
    title="autodeck DJ",
    instructions=(
        "You are DJing the owner's autodeck session. The browser plays the music; these tools "
        "read and steer it. Start with now_playing. Prefer play_next for 'play X' requests "
        "(search_songs first if you only have a name), set_genre / set_party to change the vibe, "
        "play_from_playlist to play a saved YouTube playlist (list_playlists), "
        "set_next_effect to choose how the next transition sounds, mix_now to move on early. "
        "Songs must be 'ready' (downloaded + analysed) to play; play_next preps them first."
    ),
)


def tool(title: str, doing: str, *, read_only: bool = False, destructive: bool = True, open_world: bool = False):
    """Register a tool with ALL three hints set explicitly (+ title). Defaults are the
    cautious ones (a write that changes state, local only); read tools override them.
    `doing`: the plain words the owner sees while it runs ("playing next")."""
    def register(fn):
        PLAIN[fn.__name__] = (doing, read_only)
        return server.tool(title=title, annotations=ToolAnnotations(
            title=title, read_only_hint=read_only, destructive_hint=destructive, open_world_hint=open_world))(fn)
    return register


def _api(path: str, method: str = "GET", body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={**({"Content-Type": "application/json"} if data else {}), **_headers()})
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


def _title(s: dict, sid: str) -> str:
    return next((p["title"] for p in s.get("pool", []) if p["id"] == sid), sid)


# The page's names for the effects (web/ui.js FX_LABEL), so the log reads like the screen.
FX_NAME = {"blend": "Beat blend", "half_double": "Beat blend (½×/2×)", "bass_swap": "Bass swap",
           "filter_sweep": "Filter sweep", "vocal_swap": "Vocal swap", "echo": "Echo out", "loop_roll": "Loop roll", "brake": "Brake",
           "backspin": "Backspin", "reverb": "Reverb wash", "gate": "Gate / stutter", "riser": "Riser → drop",
           "dropswap": "Drop swap", "cut": "Cut", "fade": "Fade"}
songs_n = lambda n: f"{n} song{'' if n == 1 else 's'}"  # noqa: E731


def _summary(s: dict) -> dict:
    pool = {p["id"]: p for p in s.get("pool", [])}
    return {
        "playing": _song(pool[s["playing"]]) if s.get("playing") in pool else None,
        # kept = the owner's (or your) pick; otherwise an auto pick that keeps the music going.
        "up_next": [{**_song(q), "kept": bool(q.get("kept")), **({"kept_from": q["kept_from"]} if q.get("kept_from") else {})}
                    for q in s.get("queue", [])[:10]],
        "genre": s.get("genre"), "genres": s.get("genres"), "moods": s.get("moods"),
        "party": s.get("party"), "party_flavors": s.get("flavors"),
        "playlist": _pl(s["playlist"]) if s.get("playlist") else None,
        "beat_to_beat": s.get("beat_to_beat"),
        "next_effect": s.get("next_effect") or "auto", "effects": s.get("effects"),
        "notice": s.get("notice"), "library_counts": s.get("pool_counts"),
    }


# --- reading --------------------------------------------------------------------
@tool("Now playing", "now playing", read_only=True, destructive=False)
def now_playing() -> dict:
    """What's playing, the next 10 songs (BPM, key, genre, danceability; kept = picked by the owner
    or you, else an auto pick from the active source), and the active
    genre / party flavor / beat-to-beat mode / forced next effect."""
    return _summary(_api("/api/state"))


@tool("Browse library", "library", read_only=True, destructive=False)
def list_library(query: str = "", status: str = "", limit: int = 30) -> list[dict]:
    """Songs in the library. `query` filters title/channel (case-insensitive); `status` one of
    queued, downloading, analyzing, ready, failed."""
    q = query.lower()
    out = [_song(p) for p in _api("/api/state")["pool"]
           if (not q or q in f"{p['title']} {p['channel']}".lower()) and (not status or p["status"] == status)]
    return out[: max(1, min(limit, 200))]


@tool("Played history", "history", read_only=True, destructive=False)
def get_history(limit: int = 20) -> list[dict]:
    """Songs played so far, newest first, with time, BPM, genre and the effect they came in with."""
    return _api(f"/api/history?limit={max(1, min(limit, 500))}")["history"]


@tool("Search YouTube", "search", read_only=True, destructive=False, open_world=True)
def search_songs(query: str) -> list[dict]:
    """Search YouTube for songs (title, channel, duration, and status if already in the library).
    Use the returned id with play_next."""
    return _api("/api/search?" + urllib.parse.urlencode({"q": query}))["results"]


@tool("Suggestions", "suggestions", read_only=True, destructive=False, open_world=True)
def get_suggestions() -> list[dict]:
    """The draft for the active source. Party: songs not in the library that fit what's playing
    and the flavor (Last.fm), with BPM from Deezer and 'mixes' (beat-matches the current song).
    Mood/genre: library songs of it not in Up next yet (these carry an id). Empty with no source."""
    return _api("/api/suggestions")["suggestions"]


# --- steering (change state: destructiveHint=True) -------------------------------
@tool("Play next", "playing next", open_world=True)
def play_next(song_id: str, title: str = "", channel: str = "", duration: float = 0.0) -> dict:
    """Play a song as the very next one, on the idle deck (same as the owner's deck button). The
    song planned there moves back to the top of Up next; a running mix is never interrupted (the
    song plays right after it). A song not ready yet (added to the library if new, then downloaded
    and analysed) waits at the top and swaps in once ready; `play_next.ready` says which.
    `song_id` is a YouTube video id from search_songs or list_library."""
    s = _api("/api/play_next", "POST", {"id": song_id, "title": title, "channel": channel,
                                        "duration": duration, "source": "assistant"})
    p = s["play_next"]
    t = _title(s, p["id"])
    say(f"Put {t} next" if p["ready"] else f"Getting {t} ready to play next", p["id"])
    return {**_summary(s), "play_next": p}


@tool("Mix now", "mixing now")
def mix_now() -> dict:
    """Move to the next song at the next phrase boundary instead of waiting for the outro."""
    s = _api("/api/mix_now", "POST", {})
    nxt = (s.get("queue") or [None])[0]
    say(f"Mixing into {nxt['title']} now" if nxt else "Mixing into the next song now", nxt and nxt["id"])
    return _summary(s)


@tool("Set next transition effect", "picking the next effect")
def set_next_effect(effect: str = "") -> dict:
    """Force how the next transition sounds; empty = let the app choose. Effects: blend,
    bass_swap, filter_sweep, vocal_swap (beat-matched; vocal_swap drops the playing song's vocals as
    the next one comes in, and needs its vocal/instrumental stems), echo, loop_roll, brake, backspin,
    reverb, gate, riser, dropswap, cut, fade. An effect that can't work for the song pair falls back
    to the app's choice. Applies only to the current song's exit."""
    s = _api("/api/next_effect", "POST", {"effect": effect or None})
    fx = s.get("next_effect")
    say(f"Set the next effect to {FX_NAME.get(fx, fx)}" if fx else "Let autodeck pick the next effect")
    return _summary(s)


@tool("Set genre", "switching the vibe")
def set_genre(genre: str) -> dict:
    """Play only this genre from now on ('Any' for everything). See now_playing for the list."""
    s = _api("/api/genre", "POST", {"genre": genre})
    say(f"Switched the vibe to {s['genre']}")
    return _summary(s)


@tool("Start party mode", "starting a party")
def start_party(flavor: str) -> dict:
    """Party flavor: hits, oldies or latino. Plays fitting library songs, crowd-pleasers first,
    and replaces the genre filter while on."""
    s = _api("/api/party", "POST", {"flavor": flavor})
    say(f"Started the {s['party'].title()} party")
    return _summary(s)


@tool("Stop party mode", "stopping the party")
def stop_party() -> dict:
    """Turn party mode off (back to the genre filter)."""
    s = _api("/api/party", "POST", {"flavor": None})
    say("Turned party mode off")
    return _summary(s)


@tool("Set beat-to-beat mode", "switching beat to beat")
def set_beat_to_beat(enabled: bool) -> dict:
    """On: transitions skip the outgoing outro and incoming intro (main part into main part)."""
    s = _api("/api/mode", "POST", {"beat_to_beat": enabled})
    say(f"Turned beat to beat {'on' if s['beat_to_beat'] else 'off'}")
    return _summary(s)


@tool("Accept suggestion", "keeping a suggestion", open_world=True)
def accept_suggestion(artist: str, title: str) -> dict:
    """Add a suggested song (from get_suggestions): finds it on YouTube, preps it, plays it next."""
    s = _api("/api/suggestions/accept", "POST", {"artist": artist, "title": title})
    # The app doesn't say which YouTube video it matched, so this one names the request.
    say(f"Kept {title} from the suggestions")
    return _summary(s)


@tool("Reject suggestion", "dropping a suggestion")
def reject_suggestion(artist: str, title: str) -> list[dict]:
    """Never suggest this song again. Returns the remaining suggestions."""
    r = _api("/api/suggestions/reject", "POST", {"artist": artist, "title": title})["suggestions"]
    say(f"Won't suggest {title} again")
    return r


@tool("Set a song's genre", "fixing a genre")
def set_song_genre(song_id: str, genre: str = "") -> dict:
    """Fix a song's genre label (empty = back to the app's guess)."""
    s = _api(f"/api/song/{song_id}", "POST", {"genre": genre or None})
    say(f"Set the genre of {_title(s, song_id)} to {genre or 'auto'}", song_id)
    return _summary(s)


@tool("Remove song", "removing a song")
def remove_song(song_id: str) -> dict:
    """Remove a song from the library/queue (its cached files stay on disk; re-adding restores it)."""
    s = _api(f"/api/song/{song_id}", "DELETE")
    say("Removed a song from the library", song_id)  # gone from the pool, so no title to read
    return _summary(s)


@tool("Build or refresh a mood", "building a mood", open_world=True)
def build_mood(theme: str, songs: int = 30) -> dict:
    """Create a mood from a theme ("Balkan party", "Greek taverna", "90s eurodance"): the songs that
    appear across popular public YouTube/Deezer playlists on it are imported and the mix switches to
    it. Building an existing mood again adds more songs. Takes ~10-30 s."""
    r = _api("/api/mood", "POST", {"theme": theme, "songs": songs})
    say(f"Built the mood {r['mood']} ({songs_n(r['found'])})")
    return r


@tool("Delete a mood", "deleting a mood")
def delete_mood(name: str) -> dict:
    """Forget a mood (its songs stay in the library); the mix returns to Any if it was active."""
    s = _api(f"/api/mood/{urllib.parse.quote(name)}", "DELETE")
    say(f"Deleted the mood {name}")
    return _summary(s)


@tool("Add songs by URL", "adding songs", destructive=False, open_world=True)
def add_songs(url: str, genre: str = "") -> dict:
    """Add a YouTube / YouTube Music song or playlist URL to the library (optionally 'import as' a
    genre). Purely additive: songs are queued for download; nothing playing changes."""
    r = _api("/api/add", "POST", {"url": url, "genre": genre or None})
    say(f"Added {songs_n(r['added'])} to the library")
    return r


# --- saved playlists ------------------------------------------------------------------
def _pl(p: dict) -> dict:
    keep = ("id", "name", "count", "ready", "failed", "order", "at_end", "genre", "url")
    return {k: p.get(k) for k in keep}


@tool("List saved playlists", "playlists", read_only=True, destructive=False)
def list_playlists() -> list[dict]:
    """Saved YouTube playlists, newest first: id, name, song count, how many are ready, order
    (playlist / mix) and what happens at the end (keep_going / repeat / stop)."""
    return [_pl(p) for p in _api("/api/playlists")["playlists"]]


@tool("Save a YouTube playlist", "saving a playlist", destructive=False, open_world=True)
def save_playlist(url: str, genre: str = "") -> dict:
    """Save a YouTube / YouTube Music playlist link (youtube.com/playlist?list=…) so it can be
    played from later; its songs are added to the library and download from the top. `genre`
    treats every song as that genre (owner-set genres still win). Saving one that's saved
    already returns it (existing=true). Purely additive; doesn't change what's playing."""
    r = _api("/api/playlists", "POST", {"url": url, "genre": genre or None})
    say(f"Saved the playlist {r['playlist']['name']} ({songs_n(r['playlist']['count'])})")
    return {**{k: r[k] for k in ("existing", "found", "added") if k in r}, "playlist": _pl(r["playlist"])}


@tool("Play from a saved playlist", "playing a playlist", open_world=True)
def play_from_playlist(playlist_id: str) -> dict:
    """Make a saved playlist (id from list_playlists) the source: Up next follows it, Vibe goes to
    Any and Party to Off. Re-reads it from YouTube first if the last sync is over a day old."""
    pl = next((p for p in _api("/api/playlists")["playlists"] if p["id"] == playlist_id), None)
    if pl is None:
        raise ToolError(f"No saved playlist {playlist_id}. See list_playlists.")
    if pl.get("stale"):
        _api(f"/api/playlists/{playlist_id}/sync", "POST", {})
    s = _api("/api/playlist", "POST", {"id": playlist_id})
    say(f"Playing the {s['playlist']['name']} playlist")
    return _summary(s)


@tool("Sync a saved playlist", "syncing a playlist", open_world=True)
def sync_playlist(playlist_id: str) -> dict:
    """Re-read a saved playlist on YouTube. New songs are added at their YouTube position; songs
    removed there leave the playlist (they stay in the library). Returns the new positions."""
    r = _api(f"/api/playlists/{playlist_id}/sync", "POST", {})
    say(f"Synced {r['playlist']['name']}: {len(r['added'])} new, {r['removed']} gone")
    return {"added_at": r["added"], "removed": r["removed"], "playlist": _pl(r["playlist"])}


@tool("Remove a saved playlist", "removing a playlist")
def remove_playlist(playlist_id: str) -> dict:
    """Forget a saved playlist (its songs stay in the library). If it was playing, the source
    falls back to Vibe: Any and the music keeps going."""
    r = _api(f"/api/playlists/{playlist_id}", "DELETE")
    say(f"Removed the playlist {r['name']}; its {songs_n(r['count'])} stay")
    return {"removed": r["name"], "songs_kept": r["count"], "was_playing": r["was_playing"]}


@tool("Love a pair", "loving a mix")
def love_pair(from_song_id: str, to_song_id: str) -> dict:
    """Remember that mixing from_song_id into to_song_id worked (🔥). Each love adds one to the
    pair's count (it clears an earlier 'avoid'); the planner then prefers playing to_song right
    after from_song, more with every love. Changes stored memory, so marked destructive.
    Ids are YouTube video ids (now_playing, get_history, list_library)."""
    s = _api("/api/pair", "POST", {"from": from_song_id, "to": to_song_id, "verdict": "loved"})
    say(f"Loved mixing {_title(s, from_song_id)} into {_title(s, to_song_id)}", to_song_id)
    return _summary(s)


@tool("Avoid a pair", "avoiding a mix")
def avoid_pair(from_song_id: str, to_song_id: str, why: str = "") -> dict:
    """Remember that mixing from_song_id into to_song_id didn't work (👎); resets the pair's loves.
    `why` (optional): "songs" = they don't go together, so to_song is never placed right after
    from_song again unless nothing else is ready; "effect" = only the effect used that time was
    wrong, so they can still meet with a different one. Empty = treated like "songs"."""
    s = _api("/api/pair", "POST", {"from": from_song_id, "to": to_song_id, "verdict": "avoided", "why": why or None})
    say(f"Won't mix {_title(s, from_song_id)} into {_title(s, to_song_id)} again", to_song_id)
    return _summary(s)


@tool("List remembered pairs", "pairs", read_only=True, destructive=False)
def list_pairs() -> list[dict]:
    """Every rated hand-over, most loved first: from/to ids and titles, the effect, loved (how many
    🔥), avoided (1 = 👎), why ("songs" | "effect" | null) and last (when it was rated)."""
    return _api("/api/pairs")["pairs"]


@tool("Forget a pair", "forgetting a mix")
def forget_pair(from_song_id: str, to_song_id: str) -> dict:
    """Erase what's remembered about mixing from_song_id into to_song_id (loves, avoid, reason).
    The reverse direction is a separate pair and stays. Can't be undone."""
    s = _api("/api/pair/forget", "POST", {"from": from_song_id, "to": to_song_id})
    say(f"Forgot mixing {_title(s, from_song_id)} into {_title(s, to_song_id)}", to_song_id)
    return _summary(s)


def main() -> None:
    server.run("stdio")


if __name__ == "__main__":
    main()
