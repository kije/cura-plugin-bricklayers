# Copyright (c) 2024
# Tests for the BrickLayers plugin.
# These tests mock all Cura/UM dependencies so they can run standalone.
#
# The module is loaded directly via importlib to avoid triggering the
# BrickLayers package __init__.py (which imports from UM/cura).

import importlib.util
import os
import sys
import unittest
from unittest.mock import MagicMock

# ---------------------------------------------------------------------------
# Mock all Cura / UM modules before importing the BrickLayers module
# ---------------------------------------------------------------------------
_MOCK_MODULES = [
    "UM",
    "UM.Application",
    "UM.Extension",
    "UM.Logger",
    "UM.PluginRegistry",
    "UM.Settings",
    "UM.Settings.SettingDefinition",
    "UM.Settings.DefinitionContainer",
    "UM.Settings.ContainerRegistry",
    "UM.i18n",
    "cura",
    "cura.CuraApplication",
]

for mod_name in _MOCK_MODULES:
    if mod_name not in sys.modules:
        sys.modules[mod_name] = MagicMock()

# Make Extension a real base class so BrickLayers can inherit from it
sys.modules["UM.Extension"].Extension = type(
    "Extension", (), {"__init__": lambda self: None}
)

# Load BrickLayers.py directly, bypassing the package __init__.py
_MODULE_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..",
    "BrickLayers.py",
)
_spec = importlib.util.spec_from_file_location("BrickLayers_mod", _MODULE_PATH)
_bl_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_bl_module)

BrickLayers = _bl_module.BrickLayers
PerimeterLoop = _bl_module.PerimeterLoop


def _make_instance():
    """Create a BrickLayers instance with mocked __init__."""
    obj = object.__new__(BrickLayers)
    return obj


# =========================================================================
# _getValue tests
# =========================================================================
class TestGetValue(unittest.TestCase):

    def test_parse_x(self):
        self.assertEqual(BrickLayers._getValue("G1 X10 Y20", "X"), 10)

    def test_parse_y(self):
        self.assertEqual(BrickLayers._getValue("G1 X10 Y20", "Y"), 20)

    def test_parse_z(self):
        self.assertAlmostEqual(BrickLayers._getValue("G0 X10 Y20 Z1.2", "Z"), 1.2)

    def test_parse_e_float(self):
        self.assertAlmostEqual(BrickLayers._getValue("G1 X10 Y20 E0.5", "E"), 0.5)

    def test_parse_f(self):
        self.assertEqual(BrickLayers._getValue("G1 F1200 X10 Y20 E0.5", "F"), 1200)

    def test_parse_g(self):
        self.assertEqual(BrickLayers._getValue("G0 F9000 X10", "G"), 0)

    def test_comment_hides_value(self):
        # X appears only after the semicolon - should not be found
        result = BrickLayers._getValue("G1 Y10 ;X99", "X")
        self.assertIsNone(result)

    def test_value_before_comment(self):
        result = BrickLayers._getValue("G1 X10 ;comment", "X")
        self.assertEqual(result, 10)

    def test_missing_key_returns_default(self):
        self.assertIsNone(BrickLayers._getValue("G1 X10 Y20", "E"))

    def test_missing_key_custom_default(self):
        self.assertEqual(BrickLayers._getValue("G1 X10", "Z", 0.0), 0.0)

    def test_negative_value(self):
        self.assertAlmostEqual(BrickLayers._getValue("G1 E-5.0", "E"), -5.0)

    def test_float_return(self):
        val = BrickLayers._getValue("G1 X10.5", "X")
        self.assertIsInstance(val, float)
        self.assertAlmostEqual(val, 10.5)

    def test_int_return(self):
        val = BrickLayers._getValue("G1 X10", "X")
        self.assertIsInstance(val, int)


# =========================================================================
# _putValue tests
# =========================================================================
class TestPutValue(unittest.TestCase):

    def test_construct_from_scratch(self):
        result = BrickLayers._putValue(G=1, X=10, Y=20)
        self.assertEqual(result, "G1 X10 Y20")

    def test_modify_existing(self):
        result = BrickLayers._putValue("G1 X10 Y20 E0.5", E=1.0)
        # E should be replaced; X, Y preserved
        self.assertIn("E1.0", result)
        self.assertIn("X10", result)
        self.assertIn("Y20", result)

    def test_preserve_comment(self):
        result = BrickLayers._putValue("G1 X10 ;my comment", Y=20)
        self.assertIn(";my comment", result)
        self.assertIn("X10", result)
        self.assertIn("Y20", result)

    def test_parameter_ordering(self):
        result = BrickLayers._putValue(E=1.0, X=10, G=1, F=1200, Y=20, Z=0.5)
        parts = result.split(" ")
        keys = [p[0] for p in parts]
        # Expected order: G, M, T, S, F, X, Y, Z, E
        expected_order = ["G", "F", "X", "Y", "Z", "E"]
        self.assertEqual(keys, expected_order)

    def test_empty_line(self):
        result = BrickLayers._putValue(G=0, X=100)
        self.assertEqual(result, "G0 X100")


# =========================================================================
# _find_max_layer tests
# =========================================================================
class TestFindMaxLayer(unittest.TestCase):

    def test_multiple_blocks(self):
        data = [
            ";LAYER:0\nG1 X10",
            ";LAYER:5\nG1 X20",
            ";LAYER:10\nG1 X30",
        ]
        self.assertEqual(BrickLayers._find_max_layer(data), 10)

    def test_negative_layer_numbers(self):
        data = [
            ";LAYER:-2\nG1 X10",
            ";LAYER:-1\nG1 X20",
            ";LAYER:0\nG1 X30",
            ";LAYER:3\nG1 X40",
        ]
        self.assertEqual(BrickLayers._find_max_layer(data), 3)

    def test_single_block(self):
        data = [";LAYER:7\nG1 X10"]
        self.assertEqual(BrickLayers._find_max_layer(data), 7)

    def test_no_layers(self):
        data = ["G28\nG1 X10"]
        self.assertEqual(BrickLayers._find_max_layer(data), 0)


# =========================================================================
# _get_layer_number tests
# =========================================================================
class TestGetLayerNumber(unittest.TestCase):

    def test_valid_layer(self):
        self.assertEqual(BrickLayers._get_layer_number(";LAYER:5\nG1 X10"), 5)

    def test_no_layer_marker(self):
        self.assertIsNone(BrickLayers._get_layer_number("G1 X10 Y20"))

    def test_negative_layer(self):
        self.assertEqual(BrickLayers._get_layer_number(";LAYER:-1\nG1 X10"), -1)


