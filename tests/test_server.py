import json

import numpy as np
import pytest
import soundfile as sf
from fastapi.testclient import TestClient

from autodeck import engine as engine_mod
from autodeck import library
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
    assert set(s["queue"][0]) == {"id", "title", "channel", "status", "error", "genre", "bpm", "camelot"}
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

    def spy(x, y, after=None, entered=None):
        seen["after"] = after
        return real(x, y, after=after, entered=entered)

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
                        lambda x, y, after=None, entered=None: next(trs) if x.id == a else real(x, y, after, entered))
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
    monkeypatch.setattr(engine_mod, "pick_transition", lambda x, y, after=None, entered=None:
                        Transition("blend", a, b, 32.0, 0.0, 8.0, 1.05) if x.id == a else real(x, y, after, entered))
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
