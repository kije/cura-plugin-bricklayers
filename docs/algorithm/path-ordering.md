# BrickLayers — Path Ordering & Group Boundary Detection

This document covers how BrickLayers partitions CuraEngine's per-layer path stream into a
normal-Z pass and a shifted-Z pass, how wall groups are formed, and the output ordering for
every combination of the three user-visible settings.

---

## 1. The `mesh_name` Field

Every `GCodePath` carries a `mesh_name` string (proto field 15).  CuraEngine sets this via:

```cpp
// src/plugins/converters.cpp
gcode_path->set_mesh_name(path.mesh ? path.mesh->mesh_name : "");
```

The value depends on `LayerPlan::current_mesh_`:

| Path type | `mesh_name` value |
|-----------|-------------------|
| OUTERWALL, INNERWALL (per mesh) | Non-empty — the mesh's name |
| INFILL, SKIN, SUPPORT_INTERFACE (per mesh) | Non-empty — the mesh's name |
| **Inter-object MOVERETRACTED** | **Non-empty — the *destination* mesh's name** |
| SKIRTBRIM | Empty `""` |
| SUPPORT, SUPPORTINFILL | Empty `""` |
| PRIMETOWER | Empty `""` |
| Travels outside per-mesh loops | Empty `""` |

**Key finding:** `setMesh(mesh_ptr_B)` is called *before* the PathOrderOptimizer emits the
first `addTravel()` for mesh B.  The inter-object travel from mesh A → mesh B therefore
carries `mesh_name = "MeshB"`, not `""` and not `"MeshA"`.

### Implication for BrickLayers

Paths with empty `mesh_name` are outside the per-mesh context (helper paths — skirt, brim,
support, prime tower).  They must **never** be bricklayered:

- A target-wall path with `mesh_name == ""` must **not** be added to any group (passthrough).
- A move path with `mesh_name == ""` inside an active group must **break** the group
  (conservative guard; prevents outside-mesh travels from bridging two separate objects).

A change in `mesh_name` on a move path (non-empty, but different from the current group's
mesh) signals an inter-object travel → break the group.

---

## 2. Three Phase Algorithm

### Phase 1 — Group Collection

A *group* is a maximal contiguous run of target-wall paths (INNERWALL and/or OUTERWALL,
per settings) within the same mesh, with same-mesh move paths tolerated as gaps.

**Group boundary conditions (breaks the current group):**

| Event | Condition | Action |
|-------|-----------|--------|
| Target wall, empty `mesh_name` | Always | Flush group; skip path (passthrough) |
| Target wall, non-empty `mesh_name` ≠ group's mesh | Group non-empty | Flush; start new group |
| Move feature, `mesh_name == ""` | Group non-empty | Flush group |
| Move feature, `mesh_name` ≠ group's mesh | Group non-empty | Flush group |
| Any other feature (INFILL, SKIN, SUPPORT…) | Group non-empty | Flush group |

**Tolerated (group continues):**

| Event | Condition |
|-------|-----------|
| Move feature, same `mesh_name` as group | Group non-empty |
| Move feature, any `mesh_name` | Group empty (no active group) |

This rule replaces the previous naïve "tolerate all moves" approach and correctly splits
groups at object boundaries even when `GroupOuter=True` (`apply_outer_walls=True`).

### Phase 2 — Shift Application

For each group (minimum 2 walls required):

- The **innermost wall** (last index for outside-in ordering, first for inside-out) is
  **always protected** — never shifted.
- Remaining walls are counted with a `wall_counter`; walls where `wall_counter % 2 == 0`
  receive `z_offset += layer_thickness / 2` and an adjusted `flow_ratio`.

### Phase 3 — Global Partition with Elevated Travel Clones

All paths that were **not** shifted stay in their original relative order (normal-Z pass).
After the normal-Z pass comes a single **shifted-Z pass** containing, for each group-with-shifts:

1. The shifted wall paths from that group (in their original relative order).
2. If there is a next group-with-shifts: a **clone** of every move-feature path that lies
   between the last wall of this group and the first wall of the next group, with
   `z_offset += z_shift` on each clone.

This produces exactly **one Z lift** per layer across all objects:

```
Normal-Z pass:  all objects at layer_Z (travels preserved between objects)
                ↓ single Z hop to layer_Z + 0.5 * layer_thickness
Shifted-Z pass: all objects at shifted_Z (elevated travels cloned between objects)
```

---

## 3. Settings Reference

| Setting | Rust field | Description |
|---------|-----------|-------------|
| Wall Ordering | `inside_out: bool` | `false` = outside-in (default); `true` = inside-out |
| Print Infill before Walls | — | CuraEngine setting; controls where INFILL appears in the stream (not a BrickLayers setting, but it breaks groups the same way regardless) |
| Group Outer Walls | `apply_outer_walls: bool` | `false` (default) = only inner walls shifted; `true` = outer walls also shifted |

---

## 4. Output Ordering — All 8 Permutations

