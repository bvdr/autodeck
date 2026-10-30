"""Last.fm lookups for party mode: tags, listener counts, similar tracks, tag charts.

Free API key (personal, non-commercial use per the Last.fm API terms), read from the env var
LASTFM_API_KEY or ~/.config/autodeck/lastfm.env, never from the repo. Responses are cached
on disk for a week (<library>/lastfm-cache/): the terms ask for caching, and party mode asks
the same questions every few minutes. Every call returns empty results instead of raising
when Last.fm is unreachable or no key is set: party mode degrades, playback never breaks.
"""
import hashlib
import json
import logging
import os
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path

from . import config

log = logging.getLogger(__name__)

API = "https://ws.audioscrobbler.com/2.0/"
KEY_FILE = Path.home() / ".config" / "autodeck" / "lastfm.env"
CACHE_TTL = 7 * 24 * 3600
TIMEOUT = 10
_MIN_GAP = 0.25  # s between live calls: well under Last.fm's rate limit
_last_call = 0.0


def api_key() -> str | None:
    if os.environ.get("LASTFM_API_KEY"):
        return os.environ["LASTFM_API_KEY"].strip()
    try:
        for line in KEY_FILE.read_text().splitlines():
            if line.startswith("LASTFM_API_KEY="):
                return line.split("=", 1)[1].strip() or None
    except OSError:
        pass
    return None


def _cache_file(params: dict) -> Path:
    h = hashlib.sha1(json.dumps(params, sort_keys=True).encode()).hexdigest()[:20]
    return config.library_root() / "lastfm-cache" / f"{h}.json"


def _call(method: str, **params) -> dict:
    """One API call (cached). {} on any failure."""
    key = api_key()
    if not key:
        return {}
    params = {"method": method, "format": "json", "autocorrect": "1", **params}
    cf = _cache_file(params)
    try:
        if time.time() - cf.stat().st_mtime < CACHE_TTL:
            return json.loads(cf.read_text())
    except (OSError, ValueError):
        pass
    global _last_call
    wait = _MIN_GAP - (time.monotonic() - _last_call)
    if wait > 0:
        time.sleep(wait)
    _last_call = time.monotonic()
    url = API + "?" + urllib.parse.urlencode({**params, "api_key": key})
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "autodeck/1 (personal DJ app)"})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            data = json.loads(r.read())
    except Exception as e:  # network, HTTP, JSON: degrade, never break playback
        log.warning("last.fm %s failed: %s", method, e)
        return {}
    if "error" in data:  # e.g. track not found: a valid, cacheable answer
        data = {}
    cf.parent.mkdir(parents=True, exist_ok=True)
    config.write_json_atomic(cf, data)
    return data


def _as_list(x) -> list:
    return x if isinstance(x, list) else [x] if x else []


def artist_tags(artist: str) -> list[str]:
    d = _call("artist.getTopTags", artist=artist)
    return [t["name"].lower() for t in _as_list(d.get("toptags", {}).get("tag"))[:15]]


def track_info(artist: str, title: str) -> dict:
    """{"listeners": int, "tags": [...]} for one track ({} if Last.fm doesn't know it)."""
    t = _call("track.getInfo", artist=artist, track=title).get("track") or {}
    if not t:
        return {}
    tags = [x["name"].lower() for x in _as_list(t.get("toptags", {}).get("tag"))]
    return {"listeners": int(t.get("listeners") or 0), "tags": tags}


def similar_tracks(artist: str, title: str, limit: int = 20) -> list[tuple[str, str]]:
    d = _call("track.getSimilar", artist=artist, track=title, limit=str(limit))
    return [(t["artist"]["name"], t["name"]) for t in _as_list(d.get("similartracks", {}).get("track"))]


def tag_top_tracks(tag: str, limit: int = 50) -> list[tuple[str, str]]:
    d = _call("tag.getTopTracks", tag=tag, limit=str(limit))
    return [(t["artist"]["name"], t["name"]) for t in _as_list(d.get("tracks", {}).get("track"))]


def chart_top_tracks(limit: int = 50) -> list[tuple[str, str]]:
    d = _call("chart.getTopTracks", limit=str(limit))
    return [(t["artist"]["name"], t["name"]) for t in _as_list(d.get("tracks", {}).get("track"))]


# --- YouTube title -> (artist, title) ------------------------------------------
_JUNK = re.compile(r"\s*[\(\[][^)\]]*(official|video|audio|lyric|visuali[sz]er|hd|4k|remaster|clip)[^)\]]*[\)\]]",
                   re.IGNORECASE)


def split_title(title: str, channel: str) -> tuple[str, str]:
    """Best guess at (artist, title) for a YouTube video. "Artist - Title (Official Video)" is
    the common shape; otherwise the channel is the artist ("Artist - Topic", "ArtistVEVO")."""
    t = _JUNK.sub("", title).strip()
    for sep in (" - ", " – ", " — ", " | "):
        if sep in t:
            a, b = t.split(sep, 1)
            return a.strip(), b.strip()
    artist = re.sub(r"\s*-\s*Topic$|VEVO$", "", channel or "").strip()
    return artist, t