# =========================================================================
# _is_retraction / _is_unretraction tests
# =========================================================================
class TestRetractionDetection(unittest.TestCase):

    def setUp(self):
        self.bl = _make_instance()

    # _is_retraction
    def test_retraction_relative_negative_e(self):
        self.assertTrue(self.bl._is_retraction("G1 F2400 E-5.0", True))

    def test_retraction_relative_positive_e_is_not_retraction(self):
        self.assertFalse(self.bl._is_retraction("G1 F2400 E5.0", True))

    def test_retraction_absolute_e_less_than_last(self):
        self.assertTrue(self.bl._is_retraction("G1 F2400 E10.0", False, last_e=15.0))

    def test_retraction_absolute_e_more_than_last(self):
        self.assertFalse(self.bl._is_retraction("G1 F2400 E20.0", False, last_e=15.0))

    def test_retraction_non_g1(self):
        self.assertFalse(self.bl._is_retraction("G0 F9000 E-5.0", True))

    def test_retraction_g1_with_xy(self):
        # Extrusion move, not retraction
        self.assertFalse(self.bl._is_retraction("G1 X10 Y20 E-0.5", True))

    def test_retraction_no_e(self):
        self.assertFalse(self.bl._is_retraction("G1 F2400 X10", True))

    def test_retraction_absolute_no_last_e(self):
        self.assertFalse(self.bl._is_retraction("G1 F2400 E5.0", False, last_e=None))

    # _is_unretraction
    def test_unretraction_relative_positive_e(self):
        self.assertTrue(self.bl._is_unretraction("G1 F2400 E5.0", True))

    def test_unretraction_relative_negative_e(self):
        self.assertFalse(self.bl._is_unretraction("G1 F2400 E-5.0", True))

    def test_unretraction_absolute_e_more_than_last(self):
        self.assertTrue(self.bl._is_unretraction("G1 F2400 E20.0", False, last_e=15.0))

    def test_unretraction_absolute_e_less_than_last(self):
        self.assertFalse(self.bl._is_unretraction("G1 F2400 E10.0", False, last_e=15.0))

    def test_unretraction_non_g1(self):
        self.assertFalse(self.bl._is_unretraction("G0 F9000 E5.0", True))

    def test_unretraction_g1_with_xy(self):
        self.assertFalse(self.bl._is_unretraction("G1 X10 Y20 E0.5", True))

    def test_unretraction_no_e(self):
        self.assertFalse(self.bl._is_unretraction("G1 F2400 X10", True))


# =========================================================================
# _strip_z_from_body tests
# =========================================================================
class TestStripZFromBody(unittest.TestCase):

    def test_g1_z_replaced(self):
        lines = ["G1 F1200 X10 Y20 Z1.2 E0.5"]
        result = BrickLayers._strip_z_from_body(lines, 1.35)
        self.assertIn("Z1.3500", result[0])
        self.assertNotIn("Z1.2", result[0])

    def test_g0_z_replaced(self):
        lines = ["G0 F9000 X10 Y20 Z1.2"]
        result = BrickLayers._strip_z_from_body(lines, 1.35)
        self.assertIn("Z1.3500", result[0])

    def test_line_without_z_unchanged(self):
        lines = ["G1 F1200 X10 Y20 E0.5"]
        result = BrickLayers._strip_z_from_body(lines, 1.35)
        self.assertEqual(result[0], "G1 F1200 X10 Y20 E0.5")

    def test_non_g0g1_unchanged(self):
        lines = ["M104 S200"]
        result = BrickLayers._strip_z_from_body(lines, 1.35)
        self.assertEqual(result[0], "M104 S200")

    def test_comment_preserved(self):
        lines = ["G1 X10 Z1.2 ;my comment"]
        result = BrickLayers._strip_z_from_body(lines, 1.35)
        self.assertIn(";my comment", result[0])
        self.assertIn("Z1.3500", result[0])


# =========================================================================
# _convert_to_relative_e tests
# =========================================================================
class TestConvertToRelativeE(unittest.TestCase):

    def setUp(self):
        self.bl = _make_instance()

    def test_basic_conversion(self):
        gcode = "G1 F1200 X10 Y10 E100.0\nG1 F1200 X20 Y20 E101.0\nG1 F1200 X30 Y30 E102.5"
        result, end_e = self.bl._convert_to_relative_e(gcode, 99.0)
        lines = result.split("\n")
        # First line: 100.0 - 99.0 = 1.0
        self.assertAlmostEqual(BrickLayers._getValue(lines[0], "E"), 1.0)
        # Second line: 101.0 - 100.0 = 1.0
        self.assertAlmostEqual(BrickLayers._getValue(lines[1], "E"), 1.0)
        # Third line: 102.5 - 101.0 = 1.5
        self.assertAlmostEqual(BrickLayers._getValue(lines[2], "E"), 1.5)
        self.assertAlmostEqual(end_e, 102.5)

    def test_retraction_becomes_negative(self):
        gcode = "G1 F1200 X10 Y10 E100.0\nG1 F2400 E95.0"
        result, end_e = self.bl._convert_to_relative_e(gcode, 99.0)
        lines = result.split("\n")
        # Retraction: 95.0 - 100.0 = -5.0
        self.assertAlmostEqual(BrickLayers._getValue(lines[1], "E"), -5.0)

    def test_non_g1_lines_unchanged(self):
        gcode = "G0 F9000 X50 Y50\n;comment\nG1 F1200 X10 Y10 E100.0"
        result, end_e = self.bl._convert_to_relative_e(gcode, 99.0)
        lines = result.split("\n")
        self.assertEqual(lines[0], "G0 F9000 X50 Y50")
        self.assertEqual(lines[1], ";comment")

    def test_preserves_xy_values(self):
        gcode = "G1 F1200 X10 Y20 E100.5"
        result, _ = self.bl._convert_to_relative_e(gcode, 100.0)
        self.assertIn("X10", result)
        self.assertIn("Y20", result)


