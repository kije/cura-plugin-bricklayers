# Copyright (c) 2024
# BrickLayers plugin is released under the terms of the LGPLv3 or higher.
# Inspired by TengerTechnologies/Bricklayers and GeekDetour/BrickLayers.
#
# Shifts alternating perimeter wall loops up by half a layer height,
# creating interlocking brick-like walls for dramatically stronger prints.
#
# Settings are injected into Cura's definition containers at runtime
# (same pattern as ArcWelder plugin) so they appear in the sidebar.

from collections import OrderedDict
import json
import os
import re
from typing import Dict, List, Any, Optional, Tuple

from UM.Application import Application
from UM.Extension import Extension
from UM.Job import Job
from UM.Logger import Logger
from UM.Message import Message
from UM.PluginRegistry import PluginRegistry
from UM.Settings.SettingDefinition import SettingDefinition
from UM.Settings.DefinitionContainer import DefinitionContainer
from UM.Settings.ContainerRegistry import ContainerRegistry
from UM.i18n import i18nCatalog

from cura.CuraApplication import CuraApplication

_BRICK_LAYERS_MARKER = ";BRICKLAYERS_PROCESSED"


class BrickLayersPreviewJob(Job):
    """Background job that runs BrickLayers G-code transformation and
    re-parses the result into LayerData for the SimulationView preview.

    This allows the preview to show the final post-processed G-code
    without blocking the UI thread.
    """

    def __init__(self, brick_layers_plugin: "BrickLayers",
                 gcode_list: List[str], target_node) -> None:
        super().__init__()
        self._plugin = brick_layers_plugin
        self._gcode_list = list(gcode_list)  # copy to avoid mutation
        self._target_node = target_node
        self._abort = False
        self._modified_gcode: Optional[List[str]] = None
        self._new_layer_data = None
        self._progress_message = Message(
            "BrickLayers: Processing preview...",
            lifetime=0, dismissable=False, progress=0,
            title="BrickLayers"
        )

    def abort(self) -> None:
        self._abort = True

    def run(self) -> None:
        self._progress_message.show()
        try:
            # Phase 1: Run G-code transformation
            self._progress_message.setProgress(10)
            self._modified_gcode = self._plugin._execute(self._gcode_list)
            if self._abort:
                return

            # Phase 2: Re-parse via GCodeReader into LayerData
            self._progress_message.setProgress(70)
            self._new_layer_data = self._reparse_gcode(self._modified_gcode)
            if self._abort:
                return

            self._progress_message.setProgress(100)
        except Exception:
            Logger.logException("w", "BrickLayers: Preview job failed")
        finally:
            self._progress_message.hide()

    def _reparse_gcode(self, gcode_list: List[str]):
        """Parse modified G-code into LayerData using FlavorParser directly.

        FlavorParser.processGCodeStream() has several side effects designed
        for loading .gcode files that are destructive when used for preview
        refresh: it overwrites scene.gcode_dict, shows a caution message,
        and sets backend state to Disabled (which resets the "sliced" state
        and shows the Slice button again).

        We work around this by saving and restoring the state that
        FlavorParser clobbers.
        """
        gcode_reader = PluginRegistry.getInstance().getPluginObject("GCodeReader")
        if gcode_reader is None:
            Logger.log("w", "BrickLayers: GCodeReader plugin not available")
            return None

        app = CuraApplication.getInstance()
        scene = app.getController().getScene()
        backend = app.getBackend()

        # Save state that FlavorParser will clobber
        saved_gcode_dict = getattr(scene, "gcode_dict", None)
        saved_backend_state = backend.getState() if backend else None
        saved_show_caution = app.getPreferences().getValue(
            "gcodereader/show_caution"
        )

        try:
            # Suppress the "G-code Details" caution message
            app.getPreferences().setValue("gcodereader/show_caution", False)

            gcode_stream = "\n".join(gcode_list)
            gcode_reader.preReadFromStream(gcode_stream)
            result_node = gcode_reader.readFromStream(
                gcode_stream, "bricklayers_preview"
            )
        finally:
            # Restore all clobbered state
            if saved_gcode_dict is not None:
                scene.gcode_dict = saved_gcode_dict
            if backend is not None and saved_backend_state is not None:
                backend.setState(saved_backend_state)
            app.getPreferences().setValue(
                "gcodereader/show_caution", saved_show_caution
            )

        if result_node is None:
            return None
        return result_node.callDecoration("getLayerData")


class PerimeterLoop:
    """Stores GCode lines belonging to a single perimeter loop.

    Lines are separated into prefix (retract/travel/unretract before extrusion)
    and body (extrusion moves and any mid-loop non-extrusion moves).
    """

    def __init__(self, loop_type: str) -> None:
        self.loop_type = loop_type
        self.prefix_lines: List[str] = []
        self.body_lines: List[str] = []
        self.has_extrusion: bool = False
        self._first_extrusion_seen: bool = False
        self.start_x: Optional[float] = None
        self.start_y: Optional[float] = None
        self.end_x: Optional[float] = None
        self.end_y: Optional[float] = None
        # Travel position: where the nozzle IS before the first extrusion
        # (from the last G0 in the prefix). This is the correct start
        # position for deferred loops — NOT start_x/y which is the
        # DESTINATION of the first extrusion move.
        self.travel_x: Optional[float] = None
        self.travel_y: Optional[float] = None

    def add_line(self, line: str, x: Optional[float], y: Optional[float],
                 is_extrusion: bool) -> None:
        if is_extrusion:
            if not self._first_extrusion_seen:
                self._first_extrusion_seen = True
                if x is not None:
                    self.start_x = x
                if y is not None:
                    self.start_y = y
            if x is not None:
                self.end_x = x
            if y is not None:
                self.end_y = y
            self.has_extrusion = True
            self.body_lines.append(line)
        elif self._first_extrusion_seen:
            self.body_lines.append(line)
        else:
            self.prefix_lines.append(line)
            # Track G0 travel position in prefix
            stripped = line.strip()
            if stripped.startswith("G0 "):
                if x is not None:
                    self.travel_x = x
                if y is not None:
                    self.travel_y = y


