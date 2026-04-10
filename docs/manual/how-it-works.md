# How BrickLayers Works

## The Problem with Standard FDM Printing

In standard Fused Deposition Modeling (FDM), every layer of plastic is deposited as a flat ribbon at a fixed Z height. Wall perimeters are stacked in perfect vertical alignment, creating a continuous horizontal plane of weakness at every layer boundary.

```
Cross-section of standard FDM wall (side view):

    ╭────────╮ ╭────────╮ ╭────────╮
    │ Wall 3 │ │ Wall 2 │ │ Wall 1 │   Layer N+1
    ╰────────╯ ╰────────╯ ╰────────╯
  ──────────────────────────────────── ← Continuous weak plane
    ╭────────╮ ╭────────╮ ╭────────╮
    │ Wall 3 │ │ Wall 2 │ │ Wall 1 │   Layer N
    ╰────────╯ ╰────────╯ ╰────────╯
  ──────────────────────────────────── ← Continuous weak plane
    ╭────────╮ ╭────────╮ ╭────────╮
    │ Wall 3 │ │ Wall 2 │ │ Wall 1 │   Layer N-1
    ╰────────╯ ╰────────╯ ╰────────╯
```

Each extrusion bead has a roughly oval cross-section. When two beads are stacked, they bond only at the narrow tangent contact area between them. The bonding surface is limited, and — critically — all layer interfaces align on the same horizontal plane across the full wall thickness.

Under tensile load perpendicular to the layers (pulling the part apart along the Z axis), a crack needs only to propagate along this single continuous plane. The part fails by **delamination** — a clean, flat fracture at one layer boundary.

## The Brick Layer Solution

BrickLayers shifts alternating inner wall perimeters up by half a layer height, breaking the continuous weak plane into a zig-zag pattern:

```
Cross-section of BrickLayers wall (side view):

    ╭────────╮                ╭────────╮
    │ Wall 3 │ ╭────────╮    │ Wall 1 │   Layer N+1
    ╰────────╯ │ Wall 2 │    ╰────────╯
               ╰────────╯
    ╭────────╮                ╭────────╮
    │ Wall 3 │ ╭────────╮    │ Wall 1 │   Layer N
    ╰────────╯ │ Wall 2 │    ╰────────╯
               ╰────────╯
    ╭────────╮                ╭────────╮
    │ Wall 3 │ ╭────────╮    │ Wall 1 │   Layer N-1
    ╰────────╯ │ Wall 2 │    ╰────────╯
               ╰────────╯
```

The shifted walls (Wall 2 in this example) sit at the midpoint between adjacent layers. Any crack trying to propagate through the wall must now navigate around these interlocking extrusions, requiring significantly more energy to fracture the part.

## The Brickwork Analogy

The name comes from real-world masonry. No competent bricklayer would build a wall with all vertical joints aligned — that creates a continuous line of weakness and the wall would easily split along that line:

```
WRONG — aligned joints:        RIGHT — staggered joints:

 ┌─────┐┌─────┐┌─────┐        ┌─────┐┌─────┐┌─────┐
 │     ││     ││     │        │     ││     ││     │
 ├─────┤├─────┤├─────┤        ├──┐  ├┤  ┌──┤├──┐  │
 │     ││     ││     │        │  │  ││  │  ││  │  │
 ├─────┤├─────┤├─────┤        ├──┘  ├┤  └──┤├──┘  │
 │     ││     ││     │        │     ││     ││     │
 └─────┘└─────┘└─────┘        └─────┘└─────┘└─────┘

 Splits along aligned          Crack must navigate
 joint line                    around staggered joints
```

BrickLayers applies exactly this principle to 3D printed walls.

## Three Mechanical Benefits

### 1. Increased Bonding Surface Area

When an extrusion bead is offset by half a layer height, it contacts both the flat top of one bead below and the curved side of the adjacent bead. The contact geometry changes from a narrow tangent line to a broader curved interface:

```
Standard stacking:              BrickLayers stacking:

    ╭──────╮                        ╭──────╮
    │      │                        │      │
    ╰──┬┬──╯ ← narrow contact      ╰──────╯
    ╭──┴┴──╮                     ╭──────╮
    │      │                     │      ╰──╮ ← broader contact
    ╰──────╯                     ╰──────╮  │    wrapping around
                                    ╭───╯──╯    the side
                                    │      │
                                    ╰──────╯
```

### 2. Broken Weak-Plane Continuity

In standard FDM, the layer boundary is a single continuous horizontal plane spanning the entire wall thickness. A crack can propagate straight through without changing direction.

With BrickLayers, the layer boundary zigzags through the wall. A crack must change direction at each shifted wall, absorbing more energy. The fracture surface becomes irregular rather than planar, changing the failure mode from brittle delamination to a more ductile material fracture.

### 3. Improved Packing Density

When the extrusion multiplier is increased to 1.05-1.10x (recommended), the geometric voids created by the Z-offset are filled with additional material. This increases the actual wall density beyond what standard FDM achieves, contributing to both strength and waterproofing.

## How It Works Technically (CuraEngine Plugin)

BrickLayers operates as a CuraEngine backend plugin, not a G-code post-processor. This is a critical distinction:

### Plugin Architecture

