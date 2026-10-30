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
    c = TestClient(create_app(eng))
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