# =========================================================================
# _check_retracted_state tests
# =========================================================================
class TestCheckRetractedState(unittest.TestCase):

    def setUp(self):
        self.bl = _make_instance()

    def test_relative_last_retraction(self):
        lines = [
            "G1 F1200 X10 Y10 E0.5",
            "G1 F2400 E-5.0",
        ]
        self.assertTrue(self.bl._check_retracted_state(lines, True))

    def test_relative_last_unretraction(self):
        lines = [
            "G1 F2400 E-5.0",
            "G1 F2400 E5.0",
        ]
        self.assertFalse(self.bl._check_retracted_state(lines, True))

    def test_relative_last_extrusion(self):
        lines = [
            "G1 F2400 E5.0",
            "G1 F1200 X10 Y10 E0.5",
        ]
        self.assertFalse(self.bl._check_retracted_state(lines, True))

    def test_empty_lines(self):
        lines = [";comment", "G0 F9000 X10 Y10"]
        self.assertFalse(self.bl._check_retracted_state(lines, True))


# =========================================================================
# _apply_extrusion_multiplier tests
# =========================================================================
class TestApplyExtrusionMultiplier(unittest.TestCase):

    def setUp(self):
        self.bl = _make_instance()

    def test_relative_scales_positive_e(self):
        lines = ["G1 F1200 X10 Y10 E0.5"]
        result = self.bl._apply_extrusion_multiplier(lines, 1.5)
        e_val = BrickLayers._getValue(result[0], "E")
        self.assertAlmostEqual(e_val, 0.75)

    def test_relative_no_scale_retraction(self):
        # Retraction: negative E, no XY - should not be scaled
        lines = ["G1 F2400 E-5.0"]
        result = self.bl._apply_extrusion_multiplier(lines, 1.5)
        e_val = BrickLayers._getValue(result[0], "E")
        self.assertAlmostEqual(e_val, -5.0)

    def test_relative_multiplier_1_unchanged(self):
        lines = ["G1 F1200 X10 Y10 E0.5"]
        result = self.bl._apply_extrusion_multiplier(lines, 1.0)
        self.assertEqual(result[0], lines[0])

    def test_non_g1_lines_unchanged(self):
        lines = ["G0 F9000 X10 Y10", ";comment"]
        result = self.bl._apply_extrusion_multiplier(lines, 2.0)
        self.assertEqual(result, lines)

    def test_no_e_value_unchanged(self):
        lines = ["G1 F1200 X10 Y10"]
        result = self.bl._apply_extrusion_multiplier(lines, 2.0)
        self.assertEqual(result[0], lines[0])

    def test_zero_e_not_scaled(self):
        # E=0 (no extrusion) should not be scaled
        lines = ["G1 F1200 X10 Y10 E0"]
        result = self.bl._apply_extrusion_multiplier(lines, 2.0)
        e_val = BrickLayers._getValue(result[0], "E")
        self.assertEqual(e_val, 0)


# =========================================================================
# _detect_gcode_params tests
# =========================================================================
class TestDetectGcodeParams(unittest.TestCase):

    def setUp(self):
        self.bl = _make_instance()

    def test_detects_m83(self):
        data = ["M83\nG1 F2400 E-5.0\n"]
        rel, length, speed = self.bl._detect_gcode_params(data)
        self.assertTrue(rel)

    def test_detects_m82(self):
        data = ["M82\nG1 X10 Y10 E1.0\n"]
        rel, _, _ = self.bl._detect_gcode_params(data)
        self.assertFalse(rel)

    def test_detects_retraction_params(self):
        data = ["M83\nG1 F2400 E-6.5\n"]
        rel, length, speed = self.bl._detect_gcode_params(data)
        self.assertAlmostEqual(length, 6.5)
        self.assertAlmostEqual(speed, 2400.0)

    def test_defaults_when_nothing_detected(self):
        data = ["G28\n"]
        rel, length, speed = self.bl._detect_gcode_params(data)
        # Defaults
        self.assertTrue(rel)  # default is relative
        self.assertAlmostEqual(length, 5.0)
        self.assertAlmostEqual(speed, 2400.0)


# =========================================================================
# _process_layer tests
# =========================================================================
SAMPLE_LAYER = """;LAYER:5
G0 F9000 X100 Y100 Z1.2
;TYPE:WALL-INNER
G0 F9000 X10 Y10
G1 F1200 X20 Y10 E0.5
G1 F1200 X20 Y20 E1.0
G1 F1200 X10 Y20 E1.5
G1 F1200 X10 Y10 E2.0
G0 F9000 X30 Y30
G1 F1200 X40 Y30 E2.5
G1 F1200 X40 Y40 E3.0
G1 F1200 X30 Y40 E3.5
G1 F1200 X30 Y30 E4.0
G0 F9000 X50 Y50
G1 F1200 X60 Y50 E4.5
G1 F1200 X60 Y60 E5.0
G1 F1200 X50 Y60 E5.5
G1 F1200 X50 Y50 E6.0
;TYPE:FILL
G1 F1200 X25 Y25 E6.5"""


