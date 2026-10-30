from autodeck import party
from autodeck.library import Song


def song(id, title="t", listeners=0, tags=(), artist=None):
    meta = {"artist": artist or "A", "title": title, "listeners": listeners, "tags": list(tags)}
    return Song(id, title, "c", 200.0, "ready", meta=meta)


def test_fits_each_flavor():
    hit, deep = song("h", listeners=2_000_000), song("d", listeners=9_000)
    assert party.fits(hit, "hits") and not party.fits(deep, "hits")
    assert party.fits(song("o", tags=["80s", "pop"]), "oldies")
    assert party.fits(song("l", tags=["reggaeton"]), "latino")
    assert not party.fits(song("r", tags=["romanian", "hip-hop"]), "latino")
    assert not party.fits(Song("x", "t", "c", 1.0, "ready"), "oldies")  # not enriched yet


def test_popularity_is_monotonic_and_bounded():
    vals = [party.popularity(song(str(n), listeners=n)) for n in (0, 10_000, 300_000, 3_000_000, 10**9)]
    assert vals == sorted(vals) and vals[0] == 0 and vals[-1] == 1.0


def test_key_ignores_case_accents_punctuation():
    assert party.key("Lăutarii", "Sârba (Live)") == party.key("lautarii", "sarba live")


def test_suggestions_skip_library_and_rejected(monkeypatch):
    monkeypatch.setattr(party.lastfm, "similar_tracks", lambda a, t, n=25: [("Toto", "Rosanna"), ("Toto", "Africa")])
    monkeypatch.setattr(party.lastfm, "tag_top_tracks", lambda tag, limit=50: [("a-ha", "Take on Me"), ("Queen", "Bohemian Rhapsody")])
    monkeypatch.setattr(party.lastfm, "chart_top_tracks", lambda limit=50: [])
    monkeypatch.setattr(party.deezer, "lookup", lambda a, t: {"bpm": None, "year": None})
    cur = song("cur", title="Africa", artist="Toto", tags=["80s"])
    got = party.suggestions(cur, "oldies", [cur], rejected={party.key("Queen", "Bohemian Rhapsody")}, n=8)
    assert [(g["artist"], g["title"]) for g in got] == [("Toto", "Rosanna"), ("a-ha", "Take on Me")]
    assert got[0]["reason"].startswith("people play it with")


def test_suggestions_show_bpm_and_put_beatmatchable_first(monkeypatch):
    from autodeck.library import Analysis
    monkeypatch.setattr(party.lastfm, "similar_tracks", lambda a, t, n=25: [("X", "Slow"), ("Y", "Close"), ("Z", "Double")])
    monkeypatch.setattr(party.lastfm, "chart_top_tracks", lambda limit=50: [])
    bpms = {"Slow": 70.0, "Close": 124.0, "Double": 61.0}
    monkeypatch.setattr(party.deezer, "lookup", lambda a, t: {"bpm": bpms[t], "year": 1990})
    cur = song("cur", title="Now")
    cur.analysis = Analysis(1, 122.0, [], [], True, None, None, [], [], 0.0, 1.0, None, None)
    got = party.suggestions(cur, None, [cur], set())
    assert [(g["title"], g["mixes"]) for g in got] == [("Close", True), ("Double", True), ("Slow", False)]
    assert got[0]["bpm"] == 124.0 and got[0]["year"] == 1990
