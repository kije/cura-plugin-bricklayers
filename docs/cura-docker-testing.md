# Cura-in-Docker Testing Strategy for AI Agent Plugin Verification

Research findings on running Cura in Docker for automated plugin testing by an AI coding agent.

## Recommended Two-Tier Architecture

### Tier 1 — gRPC Mock Client Tests (No Docker, No GUI)

The most practical approach for CI. Write a pytest test suite that:
1. Starts the BrickLayers plugin as a standalone gRPC server
2. Sends fabricated CuraEngine messages (handshake, settings broadcast, GCodePath modify calls)
3. Asserts on responses (z_offset values, flow_ratio modifications)

This tests plugin logic independently of Cura and CuraEngine. No Docker, no GUI, runs in CI.

### Tier 2 — Full GUI via Docker + VNC (Visual Verification)

Use `lscr.io/linuxserver/cura:latest` with mcp-vnc for AI agent interaction:

```bash
docker run -d \
  --name=cura \
  -e PUID=1000 -e PGID=1000 \
  -p 3000:3000 \
  -v /path/to/config:/config \
  lscr.io/linuxserver/cura:latest
```

Then connect Claude via `mcp-vnc` (`@hrrrsn/mcp-vnc`) pointing at the container's VNC port for screenshot + click interaction.

**Known issue:** Cura has an OOM bug in Docker. Must disable crash reports, update checks, and notifications in Preferences on first launch, or pre-seed the config directory.

## Docker Images

| Image | Display | Status | Notes |
|-------|---------|--------|-------|
| `lscr.io/linuxserver/cura` | KasmVNC/Selkies (port 3000) | Active, maintained | Best option. amd64 only |
| `helfrichmichael/cura-novnc` | TigerVNC + noVNC (port 8080) | Less active | Simpler setup |
| `mikeah/cura-novnc` | noVNC | Minimal | Unraid-focused |

## CuraEngine CLI (Headless Slicing)

CuraEngine can slice from CLI without GUI:
```
CuraEngine slice -j fdmprinter.def.json -s layer_height=0.2 -l model.stl -o output.gcode
```

**Limitation:** Plugin invocation from CLI is an open problem (GitHub issue [CuraEngine#2182](https://github.com/Ultimaker/CuraEngine/issues/2182)). The Cura GUI currently acts as the plugin port broker.

## AI Agent Interaction Methods

| Method | Feasibility | Notes |
|--------|-------------|-------|
| **mcp-vnc** (screenshot+click) | Moderate | `npm install -g @hrrrsn/mcp-vnc`. Proven for VNC interaction |
| **Playwright on noVNC** | Moderate | Click on the HTML canvas element in browser-based VNC |
| **xdotool + Xvfb** | Moderate | Coordinate-based, headless, deterministic sequences |
| **Cura Python plugin API** | Moderate | Code runs inside Cura process. Write a test companion plugin |
| **Spix** (Qt automation) | Hard | Requires recompiling/injecting into AppImage |
| **Squish** (commercial) | Hard (cost) | ~$5K+/year license |

## Software Rendering in Docker

Qt6/Cura requires OpenGL. In containers without GPU:
```bash
export LIBGL_ALWAYS_SOFTWARE=1
export GALLIUM_DRIVER=llvmpipe
```
Install `libgl1-mesa-dri` in the container. The linuxserver image handles this automatically.

## References

- [linuxserver/docker-cura](https://github.com/linuxserver/docker-cura)
- [mcp-vnc](https://github.com/hrrrsn/mcp-vnc)
- [CuraEngine gRPC definitions](https://github.com/Ultimaker/CuraEngine_grpc_definitions)
- [CuraEngine CLI + plugins issue #2182](https://github.com/Ultimaker/CuraEngine/issues/2182)
- [CuraEngine plugins discussion #15629](https://github.com/Ultimaker/Cura/discussions/15629)
