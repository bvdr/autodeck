#!/usr/bin/env bash
# Build the STANDALONE autodeck.app (runs on a Mac with nothing installed) and its DMG.
#
#   scripts/build_standalone.sh                 # app + DMG, signed, notarized, stapled
#   scripts/build_standalone.sh --no-notarize   # app + DMG, signed only (fast iteration)
#
# Output: dist/autodeck.app and dist/autodeck-<version>.dmg. The thin dev launcher
# (scripts/make_mac_app.sh, runs the repo via uv) is separate and still works.
#
# Needs on the BUILD Mac only: uv, Xcode command line tools, Homebrew ffmpeg + rubberband + deno +
# dylibbundler (their binaries are copied into the app and relinked to dylibs inside it), the
# Developer ID Application certificate (AUTODECK_SIGN_ID, SHA-1) and notarytool credentials stored
# under the keychain profile AUTODECK_NOTARY_PROFILE (default "autodeck"):
#   xcrun notarytool store-credentials autodeck --apple-id <apple id> --team-id HRP3W3WRHZ
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"
BUILD="$REPO/build"
DIST="$REPO/dist"
APP="$DIST/autodeck.app"
HERE="$REPO/scripts/standalone"
SIGN_ID="${AUTODECK_SIGN_ID:-7F80F3022AB45A042C5FEC7BCB9323EE2753E631}"
PROFILE="${AUTODECK_NOTARY_PROFILE:-autodeck}"
BREW="${HOMEBREW_PREFIX:-/opt/homebrew}"
NOTARIZE=1
[[ " $* " == *" --no-notarize "* ]] && NOTARIZE=0

step() { printf '\n==> %s\n' "$*"; }

step "python env (locked deps + PyInstaller)"
uv sync --locked --group build
VERSION="$(uv run python -c 'import tomllib;print(tomllib.load(open("pyproject.toml","rb"))["project"]["version"])')"

step "models -> build/models (bundled so first launch downloads nothing)"
mkdir -p "$BUILD/models"
uv run python - "$BUILD/models" <<'PY'
import shutil, sys, urllib.request
from pathlib import Path
out = Path(sys.argv[1])

def get(url, dest):
    if dest.exists():
        return
    tmp = dest.with_name(dest.name + ".part")
    with urllib.request.urlopen(url, timeout=60) as r, open(tmp, "wb") as f:
        shutil.copyfileobj(r, f)
    tmp.replace(dest)

# beat_this: reuse the torch hub cache if this Mac already has it, else the official URL.
from beat_this.inference import CHECKPOINT_URL
ckpt, cached = out / "beat_this-final0.ckpt", Path.home() / ".cache/torch/hub/checkpoints/beat_this-final0.ckpt"
if not ckpt.exists() and cached.exists():
    shutil.copyfile(cached, ckpt)
get(f"{CHECKPOINT_URL}/final0.ckpt", ckpt)
# Discogs-EffNet: same URLs genre.py downloads from in dev.
from autodeck.genre import MODEL_URL
for ext in (".onnx", ".json"):
    get(MODEL_URL + ext, out / f"discogs-effnet-bsdynamic-1{ext}")
# Whisper tiny (CTranslate2), as plain files (no HF cache symlinks).
from huggingface_hub import snapshot_download
snapshot_download("Systran/faster-whisper-tiny", local_dir=out / "faster-whisper-tiny",
                  allow_patterns=["config.json", "model.bin", "tokenizer.json", "vocabulary.*"])
shutil.rmtree(out / "faster-whisper-tiny" / ".cache", ignore_errors=True)
print("models:", sorted(p.name for p in out.iterdir()))
PY

step "icon"
SET="$BUILD/autodeck.iconset"
rm -rf "$SET" && mkdir -p "$SET"
for s in 16 32 128 256 512; do
  sips -z $s $s design/icon/icon-1024.png --out "$SET/icon_${s}x${s}.png" >/dev/null
  sips -z $((s * 2)) $((s * 2)) design/icon/icon-1024.png --out "$SET/icon_${s}x${s}@2x.png" >/dev/null
done
iconutil -c icns "$SET" -o "$BUILD/autodeck.icns"

step "PyInstaller -> dist/autodeck.app"
rm -rf "$APP"
uv run pyinstaller --noconfirm --clean --log-level WARN \
  --distpath "$DIST" --workpath "$BUILD/pyinstaller" "$HERE/autodeck.spec"
rm -rf "$DIST/autodeck"  # the onedir COLLECT copy; the .app holds the same files

step "native tools -> Contents/Resources/bin (relinked to dylibs inside the app)"
BIN="$APP/Contents/Resources/bin"
mkdir -p "$BIN"
for t in ffmpeg ffprobe rubberband deno; do
  src="$(readlink -f "$BREW/bin/$t")" || { echo "missing $BREW/bin/$t: brew install ffmpeg rubberband deno" >&2; exit 1; }
  install -m 755 "$src" "$BIN/$t"
