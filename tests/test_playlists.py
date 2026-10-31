"""Saved playlists (design HJGfo): playlists.json, the playlist source, its end, save/sync/remove."""
import json

import pytest
from fastapi.testclient import TestClient

from autodeck import config, library
from autodeck import engine as engine_mod
from autodeck.engine import Engine, sync_diff
from autodeck.library import Song
from autodeck.planner import order
from autodeck.server import create_app
from test_server import _ready

# p1..p4 are the playlist (YouTube order), x1/x2 library-only songs, w1 still downloading.
P = ["ppppppppp01", "ppppppppp02", "ppppppppp03", "ppppppppp04"]
X = ["xxxxxxxxx01", "xxxxxxxxx02"]
W = "wwwwwwwww01"
LID = "PLbalkanbrass"
URL = f"https://www.youtube.com/playlist?list={LID}"


@pytest.fixture
def c(tmp_library, monkeypatch):
    for sid, bpm, cam in zip(P + X, (120, 140, 121, 100, 122, 119), ("8A", "3B", "8A", "1A", "8A", "8A")):
        _ready(sid, bpm, "Pop", camelot=cam)
    library.save_song(Song(W, "waiting", "chan", 0, "queued"))
    library.save_pool(P + X + [W])
    calls = []

    def read(url):
        calls.append(url)
        return "Balkan brass", [Song(i, f"title {i}", "chan", 0, "queued") for i in c.yt]

    monkeypatch.setattr(engine_mod.fetch, "read_playlist", read)
    eng = Engine()
    c = TestClient(create_app(eng), base_url="http://127.0.0.1")
    c.engine, c.calls, c.yt = eng, calls, [P[0], P[1], W, P[2], P[3]]
    return c


def _save(c, **kw):
    r = c.post("/api/playlists", json={"url": URL, **kw})
    assert r.status_code == 200, r.text
    return r.json()


def _queue(c):
    return [q["id"] for q in c.get("/api/state").json()["queue"]]


def test_save_persists_and_same_id_twice_opens_the_existing_one(c, tmp_library):
    r = _save(c)
    assert (r["existing"], r["found"], r["added"]) == (False, 5, 0)
    assert r["playlist"]["name"] == "Balkan brass" and r["playlist"]["covers"] == [P[0], P[1], W, P[2]]
    d = json.loads((tmp_library / "playlists.json").read_text())[LID]
    assert d["song_ids"] == c.yt and (d["order"], d["at_end"], d["excluded"]) == ("playlist", "keep_going", [])
    again = _save(c)
    assert again["existing"] is True and len(c.calls) == 1  # not read again
    assert list(Engine().playlists) == [LID]  # survives a restart


def test_corrupt_playlists_file_is_tolerated(tmp_library):
    (tmp_library / "playlists.json").write_text("{not json")
    assert Engine().playlists == {}
    (tmp_library / "playlists.json").write_text(json.dumps(
        {LID: {"name": "ok", "song_ids": P, "order": "weird", "synced_at": "x"},
         "PLgood": {"name": "Good", "song_ids": "notalist", "at_end": "repeat"}, "bad id!": {}}))
    e = Engine()
    assert list(e.playlists) == ["PLgood"]  # a bad synced_at drops that entry, a bad id too
    assert e.playlists["PLgood"]["song_ids"] == [] and e.playlists["PLgood"]["at_end"] == "repeat"


def test_link_errors(c):
    r = c.post("/api/playlists", json={"url": "https://www.youtube.com/watch?v=8R6lEytvMLo&list=PLb4"})
    assert r.status_code == 400 and r.json()["error"].startswith("That’s one song playing inside a playlist")
    r = c.post("/api/playlists", json={"url": "https://youtu.be/8R6lEytvMLo"})
    assert r.status_code == 400 and "playlist link" in r.json()["error"]
    assert c.calls == []


def test_treat_songs_as_sets_genre_hint_but_owner_genres_win(c):
    c.engine.set_song_genre(P[1], "Pop")
    _save(c, genre="Hip-hop")
    by = {s.id: s for s in c.engine.pool}
    assert by[P[0]].effective_genre() == "Hip-hop" and by[P[1]].effective_genre() == "Pop"
    c.patch(f"/api/playlists/{LID}", json={"genre": None})  # back to their own genres
    assert by[P[0]].genre_hint is None


def test_sync_inserts_new_songs_at_their_position_and_removed_leave_only_the_playlist(c):
    _save(c)
    c.yt = [P[0], "nnnnnnnnn01", P[1], W, P[3]]  # one new at #2, P[2] removed on YouTube
    r = c.post(f"/api/playlists/{LID}/sync").json()
    assert (r["added"], r["new_ids"], r["removed"]) == ([2], ["nnnnnnnnn01"], 1)
    assert c.engine.playlists[LID]["song_ids"] == c.yt
    pool = {s.id for s in c.engine.pool}
    assert P[2] in pool and "nnnnnnnnn01" in pool  # nothing deleted from the library
    assert sync_diff(["a", "b"], ["c", "a"]) == ([0], ["b"])


def test_playlist_is_exclusive_with_vibe_and_party(c):
    _save(c)
    c.post("/api/genre", json={"genre": "Pop"})
    s = c.post("/api/playlist", json={"id": LID}).json()
    assert (s["genre"], s["party"], s["source"]["kind"], s["playlist"]["id"]) == ("Any", None, "playlist", LID)
    s = c.post("/api/genre", json={"genre": "Hip-hop"}).json()
    assert s["playlist"] is None and s["source"]["kind"] == "genre"
    c.post("/api/playlist", json={"id": LID})
    s = c.post("/api/party", json={"flavor": "hits"}).json()
    assert s["playlist"] is None and s["party"] == "hits"
    c.post("/api/playlist", json={"id": LID})
    s = c.post("/api/playlist", json={"id": None}).json()
    assert s["source"] is None


