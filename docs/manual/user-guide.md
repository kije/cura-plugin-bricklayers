# BrickLayers User Guide

## Quick Start

1. Install the plugin (see [README](../../README.md#installation))
2. Open Cura, load your model
3. In Print Settings, expand **Experimental**
4. Enable **Brick Layers**
5. Slice and print

The defaults are tuned for the most common use case (inner walls only, 5% extrusion boost, starting at layer 3).

## Settings Reference

All BrickLayers settings appear under **Print Settings > Experimental** when the plugin is installed.

### Brick Layers (Enable/Disable)

**Setting:** `brick_layers_enabled`
**Type:** Boolean
**Default:** Off

Master toggle. When enabled, BrickLayers registers for CuraEngine's path modification slot and shifts alternating wall paths. A save-area indicator (brick icon with "B" badge) appears next to the save button when active.

### Extrusion Multiplier

**Setting:** `brick_layers_extrusion_multiplier`
**Type:** Float
**Default:** 1.05
**Range:** 0.5 - 2.0 (warning outside 0.8 - 1.5)

Controls how much extra material is extruded on shifted wall loops to compensate for the increased gap between the shifted loop and the layer below.

**Recommendations:**
- **1.05** (default): Good starting point for most materials
- **1.08 - 1.10**: Better for waterproofing applications
- **1.00**: Not recommended — can produce weaker results than no BrickLayers at all
- **> 1.15**: Risk of over-extrusion, blobs, and dimensional inaccuracy

The extrusion multiplier is the most impactful tuning parameter. CNC Kitchen's testing showed that BrickLayers without extrusion compensation can actually reduce strength compared to standard printing, while proper compensation (+5-10%) delivers the full benefit.

### Start Layer

**Setting:** `brick_layers_start_layer`
**Type:** Integer
**Default:** 3
**Minimum:** 1

The first layer to apply the brick pattern (1-indexed, matching Cura's layer numbering). Layers before this are printed normally.

**Recommendations:**
- **2-3** (default): Skip initial layers for bed adhesion
- **1**: Apply from the very first layer (not recommended — the first layer needs maximum bed contact)
- **5+**: Useful if you have a raft or thick brim

### End Layer

**Setting:** `brick_layers_end_layer`
**Type:** Integer
**Default:** -1
**Minimum:** -1

The last layer to apply the brick pattern. Use -1 (or 0) to apply to all layers.

**When to set an end layer:**
- Parts with critical flat top surfaces where dimensional accuracy matters
- Parts with very thin top sections where there aren't enough walls to alternate
- Testing the effect on a specific layer range

### Inner Walls

**Setting:** `brick_layers_apply_inner_walls`
**Type:** Boolean
**Default:** On

Shift alternating inner wall perimeter loops. This is the primary and recommended use of BrickLayers. Inner walls are structural — they're hidden behind the outer wall and shifting them has no visible effect on surface finish.

### Outer Walls

**Setting:** `brick_layers_apply_outer_walls`
**Type:** Boolean
**Default:** Off

Shift alternating outer wall perimeter loops. **Warning:** This affects dimensional accuracy and surface finish. The outer wall determines the visible surface of the print, and shifting it creates a visible scalloped pattern.

**When to enable:**
- Maximum strength is more important than appearance
- The part will be post-processed (sanding, coating)
- Functional parts where surface finish doesn't matter

## Recommended Settings by Use Case

### Functional Parts (General)

| Setting | Value |
|---------|-------|
| Brick Layers | On |
| Inner Walls | On |
| Outer Walls | Off |
| Extrusion Multiplier | 1.05 |
| Start Layer | 3 |
| End Layer | -1 |

Standard configuration. Good balance of strength improvement and print quality.

### Maximum Strength

| Setting | Value |
|---------|-------|
| Brick Layers | On |
| Inner Walls | On |
| Outer Walls | On |
| Extrusion Multiplier | 1.08 |
| Start Layer | 2 |
| End Layer | -1 |
| Wall Count (Cura setting) | 4+ |

Both wall types shifted, higher extrusion compensation. Combine with more walls for maximum interlocking. Surface finish will be affected.

### Waterproof Parts

| Setting | Value |
|---------|-------|
| Brick Layers | On |
| Inner Walls | On |
| Outer Walls | Off |
| Extrusion Multiplier | 1.08 - 1.10 |
| Start Layer | 3 |
| End Layer | -1 |
| Wall Count (Cura setting) | 4+ |

Higher extrusion multiplier fills micro-voids more aggressively. Testing has shown zero water ingress at 4 Bar with ASA at 3mm wall thickness with these settings.

### Visual / Display Parts

| Setting | Value |
|---------|-------|
| Brick Layers | On |
| Inner Walls | On |
| Outer Walls | **Off** |
| Extrusion Multiplier | 1.03 - 1.05 |
| Start Layer | 3 |
| End Layer | -1 |

Lower extrusion multiplier minimizes any visible effect. Outer walls stay smooth.

## Material Considerations

| Material | Tested? | Notes |
|----------|---------|-------|
| PLA | Yes | +14% tensile strength (CNC Kitchen). Default settings work well |
| PETG | Yes | ~+10% tensile strength (CNC Kitchen). Default settings work well |
| ASA | Yes | Excellent waterproofing results. Use 1.08-1.10x multiplier |
| ABS | Untested | Expected to work similarly to ASA |
| TPU/Flex | Untested | May need lower extrusion multiplier due to material elasticity |
| Nylon | Untested | Expected to benefit from interlocking due to nylon's layer adhesion challenges |

## Print Settings That Affect BrickLayers

### Wall Count

More walls = more walls to alternate = stronger interlocking. **Minimum 2 walls** required for BrickLayers to have any effect (with 1 wall, there's nothing to alternate). **3-4 walls** recommended for best results.

### Layer Height

BrickLayers works at any layer height. The Z-shift is always half the layer height:

| Layer Height | Z-Shift |
|-------------|---------|
| 0.10 mm | 0.05 mm |
| 0.20 mm | 0.10 mm |
| 0.28 mm | 0.14 mm |
| 0.30 mm | 0.15 mm |

### Infill

BrickLayers does not modify infill. Infill type and density can be set independently. For maximum waterproofing, consider higher wall counts instead of relying on infill.

### Vase Mode (Spiralize)

BrickLayers is effectively a no-op in vase mode since there's only a single continuous spiral wall with no discrete loops to alternate.

## Troubleshooting

### BrickLayers settings don't appear

- Verify the plugin is installed in the correct Cura plugins directory
- Check that the directory is named `BrickLayers/` (case-sensitive)
- Look in Cura's log for `BrickLayers` messages (Help > Show Configuration Folder > cura.log)

### No visible effect after enabling

- Ensure you have **2+ wall lines** — BrickLayers needs at least 2 walls to alternate
- Check that **Inner Walls** is enabled
- Verify **Start Layer** is less than your model's total layer count
- BrickLayers only affects wall paths, not infill or skin

### Print quality issues

- **Stringing**: Reduce extrusion multiplier slightly, or increase retraction
- **Over-extrusion / blobs**: Reduce extrusion multiplier (try 1.03)
- **Weak layers**: Increase extrusion multiplier (try 1.08-1.10). Without compensation, shifted walls can be weaker than standard

### Engine plugin not found

If Cura's log shows `BrickLayers: No engine plugin executable found`:
- The plugin falls back to the Python prototype automatically
- For best performance, download the pre-built binary from the release page
- Or build from source (requires Rust toolchain and protoc)

## Save-Area Indicator

When BrickLayers is enabled, a brick wall icon with a "B" badge appears next to the save button in Cura's toolbar. Hovering over it shows a tooltip confirming BrickLayers is active. This is purely informational — BrickLayers processing happens during slicing, not on save.
