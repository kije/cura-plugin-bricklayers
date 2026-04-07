# BrickLayers Hardware Test Protocol

Rigorous validation protocol for BrickLayers plugin on real 3D printing hardware.
Covers single extruder, multi-extruder, edge-case geometries, and G-code flavors.

---

## Test Models

All models should be printed at **0.2mm layer height** unless stated otherwise.
Use **2-3 wall lines** and **15-20% infill** unless stated otherwise.

### M1 — Simple Cube (Baseline)
- **Geometry**: 20x20x20mm cube
- **STL**: Any slicer's built-in cube, or export from CAD
- **Purpose**: Baseline validation — even walls, no curves, no overhangs
- **What to check**: Layer seam alignment, wall interlocking visible on fracture, no stringing between shifted loops

### M2 — Cylinder
- **Geometry**: 20mm diameter, 20mm tall cylinder
- **Purpose**: Curved walls — validates loop reordering on non-planar perimeters where G-code has many short segments
- **What to check**: Smooth walls, no artifacts from loop reordering, no visible layer shift at seam

### M3 — Thin Wall Box
- **Geometry**: 40x40x20mm box with **1 wall line** (wall thickness = nozzle diameter, e.g. 0.4mm)
- **Slicer setting**: Wall count = 1, no infill
- **Purpose**: With only 1 wall loop per layer, there's nothing to defer (every other loop = 0 loops deferred). BrickLayers should effectively be a no-op
- **What to check**: Print should be identical to non-BrickLayers output. No crashes, no artifacts

### M4 — Overhang Staircase
- **Geometry**: Staircase/pyramid shape — a 40x40mm base stepping inward every 5mm of height (e.g., 40mm -> 30mm -> 20mm -> 10mm at top)
- **Purpose**: Tests `is_last_brick` detection at step boundaries. When the next layer has fewer/no wall sections at the boundary, BrickLayers must stop shifting loops to avoid extending walls above the model surface
- **What to check**: Clean step edges, no wall material protruding above steps, no over-extrusion at boundaries

### M5 — Sloped Overhang (45-degree and beyond)
- **Geometry**: Wedge shape — 40x20mm base, one face angled at 45 degrees, one at 60 degrees
- **Purpose**: Walls that gradually disappear as the slope angle increases. Tests `_next_layer_has_walls` detection
- **What to check**: Clean overhang surface, no extra wall material at the overhang edge, no shifted loops extending beyond model boundary

### M6 — Tall Narrow Tower
- **Geometry**: 10x10x60mm tower (6:1 aspect ratio)
- **Purpose**: Many layers processed, small cross-section. Tests cumulative E tracking accuracy over many layers, position continuity, and retraction behavior
- **What to check**: Consistent wall quality from bottom to top, no E accumulation drift (gaps or blobs near top), no stringing

### M7 — Box With Holes
- **Geometry**: 30x30x20mm box with 3 circular holes (8mm diameter) through the walls (X, Y, and Z directions)
- **Purpose**: Internal wall sections that start/stop around holes. Tests loop detection when wall perimeters are interrupted
- **What to check**: Clean hole edges, no wall artifacts around hole openings, no misplaced extrusion

### M8 — Multi-Body (Two Cubes)
- **Geometry**: Two 15x15x15mm cubes placed 10mm apart on the build plate
- **Purpose**: Tests that BrickLayers correctly handles multi-object layers where G-code has travels between objects
- **What to check**: Both cubes processed correctly, no cross-contamination of coordinates between objects

### M9 — Varying Wall Count
- **Geometry**: 30x30x30mm box with wall count changing via Cura's "per-model settings" or by using a model with thin sections
- **Alternative**: Print 3 separate 20x20x10mm cubes with wall counts of 1, 2, and 4
- **Purpose**: Tests loop alternation with different numbers of available loops
- **What to check**: Correct alternation pattern for each wall count, no crashes

