#!/usr/bin/env bash
# Build BrickLayers Rust artifacts (WASM module + native host binary).
#
# Outputs (relative to repo root):
#   src/engine_plugin/target/wasm32-wasip1/release/bricklayers_wasm.wasm
#   src/engine_plugin/target/release/bricklayers_engine
#
# Prefer running via `nix run .#build`, which provides cargo, protoc, and
# (on Darwin) the matching SDK env vars. Running directly requires those
# tools to be on PATH — easiest inside `nix develop` / direnv.

set -euo pipefail

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd -- "$HERE/.." && pwd)"

cd "$REPO/src/engine_plugin"

echo ":: cargo build -p bricklayers_wasm --target wasm32-wasip1 --release"
cargo build -p bricklayers_wasm --target wasm32-wasip1 --release

echo ":: cargo build -p bricklayers_engine --release"
cargo build -p bricklayers_engine --release

WASM="$REPO/src/engine_plugin/target/wasm32-wasip1/release/bricklayers_wasm.wasm"
BIN="$REPO/src/engine_plugin/target/release/bricklayers_engine"

echo ""
echo ":: Artifacts"
ls -lh "$WASM" "$BIN"
echo ""
echo ":: Build complete. Next: scripts/deploy.sh (or 'nix run .#deploy')."
