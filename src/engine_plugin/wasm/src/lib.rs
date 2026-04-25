//! BrickLayers algorithm — compiled to WASI.
//!
//! Pure computation: takes serialized protobuf request bytes,
//! returns serialized protobuf response bytes. No networking.
//!
//! The host (native binary) handles gRPC and calls these functions
//! via wasmtime.

use prost::Message;

pub mod proto {
    include!(concat!(env!("OUT_DIR"), "/cura.plugins.v0.rs"));
}

/// PrintFeature enum values matching the protobuf definition.
const OUTERWALL: i32 = 1;
const INNERWALL: i32 = 2;

// Move-type features (travel/retraction between wall loops).
const MOVEUNRETRACTED: i32 = 8;
const MOVERETRACTED: i32 = 9;
const MOVEWHILERETRACTING: i32 = 12;
const MOVEWHILEUNRETRACTING: i32 = 13;
const STATIONARYRETRACTUNRETRACT: i32 = 14;

fn is_move_feature(f: i32) -> bool {
    matches!(f, 0 | MOVEUNRETRACTED | MOVERETRACTED | MOVEWHILERETRACTING
                | MOVEWHILEUNRETRACTING | STATIONARYRETRACTUNRETRACT)
}

/// Settings for the brick pattern algorithm, passed from host.
#[repr(C)]
pub struct BrickSettings {
    pub enabled: bool,
    pub start_layer: i64,
    pub end_layer: i64,
    pub apply_inner_walls: bool,
    pub apply_outer_walls: bool,
    pub extrusion_multiplier: f64,
    pub layer_height: i64, // microns
    pub inside_out: bool,
    /// Maximum XY distance (microns) between consecutive wall paths that are
    /// still considered part of the same contour. Larger than this → treated
    /// as a contour boundary; the sub-group's innermost wall is protected and
    /// the brick-pattern counter is reset.
    pub contour_break_distance: i64,
}

/// Request matching the gRPC CallRequest, but as a simple protobuf message.
#[derive(Clone, Message)]
pub struct ModifyRequest {
    #[prost(message, repeated, tag = "1")]
    pub gcode_paths: Vec<proto::GCodePath>,
    #[prost(int64, tag = "2")]
    pub extruder_nr: i64,
    #[prost(int64, tag = "3")]
    pub layer_nr: i64,
}

/// Response matching the gRPC CallResponse.
#[derive(Clone, Message)]
pub struct ModifyResponse {
    #[prost(message, repeated, tag = "1")]
    pub gcode_paths: Vec<proto::GCodePath>,
}

