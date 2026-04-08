use std::path::PathBuf;

fn main() -> Result<(), Box<dyn std::error::Error>> {
    // Proto files live in the parent directory (plugins/BrickLayers/proto/).
    // In local dev a symlink at engine_plugin/proto -> ../proto may exist,
    // but on Windows CI symlinks don't work, so resolve the real path.
    let manifest_dir = PathBuf::from(std::env::var("CARGO_MANIFEST_DIR")?);
    let proto_dir = manifest_dir.join("..").join("proto");

    let proto_dir = if proto_dir.exists() {
        proto_dir
    } else {
        // Fallback: local symlink or in-tree copy
        manifest_dir.join("proto")
    };

    let handshake = proto_dir.join("cura/plugins/slots/handshake/v0/handshake.proto");
    let broadcast = proto_dir.join("cura/plugins/slots/broadcast/v0/broadcast.proto");
    let gcode_paths = proto_dir.join("cura/plugins/slots/gcode_paths/v0/modify.proto");

    tonic_build::configure()
        .build_server(true)
        .build_client(false)
        .extern_path(".cura.plugins.v0", "crate::proto::v0")
        .compile_protos(
            &[&handshake, &broadcast, &gcode_paths],
            &[&proto_dir],
        )?;
    Ok(())
}
