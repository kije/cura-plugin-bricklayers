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
    let is_target = |f: i32| -> bool {
        (target_inner && f == INNERWALL) || (target_outer && f == OUTERWALL)
    };

    let mut groups: Vec<Vec<usize>> = Vec::new();
    let mut current_group: Vec<usize> = Vec::new();
    let mut current_group_mesh = String::new();
    for (i, p) in paths.iter().enumerate() {
        if is_target(p.feature) {
            if p.mesh_name.is_empty() {
                // Outside-mesh wall — flush and skip (passthrough)
                if !current_group.is_empty() {
                    groups.push(std::mem::take(&mut current_group));
                    current_group_mesh = String::new();
                }
            } else if !current_group.is_empty() && p.mesh_name != current_group_mesh {
                // Wall from a different mesh → break group, start new one
                groups.push(std::mem::take(&mut current_group));
                current_group.push(i);
                current_group_mesh = p.mesh_name.clone();
            } else {
                // Same mesh (or first wall in new group)
                current_group.push(i);
                if current_group_mesh.is_empty() {
                    current_group_mesh = p.mesh_name.clone();
                }
            }
        } else if is_move_feature(p.feature) {
            if !current_group.is_empty() {
                if p.mesh_name.is_empty() {
                    // Outside-mesh travel inside active group → break
                    groups.push(std::mem::take(&mut current_group));
                    current_group_mesh = String::new();
                } else if p.mesh_name != current_group_mesh {
                    // Inter-object travel (destination mesh ≠ group's mesh) → break
                    groups.push(std::mem::take(&mut current_group));
                    current_group_mesh = String::new();
                }
                // else: same-mesh move, tolerate
            }
        } else {
            // Any other feature (INFILL, SKIN, SUPPORT, …) → break
            if !current_group.is_empty() {
                groups.push(std::mem::take(&mut current_group));
                current_group_mesh = String::new();
            }
        }
    }
    if !current_group.is_empty() {
        groups.push(current_group);
    }

    // Phase 2: Apply shifts per group, protecting the innermost wall.
    let mut shifted_indices: Vec<bool> = vec![false; paths.len()];

    for group in &groups {
        if group.len() < 2 {
            continue; // single wall in contour → always protected
        }

        let innermost_idx = if settings.inside_out {
            group[0]
        } else {
            group[group.len() - 1]
        };

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
            ..Default::default()
        }
    };

    // Indices of groups that have at least one shifted wall.
    let groups_with_shifts: Vec<usize> = groups.iter().enumerate()
        .filter(|(_, g)| g.iter().any(|&idx| shifted_indices[idx]))
        .map(|(i, _)| i)
        .collect();

    // Build the shifted sub-sequence with synthesized travel moves.
    let mut shifted_sequence: Vec<proto::GCodePath> = Vec::new();
    for &gi in &groups_with_shifts {
        let group = &groups[gi];
        for &idx in group {
            if shifted_indices[idx] {
                // Insert travel if the previous shifted wall ends at a different position
                if let Some(prev) = shifted_sequence.last() {
                    if !is_move_feature(prev.feature) {
                        let prev_end = prev.path.as_ref().and_then(|p| p.path.last());
                        let cur_start = paths[idx].path.as_ref().and_then(|p| p.path.first());
                        if let (Some(pe), Some(cs)) = (prev_end, cur_start) {
                            if pe.x != cs.x || pe.y != cs.y {
                                shifted_sequence.push(make_travel(pe, cs, &paths[idx].mesh_name));
                            }
                        }
                    }
                }
                shifted_sequence.push(paths[idx].clone());
            }
        }
    }

    // Normal-Z sub-sequence: all non-shifted paths in their original order.
    let mut result: Vec<proto::GCodePath> = paths.into_iter().enumerate()
        .filter(|(idx, _)| !shifted_indices[*idx])
        .map(|(_, p)| p)
        .collect();

    // Synthesize retracted travel between normal-Z and shifted-Z sub-sequences.
    if let (Some(last_normal), Some(first_shifted)) = (result.last(), shifted_sequence.first()) {
        let le = last_normal.path.as_ref().and_then(|p| p.path.last());
        let fs = first_shifted.path.as_ref().and_then(|p| p.path.first());
        if let (Some(le), Some(fs)) = (le, fs) {
            let dest_mesh = &first_shifted.mesh_name;
            result.push(make_travel(le, fs, dest_mesh));
        }
    }

    result.extend(shifted_sequence);
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
    inside_out: false,
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