/// Core brick pattern algorithm.
///
/// Shifts alternating wall paths up by half a layer height using z_offset.
/// CuraEngine applies the offset during G-code generation.
pub fn modify_paths(
    mut paths: Vec<proto::GCodePath>,
    layer_nr: i64,
    settings: &BrickSettings,
) -> Vec<proto::GCodePath> {
    if !settings.enabled {
        return paths;
    }

    if layer_nr < settings.start_layer {
        eprintln!("WASM: passthrough (layer_nr={} < start_layer={})", layer_nr, settings.start_layer);
        return paths;
    }
    if settings.end_layer > 0 && layer_nr > settings.end_layer - 1 {
        return paths;
    }

    let layer_thickness = if settings.layer_height > 0 {
        settings.layer_height
    } else {
        paths
            .iter()
            .find(|p| p.layer_thickness > 0)
            .map(|p| p.layer_thickness)
            .unwrap_or(0)
    };

    if layer_thickness <= 0 {
        return paths;
    }

    let z_shift = layer_thickness / 2;

    let target_inner = settings.apply_inner_walls;
    let target_outer = settings.apply_outer_walls;

    if !target_inner && !target_outer {
        return paths;
    }

    let is_first_brick = layer_nr == settings.start_layer;
    let is_last_brick = settings.end_layer > 0 && layer_nr == settings.end_layer - 1;
    let effective_multiplier = if is_first_brick {
        settings.extrusion_multiplier * 1.15
    } else if is_last_brick {
        settings.extrusion_multiplier * 0.85
    } else {
        settings.extrusion_multiplier
    };

    // Phase 1: Collect groups of target wall path indices.
    //
    // CuraEngine inserts travel/retraction paths between wall loops of
    // the same contour. We tolerate those gaps: a group is a maximal
    // run of target-wall indices separated only by move-type paths
    // *within the same mesh*. A change in mesh_name signals an
    // inter-object boundary and breaks the group. Paths with empty
    // mesh_name are outside the per-mesh context (skirt, brim, support,
    // prime tower) and must not be grouped.
    // Minimum XY extent for a wall path to be considered a "real" wall for
    // the brick pattern. Shorter paths are gap-fills / dense-infill bridges /
    // degenerate seam-anchor markers that CuraEngine emits with the
    // INNERWALL/OUTERWALL feature tag but zero or sub-line-width geometry
    // (in real slices ~75% of broadcast INNERWALLs are single-point
    // seam anchors). Shifting those to half-layer Z produces the
    // "scattered 0.1 mm wall fragments" artefact.
    const WALL_MIN_EXTENT_UM: i64 = 400; // 0.4 mm

    let path_extent = |p: &proto::GCodePath| -> i64 {
        // Sum of per-segment |dx|+|dy| (Manhattan length) — cheap and good
        // enough for a threshold check. Returns 0 for a path with <2 points.
        let pts = match p.path.as_ref() {
            Some(op) => &op.path,
            None => return 0,
        };
        if pts.len() < 2 {
            return 0;
        }
        let mut total: i64 = 0;
        for w in pts.windows(2) {
            let dx = (w[1].x - w[0].x).abs();
            let dy = (w[1].y - w[0].y).abs();
            total = total.saturating_add(dx).saturating_add(dy);
        }
        total
    };

    // Does this path have a feature we could potentially shift?
    let target_feature = |f: i32| -> bool {
        (target_inner && f == INNERWALL) || (target_outer && f == OUTERWALL)
    };

    // Should this path be added to a target group (i.e., considered for
    // shifting)?  Excludes per-mesh-disabled walls and sub-line-width
    // fragments, but NOTE callers must also consult `is_tolerated` below:
    // filtered-out wall-feature paths must be tolerated in Phase 1 so they
    // don't BREAK the surrounding group (treating them as "other feature"
    // would isolate real walls into singleton groups that can never shift).
    let is_target = |p: &proto::GCodePath| -> bool {
        if !target_feature(p.feature) {
            return false;
        }
        if is_mesh_disabled(&p.mesh_name) {
            return false;
        }
        if path_extent(p) < WALL_MIN_EXTENT_UM {
            return false;
        }
        true
    };

    // Tolerated = "ignore but do not flush the active wall group".  A path
    // that has a wall feature-tag (INNERWALL/OUTERWALL) but failed
    // ``is_target`` (too short, or mesh disabled) is a no-op for our
    // purposes: we don't shift it, but we must not let it split a run of
    // real walls into two groups either.  Real group boundaries come from
    // infill/skin/support/mesh-change/outside-mesh transitions — not from
    // 1-point seam anchors CuraEngine intersperses with real geometry.
    let is_tolerated = |p: &proto::GCodePath| -> bool {
        target_feature(p.feature) && !is_target(p)
    };

    // Each group entry is (path_idx, preceded_by_retract). The
    // retract flag is set when a retracted move was tolerated in the
    // same-mesh path between this wall and the previous wall in the
    // group. Phase 1.5 uses this as a strong contour-boundary signal
    // that is immune to user-tuned distance thresholds.
    let mut groups: Vec<Vec<(usize, bool)>> = Vec::new();
    let mut current_group: Vec<(usize, bool)> = Vec::new();
    let mut current_group_mesh = String::new();
    let mut pending_retract = false;
    for (i, p) in paths.iter().enumerate() {
        if is_target(p) {
            if p.mesh_name.is_empty() {
                // Outside-mesh wall — flush and skip (passthrough)
                if !current_group.is_empty() {
                    groups.push(std::mem::take(&mut current_group));
                    current_group_mesh = String::new();
                }
                pending_retract = false;
            } else if !current_group.is_empty() && p.mesh_name != current_group_mesh {
                // Wall from a different mesh → break group, start new one
                groups.push(std::mem::take(&mut current_group));
                current_group.push((i, false));
                current_group_mesh = p.mesh_name.clone();
                pending_retract = false;
            } else {
                // Same mesh (or first wall in new group). Carry the
                // "pending retract" seen on intervening travels as the
                // new wall's boundary flag.
                let is_first_in_group = current_group.is_empty();
                current_group.push((i, pending_retract && !is_first_in_group));
                if current_group_mesh.is_empty() {
                    current_group_mesh = p.mesh_name.clone();
                }
                pending_retract = false;
            }
        } else if is_move_feature(p.feature) {
            if !current_group.is_empty() {
                if p.mesh_name.is_empty() {
                    // Outside-mesh travel inside active group → break
                    groups.push(std::mem::take(&mut current_group));
                    current_group_mesh = String::new();
                    pending_retract = false;
                } else if p.mesh_name != current_group_mesh {
                    // Inter-object travel (destination mesh ≠ group's mesh) → break
                    groups.push(std::mem::take(&mut current_group));
                    current_group_mesh = String::new();
                    pending_retract = false;
                } else {
                    // Same-mesh move, tolerate — but remember if it was
                    // retracted. CuraEngine retracts on travels exceeding
                    // retraction_min_travel, which is orders of magnitude
                    // larger than intra-contour wall spacing — so a
                    // retracted same-mesh travel is the canonical signal
                    // of a contour boundary.
                    if p.retract {
                        pending_retract = true;
                    }
                }
            }
        } else if is_tolerated(p) {
            // Wall-feature-tagged but NOT a shiftable wall: seam-anchor
            // markers (single-point INNERWALL), sub-line-width fragments,
            // per-mesh-disabled walls.  Do NOT add to the group and do NOT
            // break it — these paths must remain intact in the output
            // (shifted_indices stays false, so they pass through via
            // normal_z_indexed) while the surrounding real walls keep
            // accumulating into a coherent contour group.
            //
            // In practice ~75% of INNERWALL broadcasts from CuraEngine are
            // 1-point seam anchors sitting between real walls.  Treating
            // them as "other feature → break" turned every real wall into
            // a singleton group, which the minimum-2-walls-per-group
            // shift logic then skipped entirely.  Tolerating them here is
            // the fix for "plugin runs but emits no shifts".
            //
            // NOTE: no mesh_name check — CuraEngine doesn't reroute wall
            // features mid-contour the way it does travels, so a tolerated
            // wall of another mesh is not something we expect in practice.
        } else {
            // Any other feature (INFILL, SKIN, SUPPORT, …) → break
            if !current_group.is_empty() {
                groups.push(std::mem::take(&mut current_group));
                current_group_mesh = String::new();
            }
            pending_retract = false;
        }
    }
    if !current_group.is_empty() {
        groups.push(current_group);
    }

    // Phase 1.5: Cluster each Phase-1 group's walls into per-contour
    // sub-groups using GLOBAL bbox-overlap union-find.
    //
    // Why not stream-order splitting:  CuraEngine orders wall paths by
    // spatial-traversal optimisation, not per-contour.  Two walls that
    // are concentric insets of the same physical contour can be 20+ path
    // indices apart in the stream, with walls from other contours
    // interleaved between them.  Any stream-adjacent split heuristic
    // (distance, retract, bbox-overlap-of-neighbours) misses these pairs.
    //
    // The fix is a quadratic pass:  every wall-pair whose bboxes overlap
    // is united into the same cluster.  Concentric walls of one contour
    // have nested (strongly-overlapping) bboxes.  Walls of truly disjoint
    // contours have disjoint bboxes.  For the common "hole inside a big
    // feature" edge case — the hole's bbox sits inside the outer's bbox
    // and they'd be wrongly merged — we'd need CuraEngine to expose
    // contour IDs in the proto.  Accept that limitation for now:  it
    // over-merges rather than over-splits, which degrades to the v3
    // positional-innermost behaviour (wrong walls protected, but every
    // contour shifts SOMETHING) instead of the recent degenerate 5-shifts
    // per layer regression.
    //
    // `preceded_by_retract` and `contour_break_distance` remain available
    // as tightening signals but are secondary to the bbox clustering.
    let path_bbox = |idx: usize| -> Option<(i64, i64, i64, i64)> {
        let pts = paths[idx].path.as_ref().map(|op| &op.path)?;
        if pts.is_empty() {
            return None;
        }
        let (mut xmin, mut xmax) = (i64::MAX, i64::MIN);
        let (mut ymin, mut ymax) = (i64::MAX, i64::MIN);
        for p in pts {
            if p.x < xmin { xmin = p.x; }
            if p.x > xmax { xmax = p.x; }
            if p.y < ymin { ymin = p.y; }
            if p.y > ymax { ymax = p.y; }
        }
        Some((xmin, xmax, ymin, ymax))
    };

    let bbox_overlap = |a: (i64, i64, i64, i64), b: (i64, i64, i64, i64)| -> bool {
        a.0 <= b.1 && a.1 >= b.0 && a.2 <= b.3 && a.3 >= b.2
    };

    // Simple union-find.
    fn uf_find(parent: &mut [usize], mut x: usize) -> usize {
        while parent[x] != x {
            parent[x] = parent[parent[x]];
            x = parent[x];
        }
        x
    }
    fn uf_union(parent: &mut [usize], a: usize, b: usize) {
        let ra = uf_find(parent, a);
        let rb = uf_find(parent, b);
        if ra != rb { parent[ra] = rb; }
    }

    let groups: Vec<Vec<usize>> = {
        let mut result: Vec<Vec<usize>> = Vec::new();
        for group in groups {
            let n = group.len();
            if n <= 1 {
                // Single-wall groups pass through unchanged.
                result.push(group.into_iter().map(|(idx, _)| idx).collect());
                continue;
            }
            // Compute bboxes for each wall in this group; track the wall's
            // original path index and its preceded_by_retract flag.
            let items: Vec<(usize, bool, Option<(i64, i64, i64, i64)>)> = group
                .into_iter()
                .map(|(idx, r)| (idx, r, path_bbox(idx)))
                .collect();

            // Union-find over positions 0..n.  Start each in its own cluster.
            let mut parent: Vec<usize> = (0..n).collect();
            for i in 0..n {
                for j in (i + 1)..n {
                    // Retract signal is a HARD boundary: if the step from
                    // i→j crossed a retracted travel, they cannot be in the
                    // same contour cluster no matter what their bboxes say.
                    //
                    // We approximate "step from i to j crossed a retract"
                    // by checking items[j].preceded_by_retract only when
                    // j = i+1 (adjacent in the group list).  Non-adjacent
                    // pairs rely purely on bbox overlap — the retract flag
                    // is attached to the immediate predecessor of each
                    // wall, not to arbitrary pairs.
                    if let (Some(bi), Some(bj)) = (items[i].2, items[j].2) {
                        if bbox_overlap(bi, bj) {
                            if j == i + 1 && items[j].1 {
                                // Retract between consecutive walls — keep them separate
                                // even though bboxes overlap.
                                continue;
                            }
                            uf_union(&mut parent, i, j);
                        }
                    }
                }
            }

            // Group items by their cluster root, preserving stream order
            // within each cluster (Phase 3 expects this ordering).
            use std::collections::HashMap;
            let mut clusters: HashMap<usize, Vec<usize>> = HashMap::new();
            let mut cluster_first_seen: HashMap<usize, usize> = HashMap::new();
            for (i, item) in items.iter().enumerate() {
                let root = uf_find(&mut parent, i);
                cluster_first_seen.entry(root).or_insert(i);
                clusters.entry(root).or_default().push(item.0);
            }

            // Emit clusters in the order their first member appeared in
            // the original stream — keeps the output's normal-Z ordering
            // stable relative to CuraEngine's traversal choice.
            let mut roots: Vec<usize> = clusters.keys().copied().collect();
            roots.sort_by_key(|r| cluster_first_seen[r]);
            for root in roots {
                result.push(clusters.remove(&root).unwrap());
            }
        }
        result
    };

    // Phase 2: Apply shifts per (sub-)group, protecting the innermost wall.
    //
    // Innermost wall determination is *geometry-based*: the innermost wall
    // is the one with the smallest XY bounding-box extent. This is robust to
    //   - inset_direction being set differently per printer (fdmprinter's
    //     default is inside_out, which we can't assume is what Cura sends),
    //   - material_alternate_walls reversing the wall order on alternating
    //     layers without updating inset_direction,
    //   - any other quirk that changes the path-list position of the spatial
    //     innermost wall.
    //
    // The positional rule (group[-1] for outside_in, group[0] for inside_out)
    // used earlier was correct only when Cura's actual wall order exactly
    // matched the broadcast inset_direction. In practice the ordering
    // depends on settings we don't see, and the wrong positional choice
    // causes the *actual* innermost wall (the one adjacent to skin/infill)
    // to be shifted to a half-layer Z — where Cura's preview doesn't render
    // it at the current layer, producing the "missing walls" artefact.
    //
    // Fallback: when all walls in the sub-group tie for smallest extent
    // (degenerate geometry or generic test paths with identical coords),
    // use the configured inset_direction for backwards compatibility.
    let mut shifted_indices: Vec<bool> = vec![false; paths.len()];

    let find_innermost = |group: &[usize], paths: &[proto::GCodePath]| -> usize {
        // Each wall's bounding-box "extent" (perimeter of axis-aligned bbox).
        let extents: Vec<(usize, i64)> = group.iter().map(|&idx| {
            let ext = paths[idx].path.as_ref().map(|p| {
                let pts = &p.path;
                if pts.is_empty() {
                    return i64::MAX;
                }
                let (mut xmin, mut xmax) = (i64::MAX, i64::MIN);
                let (mut ymin, mut ymax) = (i64::MAX, i64::MIN);
                for pt in pts {
                    if pt.x < xmin { xmin = pt.x; }
                    if pt.x > xmax { xmax = pt.x; }
                    if pt.y < ymin { ymin = pt.y; }
                    if pt.y > ymax { ymax = pt.y; }
                }
                (xmax - xmin) + (ymax - ymin)
            }).unwrap_or(i64::MAX);
            (idx, ext)
        }).collect();

        // Smallest extent wins. Collect all candidates matching the minimum
        // so we can break a tie using the configured inset_direction.
        let min_ext = extents.iter().map(|(_, e)| *e).min().unwrap_or(i64::MAX);
        let candidates: Vec<usize> = extents.iter()
            .filter(|(_, e)| *e == min_ext)
            .map(|(i, _)| *i)
            .collect();

        if candidates.len() == 1 {
            candidates[0]
        } else if settings.inside_out {
            // Inside-to-outside: innermost is printed first.
            candidates[0]
        } else {
            // Outside-to-inside: innermost is printed last.
            *candidates.last().unwrap()
        }
    };

    for group in &groups {
        if group.len() < 2 {
            continue; // single wall in contour → always protected
        }

        let innermost_idx = find_innermost(group, &paths);

        let mut wall_counter: u32 = 0;
        for &idx in group {
            if idx == innermost_idx {
                continue;
            }
            if wall_counter % 2 == 0 {
                paths[idx].z_offset = z_shift;
                // flow_ratio == 0.0 means CuraEngine left it at the proto default;
                // treat that as 1.0 (no change) so multiplication is correct.
                let base = if paths[idx].flow_ratio == 0.0 { 1.0_f64 } else { paths[idx].flow_ratio };
                paths[idx].flow_ratio = base * effective_multiplier;
                shifted_indices[idx] = true;
            }
            wall_counter += 1;
        }
    }

    // Phase 3: Global partition — normal-Z first (all objects), shifted-Z second (all objects).
    //
    // All paths at layer-Z print together across all objects (one Z per layer),
    // then all shifted paths print together. This minimises Z oscillation.
    //
    // Between consecutive shifted walls we synthesize retracted travel moves
    // from the previous wall's endpoint to the next wall's start-point.
    // This prevents extrusion lines between objects (or non-adjacent walls
    // within the same object) when the reordering removes intermediate paths.

    // Find a representative travel/move path from the input to copy speed and
    // metadata fields from. CuraEngine crashes (exit code 1) if synthesized
    // paths lack speed_derivatives, speed_factor, or mesh_name.
    let ref_travel = paths.iter()
        .find(|p| is_move_feature(p.feature) && p.speed_derivatives.is_some())
        .or_else(|| paths.first());

    let ref_speed = ref_travel.and_then(|p| p.speed_derivatives.clone());
    let ref_speed_factor = ref_travel.map(|p| if p.speed_factor == 0.0 { 1.0 } else { p.speed_factor }).unwrap_or(1.0);
    let ref_layer_thickness = ref_travel.map(|p| p.layer_thickness).unwrap_or(layer_thickness);

    // Helper: create a retracted travel path between two points at shifted Z.
    // Copies speed_derivatives and speed_factor from a real path so CuraEngine
    // can generate valid G-code feedrates.
    //
    // IMPORTANT: all extrusion-related fields are set explicitly to zero
    // (rather than relying on `..Default::default()`). CuraEngine and the
    // Python prototype both treat `flow_ratio == 0.0` as "unset → use 1.0"
    // when emitting extrusion — the same ambiguity shouldn't apply to move
    // features, but we keep the contract explicit so a downstream emitter
    // can never interpret the path as an extrusion by accident. This is the
    // defence against Cura's layer preview rendering these synth travels as
    // bright-green `WALL-INNER` lines.
    let make_travel = |from: &proto::Point3D, to: &proto::Point3D, dest_mesh: &str| -> proto::GCodePath {
        proto::GCodePath {
            feature: MOVERETRACTED,
            retract: true,
            path: Some(proto::OpenPath {
                path: vec![from.clone(), to.clone()],
            }),
            z_offset: z_shift,
            speed_derivatives: ref_speed.clone(),
            speed_factor: ref_speed_factor,
            layer_thickness: ref_layer_thickness,
            mesh_name: dest_mesh.to_string(),
            // Explicit non-extrusion contract — do not rely on proto defaults:
            flow: 0.0,
            flow_ratio: 0.0,
            line_width: 0,
            width_factor: 0.0,
            ..Default::default()
        }
    };

    // Minimum XY distance between two points to warrant synthesising a
    // travel path. Below this threshold the nozzle is effectively "at" the
    // target already — CuraEngine can transition directly, and emitting a
    // sub-pixel travel produces zero-length preview artefacts that Cura
    // sometimes mis-renders as inner-wall fragments.
    const TRAVEL_MIN_UM: i64 = 100; // 0.1 mm

    let should_emit_travel = |from: &proto::Point3D, to: &proto::Point3D| -> bool {
        let dx = (to.x - from.x).abs();
        let dy = (to.y - from.y).abs();
        // Manhattan ≥ 100µm is a generous lower bound on Euclidean ≥ ~70µm;
        // below that we skip the synth travel entirely.
        dx.saturating_add(dy) >= TRAVEL_MIN_UM
    };

    // Build one shifted sub-sequence PER (sub-)group, plus an anchor map that
    // tells the Phase 3b loop "after emitting normal-Z path at this orig_idx,
    // inline this group's shifted walls (bracketed by bridge travels)".
    //
    // Emitting shifted walls per-group (rather than one global run at the end
    // of the layer) keeps the synthesised travels between shifted walls short
    // and *inside one contour*. The old global run could produce long
    // cross-model diagonals between shifted walls of unrelated contours, which
    // Cura's layer preview was rendering as `WALL-INNER` extrusions (the
    // bright-green diagonals the user reported).
    //
    // Trade-off: more Z-hops per layer (one up/down per sub-group that has
    // shifts) in exchange for faithful rendering of the intended tool paths.
    // Per user instruction, correctness is the priority over path count.
    //
    // Within each group's shifted sequence we still synthesise intra-group
    // travel moves when consecutive shifted walls don't share endpoints —
    // e.g. when an unshifted wall sat between two shifted walls in the group.
    let mut shifted_per_group: Vec<Vec<proto::GCodePath>> =
        vec![Vec::new(); groups.len()];

    for (gi, group) in groups.iter().enumerate() {
        let mut seq: Vec<proto::GCodePath> = Vec::new();
        for &idx in group {
            if shifted_indices[idx] {
                if let Some(prev) = seq.last() {
                    if !is_move_feature(prev.feature) {
                        let prev_end = prev.path.as_ref().and_then(|p| p.path.last());
                        let cur_start = paths[idx].path.as_ref().and_then(|p| p.path.first());
                        if let (Some(pe), Some(cs)) = (prev_end, cur_start) {
                            if should_emit_travel(pe, cs) {
                                seq.push(make_travel(pe, cs, &paths[idx].mesh_name));
                            }
                        }
                    }
                }
                seq.push(paths[idx].clone());
            }
        }
        shifted_per_group[gi] = seq;
    }

    // Anchor = the original index of the last non-shifted wall in each group
    // that has shifts. After pushing this index in the Phase 3b normal-Z loop
    // we inline the group's shifted run. Flat `Vec<i32>` indexed by orig_idx
    // gives us O(1) lookup; -1 means "not an anchor".
    let mut anchor_of: Vec<i32> = vec![-1; paths.len()];
    for (gi, group) in groups.iter().enumerate() {
        if shifted_per_group[gi].is_empty() {
            continue;
        }
        for &idx in group.iter().rev() {
            if !shifted_indices[idx] {
                anchor_of[idx] = gi as i32;
                break;
            }
        }
    }

    // Normal-Z sub-sequence: all non-shifted paths, retaining their original indices.
    let normal_z_indexed: Vec<(usize, proto::GCodePath)> = paths.into_iter().enumerate()
        .filter(|(idx, _)| !shifted_indices[*idx])
        .collect();

    // Phase 3b: Fill gaps in normal-Z created by removing shifted walls.
    //
    // When CuraEngine places two extrusion paths with no explicit travel between
    // them (combing / no z-hop), and a shifted wall sat between them in the
    // original sequence, removing that wall leaves the two paths adjacent in
    // normal-Z with mismatched endpoints. CuraEngine would extrude across the
    // gap → spurious green line in preview.
    //
    // We detect the gap by original-index distance: if consecutive normal-Z paths
    // have original indices i and j with j > i+1, something was removed between
    // them (by construction, only shifted walls can be removed from normal-Z).
    // Insert a MOVERETRACTED travel (z_offset=0 — stays at normal layer Z) to
    // bridge those mismatched endpoints.
    //
    // If originally adjacent (j == i+1), CuraEngine placed them back-to-back on
    // purpose (combing); do NOT insert a spurious travel.
    let shifted_total: usize = shifted_per_group.iter().map(|g| g.len() + 2).sum();
    let mut result: Vec<proto::GCodePath> =
        Vec::with_capacity(normal_z_indexed.len() + shifted_total + 16);
    let mut prev_state: Option<(usize, bool, proto::Point3D)> = None;
    // (original_index, is_extrusion, last_endpoint)

    for (orig_idx, mut path) in normal_z_indexed {
        if let Some((prev_orig_idx, prev_is_extr, ref prev_end)) = prev_state {
            if orig_idx > prev_orig_idx + 1 {   // a shifted wall was removed between them
                if prev_is_extr && !is_move_feature(path.feature) {
                    // Case A: extrusion → gap → extrusion
                    // No explicit travel between them; synthesize one.
                    // Explicit non-extrusion fields — see make_travel comment above.
                    let cur_start = path.path.as_ref().and_then(|p| p.path.first());
                    if let Some(cs) = cur_start {
                        if should_emit_travel(prev_end, cs) {
                            result.push(proto::GCodePath {
                                feature: MOVERETRACTED,
                                retract: true,
                                path: Some(proto::OpenPath {
                                    path: vec![prev_end.clone(), cs.clone()],
                                }),
                                z_offset: 0,
                                speed_derivatives: ref_speed.clone(),
                                speed_factor: ref_speed_factor,
                                layer_thickness: ref_layer_thickness,
                                mesh_name: path.mesh_name.clone(),
                                flow: 0.0,
                                flow_ratio: 0.0,
                                line_width: 0,
                                width_factor: 0.0,
                                ..Default::default()
                            });
                        }
                    }
                } else if !prev_is_extr && !is_move_feature(path.feature) {
                    // Case B: travel → gap → extrusion
                    // The travel's last point was the shifted wall's start; redirect
                    // it to the next extrusion's start so the head arrives correctly.
                    let cur_start = path.path.as_ref().and_then(|p| p.path.first()).cloned();
                    if let Some(cs) = cur_start {
                        if prev_end.x != cs.x || prev_end.y != cs.y {
                            if let Some(last_travel) = result.last_mut() {
                                if let Some(tp) = last_travel.path.as_mut() {
                                    if let Some(last_pt) = tp.path.last_mut() {
                                        *last_pt = cs;
                                    }
                                }
                            }
                        }
                    }
                } else if is_move_feature(path.feature) {
                    // Case C (travel → gap → travel) or Case D (extrusion → gap → travel):
                    // The travel's first point was aimed at the removed shifted wall's
                    // end position. For open-arc walls (start ≠ end), this leaves a gap
                    // between where the nozzle is (prev_end) and where the travel begins,
                    // which CuraEngine fills with an implicit extrusion crossing the wall.
                    // Redirect the travel's start to prev_end so they connect cleanly.
                    if let Some(tp) = path.path.as_mut() {
                        if let Some(first_pt) = tp.path.first_mut() {
                            if first_pt.x != prev_end.x || first_pt.y != prev_end.y {
                                first_pt.x = prev_end.x;
                                first_pt.y = prev_end.y;
                                first_pt.z = prev_end.z;
                            }
                        }
                    }
                }
            }
        }
        let new_end = path.path.as_ref().and_then(|p| p.path.last()).cloned()
            .unwrap_or_default();
        let is_extr = !is_move_feature(path.feature);
        prev_state = Some((orig_idx, is_extr, new_end.clone()));
        result.push(path);

        // Anchor point: inline this group's shifted run bracketed by bridge
        // travels. prev_state still points at `new_end` (the anchor's
        // endpoint) — so when the next normal-Z iteration runs, its gap
        // detection sees the anchor as the immediate predecessor, which is
        // exactly the invariant we want after the bridge-down returns the
        // nozzle to `new_end` at normal Z.
        let gi = anchor_of[orig_idx];
        if gi >= 0 {
            let shifted_seq = std::mem::take(&mut shifted_per_group[gi as usize]);
            if !shifted_seq.is_empty() {
                let first_start = shifted_seq.first()
                    .and_then(|p| p.path.as_ref())
                    .and_then(|p| p.path.first())
                    .cloned();
                let last_end = shifted_seq.last()
                    .and_then(|p| p.path.as_ref())
                    .and_then(|p| p.path.last())
                    .cloned();
                let first_mesh = shifted_seq.first()
                    .map(|p| p.mesh_name.clone())
                    .unwrap_or_default();

                // Bridge UP: retracted travel at z_shift from the anchor's
                // XY endpoint to the first shifted wall's start. Skipping
                // when endpoints already coincide avoids a degenerate
                // zero-length travel path.
                if let Some(fs) = &first_start {
                    if should_emit_travel(&new_end, fs) {
                        result.push(make_travel(&new_end, fs, &first_mesh));
                    }
                }

                result.extend(shifted_seq);

                // Bridge DOWN: retracted travel at z_offset=0 back to the
                // anchor's XY endpoint, matching prev_state. Guarantees that
                // the next normal-Z path starts from a known-correct nozzle
                // position, so CuraEngine doesn't need to resolve a mid-layer
                // Z change — the transition back to the layer's base Z is
                // explicit in the G-code.
                if let Some(le) = last_end {
                    if should_emit_travel(&le, &new_end) {
                        result.push(proto::GCodePath {
                            feature: MOVERETRACTED,
                            retract: true,
                            path: Some(proto::OpenPath {
                                path: vec![le, new_end.clone()],
                            }),
                            z_offset: 0,
                            speed_derivatives: ref_speed.clone(),
                            speed_factor: ref_speed_factor,
                            layer_thickness: ref_layer_thickness,
                            mesh_name: first_mesh,
                            flow: 0.0,
                            flow_ratio: 0.0,
                            line_width: 0,
                            width_factor: 0.0,
                            ..Default::default()
                        });
                    }
                }
            }
        }
    }

    // No global bridge + shifted-sequence append — Phase 3b has already
    // emitted every group's shifted run inline at its anchor.
    result
}

