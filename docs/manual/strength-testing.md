# Strength and Testing Data

## Published Test Results

### CNC Kitchen — Tensile Strength (February 2024)

[Stefan Hermann (CNC Kitchen)](https://www.cnckitchen.com/blog/brick-layers-make-3d-prints-stronger) conducted the first controlled strength testing of the brick layer technique.

**Test Setup:**
- **Materials:** PLA and PETG
- **Layer height:** 0.25 mm
- **Wall count:** 4 perimeters, no infill (isolates wall adhesion)
- **Slicer:** Simplify3D (multi-process approach)
- **Test:** Tensile failure load (N) using DIY universal test machine

**Results:**

| Condition | Material | Failure Load | Improvement |
|-----------|----------|-------------|-------------|
| Standard slicing | PLA | 944 N | baseline |
| **Brick layers** | **PLA** | **1072 N** | **+14%** |
| Standard slicing | PETG | ~950 N | baseline |
| **Brick layers** | **PETG** | **~1045 N** | **~+10%** |

**Failure Mode Observation:**
- Standard: Clean, flat planar fracture at a single layer boundary (delamination)
- BrickLayers: Zig-zag or conical fracture surface — crack navigated around interlocked extrusions

### CNC Kitchen — OrcaSlicer Native Testing (June 2025)

Stefan re-tested using OrcaSlicer's native "Stagger Perimeters" feature:

| Condition | Result |
|-----------|--------|
| Staggered, **no** extrusion compensation (1.0x) | **Weaker** than standard baseline |
| Staggered, 110% extrusion | Significantly stronger than baseline |
| Standard, 110% extrusion (no stagger) | Somewhat stronger than baseline, but less than staggered+110% |

**Key Finding:** Extrusion compensation is essential. Without it, the voids created by the Z-offset can make the part weaker than standard printing. This is why BrickLayers defaults to a 1.05x extrusion multiplier.

### Waterproofing Test (December 2025)

[Hackaday.io project #204613](https://hackaday.io/project/204613) tested BrickLayers for underwater applications:

**Test Setup:**
- **Material:** ASA
- **Wall thickness:** 3 mm
- **Geometry:** Hollow bullet-shaped hulls
- **Test rig:** Polycarbonate tube + carbon fibre end caps, pressurized with compressed air
- **Metric:** Mass gain (water absorption in grams) before/after pressure exposure

**Results:**

| Condition | Pressure | Water Absorbed |
|-----------|----------|----------------|
| Standard FDM (best prior result) | 2 Bar (20 m depth) | Measurable leakage |
| BrickLayers, 1.0x flow | 2 Bar | ~3 ml |
| **BrickLayers, 1.05x flow** | **4 Bar (40 m depth)** | **0 measurable** |
| **BrickLayers, 1.10x flow** | **4 Bar (40 m depth)** | **0 measurable** |

At 1.05-1.10x flow, BrickLayers-printed parts were **completely watertight at 4 Bar (40 m water depth equivalent)** — surpassing acetone-smoothed samples tested in the same series.

## Understanding the Numbers

### Why +14% Matters

While 14% may seem modest, context is important:

1. **It's free.** No additional material, no additional print time, no post-processing
2. **It targets the weakest failure mode.** FDM parts fail primarily at layer boundaries — even a small improvement here matters disproportionately for functional parts
3. **It stacks with other improvements.** BrickLayers combines with higher wall counts, better temperature settings, and material selection
4. **The waterproofing benefit is dramatic.** Going from "leaks at 2 Bar" to "watertight at 4 Bar" is a qualitative improvement

### Why Extrusion Compensation Is Critical

```
Without compensation (1.0x flow):

    ╭──────╮
    │      │
    ╰──────╯         Gap: Extra void between
         ╭──────╮    shifted and non-shifted walls.
         │      │    Can be WEAKER than standard.
         ╰──────╯
    ╭──────╮
    │      │
    ╰──────╯


With compensation (1.05-1.10x flow):

    ╭──────╮
    │██████│
    ╰──────╯         Gap: Filled with extra
         ╭──────╮    material. Stronger bond
         │██████│    and better seal.
         ╰──────╯
    ╭──────╮
    │██████│
    ╰──────╯
```

## Comparison with Other Strength Techniques

| Technique | Mechanism | Strength Gain | Ease of Use | Drawbacks |
|-----------|-----------|---------------|-------------|-----------|
| **BrickLayers** | Z-offset interlocking | +10-14% tensile | Enable in settings | Needs flow tuning |
| More walls | Additional perimeters | Linear with count | Slider in settings | More material, print time |
| Higher extrusion temp | Better polymer diffusion | Material-dependent | Temperature setting | Stringing, warping |
| Cura "Alternate Extra Wall" | Extra wall on odd layers | Modest (unquantified) | Checkbox in settings | Different mechanism (not interlocking) |
| Annealing (post-process) | Crystallization | +40-100% (PLA) | Heat treatment after print | Dimensional change, warping |
| Carbon fiber filament | Reinforced material | +200-400% | Material swap | Expensive, nozzle wear |

BrickLayers is unique in providing a meaningful strength improvement with **zero additional cost or effort** beyond enabling a setting.

## Measured vs. Expected Performance

| Claim | Source | Confidence | Notes |
|-------|--------|------------|-------|
| +14% tensile (PLA) | CNC Kitchen, Feb 2024 | High (92%) | Controlled test, independently cited |
| +10% tensile (PETG) | CNC Kitchen, Feb 2024 | High (85%) | Same methodology |
| Watertight at 4 Bar (ASA) | Hackaday.io, Dec 2025 | High (88%) | Single experimenter, detailed methodology |
| ~30% improvement possible | Community forums | Low (50%) | Speculation, not measured |
| Compensation required | CNC Kitchen, Jun 2025 | High (95%) | Confirmed: stagger without compensation = weaker |

## What's Not Been Tested

The following have not been measured in publicly available testing:

- **Compressive strength** — How does interlocking affect crush resistance?
- **Shear strength** — Lateral load performance
- **Impact resistance** — Drop test / Charpy impact behavior
- **Fatigue life** — Cyclic loading durability
- **Print time overhead** — No measured data (expected to be minimal)
- **Material-specific results** beyond PLA, PETG, and ASA
- **Interaction with infill patterns** — Does infill type affect the wall interlocking benefit?

## Reproducing the Tests

For rigorous validation on your own hardware, see the [Hardware Test Protocol](../../src/tests/HARDWARE_TEST_PROTOCOL.md). It includes:

- 16 test models (M1-M16) covering cubes, cylinders, overhangs, thin walls, multi-body, bridges, and dual-extruder scenarios
- 8 settings combinations (S1-S8)
- 5 G-code flavor tests (Marlin, Griffin, RepRap, Klipper)
- 4 layer height variants
- G-code inspection procedures (E continuity, tool change validation, Z-shift verification)
- Print quality and dimensional accuracy checklists
- Strength testing protocol (delamination test, crush test)

## Prior Art and Academic Context

The brick layer concept has roots in:

- **US Patent 5,653,925 (Stratasys, 1995):** Original FDM patent covering staggered bead patterns. Expired 2015-2016, now public domain.
- **Nacre-inspired architecture (ACS Omega, 2022):** Bio-mimetic research showing that brick-and-mortar interlocking significantly improves fracture resistance in layered structures.
- **Interfacial mechanical design (npj Advanced Manufacturing, 2025):** Up to +389% improvement in PLA/TPU interfacial toughness using orientation-based mechanical interlocking.

The academic literature broadly confirms that geometric interlocking at layer interfaces is a valid and powerful strategy for improving FDM part strength, supporting the empirical results measured by CNC Kitchen and others.

## References

1. CNC Kitchen. "Brick Layers - Make 3D Prints Stronger." February 2024. [cnckitchen.com](https://www.cnckitchen.com/blog/brick-layers-make-3d-prints-stronger)
2. Creality. "What Is Brick Layer Slicing in 3D Printing?" [store.creality.com](https://store.creality.com/blogs/basics/brick-layer)
3. GeekDetour/BrickLayers. [github.com](https://github.com/GeekDetour/BrickLayers)
4. TengerTechnologies/Bricklayers. [github.com](https://github.com/TengerTechnologies/Bricklayers)
5. Hackaday. "Brick Layers: The Promise of Stronger 3D Prints." November 2024.
6. Hackaday. "Testing Brick Layers in OrcaSlicer With Staggered Perimeters." June 2025.
7. Hackaday.io. "Brick Layers: Making 3D Prints Super Waterproof." Project #204613, December 2025.
8. OrcaSlicer PR #8181. "Stagger Perimeters." [github.com](https://github.com/OrcaSlicer/OrcaSlicer/pull/8181)
