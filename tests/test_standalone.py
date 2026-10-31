"""First-launch setup of the standalone app: when it runs, progress parsing, resumable downloads."""
import hashlib
import io
import threading

import pytest

from autodeck import standalone as st

WANT = st.expected("0.2.0", "lockhash", "3.12.9")


def test_setup_runs_until_a_complete_marker_matches():
    assert st.setup_reasons(None, WANT, venv_ok=False, models_ok=False) == ["first run"]
    assert st.setup_reasons(dict(WANT), WANT, True, True) == []
    # A new app version with the same lock needs no reinstall.
    assert st.setup_reasons({**WANT, "app_version": "0.1.0"}, WANT, True, True) == []
    assert st.setup_reasons({**WANT, "lock": "old"}, WANT, True, True) == ["packages changed"]
    assert st.setup_reasons({**WANT, "python": "3.12.8"}, WANT, True, True) == ["Python changed"]
    # Something deleted behind our back (or a half-finished first run that left an old marker).
    assert st.setup_reasons(dict(WANT), WANT, False, False) == ["packages missing", "models missing"]
    assert st.setup_reasons({**WANT, "repair": True}, WANT, True, True) == ["repair requested"]


def test_reasons_reads_the_marker_on_disk(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTODECK_HOME", str(tmp_path))
    monkeypatch.setattr(st, "want", lambda: WANT)
    assert st.reasons() == ["first run"]
    st.marker_file().parent.mkdir(parents=True)
    st.marker_file().write_text("{broken")
    assert st.reasons() == ["first run"]  # a corrupt marker is no marker
    st.marker_file().write_text('{"app_version": "0.2.0", "lock": "lockhash", "python": "3.12.9"}')
    assert "first run" not in st.reasons() and "models missing" in st.reasons()
    st.request_repair()
    assert st.read_marker()["repair"] is True and st.read_marker()["lock"] == "lockhash"


def test_uv_lines_are_parsed():
    assert st.parse_uv_line("Downloading torch (70.2MiB)\n") == ("start", "torch", 70.2 * 2**20)
    assert st.parse_uv_line("   Downloading pyobjc-core (6.4MiB)") == ("start", "pyobjc-core", 6.4 * 2**20)
    assert st.parse_uv_line(" Downloaded torch\n") == ("done", "torch", 0.0)
    assert st.parse_uv_line("      Built proxy-tools==0.1.0") == ("done", "proxy-tools", 0.0)
    assert st.parse_uv_line("Prepared 10 packages in 1.52s")[0] == "prepared"
    assert st.parse_uv_line("Installed 10 packages in 23ms")[0] == "installed"
    assert st.parse_uv_line(" + torch==2.15.0") is None


def test_dry_run_lists_what_will_be_installed():
    out = "Would download 2 packages\nWould install 2 packages\n + torch==2.15.0\n + yt-dlp==2026.10.26\n - old==1\n"
    assert st.would_install(out) == {"torch", "yt-dlp"}


def test_lock_sizes_pick_the_wheel_for_this_mac():
    lock = '''
[[package]]
name = "torch"
version = "2.15.0"
wheels = [
    { url = "https://x/torch-2.15.0-cp312-cp312-manylinux_2_28_x86_64.whl", size = 900 },
    { url = "https://x/torch-2.15.0-cp312-none-macosx_11_0_arm64.whl", size = 70 },
    { url = "https://x/torch-2.15.0-cp313-none-macosx_11_0_arm64.whl", size = 71 },
]

[[package]]
name = "six"
version = "1"
sdist = { url = "https://x/six-1.tar.gz", size = 5 }
wheels = [{ url = "https://x/six-1-py2.py3-none-any.whl", size = 3 }]

[[package]]
name = "proxy-tools"
version = "0.1.0"
sdist = { url = "https://x/proxy_tools-0.1.0.tar.gz", size = 2 }
'''
    assert st.lock_sizes(lock) == {"torch": 70, "six": 3, "proxy-tools": 2}


def test_progress_is_weighted_by_bytes():
    p = st.Progress([("Python packages", 900.0), ("AI models", 100.0), ("Finishing", 1.0)])
    p.step(0, status="running", done=450.0, detail="Downloading torch")
    snap = p.snapshot()
    assert snap["percent"] == pytest.approx(45.0, abs=0.1) and snap["steps"][0]["percent"] == 50
    p.step(0, status="done")
    p.step(1, status="running", done=500.0)  # clamped to the step's own size
    assert p.snapshot()["percent"] == pytest.approx(99.9, abs=0.1)


class _Resp(io.BytesIO):
    def __init__(self, data, status):
        super().__init__(data)
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_download_resumes_a_part_file_and_checks_the_hash(tmp_path, monkeypatch):
    data = bytes(range(256)) * 40
    sha = hashlib.sha256(data).hexdigest()
    dest = tmp_path / "m" / "model.bin"
    dest.parent.mkdir()
    (dest.parent / "model.bin.part").write_bytes(data[:1000])  # an interrupted earlier run
    seen = {}

    def urlopen(req, timeout):
        seen["range"] = req.get_header("Range")
        return _Resp(data[1000:], 206)
    monkeypatch.setattr(st.urllib.request, "urlopen", urlopen)
    got = []
    st.download("https://x/model.bin", dest, sha, len(data), got.append, threading.Event())
    assert seen["range"] == "bytes=1000-" and dest.read_bytes() == data and got[-1] == len(data)
    assert not (dest.parent / "model.bin.part").exists()


def test_download_rejects_a_corrupt_file(tmp_path, monkeypatch):
    monkeypatch.setattr(st.urllib.request, "urlopen", lambda req, timeout: _Resp(b"x" * 10, 200))
    with pytest.raises(RuntimeError, match="sha256"):
        st.download("https://x/m", tmp_path / "m", "0" * 64, 10, lambda n: None, threading.Event())
    assert list(tmp_path.iterdir()) == []


def test_download_can_be_cancelled_and_keeps_the_part(tmp_path, monkeypatch):
    monkeypatch.setattr(st.urllib.request, "urlopen", lambda req, timeout: _Resp(b"x" * (3 << 20), 200))
    stop = threading.Event()
    stop.set()
    with pytest.raises(st.Cancelled):
        st.download("https://x/m", tmp_path / "m", "0" * 64, 3 << 20, lambda n: None, stop)
    assert (tmp_path / "m.part").exists()  # Retry / next launch continues from here