def test_playlist_order_feeds_up_next_and_skips_songs_not_ready(c):
    _save(c)
    c.post("/api/playlist", json={"id": LID})
    assert _queue(c) == [P[0], P[1], P[2]]  # W (#3) isn't ready: skipped, not waited for
    c.post("/api/playing", json={"id": P[0]})
    assert _queue(c)[:3] == [P[1], P[2], P[3]]
    rows = {r["id"]: r for r in c.get("/api/suggestions").json()["suggestions"]}
    assert rows[P[0]]["state"] == "playing" and rows[P[1]]["state"] == "next" and rows[W]["state"] == "waiting"
    assert [rows[i]["pos"] for i in (P[0], P[1], W, P[2])] == [1, 2, 3, 4]


def test_mix_order_uses_the_planner(c):
    _save(c)
    c.patch(f"/api/playlists/{LID}", json={"order": "mix"})
    c.post("/api/playing", json={"id": P[0]})  # picked while P[0] plays: nothing locked from before
    c.post("/api/playlist", json={"id": LID})
    by = {s.id: s for s in c.engine.pool}
    want = [s.id for s in order(by[P[0]], [by[P[1]], by[P[2]], by[P[3]]], {}, "Any")]
    assert _queue(c) == want
    rows = [r for r in c.get("/api/suggestions").json()["suggestions"] if "plan" in r]
    assert [r["id"] for r in rows] == want and rows[0]["plan"] == 0
    assert rows[0]["why"] == "same key"  # P[2] is 8A like P[0]


def test_excluded_song_is_skipped_and_undo_brings_it_back(c):
    _save(c)
    c.post("/api/playlist", json={"id": LID})
    d = c.post(f"/api/playlists/{LID}/exclude", json={"id": P[1]}).json()
    assert P[1] not in [r["id"] for r in d["suggestions"]] and P[1] not in _queue(c)
    c.post(f"/api/playlists/{LID}/unexclude", json={"id": P[1]})
    assert P[1] in _queue(c)


def _play_through(c, ids):
    for i in ids:
        c.post("/api/playing", json={"id": i})


def test_keep_going_fills_from_the_library_then_hands_over_to_vibe_any(c):
    c.yt = P  # all ready: a song still downloading would be filled around, not the end
    _save(c)
    c.post("/api/playlist", json={"id": LID})
    _play_through(c, [P[0], P[1], P[2]])
    s = c.get("/api/state").json()
    assert s["notice"] == "Balkan brass ends after title ppppppppp04. Then: similar songs from the library."
    assert s["queue"][0]["id"] == P[3] and {q["id"] for q in s["queue"][1:]} <= set(X)
    c.post("/api/playing", json={"id": P[3]})
    nxt = _queue(c)[0]
    assert nxt in X and c.engine.playlist == LID  # still the playlist while its last song plays
    c.post("/api/playing", json={"id": nxt})
    assert c.engine.playlist is None and c.get("/api/state").json()["source"] is None


def test_repeat_starts_again_from_one(c):
    c.yt = P  # all ready: a song still downloading would be filled around, not the end
    _save(c)
    c.patch(f"/api/playlists/{LID}", json={"at_end": "repeat"})
    c.post("/api/playlist", json={"id": LID})
    _play_through(c, [P[0], P[1], P[2]])
    assert _queue(c) == [P[3], P[0], P[1]]
    c.post("/api/playing", json={"id": P[3]})
    assert _queue(c)[:2] == [P[0], P[1]] and c.engine.playlist == LID


def test_stop_ends_up_next_after_the_last_song(c):
    c.yt = P  # all ready: a song still downloading would be filled around, not the end
    _save(c)
    c.patch(f"/api/playlists/{LID}", json={"at_end": "stop"})
    c.post("/api/playlist", json={"id": LID})
    _play_through(c, [P[0], P[1], P[2]])
    assert _queue(c) == [P[3]]
    c.post("/api/playing", json={"id": P[3]})
    assert _queue(c) == []


def test_remove_while_playing_falls_back_to_any_without_a_gap(c):
    _save(c)
    c.post("/api/playlist", json={"id": LID})
    c.post("/api/playing", json={"id": P[0]})
    locked = _queue(c)[: config.FIXED_NEXT]
    r = c.delete(f"/api/playlists/{LID}").json()
    assert (r["name"], r["count"], r["was_playing"]) == ("Balkan brass", 5, True)
    assert r["state"]["source"] is None and r["state"]["playlists"] == []
    assert r["state"]["queue"][0]["id"] == locked[0] and len(r["state"]["queue"]) >= config.MIN_UPCOMING
    assert {s.id for s in c.engine.pool} >= set(P)  # songs stay


def test_active_playlist_and_its_progress_survive_a_restart(c):
    _save(c)
    c.post("/api/playlist", json={"id": LID})
    _play_through(c, [P[0], P[1]])
    e = Engine()
    assert e.playlist == LID and set(e.pl_run) == {P[0], P[1]} and e.upcoming[0] == P[2]


def test_worker_prepares_playlist_songs_from_the_top(c):
    c.yt = [W, P[0]]
    _save(c)
    assert W in c.engine.get_state()[1]  # newest saved playlist: prepped before the vibe's songs
    assert c.engine.worker.wanted(*c.engine.get_state())[0] == W
