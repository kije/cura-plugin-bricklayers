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

        # Darwin SDK env for flake apps (mirrors the devShell shellHook —
        # otherwise `nix run .#build` emits linker SDK mismatch errors).
        darwinSdkEnv = pkgs.lib.optionalString pkgs.stdenv.isDarwin ''
          export DEVELOPER_DIR="${pkgs.apple-sdk_15}"
          export SDKROOT="${pkgs.apple-sdk_15}/Platforms/MacOSX.platform/Developer/SDKs/MacOSX.sdk"
        '';

        # Walk up from $PWD to find flake.nix, then exec the repo-local script.
        # Using $PWD (not ${self}) so edits to scripts/*.sh take effect without
        # re-evaluating the flake, and cargo writes to the real ./target.
        mkScriptApp = { name, script, extraInputs ? [ ] }: {
          type = "app";
          program = "${pkgs.writeShellApplication {
            name = "bricklayers-${name}";
            runtimeInputs = extraInputs;
            text = ''
              ${darwinSdkEnv}
              dir="$PWD"
              while [ ! -f "$dir/flake.nix" ]; do
                if [ "$dir" = "/" ]; then
                  echo "error: no flake.nix found from $PWD up to /" >&2
                  echo "Run from a directory inside the BrickLayers repo." >&2
                  exit 1
                fi
                dir="$(dirname "$dir")"
              done
              exec bash "$dir/scripts/${script}" "$@"
            '';
          }}/bin/bricklayers-${name}";
        };
      in
      {
        apps.build = mkScriptApp {
          name = "build";
          script = "build.sh";
          extraInputs = [
            rustToolchain
            pkgs.protobuf
            pkgs.pkg-config
          ] ++ pkgs.lib.optionals pkgs.stdenv.isDarwin [ pkgs.libiconv ];
        };

        apps.deploy = mkScriptApp {
          name = "deploy";
          script = "deploy.sh";
          # deploy.sh uses pure coreutils (cp/mkdir/chmod/find/rm); on Darwin,
          # xattr is at /usr/bin/xattr — writeShellApplication preserves $PATH
          # so system tools remain accessible.
        };

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
