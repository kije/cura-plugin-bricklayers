"""Tests for the Griffin-aware G-code linter.

Validates both the linter's error detection and that BrickLayers-relevant
G-code patterns are correctly flagged or accepted.
"""

import os
import pytest

import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gcode_lint import GCodeLinter, GCodeDialect, Severity, LintError


@pytest.fixture
def marlin_linter():
    return GCodeLinter(dialect=GCodeDialect.MARLIN, bed_size_x=200, bed_size_y=200, max_z=200)


@pytest.fixture
def griffin_linter():
    return GCodeLinter(dialect=GCodeDialect.GRIFFIN, bed_size_x=230, bed_size_y=190, max_z=300)


@pytest.fixture
def auto_linter():
    return GCodeLinter(dialect=GCodeDialect.AUTO)


# ---------------------------------------------------------------------------
# Dialect detection
# ---------------------------------------------------------------------------

class TestDialectDetection:
    def test_detects_marlin_flavor(self, auto_linter):
        gcode = ";FLAVOR:Marlin\nG28\nG1 X10 Y10 E1\n"
        errors = auto_linter.lint(gcode)
        # Should not produce Griffin-specific warnings
        assert not any(e.code.startswith("GRIFFIN") for e in errors)

    def test_detects_griffin_flavor(self, auto_linter):
        gcode = ";FLAVOR:Griffin\nG28\nM82\nG1 X10 Y10 E1\n"
        errors = auto_linter.lint(gcode)
        assert any(e.code == "GRIFFIN_M82" for e in errors)

    def test_defaults_to_marlin(self, auto_linter):
        gcode = "G28\nM82\nG1 X10 Y10 E1\n"
        errors = auto_linter.lint(gcode)
        assert not any(e.code.startswith("GRIFFIN") for e in errors)


# ---------------------------------------------------------------------------
# E-value checks
# ---------------------------------------------------------------------------

class TestEValueValidation:
    def test_valid_absolute_e_monotonic(self, marlin_linter):
        gcode = "G28\nM82\nG92 E0\nG1 X10 E1\nG1 X20 E2\nG1 X30 E3\n"
        errors = marlin_linter.lint(gcode)
        assert not any(e.code == "E_JUMP_NEG" for e in errors)

    def test_large_negative_e_jump_absolute(self, marlin_linter):
        gcode = "G28\nM82\nG92 E0\nG1 X10 E100\nG1 X20 E50\n"
        errors = marlin_linter.lint(gcode)
        assert any(e.code == "E_JUMP_NEG" for e in errors)

    def test_small_retraction_ok(self, marlin_linter):
        gcode = "G28\nM82\nG92 E0\nG1 X10 E5\nG1 E0\n"
        errors = marlin_linter.lint(gcode)
        # Small retraction (-5mm) should not trigger E_JUMP_NEG
        assert not any(e.code == "E_JUMP_NEG" for e in errors)

    def test_excessive_extrusion_per_move(self, marlin_linter):
        gcode = "G28\nM82\nG92 E0\nG1 X10 E100\n"
        errors = marlin_linter.lint(gcode)
        assert any(e.code == "E_EXCESSIVE" for e in errors)

    def test_relative_extrusion_excessive(self, marlin_linter):
        gcode = "G28\nM83\nG1 X10 E100\n"
        errors = marlin_linter.lint(gcode)
        assert any(e.code == "E_EXCESSIVE" for e in errors)

    def test_relative_extrusion_normal(self, marlin_linter):
        gcode = "G28\nM83\nG1 X10 E1.5\nG1 X20 E1.5\n"
        errors = marlin_linter.lint(gcode)
        assert not any(e.code == "E_EXCESSIVE" for e in errors)


# ---------------------------------------------------------------------------
# Z-value checks
# ---------------------------------------------------------------------------

class TestZValidation:
    def test_z_below_bed(self, marlin_linter):
        gcode = "G28\nG1 Z-1.0\n"
        errors = marlin_linter.lint(gcode)
        assert any(e.code == "Z_NEGATIVE" for e in errors)

    def test_z_exceeds_max(self, marlin_linter):
        gcode = "G28\nG1 Z250\n"
        errors = marlin_linter.lint(gcode)
        assert any(e.code == "Z_BOUNDS" for e in errors)

    def test_valid_z_range(self, marlin_linter):
        gcode = "G28\nG1 Z0.2\nG1 Z0.4\nG1 Z100\n"
        errors = marlin_linter.lint(gcode)
        assert not any(e.code in ("Z_NEGATIVE", "Z_BOUNDS") for e in errors)


