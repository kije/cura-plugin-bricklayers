use std::path::PathBuf;

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let manifest_dir = PathBuf::from(std::env::var("CARGO_MANIFEST_DIR")?);
    let proto_dir = manifest_dir.join("..").join("..").join("proto");

    // Step 1: Compile shared types
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

    // Step 2: Compile slot services with extern_path
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
