# PyInstaller spec for the standalone autodeck.app. Run through scripts/build_standalone.sh,
# which first puts the models in build/models and the icon in build/autodeck.icns, and after
# this adds the native tools, signs, notarizes and makes the DMG.
# -*- mode: python -*-
import tomllib
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

REPO = Path(SPECPATH).resolve().parent.parent
BUILD = REPO / "build"
VERSION = tomllib.loads((REPO / "pyproject.toml").read_text())["project"]["version"]

datas = [
    (str(REPO / "web"), "web"),                    # server.py serves <autodeck>/../web
    (str(REPO / "design" / "icon"), "design/icon"),  # desktop.py's Dock icon
    (str(BUILD / "models"), "models"),             # config.bundled_model(): no first-run downloads
]
datas += collect_data_files("faster_whisper")   # its Silero VAD .onnx
datas += collect_data_files("essentia")
datas += collect_data_files("mcp")

hiddenimports = (
    collect_submodules("autodeck")
    + collect_submodules("essentia", filter=lambda m: "weka" not in m and "plotting" not in m)
    + collect_submodules("faster_whisper")
    + collect_submodules("mcp", filter=lambda m: not m.startswith("mcp.cli"))  # cli needs typer (not installed)
    + collect_submodules("yt_dlp")          # extractors are looked up by name at runtime
    + collect_submodules("yt_dlp_ejs")
    + ["beat_this.inference", "webview.platforms.cocoa", "ctranslate2", "onnxruntime", "pyloudnorm", "pyrubberband",
       "soundfile", "soxr", "rotary_embedding_torch", "einops", "sklearn.cluster"]
)

a = Analysis(
    [str(REPO / "scripts" / "standalone" / "entry.py")],
    pathex=[str(REPO)],
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=[
        # Never used at runtime; dropping them saves size and stray hooks.
        "tkinter", "pytest", "_pytest", "IPython", "matplotlib", "jupyter", "notebook",
        "tensorboard", "torch.utils.tensorboard", "pytorch_lightning", "lightning",
    ],
    module_collection_mode={
        # Plain .py files on disk instead of inside the PYZ: only then can a newer yt-dlp in
        # pylib/<version>/ (autodeck/standalone.py) win via sys.path.
        "yt_dlp": "py",
        "yt_dlp_ejs": "py",
        # numba's cache=True (librosa's jitted helpers) keys its cache on the source file;
        # without it on disk numba raises "no locator available".
        "librosa": "pyz+py",
    },
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="autodeck",
    console=False,           # --windowed
    argv_emulation=False,
    target_arch="arm64",
    codesign_identity=None,  # build_standalone.sh signs everything itself, inside-out
)
coll = COLLECT(exe, a.binaries, a.datas, name="autodeck")
app = BUNDLE(
    coll,
    name="autodeck.app",
    icon=str(BUILD / "autodeck.icns"),
    bundle_identifier="com.bvdr.autodeck",
    version=VERSION,
    info_plist={
        "CFBundleName": "autodeck",
        "CFBundleDisplayName": "autodeck",
        "CFBundleShortVersionString": VERSION,
        "CFBundleVersion": VERSION,
        "LSMinimumSystemVersion": "13.0",
        "NSHighResolutionCapable": True,
        "LSApplicationCategoryType": "public.app-category.music",
    },
)