// -----------------------------------------------------------------------
// WASI FFI — called by the host via wasmtime
// -----------------------------------------------------------------------

/// Global settings, written by the host before calling modify.
static mut SETTINGS: BrickSettings = BrickSettings {
    enabled: false,
    start_layer: 2,
    end_layer: -1,
    apply_inner_walls: true,
    apply_outer_walls: false,
    extrusion_multiplier: 1.05,
    layer_height: 0,
    // Matches Cura's fdmprinter default. The geometry-based innermost
    // detection in modify_paths is the primary safety net — this setting
    // is only used as a tiebreaker when walls share identical bounding boxes.
    inside_out: true,
    contour_break_distance: 2000, // 2 mm in microns — matches Cura default
};

/// Allocate memory in the WASM module for the host to write into.
#[no_mangle]
pub extern "C" fn alloc(len: u32) -> *mut u8 {
    let mut buf = Vec::with_capacity(len as usize);
    let ptr = buf.as_mut_ptr();
    std::mem::forget(buf);
    ptr
}

/// Free memory previously allocated by alloc.
#[no_mangle]
pub extern "C" fn dealloc(ptr: *mut u8, len: u32) {
    unsafe {
        drop(Vec::from_raw_parts(ptr, 0, len as usize));
    }
}

/// Update settings from the host. Called before slicing starts.
#[no_mangle]
pub extern "C" fn set_settings(
    enabled: u32,
    start_layer: i64,
    end_layer: i64,
    apply_inner: u32,
    apply_outer: u32,
    extrusion_multiplier_x1000: i64,
    layer_height: i64,
    inside_out: u32,
    contour_break_distance: i64,
) {
    unsafe {
        SETTINGS.enabled = enabled != 0;
        SETTINGS.start_layer = start_layer;
        SETTINGS.end_layer = end_layer;
        SETTINGS.apply_inner_walls = apply_inner != 0;
        SETTINGS.apply_outer_walls = apply_outer != 0;
        SETTINGS.extrusion_multiplier = extrusion_multiplier_x1000 as f64 / 1000.0;
        SETTINGS.layer_height = layer_height;
        SETTINGS.inside_out = inside_out != 0;
        SETTINGS.contour_break_distance = contour_break_distance;
    }
}

