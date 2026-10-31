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


def test_age_gate_with_cookies_hint_is_not_cookie_error(tmp_library, monkeypatch):
    msg = ("ERROR: [youtube] abc123: Sign in to confirm your age. Use --cookies-from-browser "
           "or --cookies for the authentication. See https://github.com/yt-dlp/yt-dlp/wiki/FAQ")
    monkeypatch.setattr(yt_dlp, "YoutubeDL", _raising_ydl(DownloadError(msg)))
    with pytest.raises(fetch.FetchError) as e:
        fetch.download(_song())
    assert "Cool Song" in str(e.value) and "confirm your age" in str(e.value)
    assert str(e.value) != fetch._cookie_error()


def test_cookie_load_error_type_maps(tmp_library, monkeypatch):
    from yt_dlp.cookies import CookieLoadError
    monkeypatch.setattr(yt_dlp, "YoutubeDL", _raising_ydl(CookieLoadError("failed to load cookies")))
    with pytest.raises(fetch.FetchError, match="Couldn't read YouTube login"):
        fetch.download(_song())


def _writing_ydl(fail):
    class Y:
        def __init__(self, opts): self.dir = opts["outtmpl"].rsplit("/", 1)[0]
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def download(self, urls):
            import pathlib
            pathlib.Path(self.dir).mkdir(parents=True, exist_ok=True)  # yt-dlp creates it
            pathlib.Path(self.dir, "dl.opus").write_bytes(b"partial")
            pathlib.Path(self.dir, "dl.webm.part").write_bytes(b"x")
            if fail:
                raise DownloadError("ERROR: boom")
    return Y


def test_failed_download_leaves_no_audio_or_scratch(tmp_library, monkeypatch):
    monkeypatch.setattr(yt_dlp, "YoutubeDL", _writing_ydl(True))
    with pytest.raises(fetch.FetchError):
        fetch.download(_song())
    assert sorted(p.name for p in song_dir("abc123").iterdir()) == []


def test_successful_download_renames_and_cleans(tmp_library, monkeypatch):
    monkeypatch.setattr(yt_dlp, "YoutubeDL", _writing_ydl(False))
    p = fetch.download(_song())
    assert p.name == "audio.opus" and p.read_bytes() == b"partial"
    assert [x.name for x in song_dir("abc123").iterdir()] == ["audio.opus"]


def test_browser_none_disables_cookies(tmp_library, monkeypatch):
    from autodeck import fetch
    monkeypatch.setenv("AUTODECK_BROWSER", "none")
    assert fetch._cookie_opts() == {}
    monkeypatch.setenv("AUTODECK_BROWSER", "firefox")
    assert fetch._cookie_opts() == {"cookiesfrombrowser": ("firefox",)}
    monkeypatch.delenv("AUTODECK_BROWSER")
    assert fetch._cookie_opts() == {"cookiesfrombrowser": ("chrome",)}


def test_settings_browser_is_used_unless_the_env_var_overrides(tmp_library, monkeypatch):
    from autodeck import config, fetch
    monkeypatch.delenv("AUTODECK_BROWSER", raising=False)
    config.save_setting("youtube_browser", "safari")
    assert fetch._cookie_opts() == {"cookiesfrombrowser": ("safari",)}
    assert "from Safari" in fetch._cookie_error()
    config.save_setting("youtube_browser", "none")
    assert fetch._cookie_opts() == {}
    monkeypatch.setenv("AUTODECK_BROWSER", "firefox")
    assert fetch._cookie_opts() == {"cookiesfrombrowser": ("firefox",)}
    monkeypatch.delenv("AUTODECK_BROWSER")
    config.save_setting("youtube_browser", "netscape")  # unknown (hand-edited file): default
    assert config.youtube_browser() == "chrome"
    (tmp_library / "settings.json").write_text("{broken")
    assert config.youtube_browser() == "chrome"


def test_download_asks_ffmpeg_for_opus(tmp_library, monkeypatch):
    # Opus, not FLAC: the YouTube stream is lossy already, FLAC was a 15x bigger copy (~60 MB/song).
    seen = {}

    class Y:
        def __init__(self, opts): seen.update(opts)
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def download(self, urls): raise DownloadError("ERROR: stop")
    monkeypatch.setattr(yt_dlp, "YoutubeDL", Y)
    with pytest.raises(fetch.FetchError):
        fetch.download(_song())
    assert seen["postprocessors"][0]["preferredcodec"] == "opus"


def test_legacy_flac_still_counts_as_cached(tmp_library, monkeypatch):
    # Libraries from before the Opus switch keep working without a re-download.
    p = song_dir("abc123") / "audio.flac"
    p.parent.mkdir(parents=True)
    p.write_bytes(b"x")
    monkeypatch.setattr(yt_dlp, "YoutubeDL", _raising_ydl(AssertionError("must not be called")))
    assert fetch.download(_song()) == p


def test_search_returns_songs_from_youtube_search(monkeypatch):
    seen = {}

    class Y:
        def __init__(self, opts): seen["opts"] = opts
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def extract_info(self, url, download=False):
            seen["url"] = url
            return {"entries": [{"id": "abcdefghijk", "title": "Sârba", "channel": "Taraf", "duration": 200},
                                {"id": "zzzzzzzzzzz", "title": "[Private video]"}]}
    monkeypatch.setattr(yt_dlp, "YoutubeDL", Y)
    res = fetch.search("sarba taraf", 8)
    assert seen["url"] == "ytsearch8:sarba taraf"
    assert seen["opts"]["extract_flat"]  # one request, titles only
    assert [(s.id, s.title, s.duration) for s in res] == [("abcdefghijk", "Sârba", 200.0)]


def test_read_playlist_returns_the_youtube_name(monkeypatch):
    info = {"title": "Balkan brass", "entries": [{"id": "aaaaaaaaaaa", "title": "A"}]}
    monkeypatch.setattr(yt_dlp, "YoutubeDL", _fake_info(info))
    name, songs = fetch.read_playlist("https://www.youtube.com/playlist?list=PL1")
    assert name == "Balkan brass" and [s.id for s in songs] == ["aaaaaaaaaaa"]
