import pytest
import yt_dlp
from yt_dlp.utils import DownloadError

from autodeck import fetch
from autodeck.library import Song, song_dir


@pytest.mark.parametrize("url,expected", [
    ("https://music.youtube.com/playlist?list=LM", ("playlist", "LM")),
    # Review Focus 1: watch link with list= means just that song
    ("https://www.youtube.com/watch?v=abc123&list=RDabc", ("video", "abc123")),
    ("https://youtu.be/abc123", ("video", "abc123")),
    ("https://youtu.be/abc123?si=xyz", ("video", "abc123")),
    ("https://music.youtube.com/watch?v=abc123", ("video", "abc123")),
    ("https://www.youtube.com/playlist?list=PL123", ("playlist", "PL123")),
])
def test_classify_url(url, expected):
    assert fetch.classify_url(url) == expected


@pytest.mark.parametrize("url", ["https://example.com", "not a url", "https://www.youtube.com/"])
def test_classify_url_rejects(url):
    with pytest.raises(ValueError):
        fetch.classify_url(url)


def _song():
    return Song(id="abc123", title="Cool Song", channel="Ch", duration=100.0, status="queued")


def _raising_ydl(exc):
    class Y:
        def __init__(self, opts): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def download(self, urls): raise exc
        def extract_info(self, *a, **k): raise exc
    return Y


def test_download_maps_errors(tmp_library, monkeypatch):
    monkeypatch.setattr(yt_dlp, "YoutubeDL", _raising_ydl(DownloadError("ERROR: Sign in to confirm your age")))
    with pytest.raises(fetch.FetchError) as e:
        fetch.download(_song())
    assert "Cool Song" in str(e.value)
    assert "confirm your age" in str(e.value)


def test_cookie_error_message(tmp_library, monkeypatch):
    monkeypatch.setattr(yt_dlp, "YoutubeDL", _raising_ydl(DownloadError("could not find chrome cookies database")))
    with pytest.raises(fetch.FetchError) as e:
        fetch.download(_song())
    assert str(e.value) == "Couldn't read YouTube login from Chrome. Is Chrome logged in to YouTube?"


def test_download_idempotent(tmp_library, monkeypatch):
    song = _song()
    p = song_dir(song.id) / "audio.flac"
    p.parent.mkdir(parents=True)
    p.write_bytes(b"x")
    monkeypatch.setattr(yt_dlp, "YoutubeDL", _raising_ydl(AssertionError("must not be called")))
    assert fetch.download(song) == p


def _fake_info(info):
    class Y:
        def __init__(self, opts): self.opts = opts
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def extract_info(self, url, download=False): return info
    return Y


def test_expand_single_video(monkeypatch):
    info = {"id": "abc123", "title": "T", "channel": "C", "duration": 200}
    monkeypatch.setattr(yt_dlp, "YoutubeDL", _fake_info(info))
    [s] = fetch.expand("https://www.youtube.com/watch?v=abc123&list=RDabc")
    assert (s.id, s.title, s.channel, s.duration, s.status) == ("abc123", "T", "C", 200.0, "queued")


def test_expand_playlist_skips_unavailable_and_defaults_duration(monkeypatch):
    info = {"entries": [
        {"id": "aaaaaaaaaaa", "title": "A", "channel": "X", "duration": 10},
        {"id": "bbbbbbbbbbb", "title": "[Private video]"},
        {"id": "ccccccccccc", "title": "[Deleted video]"},
        {"id": None, "title": "no id"},
        {"id": "ddddddddddd", "title": "D", "uploader": "U"},  # no duration, no channel
    ]}
    monkeypatch.setattr(yt_dlp, "YoutubeDL", _fake_info(info))
    songs = fetch.expand("https://www.youtube.com/playlist?list=PL1")
    assert [s.id for s in songs] == ["aaaaaaaaaaa", "ddddddddddd"]
    assert songs[1].duration == 0.0 and songs[1].channel == "U"


@pytest.mark.network
def test_expand_public_playlist():
    # Public Creative Commons playlist (third-party, could vanish)
    songs = fetch.expand("https://www.youtube.com/playlist?list=PLbb_NncyMIqvil47jxdJBvefq8TpJW9ow")
    assert len(songs) > 0
    assert all(len(s.id) == 11 for s in songs)