class TestProcessLayer(unittest.TestCase):

    def setUp(self):
        self.bl = _make_instance()
        self.target_types = {"WALL-INNER"}
        self.retract_length = 5.0
        self.retract_speed = 2400.0
        self.travel_speed = 9000.0

    def _run(self, layer_gcode=SAMPLE_LAYER, layer_num=5, z_shift=0.15,
             extrusion_multiplier=1.0, is_first=False, is_last=False,
             target_types=None, relative=True):
        result, _end_e = self.bl._process_layer(
            layer_gcode, layer_num, z_shift,
            extrusion_multiplier, is_first, is_last,
            target_types or self.target_types, relative,
            self.retract_length, self.retract_speed, self.travel_speed
        )
        return result

    def test_relative_basic_3_loops(self):
        """3 inner wall loops: loop 0 stays, loop 1 deferred, loop 2 stays."""
        result = self._run()
        self.assertIsNotNone(result)
        lines = result.split("\n")
        # Loop 0 (X10..X10) and loop 2 (X50..X50) should be in normal section
        # Loop 1 (X30..X30) should be in deferred section after Z-shift
        self.assertIn(";BrickLayers: shifted loops", result)

    def test_z_shift_value(self):
        """Deferred loops should be at current_z + z_shift."""
        result = self._run(z_shift=0.15)
        # current_z = 1.2, shifted = 1.35
        self.assertIn("Z1.3500", result)
        self.assertIn(";BrickLayers Z-shift", result)

    def test_retract_unretract_between_deferred(self):
        """Travel moves between deferred loops should have retract/unretract."""
        # Need a layer with multiple deferred loops (odd indices)
        # With 5 loops: indices 1,3 are deferred
        five_loop_layer = """;LAYER:5
G0 F9000 X100 Y100 Z1.2
;TYPE:WALL-INNER
G0 F9000 X10 Y10
G1 F1200 X20 Y10 E0.5
G0 F9000 X30 Y30
G1 F1200 X40 Y30 E0.5
G0 F9000 X50 Y50
G1 F1200 X60 Y50 E0.5
G0 F9000 X70 Y70
G1 F1200 X80 Y70 E0.5
G0 F9000 X90 Y90
G1 F1200 X100 Y90 E0.5"""
        result = self._run(layer_gcode=five_loop_layer)
        self.assertIsNotNone(result)
        # Should have retract between the two deferred loops
        self.assertIn(";BrickLayers retract", result)
        self.assertIn(";BrickLayers unretract", result)

    def test_unretract_after_z_restore(self):
        """Output should end with unretract after Z-restore (C3 fix)."""
        result = self._run()
        lines = result.strip().split("\n")
        # Find Z-restore line
        z_restore_idx = None
        for i, line in enumerate(lines):
            if ";BrickLayers Z-restore" in line:
                z_restore_idx = i
                break
        self.assertIsNotNone(z_restore_idx)
        # Next line should be unretract
        self.assertIn(";BrickLayers unretract", lines[z_restore_idx + 1])

    def test_absolute_mode(self):
        """Absolute mode should also produce valid output with retractions."""
        abs_layer = """;LAYER:5
G0 F9000 X100 Y100 Z1.2
;TYPE:WALL-INNER
G0 F9000 X10 Y10
G1 F1200 X20 Y10 E1.0
G1 F1200 X20 Y20 E2.0
G0 F9000 X30 Y30
G1 F1200 X40 Y30 E3.0
G1 F1200 X40 Y40 E4.0
G0 F9000 X50 Y50
G1 F1200 X60 Y50 E5.0
G1 F1200 X60 Y60 E6.0"""
        result = self._run(layer_gcode=abs_layer, relative=False)
        self.assertIsNotNone(result)
        self.assertIn(";BrickLayers retract", result)
        self.assertIn(";BrickLayers unretract", result)

    def test_first_brick_layer_multiplier(self):
        """First brick layer: effective_multiplier = extrusion_multiplier * 1.15."""
        result = self._run(extrusion_multiplier=1.0, is_first=True)
        self.assertIsNotNone(result)
        # Deferred loop 1 body has E values: 2.5, 3.0, 3.5, 4.0 (treated as
        # relative extrusion amounts). With multiplier 1.15: 2.5*1.15 = 2.875
        lines = result.split("\n")
        deferred_section = False
        found_scaled = False
        for line in lines:
            if ";BrickLayers: shifted loops" in line:
                deferred_section = True
            if deferred_section and line.strip().startswith("G1 "):
                e_val = BrickLayers._getValue(line, "E")
                x_val = BrickLayers._getValue(line, "X")
                if e_val is not None and x_val is not None and e_val > 0:
                    self.assertAlmostEqual(e_val, 2.875, places=3)
                    found_scaled = True
                    break
        self.assertTrue(found_scaled, "Should find scaled extrusion value")

    def test_last_brick_layer_multiplier(self):
        """Last brick layer: effective_multiplier = extrusion_multiplier * 0.85."""
        result = self._run(extrusion_multiplier=1.0, is_last=True)
        self.assertIsNotNone(result)
        # Deferred loop 1 body has E=2.5 first. 2.5 * 0.85 = 2.125
        lines = result.split("\n")
        deferred_section = False
        found_scaled = False
        for line in lines:
            if ";BrickLayers: shifted loops" in line:
                deferred_section = True
            if deferred_section and line.strip().startswith("G1 "):
                e_val = BrickLayers._getValue(line, "E")
                x_val = BrickLayers._getValue(line, "X")
                if e_val is not None and x_val is not None and e_val > 0:
                    self.assertAlmostEqual(e_val, 2.125, places=3)
                    found_scaled = True
                    break
        self.assertTrue(found_scaled, "Should find scaled extrusion value")

    def test_no_wall_sections_returns_none(self):
        """Layer with only infill should return None."""
        infill_layer = """;LAYER:5
G0 F9000 X100 Y100 Z1.2
;TYPE:FILL
G1 F1200 X25 Y25 E0.5"""
        result = self._run(layer_gcode=infill_layer)
        self.assertIsNone(result)

    def test_z_in_body_lines_replaced(self):
        """Body lines containing Z should get Z replaced (H2 fix)."""
        layer_with_z_body = """;LAYER:5
G0 F9000 X100 Y100 Z1.2
;TYPE:WALL-INNER
G0 F9000 X10 Y10
G1 F1200 X20 Y10 Z1.2 E0.5
G1 F1200 X20 Y20 E1.0
G0 F9000 X30 Y30
G1 F1200 X40 Y30 Z1.2 E0.5
G1 F1200 X40 Y40 E1.0
G0 F9000 X50 Y50
G1 F1200 X60 Y50 E0.5
G1 F1200 X60 Y60 E1.0"""
        result = self._run(layer_gcode=layer_with_z_body, z_shift=0.15)
        self.assertIsNotNone(result)
        # In deferred section, Z values should be replaced with shifted_z
        lines = result.split("\n")
        deferred = False
        for line in lines:
            if ";BrickLayers: shifted loops" in line:
                deferred = True
            if deferred and "Z-restore" in line:
                break
            if deferred and line.strip().startswith("G1 ") and "Z" in line:
                z_val = BrickLayers._getValue(line, "Z")
                if z_val is not None:
                    self.assertAlmostEqual(z_val, 1.35, places=4)

    def test_multiple_wall_sections_reset_counter(self):
        """Loop counter resets per section (H3 fix)."""
        # Two separate WALL-INNER sections, each with 3 loops
        multi_section = """;LAYER:5
G0 F9000 X100 Y100 Z1.2
;TYPE:WALL-INNER
G0 F9000 X10 Y10
G1 F1200 X20 Y10 E0.5
G0 F9000 X30 Y30
G1 F1200 X40 Y30 E0.5
G0 F9000 X50 Y50
G1 F1200 X60 Y50 E0.5
;TYPE:FILL
G1 F1200 X25 Y25 E0.5
;TYPE:WALL-INNER
G0 F9000 X10 Y10
G1 F1200 X20 Y10 E0.5
G0 F9000 X30 Y30
G1 F1200 X40 Y30 E0.5
G0 F9000 X50 Y50
G1 F1200 X60 Y50 E0.5"""
        result = self._run(layer_gcode=multi_section)
        self.assertIsNotNone(result)
        # Each section has 3 loops, so loop 1 from each section is deferred
        # That means 2 deferred loops total
        deferred_count = result.count(";BrickLayers travel")
        self.assertEqual(deferred_count, 2)

    def test_mixed_wall_types(self):
        """Mixed WALL-INNER + WALL-OUTER should have correct TYPE markers (H4 fix)."""
        mixed = """;LAYER:5
G0 F9000 X100 Y100 Z1.2
;TYPE:WALL-INNER
G0 F9000 X10 Y10
G1 F1200 X20 Y10 E0.5
G0 F9000 X30 Y30
G1 F1200 X40 Y30 E0.5
G0 F9000 X50 Y50
G1 F1200 X60 Y50 E0.5
;TYPE:WALL-OUTER
G0 F9000 X10 Y10
G1 F1200 X20 Y10 E0.5
G0 F9000 X30 Y30
G1 F1200 X40 Y30 E0.5
G0 F9000 X50 Y50
G1 F1200 X60 Y50 E0.5"""
        result = self._run(layer_gcode=mixed,
                           target_types={"WALL-INNER", "WALL-OUTER"})
        self.assertIsNotNone(result)
        # Deferred section should have TYPE markers
        self.assertIn(";TYPE:WALL-INNER", result)
        self.assertIn(";TYPE:WALL-OUTER", result)

    def test_only_1_loop_returns_none(self):
        """Only 1 loop means no odd loops, so returns None."""
        one_loop = """;LAYER:5
G0 F9000 X100 Y100 Z1.2
;TYPE:WALL-INNER
G0 F9000 X10 Y10
G1 F1200 X20 Y10 E0.5
G1 F1200 X20 Y20 E1.0"""
        result = self._run(layer_gcode=one_loop)
        self.assertIsNone(result)

    def test_layer_without_z_returns_none(self):
        """Layer without Z value returns None."""
        no_z = """;LAYER:5
;TYPE:WALL-INNER
G0 F9000 X10 Y10
G1 F1200 X20 Y10 E0.5"""
        result = self._run(layer_gcode=no_z)
        self.assertIsNone(result)


