# Copyright (c) 2024
# BrickLayers plugin is released under the terms of the LGPLv3 or higher.
#
# BrickLayersView: a layer view that displays post-processed BrickLayers
# G-code toolpaths.  Subclasses SimulationView — all rendering, sliders,
# and UI are inherited.  The only difference is the data source: on
# activation we re-parse the post-processed gcode_dict through
# FlavorParser to get LayerData that reflects the brick-layer shifts.

import os.path

from UM.Application import Application
from UM.Event import Event
from UM.Logger import Logger
from UM.PluginRegistry import PluginRegistry
from UM.Scene.Iterator.DepthFirstIterator import DepthFirstIterator
from UM.i18n import i18nCatalog

from cura.CuraApplication import CuraApplication

import sys

# SimulationView is loaded by Cura's plugin system as a package.
# Import it from the already-loaded module to avoid path issues.
_sv_module = sys.modules.get("SimulationView.SimulationView")
if _sv_module is None:
    # Fallback: try direct import (works if plugins/ is on sys.path)
    from SimulationView.SimulationView import SimulationView
else:
    SimulationView = _sv_module.SimulationView

from .BrickLayersViewProxy import BrickLayersViewProxy

catalog = i18nCatalog("cura")

_BRICK_LAYERS_MARKER = ";BRICKLAYERS_PROCESSED"