### M10 — Bridge Test
- **Geometry**: Two 10x10x15mm pillars 20mm apart with a 20x10x5mm bridge connecting them at 15mm height
- **Purpose**: Bridge sections use different TYPE markers (SKIN, FILL). BrickLayers must not interfere with bridge G-code
- **What to check**: Clean bridge, no sagging caused by BrickLayers modifications, bridge extrusion unchanged

### M11 — Hemisphere
- **Geometry**: 30mm diameter hemisphere (half sphere)
- **Purpose**: Continuously changing cross-section with shrinking wall perimeters toward the top. Tests is_last_brick detection as walls disappear
- **What to check**: Smooth dome surface, no artifacts at top where walls get very short

### M12 — Vase Mode Test
- **Geometry**: 30mm diameter, 40mm tall cylinder
- **Slicer setting**: Spiralize outer contour ON (vase mode)
- **Purpose**: Vase mode produces a single continuous spiral — no discrete loops. BrickLayers should effectively be a no-op since there are no loop boundaries to detect
- **What to check**: Vase prints normally, no crashes, no artifacts. BrickLayers markers may appear in G-code but should not alter the output

---

## Dual/Multi-Extruder Test Models

### M13 — Cube + Soluble Support
- **Geometry**: 20x20x20mm cube with a 15mm overhang shelf (90-degree overhang needing support)
- **Slicer setting**: Support enabled, support extruder = T1, model extruder = T0
- **Purpose**: Core dual-extruder test. Validates that BrickLayers only processes T0's wall loops and passes T1's support G-code through unchanged
- **What to check**:
  - Support prints at correct coordinates (not shifted)
  - Model walls have brick interlocking pattern
  - Tool changes (T0/T1) occur at correct points
  - No blobs or stringing at tool change points
  - Support removal is clean (support wasn't displaced)

### M14 — Dual-Color Cube
- **Geometry**: 20x20x20mm cube split vertically — left half T0, right half T1
- **Purpose**: Both extruders print model walls. Tests that BrickLayers only processes the configured primary extruder's walls
- **What to check**: Both colors print at correct positions, no coordinate mixing, clean color boundary

### M15 — Implicit Primary Extruder
- **Geometry**: Same as M13 (cube + overhang with support)
- **Slicer setting**: T1 as support extruder (prints first in header), T0 as model extruder
- **Purpose**: Specifically tests the fixed bug — T0 is implicitly active at layer start with no explicit T0 command
- **What to check**:
  - **Critical**: T1 support coordinates must NOT appear inside WALL-INNER sections
  - **Critical**: No coordinate shifting on support extruder
  - Model walls correctly interlocked
  - G-code inspection: grep for T1 commands — they should only appear in SUPPORT sections

### M16 — Support-Only Layers
- **Geometry**: Model with a floating shelf that has support-only layers below it (T1 only, no T0 walls for several layers)
- **Purpose**: Tests layers where the primary extruder has no wall sections — BrickLayers should skip these cleanly
- **What to check**: No crashes on support-only layers, no missing or extra commands

---

## Settings Matrix

For each model, test with these setting combinations:

| Test ID | Inner Walls | Outer Walls | Ext. Multiplier | Start Layer | End Layer |
|---------|-------------|-------------|------------------|-------------|-----------|
| S1      | ON          | OFF         | 1.05 (default)   | 3 (default) | -1 (all)  |
| S2      | OFF         | ON          | 1.05             | 3           | -1        |
| S3      | ON          | ON          | 1.05             | 3           | -1        |
| S4      | ON          | OFF         | 0.8              | 3           | -1        |
| S5      | ON          | OFF         | 1.3              | 3           | -1        |
| S6      | ON          | OFF         | 1.05             | 1           | -1        |
| S7      | ON          | OFF         | 1.05             | 5           | 10        |
| S8      | OFF         | OFF         | 1.05             | 3           | -1        |

**Minimum required**: Every model with S1. Models M1, M3, M13 with all settings.

---

## G-code Flavor Tests

Test model M1 (cube) and M13 (dual-extruder) on each flavor:

### F1 — Marlin (Most common)
- **Printers**: Ender 3, Prusa MK3/MK4, Creality, most RepRap-based
- **Extrusion mode**: Absolute (M82 emitted in header)
- **Characteristics**: Explicit M82/M83 commands, standard G28 homing
- **Verify**: E values are absolute and continuous across layers

### F2 — Marlin (Relative Extrusion)
- **Printers**: Same as F1 but with relative extrusion enabled in Cura
- **Slicer setting**: `relative_extrusion = true`
- **Characteristics**: M83 emitted in header, E values are deltas
- **Verify**: E values are relative (reset-style), no E accumulation

### F3 — Griffin (Ultimaker)
- **Printers**: Ultimaker S3, S5, S7
- **Extrusion mode**: Absolute (NO M82/M83 emitted — implied by firmware)
- **Characteristics**: No M82/M83 in G-code, uses Ultimaker header format, T0/T1 for dual extruder
- **Verify**: BrickLayers detects absolute mode from Cura settings (not G-code scanning), E values stay absolute

### F4 — RepRap
- **Printers**: Various RepRap firmware machines
- **Extrusion mode**: Typically relative (M83)
- **Characteristics**: Standard G-code with M83 header
- **Verify**: Relative E handling correct

### F5 — Klipper
- **Printers**: Voron, custom Klipper builds
- **Extrusion mode**: Relative (common) or absolute
- **Characteristics**: Standard G-code, may have Klipper-specific macros in header
- **Verify**: BrickLayers ignores non-standard header commands, processes layers correctly

---

## Layer Height Variants

Test with model M1 (cube) at these layer heights:

| Test   | Layer Height | Expected z_shift |
|--------|-------------|------------------|
| LH1    | 0.1mm       | 0.05mm           |
| LH2    | 0.2mm       | 0.10mm           |
| LH3    | 0.3mm       | 0.15mm           |
| LH4    | 0.12mm      | 0.06mm           |

---

## Test Procedure

### Phase 1: G-code Inspection (Before Printing)

For each test case, BEFORE printing:

1. **Slice the model** in Cura with BrickLayers enabled
2. **Save G-code** — both with and without BrickLayers
3. **Diff the files**: Compare to verify only wall sections are modified
4. **Check for markers**: Search for `;BrickLayers` comments — should appear in processed layers
5. **Verify E continuity** (absolute mode): E values should be monotonically increasing on the primary extruder. Run:
   ```bash
   grep "^G1.*E" output.gcode | awk '{for(i=1;i<=NF;i++) if($i~/^E/) print $i}' | sed 's/E//' | awk 'NR>1 && $1<prev {print "E DECREASED at line "NR": "$1" < "prev} {prev=$1}'
   ```
6. **Verify tool changes** (dual extruder): T commands should NOT appear inside `;TYPE:WALL-INNER` or `;TYPE:WALL-OUTER` sections:
   ```bash
   awk '/;TYPE:WALL/{in_wall=1} /;TYPE:[^W]/{in_wall=0} in_wall && /^T[0-9]/{print "TOOL CHANGE IN WALL at line "NR": "$0}' output.gcode
   ```
7. **Verify foreign extruder coordinates** (dual extruder): Support extruder coordinates should be identical between BrickLayers and non-BrickLayers output:
   ```bash
   # Extract T1 sections from both files and diff
   awk '/^T1/{f=1} /^T0/{f=0} f{print}' with_bricklayers.gcode > t1_with.txt
   awk '/^T1/{f=1} /^T0/{f=0} f{print}' without_bricklayers.gcode > t1_without.txt
   diff t1_with.txt t1_without.txt
   ```
8. **Verify Z-shift**: Shifted loops should appear at `layer_z + layer_height/2`:
   ```bash
   grep "BrickLayers Z-shift" output.gcode
   ```

### Phase 2: Print and Visual Inspection

For each print:

1. **First layer adhesion**: Verify BrickLayers doesn't activate on early layers (start_layer=3 by default)
2. **Wall quality**: Look for:
   - Consistent layer lines (no gaps between shifted and non-shifted loops)
   - No blobs at retract/unretract points added by BrickLayers
   - No stringing between shifted loop travels
   - No visible Z-banding from the half-layer shifts
3. **Dimensional accuracy**: Measure with calipers:
   - XY dimensions should be within 0.1mm of expected (no coordinate drift)
   - Z dimension should be within 0.15mm (half-layer shifts don't add height)
4. **Surface finish**: Compare against non-BrickLayers print of same model

### Phase 3: Strength Testing

For models M1 (cube) and M2 (cylinder):

1. Print **two copies**: one with BrickLayers, one without
2. **Layer adhesion test**: Try to split layers apart by hand — BrickLayers version should have significantly stronger inter-layer bond
3. **Crush test** (optional): Apply perpendicular force to layers and compare failure mode:
   - Without BrickLayers: clean layer separation (delamination)
   - With BrickLayers: material fracture rather than delamination

---

## Failure Criteria (STOP AND REPORT)

Immediately stop testing and report if any of these occur:

- [ ] Nozzle collision with print or bed
- [ ] Extruder grinding (filament stripping) — indicates impossible E values
- [ ] Print shifted on bed (X/Y offset mid-print) — indicates position continuity bug
- [ ] Support material printed at wrong location (dual extruder) — indicates extruder mixing bug
- [ ] Missing wall sections — indicates loops dropped during processing
- [ ] Extra extrusion in mid-air — indicates Z-shift not restored properly
- [ ] Firmware error / emergency stop — indicates invalid G-code generated

---

## Priority Test Matrix

If time is limited, run these tests in order:

| Priority | Model | Settings | Flavor | Why |
|----------|-------|----------|--------|-----|
| P0       | M1    | S1       | F1     | Baseline — must work |
| P0       | M13   | S1       | F3     | Dual extruder on Griffin — the reported bug |
| P0       | M15   | S1       | F3     | Implicit primary extruder — the exact bug fix |
| P1       | M1    | S1       | F3     | Griffin flavor (Ultimaker) |
| P1       | M4    | S1       | F1     | Overhang boundary detection |
| P1       | M3    | S1       | F1     | Thin wall (should be no-op) |
| P1       | M6    | S1       | F1     | Tall tower — E accumulation |
| P2       | M1    | S3       | F1     | Both wall types enabled |
| P2       | M1    | S7       | F1     | Limited layer range |
| P2       | M12   | S1       | F1     | Vase mode — should not break |
| P2       | M2    | S1       | F2     | Relative extrusion mode |
| P3       | M8    | S1       | F1     | Multi-body |
| P3       | M10   | S1       | F1     | Bridge integrity |
| P3       | M11   | S1       | F1     | Hemisphere — shrinking walls |

---

## Reporting Template

For each test, record:

```
Test: [Model]-[Settings]-[Flavor]-[Layer Height]
Example: M1-S1-F1-LH2

Printer: _______________
Nozzle: ___mm
Filament: _______________

G-code inspection:
  [ ] E continuity OK
  [ ] Tool changes OK (dual only)
  [ ] Foreign extruder coords unchanged (dual only)
  [ ] Z-shift values correct
  [ ] BrickLayers markers present

Print result:
  [ ] First layer OK
  [ ] Wall quality OK
  [ ] No stringing/blobs
  [ ] Dimensions within spec
  [ ] No artifacts at boundaries

Measurements:
  X: ___ mm (expected: ___)
  Y: ___ mm (expected: ___)
  Z: ___ mm (expected: ___)

Pass/Fail: ___
Notes: _______________
```