# ---------------------------------------------------------------------------
# XY bounds
# ---------------------------------------------------------------------------

class TestXYBounds:
    def test_x_out_of_bounds(self, marlin_linter):
        gcode = "G28\nG1 X250 Y10\n"
        errors = marlin_linter.lint(gcode)
        assert any(e.code == "X_BOUNDS" for e in errors)

    def test_y_out_of_bounds(self, marlin_linter):
        gcode = "G28\nG1 X10 Y250\n"
        errors = marlin_linter.lint(gcode)
        assert any(e.code == "Y_BOUNDS" for e in errors)

    def test_valid_xy(self, marlin_linter):
        gcode = "G28\nG1 X100 Y100\n"
        errors = marlin_linter.lint(gcode)
        assert not any(e.code in ("X_BOUNDS", "Y_BOUNDS") for e in errors)


# ---------------------------------------------------------------------------
# Tool change validation (dual extruder / BrickLayers-critical)
# ---------------------------------------------------------------------------

class TestToolChanges:
    def test_tool_change_outside_wall_ok(self, marlin_linter):
        gcode = "G28\n;TYPE:FILL\nT1\nG1 X10 E1\n"
        errors = marlin_linter.lint(gcode)
        assert not any(e.code == "TOOL_IN_WALL" for e in errors)

    def test_tool_change_inside_wall_error(self, marlin_linter):
        gcode = "G28\n;TYPE:WALL-INNER\nG1 X10 E1\nT1\n"
        errors = marlin_linter.lint(gcode)
        assert any(e.code == "TOOL_IN_WALL" for e in errors)

    def test_tool_change_inside_outer_wall_error(self, marlin_linter):
        gcode = "G28\n;TYPE:WALL-OUTER\nG1 X10 E1\nT1\n"
        errors = marlin_linter.lint(gcode)
        assert any(e.code == "TOOL_IN_WALL" for e in errors)

    def test_tool_index_out_of_range(self, marlin_linter):
        gcode = "G28\nT9\n"
        errors = marlin_linter.lint(gcode)
        assert any(e.code == "TOOL_RANGE" for e in errors)


# ---------------------------------------------------------------------------
# Griffin-specific checks
# ---------------------------------------------------------------------------

class TestGriffinDialect:
    def test_m82_warning_in_griffin(self, griffin_linter):
        gcode = ";FLAVOR:Griffin\nG28\nM82\n"
        errors = griffin_linter.lint(gcode)
        assert any(e.code == "GRIFFIN_M82" for e in errors)

    def test_m83_warning_in_griffin(self, griffin_linter):
        gcode = ";FLAVOR:Griffin\nG28\nM83\n"
        errors = griffin_linter.lint(gcode)
        assert any(e.code == "GRIFFIN_M83" for e in errors)

    def test_no_m82_m83_warnings_in_marlin(self, marlin_linter):
        gcode = "G28\nM82\nM83\n"
        errors = marlin_linter.lint(gcode)
        assert not any(e.code.startswith("GRIFFIN") for e in errors)


# ---------------------------------------------------------------------------
# Temperature checks
# ---------------------------------------------------------------------------

class TestTemperature:
    def test_extreme_hotend_temp(self, marlin_linter):
        gcode = "G28\nM104 S400\n"
        errors = marlin_linter.lint(gcode)
        assert any(e.code == "TEMP_HIGH" for e in errors)

    def test_extreme_bed_temp(self, marlin_linter):
        gcode = "G28\nM140 S200\n"
        errors = marlin_linter.lint(gcode)
        assert any(e.code == "BED_TEMP_HIGH" for e in errors)

    def test_normal_temps_ok(self, marlin_linter):
        gcode = "G28\nM104 S200\nM140 S60\n"
        errors = marlin_linter.lint(gcode)
        assert not any(e.code in ("TEMP_HIGH", "BED_TEMP_HIGH") for e in errors)


# ---------------------------------------------------------------------------
# Position continuity (BrickLayers-critical)
# ---------------------------------------------------------------------------

class TestPositionContinuity:
    def test_large_xy_jump_during_extrusion(self, marlin_linter):
        gcode = "G28\nM83\nG1 X10 Y10 E1\nG1 X180 Y180 E1\n"
        errors = marlin_linter.lint(gcode)
        assert any(e.code == "XY_JUMP" for e in errors)

    def test_normal_extrusion_move(self, marlin_linter):
        gcode = "G28\nM83\nG1 X10 Y10 E1\nG1 X12 Y12 E1\n"
        errors = marlin_linter.lint(gcode)
        assert not any(e.code == "XY_JUMP" for e in errors)


