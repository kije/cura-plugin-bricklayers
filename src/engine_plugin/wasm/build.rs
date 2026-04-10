use std::path::PathBuf;

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let manifest_dir = PathBuf::from(std::env::var("CARGO_MANIFEST_DIR")?);
    // Proto files are two levels up: engine_plugin/../proto
    let proto_dir = manifest_dir.join("..").join("..").join("proto");

    prost_build::compile_protos(
        &[
            proto_dir.join("cura/plugins/v0/point3d.proto"),
            proto_dir.join("cura/plugins/v0/printfeatures.proto"),
            proto_dir.join("cura/plugins/v0/polygons.proto"),
            proto_dir.join("cura/plugins/v0/gcode_path.proto"),
        ],
        &[&proto_dir],
    )?;
    Ok(())
}
