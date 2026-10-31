#!/usr/bin/env bash
# Build the STANDALONE autodeck.app (a small app that installs its heavy parts on first launch)
# and its DMG:
#
#   scripts/build_standalone.sh                 # app + DMG, signed, notarized, stapled
#   scripts/build_standalone.sh --no-notarize   # app + DMG, signed only (fast iteration)
#
# Output: dist/autodeck.app and dist/autodeck-<version>.dmg. The thin dev launcher
# (scripts/make_mac_app.sh, runs the repo via uv) is separate and still works.
#
# What goes in the app:
#   Contents/MacOS/autodeck      launcher script -> autodeck.standalone.bootstrap()
#   Contents/Resources/python/   python-build-standalone CPython (the Python uv manages), trimmed
#   Contents/Resources/bin/uv    official uv (PyPI wheel, sha256 checked against PyPI)
#   Contents/Resources/bin/rubberband  official Rubber Band CLI (breakfastquay.com, pinned sha256)
#   Contents/Resources/app/      autodeck/, web/, design/icon, pyproject.toml, uv.lock
# First launch installs the rest (packages from uv.lock, models), see autodeck/standalone.py.
#
# Needs on the BUILD Mac only: uv (with its managed CPython 3.12: `uv python install 3.12`),
# Xcode command line tools, the Developer ID Application certificate (AUTODECK_SIGN_ID, SHA-1) and
# notarytool credentials under the keychain profile AUTODECK_NOTARY_PROFILE (default "autodeck"):
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
# Pinned third-party binaries. Bump deliberately; the hashes make a tampered download fail the build.
UV_VERSION="0.13.6"
RUBBERBAND_URL="https://breakfastquay.com/files/releases/rubberband-4.0.0-gpl-executable-macos.tar.bz2"
RUBBERBAND_SHA256="0dc91509a31a94c1144436cc20e6f1d165bdab39fe125f9be7d46139921b733f"
NOTARIZE=1
[[ " $* " == *" --no-notarize "* ]] && NOTARIZE=0

step() { printf '\n==> %s\n' "$*"; }

VERSION="$(python3 -c 'import tomllib;print(tomllib.load(open("pyproject.toml","rb"))["project"]["version"])')"
uv lock --check  # the bundled uv.lock must match pyproject.toml, or first-launch `uv sync --frozen` lies
rm -rf "$APP"
RES="$APP/Contents/Resources"
mkdir -p "$APP/Contents/MacOS" "$RES/bin" "$RES/app" "$BUILD"

step "Python (python-build-standalone, as managed by uv)"
# --no-project: not this repo's .venv. base_prefix: the real install dir behind any symlink.
PYHOME="$("$(uv python find 3.12 --managed-python --no-project)" -c 'import os,sys;print(os.path.realpath(sys.base_prefix))')"
echo "from $PYHOME"
ditto "$PYHOME" "$RES/python"
# Trim what autodeck never uses: GUI toolkits, tests, pip (uv installs), headers.
( cd "$RES/python"
  rm -rf include share lib/pkgconfig lib/tcl* lib/tk* lib/itcl* lib/thread* \
    lib/python3.12/{test,idlelib,tkinter,turtledemo,ensurepip,lib2to3} \
    lib/python3.12/lib-dynload/_tkinter* \
    bin/{idle*,pip*,2to3*,pydoc*,*-config}
  find . -name __pycache__ -type d -prune -exec rm -rf {} +
  # Empty site-packages: everything is installed into the venv. (A managed Python on a dev Mac
  # can have had things pip-installed into it; none of that may ship.)
  find lib/python3.12/site-packages -mindepth 1 -maxdepth 1 ! -name README.txt -exec rm -rf {} + )
"$RES/python/bin/python3.12" -I -B -c 'import ssl, sqlite3, ctypes, zlib; print("python ok")'

step "uv $UV_VERSION (official wheel from PyPI, sha256 checked)"
python3 - "$UV_VERSION" "$RES/bin" <<'PY'
import hashlib, io, json, os, sys, urllib.request, zipfile
ver, out = sys.argv[1:]
rel = json.load(urllib.request.urlopen(f"https://pypi.org/pypi/uv/{ver}/json", timeout=60))
w = next(u for u in rel["urls"] if u["filename"] == f"uv-{ver}-py3-none-macosx_11_0_arm64.whl")
data = urllib.request.urlopen(w["url"], timeout=120).read()
assert hashlib.sha256(data).hexdigest() == w["digests"]["sha256"], "uv wheel sha256 mismatch"
with open(os.path.join(out, "uv"), "wb") as f:
    f.write(zipfile.ZipFile(io.BytesIO(data)).read(f"uv-{ver}.data/scripts/uv"))
os.chmod(os.path.join(out, "uv"), 0o755)
PY
"$RES/bin/uv" --version

step "rubberband CLI (official build, pinned sha256)"
TB="$BUILD/$(basename "$RUBBERBAND_URL")"
[[ -f "$TB" ]] || curl -fsSL -o "$TB" "$RUBBERBAND_URL"
echo "$RUBBERBAND_SHA256  $TB" | shasum -a 256 -c -
tar -xjf "$TB" -C "$BUILD"
lipo "$BUILD/$(basename "$TB" .tar.bz2)/rubberband" -thin arm64 -output "$RES/bin/rubberband"
"$RES/bin/rubberband" -V

