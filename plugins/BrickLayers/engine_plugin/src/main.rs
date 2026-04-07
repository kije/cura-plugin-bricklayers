mod services;

use clap::Parser;
use tonic::transport::Server;
use tracing::info;

use services::{BroadcastServiceImpl, GCodePathsModifyServiceImpl, HandshakeServiceImpl};

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
        tonic::include_proto!("cura.plugins.slots.gcode_paths.v0");
    }
}

#[derive(Parser, Debug)]
#[command(name = "bricklayers_engine", about = "BrickLayers CuraEngine plugin")]
struct Cli {
    #[arg(long, default_value = "127.0.0.1")]
    address: String,

    #[arg(long)]
    port: u16,
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
    let addr = format!("{}:{}", cli.address, cli.port).parse()?;

    let settings = services::BrickSettings::default();

    info!("BrickLayers engine plugin listening on {}", addr);

    Server::builder()
        .add_service(
            proto::handshake::handshake_service_server::HandshakeServiceServer::new(
                HandshakeServiceImpl,
            ),
        )
        .add_service(
            proto::broadcast::broadcast_service_server::BroadcastServiceServer::new(
                BroadcastServiceImpl::new(settings.clone()),
            ),
        )
        .add_service(
            proto::gcode_paths::g_code_paths_modify_service_server::GCodePathsModifyServiceServer::new(
                GCodePathsModifyServiceImpl::new(settings),
            ),
        )
        .serve(addr)
        .await?;

    Ok(())
}
