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
    cur = song("cur", title="Africa", artist="Toto", tags=["80s"])
    got = party.suggestions(cur, "oldies", [cur], rejected={party.key("Queen", "Bohemian Rhapsody")}, n=8)
    assert [(g["artist"], g["title"]) for g in got] == [("Toto", "Rosanna"), ("a-ha", "Take on Me")]
    assert got[0]["reason"].startswith("people play it with")
