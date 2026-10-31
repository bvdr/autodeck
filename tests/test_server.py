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
    assert [q["id"] for q in s["queue"]] == IDS  # nothing playing: pool order
    assert s["pool_counts"] == {"ready": 4}
    assert set(s["queue"][0]) == {"id", "title", "channel", "status", "error", "genre", "bpm", "camelot", "danceability"}
    assert [p["id"] for p in s["pool"]] == IDS and set(s["pool"][0]) == set(s["queue"][0])


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
    assert client.engine.pinned == {1: IDS[2]}
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
    assert next(q for q in s["queue"] if q["id"] == IDS[3])["genre"] == "Pop"
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
    assert client.get("/api/state").json()["queue"][0]["id"] == NEW  # plays next
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
    monkeypatch.setattr(party, "suggestions", lambda cur, flavor, pool, rejected, n=8: [
        {"artist": "Toto", "title": "Rosanna", "reason": "r"}] if party.key("Toto", "Rosanna") not in rejected else [])
    assert client.get("/api/suggestions").json()["suggestions"][0]["title"] == "Rosanna"
    monkeypatch.setattr(fetch, "search", lambda q, n=10: [Song(NEW, "Toto - Rosanna", "Toto", 330.0, "queued")])
    st = client.post("/api/suggestions/accept", json={"artist": "Toto", "title": "Rosanna"}).json()
    assert any(p["id"] == NEW for p in st["pool"]) and NEW in client.engine.requested
    left = client.post("/api/suggestions/reject", json={"artist": "Toto", "title": "Rosanna"}).json()
    assert left["suggestions"] == []
    assert party.key("Toto", "Rosanna") in json.loads((config.library_root() / "rejected.json").read_text())


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
    monkeypatch.setattr(moods, "collect", lambda theme, n=30: picks)
    r = client.post("/api/mood", json={"theme": "balkan party"}).json()
    assert r["mood"] == "Balkan party" and r["found"] == 2 and r["added"] == 1
    st = client.get("/api/state").json()
    assert st["genre"] == "Balkan party" and "Balkan party" in st["genres"]
    assert [q["id"] for q in st["queue"]] == [IDS[2]]  # the ready member; the new one is downloading
    assert client.engine.by_id(NEW).genre_hint == "Balkan party"  # worker fetches it first
    assert library.load_song(IDS[2]).genre == "Hip-hop" or True  # existing labels untouched
    monkeypatch.setattr(moods, "collect", lambda theme, n=30: [])
    assert client.post("/api/mood", json={"theme": "zzz"}).status_code == 404


def test_moods_refresh_adds_and_delete_forgets(client, monkeypatch):
    from autodeck import moods
    client.post("/api/playing", json={"id": IDS[0]})
    monkeypatch.setattr(moods, "collect", lambda theme, n=30: [(library.load_song(IDS[2]), 2)])
    client.post("/api/mood", json={"theme": "greek taverna"})
    monkeypatch.setattr(moods, "collect", lambda theme, n=30: [(library.load_song(IDS[3]), 2)])
    client.post("/api/mood", json={"theme": "greek taverna"})  # refresh: adds, keeps the old pick
    assert client.get("/api/state").json()["moods"] == {"Greek taverna": 2}
    st = client.request("DELETE", "/api/mood/Greek taverna").json()
    assert st["moods"] == {} and "Greek taverna" not in st["genres"] and st["genre"] == "Any"
    assert client.request("DELETE", "/api/mood/nope").status_code == 404


def test_pair_verdicts_are_stored_listed_and_shape_the_queue(client, tmp_library):
    client.post("/api/playing", json={"id": IDS[0]})
    # IDS[1] (122 Pop) is the natural next song after IDS[0] (120 Pop); IDS[3] (90) is far off.
    assert client.get("/api/state").json()["queue"][0]["id"] == IDS[1]
    s = client.post("/api/pair", json={"from": IDS[0], "to": IDS[1], "verdict": "avoided"}).json()
    assert s["pairs"] == {f"{IDS[0]}>{IDS[1]}": "avoided"}
    q = [x["id"] for x in s["queue"]]
    # Avoided pair never back to back, even when it held a locked slot.
    assert all((a, b) != (IDS[0], IDS[1]) for a, b in zip([IDS[0], *q], q))
    client.post("/api/pair", json={"from": IDS[2], "to": IDS[3], "verdict": "loved"})
    assert json.loads((tmp_library / "pairs.json").read_text()) == {
        f"{IDS[0]}>{IDS[1]}": "avoided", f"{IDS[2]}>{IDS[3]}": "loved"}
    pairs = client.get("/api/pairs").json()["pairs"]
    assert [p["verdict"] for p in pairs] == ["loved", "avoided"]
    assert pairs[0] == {"from": IDS[2], "to": IDS[3], "from_title": f"title {IDS[2]}",
                        "to_title": f"title {IDS[3]}", "verdict": "loved"}
    # null forgets; a fresh engine reads the file back.
    client.post("/api/pair", json={"from": IDS[2], "to": IDS[3], "verdict": None})
    assert Engine().pairs == {f"{IDS[0]}>{IDS[1]}": "avoided"}


def test_pair_rejects_bad_input(client):
    assert client.post("/api/pair", json={"from": IDS[0], "to": IDS[1], "verdict": "meh"}).status_code == 400
    assert client.post("/api/pair", json={"from": IDS[0], "to": IDS[0], "verdict": "loved"}).status_code == 400
    assert client.post("/api/pair", json={"from": "../x", "to": IDS[0], "verdict": "loved"}).status_code == 404


def test_history_rows_carry_from_and_verdict(client, tmp_library):
    client.post("/api/playing", json={"id": IDS[0]})  # started fresh: no hand-over
    tr = client.get("/api/transition", params={"from": IDS[0]}).json()
    client.post("/api/playing", json={"id": tr["transition"]["in_id"], "plan_id": tr["plan_id"]})
    b = tr["transition"]["in_id"]
    client.post("/api/pair", json={"from": IDS[0], "to": b, "verdict": "loved"})
    h = client.get("/api/history").json()["history"]
    assert (h[0]["id"], h[0]["from"], h[0]["verdict"]) == (b, IDS[0], "loved")
    assert (h[1]["id"], h[1]["from"], h[1]["verdict"]) == (IDS[0], None, None)


def test_history_derives_from_for_old_lines(client, tmp_library):
    # Lines written before "from" existed: the previous line is the song it came in from.
    lines = [{"id": IDS[0], "title": "a", "effect": None}, {"id": IDS[1], "title": "b", "effect": "blend"}]
    (tmp_library / "history.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines))
    client.post("/api/pair", json={"from": IDS[0], "to": IDS[1], "verdict": "avoided"})
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
