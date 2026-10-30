"""`python -m autodeck`: serve on 127.0.0.1:8765 and open the browser."""
import os
import threading
import webbrowser

import uvicorn

from .server import create_app

HOST, PORT = "127.0.0.1", 8765  # loopback only: nothing is ever exposed to the network


def main() -> None:
    url = f"http://{HOST}:{PORT}/"
    # AUTODECK_NO_BROWSER=1 skips the browser (smoke tests, headless runs).
    if not os.environ.get("AUTODECK_NO_BROWSER"):
        # Small delay so the server is listening by the time the tab loads.
        threading.Timer(1.0, webbrowser.open, args=(url,)).start()
    uvicorn.run(create_app(), host=HOST, port=PORT)


if __name__ == "__main__":
    main()
