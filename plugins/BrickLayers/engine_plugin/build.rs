fn main() -> Result<(), Box<dyn std::error::Error>> {
    tonic_build::configure()
        .build_server(true)
        .build_client(false)
        // Map the shared types package so slot protos can reference them
        .extern_path(".cura.plugins.v0", "crate::proto::v0")
        .compile_protos(
            &[
                "proto/cura/plugins/slots/handshake/v0/handshake.proto",
                "proto/cura/plugins/slots/broadcast/v0/broadcast.proto",
                "proto/cura/plugins/slots/gcode_paths/v0/modify.proto",
            ],
            &["proto"],
        )?;
    Ok(())
}
