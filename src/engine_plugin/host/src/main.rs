mod wasm_runtime;

use std::collections::HashSet;
use std::io::Write;
use std::path::PathBuf;
use std::sync::{Arc, Mutex};

use clap::Parser;
use prost::Message;
use tonic::metadata::MetadataValue;
use tonic::transport::Server;
use tonic::{Request, Response, Status};
use tracing::{debug, info, warn};

use wasm_runtime::WasmRuntime;

// Debug dump configuration (populated once at startup from env vars).
// BRICKLAYERS_DUMP_LAYERS=148,150 selects which layers get dumped.
// BRICKLAYERS_DUMP_LAYERS=all enables dumping for every layer (large).
// BRICKLAYERS_DUMP_DIR overrides the default dump directory
// (default ~/Library/Logs/BrickLayers on macOS, /tmp/bricklayers otherwise).
//
// For each (layer_nr, extruder_nr) pair, the plugin writes four files:
//   layer_NNN_extK_in.bin    — raw CallRequest protobuf bytes
//   layer_NNN_extK_in.jsonl  — one path per line, human-readable summary
//   layer_NNN_extK_out.bin   — raw CallResponse protobuf bytes
//   layer_NNN_extK_out.jsonl — same but for the plugin's output
// The .bin files are round-trippable as pytest fixtures:
//   req = modify_pb2.CallRequest()
//   req.ParseFromString(open('layer_148_ext0_in.bin', 'rb').read())
#[derive(Clone)]
struct DumpConfig {
    layers: HashSet<i64>,
    all_layers: bool,
    dir: PathBuf,
}

impl DumpConfig {
    fn from_env() -> Option<Self> {
        let layers_env = std::env::var("BRICKLAYERS_DUMP_LAYERS").ok()?;
        let trimmed = layers_env.trim();
        if trimmed.is_empty() {
            return None;
        }
        let (layers, all_layers) = if trimmed.eq_ignore_ascii_case("all") {
            (HashSet::new(), true)
        } else {
            let ls: HashSet<i64> = trimmed
                .split(',')
                .filter_map(|s| s.trim().parse::<i64>().ok())
                .collect();
            if ls.is_empty() {
                return None;
            }
            (ls, false)
        };
        let dir = std::env::var("BRICKLAYERS_DUMP_DIR")
            .map(PathBuf::from)
            .unwrap_or_else(|_| {
                if cfg!(target_os = "macos") {
                    dirs_home().join("Library/Logs/BrickLayers")
                } else {
                    PathBuf::from("/tmp/bricklayers")
                }
            });
        let _ = std::fs::create_dir_all(&dir);
        if all_layers {
            tracing::info!("DUMP: all layers enabled → {:?}", dir);
        } else {
            tracing::info!("DUMP: layers={:?} → {:?}", layers, dir);
        }
        Some(Self { layers, all_layers, dir })
    }
}

