"""autodeck as a native macOS window: `uv run python -m autodeck.desktop`.

Runs the engine (the same FastAPI app as `python -m autodeck`) on a background thread and shows
the UI in a WKWebView window via pywebview, so there's no browser tab to close by accident or to
be throttled in the background. Closing the window stops the engine.

Two app bundles run this: the thin launcher (scripts/make_mac_app.sh, the repo via uv) and the
standalone app (scripts/build_standalone.sh), which first shows web/setup.html while
autodeck.standalone installs the packages and models. That's why the engine (uvicorn, server,
and through it torch & co.) is imported only after setup: on first launch only pywebview exists.
"""
import os
import sys
import threading
import time
import urllib.request
from pathlib import Path

import webview

from . import config, standalone

# 8765 like the browser version: the DJ MCP endpoint (/mcp) is registered at that address.
PORT = int(os.environ.get("AUTODECK_PORT", "8765"))
URL = f"http://127.0.0.1:{PORT}/"
ROOT = Path(__file__).resolve().parent.parent
ICON = ROOT / "design" / "icon" / "icon-1024.png"


def _already_running() -> bool:
    try:
        with urllib.request.urlopen(URL + "api/state", timeout=2) as r:
            return r.status == 200
    except OSError:
        return False


def _brand_process() -> None:
    """Without this the Dock and menu bar show 'Python' and the Python rocket, because the
    window belongs to the python process, not to the .app bundle that launched it."""
    if sys.platform != "darwin":
        return
    try:
        from AppKit import NSApplication, NSImage  # pyobjc, installed with pywebview
        from Foundation import NSBundle
        NSBundle.mainBundle().infoDictionary()["CFBundleName"] = "autodeck"
        if ICON.exists():
            NSApplication.sharedApplication().setApplicationIconImage_(
                NSImage.alloc().initWithContentsOfFile_(str(ICON)))
    except Exception:  # cosmetic only: never block the app over an icon
        pass


class Engine:
    """The uvicorn thread serving the engine, started once."""
    server = None
    thread = None

    @classmethod
    def start(cls) -> None:
        if cls.server is not None or _already_running():
            # A second launch (or the browser version already running) just opens another
            # window on the existing engine instead of failing on a busy port.
            return
        import uvicorn

        from .server import create_app
        if standalone.ACTIVE:
            from . import ytupdate
            ytupdate.start()  # before create_app imports yt_dlp
        moved = config.migrate_legacy_library()  # one-time move to ~/Library/Application Support
        if moved:
            print(moved, flush=True)
        cls.server = uvicorn.Server(uvicorn.Config(create_app(), host="127.0.0.1", port=PORT, log_level="warning"))
        cls.thread = threading.Thread(target=cls.server.run, daemon=True, name="autodeck-engine")
        cls.thread.start()
        deadline = time.monotonic() + 60
        while not cls.server.started:
            if not cls.thread.is_alive() or time.monotonic() > deadline:
                raise SystemExit(f"autodeck engine failed to start on port {PORT}")
            time.sleep(0.05)

    @classmethod
    def stop(cls) -> None:
        if cls.server is not None:  # window closed: stop the engine (worker threads are daemons)
            cls.server.should_exit = True
            cls.thread.join(timeout=10)


class SetupApi:
    """What web/setup.html calls (window.pywebview.api.*)."""

    def __init__(self, setup: "standalone.Setup"):
        self._setup = setup
        self._window = None

    def state(self) -> dict:
        return self._setup.progress.snapshot()

    def start(self) -> None:
        self._setup.start(self._finish)

    def cancel(self) -> None:
        self._setup.cancel()

    def _finish(self) -> None:
        # Setup done: start the engine (first import of torch & co.) and swap the page in place.
        Engine.start()
        self._window.set_title("autodeck")
        self._window.resize(1440, 900)
        self._window.load_url(URL)


def main() -> None:
    webview_storage = config.app_home() / "webview"
    if standalone.ACTIVE:
        standalone.environment()
    reasons = standalone.reasons() if standalone.ACTIVE else []
    if reasons:
        print(f"setup needed: {', '.join(reasons)}", flush=True)
        api = SetupApi(standalone.Setup())  # started by webview.start below, once the window exists
        window = webview.create_window("Setting up autodeck", url=str(ROOT / "web" / "setup.html"), js_api=api,
                                       width=640, height=560, min_size=(520, 460), background_color="#0D0C10")
        api._window = window
    else:
        Engine.start()
        window = webview.create_window("autodeck", URL, background_color="#0D0C10", text_select=True,
                                       width=1440, height=900, min_size=(1000, 680))
    _brand_process()
    # The web page keeps the resume point (song + position) in localStorage, so the window needs a
    # persistent profile; pywebview's default private mode would forget it on every launch.
    webview_storage.mkdir(parents=True, exist_ok=True)
    # http_server: setup.html is a local file that imports setup.js; WebKit won't load ES modules
    # from file:// URLs, so pywebview serves the web/ folder over its own loopback server.
    webview.start(api.start if reasons else None, private_mode=False, storage_path=str(webview_storage),
                  http_server=bool(reasons))
    Engine.stop()


if __name__ == "__main__":
    main()
