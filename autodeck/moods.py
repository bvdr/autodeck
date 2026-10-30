"""Moods: a set of songs built from what people put in public playlists on a theme.

"Balkan party" -> the top YouTube playlists for that search, each read in full; songs that
appear in several of them (also counted in Deezer playlists on the theme) rank first: they're
the crowd's picks, not one person's taste. YouTube entries already carry a video id, so the
winners import without another search. Network only; the engine stores and applies the result.
"""
import json
import logging
import urllib.parse
import urllib.request

import yt_dlp

from . import fetch, lastfm, party
from .library import Song

log = logging.getLogger(__name__)
PLAYLISTS = 6        # YouTube playlists read per theme
DEEZER_PLAYLISTS = 5
MAX_PER_PLAYLIST = 150


def _youtube_playlists(theme: str) -> list[str]:
    """Playlist URLs from a YouTube playlist search (sp=EgIQAw== is YouTube's 'Playlists' filter)."""
    url = ("https://www.youtube.com/results?search_query=" + urllib.parse.quote(theme)
           + "&sp=EgIQAw%253D%253D")
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "extract_flat": True, "playlistend": PLAYLISTS,
                               **fetch._cookie_opts()}) as y:
            info = y.extract_info(url, download=False) or {}
    except Exception as e:
        log.warning("playlist search failed: %s", e)
        return []
    return [f"https://www.youtube.com/playlist?list={e['id']}" for e in info.get("entries") or []
            if e and e.get("id")][:PLAYLISTS]


def _deezer_keys(theme: str) -> set[str]:
    """party.key()s of songs in the top Deezer playlists for the theme (consensus signal only)."""
    def get(path):
        try:
            with urllib.request.urlopen("https://api.deezer.com" + path, timeout=10) as r:
                return json.loads(r.read())
        except Exception as e:
            log.warning("deezer %s failed: %s", path, e)
            return {}
    keys: set[str] = set()
    for p in (get(f"/search/playlist?q={urllib.parse.quote(theme)}&limit={DEEZER_PLAYLISTS}").get("data") or []):
        for t in (get(f"/playlist/{int(p['id'])}/tracks?limit=200").get("data") or []):
            keys.add(party.key(t.get("artist", {}).get("name", ""), t.get("title", "")))
    return keys


def rank(lists: list[list[Song]], deezer: set[str]) -> list[tuple[Song, int]]:
    """(song, votes) best first. One vote per playlist a song is in (by artist+title, so two
    uploads of the same song count together), +1 if Deezer's playlists have it too. Ties keep
    first-seen order (earlier = higher in a more relevant playlist)."""
    votes: dict[str, int] = {}
    first: dict[str, Song] = {}
    for songs in lists:
        seen = set()
        for s in songs[:MAX_PER_PLAYLIST]:
            k = party.key(*lastfm.split_title(s.title, s.channel))
            if not k or k in seen:
                continue
            seen.add(k)
            votes[k] = votes.get(k, 0) + 1
            first.setdefault(k, s)
    for k in votes:
        if k in deezer:
            votes[k] += 1
    order = sorted(votes, key=lambda k: -votes[k])  # stable: first-seen order within a tie
    return [(first[k], votes[k]) for k in order]


def collect(theme: str, n: int = 30) -> list[tuple[Song, int]]:
    """Top n songs for a theme with their vote counts. Empty if nothing was found."""
    lists = []
    for url in _youtube_playlists(theme):
        try:
            lists.append(fetch.expand(url))
        except fetch.FetchError as e:
            log.warning("playlist %s skipped: %s", url, e)
    if not lists:
        return []
    return rank(lists, _deezer_keys(theme))[:n]
