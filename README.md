# BrickLayers - CuraEngine Plugin for Interlocking Wall Layers

**Dramatically improve 3D print strength and waterproofing by staggering wall layers like real brickwork.**

BrickLayers shifts alternating inner wall perimeters up by half a layer height, creating a mechanically interlocking structure that eliminates the continuous horizontal weak plane inherent to standard FDM printing. Tested results show **+14% tensile strength (PLA)** and **complete waterproofing at 4 Bar / 40m depth (ASA)**.

```
Standard FDM walls:          BrickLayers walls:

 ┌──────────────────┐        ┌──────────────────┐
 │     Layer N+2    │        │     Layer N+2    │
 ├──────────────────┤        ├────────┐         │
 │     Layer N+1    │        │        ├─────────┤
 ├──────────────────┤        ├────────┘         │
 │     Layer N      │        │     Layer N      │
 └──────────────────┘        └──────────────────┘

 Continuous weak plane        Interlocking bond
 at every layer line          across layers
```

## How It Works

Unlike post-processing scripts that modify G-code after slicing, BrickLayers operates as a **native CuraEngine plugin** using the `GCODE_PATHS_MODIFY` slot (103). This means:

- The engine sends structured path data *before* G-code generation
- BrickLayers shifts wall paths by modifying `z_offset` and `flow_ratio`
- CuraEngine handles all extrusion, retraction, and travel calculations from the modified paths
- No G-code parsing, no coordinate recalculation, no tool-change corruption

The core algorithm is implemented in Rust (compiled to WASM for portability), with a Python prototype fallback. A native host binary handles gRPC communication with CuraEngine and delegates computation to the WASM module.

## Requirements

- **Cura 5.6+** (requires `GCODE_PATHS_MODIFY` slot support)
- Any FDM printer supported by Cura

## Installation

### From Release (Recommended)

