#!/usr/bin/env bash
# Deploy the BrickLayers plugin to the local Cura user plugins directory.
#
# Usage:
#   scripts/deploy.sh            # deploy to Cura 5.12 (default)
#   scripts/deploy.sh 5.11       # deploy to Cura 5.11
#   CURA_VERSION=5.11 scripts/deploy.sh
#
# Target layout (matches .github/workflows/bricklayers-engine.yml packaging):
#   <plugins>/BrickLayers/
#     plugin.json, __init__.py, BrickLayers.py, BrickLayersEnginePlugin.py,
#     engine_prototype.py, brick_layers_settings.def.json,
#     BrickLayersSaveAreaButton.qml, LICENSE, proto/
#     bin/bricklayers.wasm
#     bin/<platform-arch>/bricklayers_engine
#
# Requires build artifacts — run scripts/build.sh (or `nix run .#build`) first.

set -euo pipefail

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd -- "$HERE/.." && pwd)"

CURA_VERSION="${1:-${CURA_VERSION:-5.12}}"

# --- Platform detection ------------------------------------------------------
uname_s="$(uname -s)"
uname_m="$(uname -m)"

case "$uname_m" in
  arm64|aarch64) ARCH="aarch64" ;;
  x86_64|amd64)  ARCH="x86_64" ;;
  *) echo "error: unsupported machine arch: $uname_m" >&2; exit 1 ;;
esac

BIN_NAME="bricklayers_engine"
case "$uname_s" in
  Darwin)
    PLATFORM_SUBDIR="macos-$ARCH"
    PLUGINS_ROOT="$HOME/Library/Application Support/cura/$CURA_VERSION/plugins"
    ;;
  Linux)
    PLATFORM_SUBDIR="linux-$ARCH"
    PLUGINS_ROOT="${XDG_DATA_HOME:-$HOME/.local/share}/cura/$CURA_VERSION/plugins"
    ;;
  *) echo "error: unsupported OS: $uname_s" >&2; exit 1 ;;
esac

DEST="$PLUGINS_ROOT/BrickLayers"
WASM_SRC="$REPO/src/engine_plugin/target/wasm32-wasip1/release/bricklayers_wasm.wasm"
BIN_SRC="$REPO/src/engine_plugin/target/release/$BIN_NAME"

# --- Sanity checks -----------------------------------------------------------
for f in "$WASM_SRC" "$BIN_SRC"; do
  if [ ! -f "$f" ]; then
    echo "error: missing build artifact: $f" >&2
    echo "Run scripts/build.sh (or 'nix run .#build') first." >&2
    exit 1
  fi
done

echo ":: Cura version:  $CURA_VERSION"
echo ":: Platform:      $PLATFORM_SUBDIR"
echo ":: Destination:   $DEST"

# --- Wipe + stage tree -------------------------------------------------------
rm -rf "$DEST"
mkdir -p "$DEST/bin/$PLATFORM_SUBDIR"

# Plugin sources
cp    "$REPO/src/plugin.json"                     "$DEST/"
cp    "$REPO/src/__init__.py"                     "$DEST/"
cp    "$REPO/src/BrickLayers.py"                  "$DEST/"
cp    "$REPO/src/BrickLayersEnginePlugin.py"      "$DEST/"
cp    "$REPO/src/engine_prototype.py"             "$DEST/"
cp    "$REPO/src/brick_layers_settings.def.json"  "$DEST/"
cp    "$REPO/src/BrickLayersSaveAreaButton.qml"   "$DEST/"
cp -R "$REPO/src/proto"                           "$DEST/proto"
if [ -f "$REPO/LICENSE" ]; then
  cp "$REPO/LICENSE" "$DEST/"
fi

# Built artifacts
cp "$WASM_SRC" "$DEST/bin/bricklayers.wasm"
cp "$BIN_SRC"  "$DEST/bin/$PLATFORM_SUBDIR/$BIN_NAME"
chmod +x "$DEST/bin/$PLATFORM_SUBDIR/$BIN_NAME"

# Strip any __pycache__ that tagged along with proto/
find "$DEST" -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true

# macOS: clear Gatekeeper quarantine so the binary can execute
if [ "$uname_s" = "Darwin" ] && command -v xattr >/dev/null 2>&1; then
  xattr -d com.apple.quarantine "$DEST/bin/$PLATFORM_SUBDIR/$BIN_NAME" 2>/dev/null || true
fi

echo ""
echo ":: Deploy complete. Quit and relaunch Cura to pick up the new plugin."
