# Tier 2 — Cura GUI Testing via Docker

Browser-accessible Cura instance for manual and AI-agent-driven plugin verification.

## Prerequisites

- Docker with Compose v2 (`docker compose`)
- Colima running: `colima start` (if using Colima as the container runtime)

## Start

```sh
docker compose up -d cura
```

Cura takes 20–40 seconds to initialise on first launch.

## Access

Open http://localhost:3000 in any browser. The KasmVNC interface renders the
full Cura desktop. No VNC client required.

## Plugin Installation

`src/` is bind-mounted to Cura's plugin directory inside the container:

```
./src  →  /config/.local/share/cura/5.x/plugins/BrickLayers
```

The plugin appears under **Extensions → BrickLayers** after Cura restarts.
To apply source changes without rebuilding the image, restart Cura inside the
container (close and reopen via the KasmVNC desktop).

## Pre-seeded Config

`docker/cura-config/cura.cfg` disables crash reports, update checks, and
telemetry. This prevents the OOM dialog that appears on first launch in Docker.
The file is bind-mounted at container start — no manual preference editing
needed.

## AI Agent Access (mcp-vnc)

Install the MCP VNC tool:

```sh
npm install -g @hrrrsn/mcp-vnc
```

Point it at the container's VNC port (the KasmVNC server listens on 3000).
An AI agent can then take screenshots, click UI elements, and fill forms to
drive Cura through a slicing workflow:

1. Load a test STL file via the file manager in the VNC session.
2. Verify the BrickLayers settings panel appears in the right sidebar.
3. Enable the plugin, adjust start/end layers, and slice.
4. Inspect the preview for the alternating z-offset pattern.

## Stop

```sh
docker compose down
```

## Notes

- The `lscr.io/linuxserver/cura` image is amd64-only. On Apple Silicon, run
  Colima with `colima start --arch x86_64 --vm-type vz`.
- Software rendering (`LIBGL_ALWAYS_SOFTWARE=1 / GALLIUM_DRIVER=llvmpipe`) is
  set in `docker-compose.yml`; no host GPU is needed.
- CuraEngine's plugin port brokering happens through the Cura GUI process.
  Headless slicing via the `CuraEngine slice` CLI does not invoke gRPC plugins
  (upstream issue CuraEngine#2182).
