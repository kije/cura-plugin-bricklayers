import os
import platform
import socket
import subprocess
import stat
import sys
import time
from typing import List, Optional

from UM.Logger import Logger
from UM.Platform import Platform

from cura.BackendPlugin import BackendPlugin
from cura.CuraApplication import CuraApplication


class BrickLayersEnginePlugin(BackendPlugin):
    """CuraEngine backend plugin for BrickLayers.

    Registers for the GCODE_PATHS_MODIFY slot (103) so the brick-layer
    wall shifting happens at the engine level, before G-code generation.
    The engine sends structured GCodePath data per layer; the plugin
    shifts alternating wall paths up by half a layer height and returns
    modified paths. The engine then handles extrusion, retraction, and
    travel from the modified paths.
    """

    GCODE_PATHS_MODIFY_SLOT = 103

    def __init__(self) -> None:
        super().__init__()
        self._supported_slots: List[int] = [self.GCODE_PATHS_MODIFY_SLOT]
        self._plugin_command = self._find_plugin_executable()
        self._warmup_engine()

    @staticmethod
    def _platform_subdir() -> str:
        """Return the platform-specific binary subdirectory name."""
        machine = platform.machine().lower()
        if machine in ("x86_64", "amd64"):
            arch = "x86_64"
        elif machine in ("aarch64", "arm64"):
            arch = "aarch64"
        else:
            arch = machine

        if Platform.isWindows():
            return f"windows-{arch}"
        elif Platform.isOSX():
            return f"macos-{arch}"
        else:
            return f"linux-{arch}"

    @staticmethod
    def _ensure_executable(path: str) -> None:
        """Ensure the binary is executable and not quarantined (macOS).

        On macOS, downloaded files get a com.apple.quarantine extended
        attribute that prevents execution. Remove it if present. Also
        ensure the execute permission bit is set.
        """
        # Ensure execute permission
        try:
            st = os.stat(path)
            if not (st.st_mode & stat.S_IXUSR):
                os.chmod(path, st.st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
                Logger.log("d", "BrickLayers: Set execute permission on %s", path)
        except OSError as e:
            Logger.log("w", "BrickLayers: Could not set permissions on %s: %s", path, e)

        # Remove macOS quarantine attribute
        if Platform.isOSX():
            try:
                subprocess.run(
                    ["xattr", "-d", "com.apple.quarantine", path],
                    capture_output=True, timeout=5,
                )
                Logger.log("d", "BrickLayers: Removed quarantine attribute from %s", path)
            except (subprocess.SubprocessError, FileNotFoundError):
                pass  # xattr not found or failed — not critical

    def _find_plugin_executable(self) -> Optional[List[str]]:
        """Locate the engine plugin executable.

        Checks for a compiled Rust binary in the platform-specific bin/
        subdirectory first, then a flat bin/ directory, then falls back
        to the Python prototype.
        """
        plugin_dir = os.path.dirname(os.path.abspath(__file__))

        if Platform.isWindows():
            binary_name = "bricklayers_engine.exe"
        else:
            binary_name = "bricklayers_engine"

        # Check platform-specific subdirectory (from CI package)
        platform_path = os.path.join(
            plugin_dir, "bin", self._platform_subdir(), binary_name
        )
        if os.path.isfile(platform_path):
            self._ensure_executable(platform_path)
            Logger.log("d", "BrickLayers: Using compiled engine plugin: %s", platform_path)
            return [platform_path]

        # Check flat bin/ directory (local dev build)
        flat_path = os.path.join(plugin_dir, "bin", binary_name)
        if os.path.isfile(flat_path):
            self._ensure_executable(flat_path)
            Logger.log("d", "BrickLayers: Using compiled engine plugin: %s", flat_path)
            return [flat_path]

        # Fall back to Python prototype
        prototype_path = os.path.join(plugin_dir, "engine_prototype.py")
        if os.path.isfile(prototype_path):
            python = self._find_python()
            Logger.log("d", "BrickLayers: Using Python prototype engine plugin: %s (python: %s)", prototype_path, python)
            return [python, prototype_path]

        Logger.log("w", "BrickLayers: No engine plugin executable found")
        return None

    @staticmethod
    def _find_python() -> str:
        """Return the best available Python 3 executable for the current environment.

        Priority order:
        1. /lsiopy/bin/python3  — linuxserver.io Docker images (VNC test container)
        2. sys.executable       — the interpreter Cura itself is running under
        3. shutil.which("python3") / shutil.which("python") — PATH fallback
        """
        import shutil

        candidates = [
            "/lsiopy/bin/python3",  # linuxserver.io venv (Docker / VNC container)
            sys.executable,         # Cura's own interpreter (macOS app bundle, pip install, etc.)
        ]
        for path in candidates:
            if path and os.path.isfile(path):
                return path

        # Last resort: search PATH
        for name in ("python3", "python"):
            found = shutil.which(name)
            if found:
                return found

        # Should never happen — sys.executable is always set
        return sys.executable

    def _warmup_engine(self) -> None:
        """Pre-compile the WASM module in the background at plugin load time.

        On macOS, the first execution of a newly installed binary is delayed by
        Gatekeeper verification. If the binary hasn't run yet when a slice starts,
        CuraEngine can connect to the port before the binary binds it, causing
        a RemoteException on Slot 103.

        Running with --warmup at load time (Cura startup) triggers OS verification
        and pre-creates the .cwasm compilation cache, so the binary starts instantly
        when the real slice begins.
        """
        if self._plugin_command is None:
            return
        # Only applies to the compiled binary; the Python prototype has no WASM cache.
        # Prototype commands are [python, script] (length >= 2); binary commands are [path] (length 1).
        if len(self._plugin_command) >= 2:
            return
        binary = self._plugin_command[0]
        try:
            subprocess.Popen(
                [binary, "--warmup"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            Logger.log("d", "BrickLayers: Warming up WASM cache in background")
        except OSError as e:
            Logger.log("w", "BrickLayers: Warmup launch failed: %s", e)

    def start(self) -> bool:
        """Launch the engine subprocess and wait until the gRPC port is ready.

        BackendPlugin.start() spawns the process and returns immediately.
        CuraEngine then tries to connect without delay. On slow starts (first
        run, large WASM recompile) the port may not be bound yet. Polling here
        keeps the Python side blocked until the engine is accepting connections,
        ensuring CuraEngine never sees "Connection refused".
        """
        if not super().start():
            return False
        port = self.getPort()
        if port:
            self._poll_port_ready("127.0.0.1", port, timeout=10.0)
        return True

    def _poll_port_ready(self, host: str, port: int, timeout: float) -> None:
        """Block until the TCP port accepts connections or the timeout expires."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                with socket.create_connection((host, port), timeout=0.5):
                    Logger.log("d", "BrickLayers: Engine ready on port %d", port)
                    return
            except OSError:
                time.sleep(0.1)
        Logger.log(
            "w",
            "BrickLayers: Engine did not bind port %d within %.0fs — proceeding anyway",
            port,
            timeout,
        )

    def usePlugin(self) -> bool:
        """Only activate when brick_layers_enabled is true and an executable exists.

        ``brick_layers_enabled`` is ``settable_per_extruder: true`` so the
        authoritative value lives on the extruder stacks, not the global
        stack. Cura's global-stack resolver returns the definition default
        (``false``) when a per-extruder setting isn't set on the global
        container, which previously caused the plugin to silently refuse
        to start whenever the user toggled bricks on via the per-extruder
        UI. Check every active extruder stack; any extruder with bricks
        enabled is enough reason to launch the plugin (CuraEngine will
        call modify per-extruder anyway and the plugin will passthrough
        the extruders that have it off).
        """
        if self._plugin_command is None:
            return False

        app = CuraApplication.getInstance()
        global_stack = app.getGlobalContainerStack()
        if global_stack is None:
            return False

        # Global-stack check — still the first and cheapest resolution path.
        if bool(global_stack.getProperty("brick_layers_enabled", "value")):
            return True

        # Per-extruder resolution: settable_per_extruder settings resolve on
        # extruder stacks. Iterate active extruders and return True if any
        # has bricks enabled.
        try:
            from cura.Settings.ExtruderManager import ExtruderManager
            ext_manager = ExtruderManager.getInstance()
            for ext_stack in ext_manager.getActiveExtruderStacks():
                if bool(ext_stack.getProperty("brick_layers_enabled", "value")):
                    return True
        except Exception as e:
            Logger.log("w", "BrickLayers: extruder enablement check failed: %s", e)

        return False