class BrickLayers(Extension):

    def __init__(self) -> None:
        super().__init__()

        self._application = Application.getInstance()

        # Load settings definition from JSON
        settings_definition_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "brick_layers_settings.def.json"
        )
        try:
            with open(settings_definition_path, "r", encoding="utf-8") as f:
                self._settings_dict = json.load(
                    f, object_pairs_hook=OrderedDict
                )["settings"]
        except Exception:
            Logger.logException("e", "Could not load brick layers settings definition")
            return

        self._application.getPreferences().addPreference(
            "bricklayers/settings_made_visible", False
        )

        ContainerRegistry.getInstance().containerLoadComplete.connect(
            self._onContainerLoadComplete
        )
        self._application.getOutputDeviceManager().writeStarted.connect(
            self._onWriteStarted
        )

        # Preview pipeline: process G-code after slicing and update preview
        self._preview_job: Optional[BrickLayersPreviewJob] = None
        self._application.callLater(self._connectSceneSignals)

    # ------------------------------------------------------------------ #
    # Preview pipeline: scene signal connections
    # ------------------------------------------------------------------ #

    def _connectSceneSignals(self) -> None:
        """Deferred connection — scene may not exist at __init__ time."""
        try:
            scene = self._application.getController().getScene()
            scene.getRoot().childrenChanged.connect(
                self._onSceneChildrenChanged
            )

            backend = self._application.getBackend()
            if backend:
                backend.backendStateChange.connect(
                    self._onBackendStateChange
                )
        except Exception:
            Logger.logException(
                "w", "BrickLayers: Failed to connect scene signals"
            )

    # ------------------------------------------------------------------ #
    # Settings injection (same pattern as ArcWelder plugin)
    # ------------------------------------------------------------------ #

    def _onContainerLoadComplete(self, container_id: str) -> None:
        if not ContainerRegistry.getInstance().isLoaded(container_id):
            return

        try:
            container = ContainerRegistry.getInstance().findContainers(
                id=container_id
            )[0]
        except IndexError:
            return

        if not isinstance(container, DefinitionContainer):
            return
        if container.getMetaDataEntry("type") == "extruder":
            return

        # Add settings under the "experimental" category
        try:
            category = container.findDefinitions(key="experimental")[0]
        except IndexError:
            Logger.log("e", "BrickLayers: Could not find 'experimental' category")
            return

        for setting_key in self._settings_dict.keys():
            # M1 fix: skip if this setting already exists in the container
            if setting_key in container._definition_cache:
                continue

            setting_definition = SettingDefinition(
                setting_key, container, category, None
            )
            setting_definition.deserialize(self._settings_dict[setting_key])

            category._children.append(setting_definition)
            container._definition_cache[setting_key] = setting_definition

            self._expanded_categories = self._application.expandedCategories.copy()
            self._updateAddedChildren(container, setting_definition)
            self._application.setExpandedCategories(self._expanded_categories)
            self._expanded_categories.clear()
            container._updateRelations(setting_definition)

        # Make settings visible in the sidebar (only once)
        preferences = self._application.getPreferences()
        if not preferences.getValue("bricklayers/settings_made_visible"):
            setting_keys = self._getAllSettingKeys(self._settings_dict)

            visible_settings = preferences.getValue("general/visible_settings")
            visible_settings_changed = False
            for key in setting_keys:
                if key not in visible_settings:
                    visible_settings += ";%s" % key
                    visible_settings_changed = True

            if visible_settings_changed:
                preferences.setValue("general/visible_settings", visible_settings)

            preferences.setValue("bricklayers/settings_made_visible", True)

    def _updateAddedChildren(
        self, container: DefinitionContainer, setting_definition: SettingDefinition
    ) -> None:
        children = setting_definition.children
        if not children or not setting_definition.parent:
            return

        if setting_definition.parent.key in self._expanded_categories:
            self._expanded_categories.append(setting_definition.key)

        for child in children:
            container._definition_cache[child.key] = child
            self._updateAddedChildren(container, child)

    def _getAllSettingKeys(self, definition: Dict[str, Any]) -> List[str]:
        children = []
        for key in definition:
            children.append(key)
            if "children" in definition[key]:
                children.extend(self._getAllSettingKeys(definition[key]["children"]))
        return children

    # ------------------------------------------------------------------ #
    # Preview pipeline: scene change detection and job management
    # ------------------------------------------------------------------ #

    def _onSceneChildrenChanged(self, node) -> None:
        """Called when scene children change.

        After slicing, ProcessSlicedLayersJob attaches a node with LayerData
        to the scene, which fires this signal.  At that point gcode_dict is
        already populated, so we can run BrickLayers and update the preview.
        """
        global_stack = CuraApplication.getInstance().getGlobalContainerStack()
        if not global_stack:
            return
        if not global_stack.getProperty("brick_layers_enabled", "value"):
            return

        scene = self._application.getController().getScene()
        if not hasattr(scene, "gcode_dict"):
            return
        gcode_dict = getattr(scene, "gcode_dict")
        if not gcode_dict:
            return

        active_build_plate_id = (
            CuraApplication.getInstance()
            .getMultiBuildPlateModel()
            .activeBuildPlate
        )
        gcode_list = gcode_dict.get(active_build_plate_id)
        if not gcode_list:
            return

        # Already processed?
        if _BRICK_LAYERS_MARKER in gcode_list[0]:
            return

        # Find the node with LayerData (added by ProcessSlicedLayersJob)
        from UM.Scene.Iterator.DepthFirstIterator import DepthFirstIterator

        target_node = None
        for n in DepthFirstIterator(scene.getRoot()):
            if n.callDecoration("getLayerData"):
                target_node = n
                break
        if target_node is None:
            return

        # Abort any running preview job
        if self._preview_job is not None and self._preview_job.isRunning():
            self._preview_job.abort()
            self._preview_job = None

        # Launch background job
        self._preview_job = BrickLayersPreviewJob(
            self, gcode_list, target_node
        )
        self._preview_job.finished.connect(self._onPreviewJobFinished)
        self._preview_job.start()

    def _onPreviewJobFinished(self, job: BrickLayersPreviewJob) -> None:
        """Called on the main thread when the preview job completes."""
        if job is not self._preview_job:
            return  # stale job

        if (
            self._preview_job._modified_gcode is None
            or self._preview_job._new_layer_data is None
        ):
            Logger.log("w", "BrickLayers: Preview job produced no layer data")
            self._preview_job = None
            return

        # Replace LayerData on the scene node
        from cura.LayerDataDecorator import LayerDataDecorator

        target_node = job._target_node
        old_decorator = target_node.getDecorator(LayerDataDecorator)
        if old_decorator:
            old_decorator.setLayerData(job._new_layer_data)
        else:
            decorator = LayerDataDecorator()
            decorator.setLayerData(job._new_layer_data)
            target_node.addDecorator(decorator)

        # Update gcode_dict so writeStarted finds already-processed data
        scene = self._application.getController().getScene()
        gcode_dict = getattr(scene, "gcode_dict", {})
        active_build_plate_id = (
            CuraApplication.getInstance()
            .getMultiBuildPlateModel()
            .activeBuildPlate
        )
        modified_gcode = job._modified_gcode
        modified_gcode[0] += _BRICK_LAYERS_MARKER + "\n"
        gcode_dict[active_build_plate_id] = modified_gcode
        setattr(scene, "gcode_dict", gcode_dict)

        # Notify SimulationView to recalculate layers/paths
        view = self._application.getController().getActiveView()
        if hasattr(view, "resetLayerData"):
            view.resetLayerData()

        Logger.log("d", "BrickLayers: Preview updated with post-processed G-code")
        self._preview_job = None

    def _onBackendStateChange(self, state) -> None:
        """Abort preview job if a new slice starts."""
        from UM.Backend.Backend import BackendState

        if state in (BackendState.NotStarted, BackendState.Processing):
            if self._preview_job is not None and self._preview_job.isRunning():
                self._preview_job.abort()
                self._preview_job = None

    # ------------------------------------------------------------------ #
    # GCode pipeline hook (fallback for immediate save before preview)
    # ------------------------------------------------------------------ #

    def _onWriteStarted(self, output_device) -> None:
        """Fallback: if the preview job hasn't processed gcode_dict yet
        (e.g. user clicked Save immediately after slicing), process
        synchronously now.  If the preview job already ran, the marker
        is present and we skip."""
        global_stack = CuraApplication.getInstance().getGlobalContainerStack()
        if not global_stack:
            return

        if not global_stack.getProperty("brick_layers_enabled", "value"):
            return

        scene = Application.getInstance().getController().getScene()
        if not hasattr(scene, "gcode_dict"):
            return
        gcode_dict = getattr(scene, "gcode_dict")
        if not gcode_dict:
            return

        active_build_plate_id = CuraApplication.getInstance().getMultiBuildPlateModel().activeBuildPlate
        gcode_list = gcode_dict[active_build_plate_id]
        if not gcode_list:
            return

        # Already processed by preview job — nothing to do
        if _BRICK_LAYERS_MARKER in gcode_list[0]:
            return

        # Synchronous fallback processing
        gcode_list = self._execute(gcode_list)
        gcode_list[0] += _BRICK_LAYERS_MARKER + "\n"
        gcode_dict[active_build_plate_id] = gcode_list
        setattr(scene, "gcode_dict", gcode_dict)

        self._refreshPreviewLayerData(scene, gcode_list)

    def _refreshPreviewLayerData(self, scene, gcode_list: List[str]) -> None:
        """Attempt to refresh the preview layer data with the modified G-code.

        This is best-effort only. We avoid emitting sceneChanged to prevent
        triggering a re-slice loop. The preview will update on the next
        natural view refresh.
        """
        if getattr(self, "_refreshing_preview", False):
            return
        self._refreshing_preview = True
        try:
            from cura.LayerDataDecorator import LayerDataDecorator
            from UM.Scene.Iterator.DepthFirstIterator import DepthFirstIterator

            target_node = None
            for node in DepthFirstIterator(scene.getRoot()):
                if node.callDecoration("getLayerData"):
                    target_node = node
                    break

            if target_node is None:
                return

            gcode_reader = PluginRegistry.getInstance().getPluginObject("GCodeReader")
            if gcode_reader is None:
                return

            app = CuraApplication.getInstance()
            backend = app.getBackend()

            # Save state that FlavorParser will clobber
            saved_gcode_dict = getattr(scene, "gcode_dict", None)
            saved_backend_state = backend.getState() if backend else None
            saved_show_caution = app.getPreferences().getValue(
                "gcodereader/show_caution"
            )

            try:
                # Suppress the "G-code Details" caution message
                app.getPreferences().setValue("gcodereader/show_caution", False)

                gcode_stream = "\n".join(gcode_list)
                gcode_reader.preReadFromStream(gcode_stream)
                result_node = gcode_reader.readFromStream(
                    gcode_stream, "bricklayers_preview"
                )
            finally:
                # Restore all clobbered state
                if saved_gcode_dict is not None:
                    scene.gcode_dict = saved_gcode_dict
                if backend is not None and saved_backend_state is not None:
                    backend.setState(saved_backend_state)
                app.getPreferences().setValue(
                    "gcodereader/show_caution", saved_show_caution
                )

            if result_node is None:
                return

            new_layer_data = result_node.callDecoration("getLayerData")
            if new_layer_data is None:
                return

            old_decorator = target_node.getDecorator(LayerDataDecorator)
            if old_decorator:
                old_decorator.setLayerData(new_layer_data)
            else:
                decorator = LayerDataDecorator()
                decorator.setLayerData(new_layer_data)
                target_node.addDecorator(decorator)

            view = Application.getInstance().getController().getActiveView()
            if hasattr(view, "resetLayerData"):
                view.resetLayerData()

            # NOTE: Do NOT emit scene.sceneChanged here as it can trigger
            # a re-slice loop. The preview will update on the next view refresh.
            Logger.log("d", "BrickLayers: Preview layer data refreshed")

        except Exception:
            Logger.logException("w", "BrickLayers: Failed to refresh preview layer data")
        finally:
            self._refreshing_preview = False

    # ------------------------------------------------------------------ #
    # GCode transformation logic
    # ------------------------------------------------------------------ #

    def _execute(self, data: List[str]) -> List[str]:
        global_stack = CuraApplication.getInstance().getGlobalContainerStack()
        if global_stack is None:
            return data

        layer_height = float(global_stack.getProperty("layer_height", "value"))
        extrusion_multiplier = float(global_stack.getProperty(
            "brick_layers_extrusion_multiplier", "value"))
        start_layer = int(global_stack.getProperty(
            "brick_layers_start_layer", "value"))
        end_layer = int(global_stack.getProperty(
            "brick_layers_end_layer", "value"))
        apply_inner = bool(global_stack.getProperty(
            "brick_layers_apply_inner_walls", "value"))
        apply_outer = bool(global_stack.getProperty(
            "brick_layers_apply_outer_walls", "value"))

        z_shift = layer_height / 2.0

        target_types = set()
        if apply_inner:
            target_types.add("WALL-INNER")
        if apply_outer:
            target_types.add("WALL-OUTER")

        if not target_types:
            Logger.log("w", "BrickLayers: No wall types selected, nothing to do.")
            return data

        relative_extrusion, retract_length, retract_speed = \
            self._detect_gcode_params(data, global_stack)

        # M5 fix: use Cura's travel speed setting instead of heuristic detection
        travel_speed_mm_s = global_stack.getProperty("speed_travel", "value")
        if travel_speed_mm_s is not None:
            travel_speed = float(travel_speed_mm_s) * 60.0  # convert mm/s to mm/min
        else:
            travel_speed = 9000.0  # fallback

        max_layer_num = self._find_max_layer(data)
        # M4 fix: treat end_layer <= 0 (except -1) as "all layers"
        if end_layer <= 0:
            end_layer_gcode = max_layer_num
        else:
            end_layer_gcode = end_layer - 1

        start_layer_gcode = start_layer - 1
        layers_modified = 0

        # For absolute extrusion mode: track TWO E positions across layers:
        # - original_e: tracks slicer's original E values (for converting
        #   input layers from absolute to relative)
        # - output_e: tracks actual output E values (for converting
        #   processed output from relative back to absolute)
        # These diverge when the extrusion multiplier != 1.0 or when
        # extra retracts/unretracts are added by BrickLayers.
        original_e = 0.0
        output_e = 0.0
        if not relative_extrusion:
            # Scan data blocks before the first processable layer to find
            # the E position at the start of processing.
            for index in range(len(data)):
                layer_num = self._get_layer_number(data[index])
                if layer_num is not None and layer_num >= start_layer_gcode:
                    break
                for line in data[index].split("\n"):
                    stripped = line.strip()
                    if stripped.startswith("G1 "):
                        e = self._getValue(stripped, "E")
                        if e is not None:
                            original_e = float(e)
            output_e = original_e

        for index in range(len(data)):
            layer_gcode = data[index]

            layer_num = self._get_layer_number(layer_gcode)
            if layer_num is None or layer_num < 0:
                # Non-layer block: track E for absolute mode
                if not relative_extrusion:
                    for line in layer_gcode.split("\n"):
                        stripped = line.strip()
                        if stripped.startswith("G1 "):
                            e = self._getValue(stripped, "E")
                            if e is not None:
                                original_e = float(e)
                                output_e = float(e)
                continue
            if layer_num < start_layer_gcode or layer_num > end_layer_gcode:
                # Non-processed layer: track E for absolute mode
                if not relative_extrusion:
                    for line in layer_gcode.split("\n"):
                        stripped = line.strip()
                        if stripped.startswith("G1 "):
                            e = self._getValue(stripped, "E")
                            if e is not None:
                                original_e = float(e)
                                output_e = float(e)
                continue

            # For absolute mode: find the original end E BEFORE modifying
            original_layer_end_e = original_e
            if not relative_extrusion:
                for line in layer_gcode.split("\n"):
                    stripped = line.strip()
                    if stripped.startswith("G1 "):
                        e = self._getValue(stripped, "E")
                        if e is not None:
                            original_layer_end_e = float(e)

            is_first_brick = (layer_num == start_layer_gcode)
            is_last_brick = (layer_num == end_layer_gcode)

            # Check if the NEXT layer has target wall sections.
            # If not, we're at a model boundary (slope/edge/top) and
            # shifting loops up would extend walls beyond the model.
            if not is_last_brick:
                next_has_walls = self._next_layer_has_walls(
                    data, index, target_types)
                if not next_has_walls:
                    is_last_brick = True

            new_layer, actual_end_e = self._process_layer(
                layer_gcode, layer_num, z_shift,
                extrusion_multiplier, is_first_brick, is_last_brick,
                target_types, relative_extrusion,
                retract_length, retract_speed, travel_speed,
                original_e, output_e
            )

            if new_layer is not None:
                data[index] = new_layer
                layers_modified += 1

                # Fix position continuity at layer boundaries:
                # BrickLayers reorders loops, so the nozzle ends at a different
                # XY than the original layer. The next data block's first G1
                # extrusion move assumes position continuity from the original.
                # Insert a G0 travel to bridge the gap.
                self._fix_next_block_position(data, index, travel_speed)

            # Update E tracking for the next layer
            if not relative_extrusion:
                original_e = original_layer_end_e
                if new_layer is not None:
                    output_e = actual_end_e
                else:
                    # Layer wasn't modified — original E values pass through
                    # unchanged, so output_e must match original_e
                    output_e = original_layer_end_e

        Logger.log("d", "BrickLayers: Modified %d layers (layers %d-%d, z_shift=%.3fmm)",
                   layers_modified, start_layer_gcode, end_layer_gcode, z_shift)

        return data

    # ------------------------------------------------------------------ #
    # GCode helpers
    # ------------------------------------------------------------------ #

    @staticmethod
    def _getValue(line: str, key: str, default=None):
        if key not in line or (";" in line and line.find(key) > line.find(";")):
            return default
        sub_part = line[line.find(key) + 1:]
        m = re.search(r"^-?[0-9]+\.?[0-9]*", sub_part)
        if m is None:
            return default
        try:
            return int(m.group(0))
        except ValueError:
            try:
                return float(m.group(0))
            except ValueError:
                return default

    @staticmethod
    def _putValue(line: str = "", **kwargs) -> str:
        if ";" in line:
            comment = line[line.find(";"):]
            line = line[:line.find(";")]
        else:
            comment = ""

        for part in line.split(" "):
            if part == "":
                continue
            parameter = part[0]
            if parameter not in kwargs:
                kwargs[parameter] = part[1:]

        line_parts = []
        for parameter in ["G", "M", "T", "S", "F", "X", "Y", "Z", "E"]:
            if parameter in kwargs:
                value = kwargs.pop(parameter)
                line_parts.append(parameter + str(value))
        for parameter, value in kwargs.items():
            line_parts.append(parameter + str(value))

        if comment != "":
            line_parts.append(comment)

        return " ".join(line_parts)

    # ------------------------------------------------------------------ #
    # GCode analysis
    # ------------------------------------------------------------------ #

    def _detect_gcode_params(self, data: List[str],
                              global_stack=None
                              ) -> Tuple[bool, float, float]:
        """Detect extrusion mode and retraction parameters.

        Queries Cura settings first (authoritative), falls back to G-code
        scanning. Griffin-flavor G-code (Ultimaker S5) does NOT emit M82/M83
        so we cannot rely on G-code scanning alone.

        Returns (relative_extrusion, retract_length, retract_speed).
        """
        # Query Cura settings first — authoritative source
        if global_stack is not None:
            rel_ext_setting = global_stack.getProperty(
                "relative_extrusion", "value")
            if rel_ext_setting is not None:
                relative_extrusion = bool(rel_ext_setting)
            else:
                relative_extrusion = False  # absolute is safer default
        else:
            relative_extrusion = False  # absolute is safer default

        # Query retraction settings from Cura
        retract_length = 5.0
        retract_speed = 2400.0
        if global_stack is not None:
            rl = global_stack.getProperty("retraction_amount", "value")
            if rl is not None:
                retract_length = float(rl)
            rs = global_stack.getProperty("retraction_retract_speed", "value")
            if rs is not None:
                retract_speed = float(rs) * 60.0  # mm/s to mm/min

        # Still scan G-code for M82/M83 to override if explicitly present
        for block in data[:min(6, len(data))]:
            for line in block.split("\n"):
                stripped = line.strip()
                if stripped == "M83" or stripped.startswith("M83 "):
                    relative_extrusion = True
                elif stripped == "M82" or stripped.startswith("M82 "):
                    relative_extrusion = False

        return relative_extrusion, retract_length, retract_speed

    @staticmethod
    def _find_max_layer(data: List[str]) -> int:
        max_num = 0
        for block in data:
            for m in re.findall(r";LAYER:(-?\d+)", block):
                num = int(m)
                if num > max_num:
                    max_num = num
        return max_num

    @staticmethod
    def _get_layer_number(layer_gcode: str) -> Optional[int]:
        match = re.search(r";LAYER:(-?\d+)", layer_gcode)
        if match:
            return int(match.group(1))
        return None

    def _is_retraction(self, line: str, relative_extrusion: bool,
                       last_e: Optional[float] = None) -> bool:
        stripped = line.strip()
        if not stripped.startswith("G1 "):
            return False
        e_val = self._getValue(stripped, "E")
        x_val = self._getValue(stripped, "X")
        y_val = self._getValue(stripped, "Y")
        if e_val is None:
            return False
        if x_val is not None or y_val is not None:
            return False
        if relative_extrusion:
            return e_val < 0
        else:
            # Absolute mode: retraction means E decreased from last known position
            if last_e is None:
                return False
            return e_val < last_e

    def _is_unretraction(self, line: str, relative_extrusion: bool,
                         last_e: Optional[float] = None) -> bool:
        stripped = line.strip()
        if not stripped.startswith("G1 "):
            return False
        e_val = self._getValue(stripped, "E")
        x_val = self._getValue(stripped, "X")
        y_val = self._getValue(stripped, "Y")
        if e_val is None:
            return False
        if x_val is not None or y_val is not None:
            return False
        if relative_extrusion:
            return e_val > 0
        else:
            # Absolute mode: unretraction means E increased without XY move
            if last_e is None:
                return False
            return e_val > last_e

    # ------------------------------------------------------------------ #
    # Per-layer processing
    # ------------------------------------------------------------------ #

    def _process_layer(self, layer_gcode: str, layer_num: int, z_shift: float,
                       extrusion_multiplier: float, is_first_brick: bool,
                       is_last_brick: bool, target_types: set,
                       relative_extrusion: bool,
                       retract_length: float, retract_speed: float,
                       travel_speed: float,
                       layer_start_e: float = 0.0,
                       output_start_e: float = 0.0
                       ) -> Tuple[Optional[str], float]:

        # For absolute extrusion mode: convert the entire layer to relative E
        # BEFORE processing. This makes loop reordering safe since each E
        # value becomes a self-contained delta rather than a positional value.
        original_end_e = None
        if not relative_extrusion:
            layer_gcode, original_end_e = self._convert_to_relative_e(
                layer_gcode, layer_start_e)

        # After conversion, always process using relative extrusion logic
        effective_relative = True

        lines = layer_gcode.split("\n")

        # Detect the layer's working Z by tracking Z position and finding
        # the Z at which the first extrusion occurs. This correctly handles
        # Cura's common pattern where a high travel Z appears before the
        # TYPE marker, then a Z-drop to the actual working height appears
        # after it. Example from Cura output:
        #   G0 Z2.03           ← travel height (tracked_z = 2.03)
        #   ;TYPE:WALL-OUTER
        #   G1 F630 Z0.78      ← Z-drop to working height (tracked_z = 0.78)
        #   G1 X... Y... E...  ← first extrusion → current_z = 0.78
        tracked_z = None
        current_z = None
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("G0 ") or stripped.startswith("G1 "):
                z_val = self._getValue(stripped, "Z")
                if z_val is not None:
                    tracked_z = float(z_val)
                e_val = self._getValue(stripped, "E")
                x_val = self._getValue(stripped, "X")
                y_val = self._getValue(stripped, "Y")
                # First extrusion move (E > 0 in relative mode) with X or Y
                if (e_val is not None and float(e_val) > 0
                        and (x_val is not None or y_val is not None)):
                    current_z = tracked_z
                    break
        if current_z is None:
            # Fallback: use the lowest Z in the layer (Z-hops always go
            # up from the working Z, so the minimum is the working Z)
            min_z = None
            for line in lines:
                stripped = line.strip()
                if stripped.startswith("G0 ") or stripped.startswith("G1 "):
                    z_val = self._getValue(stripped, "Z")
                    if z_val is not None:
                        z_float = float(z_val)
                        if min_z is None or z_float < min_z:
                            min_z = z_float
            current_z = min_z

        if current_z is None:
            return None, output_start_e

        shifted_z = round(current_z + z_shift, 4)

        sections = []
        current_type = None
        current_loops: List[PerimeterLoop] = []
        current_loop: Optional[PerimeterLoop] = None
        other_lines: List[str] = []
        in_target_section = False

        for line in lines:
            stripped = line.strip()

            if stripped.startswith(";TYPE:"):
                new_type = stripped[6:]

                if in_target_section and new_type not in target_types:
                    if current_loop and current_loop.has_extrusion:
                        current_loops.append(current_loop)
                    # Preserve trailing non-extruding lines (retract/Z-hop/
                    # travel) that belong between the wall section and the
                    # next section. Without these, the nozzle doesn't retract
                    # or Z-hop before traveling, causing stringing and Z issues.
                    trailing = []
                    if current_loop and not current_loop.has_extrusion:
                        trailing.extend(current_loop.prefix_lines)
                        trailing.extend(current_loop.body_lines)
                    if current_loops:
                        if other_lines:
                            sections.append(("other", list(other_lines)))
                            other_lines = []
                        sections.append(("loops", list(current_loops), current_type))
                    # Add trailing lines to other_lines so they appear
                    # AFTER the loops section in the output
                    other_lines.extend(trailing)
                    current_loops = []
                    current_loop = None
                    in_target_section = False

                if new_type in target_types:
                    if not in_target_section:
                        if other_lines:
                            sections.append(("other", list(other_lines)))
                            other_lines = []
                        in_target_section = True
                        current_loop = PerimeterLoop(new_type)
                    other_lines.append(line)
                    current_type = new_type
                    continue
                else:
                    current_type = new_type
                    in_target_section = False
                    other_lines.append(line)
                    continue

            if not in_target_section:
                other_lines.append(line)
                continue

            is_g0 = stripped.startswith("G0 ")
            is_g1 = stripped.startswith("G1 ")

            if is_g1:
                e_val = self._getValue(stripped, "E")
                x_val = self._getValue(stripped, "X")
                y_val = self._getValue(stripped, "Y")

                # Always use relative extrusion logic after conversion
                is_extrusion = (e_val is not None and e_val > 0
                                and (x_val is not None or y_val is not None))

                is_retract = self._is_retraction(stripped, effective_relative)

                if is_retract:
                    if current_loop and current_loop.has_extrusion:
                        current_loops.append(current_loop)
                    current_loop = PerimeterLoop(current_type or "WALL-INNER")
                    current_loop.add_line(line, None, None, False)
                elif is_extrusion:
                    if current_loop is None:
                        current_loop = PerimeterLoop(current_type or "WALL-INNER")
                    current_loop.add_line(line, x_val, y_val, True)
                else:
                    if current_loop is None:
                        current_loop = PerimeterLoop(current_type or "WALL-INNER")
                    current_loop.add_line(line, x_val, y_val, False)

            elif is_g0:
                if current_loop and current_loop.has_extrusion:
                    current_loops.append(current_loop)
                    current_loop = PerimeterLoop(current_type or "WALL-INNER")
                elif current_loop is None:
                    current_loop = PerimeterLoop(current_type or "WALL-INNER")
                g0_x = self._getValue(stripped, "X")
                g0_y = self._getValue(stripped, "Y")
                current_loop.add_line(line, g0_x, g0_y, False)
            else:
                if current_loop is None:
                    current_loop = PerimeterLoop(current_type or "WALL-INNER")
                current_loop.add_line(line, None, None, False)

        if in_target_section:
            if current_loop and current_loop.has_extrusion:
                current_loops.append(current_loop)
            # Preserve trailing non-extruding lines at end of layer
            trailing = []
            if current_loop and not current_loop.has_extrusion:
                trailing.extend(current_loop.prefix_lines)
                trailing.extend(current_loop.body_lines)
            if current_loops:
                if other_lines:
                    sections.append(("other", list(other_lines)))
                    other_lines = []
                sections.append(("loops", list(current_loops), current_type))
                current_loops = []
            other_lines.extend(trailing)
        if other_lines:
            sections.append(("other", list(other_lines)))

        has_loops = any(s[0] == "loops" for s in sections)
        if not has_loops:
            return None, output_start_e

        # H1 fix: use conservative multipliers for first/last brick layers
        if is_first_brick:
            effective_multiplier = extrusion_multiplier * 1.15
        elif is_last_brick:
            effective_multiplier = extrusion_multiplier * 0.85
        else:
            effective_multiplier = extrusion_multiplier

        output_lines = []

        # H3 fix: collect deferred loops per-section to reset alternation
        # H4 fix: track wall type per deferred loop for correct TYPE markers
        all_deferred: List[Tuple[PerimeterLoop, str]] = []

        # Track whether the last loop section deferred its final loop,
        # which means position continuity may be broken for the next section.
        last_section_deferred_last = False

        for section in sections:
            if section[0] == "other":
                # If previous loops section deferred its last loop, the nozzle
                # is at a different position than the original. Fix position
                # for the first G1 extrusion in this section if needed.
                if last_section_deferred_last:
                    self._fix_section_position(output_lines, section[1],
                                               travel_speed)
                    last_section_deferred_last = False
                output_lines.extend(section[1])
            elif section[0] == "loops":
                loops = section[1]
                section_wall_type = section[2] if len(section) > 2 else "WALL-INNER"
                # H3 fix: reset loop counter per wall section
                loop_counter = 0
                last_section_deferred_last = False
                # Track whether we've skipped any loops since last emit,
                # to detect within-section position discontinuities
                skipped_since_last_emit = False

                for loop in loops:
                    if not loop.has_extrusion:
                        output_lines.extend(loop.prefix_lines)
                        output_lines.extend(loop.body_lines)
                        continue

                    # On the last brick layer, don't defer any loops.
                    # There's no layer above to interlock with, and shifting
                    # loops up causes inner walls to extend above the model.
                    if is_last_brick or loop_counter % 2 == 0:
                        # Fix within-section position continuity: if we
                        # skipped loops and this loop's prefix lacks a G0
                        # travel, the nozzle is at the wrong position.
                        if skipped_since_last_emit:
                            if not self._prefix_has_travel(loop.prefix_lines):
                                tx = loop.travel_x if loop.travel_x is not None else loop.start_x
                                ty = loop.travel_y if loop.travel_y is not None else loop.start_y
                                if tx is not None or ty is not None:
                                    parts = ["G0", "F%.0f" % travel_speed]
                                    if tx is not None:
                                        parts.append("X%.3f" % tx)
                                    if ty is not None:
                                        parts.append("Y%.3f" % ty)
                                    output_lines.append(
                                        " ".join(parts) + " ;BrickLayers position-fix")
                            skipped_since_last_emit = False
                        output_lines.extend(loop.prefix_lines)
                        output_lines.extend(loop.body_lines)
                        last_section_deferred_last = False
                    else:
                        all_deferred.append((loop, section_wall_type))
                        last_section_deferred_last = True
                        skipped_since_last_emit = True
                    loop_counter += 1

        if not all_deferred:
            return None, output_start_e

        is_retracted = self._check_retracted_state(output_lines, effective_relative)

        # Track XY position before deferred section so we can restore it after
        pre_deferred_x = None
        pre_deferred_y = None
        for line in reversed(output_lines):
            stripped = line.strip()
            if stripped.startswith("G0 ") or stripped.startswith("G1 "):
                if pre_deferred_x is None:
                    x = self._getValue(stripped, "X")
                    if x is not None:
                        pre_deferred_x = x
                if pre_deferred_y is None:
                    y = self._getValue(stripped, "Y")
                    if y is not None:
                        pre_deferred_y = y
                if pre_deferred_x is not None and pre_deferred_y is not None:
                    break

        output_lines.append(
            ";BrickLayers: shifted loops at Z=%.4f (offset +%.3f)" % (shifted_z, z_shift))

        # Retract before Z-shift travel (always relative E at this point)
        if not is_retracted:
            output_lines.append(
                "G1 F%.0f E%.5f ;BrickLayers retract" % (retract_speed, -retract_length))
            is_retracted = True

        output_lines.append(
            "G0 F%.0f Z%.4f ;BrickLayers Z-shift" % (travel_speed, shifted_z))

        current_deferred_type = None

        for i, (loop, wall_type) in enumerate(all_deferred):
            # H4 fix: emit TYPE marker when wall type changes
            if wall_type != current_deferred_type:
                output_lines.append(";TYPE:%s" % wall_type)
                current_deferred_type = wall_type

            # Use travel_x/y (G0 position from prefix) if available,
            # otherwise fall back to start_x/y. travel_x/y is where the
            # nozzle actually IS before the first extrusion, while
            # start_x/y is the DESTINATION of the first extrusion move.
            tx = loop.travel_x if loop.travel_x is not None else loop.start_x
            ty = loop.travel_y if loop.travel_y is not None else loop.start_y
            if tx is not None or ty is not None:
                travel_parts = ["G0"]
                travel_parts.append("F%.0f" % travel_speed)
                if tx is not None:
                    travel_parts.append("X%.3f" % tx)
                if ty is not None:
                    travel_parts.append("Y%.3f" % ty)
                output_lines.append(
                    " ".join(travel_parts) + " ;BrickLayers travel")

            # Unretract (always relative E)
            if is_retracted:
                output_lines.append(
                    "G1 F%.0f E%.5f ;BrickLayers unretract" % (
                        retract_speed, retract_length))
                is_retracted = False

            # H2 fix: strip Z values from deferred body lines to prevent
            # conflicting with the shifted Z position
            cleaned_body = self._strip_z_from_body(loop.body_lines, shifted_z)

            body_with_multiplier = self._apply_extrusion_multiplier(
                cleaned_body, effective_multiplier)
            output_lines.extend(body_with_multiplier)

            # Retract between deferred loops
            if i < len(all_deferred) - 1:
                output_lines.append(
                    "G1 F%.0f E%.5f ;BrickLayers retract" % (
                        retract_speed, -retract_length))
                is_retracted = True

        # Retract before Z-restore travel
        if not is_retracted:
            output_lines.append(
                "G1 F%.0f E%.5f ;BrickLayers retract" % (retract_speed, -retract_length))

        output_lines.append(";BrickLayers: restoring Z=%.4f" % current_z)
        output_lines.append(
            "G0 F%.0f Z%.4f ;BrickLayers Z-restore" % (travel_speed, current_z))

        # Restore XY position to where nozzle was before deferred section
        # (while still retracted, so no filament ooze during travel)
        if pre_deferred_x is not None or pre_deferred_y is not None:
            travel_parts = ["G0", "F%.0f" % travel_speed]
            if pre_deferred_x is not None:
                travel_parts.append("X%.3f" % pre_deferred_x)
            if pre_deferred_y is not None:
                travel_parts.append("Y%.3f" % pre_deferred_y)
            output_lines.append(
                " ".join(travel_parts) + " ;BrickLayers XY-restore")

        # C3 fix: unretract after Z-restore so subsequent G-code finds
        # the nozzle in the expected primed state
        output_lines.append(
            "G1 F%.0f E%.5f ;BrickLayers unretract" % (retract_speed, retract_length))

        # For absolute mode: convert relative E values back to absolute
        # so the output uses only G0/G1 commands (no M82/M83/G92 needed).
        # Use output_start_e (actual E from previous output) not layer_start_e
        # (original slicer E) so E values are continuous across layers.
        if not relative_extrusion:
            output_lines, actual_end_e = self._convert_to_absolute_e(
                output_lines, output_start_e)
            return "\n".join(output_lines), actual_end_e

        return "\n".join(output_lines), output_start_e

    def _check_retracted_state(self, lines: List[str],
                                relative_extrusion: bool) -> bool:
        """Check if the nozzle is currently retracted based on recent lines.

        After absolute-to-relative conversion, this is always called with
        relative E values, so we only need the relative mode check.
        """
        scan_lines = lines[-20:] if len(lines) > 20 else lines
        for line in reversed(scan_lines):
            if self._is_retraction(line, relative_extrusion):
                return True
            if self._is_unretraction(line, relative_extrusion):
                return False
            stripped = line.strip()
            if stripped.startswith("G1 "):
                e_val = self._getValue(stripped, "E")
                x_val = self._getValue(stripped, "X")
                if e_val is not None and x_val is not None and e_val > 0:
                    return False
        return False

    def _fix_next_block_position(self, data: List[str], current_index: int,
                                  travel_speed: float) -> None:
        """Insert a G0 travel at the start of the next data block if needed.

        After BrickLayers reorders loops in a layer, the nozzle ends at a
        different XY than the original slicer output. The next data block's
        first G1 extrusion move assumes position continuity from the original.
        This creates a diagonal extrusion line from the wrong start position.

        Fix: find the next block's first G1 with X/Y+E (extrusion move).
        If no G0 travel precedes it to set XY position, insert one.
        """
        next_index = current_index + 1
        if next_index >= len(data):
            return

        next_block = data[next_index]
        lines = next_block.split("\n")

        # Find the first G1 extrusion move (has X or Y + E) in the next block.
        # If a G0 with X or Y appears before it, the nozzle is already
        # positioned correctly and no fix is needed.
        insert_pos = None
        target_x = None
        target_y = None

        for i, line in enumerate(lines):
            stripped = line.strip()

            if stripped.startswith("G0 "):
                x = self._getValue(stripped, "X")
                y = self._getValue(stripped, "Y")
                if x is not None or y is not None:
                    # A G0 travel already positions the nozzle — no fix needed
                    return

            elif stripped.startswith("G1 "):
                e = self._getValue(stripped, "E")
                x = self._getValue(stripped, "X")
                y = self._getValue(stripped, "Y")
                if e is not None and (x is not None or y is not None):
                    # This is the first extrusion move — needs a preceding
                    # G0 travel to ensure correct start position
                    insert_pos = i
                    target_x = x
                    target_y = y
                    break

        if insert_pos is not None and (target_x is not None or target_y is not None):
            travel_parts = ["G0", "F%.0f" % travel_speed]
            if target_x is not None:
                travel_parts.append("X%.3f" % target_x)
            if target_y is not None:
                travel_parts.append("Y%.3f" % target_y)
            travel_line = " ".join(travel_parts) + " ;BrickLayers position-fix"
            lines.insert(insert_pos, travel_line)
            data[next_index] = "\n".join(lines)

    def _next_layer_has_walls(self, data: List[str], current_index: int,
                               target_types: set) -> bool:
        """Check if the next layer data block contains target wall sections.

        If the next layer has no wall sections matching target_types, we're
        at a model boundary (slope/edge/top). Shifting loops up would
        extend walls above the model surface.
        """
        for i in range(current_index + 1, min(current_index + 3, len(data))):
            block = data[i]
            layer_num = self._get_layer_number(block)
            if layer_num is None:
                continue
            # Found the next layer block — check for target wall TYPE markers
            for line in block.split("\n"):
                stripped = line.strip()
                if stripped.startswith(";TYPE:"):
                    wall_type = stripped[6:]
                    if wall_type in target_types:
                        return True
            return False
        return False

    def _prefix_has_travel(self, prefix_lines: List[str]) -> bool:
        """Check if a loop's prefix lines contain a G0 with X or Y."""
        for line in prefix_lines:
            stripped = line.strip()
            if stripped.startswith("G0 "):
                x = self._getValue(stripped, "X")
                y = self._getValue(stripped, "Y")
                if x is not None or y is not None:
                    return True
        return False

    def _fix_section_position(self, output_lines: List[str],
                               section_lines: List[str],
                               travel_speed: float) -> None:
        """Fix position continuity between a loops section and a following
        'other' section within the same layer.

        When the last loop in a wall section was deferred (skipped), the
        nozzle is at the previous even loop's end, not where the original
        slicer left it. If the 'other' section starts with G1 extrusion
        without a preceding G0 travel, a diagonal extrusion line would
        result. This inserts a G0 travel to prevent that.
        """
        for i, line in enumerate(section_lines):
            stripped = line.strip()

            if stripped.startswith("G0 "):
                x = self._getValue(stripped, "X")
                y = self._getValue(stripped, "Y")
                if x is not None or y is not None:
                    return  # Already positioned by G0

            elif stripped.startswith("G1 "):
                e = self._getValue(stripped, "E")
                x = self._getValue(stripped, "X")
                y = self._getValue(stripped, "Y")
                if e is not None and (x is not None or y is not None):
                    # First extrusion without preceding G0 — insert travel
                    travel_parts = ["G0", "F%.0f" % travel_speed]
                    if x is not None:
                        travel_parts.append("X%.3f" % x)
                    if y is not None:
                        travel_parts.append("Y%.3f" % y)
                    # Insert into section_lines before the G1 extrusion
                    section_lines.insert(
                        i, " ".join(travel_parts) + " ;BrickLayers position-fix")
                    return

    def _convert_to_relative_e(self, layer_gcode: str,
                                start_e: float) -> Tuple[str, float]:
        """Convert all G1 E values in a layer from absolute to relative deltas.

        Each absolute E value is replaced with the delta from the previous E.
        This makes loop reordering safe since deltas are order-independent.

        Returns (converted_gcode, original_end_e).
        """
        lines = layer_gcode.split("\n")
        result = []
        last_e = start_e
        original_end_e = start_e

        for line in lines:
            stripped = line.strip()
            if stripped.startswith("G1 "):
                e_val = self._getValue(stripped, "E")
                if e_val is not None:
                    original_end_e = float(e_val)
                    delta = round(float(e_val) - last_e, 5)
                    last_e = float(e_val)
                    result.append(self._putValue(stripped, E=delta))
                    continue
            result.append(line)

        return "\n".join(result), original_end_e

    def _convert_to_absolute_e(self, lines: List[str],
                                start_e: float) -> Tuple[List[str], float]:
        """Convert relative E values back to absolute by accumulating deltas.

        Takes output lines with relative E and returns lines with absolute E,
        starting from start_e. This avoids needing M83/M82/G92 commands which
        some firmware (e.g. Ultimaker S5) does not support.

        Returns (converted_lines, final_e_position).
        """
        result = []
        current_e = start_e

        for line in lines:
            stripped = line.strip()
            if stripped.startswith("G1 "):
                e_val = self._getValue(stripped, "E")
                if e_val is not None:
                    current_e = round(current_e + float(e_val), 5)
                    result.append(self._putValue(stripped, E=current_e))
                    continue
            result.append(line)

        return result, current_e

    @staticmethod
    def _strip_z_from_body(lines: List[str], shifted_z: float) -> List[str]:
        """Remove or replace Z values in body lines to prevent conflicts
        with the shifted Z position."""
        result = []
        for line in lines:
            stripped = line.strip()
            if (stripped.startswith("G0 ") or stripped.startswith("G1 ")):
                z_val = BrickLayers._getValue(stripped, "Z")
                if z_val is not None:
                    # Replace the Z value with the shifted Z
                    line = re.sub(r'Z-?[0-9]+\.?[0-9]*', 'Z%.4f' % shifted_z, line)
            result.append(line)
        return result

    def _apply_extrusion_multiplier(self, lines: List[str],
                                     multiplier: float) -> List[str]:
        """Apply extrusion multiplier to lines with relative E values.

        Scales positive E values on extrusion moves (G1 with X/Y and E > 0).
        """
        if multiplier == 1.0:
            return list(lines)

        result = []
        for line in lines:
            stripped = line.strip()
            if not stripped.startswith("G1 "):
                result.append(line)
                continue

            e_val = self._getValue(stripped, "E")
            x_val = self._getValue(stripped, "X")
            y_val = self._getValue(stripped, "Y")

            if e_val is None:
                result.append(line)
                continue

            is_extrusion_move = (x_val is not None or y_val is not None)
            if is_extrusion_move and e_val > 0:
                new_e = round(e_val * multiplier, 5)
                result.append(self._putValue(stripped, E=new_e))
            else:
                result.append(line)

        return result