step "autodeck source + lock"
rsync -a --exclude __pycache__ autodeck "$RES/app/"
rsync -a --exclude '*.test.mjs' web "$RES/app/"
mkdir -p "$RES/app/design" && cp -R design/icon "$RES/app/design/"
cp pyproject.toml uv.lock .python-version "$RES/app/"

step "bundle files (Info.plist, icon, launcher)"
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>autodeck</string>
  <key>CFBundleDisplayName</key><string>autodeck</string>
  <key>CFBundleIdentifier</key><string>com.bvdr.autodeck</string>
  <key>CFBundleVersion</key><string>$VERSION</string>
  <key>CFBundleShortVersionString</key><string>$VERSION</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>autodeck</string>
  <key>CFBundleIconFile</key><string>autodeck</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>LSApplicationCategoryType</key><string>public.app-category.music</string>
</dict>
</plist>
PLIST
SET="$BUILD/autodeck.iconset"
rm -rf "$SET" && mkdir -p "$SET"
for s in 16 32 128 256 512; do
  sips -z $s $s design/icon/icon-1024.png --out "$SET/icon_${s}x${s}.png" >/dev/null
  sips -z $((s * 2)) $((s * 2)) design/icon/icon-1024.png --out "$SET/icon_${s}x${s}@2x.png" >/dev/null
done
iconutil -c icns "$SET" -o "$RES/autodeck.icns"
cat > "$APP/Contents/MacOS/autodeck" <<'LAUNCH'
#!/bin/bash
# autodeck standalone launcher: first-launch setup + start, see autodeck/standalone.py.
# -I ignores PYTHON* env vars and user site-packages; bytecode goes to the caches folder,
# never next to the sources inside this signed bundle.
RES="$(cd "$(dirname "$0")/../Resources" && pwd -P)"
export AUTODECK_RESOURCES="$RES"
exec "$RES/python/bin/python3.12" -I -X "pycache_prefix=${AUTODECK_CACHES:-$HOME/Library/Caches/autodeck}/pycache" \
  -c 'import sys; sys.path.insert(0, sys.argv[1]); from autodeck import standalone; standalone.bootstrap()' "$RES/app"
LAUNCH
chmod +x "$APP/Contents/MacOS/autodeck"

step "sign (inside-out, hardened runtime, timestamp)"
# Every Mach-O, deepest path first. The Python interpreter gets the entitlements (see the plist
# for why each one); its dylibs/.so files and the other tools need none.
python3 - "$RES" > "$BUILD/macho.txt" <<'PY'
import os, sys
MAGIC = {b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe"}  # 64-bit Mach-O, universal
found = []
for root, _, files in os.walk(sys.argv[1]):
    for f in files:
        p = os.path.join(root, f)
        if not os.path.islink(p):
            with open(p, "rb") as fh:
                if fh.read(4) in MAGIC:
                    found.append(p)
print("\n".join(sorted(found, key=lambda p: (-p.count("/"), p))))
PY
INTERP="$RES/python/bin/python3.12"
grep -v -x -e "$INTERP" "$BUILD/macho.txt" | tr '\n' '\0' \
  | xargs -0 -n 50 -P 8 codesign --force --options runtime --timestamp --sign "$SIGN_ID" 2>&1 \
  | { grep -v ': replacing existing signature$' || true; }
codesign --force --options runtime --timestamp --sign "$SIGN_ID" --entitlements "$HERE/entitlements.plist" "$INTERP" 2>/dev/null
codesign --force --options runtime --timestamp --sign "$SIGN_ID" "$APP" 2>/dev/null
codesign --verify --deep --strict --verbose=1 "$APP"
echo "signed $(wc -l < "$BUILD/macho.txt" | tr -d ' ') Mach-O files"
# Nothing may point at Homebrew or /usr/local (LC_LOAD_DYLIB or LC_RPATH).
LEAKS="$(tr '\n' '\0' < "$BUILD/macho.txt" | xargs -0 otool -L -l | grep -E '^\s+(path |name )?(/opt/homebrew|/usr/local)' || true)"
[[ -z "$LEAKS" ]] || { echo "Homebrew/usr/local references:" >&2; echo "$LEAKS" >&2; exit 1; }

notarize() {  # $1 = file to submit; prints the log on failure
  local out id
  out="$(xcrun notarytool submit "$1" --keychain-profile "$PROFILE" --wait --output-format json)" || true
  echo "$out"
  id="$(echo "$out" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("id",""))' 2>/dev/null || true)"
  if ! echo "$out" | grep -q '"status" *: *"Accepted"'; then
    [[ -n "$id" ]] && xcrun notarytool log "$id" --keychain-profile "$PROFILE"
    echo "notarization failed for $1" >&2
    exit 1
  fi
}

if (( NOTARIZE )); then
  # The app first, stapled, so the copy inside the DMG carries its own ticket (Gatekeeper then
  # passes offline after it's dragged to Applications).
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