done
# -b bundle deps, -x fix these binaries, -d copy dylibs here, -p how binaries find them.
"$BREW/bin/dylibbundler" -of -cd -b -d "$BIN/lib" -p @executable_path/lib/ \
  -x "$BIN/ffmpeg" -x "$BIN/ffprobe" -x "$BIN/rubberband" -x "$BIN/deno" >/dev/null
# Nothing in the app may point at Homebrew or /usr/local any more.
# otool -l also lists LC_RPATH entries, which would make dyld search there too.
LEAKS="$(find "$APP" -type f \( -perm -u+x -o -name '*.dylib' -o -name '*.so' \) -print0 \
  | xargs -0 otool -L -l 2>/dev/null | grep -E '^\s+(path |name )?(/opt/homebrew|/usr/local)' || true)"
[[ -z "$LEAKS" ]] || { echo "Homebrew/usr/local references left:" >&2; echo "$LEAKS" >&2; exit 1; }
"$BIN/ffmpeg" -hide_banner -version | head -1
"$BIN/rubberband" --version 2>&1 | head -1
"$BIN/deno" --version | head -1

step "sign (inside-out, hardened runtime, timestamp)"
# Every Mach-O file, deepest path first, so each container is signed after what it holds.
# Symlinks are skipped (PyInstaller links Frameworks/<x> -> ../Resources/<x>); their targets are signed.
uv run python - "$APP" > "$BUILD/macho.txt" <<'PY'
import os, sys
MAGIC = {b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe", b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca"}
found = []
for root, _, files in os.walk(sys.argv[1]):
    for f in files:
        p = os.path.join(root, f)
        if os.path.islink(p):
            continue
        with open(p, "rb") as fh:
            if fh.read(4) in MAGIC:
                found.append(p)
for p in sorted(found, key=lambda p: (-p.count("/"), p)):
    print(p)
PY
MAIN="$APP/Contents/MacOS/autodeck"
grep -v -x -e "$MAIN" -e "$BIN/deno" "$BUILD/macho.txt" | tr '\n' '\0' \
  | xargs -0 -n 50 -P 8 codesign --force --options runtime --timestamp --sign "$SIGN_ID" 2>&1 \
  | { grep -v ': replacing existing signature$' || true; }
codesign --force --options runtime --timestamp --sign "$SIGN_ID" --entitlements "$HERE/deno.entitlements.plist" "$BIN/deno" 2>/dev/null
codesign --force --options runtime --timestamp --sign "$SIGN_ID" --entitlements "$HERE/entitlements.plist" "$MAIN"
codesign --force --options runtime --timestamp --sign "$SIGN_ID" --entitlements "$HERE/entitlements.plist" "$APP"
codesign --verify --deep --strict --verbose=1 "$APP"
echo "signed $(wc -l < "$BUILD/macho.txt" | tr -d ' ') Mach-O files"

notarize() {  # $1 = file to submit; fetches the log on failure
  local out id
  out="$(xcrun notarytool submit "$1" --keychain-profile "$PROFILE" --wait --output-format json)" || true
  echo "$out"
  id="$(echo "$out" | uv run python -c 'import json,sys;print(json.load(sys.stdin).get("id",""))' 2>/dev/null || true)"
  if ! echo "$out" | grep -q '"status" *: *"Accepted"'; then
    [[ -n "$id" ]] && xcrun notarytool log "$id" --keychain-profile "$PROFILE" "$BUILD/notary-$id.json" && cat "$BUILD/notary-$id.json"
    echo "notarization failed for $1" >&2
    exit 1
  fi
}

if (( NOTARIZE )); then
  # The app is notarized and stapled first so the copy inside the DMG carries its own ticket
  # (it then passes Gatekeeper offline after being dragged to Applications).
  step "notarize the app"
  ZIP="$BUILD/autodeck-app.zip"
  rm -f "$ZIP" && ditto -c -k --keepParent "$APP" "$ZIP"
  notarize "$ZIP"
  rm -f "$ZIP"
  xcrun stapler staple "$APP"
fi

step "DMG"
DMG="$DIST/autodeck-$VERSION.dmg"
STAGE="$BUILD/dmg"
rm -rf "$STAGE" "$DMG" && mkdir -p "$STAGE"
ditto "$APP" "$STAGE/autodeck.app"
ln -s /Applications "$STAGE/Applications"
hdiutil create -volname autodeck -srcfolder "$STAGE" -fs HFS+ -format UDZO -imagekey zlib-level=9 -ov "$DMG" >/dev/null
rm -rf "$STAGE"
codesign --force --timestamp --sign "$SIGN_ID" "$DMG"

if (( NOTARIZE )); then
  step "notarize the DMG"
  notarize "$DMG"
  xcrun stapler staple "$DMG"
  step "Gatekeeper"
  spctl -a -t exec -vv "$APP"
  spctl -a -t open --context context:primary-signature -vv "$DMG"
fi

step "done"
du -sh "$APP" "$DMG"