```
┌─────────────┐     gRPC      ┌─────────────────────────────────┐
│ CuraEngine  │ ◄────────────►│ BrickLayers Engine              │
│             │               │                                 │
│  Slicing    │               │  ┌───────────────────────────┐  │
│  Pipeline   │               │  │ Native Host (Rust/tonic)  │  │
│             │               │  │                           │  │
│  1. Mesh    │ Handshake     │  │  gRPC ←→ Protobuf codec  │  │
│  2. Layers  │ ◄────────────►│  │  Settings management     │  │
│  3. Paths   │               │  │                           │  │
│  4. G-code  │ Settings      │  │  ┌───────────────────┐   │  │
│             │ Broadcast     │  │  │ WASM Module (Rust) │   │  │
│             │ ◄────────────►│  │  │                   │   │  │
│  Step 3:    │               │  │  │ Pure algorithm:   │   │  │
│  For each   │ GCodePath[]   │  │  │ • Check settings  │   │  │
│  layer,     │ ──────────────►  │  │ • Count walls     │   │  │
│  send paths │               │  │  │ • Shift odd walls │   │  │
│  to plugin  │ GCodePath[]   │  │  │ • Adjust flow     │   │  │
│             │ ◄──────────────  │  │                   │   │  │
│  Step 4:    │               │  │  └───────────────────┘   │  │
│  Generate   │               │  └───────────────────────────┘  │
│  G-code     │               │                                 │
│  from       │               │  Fallback: Python prototype     │
│  modified   │               │  (same algorithm, no WASM)      │
│  paths      │               └─────────────────────────────────┘
└─────────────┘
```

### The GCODE_PATHS_MODIFY Slot (103)

CuraEngine 5.6+ exposes a plugin slot called `GCODE_PATHS_MODIFY` (slot ID 103). After CuraEngine computes the toolpaths for each layer but *before* generating G-code, it sends the paths to registered plugins as structured `GCodePath` protobuf messages.

Each `GCodePath` contains:
- **feature**: The path type (`INNERWALL`, `OUTERWALL`, `SKIN`, `FILL`, etc.)
- **points**: The XY coordinates of the path
- **z_offset**: Additional Z offset applied during G-code generation
- **flow_ratio**: Multiplier for the extrusion amount
- **layer_thickness**: The nominal layer height in microns

BrickLayers inspects each path's `feature` field. For wall paths matching the configured target (inner walls by default), it counts them sequentially. Every odd-numbered wall gets:

1. `z_offset += layer_height / 2` — shifts the wall up by half a layer
2. `flow_ratio *= extrusion_multiplier` — compensates for the increased gap

CuraEngine then generates G-code from the modified paths, automatically handling all extrusion calculations, retraction, and travel moves.

### Why Plugin > Post-Processing

Post-processing scripts (like GeekDetour/BrickLayers or TengerTechnologies/Bricklayers) operate on the finished G-code text. They must:

- Parse G-code line by line (fragile, flavor-dependent)
- Track extruder state, absolute/relative extrusion, retraction state
- Recalculate E values across modified sections
- Handle tool changes for multi-extruder setups
- Avoid corrupting support, skin, and infill sections

This is inherently fragile. Known issues with post-processing scripts include stringing, tool-change corruption, broken cancel-object metadata, and incompatibility with arc fitting and binary G-code formats.

BrickLayers as a CuraEngine plugin avoids all of these problems because it operates on the structured path data before G-code is generated. CuraEngine handles all the complexity of G-code generation from the modified paths.

### WASM Split Architecture

The algorithm is compiled to WebAssembly (WASI target) for portability:

- **One WASM binary** works on all platforms (Linux, macOS, Windows, x86_64, aarch64)
- The native **host binary** is platform-specific but only handles gRPC (thin wrapper)
- The **host** loads the WASM module via wasmtime, passes serialized protobuf data, and returns results
- If neither binary is available, a **Python prototype** provides the same algorithm as a fallback

## What Gets Modified (and What Doesn't)

BrickLayers **only modifies wall paths**:

| Path Type | Modified? | Notes |
|-----------|-----------|-------|
| `INNERWALL` | Yes (by default) | Primary use case |
| `OUTERWALL` | Optional (off by default) | Affects surface finish |
| `SKIN` | Never | Top/bottom surfaces unchanged |
| `FILL` | Never | Infill unchanged |
| `SUPPORT` | Never | Support structures unchanged |
| Travel moves | Never | Handled by CuraEngine |
| Retractions | Never | Handled by CuraEngine |

## Extrusion Compensation

When a wall bead is shifted up by half a layer height, it must bridge a slightly larger gap to the layer below. Without compensation, this creates micro-voids that can actually weaken the print (confirmed by CNC Kitchen's OrcaSlicer testing).

BrickLayers applies a configurable extrusion multiplier (default 1.05x = 5% more material) to shifted walls. Additionally:

- **First brick layer**: Gets an extra 15% boost (multiplier * 1.15) to establish a strong bond with the non-shifted layers below
- **Last brick layer**: Gets a 15% reduction (multiplier * 0.85) to avoid over-extrusion at the transition back to normal layers

## Layer Range Control

BrickLayers can be restricted to a layer range:

- **Start Layer** (default: 3): Skips the first few layers to preserve bed adhesion. The initial layers need maximum contact area with the build plate.
- **End Layer** (default: -1 = all): Optionally stop brick pattern before a specific layer, useful for parts with critical top surfaces.