1. Download `BrickLayers-plugin.zip` from the [latest release](https://github.com/kije/cura-plugin-bricklayers/releases)
2. Extract the `BrickLayers/` folder
3. Copy it to your Cura plugins directory:
   - **Windows:** `%APPDATA%\cura\<version>\plugins\`
   - **macOS:** `~/Library/Application Support/cura/<version>/plugins/`
   - **Linux:** `~/.local/share/cura/<version>/plugins/`
4. Restart Cura

### From Source

```bash
git clone https://github.com/kije/cura-plugin-bricklayers.git
cd cura-plugin-bricklayers

# Build the Rust engine (requires Rust toolchain + protoc)
cd src/engine_plugin
cargo build -p bricklayers_wasm --target wasm32-wasip1 --release
cargo build -p bricklayers_engine --release

# Copy plugin files to Cura
cp -r src/ ~/.local/share/cura/5.12/plugins/BrickLayers/
cp target/release/bricklayers_engine ~/.local/share/cura/5.12/plugins/BrickLayers/bin/
cp target/wasm32-wasip1/release/bricklayers_wasm.wasm ~/.local/share/cura/5.12/plugins/BrickLayers/bin/bricklayers.wasm
```

## Usage

After installation, BrickLayers settings appear under **Print Settings > Experimental**:

| Setting | Default | Description |
|---------|---------|-------------|
| **Brick Layers** | Off | Master enable/disable toggle |
| **Extrusion Multiplier** | 1.05 | Compensates for voids from the Z-offset. 1.05-1.10 recommended |
| **Start Layer** | 3 | Skip early layers for bed adhesion |
| **End Layer** | -1 | -1 = all layers |
| **Inner Walls** | On | Shift alternating inner wall loops (primary use case) |
| **Outer Walls** | Off | Shift outer walls too (affects surface finish) |

**Quick start:** Enable "Brick Layers" and slice. The defaults work well for most prints.

## Architecture

```
CuraEngine                    BrickLayers Plugin
    │                              │
    ├─ Handshake ────────────────> │  Register for GCODE_PATHS_MODIFY
    │                              │
    ├─ Settings Broadcast ───────> │  Receive print settings
    │                              │
    │  For each layer:             │
    ├─ GCodePath[] ──────────────> │  Receive wall paths
    │                              ├── Identify INNERWALL / OUTERWALL
    │                              ├── Shift odd-numbered walls: z_offset += layer_height/2
    │                              ├── Adjust flow_ratio *= extrusion_multiplier
    │  <───────────── GCodePath[] ─┤  Return modified paths
    │                              │
    └─ Generate G-code             │
       from modified paths         │
```

The plugin has a split architecture:

- **Host** (`src/engine_plugin/host/`): Native Rust binary handling gRPC communication with CuraEngine via tonic. Delegates path modification to the WASM module.
- **WASM** (`src/engine_plugin/wasm/`): Pure Rust algorithm compiled to `wasm32-wasip1`. Portable across all platforms from a single build.
- **Python Prototype** (`src/engine_prototype.py`): Fallback gRPC server implementing the same algorithm in Python. Used automatically if no compiled binary is found.

## Prior Art and References

This plugin is an **independent implementation** of the brick layer technique, inspired by the public-domain concept and the following open-source projects. No code was copied from any proprietary implementation (such as ADDMAN's ADDCAAM or Create it REAL's REALvision Pro).

The brick layer concept originates from the observation that real-world masonry never aligns vertical joints between courses. The technique was first described in [US Patent 5,653,925](https://patents.google.com/patent/US5653925A/en) (Stratasys, 1995, expired 2015 — now public domain). Applied to FDM 3D printing:

- **CNC Kitchen (Stefan Hermann)** first demonstrated the technique in February 2024, measuring +14% tensile strength in PLA using a multi-process Simplify3D approach. [Blog](https://www.cnckitchen.com/blog/brick-layers-make-3d-prints-stronger)
- **GeekDetour/BrickLayers** — Post-processing script for PrusaSlicer/OrcaSlicer/BambuStudio. [GitHub](https://github.com/GeekDetour/BrickLayers)
- **TengerTechnologies/Bricklayers** — Post-processing script with additional non-planar infill support. [GitHub](https://github.com/TengerTechnologies/Bricklayers)
- **OrcaSlicer** — Native "Stagger Perimeters" feature in nightly builds ([PR #8181](https://github.com/OrcaSlicer/OrcaSlicer/pull/8181))
- **Creality** — [Explainer article](https://store.creality.com/blogs/basics/brick-layer)

This plugin differs from post-processing scripts by operating at the CuraEngine level (pre-G-code), avoiding the fragility of G-code text manipulation.

## Documentation

See [docs/manual/](docs/manual/) for comprehensive user documentation including:

- [How BrickLayers Works](docs/manual/how-it-works.md) — Technical explanation with diagrams
- [User Guide](docs/manual/user-guide.md) — Settings reference and recommendations
- [Strength and Testing Data](docs/manual/strength-testing.md) — Published test results and benchmarks
- [Hardware Test Protocol](src/tests/HARDWARE_TEST_PROTOCOL.md) — Validation procedure for real printers

## Development

### Building

```bash
# Rust engine (requires protoc)
cd src/engine_plugin
cargo build -p bricklayers_wasm --target wasm32-wasip1
cargo build -p bricklayers_engine

# Python tests
cd src
python -m unittest tests.TestBrickLayers -v
```

### CI

The GitHub Actions workflow (`.github/workflows/bricklayers-engine.yml`) builds:
- WASM module (universal, single build)
- Native host binaries for Linux (x86_64, aarch64), macOS (x86_64, aarch64), and Windows (x86_64)
- Packaged plugin zip with all binaries

## Patent Notice

The brick layer / staggered perimeter technique for FDM 3D printing was first
described in US Patent 5,653,925 (Stratasys, filed 1995, expired 2015), which
is now in the public domain. Subsequent patents covering similar subject matter
have been filed and are the subject of ongoing validity challenges. Users should
be aware of the patent landscape in their jurisdiction. This notice is
informational and does not constitute legal advice.

See [docs/legal/LEGAL_ANALYSIS.md](docs/legal/LEGAL_ANALYSIS.md) for a detailed analysis.

## License

LGPLv3 or later. See [LICENSE](LICENSE).

Note: CuraEngine itself is licensed under AGPLv3. BrickLayers communicates with
CuraEngine via gRPC as a separate process, which is the intended plugin
architecture. The gRPC protocol definitions are MIT-licensed
([CuraEngine_grpc_definitions](https://github.com/Ultimaker/CuraEngine_grpc_definitions)).
The plugin's LGPLv3 license is appropriate for this architecture.
