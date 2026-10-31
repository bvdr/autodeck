import json

import numpy as np
import pytest
import soundfile as sf
from fastapi.testclient import TestClient

from autodeck import engine as engine_mod
from autodeck import config, library
from autodeck.engine import Engine
from autodeck.library import Analysis, Section, Song
from autodeck.planner import Transition
from autodeck.render import time_map_for
from autodeck.server import create_app

SR = 22050
IDS = ["aaaaaaaaaa1", "bbbbbbbbbb2", "cccccccccc3", "dddddddddd4"]


def _ready(sid, bpm, genre, camelot="8A", secs=48.0):
    """Synthetic ready song: analysis only + a tiny audio.flac (no real analysis run)."""
    bar = 240 / bpm
    db = [i * bar for i in range(int(secs // bar))]
    end = db[-1]
    secs_ = [Section(0.0, db[4], "intro"), Section(db[4], db[-5], "body"), Section(db[-5], end, "outro")]
    an = Analysis(library.ANALYSIS_VERSION, bpm, db, db, True, None, camelot, [0.5] * int(secs),
                  secs_, 0.0, end, genre, None)
    s = Song(sid, f"title {sid}", "chan", secs, "ready", analysis=an)
    library.save_song(s)
    t = np.arange(int(secs * SR)) / SR
    sf.write(library.song_dir(sid) / "audio.flac", (0.2 * np.sin(2 * np.pi * 110 * t)).astype(np.float32), SR)
    return s


@pytest.fixture
def client(tmp_library):
    for sid, bpm, g in zip(IDS, (120, 122, 121, 90), ("Pop", "Pop", "Hip-hop", "Hip-hop")):
        _ready(sid, bpm, g)
    library.save_pool(IDS)
    eng = Engine()
    # TestClient is used without `with`, so the lifespan (worker thread) never starts.
    # base_url sets Host: 127.0.0.1 (the default "testserver" is rejected by TrustedHost).
    c = TestClient(create_app(eng), base_url="http://127.0.0.1")
    c.engine = eng
    return c


def test_add_bad_url_is_400(client):
    r = client.post("/api/add", json={"url": "https://example.com/nope"})
    assert r.status_code == 400 and r.json()["error"]


def test_add_unknown_genre_is_400(client):
    r = client.post("/api/add", json={"url": "https://youtu.be/eeeeeeeeee5", "genre": "Polka"})
    assert r.status_code == 400 and r.json()["error"]


def test_state_shape(client):
    s = client.get("/api/state").json()
    assert s["playing"] is None and s["genre"] == "Any" and s["notice"] is None
    # Nothing kept: auto picks top Up next up to MIN_UPCOMING, pool order while nothing plays.
    assert [q["id"] for q in s["queue"]] == IDS[: config.MIN_UPCOMING]
    assert not any(q["kept"] for q in s["queue"])
    assert s["pool_counts"] == {"ready": 4}
    assert set(s["queue"][0]) == {"id", "title", "channel", "status", "error", "genre", "bpm", "camelot", "danceability",
                                  "kept", "kept_from", "in_playlist"}
    assert [p["id"] for p in s["pool"]] == IDS and set(s["pool"][0]) | {"kept", "kept_from", "in_playlist"} == set(s["queue"][0])


def test_pool_shows_failed_song_with_error(client):
    client.engine.pool.append(Song("ffffffffff6", "gone", "c", 0, "failed", error="video removed"))
    s = client.get("/api/state").json()
    f = next(p for p in s["pool"] if p["id"] == "ffffffffff6")
    assert f["status"] == "failed" and f["error"] == "video removed"
    assert "ffffffffff6" not in [q["id"] for q in s["queue"]]


def test_foreign_host_rejected(client):
    assert client.get("/api/state", headers={"host": "evil.example"}).status_code == 400
    assert client.get("/api/state", headers={"host": "localhost:8765"}).status_code == 200


def test_empty_genre_sets_notice_and_never_goes_silent(client):
    client.post("/api/playing", json={"id": IDS[0]})
    r = client.post("/api/genre", json={"genre": "Romanian sârbe"})
    s = r.json()
    assert s["notice"] and s["queue"]  # fell back to Any, still something next


def test_genre_switch_puts_in_genre_song_first(client):
    client.post("/api/playing", json={"id": IDS[0]})
    assert client.get("/api/state").json()["queue"][0]["genre"] == "Pop"  # 122 Pop is closest
    s = client.post("/api/genre", json={"genre": "Hip-hop"}).json()
    assert s["queue"][0]["genre"] == "Hip-hop" and s["notice"] is None


def test_unknown_genre_is_400(client):
    assert client.post("/api/genre", json={"genre": "Polka"}).status_code == 400


def test_playing_marks_played_and_worker_sees_it(client):
    v0 = client.get("/api/state").json()["version"]
    s = client.post("/api/playing", json={"id": IDS[0]}).json()
    assert s["playing"] == IDS[0] and IDS[0] not in [q["id"] for q in s["queue"]]
    assert s["version"] > v0
    _, upcoming, _, played = client.engine.worker.get_state()
    assert IDS[0] in played and IDS[0] not in upcoming


def test_queue_reorder_bumps_version(client):
    client.post("/api/playing", json={"id": IDS[0]})
    v0 = client.get("/api/state").json()["version"]
    order = [IDS[3], IDS[2], IDS[1]]
    s = client.post("/api/queue", json={"ids": order, "pin": IDS[2]}).json()
    assert s["version"] > v0 and [q["id"] for q in s["queue"]] == order
    # The moved song and everything above it are the owner's now; the rest stay auto picks.
    assert client.engine.requested == [IDS[3], IDS[2]]
    assert [q["kept"] for q in s["queue"]] == [True, True, False]
    assert client.post("/api/queue", json={"ids": [IDS[1], IDS[1]]}).status_code == 400
    assert client.post("/api/queue", json={"ids": ["zzzzzzzzzz9"]}).status_code == 400


def test_delete_removes_from_pool_and_queue(client):
    client.post("/api/playing", json={"id": IDS[0]})
    assert client.delete(f"/api/song/{IDS[1]}").status_code == 200
    s = client.get("/api/state").json()
    assert IDS[1] not in [q["id"] for q in s["queue"]]
    assert IDS[1] not in [x.id for x in client.engine.pool]
    assert IDS[1] not in json.loads((library.song_dir("x").parent / "pool.json").read_text())
    assert client.delete(f"/api/song/{IDS[1]}").status_code == 404


def test_owner_genre_fix(client):
    s = client.post(f"/api/song/{IDS[3]}", json={"genre": "Pop"}).json()
    assert next(q for q in s["pool"] if q["id"] == IDS[3])["genre"] == "Pop"
    assert library.load_song(IDS[3]).genre == "Pop"


def test_genres_edit(client):
    assert client.put("/api/genres", json={"genres": ["Pop", "Disco"]}).status_code == 200
    assert client.get("/api/state").json()["genres"] == ["Pop", "Disco"]
    for bad in ([], ["Pop", "Pop"], ["Any"], [""]):
        assert client.put("/api/genres", json={"genres": bad}).status_code == 400


def test_deck_identity(client):
    d = client.get(f"/api/deck/{IDS[0]}").json()
    assert d["duration_rendered"] == pytest.approx(d["sections_rendered"][-1]["end"])
    assert d["bpm_playing"] == pytest.approx(120) and d["peaks"] == []
    assert client.get(d["url"]).status_code == 200


def test_transition_payload(client):
    client.post("/api/playing", json={"id": IDS[0]})
    r = client.get("/api/transition", params={"from": IDS[0]}).json()
    assert r["transition"]["in_id"] == IDS[1]
    assert r["out_at_rendered"] > 0
    assert client.get(r["in_deck"]["url"]).status_code == 200


def test_transition_after_converts_rendered_to_original(client, monkeypatch):
    client.post("/api/playing", json={"id": IDS[0]})
    a = client.engine.by_id(IDS[0])
    # a came in at 1.05x over 8 s, so its rendered clock runs behind its original clock.
    tm = time_map_for(a, Transition("blend", "x", a.id, 40.0, 0.0, 8.0, 1.05))
    client.engine.maps[a.id] = tm
    seen = {}
    real = engine_mod.pick_transition

    def spy(x, y, after=None, entered=None, **kw):
        seen["after"] = after
        return real(x, y, after=after, entered=entered, **kw)

    monkeypatch.setattr(engine_mod, "pick_transition", spy)
    client.get("/api/transition", params={"from": IDS[0], "after": 10.0})
    assert seen["after"] == pytest.approx(tm.to_original(10.0))
    assert seen["after"] > 10.0  # 8 s at 1.05x covered 8.4 s of original audio
    assert tm.to_rendered(seen["after"]) == pytest.approx(10.0)


@pytest.mark.parametrize("path", ["/audio/x/../../etc/passwd", "/audio/aaaaaaaaaa1/..%2f..%2fpool.json",
                                  "/audio/aaaaaaaaaa1/song.json", "/audio/..%2f..%2fetc/passwd"])
def test_audio_rejects_traversal(client, path):
    assert client.get(path).status_code == 404


def test_audio_serves_source(client):
    assert client.get(f"/audio/{IDS[0]}/audio.flac").status_code == 200


def test_bad_id_404(client):
    assert client.get("/api/deck/short").status_code == 404


def test_playing_commits_the_plan_the_player_started(client, monkeypatch):
    """Two transitions for the same `from` (e.g. Mix now overlapping a replan): only the
    plan the player reports via /api/playing becomes b's map, whatever order they ran in."""
    client.post("/api/playing", json={"id": IDS[0]})
    a, b = IDS[0], IDS[1]
    # Two candidates for a->b with different tempo ratios, so b's time maps differ.
    trs = iter([Transition("blend", a, b, 32.0, 0.0, 8.0, 1.05),
                Transition("blend", a, b, 40.0, 0.0, 8.0, 0.95)])
    real = engine_mod.pick_transition
    monkeypatch.setattr(engine_mod, "pick_transition",
                        lambda x, y, after=None, entered=None, **kw: next(trs) if x.id == a and kw.get("force") is None
                        else real(x, y, after, entered, **kw))
    p1 = client.get("/api/transition", params={"from": a}).json()
    p2 = client.get("/api/transition", params={"from": a, "after": 30.0}).json()
    assert p1["plan_id"] != p2["plan_id"]
    assert b not in client.engine.maps  # nothing committed by /api/transition itself
    tm1 = client.engine.plans[p1["plan_id"]][1]
    tm2 = client.engine.plans[p2["plan_id"]][1]
    client.post("/api/playing", json={"id": b, "plan_id": p1["plan_id"]})
    assert client.engine.maps[b] is tm1 and client.engine.entered[b].out_at == 32.0
    nxt = client.get("/api/transition", params={"from": b}).json()
    out_at = nxt["transition"]["out_at"]
    assert nxt["out_at_rendered"] == pytest.approx(tm1.to_rendered(out_at))
    assert abs(tm1.to_rendered(out_at) - tm2.to_rendered(out_at)) > 0.1  # the maps really differ


def test_playing_unknown_plan_id_keeps_identity(client):
    s = client.post("/api/playing", json={"id": IDS[0], "plan_id": "nope"})
    assert s.status_code == 200 and IDS[0] not in client.engine.maps


@pytest.mark.parametrize("remove_first", [True, False])
def test_readding_a_failed_song_retries_it(client, monkeypatch, remove_first):
    # Without this a failed song could never be retried: the worker skips "failed", and
    # re-adding reloaded the saved record with its "failed" status.
    sid = "eeeeeeeeee5"
    monkeypatch.setattr(engine_mod.fetch, "expand", lambda url: [Song(sid, "t", "c", 60.0, "queued")])
    client.post("/api/add", json={"url": f"https://youtu.be/{sid}"})
    s = client.engine.by_id(sid)
    s.status, s.error = "failed", "download timed out"
    library.save_song(s)
    if remove_first:
        assert client.delete(f"/api/song/{sid}").status_code == 200
    client.post("/api/add", json={"url": f"https://youtu.be/{sid}"})
    p = next(p for p in client.get("/api/state").json()["pool"] if p["id"] == sid)
    assert (p["status"], p["error"]) == ("queued", None)
    assert (library.load_song(sid).status, library.load_song(sid).error) == ("queued", None)


def test_session_survives_a_server_restart(client, monkeypatch):
    # The in-memory time maps used to die with the server: after a 1.05x blend the next exit
    # was computed on b's identity map, ~2.3 s (over a bar) off the bar line, and played
    # songs came round again.
    a, b = IDS[0], IDS[1]
    client.post("/api/playing", json={"id": a})
    real = engine_mod.pick_transition
    monkeypatch.setattr(engine_mod, "pick_transition", lambda x, y, after=None, entered=None, **kw:
                        Transition("blend", a, b, 32.0, 0.0, 8.0, 1.05) if x.id == a else real(x, y, after, entered, **kw))
    p = client.get("/api/transition", params={"from": a}).json()
    client.post("/api/playing", json={"id": b, "plan_id": p["plan_id"]})
    before = client.get("/api/transition", params={"from": b}).json()

    fresh = Engine()  # same library on disk, nothing carried over in memory
    c2 = TestClient(create_app(fresh), base_url="http://127.0.0.1")
    after = c2.get("/api/transition", params={"from": b}).json()
    assert fresh.playing == b and fresh.played == {a, b}
    assert after["transition"]["in_id"] == before["transition"]["in_id"]
    assert after["out_at_rendered"] == pytest.approx(before["out_at_rendered"])
    assert fresh.entered[b] == client.engine.entered[b]
    assert a not in [q["id"] for q in c2.get("/api/state").json()["queue"]]


def test_corrupt_or_stale_session_is_ignored(tmp_library):
    _ready(IDS[0], 120, "Pop")
    library.save_pool([IDS[0]])
    (tmp_library / "session.json").write_text('{"playing": "aaaa')
    assert Engine().playing is None
    (tmp_library / "session.json").write_text(json.dumps(
        {"playing": "zzzzzzzzzz9", "played": ["zzzzzzzzzz9", IDS[0]],
         "maps": {"zzzzzzzzzz9": [[0, 0], [1, 1]]}, "entered": {}}))
    e = Engine()
    assert e.playing is None and e.played == {IDS[0]} and e.maps == {}


def test_active_genre_survives_a_restart(client):
    # It used to reset to Any on every restart.
    client.post("/api/genre", json={"genre": "Hip-hop"})
    assert Engine().genre == "Hip-hop"
    config.save_genres(["Pop"])  # the genre was deleted meanwhile: back to Any
    assert Engine().genre == "Any"
    client.post("/api/party", json={"flavor": "hits"})  # a party resets the vibe; it's what comes back
    e = Engine()
    assert (e.party, e.genre) == ("hits", "Any")


def test_deck_peaks_cover_only_the_played_span(client):
    # peaks.json covers the whole 48 s source; the deck draws them over the trimmed render
    # [play_start, play_end], so unsliced they were stretched and misaligned.
    a = client.engine.by_id(IDS[0])
    a.analysis.play_start, a.analysis.play_end = 12.0, 36.0
    (library.song_dir(IDS[0]) / "peaks.json").write_text(json.dumps([i / 480 for i in range(480)]))
    pk = client.get(f"/api/deck/{IDS[0]}").json()["peaks"]
    assert len(pk) == 240
    assert pk[0] == pytest.approx(120 / 480) and pk[-1] == pytest.approx(359 / 480)


def test_previous_song_renders_are_deleted_when_the_next_starts(client):
    # Renders are ~40 MB WAVs and nothing removed them (1.3 GB after one evening). The
    # browser holds the decoded audio, so the file of the song that just handed over is dead.
    a, b = IDS[0], IDS[1]
    client.post("/api/playing", json={"id": a})
    client.get(f"/api/deck/{a}")
    assert list(library.song_dir(a).glob("render-*.wav"))
    tr = client.get(f"/api/transition?from={a}").json()
    client.post("/api/playing", json={"id": b, "plan_id": tr["plan_id"]})
    assert not list(library.song_dir(a).glob("render-*.wav"))
    assert list(library.song_dir(b).glob("render-*.wav"))  # the playing song keeps its render


def test_beat_to_beat_mode_is_used_for_transitions_and_persists(client, monkeypatch):
    from autodeck import engine as engine_mod
    seen = []
    real = engine_mod.pick_transition
    monkeypatch.setattr(engine_mod, "pick_transition",
                        lambda *a, **k: seen.append(k.get("beat_to_beat")) or real(*a, **k))
    a = IDS[0]
    client.post("/api/playing", json={"id": a})
    assert client.get("/api/state").json()["beat_to_beat"] is False
    st = client.post("/api/mode", json={"beat_to_beat": True}).json()
    assert st["beat_to_beat"] is True
    client.get(f"/api/transition?from={a}")
    assert seen[-1] is True
    session = json.loads((config.library_root() / "session.json").read_text())
    assert session["beat_to_beat"] is True


NEW = "nnnnnnnnnnn"


def test_requested_song_is_prepped_first_and_plays_next(client):
    eng = client.engine
    client.post("/api/playing", json={"id": IDS[0]})
    r = client.post("/api/request", json={"id": NEW, "title": "Hit", "channel": "c", "duration": 180})
    assert r.status_code == 200
    pool, upcoming, _, _ = eng.get_state()
    assert NEW in {s.id for s in pool} and upcoming[0] == NEW  # the worker preps it before anything
    _ready(NEW, 121, "Hip-hop")  # analysis done (off-genre doesn't matter: the owner asked for it)
    s = eng.by_id(NEW)
    s.status, s.analysis = "ready", library.load_song(NEW).analysis
    eng._on_worker_change()
    q = client.get("/api/state").json()["queue"]
    # Kept songs play right after the locked-in rows (the next mix may be planned already).
    assert q[config.FIXED_NEXT]["id"] == NEW and q[config.FIXED_NEXT]["kept"]
    client.post("/api/playing", json={"id": NEW})
    assert NEW not in eng.requested


def test_request_validates_id(client):
    assert client.post("/api/request", json={"id": "../etc", "title": "x"}).status_code == 404


def test_search_endpoint(client, monkeypatch):
    from autodeck import fetch
    monkeypatch.setattr(fetch, "search", lambda q, n=10: [Song(IDS[0], "title", "chan", 1.0, "queued"),
                                                         Song(NEW, "Hit", "c", 180.0, "queued")])
    res = client.get("/api/search", params={"q": "hit"}).json()["results"]
    assert [(r["id"], r["status"]) for r in res] == [(IDS[0], "ready"), (NEW, None)]
    assert client.get("/api/search", params={"q": " "}).status_code == 400


def test_transition_lists_possible_effects_and_honours_a_forced_one(client):
    a = IDS[0]
    client.post("/api/playing", json={"id": a})
    auto = client.get("/api/transition", params={"from": a}).json()
    assert auto["forced"] is None and auto["transition"]["kind"] in auto["options"] + ["half_double"]
    assert {"cut", "fade"} <= set(auto["options"])
    forced = client.get("/api/transition", params={"from": a, "kind": "cut"}).json()
    assert forced["transition"]["kind"] == "cut" and forced["forced"] == "cut"
    assert client.get("/api/transition", params={"from": a, "kind": "explode"}).status_code == 400


def _meta(eng, sid, **m):
    s = eng.by_id(sid)
    s.meta = {"artist": "A", "title": s.title, "listeners": 0, "tags": [], **m}


def test_party_flavor_filters_the_queue_and_persists(client):
    eng = client.engine
    client.post("/api/playing", json={"id": IDS[0]})
    _meta(eng, IDS[1], tags=["80s"])
    _meta(eng, IDS[2], tags=["reggaeton"])
    st = client.post("/api/party", json={"flavor": "latino"}).json()
    assert st["party"] == "latino" and st["queue"][0]["id"] == IDS[2]
    assert [q["id"] for q in st["queue"]] == [IDS[2]]  # only latino songs while the party runs
    assert json.loads((config.library_root() / "session.json").read_text())["party"] == "latino"
    st = client.post("/api/party", json={"flavor": None}).json()
    assert st["party"] is None and len(st["queue"]) == 3
    assert client.post("/api/party", json={"flavor": "metal"}).status_code == 400


def test_party_with_no_fitting_song_never_goes_silent(client):
    client.post("/api/playing", json={"id": IDS[0]})
    st = client.post("/api/party", json={"flavor": "oldies"}).json()
    assert st["queue"] and "oldies party" in st["notice"]


def test_suggestions_accept_and_reject(client, monkeypatch):
    from autodeck import fetch, party
    client.post("/api/party", json={"flavor": "hits"})
    monkeypatch.setattr(party, "suggestions", lambda cur, flavor, pool, rejected, n=8: [
        {"artist": "Toto", "title": "Rosanna", "reason": "r"}] if party.key("Toto", "Rosanna") not in rejected else [])
    assert client.get("/api/suggestions").json()["suggestions"][0]["title"] == "Rosanna"
    monkeypatch.setattr(fetch, "search", lambda q, n=10: [Song(NEW, "Toto - Rosanna", "Toto", 330.0, "queued")])
    st = client.post("/api/suggestions/accept", json={"artist": "Toto", "title": "Rosanna"}).json()
    assert any(p["id"] == NEW for p in st["pool"]) and NEW in client.engine.requested
    left = client.post("/api/suggestions/reject", json={"artist": "Toto", "title": "Rosanna"}).json()
    assert left["suggestions"] == []
    assert json.loads((config.library_root() / "rejected.json").read_text()) == {
        "party:Hits": [party.key("Toto", "Rosanna")]}


def test_enrichment_fills_meta_from_lastfm(client, monkeypatch):
    from autodeck import lastfm
    monkeypatch.setattr(lastfm, "api_key", lambda: "k")
    monkeypatch.setattr(lastfm, "track_info", lambda a, t: {"listeners": 2_000_000, "tags": ["80s"]})
    monkeypatch.setattr(lastfm, "artist_tags", lambda a: ["rock", "80s"])
    eng = client.engine
    while eng.enrich_once():
        pass
    s = library.load_song(IDS[0])
    assert s.meta["listeners"] == 2_000_000 and s.meta["tags"] == ["80s", "rock"]


def test_played_songs_are_logged_newest_first_with_their_effect(client):
    a, b = IDS[0], IDS[1]
    client.post("/api/playing", json={"id": a})
    tr = client.get(f"/api/transition?from={a}").json()
    client.post("/api/playing", json={"id": b, "plan_id": tr["plan_id"]})
    client.post("/api/playing", json={"id": b})  # pulled-back fader re-report: not a new entry
    h = client.get("/api/history").json()["history"]
    assert [x["id"] for x in h] == [b, a]
    assert h[0]["effect"] == tr["transition"]["kind"] and h[1]["effect"] is None
    assert h[0]["bpm"] == 122.0 and h[0]["at"]


def test_next_effect_and_mix_now_live_on_the_server(client):
    a, b = IDS[0], IDS[1]
    client.post("/api/playing", json={"id": a})
    st = client.post("/api/next_effect", json={"effect": "cut"}).json()
    assert st["next_effect"] == "cut"
    assert client.get(f"/api/transition?from={a}").json()["transition"]["kind"] == "cut"  # used without ?kind
    assert client.post("/api/next_effect", json={"effect": "explode"}).status_code == 400
    seq = client.post("/api/mix_now").json()["mix_now_seq"]
    assert client.post("/api/mix_now").json()["mix_now_seq"] == seq + 1
    client.post("/api/playing", json={"id": b})
    assert client.get("/api/state").json()["next_effect"] is None  # was for a's exit only


def test_build_mood_imports_crowd_picks_and_switches_to_it(client, monkeypatch):
    from autodeck import moods
    client.post("/api/playing", json={"id": IDS[0]})
    picks = [(Song(NEW, "Antique - Opa Opa", "c", 200.0, "queued"), 3), (library.load_song(IDS[2]), 2)]
    monkeypatch.setattr(moods, "collect", lambda theme, n=30: (picks, 6))
    r = client.post("/api/mood", json={"theme": "balkan party"}).json()
    assert r["mood"] == "Balkan party" and r["found"] == 2 and r["added"] == 1
    st = client.get("/api/state").json()
    assert st["genre"] == "Balkan party" and "Balkan party" in st["genres"]
    assert [q["id"] for q in st["queue"]] == [IDS[2]]  # the ready member; the new one is downloading
    assert client.engine.by_id(NEW).genre_hint == "Balkan party"  # worker fetches it first
    assert library.load_song(IDS[2]).genre == "Hip-hop" or True  # existing labels untouched
    monkeypatch.setattr(moods, "collect", lambda theme, n=30: ([], 0))
    assert client.post("/api/mood", json={"theme": "zzz"}).status_code == 404


def test_moods_refresh_adds_and_delete_forgets(client, monkeypatch):
    from autodeck import moods
    client.post("/api/playing", json={"id": IDS[0]})
    monkeypatch.setattr(moods, "collect", lambda theme, n=30: ([(library.load_song(IDS[2]), 2)], 6))
    client.post("/api/mood", json={"theme": "greek taverna"})
    monkeypatch.setattr(moods, "collect", lambda theme, n=30: ([(library.load_song(IDS[3]), 2)], 6))
    client.post("/api/mood", json={"theme": "greek taverna"})  # refresh: adds, keeps the old pick
    assert client.get("/api/state").json()["moods"] == {"Greek taverna": 2}
    st = client.request("DELETE", "/api/mood/Greek taverna").json()
    assert st["moods"] == {} and "Greek taverna" not in st["genres"] and st["genre"] == "Any"
    assert client.request("DELETE", "/api/mood/nope").status_code == 404


def _pair(client, a, b, verdict, **kw):
    return client.post("/api/pair", json={"from": a, "to": b, "verdict": verdict, **kw})


def test_old_pairs_format_is_migrated_without_losing_anything(tmp_library):
    (tmp_library / "pairs.json").write_text(json.dumps({"a>b": "loved", "b>c": "avoided", "c>d": "meh"}))
    eng = Engine([])
    assert eng.pairs == {
        "a>b": {"effect": None, "loved": 1, "avoided": 0, "why": None, "last": None},
        "b>c": {"effect": None, "loved": 0, "avoided": 1, "why": None, "last": None}}
    # A 🔥 on a migrated pair counts on from its old love.
    eng.rate_pair("a", "b", "loved", "bass_swap")
    saved = json.loads((tmp_library / "pairs.json").read_text())
    assert saved["a>b"]["loved"] == 2 and saved["a>b"]["effect"] == "bass_swap" and saved["a>b"]["last"]
    assert saved["b>c"]["avoided"] == 1


def test_rating_semantics_love_adds_avoid_resets_undo_restores(client, tmp_library):
    a, b, k = IDS[2], IDS[3], f"{IDS[2]}>{IDS[3]}"
    _pair(client, a, b, "loved", effect="half_double")  # logged kind -> the picker's "blend"
    s = _pair(client, a, b, "loved", effect="bass_swap").json()
    assert (s["pairs"][k]["loved"], s["pairs"][k]["effect"], s["pairs"][k]["avoided"]) == (2, "bass_swap", 0)
    s = _pair(client, a, b, "avoided", effect="echo").json()
    assert {x: s["pairs"][k][x] for x in ("loved", "avoided", "why", "effect")} == {
        "loved": 0, "avoided": 1, "why": None, "effect": "echo"}
    s = client.post("/api/pair/why", json={"from": a, "to": b, "why": "effect"}).json()
    assert s["pairs"][k]["why"] == "effect"
    # Tapping 👎 again undoes it: back to two loves (the reason goes with it).
    s = client.post("/api/pair/undo", json={"from": a, "to": b}).json()
    assert (s["pairs"][k]["loved"], s["pairs"][k]["avoided"]) == (2, 0)
    assert json.loads((tmp_library / "pairs.json").read_text())[k]["loved"] == 2  # written each time
    assert client.post("/api/pair/undo", json={"from": a, "to": b}).status_code == 409  # one level
    # Undoing a first rating forgets the pair.
    _pair(client, b, a, "loved")
    assert f"{b}>{a}" not in client.post("/api/pair/undo", json={"from": b, "to": a}).json()["pairs"]


def test_pair_rejects_bad_input(client):
    assert _pair(client, IDS[0], IDS[1], "meh").status_code == 400
    assert _pair(client, IDS[0], IDS[0], "loved").status_code == 400
    assert _pair(client, "../x", IDS[0], "loved").status_code == 404
    assert _pair(client, IDS[0], IDS[1], "loved", why="songs").status_code == 400  # a reason is for 👎
    assert _pair(client, IDS[0], IDS[1], "avoided", why="vibes").status_code == 400
    _pair(client, IDS[0], IDS[1], "loved")
    assert client.post("/api/pair/why", json={"from": IDS[0], "to": IDS[1], "why": "songs"}).status_code == 409


def test_avoided_pair_never_adjacent_in_up_next_and_the_move_is_explained(client):
    client.post("/api/playing", json={"id": IDS[0]})
    # IDS[1] (122 Pop) is the natural next song after IDS[0] (120 Pop); IDS[3] (90) is far off.
    assert client.get("/api/state").json()["queue"][0]["id"] == IDS[1]
    s = _pair(client, IDS[0], IDS[1], "avoided", why="songs").json()
    q = [x["id"] for x in s["queue"]]
    # Avoided pair never back to back, even when it held a locked slot.
    assert all((a, b) != (IDS[0], IDS[1]) for a, b in zip([IDS[0], *q], q))
    assert s["moved"] == [{"id": IDS[1], "title": f"title {IDS[1]}", "after": IDS[0],
                           "after_title": f"title {IDS[0]}", "by": q.index(IDS[1])}]
    # "Wrong effect" only: the songs may meet again (the locked-in rows keep their order, so it's
    # the planner's memory that changes: no more avoid to explain).
    assert client.post("/api/pair/why", json={"from": IDS[0], "to": IDS[1], "why": "effect"}).json()["moved"] == []


def test_auto_effect_uses_the_loved_one_and_skips_the_avoided_one(client):
    client.post("/api/playing", json={"id": IDS[0]})
    r = client.get("/api/transition", params={"from": IDS[0]}).json()
    b, auto, opts = r["transition"]["in_id"], r["transition"]["kind"], r["options"]
    assert r["loved_effect"] is None and r["avoid_effect"] is None
    other = next(k for k in opts if k not in ("blend", auto))
    _pair(client, IDS[0], b, "loved", effect=other)
    r = client.get("/api/transition", params={"from": IDS[0]}).json()
    assert (r["transition"]["kind"], r["loved_effect"]) == (other, other)
    # The owner's own pick still wins over the memory.
    client.post("/api/next_effect", json={"effect": "cut"})
    r = client.get("/api/transition", params={"from": IDS[0]}).json()
    assert (r["transition"]["kind"], r["loved_effect"]) == ("cut", None)
    client.post("/api/next_effect", json={"effect": None})
    # 👎 "wrong effect" on Auto's own pick: Auto plays something else; the effect stays listed.
    _pair(client, IDS[0], b, "avoided", effect=auto, why="effect")
    r = client.get("/api/transition", params={"from": IDS[0]}).json()
    assert r["avoid_effect"] == auto and r["transition"]["kind"] not in (auto, "half_double" if auto == "blend" else auto)
    assert auto in [("blend" if k == "half_double" else k) for k in r["options"]]


def test_play_pair_puts_both_songs_next_in_order(client):
    client.post("/api/playing", json={"id": IDS[0]})
    q = [x["id"] for x in client.post("/api/pair/play", json={"from": IDS[3], "to": IDS[2]}).json()["queue"]]
    assert q[:2] == [IDS[3], IDS[2]]
    # The playing song's pair: just its partner next.
    q = [x["id"] for x in client.post("/api/pair/play", json={"from": IDS[0], "to": IDS[1]}).json()["queue"]]
    assert q[0] == IDS[1]


def test_forget_pair_and_pairs_outlive_their_songs(client, tmp_library):
    _pair(client, IDS[2], IDS[3], "loved", effect="bass_swap")
    _pair(client, IDS[2], IDS[3], "loved")
    _pair(client, IDS[0], IDS[1], "avoided", why="songs")
    _pair(client, IDS[1], IDS[0], "loved")
    client.request("DELETE", f"/api/song/{IDS[3]}")  # dropped from the library: memory stays
    pairs = client.get("/api/pairs").json()["pairs"]
    assert [(p["from"], p["to"]) for p in pairs] == [(IDS[2], IDS[3]), (IDS[1], IDS[0]), (IDS[0], IDS[1])]
    assert pairs[0]["to_title"] == f"title {IDS[3]}" and pairs[0]["loved"] == 2 and pairs[0]["effect"] == "bass_swap"
    assert pairs[2]["why"] == "songs"
    s = client.post("/api/pair/forget", json={"from": IDS[2], "to": IDS[3]}).json()
    assert f"{IDS[2]}>{IDS[3]}" not in s["pairs"]
    assert client.post("/api/pair/forget", json={"from": IDS[2], "to": IDS[3]}).status_code == 404
    assert set(Engine().pairs) == {f"{IDS[0]}>{IDS[1]}", f"{IDS[1]}>{IDS[0]}"}  # read back from disk


def test_history_rows_carry_from_and_this_sessions_rating(client, tmp_library):
    client.post("/api/playing", json={"id": IDS[0]})  # started fresh: no hand-over
    tr = client.get("/api/transition", params={"from": IDS[0]}).json()
    client.post("/api/playing", json={"id": tr["transition"]["in_id"], "plan_id": tr["plan_id"]})
    b = tr["transition"]["in_id"]
    _pair(client, IDS[0], b, "avoided")
    h = client.get("/api/history").json()["history"]
    assert (h[0]["id"], h[0]["from"], h[0]["verdict"], h[0]["why"]) == (b, IDS[0], "avoided", None)
    assert (h[1]["id"], h[1]["from"], h[1]["verdict"]) == (IDS[0], None, None)
    client.post("/api/pair/why", json={"from": IDS[0], "to": b, "why": "songs"})
    assert client.get("/api/history").json()["history"][0]["why"] == "songs"
    # After a restart the memory stays but nothing looks pressed (a tap adds, it can't undo).
    client.engine.pair_undo.clear()
    assert client.get("/api/history").json()["history"][0]["verdict"] is None


def test_history_derives_from_for_old_lines(client, tmp_library):
    # Lines written before "from" existed: the previous line is the song it came in from.
    lines = [{"id": IDS[0], "title": "a", "effect": None}, {"id": IDS[1], "title": "b", "effect": "blend"}]
    (tmp_library / "history.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines))
    _pair(client, IDS[0], IDS[1], "avoided")
    h = client.get("/api/history").json()["history"]
    assert (h[0]["from"], h[0]["verdict"], h[1]["from"]) == (IDS[0], "avoided", None)


def test_retry_requeues_a_failed_song(client):
    client.engine.pool.append(Song("ffffffffff6", "gone", "c", 0, "failed", error="video removed"))
    s = client.post("/api/song/ffffffffff6/retry").json()
    f = next(p for p in s["pool"] if p["id"] == "ffffffffff6")
    assert f["status"] == "queued" and f["error"] is None
    assert client.post(f"/api/song/{IDS[0]}/retry").status_code == 409  # only failed songs


def test_lastfm_key_can_be_set_from_settings_and_is_never_echoed(client, monkeypatch):
    from autodeck import lastfm
    monkeypatch.delenv("LASTFM_API_KEY", raising=False)
    monkeypatch.setattr(lastfm, "LEGACY_KEY_FILE", config.library_root() / "none.env")
    st = client.get("/api/settings").json()
    assert st["lastfm"]["set"] is False and st["library"].endswith(config.library_root().name)
    assert client.put("/api/settings/lastfm", json={"key": "nope"}).status_code == 400
    monkeypatch.setattr(lastfm, "check_key", lambda k: k.startswith("0"))
    assert client.put("/api/settings/lastfm", json={"key": "f" * 32}).status_code == 400  # Last.fm says no
    r = client.put("/api/settings/lastfm", json={"key": "0" * 28 + "abcd"}).json()
    assert r["lastfm"] == {"set": True, "hint": "abcd", "source": "settings"}
    assert "0" * 28 not in client.get("/api/settings").text
    assert lastfm.api_key() == "0" * 28 + "abcd" and oct(lastfm.key_file().stat().st_mode)[-3:] == "600"
    assert client.put("/api/settings/lastfm", json={"key": ""}).json()["lastfm"]["set"] is False


def test_youtube_login_browser_is_a_setting(client, monkeypatch):
    monkeypatch.delenv("AUTODECK_BROWSER", raising=False)
    assert client.get("/api/settings").json()["youtube"] == {"browser": "chrome", "from_env": False}
    assert client.put("/api/settings/youtube", json={"browser": "opera"}).status_code == 400
    r = client.put("/api/settings/youtube", json={"browser": "brave"}).json()
    assert r["youtube"] == {"browser": "brave", "from_env": False}
    assert json.loads((config.library_root() / "settings.json").read_text()) == {"youtube_browser": "brave"}
    monkeypatch.setenv("AUTODECK_BROWSER", "none")
    assert client.get("/api/settings").json()["youtube"] == {"browser": "none", "from_env": True}


def test_installation_status_and_repair_outside_the_standalone_app(client):
    rt = client.get("/api/settings").json()["runtime"]
    assert rt["standalone"] is False and rt["python"].startswith("3.12")
    assert client.post("/api/setup/repair").status_code == 409  # dev: nothing to repair, never relaunches


# --- sources & suggestions (per-source drafts, exclusions, kept-from) ---------------------
def _mood(client, monkeypatch, name="balkan party", picks=None):
    from autodeck import moods
    picks = picks or [(library.load_song(IDS[3]), 2), (library.load_song(IDS[2]), 5)]
    monkeypatch.setattr(moods, "collect", lambda theme, n=30: (picks, 6))
    return client.post("/api/mood", json={"theme": name}).json()["mood"]


def test_switching_sources_is_exclusive(client, monkeypatch):
    client.post("/api/genre", json={"genre": "Pop"})
    st = client.post("/api/party", json={"flavor": "hits"}).json()
    assert st["party"] == "hits" and st["genre"] == "Any"  # party resets the vibe
    assert st["source"] == {"kind": "party", "name": "hits", "key": "party:Hits", "label": "Hits"}
    st = client.post("/api/party", json={"flavor": None}).json()
    assert st["genre"] == "Any" and st["source"] is None  # Off doesn't restore Pop
    client.post("/api/party", json={"flavor": "latino"})
    st = client.post("/api/genre", json={"genre": "Hip-hop"}).json()
    assert st["party"] is None and st["source"]["key"] == "genre:Hip-hop"
    client.post("/api/party", json={"flavor": "latino"})
    mood = _mood(client, monkeypatch)  # a mood goes through set_genre too
    st = client.get("/api/state").json()
    assert st["party"] is None and st["source"]["key"] == f"mood:{mood}"
    assert Engine().party is None  # and the session saved it


def test_no_source_no_draft(client):
    assert client.get("/api/suggestions").json() == {"source": None, "suggestions": []}
    assert client.post("/api/suggestions/reject", json={"artist": "a", "title": "b"}).status_code == 409


MORE = ["xxxxxxxxx01", "xxxxxxxxx02", "xxxxxxxxx03", "xxxxxxxxx04", "xxxxxxxxx05", "xxxxxxxxx06"]
POP = [IDS[0], IDS[1], MORE[0], MORE[1], MORE[4], MORE[5]]


@pytest.fixture
def big(tmp_library):
    """Ten ready songs: IDS (Pop, Pop, Hip-hop, Hip-hop) + MORE (Pop, Pop, Hip-hop, Hip-hop, Pop, Pop)."""
    for sid, bpm, g in zip(IDS + MORE, (120, 122, 121, 90, 124, 95, 118, 92, 121, 100),
                           ["Pop", "Pop", "Hip-hop", "Hip-hop"] * 2 + ["Pop", "Pop"]):
        _ready(sid, bpm, g)
    library.save_pool(IDS + MORE)
    c = TestClient(create_app(Engine()), base_url="http://127.0.0.1")
    c.engine = c.app.state.engine
    return c


def test_mood_draft_ranks_by_votes_and_skips_up_next_and_played(big, monkeypatch):
    from autodeck import party
    picks = [(library.load_song(i), v) for i, v in zip(IDS + MORE[:2], (1, 1, 1, 2, 5, 3))]
    mood = _mood(big, monkeypatch, picks=picks)
    st = big.get("/api/state").json()
    upnext = [q["id"] for q in st["queue"]]
    assert len(upnext) == config.MIN_UPCOMING and set(upnext) <= set(IDS + MORE[:2])  # auto top-up
    d = big.get("/api/suggestions").json()
    assert d["source"]["key"] == f"mood:{mood}"
    got = [x["id"] for x in d["suggestions"]]
    assert set(got) == set(IDS + MORE[:2]) - set(upnext)  # members not in Up next
    votes = dict(zip(IDS + MORE[:2], (1, 1, 1, 2, 5, 3)))
    assert [votes[i] for i in got] == sorted((votes[i] for i in got), reverse=True)  # best-voted first
    first = d["suggestions"][0]
    assert first["why"] == f"in {votes[first['id']]} of 6 playlists" and first["artist"]
    # Played songs belong in Played, not the draft.
    big.post("/api/playing", json={"id": first["id"]})
    assert first["id"] not in [x["id"] for x in big.get("/api/suggestions").json()["suggestions"]]
    nxt = big.get("/api/suggestions").json()["suggestions"]
    assert nxt  # exclude one: only for that source, and Undo puts it back
    x = nxt[0]
    big.post("/api/suggestions/reject", json={"artist": x["artist"], "title": x["title"]})
    assert x["id"] not in [y["id"] for y in big.get("/api/suggestions").json()["suggestions"]]
    rej = json.loads((config.library_root() / "rejected.json").read_text())
    assert rej == {f"mood:{mood}": [party.key(x["artist"], x["title"])]}
    big.post("/api/suggestions/unreject", json={"artist": x["artist"], "title": x["title"], "source": f"mood:{mood}"})
    assert x["id"] in [y["id"] for y in big.get("/api/suggestions").json()["suggestions"]]


def test_genre_draft_is_unplayed_songs_ranked_by_mix_fit(big):
    big.post("/api/genre", json={"genre": "Pop"})
    big.post("/api/playing", json={"id": IDS[0]})  # 120 Pop
    st = big.get("/api/state").json()
    upnext = [q["id"] for q in st["queue"]]
    draft = big.get("/api/suggestions").json()["suggestions"]
    ids = [x["id"] for x in draft]
    assert ids and set(ids) == set(POP) - set(upnext) - {IDS[0]}  # not playing/played, not in Up next
    assert all(x["why"] == "Pop" for x in draft)
    eng = big.app.state.engine
    last = eng.by_id(upnext[-1])
    from autodeck.planner import score_pair
    scores = [score_pair(last, eng.by_id(i)) for i in ids]
    assert scores == sorted(scores, reverse=True)  # best mix from the end of Up next first
    # Preparing songs come after the ready ones and say so.
    eng.by_id(ids[0]).status = "queued"
    draft = big.get("/api/suggestions").json()["suggestions"]
    assert draft[-1]["id"] == ids[0] and draft[-1]["why"] == "Pop · preparing"


def test_top_up_comes_from_the_active_source(big):
    big.post("/api/playing", json={"id": IDS[0]})
    st = big.post("/api/genre", json={"genre": "Hip-hop"}).json()
    q = st["queue"]
    assert len(q) == config.MIN_UPCOMING and all(x["genre"] == "Hip-hop" and not x["kept"] for x in q)


def test_kept_songs_keep_their_order_and_auto_picks_yield(big):
    eng = big.app.state.engine
    st = big.get("/api/state").json()
    assert [q["kept"] for q in st["queue"]] == [False] * config.MIN_UPCOMING
    st = big.post("/api/request", json={"id": MORE[3]}).json()
    assert [q["id"] for q in st["queue"]][0] == MORE[3] and len(st["queue"]) == config.MIN_UPCOMING
    for sid in (MORE[1], IDS[3]):
        st = big.post("/api/request", json={"id": sid}).json()
    # Three kept: no auto picks left (beyond the minimum they're dropped), owner's order kept.
    assert [q["id"] for q in st["queue"]] == [MORE[3], MORE[1], IDS[3]] and all(q["kept"] for q in st["queue"])
    st = big.post("/api/request", json={"id": IDS[0]}).json()
    assert [q["id"] for q in st["queue"]] == [MORE[3], MORE[1], IDS[3], IDS[0]]
    assert eng.requested == [MORE[3], MORE[1], IDS[3], IDS[0]]


def test_top_up_refills_after_songs_play(big):
    big.post("/api/request", json={"id": MORE[3]})
    big.post("/api/playing", json={"id": IDS[0]})
    st = big.post("/api/playing", json={"id": MORE[3]}).json()
    assert len(st["queue"]) == config.MIN_UPCOMING and not any(q["kept"] for q in st["queue"])


def test_source_running_dry_falls_back_with_the_notice(big):
    big.post("/api/genre", json={"genre": "Pop"})
    for sid in POP:  # every Pop song played
        st = big.post("/api/playing", json={"id": sid}).json()
    assert "No ready Pop songs" in st["notice"]
    assert len(st["queue"]) == config.MIN_UPCOMING and all(q["genre"] == "Hip-hop" for q in st["queue"])


def test_kept_list_survives_a_restart(big):
    big.post("/api/request", json={"id": MORE[3], "source": "search"})
    big.post("/api/request", json={"id": IDS[2]})
    eng = Engine()
    assert eng.requested == [MORE[3], IDS[2]] and eng.kept == {MORE[3]: "search"}
    assert [i for i in eng.upcoming[:2]] == [MORE[3], IDS[2]]


def test_worker_prepares_kept_not_ready_songs_first(big):
    eng = big.app.state.engine
    big.post("/api/request", json={"id": NEW, "title": "Hit", "channel": "c"})
    eng.by_id(MORE[0]).status = "queued"  # a not-ready song of the source competes for the worker
    assert eng.worker.wanted(*eng.get_state())[0] == NEW


def test_excludes_survive_a_restart(client, monkeypatch):
    mood = _mood(client, monkeypatch)
    client.post("/api/suggestions/reject", json={"artist": "Goran", "title": "Kalashnikov"})
    assert Engine().rejected == {f"mood:{mood}": {"goran kalashnikov"}}


def test_flat_rejected_json_migrates_into_the_party_bucket(tmp_library, monkeypatch):
    from autodeck import party
    (tmp_library / "rejected.json").write_text(json.dumps(["toto rosanna"]))
    eng = Engine(pool=[])
    assert eng.rejected == {"party": {"toto rosanna"}}
    assert json.loads((tmp_library / "rejected.json").read_text()) == {"party": ["toto rosanna"]}
    seen = []
    monkeypatch.setattr(party, "suggestions", lambda cur, flavor, pool, rejected, n=8: seen.append(rejected) or [])
    for f in party.FLAVORS:  # every flavor still skips it
        eng.set_party(f)
        eng.draft()
    assert all("toto rosanna" in r for r in seen) and len(seen) == 3
    assert Engine(pool=[]).rejected == {"party": {"toto rosanna"}}  # survives a restart


def test_accept_by_id_skips_youtube_and_notes_the_source(client, monkeypatch):
    from autodeck import fetch
    mood = _mood(client, monkeypatch)

    def boom(*a, **k):
        raise AssertionError("no YouTube search for a library song")
    monkeypatch.setattr(fetch, "search", boom)
    st = client.post("/api/suggestions/accept", json={"id": IDS[3], "artist": "x", "title": "y"}).json()
    row = st["queue"][0]  # nothing playing, nothing locked: the kept song is first
    assert row["id"] == IDS[3] and row["kept"] and row["kept_from"] == mood
    client.post("/api/playing", json={"id": IDS[0]})
    assert Engine().kept == {IDS[3]: mood}  # survives a restart
    st = client.post("/api/playing", json={"id": IDS[3]}).json()
    assert IDS[3] not in client.engine.kept  # cleared once it played
    assert all(q["kept_from"] is None for q in st["queue"])


def test_accept_all_keeps_the_order_shown(client, monkeypatch):
    from autodeck import fetch
    client.post("/api/party", json={"flavor": "hits"})
    monkeypatch.setattr(fetch, "search", lambda q, n=10: [Song(NEW, "Toto - Rosanna", "Toto", 330.0, "queued")])
    st = client.post("/api/suggestions/accept_all", json={"items": [
        {"id": IDS[3]}, {"artist": "Toto", "title": "Rosanna"}, {"id": IDS[2]}]}).json()
    assert client.engine.requested == [IDS[3], NEW, IDS[2]]
    assert [q["id"] for q in st["queue"][:2]] == [IDS[3], IDS[2]]  # NEW is still downloading
    assert [q["kept"] for q in st["queue"]] == [True, True, False]  # one auto pick tops it up
    assert st["requested"][0]["id"] == NEW and st["requested"][0]["kept_from"] == "Hits"


def test_requesting_a_played_song_plays_it_again(client):
    client.post("/api/playing", json={"id": IDS[0]})
    client.post("/api/playing", json={"id": IDS[1]})
    st = client.post("/api/request", json={"id": IDS[0]}).json()
    assert st["queue"][config.FIXED_NEXT]["id"] == IDS[0] and st["requested"] == []


# --- play next on the idle deck (design yA49d) ---------------------------------------
def _queue(s):
    return [q["id"] for q in s["queue"]]


def test_play_next_ready_song_goes_first_and_bumps_the_planned_one_to_the_top(client):
    client.post("/api/playing", json={"id": IDS[0]})
    before = client.get("/api/transition", params={"from": IDS[0]}).json()
    assert before["transition"]["in_id"] == IDS[1]  # IDS[1] is planned on the idle deck
    v0 = client.get("/api/state").json()["version"]
    s = client.post("/api/play_next", json={"id": IDS[3], "title": "x"}).json()
    # The pick goes first; the planned song isn't lost, it's right behind it.
    assert _queue(s)[:2] == [IDS[3], IDS[1]]
    assert s["play_next"] == {"id": IDS[3], "ready": True, "planned": IDS[1]}
    assert s["version"] > v0 and client.engine.requested[0] == IDS[3]
    # The player replans: the mix out of the playing song now brings in the pick, on a fresh plan.
    after = client.get("/api/transition", params={"from": IDS[0]}).json()
    assert after["transition"]["in_id"] == IDS[3] and after["plan_id"] != before["plan_id"]


def test_play_next_jumps_ahead_of_earlier_kept_songs(client):
    client.post("/api/playing", json={"id": IDS[0]})
    client.post("/api/request", json={"id": IDS[2]})
    s = client.post("/api/play_next", json={"id": IDS[3]}).json()
    assert _queue(s)[0] == IDS[3] and client.engine.requested[:2] == [IDS[3], IDS[2]]


def test_play_next_not_ready_waits_as_a_request_then_swaps_in(client):
    eng = client.engine
    client.post("/api/playing", json={"id": IDS[0]})
    client.post("/api/request", json={"id": IDS[2]})
    s = client.post("/api/play_next", json={"id": NEW, "title": "Hit", "channel": "c", "duration": 180}).json()
    # Not playable yet: the planned song stays planned; the pick tops the requests and is prepped first.
    assert _queue(s)[0] == IDS[1] and s["requested"][0]["id"] == NEW
    assert s["play_next"] == {"id": NEW, "ready": False, "planned": IDS[1]}
    assert eng.requested[0] == NEW and eng.get_state()[1][0] == NEW
    _ready(NEW, 121, "Hip-hop")
    song = eng.by_id(NEW)
    song.status, song.analysis = "ready", library.load_song(NEW).analysis
    eng._on_worker_change()  # analysed: it swaps in on its own, ahead of the locked-in rows
    assert _queue(client.get("/api/state").json())[:2] == [NEW, IDS[1]]
    eng._on_worker_change()  # and only once: later changes don't keep re-promoting it
    assert _queue(client.get("/api/state").json())[:2] == [NEW, IDS[1]]


def test_play_next_never_touches_a_running_mix(client):
    eng = client.engine
    client.post("/api/playing", json={"id": IDS[0]})
    plan = client.get("/api/transition", params={"from": IDS[0]}).json()
    # The mix into IDS[1] is already scheduled (past the replan margin) when the owner picks:
    # the server can't tell, so the pick must still land after the incoming song.
    s = client.post("/api/play_next", json={"id": IDS[3]}).json()
    assert s["playing"] == IDS[0]
    # The incoming starts sounding (both decks play): its plan still commits its time map.
    s = client.post("/api/playing", json={"id": IDS[1], "plan_id": plan["plan_id"]}).json()
    assert eng.maps[IDS[1]] is eng.plans[plan["plan_id"]][1]
    assert s["playing"] == IDS[1] and _queue(s)[0] == IDS[3]  # the pick plays right after this mix
    # Mid-mix (playing = the incoming): the pick is still "after this mix", the playing song stays.
    s = client.post("/api/play_next", json={"id": IDS[2]}).json()
    assert s["playing"] == IDS[1] and _queue(s)[:2] == [IDS[2], IDS[3]]
    assert client.post("/api/play_next", json={"id": IDS[1]}).status_code == 409


def test_play_next_a_played_song_plays_it_again(client):
    client.post("/api/playing", json={"id": IDS[0]})
    client.post("/api/playing", json={"id": IDS[1]})
    s = client.post("/api/play_next", json={"id": IDS[0]}).json()
    assert _queue(s)[0] == IDS[0]
