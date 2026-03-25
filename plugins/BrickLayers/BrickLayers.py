# Copyright (c) 2024
# BrickLayers plugin is released under the terms of the LGPLv3 or higher.
# Inspired by TengerTechnologies/Bricklayers and GeekDetour/BrickLayers.
#
# Shifts alternating perimeter wall loops up by half a layer height,
# creating interlocking brick-like walls for dramatically stronger prints.
#
# Settings are defined in fdmprinter.def.json under the "experimental"
# category and appear in Cura's sidebar like any other print setting.

import re
from typing import List, Optional, Tuple

from UM.Application import Application
from UM.Extension import Extension
from UM.Logger import Logger
from UM.PluginRegistry import PluginRegistry

from cura.CuraApplication import CuraApplication

# Sentinel comment to prevent double-processing
_BRICK_LAYERS_MARKER = ";BRICKLAYERS_PROCESSED"


class PerimeterLoop:
    """Stores GCode lines belonging to a single perimeter loop.

    Lines are separated into prefix (retract/travel/unretract before extrusion)
    and body (extrusion moves and any mid-loop non-extrusion moves).
    This separation allows clean reconstruction of transitions when loops
    are reordered.
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
        Application.getInstance().getOutputDeviceManager().writeStarted.connect(
            self._onWriteStarted)

    # ------------------------------------------------------------------ #
    # Settings (read from global container stack — defined in fdmprinter.def.json)
    # ------------------------------------------------------------------ #

    @staticmethod
    def _getSetting(key: str):
        """Read a setting from the global container stack."""
        global_stack = CuraApplication.getInstance().getGlobalContainerStack()
        if global_stack is None:
            return None
        return global_stack.getProperty(key, "value")

    # ------------------------------------------------------------------ #
    # GCode pipeline hook
    # ------------------------------------------------------------------ #

    def _onWriteStarted(self, output_device) -> None:
        """Hook into GCode write pipeline — modify GCode before saving."""
        if not self._getSetting("brick_layers_enabled"):
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

        # Don't process twice
        if _BRICK_LAYERS_MARKER in gcode_list[0]:
            return

        gcode_list = self._execute(gcode_list)
        gcode_list[0] += _BRICK_LAYERS_MARKER + "\n"
        gcode_dict[active_build_plate_id] = gcode_list
        setattr(scene, "gcode_dict", gcode_dict)

        # Refresh the preview layer data so the Z-shifts are visible
        self._refreshPreviewLayerData(scene, gcode_list)

    def _refreshPreviewLayerData(self, scene, gcode_list: List[str]) -> None:
        """Re-parse modified GCode into layer data and update the preview."""
        try:
            from UM.Scene.Iterator.DepthFirstIterator import DepthFirstIterator
            from cura.LayerDataDecorator import LayerDataDecorator

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

            scene.sceneChanged.emit(target_node)
            Logger.log("d", "BrickLayers: Preview layer data refreshed")

        except Exception:
            Logger.logException("w", "BrickLayers: Failed to refresh preview layer data")

    # ------------------------------------------------------------------ #
    # GCode transformation logic
    # ------------------------------------------------------------------ #

    def _execute(self, data: List[str]) -> List[str]:
        global_stack = CuraApplication.getInstance().getGlobalContainerStack()
        if global_stack is None:
            return data

        # Read settings from the print profile
        layer_height = float(global_stack.getProperty("layer_height", "value"))
        extrusion_multiplier = float(global_stack.getProperty("brick_layers_extrusion_multiplier", "value"))
        start_layer = int(global_stack.getProperty("brick_layers_start_layer", "value"))
        end_layer = int(global_stack.getProperty("brick_layers_end_layer", "value"))
        apply_inner = bool(global_stack.getProperty("brick_layers_apply_inner_walls", "value"))
        apply_outer = bool(global_stack.getProperty("brick_layers_apply_outer_walls", "value"))

        z_shift = layer_height / 2.0

        target_types = set()
        if apply_inner:
            target_types.add("WALL-INNER")
        if apply_outer:
            target_types.add("WALL-OUTER")

        if not target_types:
            Logger.log("w", "BrickLayers: No wall types selected, nothing to do.")
            return data

        relative_extrusion, retract_length, retract_speed, travel_speed = \
            self._detect_gcode_params(data)

        max_layer_num = self._find_max_layer(data)
        if end_layer == -1:
            end_layer_gcode = max_layer_num
        else:
            end_layer_gcode = end_layer - 1

        start_layer_gcode = start_layer - 1
        layers_modified = 0

        for index in range(len(data)):
            layer_gcode = data[index]

            layer_num = self._get_layer_number(layer_gcode)
            if layer_num is None or layer_num < 0:
                continue
            if layer_num < start_layer_gcode or layer_num > end_layer_gcode:
                continue

            is_first_brick = (layer_num == start_layer_gcode)
            is_last_brick = (layer_num == end_layer_gcode)

            new_layer = self._process_layer(
                layer_gcode, layer_num, z_shift,
                extrusion_multiplier, is_first_brick, is_last_brick,
                target_types, relative_extrusion,
                retract_length, retract_speed, travel_speed
            )

            if new_layer is not None:
                data[index] = new_layer
                layers_modified += 1

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
                              ) -> Tuple[bool, float, float, float]:
        relative_extrusion = True
        retract_length = 5.0
        retract_speed = 2400.0
        travel_speed = 9000.0
        retract_detected = False

        for block in data[:min(6, len(data))]:
            for line in block.split("\n"):
                stripped = line.strip()
                if stripped == "M83":
                    relative_extrusion = True
                elif stripped == "M82":
                    relative_extrusion = False
                elif stripped.startswith("G0 "):
                    f_val = self._getValue(stripped, "F")
                    if f_val is not None and f_val > travel_speed:
                        travel_speed = f_val
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

        return relative_extrusion, retract_length, retract_speed, travel_speed

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

    def _is_retraction(self, line: str, relative_extrusion: bool) -> bool:
        stripped = line.strip()
        if not stripped.startswith("G1 "):
            return False
        e_val = self._getValue(stripped, "E")
        x_val = self._getValue(stripped, "X")
        y_val = self._getValue(stripped, "Y")
        if e_val is None:
            return False
        if relative_extrusion:
            return e_val < 0 and x_val is None and y_val is None
        else:
            return x_val is None and y_val is None

    def _is_unretraction(self, line: str, relative_extrusion: bool) -> bool:
        stripped = line.strip()
        if not stripped.startswith("G1 "):
            return False
        e_val = self._getValue(stripped, "E")
        x_val = self._getValue(stripped, "X")
        y_val = self._getValue(stripped, "Y")
        if e_val is None:
            return False
        if relative_extrusion:
            return e_val > 0 and x_val is None and y_val is None
        else:
            return x_val is None and y_val is None

    # ------------------------------------------------------------------ #
    # Per-layer processing
    # ------------------------------------------------------------------ #

    def _process_layer(self, layer_gcode: str, layer_num: int, z_shift: float,
                       extrusion_multiplier: float, is_first_brick: bool,
                       is_last_brick: bool, target_types: set,
                       relative_extrusion: bool,
                       retract_length: float, retract_speed: float,
                       travel_speed: float) -> Optional[str]:
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
            return None

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

                if relative_extrusion:
                    is_extrusion = (e_val is not None and e_val > 0
                                    and (x_val is not None or y_val is not None))
                else:
                    is_extrusion = (e_val is not None
                                    and (x_val is not None or y_val is not None))

                is_retract = self._is_retraction(stripped, relative_extrusion)

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
            return None

        if is_first_brick:
            effective_multiplier = extrusion_multiplier * 1.5
        elif is_last_brick:
            effective_multiplier = extrusion_multiplier * 0.5
        else:
            effective_multiplier = extrusion_multiplier

        output_lines = []
        all_deferred: List[PerimeterLoop] = []
        deferred_type = None
        loop_counter = 0

        for section in sections:
            if section[0] == "other":
                output_lines.extend(section[1])
            elif section[0] == "loops":
                loops = section[1]
                section_wall_type = section[2] if len(section) > 2 else "WALL-INNER"

                for loop in loops:
                    if not loop.has_extrusion:
                        output_lines.extend(loop.prefix_lines)
                        output_lines.extend(loop.body_lines)
                        continue

                    if loop_counter % 2 == 1:
                        all_deferred.append(loop)
                        deferred_type = section_wall_type
                    else:
                        output_lines.extend(loop.prefix_lines)
                        output_lines.extend(loop.body_lines)
                    loop_counter += 1

        if not all_deferred:
            return None

        is_retracted = self._check_retracted_state(output_lines, relative_extrusion)

        output_lines.append(
            ";BrickLayers: shifted loops at Z=%.4f (offset +%.3f)" % (shifted_z, z_shift))
        output_lines.append(";TYPE:%s" % (deferred_type or "WALL-INNER"))

        if not is_retracted and relative_extrusion:
            output_lines.append(
                "G1 F%.0f E%.5f ;BrickLayers retract" % (retract_speed, -retract_length))
            is_retracted = True

        output_lines.append(
            "G0 F%.0f Z%.4f ;BrickLayers Z-shift" % (travel_speed, shifted_z))

        for i, loop in enumerate(all_deferred):
            if loop.start_x is not None or loop.start_y is not None:
                travel_parts = ["G0"]
                travel_parts.append("F%.0f" % travel_speed)
                if loop.start_x is not None:
                    travel_parts.append("X%.3f" % loop.start_x)
                if loop.start_y is not None:
                    travel_parts.append("Y%.3f" % loop.start_y)
                output_lines.append(
                    " ".join(travel_parts) + " ;BrickLayers travel")

            if is_retracted and relative_extrusion:
                output_lines.append(
                    "G1 F%.0f E%.5f ;BrickLayers unretract" % (
                        retract_speed, retract_length))
                is_retracted = False

            body_with_multiplier = self._apply_extrusion_multiplier(
                loop.body_lines, effective_multiplier, relative_extrusion)
            output_lines.extend(body_with_multiplier)

            if i < len(all_deferred) - 1:
                if relative_extrusion:
                    output_lines.append(
                        "G1 F%.0f E%.5f ;BrickLayers retract" % (
                            retract_speed, -retract_length))
                    is_retracted = True

        if not is_retracted and relative_extrusion:
            output_lines.append(
                "G1 F%.0f E%.5f ;BrickLayers retract" % (retract_speed, -retract_length))

        output_lines.append(";BrickLayers: restoring Z=%.4f" % current_z)
        output_lines.append(
            "G0 F%.0f Z%.4f ;BrickLayers Z-restore" % (travel_speed, current_z))

        return "\n".join(output_lines)

    def _check_retracted_state(self, lines: List[str],
                                relative_extrusion: bool) -> bool:
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
                if e_val is not None and x_val is not None:
                    if relative_extrusion and e_val > 0:
                        return False
        return False

    def _apply_extrusion_multiplier(self, lines: List[str], multiplier: float,
                                     relative_extrusion: bool) -> List[str]:
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

            if relative_extrusion:
                if e_val > 0 and (x_val is not None or y_val is not None):
                    new_e = round(e_val * multiplier, 5)
                    result.append(self._putValue(stripped, E=new_e))
                else:
                    result.append(line)
            else:
                result.append(line)

        return result
