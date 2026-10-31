"""Deezer lookups: BPM and release year for songs that aren't in the library yet.

Suggestions have no audio to analyze, so their tempo comes from Deezer's public API (free,
no key). Coverage is partial: some tracks report bpm 0 (unknown) and many Romanian ones aren't
found, so every result may be None. Cached a week on disk; failures degrade to None.
"""
import hashlib
import json
import logging
import time
import urllib.parse
import urllib.request

from . import config

log = logging.getLogger(__name__)
API = "https://api.deezer.com"
CACHE_TTL = 7 * 24 * 3600
TIMEOUT = 8


def _get(path: str) -> dict:
    cf = config.cache_root() / "deezer-cache" / (hashlib.sha1(path.encode()).hexdigest()[:20] + ".json")
    try:
        if time.time() - cf.stat().st_mtime < CACHE_TTL:
            return json.loads(cf.read_text())
    except (OSError, ValueError):
        pass
    try:
        req = urllib.request.Request(API + path, headers={"User-Agent": "autodeck/1 (personal DJ app)"})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            data = json.loads(r.read())
    except Exception as e:
        log.warning("deezer %s failed: %s", path, e)
        return {}
    if not isinstance(data, dict) or "error" in data:
        data = {}
    cf.parent.mkdir(parents=True, exist_ok=True)
    config.write_json_atomic(cf, data)
    return data


def lookup(artist: str, title: str) -> dict:
    """{"bpm": float|None, "year": int|None} for a song, best search match."""
    q = urllib.parse.quote(f'artist:"{artist}" track:"{title}"')
    hits = _get(f"/search?q={q}&limit=1").get("data") or []
    if not hits:  # the strict query misses "feat." variants; plain text search is looser
        hits = _get(f"/search?q={urllib.parse.quote(f'{artist} {title}')}&limit=1").get("data") or []
    if not hits:
        return {"bpm": None, "year": None}
    t = _get(f"/track/{int(hits[0]['id'])}")
    bpm = t.get("bpm") or None  # Deezer reports 0 for "unknown"
    date = t.get("release_date") or ""
    return {"bpm": float(bpm) if bpm else None, "year": int(date[:4]) if date[:4].isdigit() else None}
