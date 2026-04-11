{
  description = "BrickLayers CuraEngine plugin dev environment";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";
    rust-overlay = {
      url = "github:oxalica/rust-overlay";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, rust-overlay, flake-utils }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        overlays = [ (import rust-overlay) ];
        pkgs = import nixpkgs { inherit system overlays; };

        rustToolchain = pkgs.rust-bin.stable.latest.default.override {
          extensions = [ "rust-src" "rust-analyzer" "clippy" ];
          targets = [ "wasm32-wasip1" ];
        };

        python = pkgs.python311;
        pythonWithPkgs = python.withPackages (ps: with ps; [
          grpcio
          grpcio-tools
          pytest
        ]);
      in
      {
        devShells.default = pkgs.mkShell {
          packages = [
            # Rust
            rustToolchain

            # Python (prototype + tests)
            pythonWithPkgs

            # Protobuf
            pkgs.protobuf

            # Build tools
            pkgs.pkg-config
          ] ++ pkgs.lib.optionals pkgs.stdenv.isDarwin [
            pkgs.apple-sdk_15
            pkgs.libiconv
          ];

          PROTOC = "${pkgs.protobuf}/bin/protoc";

          shellHook = ''
            echo "BrickLayers dev environment"
            echo "  Rust:   $(rustc --version)"
            echo "  Python: $(python3 --version)"
            echo "  protoc: $(protoc --version)"
          '' + pkgs.lib.optionalString pkgs.stdenv.isDarwin ''
            # Override DEVELOPER_DIR after cc-wrapper setup hooks so it matches
            # the Rust toolchain's embedded SDK 15.5, preventing linker errors:
            # "Multiple conflicting values for DEVELOPER_DIR_arm64_apple_darwin"
            export DEVELOPER_DIR="${pkgs.apple-sdk_15}"
            export SDKROOT="${pkgs.apple-sdk_15}/Platforms/MacOSX.platform/Developer/SDKs/MacOSX.sdk"
          '';
        };
      });
}
