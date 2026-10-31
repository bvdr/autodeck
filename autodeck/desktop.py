"""autodeck as a native macOS window: `uv run python -m autodeck.desktop`.

Runs the engine (the same FastAPI app as `python -m autodeck`) on a background thread and shows
the UI in a WKWebView window via pywebview, so there's no browser tab to close by accident or to
be throttled in the background. Closing the window stops the engine.

The packaged `autodeck.app` (scripts/make_mac_app.sh) is a thin launcher that runs this module
from the repo's own environment.
"""
import os
import sys
import threading
import time
import urllib.request
from pathlib import Path

import uvicorn
import webview

from . import config
from .server import create_app

# 8765 like the browser version: the DJ MCP endpoint (/mcp) is registered at that address.
PORT = int(os.environ.get("AUTODECK_PORT", "8765"))
URL = f"http://127.0.0.1:{PORT}/"
ICON = Path(__file__).resolve().parent.parent / "design" / "icon" / "icon-1024.png"
# The web page keeps the resume point (song + position) in localStorage, so the window needs a
# persistent profile; pywebview's default private mode would forget it on every launch.
STORAGE = Path.home() / "Library" / "Application Support" / "autodeck" / "webview"


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


def main() -> None:
    server = None
    if not _already_running():
        moved = config.migrate_legacy_library()  # one-time move to ~/Library/Application Support
        if moved:
            print(moved, flush=True)
        # A second launch (or the browser version already running) just opens another window on
        # the existing engine instead of failing on a busy port.
        server = uvicorn.Server(uvicorn.Config(create_app(), host="127.0.0.1", port=PORT, log_level="warning"))
        thread = threading.Thread(target=server.run, daemon=True, name="autodeck-engine")
        thread.start()
        deadline = time.monotonic() + 30
        while not server.started:
            if not thread.is_alive() or time.monotonic() > deadline:
                raise SystemExit(f"autodeck engine failed to start on port {PORT}")
            time.sleep(0.05)
    _brand_process()
    STORAGE.mkdir(parents=True, exist_ok=True)
    webview.create_window("autodeck", URL, width=1440, height=900, min_size=(1000, 680),
                          background_color="#0D0C10", text_select=True)
    webview.start(private_mode=False, storage_path=str(STORAGE))
    if server is not None:  # window closed: stop the engine (worker threads are daemons)
        server.should_exit = True
        thread.join(timeout=10)


if __name__ == "__main__":
    main()