# ---------------------------------------------------------------------------
# Sample G-code validation
# ---------------------------------------------------------------------------

class TestSampleGCode:
    """Validate the existing sample G-code file passes the linter."""

    def test_sample_gcode_no_errors(self):
        sample = os.path.join(os.path.dirname(__file__), "sample_gcode.gcode")
        if not os.path.exists(sample):
            pytest.skip("sample_gcode.gcode not found")
        linter = GCodeLinter(dialect=GCodeDialect.AUTO, bed_size_x=200, bed_size_y=200)
        errors = linter.lint_file(sample)
        error_list = [e for e in errors if e.severity == Severity.ERROR]
        assert error_list == [], f"Errors in sample G-code: {[(e.line, e.code, e.message) for e in error_list]}"


# ---------------------------------------------------------------------------
# BrickLayers-specific G-code patterns
# ---------------------------------------------------------------------------

class TestBrickLayersPatterns:
    """Test G-code patterns that BrickLayers would produce."""

    def test_shifted_z_within_layer_ok(self, marlin_linter):
        """BrickLayers shifts odd walls by half layer height — this should be valid."""
        gcode = (
            "G28\nM83\n"
            ";LAYER:1\n"
            "G0 Z0.4\n"
            ";TYPE:WALL-OUTER\n"
            "G1 X10 Y10 E1\nG1 X50 Y10 E1\n"
            ";TYPE:WALL-INNER\n"
            "G0 X12 Y12 Z0.4\n"  # normal Z
            "G1 X48 Y12 E1\n"
            "G0 X14 Y14 Z0.5\n"  # shifted Z (+0.1 = half of 0.2 layer height)
            "G1 X46 Y14 E1\n"
        )
        errors = marlin_linter.lint(gcode)
        error_list = [e for e in errors if e.severity == Severity.ERROR]
        assert error_list == []

    def test_dual_extruder_clean_tool_change(self, marlin_linter):
        """Tool changes between wall sections, not inside them."""
        gcode = (
            "G28\nM83\n"
            ";LAYER:0\n"
            "G0 Z0.3\n"
            ";TYPE:WALL-INNER\n"
            "G1 X10 Y10 E1\n"
            ";TYPE:SUPPORT\n"
            "T1\n"  # tool change outside wall section
            "G1 X20 Y20 E1\n"
            "T0\n"
            ";TYPE:WALL-INNER\n"
            "G1 X30 Y30 E1\n"
        )
        errors = marlin_linter.lint(gcode)
        assert not any(e.code == "TOOL_IN_WALL" for e in errors)

    def test_e_continuity_across_shifted_layers(self, marlin_linter):
        """Absolute E should remain monotonic across BrickLayers-modified layers."""
        gcode = (
            "G28\nM82\nG92 E0\n"
            ";LAYER:0\nG0 Z0.3\n"
            ";TYPE:WALL-INNER\n"
            "G1 X10 E1\nG1 X20 E2\n"
            ";LAYER:1\nG0 Z0.5\n"
            ";TYPE:WALL-INNER\n"
            "G1 X10 E3\nG1 X20 E4\n"  # E continues monotonically
            ";LAYER:2\nG0 Z0.7\n"
            ";TYPE:WALL-INNER\n"
            "G1 X10 E5\nG1 X20 E6\n"
        )
        errors = marlin_linter.lint(gcode)
        assert not any(e.code == "E_JUMP_NEG" for e in errors)

    def test_e_drift_detected(self, marlin_linter):
        """E-value going backwards in absolute mode = BrickLayers bug."""
        gcode = (
            "G28\nM82\nG92 E0\n"
            ";LAYER:0\nG0 Z0.3\n"
            "G1 X10 E5\nG1 X20 E10\n"
            ";LAYER:1\nG0 Z0.5\n"
            "G1 X10 E-15\n"  # large backward jump = E drift bug
        )
        errors = marlin_linter.lint(gcode)
        assert any(e.code == "E_JUMP_NEG" for e in errors)

    def test_griffin_no_m82_m83(self, griffin_linter):
        """Griffin G-code from BrickLayers should NOT contain M82/M83."""
        gcode = (
            ";FLAVOR:Griffin\n"
            "G28\nG92 E0\n"
            ";LAYER:0\nG0 Z0.3\n"
            ";TYPE:WALL-INNER\n"
            "G1 X10 E1\nG1 X20 E2\n"
        )
        errors = griffin_linter.lint(gcode)
        assert not any(e.code.startswith("GRIFFIN") for e in errors)
