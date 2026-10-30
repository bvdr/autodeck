# autodeck

Local auto-DJ: import YouTube songs, analyze them, and play a beatmatched mix in the browser.

## One-time setup

    brew install uv rubberband ffmpeg
    uv sync

## Run

    uv run python -m autodeck      # serves http://127.0.0.1:8765

Library cache: `~/autodeck-library` (override with `AUTODECK_LIBRARY`). Browser for YouTube cookies: `AUTODECK_BROWSER` (default chrome).

## Tests

    uv run pytest               # add -m "not slow and not network" to skip heavy ones