/// List of mesh_names whose per-mesh settings have `brick_layers_enabled =
/// false`. Walls belonging to these meshes are excluded from target groups
/// (treated as non-target features), so they pass through without any
/// shift. This implements the per-mesh "disable bricks on this model"
/// override in `object_settings` broadcasts.
///
/// The host calls `clear_disabled_meshes()` then `add_disabled_mesh()`
/// once per disabled mesh before every `process_layer` call, so the list
/// always reflects the CURRENT slice's per-mesh state.
static mut DISABLED_MESHES: Vec<String> = Vec::new();

#[no_mangle]
pub extern "C" fn clear_disabled_meshes() {
    unsafe {
        DISABLED_MESHES.clear();
    }
}

#[no_mangle]
pub extern "C" fn add_disabled_mesh(name_ptr: *const u8, name_len: u32) {
    if name_ptr.is_null() || name_len == 0 {
        return;
    }
    unsafe {
        let bytes = std::slice::from_raw_parts(name_ptr, name_len as usize);
        if let Ok(s) = std::str::from_utf8(bytes) {
            DISABLED_MESHES.push(s.to_string());
        }
    }
}

/// Check whether the given path's mesh_name is on the disabled list.
/// Called from modify_paths; safe to call from non-WASM tests too.
pub(crate) fn is_mesh_disabled(mesh_name: &str) -> bool {
    if mesh_name.is_empty() {
        return false;
    }
    unsafe {
        DISABLED_MESHES.iter().any(|n| n == mesh_name)
    }
}

