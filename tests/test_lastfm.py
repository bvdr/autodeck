import io
import json

import pytest

from autodeck import lastfm


class _Resp(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *a): return False


@pytest.fixture
def fake_api(tmp_library, monkeypatch):
    calls = []
    answers = {}

    def urlopen(req, timeout=None):
        calls.append(req.full_url)
        method = next(p.split("=")[1] for p in req.full_url.split("?")[1].split("&") if p.startswith("method="))
        return _Resp(json.dumps(answers.get(method, {"error": 6})).encode())
    monkeypatch.setenv("LASTFM_API_KEY", "k")
    monkeypatch.setattr(lastfm.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(lastfm, "_MIN_GAP", 0)
    return calls, answers


def test_artist_tags_and_cache(fake_api):
    calls, answers = fake_api
    answers["artist.getTopTags"] = {"toptags": {"tag": [{"name": "Hip-Hop"}, {"name": "Romanian"}]}}
    assert lastfm.artist_tags("B.U.G. Mafia") == ["hip-hop", "romanian"]
    assert lastfm.artist_tags("B.U.G. Mafia") == ["hip-hop", "romanian"]
    assert len(calls) == 1  # second answer came from the disk cache


def test_track_info_and_similar(fake_api):
    _, answers = fake_api
    answers["track.getInfo"] = {"track": {"listeners": "1500000", "toptags": {"tag": [{"name": "80s"}]}}}
    answers["track.getSimilar"] = {"similartracks": {"track": [{"name": "Rosanna", "artist": {"name": "Toto"}}]}}
    assert lastfm.track_info("Toto", "Africa") == {"listeners": 1500000, "tags": ["80s"]}
    assert lastfm.similar_tracks("Toto", "Africa") == [("Toto", "Rosanna")]


def test_unknown_track_and_no_key_degrade_to_empty(fake_api, monkeypatch):
    assert lastfm.track_info("Nobody", "Nothing") == {}
    monkeypatch.delenv("LASTFM_API_KEY")
    monkeypatch.setattr(lastfm, "KEY_FILES", (lastfm.KEY_FILES[0].with_name("missing.env"),))
    assert lastfm.artist_tags("Anyone") == []


def test_network_failure_degrades(tmp_library, monkeypatch):
    monkeypatch.setenv("LASTFM_API_KEY", "k")
    monkeypatch.setattr(lastfm, "_MIN_GAP", 0)

    def boom(*a, **k):
        raise OSError("offline")
    monkeypatch.setattr(lastfm.urllib.request, "urlopen", boom)
    assert lastfm.similar_tracks("Toto", "Africa") == []


@pytest.mark.parametrize("title,channel,expected", [
    ("Toto - Africa (Official HD Video)", "TotoVEVO", ("Toto", "Africa")),
    ("B.U.G. Mafia – Toti Borfasii", "x", ("B.U.G. Mafia", "Toti Borfasii")),
    ("Africa", "Toto - Topic", ("Toto", "Africa")),
    ("Despacito [Lyric Video]", "LuisFonsiVEVO", ("LuisFonsi", "Despacito")),
])
def test_split_title(title, channel, expected):
    assert lastfm.split_title(title, channel) == expected