Notation:
- `O` = outer wall, `Iᵢ` = innermost inner wall (protected), `Iₛ` = shiftable inner wall
- `F` = infill, `T` = travel/retraction (MOVERETRACTED)
- `↑` = elevated by z_shift (shifted-Z pass), `T↑` = elevated travel clone
- Two objects on the plate: subscript 1 = obj1, 2 = obj2

### GroupOuter = False (apply_outer_walls = False) — all correct ✓

The outer wall (`O`) is not a target feature, so it acts as a group-break.  Each object's
inner walls form their own group.  Inter-object travel is correctly cloned in the shifted pass.

| # | Wall Order | Infill First | Normal-Z pass | Shifted-Z pass |
|---|-----------|-------------|---------------|----------------|
| 1 | Outside→In | Yes | `F, O₁, Iᵢ₁, T, O₂, Iᵢ₂` | `Iₛ₁↑, T↑, Iₛ₂↑` |
| 2 | Outside→In | No  | `O₁, Iᵢ₁, T, O₂, Iᵢ₂, F` | `Iₛ₁↑, T↑, Iₛ₂↑` |
| 3 | Inside→Out | Yes | `F, Iᵢ₁, O₁, T, Iᵢ₂, O₂` | `Iₛ₁↑, T↑, Iₛ₂↑` |
| 4 | Inside→Out | No  | `Iᵢ₁, O₁, T, Iᵢ₂, O₂, F` | `Iₛ₁↑, T↑, Iₛ₂↑` |

The shifted pass is identical in all 4 cases — exactly one Z lift, inter-object travel preserved.

### GroupOuter = True (apply_outer_walls = True) — correct after mesh_name fix ✓

CuraEngine emits outers for all objects first, then inners for all objects (when
`group_outer_walls` is enabled in Cura).  The inter-object MOVERETRACTED between obj1's
inners and obj2's inners carries `mesh_name = "MeshB"` (destination).  Phase 1 detects
this mesh change and breaks the group, yielding two separate groups and a correctly cloned
inter-object travel in the shifted pass.

Input stream structure (Outside→In, Infill First):
```
F, O₁[A], T_oa[A], O₂[B], T_ob[B], Iₛ₁[A], Iᵢ₁[A], T_inter[B], Iₛ₂[B], Iᵢ₂[B]
```

Group detection:
- `O₁` → OUTERWALL is also a target → starts group [O₁, Iₛ₁, Iᵢ₁] mesh=A ... but `O₂`
  has mesh=B → breaks at `T_ob[B]` or at `O₂[B]` itself.
- Final groups: `[O₁[A], Iₛ₁[A], Iᵢ₁[A]]` and `[O₂[B], Iₛ₂[B], Iᵢ₂[B]]`

| # | Wall Order | Infill First | Normal-Z pass | Shifted-Z pass |
|---|-----------|-------------|---------------|----------------|
| 5 | Outside→In | Yes | `F, O₁, T_oa, O₂, T_ob, Iᵢ₁, T_inter, Iᵢ₂` | `Iₛ₁↑, Oᵤ₁↑ (if shifted), T↑, Iₛ₂↑, Oᵤ₂↑ (if shifted)` |
| 6 | Outside→In | No  | `O₁, T_oa, O₂, T_ob, Iᵢ₁, T_inter, Iᵢ₂, F` | same shifted pass |
| 7 | Inside→Out | Yes | `F, Iᵢ₁, T_inter, Iᵢ₂, O₁, T_oa, O₂` | `Iₛ₁↑, T↑, Iₛ₂↑` |
| 8 | Inside→Out | No  | `Iᵢ₁, T_inter, Iᵢ₂, O₁, T_oa, O₂, F` | `Iₛ₁↑, T↑, Iₛ₂↑` |

All 8 permutations now produce exactly one Z lift per layer with no inter-object extrusion.

---

## 5. Empty mesh_name — Passthrough Guarantee

Paths with `mesh_name == ""` (skirt, brim, support, prime tower, their travels):

- **Never shifted** — they are either non-target features (naturally bypass grouping) or
  move features that break an active group before they can be tolerated inside it.
- **Always preserved in output** at their original position — the normal-Z filter only
  removes paths that are in `shifted_indices`; empty-mesh paths are never indexed there.
- Their travels are **never cloned** into the shifted sequence — Phase 3 only clones
  moves between two groups-with-shifts, which are always non-empty-mesh groups.

---

## 6. Revision History

| Date | Change |
|------|--------|
| 2026-04-13 | Initial algorithm (v1): global partition, no mesh_name awareness |
| 2026-04-13 | v2: group-local insertion (incorrect — caused per-object Z oscillation) |
| 2026-04-13 | v3: global partition + elevated travel clones (correct for GroupOuter=False) |
| 2026-04-13 | v4: mesh_name boundary detection in Phase 1 — fixes GroupOuter=True, adds empty-mesh passthrough guarantee |