/// Process a layer's paths. Input/output are protobuf-encoded bytes.
///
/// The host writes the serialized ModifyRequest at `input_ptr`,
/// calls this function, then reads the serialized ModifyResponse
/// from the returned pointer. The response length is written to
/// `output_len_ptr`.
#[no_mangle]
pub extern "C" fn process_layer(
    input_ptr: *const u8,
    input_len: u32,
    output_len_ptr: *mut u32,
) -> *mut u8 {
    let input = unsafe { std::slice::from_raw_parts(input_ptr, input_len as usize) };

    let request = match ModifyRequest::decode(input) {
        Ok(r) => r,
        Err(_) => {
            unsafe { *output_len_ptr = 0; }
            return std::ptr::null_mut();
        }
    };

    let settings = unsafe { &*std::ptr::addr_of!(SETTINGS) };
    eprintln!(
        "WASM process_layer: layer_nr={} extruder_nr={} paths={} start_layer={} enabled={}",
        request.layer_nr, request.extruder_nr, request.gcode_paths.len(),
        settings.start_layer, settings.enabled,
    );

    let result = unsafe {
        modify_paths(request.gcode_paths, request.layer_nr, &SETTINGS)
    };

    let response = ModifyResponse {
        gcode_paths: result,
    };

    let output = response.encode_to_vec();
    let len = output.len();
    let ptr = alloc(len as u32);
    unsafe {
        std::ptr::copy_nonoverlapping(output.as_ptr(), ptr, len);
        *output_len_ptr = len as u32;
    }
    ptr
}
