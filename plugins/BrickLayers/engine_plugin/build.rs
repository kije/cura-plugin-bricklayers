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

    // Step 1: Compile shared types (cura.plugins.v0 package).
    // These are included via tonic::include_proto!("cura.plugins.v0") in main.rs.
    tonic_build::configure()
        .build_server(false)
        .build_client(false)
        .compile_protos(
            &[
                proto_dir.join("cura/plugins/v0/slot_id.proto"),
                proto_dir.join("cura/plugins/v0/point3d.proto"),
                proto_dir.join("cura/plugins/v0/printfeatures.proto"),
                proto_dir.join("cura/plugins/v0/polygons.proto"),
                proto_dir.join("cura/plugins/v0/gcode_path.proto"),
            ],
            &[&proto_dir],
        )?;

    // Step 2: Compile slot service protos. These reference cura.plugins.v0
    // types which are mapped via extern_path to the module from step 1.
    tonic_build::configure()
        .build_server(true)
        .build_client(false)
        .extern_path(".cura.plugins.v0", "crate::proto::v0")
        .compile_protos(
            &[
                proto_dir.join("cura/plugins/slots/handshake/v0/handshake.proto"),
                proto_dir.join("cura/plugins/slots/broadcast/v0/broadcast.proto"),
                proto_dir.join("cura/plugins/slots/gcode_paths/v0/modify.proto"),
            ],
            &[&proto_dir],
        )?;

    Ok(())
}
