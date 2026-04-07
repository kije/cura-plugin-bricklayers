# Copyright (c) 2024
# BrickLayers plugin is released under the terms of the LGPLv3 or higher.

import os
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

    def _find_plugin_executable(self) -> Optional[List[str]]:
        """Locate the engine plugin executable.

        Checks for a compiled Rust binary first, then falls back to the
        Python prototype if available.
        """
        plugin_dir = os.path.dirname(os.path.abspath(__file__))

        # Check for compiled Rust binary
        if Platform.isWindows():
            binary_name = "bricklayers_engine.exe"
        else:
            binary_name = "bricklayers_engine"

        binary_path = os.path.join(plugin_dir, "bin", binary_name)
        if os.path.isfile(binary_path):
            Logger.log("d", "BrickLayers: Using compiled engine plugin: %s", binary_path)
            return [binary_path]

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
