# Copyright (c) 2024
# BrickLayers plugin is released under the terms of the LGPLv3 or higher.

import os
import platform
import subprocess
import stat
import sys
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
            Logger.log("d", "BrickLayers: Using Python prototype engine plugin: %s", prototype_path)
            return [sys.executable, prototype_path]

        Logger.log("w", "BrickLayers: No engine plugin executable found")
        return None

    def usePlugin(self) -> bool:
        """Only activate when brick_layers_enabled is true and an executable exists."""
        if self._plugin_command is None:
            return False

        stack = CuraApplication.getInstance().getGlobalContainerStack()
        if stack is None:
            return False

        return bool(stack.getProperty("brick_layers_enabled", "value"))
