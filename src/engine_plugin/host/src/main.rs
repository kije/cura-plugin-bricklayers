mod wasm_runtime;

use std::sync::{Arc, Mutex};

use clap::Parser;
use prost::Message;
use tonic::metadata::MetadataValue;
use tonic::transport::Server;
use tonic::{Request, Response, Status};
use tracing::{debug, info, warn};

use wasm_runtime::WasmRuntime;

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

#[derive(Debug, Clone, Default)]
struct BrickSettings {
    enabled: bool,
    start_layer: i64,
    end_layer: i64,
    apply_inner_walls: bool,
    apply_outer_walls: bool,
    extrusion_multiplier: f64,
    layer_height: i64,
    inside_out: bool,
}

type SharedSettings = Arc<Mutex<BrickSettings>>;

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
                    s.layer_height = (v * 1000.0) as i64;
                }
            }
            "inset_direction" => {
                s.inside_out = val == "inside_out";
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
}

#[tonic::async_trait]
impl proto::broadcast::broadcast_service_server::BroadcastService for BroadcastServiceImpl {
    async fn broadcast_settings(
        &self,
        request: Request<proto::broadcast::BroadcastServiceSettingsRequest>,
    ) -> Result<Response<()>, Status> {
        let req = request.into_inner();
        info!("Received settings broadcast");

        let mut s = self.settings.lock().unwrap();
        if let Some(global) = &req.global_settings {
            parse_settings(&mut s, &global.settings);
        }
        for ext in &req.extruder_settings {
            parse_settings(&mut s, &ext.settings);
        }

        info!(
            "BrickSettings: enabled={} start={} end={} inner={} outer={} \
             multiplier={:.2} layer_height={}",
            s.enabled, s.start_layer, s.end_layer, s.apply_inner_walls,
            s.apply_outer_walls, s.extrusion_multiplier, s.layer_height,
        );

        // Push settings to WASM module
        if let Ok(mut wasm) = self.wasm.lock() {
            wasm.set_settings(&s);
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
        let path_count = req.gcode_paths.len();

        info!(
            ">>> GCodePathsModify called: layer={} extruder={} paths={}",
            layer_nr, req.extruder_nr, path_count,
        );

        // Quick check: if disabled, skip WASM call entirely
        {
            let s = self.settings.lock().unwrap();
            debug!(
                "  settings: layer_height={}µm enabled={}",
                s.layer_height, s.enabled
            );
            if !s.enabled {
                info!("  -> passthrough (disabled)");
                let mut r = Response::new(proto::gcode_paths::CallResponse {
                    gcode_paths: req.gcode_paths,
                });
                *r.metadata_mut() = slot_metadata();
                return Ok(r);
            }
        }

        // Serialize request to protobuf bytes
        let input_bytes = req.encode_to_vec();

        // Call WASM module
        let output_bytes = {
            let mut wasm = self.wasm.lock().unwrap();
            match wasm.process_layer(&input_bytes) {
                Ok(bytes) => bytes,
                Err(e) => {
                    warn!("  -> WASM error: {}, returning paths unchanged", e);
                    let mut r = Response::new(proto::gcode_paths::CallResponse {
                        gcode_paths: req.gcode_paths,
                    });
                    *r.metadata_mut() = slot_metadata();
                    return Ok(r);
                }
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

    let settings: SharedSettings = Arc::new(Mutex::new(BrickSettings::default()));

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
                },
            ),
        )
        .add_service(
            proto::gcode_paths::g_code_paths_modify_service_server::GCodePathsModifyServiceServer::new(
                GCodePathsModifyServiceImpl {
                    settings,
                    wasm,
                },
            ),
        )
        .serve_with_incoming(incoming)
        .await?;

    Ok(())
}
