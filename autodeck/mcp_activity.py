"""What assistants do over MCP, for the owner (design m6p10): the MCP button, its toast and the
Activity log in the MCP settings, plus the owner's two switches (on/off, steer / only read).

dj_mcp drives autodeck through autodeck's own HTTP API. Every one of its calls carries
X-Autodeck-* headers (see server.py), and the first one of a tool call `begin`s its entry here
(the button's "working" state). When the tool is done, dj_mcp posts the outcome to
/api/mcp/activity, which `finish`es the entry with one plain sentence. The log is memory only:
it is about tonight, a restart starts it empty. The switches and ever_connected persist in
settings.json (config.save_setting), so a restart keeps what the owner chose.
"""
import threading
import time
from collections import deque

from . import config

LOG_SIZE = 50       # the Activity log is a ring buffer: the newest 50 calls, reads included
CONNECTED_S = 600   # "connected" (the button's green dot) = an assistant call in the last 10 min
MODES = ("steer", "read")  # "Steer the session" | "Only read"


def switches() -> tuple[bool, str]:
    """(enabled, mode) from settings.json, read live: dj_mcp's gate and /api/state both use this,
    so there is one source of truth even when dj_mcp runs as its own process (stdio). Default:
    on + Steer, because the owner already has Claude Code registered against /mcp."""
    st = config.settings()
    mode = st.get("mcp_mode")
    return st.get("mcp_enabled", True) is not False, mode if mode in MODES else "steer"


def set_switches(enabled: bool | None = None, mode: str | None = None) -> None:
    if mode is not None and mode not in MODES:
        raise ValueError(f"Unknown MCP mode: {mode}")
    if enabled is not None:
        config.save_setting("mcp_enabled", bool(enabled))
    if mode is not None:
        config.save_setting("mcp_mode", mode)


class Activity:
    def __init__(self, clock=time.time):
        self.clock = clock  # injectable so tests can move time past the 10-minute window
        self.log: deque[dict] = deque(maxlen=LOG_SIZE)  # oldest first; state() flips it
        self.client: str | None = None
        self.last_call_at: float | None = None
        # The last failed call's reason (the button's red state). Stays until the owner opens the
        # MCP settings (seen()), so a failure isn't missed because nobody looked at that second.
        self.last_error: str | None = None
        # First run: until an assistant has ever called, the settings show how to connect one.
        self.ever_connected = bool(config.settings().get("mcp_ever_connected"))
        # Server requests run on several threads (and the event loop): one small lock.
        self._lock = threading.Lock()

    def _entry(self, call: str, tool: str, client: str | None, doing: str, read: bool) -> dict:
        # ponytail: linear scan of <= 50 entries per call; an index if the log ever grows.
        e = next((x for x in self.log if x["id"] == call), None)
        if e is None:
            e = {"id": call, "at": self.clock(), "tool": tool, "client": client, "read": read,
                 "status": "running", "text": doing, "song": None}
            self.log.append(e)
        return e

    def _seen(self, client: str | None) -> None:
        self.client = client or self.client
        self.last_call_at = self.clock()
        if not self.ever_connected:
            self.ever_connected = True
            config.save_setting("mcp_ever_connected", True)

    def begin(self, call: str, tool: str, client: str | None, doing: str, read: bool) -> None:
        """An API request of a tool call arrived (idempotent: a tool may make several)."""
        with self._lock:
            self._entry(call, tool, client, doing, read)
            self._seen(client)

    def finish(self, call: str, tool: str, client: str | None, doing: str, read: bool,
               ok: bool, text: str | None, song: str | None = None) -> None:
        """The tool is done: its one plain sentence (or the error), and the song it was about
        (the toast's thumbnail). Creates the entry if no API call came first (a refusal)."""
        with self._lock:
            e = self._entry(call, tool, client, doing, read)
            e.update(status="ok" if ok else "error", text=text or doing, song=song)
            self._seen(client)
            if not ok:
                self.last_error = e["text"]

    def seen(self) -> None:
        """The owner opened the MCP settings: the error has been seen."""
        with self._lock:
            self.last_error = None

    def state(self) -> dict:
        enabled, mode = switches()
        with self._lock:
            now = self.clock()
            # Working = a steering call in flight. Reads only flash the dot (the UI sees
            # last_call_at move), they never take over the button.
            run = next((e for e in reversed(self.log) if e["status"] == "running" and not e["read"]), None)
            return {"enabled": enabled, "mode": mode, "client": self.client, "last_call_at": self.last_call_at,
                    "connected": self.last_call_at is not None and now - self.last_call_at < CONNECTED_S,
                    "running": {"tool": run["tool"], "text": run["text"], "client": run["client"]} if run else None,
                    "last_error": self.last_error, "ever_connected": self.ever_connected,
                    # Newest first; copies, so serialising never races a finish().
                    "log": [dict(e) for e in reversed(self.log)]}
