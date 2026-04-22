inherit: /Users/kimjekerawl/.claude/CLAUDE.md

# cura-plugin-bricklayers

## Project
CuraEngine plugin (BrickLayers) — interlocking brick-pattern wall layers for Cura 5.6+.
Rust WASM algorithm + native gRPC host + Cura Python extension. License: LGPLv3.

## Dev Environment
- Activate: `direnv allow` (nix-direnv, flake.nix)
- Provides: Rust stable + `wasm32-wasip1` target, Python 3.11, protoc, grpcio, grpcio-tools, pytest

## Build Commands
Work from `src/engine_plugin/` (Rust workspace):
- WASM: `cargo build -p bricklayers_wasm --target wasm32-wasip1 --release`
- Host binary: `cargo build -p bricklayers_engine --release`
- CI workflow: `.github/workflows/bricklayers-engine.yml`

## Testing
```bash
python -m pytest tests/test_grpc_integration.py tests/test_gcode_lint.py -v
```
Run from project root.

## Key Files
- `src/BrickLayers.py` — Cura Extension (settings injection)
- `src/BrickLayersEnginePlugin.py` — BackendPlugin (binary locator)
- `src/engine_plugin/wasm/` — BrickLayers algorithm (Rust → WASM)
- `src/engine_plugin/host/` — gRPC host binary (`bricklayers_engine`)
- `src/proto/` — Protobuf definitions + generated Python stubs
- `tests/` — All tests (project root, NOT src/tests/)
- `conftest.py` — Root conftest with UM/Cura mocks (avoids proto stub conflicts)
- `docker-compose.yml` — Cura 5.12 + x11vnc test environment

## Architecture
- WASM/host split: algorithm in WASM (portable), gRPC host per-platform
- CuraEngine slot: `GCODE_PATHS_MODIFY` (slot 103)
- `plugin.json`: `supported_sdk_versions` 8.6.0–8.12.0
- Innermost wall of each group is never shifted (groups tolerate interleaved travel/retraction paths)
- Min 2 target walls per group required for shifting; shift counter%2==0 walls
- `inset_direction` read from CuraEngine settings broadcast (not a BrickLayers setting)

## Gotchas
- Darwin SDK: use `pkgs.apple-sdk_15`, NOT `darwin.apple_sdk.frameworks.*`
- SDK env vars: set `DEVELOPER_DIR`/`SDKROOT` in `shellHook` only (not as `mkShell` env attrs — cc wrapper overrides those)
- `~/.cargo/config.toml`: global `target-cpu=native` + `target-feature=+neon` for aarch64
- Cura 5.12 Docker: QML errors in `BrickLayersSaveAreaButton.qml:20,29` — known, cosmetic
- Engine plugin not invoked during Docker slicing (gRPC handshake not initiated) — known issue
- WASM warmup: `--warmup` flag pre-compiles `.cwasm` cache at plugin load; addresses macOS Gatekeeper delay
- Port readiness: `start()` polls TCP port after `super().start()` to prevent "Connection refused" races

## Docker Testing
```bash
# Colima must be running; DOCKER_HOST must be set (see root CLAUDE.md local system notes)
DOCKER_HOST=unix://$HOME/.config/colima/default/docker.sock docker compose up -d
# Cura 5.12 + x11vnc at localhost:5900
```
Plugin bind-mounted; `cura-data` volume for `/config`. VNC MCP at localhost:5900.

## Memory-Keeper
Channel: `bricklayers_kije-project-init` (update to branch-specific channel per session)

## context7 Library IDs
Unresolved — run `mcp__context7__resolve-library-id` on first use and cache here:
- `tonic` (crate, v0.12): TBD
- `prost` (crate, v0.13): TBD
- `wasmtime` (crate, v43): TBD
- `clap` (crate, v4): TBD
- `grpcio-tools` (Python): TBD

## Recommended Agent Types
- `rust-engineer` — WASM algorithm, host binary, wasmtime, tonic
- `python-pro` — BrickLayers.py, BrickLayersEnginePlugin.py, gRPC Python
- `test-engineer` — pytest suite, gRPC mocks
- `qa-code-reviewer` — pre-commit reviews, Rust unsafe audits
- `debugger` — gRPC handshake issues, QML failures
- `performance-engineer` — brick-shift algorithm profiling