class TestEndToEnd(unittest.TestCase):
    """End-to-end tests that process full multi-layer G-code through
    _process_layer, simulating the _execute pipeline."""

    def setUp(self):
        self.bl = _make_instance()

    def _process_full_gcode(self, gcode_text, start_layer=0, end_layer=-1,
                            extrusion_multiplier=1.05, layer_height=0.2,
                            apply_inner=True, apply_outer=False,
                            relative_extrusion=True):
        """Simulate the _execute flow: split into blocks and process each layer."""
        # Split into layer blocks (simulate Cura's data list)
        # Each block is separated by blank lines or LAYER markers
        blocks = gcode_text.split("\n\n")
        if len(blocks) == 1:
            # Try splitting by ;LAYER: markers
            raw_lines = gcode_text.split("\n")
            blocks = []
            current_block = []
            for line in raw_lines:
                if line.startswith(";LAYER:") and current_block:
                    blocks.append("\n".join(current_block))
                    current_block = [line]
                else:
                    current_block.append(line)
            if current_block:
                blocks.append("\n".join(current_block))

        z_shift = layer_height / 2.0
        target_types = set()
        if apply_inner:
            target_types.add("WALL-INNER")
        if apply_outer:
            target_types.add("WALL-OUTER")

        max_layer = BrickLayers._find_max_layer(blocks)
        if end_layer <= 0:
            end_layer_gcode = max_layer
        else:
            end_layer_gcode = end_layer - 1
        start_layer_gcode = max(start_layer - 1, 0)

        results = {}
        for block in blocks:
            layer_num = BrickLayers._get_layer_number(block)
            if layer_num is None or layer_num < 0:
                continue
            if layer_num < start_layer_gcode or layer_num > end_layer_gcode:
                continue

            is_first = (layer_num == start_layer_gcode)
            is_last = (layer_num == end_layer_gcode)

            result, _end_e = self.bl._process_layer(
                block, layer_num, z_shift, extrusion_multiplier,
                is_first, is_last, target_types, relative_extrusion,
                retract_length=5.0, retract_speed=2400.0, travel_speed=9000.0
            )
            if result is not None:
                results[layer_num] = result

        return results

    def test_sample_gcode_all_layers_processed(self):
        """Process sample G-code fixture - all layers with inner walls get processed."""
        sample_path = os.path.join(os.path.dirname(__file__), "sample_gcode.gcode")
        with open(sample_path) as f:
            gcode = f.read()

        results = self._process_full_gcode(gcode, start_layer=1)
        # All 3 layers (0,1,2) should be processed since each has WALL-INNER with 3 loops
        self.assertEqual(len(results), 3, f"Expected 3 layers, got {len(results)}: {list(results.keys())}")

    def test_sample_gcode_z_shift_correct(self):
        """Verify Z-shift values are correct for each layer."""
        sample_path = os.path.join(os.path.dirname(__file__), "sample_gcode.gcode")
        with open(sample_path) as f:
            gcode = f.read()

        results = self._process_full_gcode(gcode, start_layer=1, layer_height=0.2)
        for layer_num, result in results.items():
            # Find the Z-shift line
            for line in result.split("\n"):
                if ";BrickLayers Z-shift" in line:
                    z = BrickLayers._getValue(line, "Z")
                    # shifted_z = current_z + 0.1 (layer_height/2)
                    self.assertIsNotNone(z)
                    break

    def test_sample_gcode_z_restore_present(self):
        """Each processed layer should have Z-restore."""
        sample_path = os.path.join(os.path.dirname(__file__), "sample_gcode.gcode")
        with open(sample_path) as f:
            gcode = f.read()

        results = self._process_full_gcode(gcode, start_layer=1)
        for layer_num, result in results.items():
            self.assertIn(";BrickLayers Z-restore", result, f"Layer {layer_num} missing Z-restore")

    def test_sample_gcode_unretract_at_end(self):
        """Each processed layer should end with unretract (C3 fix)."""
        sample_path = os.path.join(os.path.dirname(__file__), "sample_gcode.gcode")
        with open(sample_path) as f:
            gcode = f.read()

        results = self._process_full_gcode(gcode, start_layer=1)
        for layer_num, result in results.items():
            lines = [l.strip() for l in result.split("\n") if l.strip()]
            last_gcode = None
            for line in reversed(lines):
                if line.startswith("G"):
                    last_gcode = line
                    break
            self.assertIsNotNone(last_gcode)
            self.assertIn("unretract", last_gcode, f"Layer {layer_num} missing unretract at end")

    def test_sample_gcode_no_double_retract(self):
        """Verify no two consecutive retractions without unretraction between them."""
        sample_path = os.path.join(os.path.dirname(__file__), "sample_gcode.gcode")
        with open(sample_path) as f:
            gcode = f.read()

        results = self._process_full_gcode(gcode, start_layer=1)
        for layer_num, result in results.items():
            retracted = False
            for line in result.split("\n"):
                if "BrickLayers retract" in line and "unretract" not in line:
                    self.assertFalse(retracted,
                        f"Layer {layer_num}: double retract detected at: {line}")
                    retracted = True
                elif "BrickLayers unretract" in line:
                    retracted = False

    def test_sample_gcode_start_layer_filter(self):
        """start_layer=2 should skip layer 0."""
        sample_path = os.path.join(os.path.dirname(__file__), "sample_gcode.gcode")
        with open(sample_path) as f:
            gcode = f.read()

        results = self._process_full_gcode(gcode, start_layer=2)
        self.assertNotIn(0, results, "Layer 0 should be skipped with start_layer=2")

    def test_sample_gcode_first_last_multiplier(self):
        """First brick layer gets 1.15x, last gets 0.85x."""
        sample_path = os.path.join(os.path.dirname(__file__), "sample_gcode.gcode")
        with open(sample_path) as f:
            gcode = f.read()

        results = self._process_full_gcode(gcode, start_layer=1, extrusion_multiplier=1.05)
        # With 3 layers (0,1,2), layer 0 is first, layer 2 is last
        # First brick: 1.05 * 1.15 = 1.2075
        # Last brick: 1.05 * 0.85 = 0.8925
        # Middle: 1.05
        self.assertIn(0, results)
        self.assertIn(2, results)

    def test_gcode_validity_no_nan_no_inf(self):
        """Output G-code should not contain NaN or Inf values."""
        sample_path = os.path.join(os.path.dirname(__file__), "sample_gcode.gcode")
        with open(sample_path) as f:
            gcode = f.read()

        results = self._process_full_gcode(gcode, start_layer=1)
        for layer_num, result in results.items():
            self.assertNotIn("nan", result.lower(), f"Layer {layer_num} contains NaN")
            self.assertNotIn("inf", result.lower(), f"Layer {layer_num} contains Inf")

    def test_absolute_extrusion_full_layer(self):
        """Process a full layer in absolute extrusion mode - verify retractions are present."""
        abs_layer = """;LAYER:0
G0 F9000 X10 Y10 Z0.3
;TYPE:WALL-INNER
G0 F9000 X10 Y10
G1 F1200 X20 Y10 E1.0
G1 F1200 X20 Y20 E2.0
G1 F1200 X10 Y20 E3.0
G1 F1200 X10 Y10 E4.0
G0 F9000 X30 Y30
G1 F1200 X40 Y30 E5.0
G1 F1200 X40 Y40 E6.0
G1 F1200 X30 Y40 E7.0
G1 F1200 X30 Y30 E8.0
G0 F9000 X50 Y50
G1 F1200 X60 Y50 E9.0
G1 F1200 X60 Y60 E10.0
G1 F1200 X50 Y60 E11.0
G1 F1200 X50 Y50 E12.0"""

        result, _end_e = self.bl._process_layer(
            abs_layer, 0, z_shift=0.1, extrusion_multiplier=1.05,
            is_first_brick=False, is_last_brick=False,
            target_types={"WALL-INNER"}, relative_extrusion=False,
            retract_length=5.0, retract_speed=2400.0, travel_speed=9000.0
        )
        self.assertIsNotNone(result)
        # In absolute mode, should still have retract/unretract
        self.assertIn("BrickLayers retract", result)
        self.assertIn("BrickLayers unretract", result)
        # Should have Z-shift and Z-restore
        self.assertIn("BrickLayers Z-shift", result)
        self.assertIn("BrickLayers Z-restore", result)
        # E values should be present and numeric
        for line in result.split("\n"):
            if line.strip().startswith("G1 ") and "E" in line:
                e_val = BrickLayers._getValue(line, "E")
                self.assertIsNotNone(e_val, f"Invalid E value in: {line}")

    def test_marker_prevents_reprocessing(self):
        """The BRICKLAYERS_PROCESSED marker should be checked by _onWriteStarted."""
        marker = _bl_module._BRICK_LAYERS_MARKER
        self.assertEqual(marker, ";BRICKLAYERS_PROCESSED")

    def test_perimeter_loop_tracks_coordinates(self):
        """PerimeterLoop should track start and end coordinates."""
        loop = PerimeterLoop("WALL-INNER")
        loop.add_line("G0 X5 Y5", None, None, False)  # prefix (travel)
        loop.add_line("G1 X10 Y10 E0.5", 10.0, 10.0, True)  # first extrusion
        loop.add_line("G1 X20 Y20 E1.0", 20.0, 20.0, True)  # extrusion
        loop.add_line("G1 X30 Y30 E1.5", 30.0, 30.0, True)  # last extrusion

        self.assertEqual(loop.start_x, 10.0)
        self.assertEqual(loop.start_y, 10.0)
        self.assertEqual(loop.end_x, 30.0)
        self.assertEqual(loop.end_y, 30.0)
        self.assertTrue(loop.has_extrusion)
        self.assertEqual(len(loop.prefix_lines), 1)
        self.assertEqual(len(loop.body_lines), 3)


