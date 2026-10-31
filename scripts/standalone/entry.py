"""Entry point of the standalone (PyInstaller) autodeck.app. See scripts/build_standalone.sh."""
import multiprocessing

# Anything that spawns a helper process (joblib/loky, multiprocessing's resource tracker) re-runs
# this executable; freeze_support() turns those into the helper instead of a second app window.
multiprocessing.freeze_support()

from autodeck import standalone  # noqa: E402

standalone.boot()  # PATH, caches, logs and the downloaded yt-dlp: must precede `import yt_dlp`

from autodeck.desktop import main  # noqa: E402

main()
