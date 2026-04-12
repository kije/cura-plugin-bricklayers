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
    // run of target-wall indices separated only by move-type paths.
    let is_target = |f: i32| -> bool {
        (target_inner && f == INNERWALL) || (target_outer && f == OUTERWALL)
    };

    let mut groups: Vec<Vec<usize>> = Vec::new();
    let mut current_group: Vec<usize> = Vec::new();
    for (i, p) in paths.iter().enumerate() {
        if is_target(p.feature) {
            current_group.push(i);
        } else if is_move_feature(p.feature) {
            // tolerate travel gaps inside a group
        } else {
            if !current_group.is_empty() {
                groups.push(std::mem::take(&mut current_group));
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
                paths[idx].z_offset += z_shift;
                // flow_ratio == 0.0 means CuraEngine left it at the proto default;
                // treat that as 1.0 (no change) so multiplication is correct.
                let base = if paths[idx].flow_ratio == 0.0 { 1.0_f64 } else { paths[idx].flow_ratio };
                paths[idx].flow_ratio = base * effective_multiplier;
                shifted_indices[idx] = true;
            }
            wall_counter += 1;
        }
    }

    // Phase 3: Stable partition — normal-Z paths first, shifted paths second.
    let mut normal_z: Vec<proto::GCodePath> = Vec::new();
    let mut shifted_z: Vec<proto::GCodePath> = Vec::new();
    for (idx, path) in paths.into_iter().enumerate() {
        if shifted_indices[idx] {
            shifted_z.push(path);
        } else {
            normal_z.push(path);
        }
    }
    normal_z.extend(shifted_z);
    normal_z
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
