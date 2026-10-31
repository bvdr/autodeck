"""yt-dlp self-update for the frozen app: version picking, PyPI parsing, the download itself (faked)."""
import hashlib
import io
import json
import zipfile

import pytest

from autodeck import standalone as st


def test_versions_compare_numerically():
    assert st.parse_version("2026.10.26") == (2026, 10, 26)
    assert st.parse_version("2026.10.26.1") > st.parse_version("2026.10.26")
    assert st.parse_version("2026.11.2") > st.parse_version("2026.10.26")  # not a string compare
    assert st.parse_version("0.10.0") > st.parse_version("0.9.9")


def test_ejs_pin_is_read_from_requires_dist():
    reqs = ['brotli; extra == "default"', 'yt-dlp-ejs==0.8.0; extra == "default"', 'yt-dlp-ejs==0.8.0; extra == "pin"']
    assert st.ejs_pin(reqs) == "0.8.0"
    assert st.ejs_pin(["yt-dlp-ejs>=0.5"]) is None and st.ejs_pin(None) is None


def test_only_the_pure_python_wheel_is_picked():
    rel = {"urls": [{"packagetype": "sdist", "filename": "yt_dlp-1.tar.gz", "url": "s", "digests": {"sha256": "a"}},
                    {"packagetype": "bdist_wheel", "filename": "yt_dlp-1-py3-none-any.whl", "url": "w", "digests": {"sha256": "b"}}]}
    assert st.pick_wheel(rel) == ("w", "b")
    assert st.pick_wheel({"urls": rel["urls"][:1]}) is None


def _version_dir(pylib, v, complete=True):
    d = pylib / v
    d.mkdir(parents=True)
    if complete:
        (d / st.DONE).write_text(v)
    return d


def test_candidates_are_newer_than_bundled_complete_and_newest_first(tmp_path):
    for v in ("2026.9.1", "2026.11.2", "2026.12.1"):
        _version_dir(tmp_path, v)
    _version_dir(tmp_path, "2027.1.1", complete=False)  # half-downloaded: never used
    (tmp_path / ".2027.2.2.tmp").mkdir()
    assert [p.name for p in st.candidates(tmp_path, "2026.10.26")] == ["2026.12.1", "2026.11.2"]
    assert st.candidates(tmp_path / "missing", "2026.10.26") == []


def _wheel(files: dict) -> bytes:
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        for name, text in files.items():
            z.writestr(name, text)
    return b.getvalue()


@pytest.fixture
def pypi(monkeypatch):
    """Fake PyPI: yt-dlp 2026.11.2 pinning yt-dlp-ejs 0.9.0."""
    wheels = {"yt.whl": _wheel({"yt_dlp/__init__.py": "", "yt_dlp/version.py": "__version__ = '2026.11.2'"}),
              "ejs.whl": _wheel({"yt_dlp_ejs/__init__.py": ""})}
    meta = {
        "https://pypi.org/pypi/yt-dlp/json": {
            "info": {"name": "yt-dlp", "version": "2026.11.2", "requires_dist": ['yt-dlp-ejs==0.9.0; extra == "default"']},
            "urls": [{"packagetype": "bdist_wheel", "filename": "yt_dlp-2026.11.2-py3-none-any.whl", "url": "yt.whl",
                      "digests": {"sha256": hashlib.sha256(wheels["yt.whl"]).hexdigest()}}]},
        "https://pypi.org/pypi/yt-dlp-ejs/0.9.0/json": {
            "info": {"name": "yt-dlp-ejs", "version": "0.9.0"},
            "urls": [{"packagetype": "bdist_wheel", "filename": "yt_dlp_ejs-0.9.0-py3-none-any.whl", "url": "ejs.whl",
                      "digests": {"sha256": hashlib.sha256(wheels["ejs.whl"]).hexdigest()}}]},
    }
    calls = []

    def get(url):
        calls.append(url)
        return json.dumps(meta[url]).encode() if url in meta else wheels[url]
    monkeypatch.setattr(st, "_get", get)
    return {"wheels": wheels, "meta": meta, "calls": calls}


def test_update_downloads_both_wheels_then_waits_a_day(tmp_path, pypi):
    assert st.update(tmp_path, "2026.10.26", now=1000.0) == "2026.11.2"
    d = tmp_path / "2026.11.2"
    assert (d / "yt_dlp" / "version.py").exists() and (d / "yt_dlp_ejs" / "__init__.py").exists()
    assert [p.name for p in st.candidates(tmp_path, "2026.10.26")] == ["2026.11.2"]
    n = len(pypi["calls"])
    assert st.update(tmp_path, "2026.10.26", now=1000.0 + 3600) is None and len(pypi["calls"]) == n  # throttled
    # A day later PyPI is asked again, but 2026.11.2 is already here: nothing downloaded.
    assert st.update(tmp_path, "2026.10.26", now=1000.0 + st.CHECK_EVERY + 1) is None
    assert pypi["calls"][n:] == ["https://pypi.org/pypi/yt-dlp/json"]


def test_update_skips_when_bundled_is_current(tmp_path, pypi):
    assert st.update(tmp_path, "2026.11.2", now=1.0) is None
    assert st.installed(tmp_path) == []


def test_bad_checksum_leaves_nothing_behind(tmp_path, pypi):
    pypi["wheels"]["yt.whl"] = b"tampered"
    with pytest.raises(RuntimeError, match="sha256"):
        st.update(tmp_path, "2026.10.26", now=1.0)
    assert [p.name for p in tmp_path.iterdir()] == ["last-check"]


def test_old_versions_are_pruned_keeping_the_last_good_one(tmp_path, pypi):
    for v in ("2026.10.30", "2026.11.1"):
        _version_dir(tmp_path, v)
    st.update(tmp_path, "2026.10.26", now=1.0)
    assert [p.name for _, p in st.installed(tmp_path)] == ["2026.11.2", "2026.11.1"]


def test_a_version_that_does_not_import_is_dropped(tmp_path, monkeypatch):
    bad = _version_dir(tmp_path, "2099.1.1")
    (bad / "yt_dlp").mkdir()
    (bad / "yt_dlp" / "__init__.py").write_text("raise ImportError('broken release')")
    import sys
    saved = {k: v for k, v in sys.modules.items() if k == "yt_dlp" or k.startswith(("yt_dlp.", "yt_dlp_ejs"))}
    for k in saved:
        del sys.modules[k]
    try:
        assert st.activate(tmp_path, "2026.10.26") is None
        assert not bad.exists() and str(bad) not in sys.path
    finally:
        sys.modules.update(saved)


def test_bundled_version_is_read_without_importing(monkeypatch):
    import yt_dlp.version
    assert st.bundled_ytdlp_version() == yt_dlp.version.__version__


def test_bundled_model_is_only_found_inside_a_frozen_app(tmp_path, monkeypatch):
    import sys
    from autodeck import config
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "m.onnx").write_bytes(b"x")
    assert config.bundled_model("m.onnx") is None  # dev: not frozen, callers download as before
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    assert config.bundled_model("m.onnx") == tmp_path / "models" / "m.onnx"
    assert config.bundled_model("other.onnx") is None
