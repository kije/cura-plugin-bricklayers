use std::sync::{Arc, Mutex};

use tonic::{Request, Response, Status};
use tracing::{debug, info, warn};

use tonic::metadata::MetadataValue;

use crate::proto;

const PLUGIN_NAME: &str = "BrickLayers";
const PLUGIN_VERSION: &str = "1.0.0";
const SLOT_VERSION: &str = "0.1.0-alpha";

/// Create gRPC response metadata with the slot version header.
/// CuraEngine checks this to validate plugin compatibility.
fn slot_metadata() -> tonic::metadata::MetadataMap {
    let mut meta = tonic::metadata::MetadataMap::new();
    meta.insert("cura-slot-version", MetadataValue::from_static(SLOT_VERSION));
    meta.insert("cura-plugin-name", MetadataValue::from_static(PLUGIN_NAME));
    meta.insert("cura-plugin-version", MetadataValue::from_static(PLUGIN_VERSION));
    meta
}

// ---------------------------------------------------------------------------
// Settings
// ---------------------------------------------------------------------------

#[derive(Debug, Clone)]
pub struct BrickSettings {
    inner: Arc<Mutex<BrickSettingsInner>>,
}

#[derive(Debug)]
struct BrickSettingsInner {
    enabled: bool,
    start_layer: i64,     // 0-indexed
    end_layer: i64,       // -1 = all layers
    apply_inner_walls: bool,
    apply_outer_walls: bool,
    extrusion_multiplier: f64,
    layer_height: i64,    // microns
}

impl Default for BrickSettings {
    fn default() -> Self {
        Self {
            inner: Arc::new(Mutex::new(BrickSettingsInner {
                enabled: false,
                start_layer: 2,
                end_layer: -1,
                apply_inner_walls: true,
                apply_outer_walls: false,
                extrusion_multiplier: 1.05,
                layer_height: 0,
            })),
        }
    }
}

impl BrickSettings {
    fn get(&self) -> std::sync::MutexGuard<'_, BrickSettingsInner> {
        self.inner.lock().unwrap()
    }

    fn parse_settings(&self, settings: &proto::broadcast::Settings) {
        let mut s = self.inner.lock().unwrap();
        for (name, value_bytes) in &settings.settings {
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
                        s.end_layer = v as i64;
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
                        s.layer_height = (v * 1000.0) as i64; // mm to microns
                    }
                }
                _ => {}
            }
        }
    }
}

// ---------------------------------------------------------------------------
// HandshakeService
// ---------------------------------------------------------------------------

pub struct HandshakeServiceImpl;

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

pub struct BroadcastServiceImpl {
    settings: BrickSettings,
}

impl BroadcastServiceImpl {
    pub fn new(settings: BrickSettings) -> Self {
        Self { settings }
    }
}

#[tonic::async_trait]
impl proto::broadcast::broadcast_service_server::BroadcastService for BroadcastServiceImpl {
    async fn broadcast_settings(
        &self,
        request: Request<proto::broadcast::BroadcastServiceSettingsRequest>,
    ) -> Result<Response<()>, Status> {
        let req = request.into_inner();
        info!("Received settings broadcast");

        if let Some(global) = &req.global_settings {
            self.settings.parse_settings(global);
        }
        for ext in &req.extruder_settings {
            self.settings.parse_settings(ext);
        }

        let s = self.settings.get();
        info!(
            "BrickSettings: enabled={} start={} end={} inner={} outer={} \
             multiplier={:.2} layer_height={}",
            s.enabled, s.start_layer, s.end_layer,
            s.apply_inner_walls, s.apply_outer_walls,
            s.extrusion_multiplier, s.layer_height,
        );

        Ok(Response::new(()))
    }
}

// ---------------------------------------------------------------------------
// GCodePathsModifyService — core brick-layer algorithm
// ---------------------------------------------------------------------------

pub struct GCodePathsModifyServiceImpl {
    settings: BrickSettings,
}

impl GCodePathsModifyServiceImpl {
    pub fn new(settings: BrickSettings) -> Self {
        Self { settings }
    }
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
        let mut paths = req.gcode_paths;

        let s = self.settings.get();

        let passthrough = |p: Vec<proto::v0::GCodePath>| {
            let mut r = Response::new(proto::gcode_paths::CallResponse { gcode_paths: p });
            *r.metadata_mut() = slot_metadata();
            Ok(r)
        };

        if !s.enabled {
            return passthrough(paths);
        }

        // Check layer range
        if layer_nr < s.start_layer {
            return passthrough(paths);
        }
        if s.end_layer > 0 && layer_nr > s.end_layer - 1 {
            return passthrough(paths);
        }

        // Determine layer thickness
        let layer_thickness = if s.layer_height > 0 {
            s.layer_height
        } else {
            // Fallback: get from path data
            paths
                .iter()
                .find(|p| p.layer_thickness > 0)
                .map(|p| p.layer_thickness)
                .unwrap_or(0)
        };

        if layer_thickness <= 0 {
            warn!("Layer {}: no layer thickness available, skipping", layer_nr);
            return passthrough(paths);
        }

        let z_shift = layer_thickness / 2; // microns

        // Determine target features
        let target_inner = s.apply_inner_walls;
        let target_outer = s.apply_outer_walls;

        if !target_inner && !target_outer {
            return passthrough(paths);
        }

        // First/last brick layer multiplier adjustments
        let is_first_brick = layer_nr == s.start_layer;
        let is_last_brick = s.end_layer > 0 && layer_nr == s.end_layer - 1;
        let effective_multiplier = if is_first_brick {
            s.extrusion_multiplier * 1.15
        } else if is_last_brick {
            s.extrusion_multiplier * 0.85
        } else {
            s.extrusion_multiplier
        };

        // Drop the settings lock before modifying paths
        drop(s);

        // Shift alternating wall paths
        let mut wall_counter: u32 = 0;
        let mut modified_count: u32 = 0;

        for path in &mut paths {
            let feature = path.feature;
            let is_target = (target_inner && feature == proto::v0::PrintFeature::Innerwall as i32)
                || (target_outer && feature == proto::v0::PrintFeature::Outerwall as i32);

            if is_target {
                if wall_counter % 2 == 1 {
                    path.z_offset += z_shift;
                    path.flow_ratio *= effective_multiplier;
                    modified_count += 1;
                }
                wall_counter += 1;
            }
        }

        if modified_count > 0 {
            debug!(
                "Layer {}: shifted {}/{} wall paths (z_shift={} um, multiplier={:.3})",
                layer_nr, modified_count, wall_counter, z_shift, effective_multiplier,
            );
        }

        let mut resp = Response::new(proto::gcode_paths::CallResponse {
            gcode_paths: paths,
        });
        *resp.metadata_mut() = slot_metadata();
        Ok(resp)
    }
}