class BrickLayersView(SimulationView):
    """Layer view that shows post-processed BrickLayers G-code.

    Inherits all rendering and UI from SimulationView.  On activation
    we swap the scene's LayerData with one parsed from the post-processed
    gcode_dict.  On deactivation we restore the original.
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)

        # Saved state for LayerData swap
        self._original_layer_data_decorator = None
        self._swap_target_node = None
        self._bl_layer_data_decorator = None
        self._data_swapped = False

        self._proxy = None

    def _onEngineCreated(self) -> None:
        """Load our own QML components instead of SimulationView's."""
        plugin_path = PluginRegistry.getInstance().getPluginPath(self.getPluginId())
        if plugin_path:
            view_dir = os.path.join(plugin_path, "view")
            self.addDisplayComponent("main", os.path.join(view_dir, "BrickLayersViewMainComponent.qml"))
            self.addDisplayComponent("menu", os.path.join(view_dir, "BrickLayersViewMenuComponent.qml"))
        else:
            Logger.log("e", "Unable to find the path for %s", self.getPluginId())

    def getProxy(self, engine, script_engine):
        if self._proxy is None:
            self._proxy = BrickLayersViewProxy(self)
        return self._proxy

    def event(self, event) -> bool:
        if event.type == Event.ViewActivateEvent:
            self._swapLayerData()
        elif event.type == Event.ViewDeactivateEvent:
            self._restoreLayerData()

        return super().event(event)

    # ------------------------------------------------------------------ #
    # LayerData swap logic
    # ------------------------------------------------------------------ #

    def _swapLayerData(self) -> None:
        """Replace the scene's LayerData with one parsed from post-processed G-code."""
        if self._data_swapped:
            return

        try:
            scene = self.getController().getScene()

            # Find the node that has LayerData (placed by ProcessSlicedLayersJob)
            target_node = None
            for node in DepthFirstIterator(scene.getRoot()):
                if node.callDecoration("getLayerData"):
                    target_node = node
                    break

            if target_node is None:
                Logger.log("d", "BrickLayersView: No LayerData node found — nothing to swap")
                return

            # Get post-processed G-code
            gcode_list = self._getProcessedGcode()
            if gcode_list is None:
                Logger.log("d", "BrickLayersView: No post-processed G-code available")
                return

            # Parse G-code into LayerData via FlavorParser
            new_layer_data = self._parseGcodeToLayerData(gcode_list)
            if new_layer_data is None:
                Logger.log("w", "BrickLayersView: Failed to parse G-code into LayerData")
                return

            # Save original decorator and swap
            from cura.LayerDataDecorator import LayerDataDecorator

            self._swap_target_node = target_node
            self._original_layer_data_decorator = target_node.getDecorator(LayerDataDecorator)

            new_decorator = LayerDataDecorator()
            new_decorator.setLayerData(new_layer_data)
            self._bl_layer_data_decorator = new_decorator

            # Remove old, add new
            if self._original_layer_data_decorator:
                target_node.removeDecorator(LayerDataDecorator)
            target_node.addDecorator(new_decorator)

            self._data_swapped = True
            Logger.log("d", "BrickLayersView: LayerData swapped to post-processed G-code")

        except Exception:
            Logger.logException("w", "BrickLayersView: Failed to swap LayerData")

    def _restoreLayerData(self) -> None:
        """Restore the original LayerData on the scene node."""
        if not self._data_swapped:
            return

        try:
            from cura.LayerDataDecorator import LayerDataDecorator

            if self._swap_target_node is not None and self._original_layer_data_decorator is not None:
                # Remove our decorator, restore original
                self._swap_target_node.removeDecorator(LayerDataDecorator)
                self._swap_target_node.addDecorator(self._original_layer_data_decorator)
                Logger.log("d", "BrickLayersView: Original LayerData restored")

            self._data_swapped = False
            self._swap_target_node = None
            self._original_layer_data_decorator = None
            self._bl_layer_data_decorator = None

        except Exception:
            Logger.logException("w", "BrickLayersView: Failed to restore LayerData")
            self._data_swapped = False

    def _getProcessedGcode(self):
        """Get post-processed G-code from gcode_dict.

        If BrickLayers hasn't processed it yet (no marker), run _execute() now.
        Returns the gcode_list or None.
        """
        scene = self.getController().getScene()
        gcode_dict = getattr(scene, "gcode_dict", None)
        if not gcode_dict:
            return None

        active_build_plate_id = (
            CuraApplication.getInstance().getMultiBuildPlateModel().activeBuildPlate
        )
        gcode_list = gcode_dict.get(active_build_plate_id)
        if not gcode_list:
            return None

        # Check if BrickLayers is enabled
        global_stack = CuraApplication.getInstance().getGlobalContainerStack()
        if not global_stack:
            return None
        if not global_stack.getProperty("brick_layers_enabled", "value"):
            return None

        # If not yet processed, process now
        if _BRICK_LAYERS_MARKER not in gcode_list[0]:
            brick_layers = PluginRegistry.getInstance().getPluginObject("BrickLayers")
            if brick_layers is None:
                Logger.log("w", "BrickLayersView: BrickLayers plugin not available")
                return None
            gcode_list = brick_layers._execute(gcode_list)
            gcode_list[0] += _BRICK_LAYERS_MARKER + "\n"
            gcode_dict[active_build_plate_id] = gcode_list
            setattr(scene, "gcode_dict", gcode_dict)

        return gcode_list

    def _parseGcodeToLayerData(self, gcode_list):
        """Parse G-code text into LayerData using GCodeReader/FlavorParser.

        Temporarily suppresses FlavorParser side effects:
        - Saves/restores gcode_dict (FlavorParser overwrites it)
        - Suppresses caution message
        - Restores backend state to Done
        - Disconnects CuraEngineBackend scene listener to prevent stopSlicing
        """
        gcode_reader = PluginRegistry.getInstance().getPluginObject("GCodeReader")
        if gcode_reader is None:
            Logger.log("w", "BrickLayersView: GCodeReader plugin not available")
            return None

        app = CuraApplication.getInstance()
        scene = app.getController().getScene()
        backend = app.getBackend()

        # Save state that FlavorParser will clobber
        saved_gcode_dict = getattr(scene, "gcode_dict", None)
        saved_show_caution = app.getPreferences().getValue("gcodereader/show_caution")

        # Disconnect backend scene listener to prevent stopSlicing
        backend_disconnected = False
        if backend is not None and hasattr(backend, "_onSceneChanged"):
            try:
                scene.sceneChanged.disconnect(backend._onSceneChanged)
                backend_disconnected = True
            except Exception:
                pass

        try:
            app.getPreferences().setValue("gcodereader/show_caution", False)

            gcode_stream = "\n".join(gcode_list)
            gcode_reader.preReadFromStream(gcode_stream)
            result_node = gcode_reader.readFromStream(gcode_stream, "bricklayers_preview")
        finally:
            # Restore all clobbered state
            if saved_gcode_dict is not None:
                scene.gcode_dict = saved_gcode_dict
            app.getPreferences().setValue("gcodereader/show_caution", saved_show_caution)
            if backend is not None:
                from UM.Backend.Backend import BackendState
                backend.setState(BackendState.Done)
            if backend_disconnected:
                scene.sceneChanged.connect(backend._onSceneChanged)

        if result_node is None:
            return None
        return result_node.callDecoration("getLayerData")