fn dirs_home() -> PathBuf {
    std::env::var("HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|_| PathBuf::from("/tmp"))
}

/// Should this layer be dumped?
fn dump_matches(cfg: &DumpConfig, layer_nr: i64) -> bool {
    cfg.all_layers || cfg.layers.contains(&layer_nr)
}

/// Dump a call's full input/output in TWO formats side by side:
///   * `.bin`  — raw protobuf serialization of the `CallRequest` /
///     `CallResponse` message. Round-trips losslessly — the exact bytes
///     Cura sent to us (or we sent back) can be loaded directly into a
///     pytest fixture via ``CallRequest().ParseFromString(bytes)``.
///   * `.jsonl` — one JSON per path, human-readable summary for grep/debug.
///
/// Both filenames share a common prefix so a given call produces e.g.:
///   layer_148_ext0_in.bin
///   layer_148_ext0_in.jsonl
///   layer_148_ext0_out.bin
///   layer_148_ext0_out.jsonl
fn dump_layer_request(
    cfg: &DumpConfig,
    layer_nr: i64,
    extruder_nr: i64,
    label: &str,
    req: &proto::gcode_paths::CallRequest,
) {
    if !dump_matches(cfg, layer_nr) {
        return;
    }
    let base = format!("layer_{}_ext{}_{}", layer_nr, extruder_nr, label);
    // Binary proto — the fixture-quality dump.
    let bin_path = cfg.dir.join(format!("{}.bin", base));
    let bytes = req.encode_to_vec();
    if let Err(e) = std::fs::write(&bin_path, &bytes) {
        warn!("DUMP: failed to write {:?}: {}", bin_path, e);
    } else {
        info!("DUMP: wrote {:?} ({} bytes, {} paths)",
              bin_path, bytes.len(), req.gcode_paths.len());
    }
    dump_paths_jsonl(cfg, &base, &req.gcode_paths);
}

fn dump_layer_response(
    cfg: &DumpConfig,
    layer_nr: i64,
    extruder_nr: i64,
    label: &str,
    resp: &proto::gcode_paths::CallResponse,
) {
    if !dump_matches(cfg, layer_nr) {
        return;
    }
    let base = format!("layer_{}_ext{}_{}", layer_nr, extruder_nr, label);
    let bin_path = cfg.dir.join(format!("{}.bin", base));
    let bytes = resp.encode_to_vec();
    if let Err(e) = std::fs::write(&bin_path, &bytes) {
        warn!("DUMP: failed to write {:?}: {}", bin_path, e);
    } else {
        info!("DUMP: wrote {:?} ({} bytes, {} paths)",
              bin_path, bytes.len(), resp.gcode_paths.len());
    }
    dump_paths_jsonl(cfg, &base, &resp.gcode_paths);
}

fn dump_paths_jsonl(cfg: &DumpConfig, base: &str, paths: &[proto::v0::GCodePath]) {
    let jsonl_path = cfg.dir.join(format!("{}.jsonl", base));
    let file = match std::fs::OpenOptions::new()
        .create(true)
        .truncate(true)
        .write(true)
        .open(&jsonl_path)
    {
        Ok(f) => f,
        Err(e) => {
            warn!("DUMP: failed to open {:?}: {}", jsonl_path, e);
            return;
        }
    };
    let mut w = std::io::BufWriter::new(file);
    for p in paths {
        let pts: Vec<(i64, i64, i64)> = p
            .path
            .as_ref()
            .map(|op| op.path.iter().map(|pt| (pt.x, pt.y, pt.z)).collect())
            .unwrap_or_default();
        let json = format!(
            "{{\"feature\":{},\"mesh\":{:?},\"z_offset\":{},\"flow\":{},\"flow_ratio\":{},\"line_width\":{},\"width_factor\":{},\"retract\":{},\"perform_z_hop\":{},\"layer_thickness\":{},\"speed_factor\":{},\"points\":{:?}}}\n",
            p.feature, p.mesh_name, p.z_offset,
            p.flow, p.flow_ratio, p.line_width, p.width_factor,
            p.retract, p.perform_z_hop, p.layer_thickness,
            p.speed_factor, pts,
        );
        if w.write_all(json.as_bytes()).is_err() {
            return;
        }
    }
}

// Generated protobuf modules
pub mod proto {
    pub mod v0 {
        tonic::include_proto!("cura.plugins.v0");
    }
    pub mod handshake {
        tonic::include_proto!("cura.plugins.slots.handshake.v0");
    }
    pub mod broadcast {
        tonic::include_proto!("cura.plugins.slots.broadcast.v0");
    }
    pub mod gcode_paths {
        tonic::include_proto!("cura.plugins.slots.gcode_paths.v0.modify");
    }
}

const PLUGIN_NAME: &str = "BrickLayers";
const PLUGIN_VERSION: &str = "1.0.0";
const SLOT_VERSION: &str = "0.1.0-alpha";

fn slot_metadata() -> tonic::metadata::MetadataMap {
    let mut meta = tonic::metadata::MetadataMap::new();
    meta.insert("cura-slot-version", MetadataValue::from_static(SLOT_VERSION));
    meta.insert("cura-plugin-name", MetadataValue::from_static(PLUGIN_NAME));
    meta.insert(
        "cura-plugin-version",
        MetadataValue::from_static(PLUGIN_VERSION),
    );
    meta
}

// ---------------------------------------------------------------------------
// Settings (shared between broadcast handler and modify handler)
// ---------------------------------------------------------------------------

#[derive(Debug, Clone)]
struct BrickSettings {
    enabled: bool,
    start_layer: i64,
    /// Raw end_layer value as broadcast by Cura:
    ///   >0  → last layer (1-indexed, inclusive) to apply bricks.
    ///   0, -1 → "apply to all layers" (legacy sentinel).
    ///   <-1 → python-style offset from the top: `-3` means "leave the top
    ///        3 layers plain". Resolved to an effective 1-indexed end
    ///        using `machine_height_um / layer_height` at modify time.
    end_layer_raw: i64,
    apply_inner_walls: bool,
    apply_outer_walls: bool,
    extrusion_multiplier: f64,
    layer_height: i64,
    inside_out: bool,
    /// XY distance (microns) above which consecutive wall paths are treated
    /// as belonging to different spatial contours, so each contour's local
    /// innermost wall is protected independently.
    contour_break_distance: i64,
    /// Build volume height (µm). Used only for resolving negative
    /// `end_layer_raw` values into a concrete layer index.
    machine_height_um: i64,
}

impl Default for BrickSettings {
    fn default() -> Self {
        Self {
            enabled: false,
            start_layer: 2,
            end_layer_raw: -1,
            apply_inner_walls: true,
            apply_outer_walls: false,
            extrusion_multiplier: 1.05,
            layer_height: 0,
            // Cura's fdmprinter default is "inside_out". If CuraEngine doesn't
            // broadcast inset_direction (or broadcasts only overrides), the
            // plugin needs a default that agrees with CuraEngine's actual
            // wall order for innermost-wall tiebreaking. The real safety
            // net is the geometry-based innermost detection in the WASM
            // module — this default is only the tiebreaker for walls with
            // identical bounding boxes.
            inside_out: true,
            contour_break_distance: 2000, // 2 mm — matches Cura setting default
            machine_height_um: 0,
        }
    }
}

impl BrickSettings {
    /// Resolve `end_layer_raw` into an effective 1-indexed last-inclusive
    /// layer. `-1` (= "no cap") is returned as the canonical sentinel when
    /// either the user requested "apply to all" or the negative offset
    /// cannot be resolved (machine_height / layer_height unknown).
    fn effective_end_layer(&self) -> i64 {
        if self.end_layer_raw > 0 {
            return self.end_layer_raw;
        }
        if self.end_layer_raw == 0 || self.end_layer_raw == -1 {
            return -1;
        }
        if self.layer_height > 0 && self.machine_height_um > 0 {
            let total_layers = self.machine_height_um / self.layer_height;
            (total_layers + self.end_layer_raw).max(1)
        } else {
            -1
        }
    }
}

/// Per-extruder & per-mesh settings resolution.
///
/// * `global` — baseline settings parsed from the broadcast's `global_settings`.
/// * `per_extruder[i]` — global overlaid with `extruder_settings[i]` overrides.
/// * `per_mesh[mesh_name]` — global overlaid with that mesh's `object_settings`
///   overrides. Keyed by the `mesh_name` value CuraEngine stores in each
///   object_settings map (newer Cura versions populate this; older versions
///   won't, in which case `per_mesh` stays empty and per-mesh Cura overrides
///   degrade silently to global).
/// * `explicitly_disabled_meshes` — set of `mesh_name` values whose
///   object_settings map contained an EXPLICIT `brick_layers_enabled = false`
///   entry. This is distinct from inheriting a false default via the global
///   stack: Cura's per-mesh container can hold resolved default values even
///   when the user never set them, so we can't treat
///   `per_mesh[...].enabled == false` as user intent. Only settings whose
///   keys physically appear in the broadcast's object_settings map were
///   explicitly toggled.
#[derive(Debug, Default)]
struct SettingsBundle {
    global: BrickSettings,
    per_extruder: Vec<BrickSettings>,
    per_mesh: std::collections::HashMap<String, BrickSettings>,
    explicitly_disabled_meshes: std::collections::HashSet<String>,
}

impl SettingsBundle {
    fn for_extruder(&self, extruder_nr: i64) -> &BrickSettings {
        if extruder_nr >= 0 {
            if let Some(s) = self.per_extruder.get(extruder_nr as usize) {
                return s;
            }
        }
        &self.global
    }
}

type SharedSettings = Arc<Mutex<SettingsBundle>>;

fn parse_settings(s: &mut BrickSettings, settings_map: &std::collections::HashMap<String, Vec<u8>>) {
    for (name, value_bytes) in settings_map {
        let val = String::from_utf8_lossy(value_bytes).trim().to_string();
        match name.as_str() {
            "brick_layers_enabled" => {
                s.enabled = matches!(val.as_str(), "true" | "1" | "True" | "yes");
            }
            "brick_layers_start_layer" => {
                if let Ok(v) = val.parse::<f64>() {
                    s.start_layer = (v as i64 - 1).max(0);
                }
            }
            "brick_layers_end_layer" => {
                if let Ok(v) = val.parse::<f64>() {
                    s.end_layer_raw = v as i64;
                }
            }
            "machine_height" => {
                if let Ok(v) = val.parse::<f64>() {
                    // Cura broadcasts machine_height in millimetres.
                    s.machine_height_um = (v * 1000.0).max(0.0) as i64;
                }
            }
            "brick_layers_apply_inner_walls" => {
                s.apply_inner_walls = matches!(val.as_str(), "true" | "1" | "True" | "yes");
            }
            "brick_layers_apply_outer_walls" => {
                s.apply_outer_walls = matches!(val.as_str(), "true" | "1" | "True" | "yes");
            }
            "brick_layers_extrusion_multiplier" => {
                if let Ok(v) = val.parse::<f64>() {
                    s.extrusion_multiplier = v;
                }
            }
            "layer_height" => {
                if let Ok(v) = val.parse::<f64>() {
                    s.layer_height = (v * 1000.0) as i64;
                }
            }
            "inset_direction" => {
                s.inside_out = val == "inside_out";
            }
            "brick_layers_contour_break_distance" => {
                if let Ok(v) = val.parse::<f64>() {
                    // Cura sends millimetres; plugin uses microns.
                    s.contour_break_distance = (v * 1000.0).max(0.0) as i64;
                }
            }
            _ => {}
        }
    }
}

// ---------------------------------------------------------------------------
// HandshakeService
// ---------------------------------------------------------------------------

struct HandshakeServiceImpl;

#[tonic::async_trait]
impl proto::handshake::handshake_service_server::HandshakeService for HandshakeServiceImpl {
    async fn call(
        &self,
        request: Request<proto::handshake::CallRequest>,
    ) -> Result<Response<proto::handshake::CallResponse>, Status> {
        let req = request.into_inner();
        info!(
            "Handshake: slot={} plugin={} version={}",
            req.slot_id, req.plugin_name, req.version,
        );
        let mut resp = Response::new(proto::handshake::CallResponse {
            slot_version_range: SLOT_VERSION.to_string(),
            plugin_name: PLUGIN_NAME.to_string(),
            plugin_version: PLUGIN_VERSION.to_string(),
            broadcast_subscriptions: vec![proto::v0::SlotId::SettingsBroadcast.into()],
        });
        *resp.metadata_mut() = slot_metadata();
        Ok(resp)
    }
}

// ---------------------------------------------------------------------------
// BroadcastService
// ---------------------------------------------------------------------------

struct BroadcastServiceImpl {
    settings: SharedSettings,
    wasm: Arc<Mutex<WasmRuntime>>,
    dump_config: Option<DumpConfig>,
}

#[tonic::async_trait]
impl proto::broadcast::broadcast_service_server::BroadcastService for BroadcastServiceImpl {
    async fn broadcast_settings(
        &self,
        request: Request<proto::broadcast::BroadcastServiceSettingsRequest>,
    ) -> Result<Response<()>, Status> {
        let req = request.into_inner();
        info!("Received settings broadcast");

        // Capture the raw broadcast bytes so tests can replay the exact
        // settings context a real slice produced. Written once per slice
        // (Cura re-sends on each slice request, so the latest file reflects
        // the most recent state).
        if let Some(cfg) = &self.dump_config {
            let bytes = req.encode_to_vec();
            let p = cfg.dir.join("broadcast_settings.bin");
            if let Err(e) = std::fs::write(&p, &bytes) {
                warn!("DUMP: failed to write {:?}: {}", p, e);
            } else {
                info!("DUMP: wrote {:?} ({} bytes)", p, bytes.len());
            }
        }

        let mut bundle = self.settings.lock().unwrap();

        // Start from a fresh default and apply global_settings.
        let mut global = BrickSettings::default();
        if let Some(g) = &req.global_settings {
            parse_settings(&mut global, &g.settings);
        }

        // Build per-extruder by starting from the freshly-parsed global and
        // overlaying each extruder_settings entry. Index corresponds to
        // extruder position (extruder_settings[0] = extruder 0, …).
        let mut per_extruder: Vec<BrickSettings> = Vec::with_capacity(req.extruder_settings.len());
        for ext in &req.extruder_settings {
            let mut s = global.clone();
            parse_settings(&mut s, &ext.settings);
            per_extruder.push(s);
        }

        // Parse per-mesh overrides. CuraEngine populates each
        // object_settings entry's map with `mesh_name -> <the mesh's name>`
        // alongside any user-set overrides, so we key the result by
        // mesh_name to make per-path resolution possible.
        //
        // CRITICAL: Cura's per-mesh container stores RESOLVED values for
        // every `settable_per_mesh` setting, which means the broadcast can
        // contain `brick_layers_enabled = false` for a mesh purely because
        // that's the global/stack default — not because the user toggled
        // it off for that model. We distinguish user intent from inherited
        // defaults by tracking which keys physically appear in each
        // object_settings map: only meshes whose map explicitly contains
        // `brick_layers_enabled = false` are added to the explicit-disable
        // set that drives the WASM passthrough filter.
        let mut per_mesh: std::collections::HashMap<String, BrickSettings> =
            std::collections::HashMap::new();
        let mut explicitly_disabled_meshes: std::collections::HashSet<String> =
            std::collections::HashSet::new();
        for obj in &req.object_settings {
            let mesh_name = obj
                .settings
                .get("mesh_name")
                .map(|v| String::from_utf8_lossy(v).trim().to_string())
                .unwrap_or_default();
            let mut s = global.clone();
            parse_settings(&mut s, &obj.settings);

            // Explicit per-mesh disable detection: the key physically appears
            // in the map AND its parsed value is a truthy "off" marker. If
            // the key is ABSENT we ignore the per-mesh disable regardless of
            // what inheritance would resolve to, because Cura sends default
            // values for every settable_per_mesh setting even when the user
            // never touched them.
            let explicit_off = obj
                .settings
                .get("brick_layers_enabled")
                .map(|v| {
                    let raw = String::from_utf8_lossy(v).trim().to_ascii_lowercase();
                    matches!(raw.as_str(), "false" | "0" | "no")
                })
                .unwrap_or(false);

            if mesh_name.is_empty() {
                info!(
                    "BrickSettings(object anon): enabled={} start={} end_raw={} \
                     apply_inner={} apply_outer={} multiplier={:.2} explicit_off={}",
                    s.enabled, s.start_layer, s.end_layer_raw,
                    s.apply_inner_walls, s.apply_outer_walls, s.extrusion_multiplier,
                    explicit_off,
                );
            } else {
                per_mesh.insert(mesh_name.clone(), s);
                if explicit_off {
                    explicitly_disabled_meshes.insert(mesh_name);
                }
            }
        }
        let object_overrides_count = req.object_settings.len();

        info!(
            "BrickSettings(global): enabled={} start={} end_raw={} inner={} outer={} \
             multiplier={:.2} layer_height={} inside_out={} contour_break={}µm \
             machine_height={}µm extruders={} object_overrides={}",
            global.enabled, global.start_layer, global.end_layer_raw,
            global.apply_inner_walls, global.apply_outer_walls,
            global.extrusion_multiplier, global.layer_height, global.inside_out,
            global.contour_break_distance, global.machine_height_um,
            per_extruder.len(), object_overrides_count,
        );
        for (i, s) in per_extruder.iter().enumerate() {
            info!(
                "BrickSettings(ext {}): enabled={} start={} end_raw={} (→eff={}) \
                 inner={} outer={} multiplier={:.2} contour_break={}µm",
                i, s.enabled, s.start_layer, s.end_layer_raw, s.effective_end_layer(),
                s.apply_inner_walls, s.apply_outer_walls,
                s.extrusion_multiplier, s.contour_break_distance,
            );
        }

        for (name, s) in &per_mesh {
            info!(
                "BrickSettings(mesh {:?}): enabled={} start={} end_raw={} \
                 apply_inner={} apply_outer={} multiplier={:.2} contour_break={}µm",
                name, s.enabled, s.start_layer, s.end_layer_raw,
                s.apply_inner_walls, s.apply_outer_walls, s.extrusion_multiplier,
                s.contour_break_distance,
            );
        }

        if !explicitly_disabled_meshes.is_empty() {
            info!(
                "BrickLayers: explicitly disabled per-mesh for {} mesh(es): {:?}",
                explicitly_disabled_meshes.len(),
                explicitly_disabled_meshes,
            );
        }

        bundle.global = global.clone();
        bundle.per_extruder = per_extruder;
        bundle.per_mesh = per_mesh;
        bundle.explicitly_disabled_meshes = explicitly_disabled_meshes;
        drop(bundle);

        // Seed the WASM side with the global settings so an initial
        // `enabled=false` passthrough decision in the modify handler matches
        // what the WASM module sees. The per-extruder settings are pushed
        // just-in-time on each modify call (the relevant extruder's
        // resolved BrickSettings).
        if let Ok(mut wasm) = self.wasm.lock() {
            wasm.set_settings(&global);
        }

        Ok(Response::new(()))
    }
}

// ---------------------------------------------------------------------------
// GCodePathsModifyService — delegates to WASM
// ---------------------------------------------------------------------------

struct GCodePathsModifyServiceImpl {
    settings: SharedSettings,
    wasm: Arc<Mutex<WasmRuntime>>,
    dump_config: Option<DumpConfig>,
}

#[tonic::async_trait]
impl proto::gcode_paths::g_code_paths_modify_service_server::GCodePathsModifyService
    for GCodePathsModifyServiceImpl
{
    async fn call(
        &self,
        request: Request<proto::gcode_paths::CallRequest>,
    ) -> Result<Response<proto::gcode_paths::CallResponse>, Status> {
        let req = request.into_inner();
        let layer_nr = req.layer_nr;
        let extruder_nr = req.extruder_nr;
        let path_count = req.gcode_paths.len();
        let input_z_shifted = req.gcode_paths.iter().filter(|p| p.z_offset != 0).count();

        info!(
            ">>> GCodePathsModify called: layer={} extruder={} paths={} (input_z_shifted={})",
            layer_nr, extruder_nr, path_count, input_z_shifted,
        );

        if let Some(cfg) = &self.dump_config {
            dump_layer_request(cfg, layer_nr, extruder_nr, "in", &req);
        }

        // Resolve per-extruder settings for this call and push them to WASM.
        // Also build the list of mesh_names whose per-mesh override disables
        // brick layers — WASM uses that list to passthrough matching walls.
        // `effective_end_layer()` collapses negative raw values into a concrete
        // 1-indexed end, using machine_height/layer_height from the broadcast.
        let (effective, disabled_meshes) = {
            let bundle = self.settings.lock().map_err(|e| {
                Status::internal(format!("settings lock poisoned: {}", e))
            })?;
            let resolved = bundle.for_extruder(extruder_nr).clone();
            // ONLY meshes whose per-mesh broadcast EXPLICITLY carried
            // brick_layers_enabled=false are disabled. Inheriting a false
            // default from the global stack does not count as user intent —
            // see the broadcast handler for the rationale.
            let disabled: Vec<String> = bundle
                .explicitly_disabled_meshes
                .iter()
                .cloned()
                .collect();
            debug!(
                "  settings(ext={}): layer_height={}µm enabled={} end_raw={} → eff={}",
                extruder_nr, resolved.layer_height, resolved.enabled,
                resolved.end_layer_raw, resolved.effective_end_layer(),
            );
            if !resolved.enabled {
                info!("  -> passthrough (disabled)");
                let mut r = Response::new(proto::gcode_paths::CallResponse {
                    gcode_paths: req.gcode_paths,
                });
                *r.metadata_mut() = slot_metadata();
                return Ok(r);
            }
            (resolved, disabled)
        };

        if !disabled_meshes.is_empty() {
            debug!(
                "  per-mesh disabled: {:?} → passthrough for those meshes",
                disabled_meshes
            );
        }

        // Serialize request to protobuf bytes
        let input_bytes = req.encode_to_vec();

        // Call WASM module on a blocking thread — wasmtime-wasi's sync
        // implementation uses block_on internally, which panics if called
        // from within a tokio async runtime.
        let wasm = self.wasm.clone();
        let output_bytes = match tokio::task::spawn_blocking(move || {
            let mut wasm = wasm.lock().map_err(|e| format!("wasm lock poisoned: {}", e))?;
            // Push the per-extruder resolved settings with the effective
            // end-layer substituted into `end_layer_raw` so the WASM side
            // sees a concrete 1-indexed value and its existing
            // `end_layer > 0 && layer_nr > end_layer - 1` gate works
            // unchanged.
            let mut wasm_settings = effective.clone();
            wasm_settings.end_layer_raw = effective.effective_end_layer();
            wasm.set_settings(&wasm_settings);
            // Per-mesh disabled list (new every call — replaces previous).
            wasm.set_disabled_meshes(&disabled_meshes);
            wasm.process_layer(&input_bytes).map_err(|e| e.to_string())
        })
        .await
        {
            Ok(Ok(bytes)) => bytes,
            Ok(Err(e)) => {
                warn!("  -> WASM error: {}, returning paths unchanged", e);
                let mut r = Response::new(proto::gcode_paths::CallResponse {
                    gcode_paths: req.gcode_paths,
                });
                *r.metadata_mut() = slot_metadata();
                return Ok(r);
            }
            Err(e) => {
                warn!("  -> WASM task failed: {}, returning paths unchanged", e);
                let mut r = Response::new(proto::gcode_paths::CallResponse {
                    gcode_paths: req.gcode_paths,
                });
                *r.metadata_mut() = slot_metadata();
                return Ok(r);
            }
        };

        // Deserialize response
        let response = match proto::gcode_paths::CallResponse::decode(output_bytes.as_slice()) {
            Ok(r) => r,
            Err(e) => {
                warn!("  -> protobuf decode error: {}, returning paths unchanged", e);
                let mut r = Response::new(proto::gcode_paths::CallResponse {
                    gcode_paths: req.gcode_paths,
                });
                *r.metadata_mut() = slot_metadata();
                return Ok(r);
            }
        };

        let shifted = response.gcode_paths.iter().filter(|p| p.z_offset != 0).count();
        info!(
            "  -> layer {}: WASM returned {} paths, {} z-shifted",
            layer_nr,
            response.gcode_paths.len(),
            shifted,
        );

        if let Some(cfg) = &self.dump_config {
            dump_layer_response(cfg, layer_nr, extruder_nr, "out", &response);
        }

        let mut r = Response::new(response);
        *r.metadata_mut() = slot_metadata();
        Ok(r)
    }
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

#[derive(Parser, Debug)]
#[command(name = "bricklayers_engine", about = "BrickLayers CuraEngine plugin")]
struct Cli {
    #[arg(long, default_value = "127.0.0.1")]
    address: String,

    /// gRPC listen port (required unless --warmup is set)
    #[arg(long, required_unless_present = "warmup")]
    port: Option<u16>,

    /// Pre-compile the WASM module to create the .cwasm cache, then exit.
    /// Called at plugin load time to eliminate startup delay on first run.
    #[arg(long)]
    warmup: bool,
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| tracing_subscriber::EnvFilter::new("info")),
        )
        .init();

    let cli = Cli::parse();

    // Warmup mode: compile WASM (creating .cwasm cache) then exit.
    // Called from Python at plugin load time so that the first real slice
    // starts the binary instantly (OS has already verified it and the
    // .cwasm cache avoids recompilation).
    if cli.warmup {
        let exe_dir = std::env::current_exe()?
            .parent()
            .unwrap_or(std::path::Path::new("."))
            .to_path_buf();
        let wasm_path = [
            exe_dir.join("bricklayers.wasm"),
            exe_dir.join("../bricklayers.wasm"),
            std::path::PathBuf::from("bricklayers.wasm"),
        ]
        .into_iter()
        .find(|p| p.exists())
        .ok_or("Could not find bricklayers.wasm")?;
        info!("BrickLayers warmup: compiling WASM from {:?}", wasm_path);
        WasmRuntime::new(&wasm_path)?;
        info!("BrickLayers warmup complete");
        return Ok(());
    }

    let port = cli.port.expect("--port is required without --warmup");
    let addr = format!("{}:{}", cli.address, port);

    // Bind the TCP listener FIRST so the port is open before CuraEngine
    // tries to connect. WASM compilation can take hundreds of milliseconds;
    // without early binding CuraEngine gets "Connection refused".
    let listener = tokio::net::TcpListener::bind(&addr).await?;
    info!("BrickLayers engine plugin listening on {}", addr);

    // Find the WASM module next to the executable
    let exe_dir = std::env::current_exe()?
        .parent()
        .unwrap_or(std::path::Path::new("."))
        .to_path_buf();

    let wasm_path = [
        exe_dir.join("bricklayers.wasm"),
        exe_dir.join("../bricklayers.wasm"),
        std::path::PathBuf::from("bricklayers.wasm"),
    ]
    .into_iter()
    .find(|p| p.exists())
    .ok_or("Could not find bricklayers.wasm")?;

    info!("Loading WASM module from {:?}", wasm_path);
    let wasm = Arc::new(Mutex::new(WasmRuntime::new(&wasm_path)?));

    let settings: SharedSettings = Arc::new(Mutex::new(SettingsBundle::default()));

    let dump_config = DumpConfig::from_env();
    if let Some(cfg) = &dump_config {
        info!(
            "DUMP: enabled for layers {:?} → {:?}",
            cfg.layers, cfg.dir
        );
    }

    let incoming = tokio_stream::wrappers::TcpListenerStream::new(listener);

    Server::builder()
        .add_service(
            proto::handshake::handshake_service_server::HandshakeServiceServer::new(
                HandshakeServiceImpl,
            ),
        )
        .add_service(
            proto::broadcast::broadcast_service_server::BroadcastServiceServer::new(
                BroadcastServiceImpl {
                    settings: settings.clone(),
                    wasm: wasm.clone(),
                    dump_config: dump_config.clone(),
                },
            ),
        )
        .add_service(
            proto::gcode_paths::g_code_paths_modify_service_server::GCodePathsModifyServiceServer::new(
                GCodePathsModifyServiceImpl {
                    settings,
                    wasm,
                    dump_config,
                },
            )
            .max_decoding_message_size(100 * 1024 * 1024)
            .max_encoding_message_size(100 * 1024 * 1024),
        )
        .serve_with_incoming(incoming)
        .await?;

    Ok(())
}
