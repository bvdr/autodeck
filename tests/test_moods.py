from autodeck import moods, party
from autodeck.library import Song


def s(id, title, channel="c"):
    return Song(id, title, channel, 200.0, "queued")


def test_rank_by_playlist_votes_with_deezer_bonus_and_dedup():
    p1 = [s("a1", "Goran Bregovic - Kalashnikov"), s("b1", "Shantel - Disko Partizani"), s("c1", "X - Only Here")]
    p2 = [s("b2", "Shantel - Disko Partizani (Official Video)"), s("a2", "Goran Bregovic - Kalashnikov"),
          s("a3", "Goran Bregovic - Kalashnikov")]  # same song twice in one playlist = one vote
    p3 = [s("d1", "Antique - Opa Opa")]
    deezer = {party.key("Antique", "Opa Opa")}
    got = [(x.id, v) for x, v in moods.rank([p1, p2, p3], deezer)]
    assert got[:3] == [("a1", 2), ("b1", 2), ("d1", 2)]  # first upload kept; Deezer adds a vote
    assert ("c1", 1) in got


def test_collect_reads_playlists_and_handles_none(monkeypatch):
    monkeypatch.setattr(moods, "_youtube_playlists", lambda t: [])
    assert moods.collect("nothing") == ([], 0)
    monkeypatch.setattr(moods, "_youtube_playlists", lambda t: ["u1", "u2"])
    monkeypatch.setattr(moods.fetch, "expand", lambda url: [s("a" + url, "A - Song"), s("b" + url, f"B - {url}")])
    monkeypatch.setattr(moods, "_deezer_keys", lambda t: set())
    top, of = moods.collect("theme", n=2)
    assert of == 2  # two playlists, no Deezer vote
    assert top[0][1] == 2 and top[0][0].title == "A - Song" and len(top) == 2