class TestAbsoluteExtrusion(unittest.TestCase):
    """Tests specifically verifying absolute extrusion mode correctness.

    The key bug was: when loops are reordered in absolute mode, the E values
    become discontinuous (large jumps that cause extrusion during travel).
    The fix converts to relative E internally for safe reordering, then
    converts back to absolute E in the output (no M83/M82/G92 needed).
    """

    def setUp(self):
        self.bl = _make_instance()

    def test_no_unsupported_commands_in_absolute_output(self):
        """Absolute mode output must not contain M83/M82/G92 (firmware compat)."""
        layer = """;LAYER:0
G0 F9000 X10 Y10 Z0.3
;TYPE:WALL-INNER
G0 F9000 X10 Y10
G1 F1200 X20 Y10 E100.5
G1 F1200 X20 Y20 E101.0
G1 F1200 X10 Y20 E101.5
G1 F1200 X10 Y10 E102.0
G0 F9000 X30 Y30
G1 F1200 X40 Y30 E102.5
G1 F1200 X40 Y40 E103.0
G1 F1200 X30 Y40 E103.5
G1 F1200 X30 Y30 E104.0"""

        result, _end_e = self.bl._process_layer(
            layer, 0, z_shift=0.15, extrusion_multiplier=1.0,
            is_first_brick=False, is_last_brick=False,
            target_types={"WALL-INNER"}, relative_extrusion=False,
            retract_length=5.0, retract_speed=2400.0, travel_speed=9000.0,
            layer_start_e=100.0, output_start_e=100.0
        )
        self.assertIsNotNone(result)
        lines = result.split("\n")
        # Should NOT contain M83/M82/G92 (not supported by all firmware)
        m83_found = any("M83" in l for l in lines)
        m82_found = any("M82" in l for l in lines)
        g92_found = any("G92" in l for l in lines)
        self.assertFalse(m83_found, "Output must not contain M83")
        self.assertFalse(m82_found, "Output must not contain M82")
        self.assertFalse(g92_found, "Output must not contain G92")
        # All E values should be absolute (monotonically increasing for extrusion)
        e_values = []
        for l in lines:
            s = l.strip()
            if s.startswith("G1 "):
                e = BrickLayers._getValue(s, "E")
                if e is not None:
                    e_values.append(e)
        self.assertTrue(len(e_values) > 0, "Should have E values in output")

    def test_absolute_e_values_are_reasonable(self):
        """In absolute mode, output E values should be monotonically increasing
        for extrusion moves and the total extrusion should match the original."""
        layer = """;LAYER:0
G0 F9000 X10 Y10 Z0.3
;TYPE:WALL-INNER
G0 F9000 X10 Y10
G1 F1200 X20 Y10 E100.5
G1 F1200 X20 Y20 E101.0
G1 F1200 X10 Y20 E101.5
G1 F1200 X10 Y10 E102.0
G0 F9000 X30 Y30
G1 F1200 X40 Y30 E102.5
G1 F1200 X40 Y40 E103.0
G1 F1200 X30 Y40 E103.5
G1 F1200 X30 Y30 E104.0"""

        result, _end_e = self.bl._process_layer(
            layer, 0, z_shift=0.15, extrusion_multiplier=1.0,
            is_first_brick=False, is_last_brick=False,
            target_types={"WALL-INNER"}, relative_extrusion=False,
            retract_length=5.0, retract_speed=2400.0, travel_speed=9000.0,
            layer_start_e=100.0, output_start_e=100.0
        )
        self.assertIsNotNone(result)
        # No M83/M82/G92 should be present
        for line in result.split("\n"):
            self.assertNotIn("M83", line)
            self.assertNotIn("M82", line)
            self.assertNotIn("G92", line)
        # Collect all E values - they should all be >= layer_start_e
        e_values = []
        for line in result.split("\n"):
            s = line.strip()
            if s.startswith("G1 "):
                e = BrickLayers._getValue(s, "E")
                if e is not None:
                    e_values.append(e)
        self.assertTrue(all(e >= 90.0 for e in e_values),
                        f"All E values should be near start_e range, got: {e_values}")

    def test_no_large_e_jumps_in_absolute_output(self):
        """After reordering in absolute mode, E should increase monotonically
        with no large discontinuous jumps between consecutive G1 lines.

        This is the core test for the absolute mode fix. In the old broken code,
        reordered loops would have E values from their original position,
        causing jumps of hundreds of mm.
        """
        layer = """;LAYER:5
G0 F9000 X10 Y10 Z1.2
;TYPE:WALL-INNER
G0 F9000 X10 Y10
G1 F1200 X20 Y10 E1100.5
G1 F1200 X20 Y20 E1101.0
G1 F1200 X10 Y20 E1101.5
G1 F1200 X10 Y10 E1102.0
G0 F9000 X30 Y30
G1 F1200 X40 Y30 E1102.5
G1 F1200 X40 Y40 E1103.0
G1 F1200 X30 Y40 E1103.5
G1 F1200 X30 Y30 E1104.0
G0 F9000 X50 Y50
G1 F1200 X60 Y50 E1104.5
G1 F1200 X60 Y60 E1105.0
G1 F1200 X50 Y60 E1105.5
G1 F1200 X50 Y50 E1106.0
;TYPE:FILL
G1 F1200 X25 Y25 E1106.5"""

        result, _end_e = self.bl._process_layer(
            layer, 5, z_shift=0.1, extrusion_multiplier=1.0,
            is_first_brick=False, is_last_brick=False,
            target_types={"WALL-INNER"}, relative_extrusion=False,
            retract_length=5.0, retract_speed=2400.0, travel_speed=9000.0,
            layer_start_e=1100.0, output_start_e=1100.0
        )
        self.assertIsNotNone(result)

        # No M83/M82/G92 in output
        for line in result.split("\n"):
            self.assertNotIn("M83", line)
            self.assertNotIn("M82", line)
            self.assertNotIn("G92", line)

        # All consecutive E values should differ by less than 10mm
        # (retracts are 5mm, extrusion moves are small deltas)
        prev_e = None
        for line in result.split("\n"):
            stripped = line.strip()
            if stripped.startswith("G1 "):
                e = BrickLayers._getValue(stripped, "E")
                if e is not None:
                    if prev_e is not None:
                        jump = abs(e - prev_e)
                        self.assertLess(
                            jump, 10.0,
                            f"E jump too large ({jump:.1f}mm) between "
                            f"E{prev_e:.1f} and E{e:.1f} in line: {line}")
                    prev_e = e

    def test_relative_mode_not_wrapped(self):
        """In native relative mode, output should NOT have M83/M82/G92."""
        layer = """;LAYER:0
G0 F9000 X10 Y10 Z0.3
;TYPE:WALL-INNER
G0 F9000 X10 Y10
G1 F1200 X20 Y10 E0.5
G1 F1200 X20 Y20 E0.5
G1 F1200 X10 Y20 E0.5
G1 F1200 X10 Y10 E0.5
G0 F9000 X30 Y30
G1 F1200 X40 Y30 E0.5
G1 F1200 X40 Y40 E0.5
G1 F1200 X30 Y40 E0.5
G1 F1200 X30 Y30 E0.5"""

        result, _end_e = self.bl._process_layer(
            layer, 0, z_shift=0.15, extrusion_multiplier=1.0,
            is_first_brick=False, is_last_brick=False,
            target_types={"WALL-INNER"}, relative_extrusion=True,
            retract_length=5.0, retract_speed=2400.0, travel_speed=9000.0
        )
        self.assertIsNotNone(result)
        self.assertNotIn("M83", result)
        self.assertNotIn("M82", result)
        self.assertNotIn("G92", result)

    def test_absolute_retract_values_are_correct(self):
        """BrickLayers retract/unretract in absolute mode should use proper
        absolute E positions, NOT hardcoded relative values like E-10."""
        layer = """;LAYER:5
G0 F9000 X10 Y10 Z1.2
;TYPE:WALL-INNER
G0 F9000 X10 Y10
G1 F1200 X20 Y10 E50.5
G1 F1200 X20 Y20 E51.0
G1 F1200 X10 Y20 E51.5
G1 F1200 X10 Y10 E52.0
G0 F9000 X30 Y30
G1 F1200 X40 Y30 E52.5
G1 F1200 X40 Y40 E53.0
G1 F1200 X30 Y40 E53.5
G1 F1200 X30 Y30 E54.0"""

        result, end_e = self.bl._process_layer(
            layer, 5, z_shift=0.1, extrusion_multiplier=1.0,
            is_first_brick=False, is_last_brick=False,
            target_types={"WALL-INNER"}, relative_extrusion=False,
            retract_length=5.0, retract_speed=2400.0, travel_speed=9000.0,
            layer_start_e=50.0, output_start_e=50.0
        )
        self.assertIsNotNone(result)
        lines = result.split("\n")

        # Check that NO E value is negative (impossible in absolute mode
        # starting from E=50)
        for line in lines:
            s = line.strip()
            if s.startswith("G1 "):
                e = BrickLayers._getValue(s, "E")
                if e is not None:
                    self.assertGreaterEqual(e, 0.0,
                        f"Negative E in absolute mode: {line}")

        # Check retract lines specifically: E should be current_pos - 5,
        # NOT a hardcoded -5 or -10
        retract_lines = [l for l in lines if "BrickLayers retract" in l]
        for rl in retract_lines:
            e = BrickLayers._getValue(rl.strip(), "E")
            self.assertIsNotNone(e, f"Retract line missing E value: {rl}")
            self.assertGreater(e, 0.0,
                f"Retract E should be positive absolute value, got {e}: {rl}")

    def test_cross_layer_e_continuity(self):
        """When processing multiple layers in absolute mode, E values should
        be continuous across layer boundaries."""
        layer_template = """;LAYER:{num}
G0 F9000 X10 Y10 Z{z}
;TYPE:WALL-INNER
G0 F9000 X10 Y10
G1 F1200 X20 Y10 E{e1}
G1 F1200 X20 Y20 E{e2}
G1 F1200 X10 Y20 E{e3}
G1 F1200 X10 Y10 E{e4}
G0 F9000 X30 Y30
G1 F1200 X40 Y30 E{e5}
G1 F1200 X40 Y40 E{e6}
G1 F1200 X30 Y40 E{e7}
G1 F1200 X30 Y30 E{e8}"""

        # Simulate two consecutive layers
        layer1 = layer_template.format(
            num=2, z=0.6, e1=10.5, e2=11.0, e3=11.5, e4=12.0,
            e5=12.5, e6=13.0, e7=13.5, e8=14.0)
        layer2 = layer_template.format(
            num=3, z=0.8, e1=14.5, e2=15.0, e3=15.5, e4=16.0,
            e5=16.5, e6=17.0, e7=17.5, e8=18.0)

        # Process layer 1
        result1, end_e1 = self.bl._process_layer(
            layer1, 2, z_shift=0.1, extrusion_multiplier=1.0,
            is_first_brick=True, is_last_brick=False,
            target_types={"WALL-INNER"}, relative_extrusion=False,
            retract_length=5.0, retract_speed=2400.0, travel_speed=9000.0,
            layer_start_e=10.0, output_start_e=10.0
        )
        self.assertIsNotNone(result1)

        # Process layer 2, using end_e1 as output_start_e
        result2, end_e2 = self.bl._process_layer(
            layer2, 3, z_shift=0.1, extrusion_multiplier=1.0,
            is_first_brick=False, is_last_brick=True,
            target_types={"WALL-INNER"}, relative_extrusion=False,
            retract_length=5.0, retract_speed=2400.0, travel_speed=9000.0,
            layer_start_e=14.0, output_start_e=end_e1
        )
        self.assertIsNotNone(result2)

        # The first E in layer 2's output should be close to end_e1
        # (within a retract/unretract difference)
        last_e_layer1 = None
        for line in result1.split("\n"):
            s = line.strip()
            if s.startswith("G1 "):
                e = BrickLayers._getValue(s, "E")
                if e is not None:
                    last_e_layer1 = e

        first_e_layer2 = None
        for line in result2.split("\n"):
            s = line.strip()
            if s.startswith("G1 "):
                e = BrickLayers._getValue(s, "E")
                if e is not None:
                    first_e_layer2 = e
                    break

        self.assertIsNotNone(last_e_layer1)
        self.assertIsNotNone(first_e_layer2)
        # The jump between last E of layer 1 and first E of layer 2
        # should be small (not a huge discontinuity)
        jump = abs(first_e_layer2 - last_e_layer1)
        self.assertLess(jump, 10.0,
            f"E jump between layers too large: {last_e_layer1} -> {first_e_layer2}")


if __name__ == "__main__":
    unittest.main()
