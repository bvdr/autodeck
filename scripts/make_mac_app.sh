#!/usr/bin/env bash
# Build autodeck.app: a thin macOS launcher for THIS checkout.
#
#   scripts/make_mac_app.sh                       # -> dist/autodeck.app, signed
#   scripts/make_mac_app.sh --notarize            # + Apple notarization, ticket stapled
#   scripts/make_mac_app.sh --notarize --install  # + copy to ~/Applications
#
# Signing uses the Developer ID Application certificate (override with AUTODECK_SIGN_ID; set it
# to "-" for an ad-hoc signature). Notarizing needs notarytool credentials stored once under the
# keychain profile AUTODECK_NOTARY_PROFILE (default "autodeck"):
#   xcrun notarytool store-credentials autodeck --apple-id <apple id> --team-id HRP3W3WRHZ
#
# The app runs `uv run python -m autodeck.desktop` in this repo (its own .venv), so it stays tiny
# and always runs the current code. It needs the repo, uv and the Homebrew tools (ffmpeg,
# rubberband, deno) on this Mac. For a self-contained app + DMG that runs on a Mac without any
# of that, use scripts/build_standalone.sh.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP="$REPO/dist/autodeck.app"
ICON_SRC="$REPO/design/icon/icon-1024.png"

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>autodeck</string>
  <key>CFBundleDisplayName</key><string>autodeck</string>
  <key>CFBundleIdentifier</key><string>com.bvdr.autodeck</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>autodeck</string>
  <key>CFBundleIconFile</key><string>autodeck</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
PLIST

# Launcher. Apps opened from Finder/Dock get a minimal PATH (no Homebrew, no ~/.local/bin), so
# set it explicitly: uv, ffmpeg, rubberband and deno all live there. Output goes to a log file
# (there's no terminal) so a failed start can be diagnosed.
cat > "$APP/Contents/MacOS/autodeck" <<LAUNCH
#!/bin/bash
export PATH="/opt/homebrew/bin:/usr/local/bin:\$HOME/.local/bin:\$HOME/.cargo/bin:/usr/bin:/bin:/usr/sbin:/sbin"
LOG="\$HOME/Library/Logs/autodeck.log"
cd "$REPO" || { osascript -e 'display alert "autodeck" message "Repo not found: $REPO"'; exit 1; }
echo "--- \$(date) starting autodeck" >> "\$LOG"
exec uv run --project "$REPO" python -m autodeck.desktop >> "\$LOG" 2>&1
LAUNCH
chmod +x "$APP/Contents/MacOS/autodeck"

# Icon: the design's 1024 px icon -> .icns with every size macOS asks for.
if [[ -f "$ICON_SRC" ]]; then
  SET="$(mktemp -d)/autodeck.iconset"
  mkdir -p "$SET"
  for s in 16 32 128 256 512; do
    sips -z $s $s "$ICON_SRC" --out "$SET/icon_${s}x${s}.png" >/dev/null
    sips -z $((s * 2)) $((s * 2)) "$ICON_SRC" --out "$SET/icon_${s}x${s}@2x.png" >/dev/null
  done
  iconutil -c icns "$SET" -o "$APP/Contents/Resources/autodeck.icns"
fi

# Developer ID signature with the hardened runtime and a secure timestamp: what Gatekeeper and
# the notary service require. (Two identical certificates exist in the keychain, so the
# identity is pinned by its SHA-1 hash rather than its name.)
SIGN_ID="${AUTODECK_SIGN_ID:-7F80F3022AB45A042C5FEC7BCB9323EE2753E631}"
if [[ "$SIGN_ID" == "-" ]]; then
  codesign --force --sign - "$APP"
else
  codesign --force --options runtime --timestamp --sign "$SIGN_ID" "$APP"
fi
codesign --verify --strict --verbose=1 "$APP"
echo "built and signed $APP"

if [[ " $* " == *" --notarize "* ]]; then
  PROFILE="${AUTODECK_NOTARY_PROFILE:-autodeck}"
  ZIP="$REPO/dist/autodeck.zip"
  ditto -c -k --keepParent "$APP" "$ZIP"
  xcrun notarytool submit "$ZIP" --keychain-profile "$PROFILE" --wait
  xcrun stapler staple "$APP"   # ticket inside the app: Gatekeeper passes even offline
  rm -f "$ZIP"
  spctl --assess --type execute --verbose "$APP"
fi

if [[ " $* " == *" --install "* ]]; then
  mkdir -p "$HOME/Applications"
  rm -rf "$HOME/Applications/autodeck.app"
  cp -R "$APP" "$HOME/Applications/"
  echo "installed $HOME/Applications/autodeck.app"
fi
