"""yt-dlp keeps itself current in the standalone app.

YouTube breaks old yt-dlp releases within weeks, but the app's packages are pinned by uv.lock.
yt-dlp and yt-dlp-ejs are pure Python, so a newer release can sit beside the pinned one: once a
day (background thread) PyPI is asked for the latest yt-dlp and the yt-dlp-ejs version it pins;
both wheels are sha256-checked and unpacked into pylib/<version>/, and the NEXT launch puts that
dir first on sys.path, before anything imports yt_dlp. The two newest versions are kept; one
that won't import is deleted. "installed" below = the version uv installed from the lock.
"""
import hashlib
import importlib.util
import io
import json
import logging
import os
import re
import shutil
import sys
import threading
import time
import urllib.request
import zipfile
from pathlib import Path

from . import config

log = logging.getLogger(__name__)

CHECK_EVERY = 24 * 3600  # PyPI is asked at most once a day
KEEP = 2                 # newest downloaded versions kept: the current one and the last good one
PYPI = "https://pypi.org/pypi/{name}/{version}json"
DONE = "COMPLETE"        # written last: a dir without it is a half-finished download


def pylib_dir() -> Path:
    return config.data_dir() / "pylib"


# --- pure logic (unit-tested) --------------------------------------------------------
def parse_version(v: str) -> tuple[int, ...]:
    """'2026.10.26' -> (2026, 10, 26). ponytail: digits only, so a pre-release ('1.0rc1') sorts
    after its final release; fine because we only ever compare PyPI's latest *stable* version."""
    return tuple(int(x) for x in re.findall(r"\d+", v))


def ejs_pin(requires_dist: list[str] | None) -> str | None:
    """yt-dlp pins the yt-dlp-ejs release it works with (its challenge-solver scripts are
    version-checked), so the update takes exactly that one, not ejs's own latest."""
    for r in requires_dist or []:
        m = re.match(r"\s*yt-dlp-ejs\s*==\s*([0-9][^;\s,]*)", r)
        if m:
            return m.group(1)
    return None


def pick_wheel(release: dict) -> tuple[str, str] | None:
    """(url, sha256) of the pure-Python wheel in a PyPI release JSON."""
    for u in release.get("urls", []):
        if u.get("packagetype") == "bdist_wheel" and u.get("filename", "").endswith("-py3-none-any.whl"):
            return u["url"], u["digests"]["sha256"]
    return None


def installed(pylib: Path) -> list[tuple[tuple[int, ...], Path]]:
    """Complete downloaded versions, newest first."""
    if not pylib.is_dir():
        return []
    found = [(parse_version(p.name), p) for p in pylib.iterdir()
             if p.is_dir() and not p.name.startswith(".") and (p / DONE).exists()]
    return sorted(found, reverse=True)


def candidates(pylib: Path, bundled: str) -> list[Path]:
    """Downloaded versions newer than the bundled one, newest first. Older ones (left over
    from before an app update that shipped a newer yt-dlp) are never used."""
    b = parse_version(bundled)
    return [p for v, p in installed(pylib) if v > b]


# --- startup -------------------------------------------------------------------------
def installed_version() -> str:
    """Read yt_dlp/version.py as text: importing yt_dlp here would pin the installed copy
    before the downloaded one can go on sys.path."""
    spec = importlib.util.find_spec("yt_dlp")
    if spec is None or not spec.origin:
        return "0"
    m = re.search(r"__version__\s*=\s*['\"]([^'\"]+)", (Path(spec.origin).parent / "version.py").read_text())
    return m.group(1) if m else "0"


def activate(pylib: Path, bundled: str) -> Path | None:
    """Put the newest working downloaded yt-dlp first on sys.path. A version that won't even
    import is deleted and the next one (or the installed copy) is used."""
    for p in candidates(pylib, bundled):
        sys.path.insert(0, str(p))
        try:
            import yt_dlp  # noqa: F401
            if Path(yt_dlp.__file__).resolve().is_relative_to(p.resolve()):
                return p
        except Exception:
            log.warning("yt-dlp %s doesn't import; dropping it", p.name, exc_info=True)
        sys.path.remove(str(p))
        for name in [m for m in sys.modules if m == "yt_dlp" or m.startswith(("yt_dlp.", "yt_dlp_ejs"))]:
            del sys.modules[name]
        shutil.rmtree(p, ignore_errors=True)
    return None


# --- background update -----------------------------------------------------------------
def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "autodeck (yt-dlp updater)"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def _release(name: str, version: str = "") -> dict:
    return json.loads(_get(PYPI.format(name=name, version=f"{version}/" if version else "")))


def _fetch_wheel(release: dict, into: Path) -> None:
    w = pick_wheel(release)
    if w is None:
        raise RuntimeError(f"no pure-Python wheel for {release['info']['name']} {release['info']['version']}")
    url, sha = w
    data = _get(url)
    if hashlib.sha256(data).hexdigest() != sha:
        raise RuntimeError(f"sha256 mismatch for {url}")
    zipfile.ZipFile(io.BytesIO(data)).extractall(into)  # extractall drops absolute and ../ paths


def update(pylib: Path, bundled: str, now: float | None = None) -> str | None:
    """Download yt-dlp (+ its pinned yt-dlp-ejs) into pylib/<version>/ when PyPI has a version
    newer than both the bundled and the newest downloaded one. Returns the new version or None.
    Used on the NEXT launch: swapping modules under a running download isn't safe."""
    now = time.time() if now is None else now
    stamp = pylib / "last-check"
    if stamp.exists() and now - stamp.stat().st_mtime < CHECK_EVERY:
        return None
    pylib.mkdir(parents=True, exist_ok=True)
    stamp.touch()  # before any network: a failing check is also retried only tomorrow
    os.utime(stamp, (now, now))
    rel = _release("yt-dlp")
    latest = rel["info"]["version"]
    have = max([parse_version(bundled)] + [v for v, _ in installed(pylib)])
    if parse_version(latest) <= have:
        return None
    tmp = pylib / f".{latest}.tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    try:
        _fetch_wheel(rel, tmp)
        pin = ejs_pin(rel["info"].get("requires_dist"))
        if pin:
            _fetch_wheel(_release("yt-dlp-ejs", pin), tmp)
        (tmp / DONE).write_text(latest)
        dest = pylib / latest
        shutil.rmtree(dest, ignore_errors=True)
        os.replace(tmp, dest)  # atomic: a version dir is either complete or absent
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    for _, old in installed(pylib)[KEEP:]:
        shutil.rmtree(old, ignore_errors=True)
    return latest


def start(pylib: Path | None = None) -> None:
    """At launch, before anything imports yt_dlp: use the newest good download, and check for
    the next one in the background (never blocks the window)."""
    pylib = pylib or pylib_dir()
    bundled = installed_version()
    active = activate(pylib, bundled)
    log.warning("yt-dlp: installed %s, using %s", bundled, active.name if active else "installed")
    threading.Thread(target=_update_quietly, args=(pylib, bundled), daemon=True, name="ytdlp-update").start()


def _update_quietly(pylib: Path, bundled: str) -> None:
    try:
        v = update(pylib, bundled)
        if v:
            log.warning("downloaded yt-dlp %s; it's used from the next launch", v)
    except Exception as e:  # offline, PyPI down, bad wheel: keep what we have
        log.warning("yt-dlp update check failed: %s", e)
