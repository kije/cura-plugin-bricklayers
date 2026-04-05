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
from UM.Logger import Logger
from UM.PluginRegistry import PluginRegistry
from UM.Settings.SettingDefinition import SettingDefinition
from UM.Settings.DefinitionContainer import DefinitionContainer
from UM.Settings.ContainerRegistry import ContainerRegistry
from UM.i18n import i18nCatalog

from cura.CuraApplication import CuraApplication

_BRICK_LAYERS_MARKER = ";BRICKLAYERS_PROCESSED"


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
    # GCode pipeline hook
    # ------------------------------------------------------------------ #

    def _onWriteStarted(self, output_device) -> None:
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

        if _BRICK_LAYERS_MARKER in gcode_list[0]:
            return

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

            gcode_stream = "\n".join(gcode_list)
            gcode_reader.preReadFromStream(gcode_stream)
            result_node = gcode_reader.readFromStream(gcode_stream, "bricklayers_preview")

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
            self._detect_gcode_params(data)

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

            # Update E tracking for the next layer
            if not relative_extrusion:
                original_e = original_layer_end_e
                output_e = actual_end_e

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

    def _detect_gcode_params(self, data: List[str]
                              ) -> Tuple[bool, float, float]:
        """Detect extrusion mode and retraction parameters from G-code header.

        Returns (relative_extrusion, retract_length, retract_speed).
        """
        relative_extrusion = True
        retract_length = 5.0
        retract_speed = 2400.0
        retract_detected = False

        for block in data[:min(6, len(data))]:
            for line in block.split("\n"):
                stripped = line.strip()
                if stripped == "M83":
                    relative_extrusion = True
                elif stripped == "M82":
                    relative_extrusion = False
                elif not retract_detected and stripped.startswith("G1 "):
                    e_val = self._getValue(stripped, "E")
                    f_val = self._getValue(stripped, "F")
                    x_val = self._getValue(stripped, "X")
                    y_val = self._getValue(stripped, "Y")
                    if e_val is not None and x_val is None and y_val is None:
                        if relative_extrusion and e_val < 0:
                            retract_length = abs(e_val)
                            if f_val is not None:
                                retract_speed = f_val
                            retract_detected = True

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

        current_z = None
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("G0 ") or stripped.startswith("G1 "):
                z_val = self._getValue(stripped, "Z")
                if z_val is not None:
                    current_z = z_val
                    break

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
                    if current_loops:
                        if other_lines:
                            sections.append(("other", list(other_lines)))
                            other_lines = []
                        sections.append(("loops", list(current_loops), current_type))
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
                current_loop.add_line(line, None, None, False)
            else:
                if current_loop is None:
                    current_loop = PerimeterLoop(current_type or "WALL-INNER")
                current_loop.add_line(line, None, None, False)

        if in_target_section:
            if current_loop and current_loop.has_extrusion:
                current_loops.append(current_loop)
            if current_loops:
                if other_lines:
                    sections.append(("other", list(other_lines)))
                    other_lines = []
                sections.append(("loops", list(current_loops), current_type))
                current_loops = []
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

        for section in sections:
            if section[0] == "other":
                output_lines.extend(section[1])
            elif section[0] == "loops":
                loops = section[1]
                section_wall_type = section[2] if len(section) > 2 else "WALL-INNER"
                # H3 fix: reset loop counter per wall section
                loop_counter = 0

                for loop in loops:
                    if not loop.has_extrusion:
                        output_lines.extend(loop.prefix_lines)
                        output_lines.extend(loop.body_lines)
                        continue

                    if loop_counter % 2 == 1:
                        all_deferred.append((loop, section_wall_type))
                    else:
                        output_lines.extend(loop.prefix_lines)
                        output_lines.extend(loop.body_lines)
                    loop_counter += 1

        if not all_deferred:
            return None, output_start_e

        is_retracted = self._check_retracted_state(output_lines, effective_relative)

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

            if loop.start_x is not None or loop.start_y is not None:
                travel_parts = ["G0"]
                travel_parts.append("F%.0f" % travel_speed)
                if loop.start_x is not None:
                    travel_parts.append("X%.3f" % loop.start_x)
                if loop.start_y is not None:
                    travel_parts.append("Y%.3f" % loop.start_y)
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
                                start_e: float) -> List[str]:
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
