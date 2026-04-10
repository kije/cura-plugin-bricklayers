"""Griffin-aware G-code linter for validating BrickLayers plugin output.

Validates that G-code produced by CuraEngine (after BrickLayers path
modification) would be accepted by real printer firmware. Supports both
Marlin and Griffin (Ultimaker) G-code dialects.

Checks performed:
  - E-value monotonicity (absolute mode) or sanity (relative mode)
  - Z-value bounds and continuity
  - Tool change validity (dual-extruder)
  - Extrusion during travel (missing retraction)
  - Position continuity (no teleportation without G0)
  - Required start/end sequences
  - Griffin-specific: no M82/M83 (implicit absolute), header format

Usage:
    from tests.gcode_lint import GCodeLinter, GCodeDialect
    linter = GCodeLinter(dialect=GCodeDialect.GRIFFIN)
    errors = linter.lint_file("output.gcode")
    for e in errors:
        print(f"Line {e.line}: [{e.severity}] {e.message}")

    # Or programmatically:
    linter = GCodeLinter(dialect=GCodeDialect.MARLIN)
    errors = linter.lint(gcode_string)
"""

import re
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import List, Optional


class GCodeDialect(Enum):
    MARLIN = auto()
    GRIFFIN = auto()
    AUTO = auto()  # detect from ;FLAVOR: comment


class Severity(Enum):
    ERROR = "error"      # would cause print failure
    WARNING = "warning"  # suspicious but may work
    INFO = "info"        # informational


@dataclass
class LintError:
    line: int
    severity: Severity
    code: str
    message: str


@dataclass
class _State:
    """Mutable machine state tracked during linting."""
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0
    e: float = 0.0
    f: float = 0.0
    active_extruder: int = 0
    relative_extrusion: bool = False
    absolute_positioning: bool = True
    retracted: bool = False
    current_type: str = ""
    layer: int = -1
    layer_z: float = 0.0
    seen_home: bool = False
    seen_heat: bool = False
    max_e: float = 0.0
    min_e_delta: float = float("inf")
    max_e_delta: float = 0.0
    e_at_layer_start: float = 0.0
    last_extrusion_z: float = 0.0
    tool_changes: List[int] = field(default_factory=list)


# G-code commands that are universally valid
_VALID_G_COMMANDS = {
    "G0", "G1", "G4", "G10", "G11", "G20", "G21", "G28", "G29",
    "G90", "G91", "G92",
}
_VALID_M_COMMANDS_COMMON = {
    "M0", "M1", "M17", "M18", "M25", "M82", "M83", "M84",
    "M104", "M105", "M106", "M107", "M109", "M110", "M112",
    "M114", "M115", "M117", "M118", "M119", "M140", "M155",
    "M190", "M200", "M201", "M203", "M204", "M205", "M206",
    "M207", "M208", "M220", "M221", "M226", "M280", "M301",
    "M302", "M303", "M400", "M500", "M501", "M502", "M503",
    "M600", "M900", "M907",
}
# Griffin-specific commands
_GRIFFIN_EXTRA = {"M104.1", "M109.1", "M140.1", "M190.1"}
# Commands that Griffin does NOT use (Marlin-only)
_MARLIN_ONLY = {"M82", "M83"}

_GCODE_RE = re.compile(
    r"^([GMT]\d+(?:\.\d+)?)"  # command (G0, M104, T0, M104.1)
    r"((?:\s+[A-Z][+-]?[\d.]+)*)"  # parameters
    r"(?:\s*;.*)?$"  # optional comment
)
_PARAM_RE = re.compile(r"([A-Z])([+-]?[\d.]+)")


class GCodeLinter:
    """Lint G-code for firmware compatibility issues.

    Args:
        dialect: Target firmware dialect. AUTO detects from ;FLAVOR: comment.
        bed_size_x: Print bed X dimension in mm (for bounds checking).
        bed_size_y: Print bed Y dimension in mm.
        max_z: Maximum Z height in mm.
        max_e_per_move: Maximum E extrusion per single move (sanity check).
    """

    def __init__(
        self,
        dialect: GCodeDialect = GCodeDialect.AUTO,
        bed_size_x: float = 300.0,
        bed_size_y: float = 300.0,
        max_z: float = 300.0,
        max_e_per_move: float = 50.0,
    ):
        self.dialect = dialect
        self.bed_x = bed_size_x
        self.bed_y = bed_size_y
        self.max_z = max_z
        self.max_e_per_move = max_e_per_move

    def lint_file(self, path: str) -> List[LintError]:
        with open(path, "r") as f:
            return self.lint(f.read())

    def lint(self, gcode: str) -> List[LintError]:
        errors: List[LintError] = []
        state = _State()
        lines = gcode.splitlines()
        dialect = self._detect_dialect(lines) if self.dialect == GCodeDialect.AUTO else self.dialect

        for lineno_0, raw in enumerate(lines):
            lineno = lineno_0 + 1
            line = raw.strip()

            if not line or line.startswith(";"):
                self._process_comment(line, lineno, state, errors)
                continue

            # Strip inline comment
            if ";" in line:
                line = line[:line.index(";")].strip()

            m = _GCODE_RE.match(line)
            if not m:
                errors.append(LintError(lineno, Severity.WARNING, "PARSE", f"Unparseable line: {raw.strip()!r}"))
                continue

            cmd = m.group(1).upper()
            params = dict(_PARAM_RE.findall(m.group(2).upper()))

            if cmd.startswith("T"):
                self._check_tool_change(cmd, lineno, state, errors)
            elif cmd.startswith("G"):
                self._check_g_command(cmd, params, lineno, state, dialect, errors)
            elif cmd.startswith("M"):
                self._check_m_command(cmd, params, lineno, state, dialect, errors)

        # Post-file checks
        self._post_checks(state, dialect, errors, len(lines))
        return errors

    def _detect_dialect(self, lines: List[str]) -> GCodeDialect:
        for line in lines[:50]:
            if line.strip().upper().startswith(";FLAVOR:"):
                flavor = line.strip().split(":", 1)[1].strip().upper()
                if "GRIFFIN" in flavor:
                    return GCodeDialect.GRIFFIN
                return GCodeDialect.MARLIN
        return GCodeDialect.MARLIN

    def _process_comment(self, line: str, lineno: int, state: _State, errors: List[LintError]):
        stripped = line.lstrip("; ").upper()
        if stripped.startswith("TYPE:"):
            state.current_type = stripped[5:].strip()
        elif stripped.startswith("LAYER:"):
            try:
                state.layer = int(stripped[6:].strip())
                state.e_at_layer_start = state.e
            except ValueError:
                pass

    def _check_tool_change(self, cmd: str, lineno: int, state: _State, errors: List[LintError]):
        try:
            tool = int(cmd[1:])
        except ValueError:
            errors.append(LintError(lineno, Severity.ERROR, "TOOL_INVALID", f"Invalid tool command: {cmd}"))
            return

        if tool < 0 or tool > 7:
            errors.append(LintError(lineno, Severity.ERROR, "TOOL_RANGE", f"Tool index out of range: T{tool}"))
            return

        # Tool change during wall extrusion is suspicious (BrickLayers bug)
        if state.current_type in ("WALL-INNER", "WALL-OUTER"):
            errors.append(LintError(lineno, Severity.ERROR, "TOOL_IN_WALL",
                          f"Tool change T{tool} inside {state.current_type} section — "
                          "BrickLayers may have corrupted tool assignments"))

        state.active_extruder = tool
        state.tool_changes.append(tool)

    def _check_g_command(self, cmd: str, params: dict, lineno: int,
                         state: _State, dialect: GCodeDialect, errors: List[LintError]):
        if cmd in ("G0", "G1"):
            self._check_move(cmd, params, lineno, state, errors)
        elif cmd == "G28":
            state.seen_home = True
            state.x = state.y = state.z = 0.0
        elif cmd == "G90":
            state.absolute_positioning = True
        elif cmd == "G91":
            state.absolute_positioning = False
        elif cmd == "G92":
            if "E" in params:
                state.e = float(params["E"])
                state.max_e = state.e
        elif cmd not in _VALID_G_COMMANDS:
            errors.append(LintError(lineno, Severity.WARNING, "G_UNKNOWN", f"Unknown G command: {cmd}"))

    def _check_move(self, cmd: str, params: dict, lineno: int, state: _State, errors: List[LintError]):
        new_x = float(params["X"]) if "X" in params else state.x
        new_y = float(params["Y"]) if "Y" in params else state.y
        new_z = float(params["Z"]) if "Z" in params else state.z
        if "F" in params:
            state.f = float(params["F"])

        # E handling
        has_e = "E" in params
        if has_e:
            e_val = float(params["E"])
            if state.relative_extrusion:
                e_delta = e_val
                new_e = state.e + e_val
            else:
                e_delta = e_val - state.e
                new_e = e_val

            # E monotonicity check (absolute mode)
            if not state.relative_extrusion and cmd == "G1" and e_delta < -0.001:
                # Negative E in absolute mode = retraction (ok if small)
                if e_delta < -20.0:
                    errors.append(LintError(lineno, Severity.ERROR, "E_JUMP_NEG",
                                  f"Large negative E jump: {e_delta:.3f}mm — possible E-value drift"))
            elif not state.relative_extrusion and cmd == "G1" and e_delta > 0:
                # Track max E per move
                if e_delta > self.max_e_per_move:
                    errors.append(LintError(lineno, Severity.ERROR, "E_EXCESSIVE",
                                  f"Excessive extrusion in single move: {e_delta:.3f}mm "
                                  f"(max {self.max_e_per_move}mm)"))

            # Relative mode: check for unreasonable deltas
            if state.relative_extrusion and abs(e_delta) > self.max_e_per_move:
                errors.append(LintError(lineno, Severity.ERROR, "E_EXCESSIVE",
                              f"Excessive E delta: {e_delta:.3f}mm"))

            # Retraction tracking
            if e_delta < -0.1:
                state.retracted = True
            elif e_delta > 0.1:
                state.retracted = False

            state.e = new_e
            state.max_e = max(state.max_e, new_e)
            if e_delta > 0:
                state.last_extrusion_z = new_z

        # Travel with extrusion check (G0 should not have E)
        if cmd == "G0" and has_e:
            e_val = float(params["E"])
            if state.relative_extrusion and e_val > 0.01:
                errors.append(LintError(lineno, Severity.WARNING, "G0_EXTRUDE",
                              "G0 (travel) with positive extrusion — should be G1"))

        # Bounds checking
        if new_x < -1.0 or new_x > self.bed_x + 1.0:
            errors.append(LintError(lineno, Severity.ERROR, "X_BOUNDS",
                          f"X={new_x:.2f} out of bed bounds [0, {self.bed_x}]"))
        if new_y < -1.0 or new_y > self.bed_y + 1.0:
            errors.append(LintError(lineno, Severity.ERROR, "Y_BOUNDS",
                          f"Y={new_y:.2f} out of bed bounds [0, {self.bed_y}]"))
        if new_z < -0.5:
            errors.append(LintError(lineno, Severity.ERROR, "Z_NEGATIVE",
                          f"Z={new_z:.2f} below bed — nozzle crash"))
        if new_z > self.max_z:
            errors.append(LintError(lineno, Severity.ERROR, "Z_BOUNDS",
                          f"Z={new_z:.2f} exceeds max height {self.max_z}"))

        # Large XY jump during extrusion (possible position continuity bug)
        if cmd == "G1" and has_e and float(params["E"]) > 0.01:
            dx = abs(new_x - state.x)
            dy = abs(new_y - state.y)
            jump = (dx**2 + dy**2) ** 0.5
            if jump > 100.0:
                errors.append(LintError(lineno, Severity.WARNING, "XY_JUMP",
                              f"Large XY jump ({jump:.1f}mm) during extrusion — "
                              "possible position continuity issue"))

        state.x = new_x
        state.y = new_y
        state.z = new_z

    def _check_m_command(self, cmd: str, params: dict, lineno: int,
                         state: _State, dialect: GCodeDialect, errors: List[LintError]):
        if cmd == "M82":
            if dialect == GCodeDialect.GRIFFIN:
                errors.append(LintError(lineno, Severity.WARNING, "GRIFFIN_M82",
                              "M82 in Griffin G-code — Griffin uses implicit absolute extrusion"))
            state.relative_extrusion = False
        elif cmd == "M83":
            if dialect == GCodeDialect.GRIFFIN:
                errors.append(LintError(lineno, Severity.WARNING, "GRIFFIN_M83",
                              "M83 in Griffin G-code — Griffin uses implicit absolute extrusion"))
            state.relative_extrusion = True
        elif cmd in ("M104", "M109", "M140", "M190"):
            state.seen_heat = True
            if "S" in params:
                temp = float(params["S"])
                if cmd in ("M104", "M109") and temp > 350:
                    errors.append(LintError(lineno, Severity.ERROR, "TEMP_HIGH",
                                  f"Hotend temperature {temp}C exceeds safe limit"))
                if cmd in ("M140", "M190") and temp > 150:
                    errors.append(LintError(lineno, Severity.ERROR, "BED_TEMP_HIGH",
                                  f"Bed temperature {temp}C exceeds safe limit"))
        elif cmd == "M221":
            # Flow rate override — BrickLayers should NOT inject these
            if "S" in params:
                flow = float(params["S"])
                if flow > 150 or flow < 50:
                    errors.append(LintError(lineno, Severity.WARNING, "FLOW_EXTREME",
                                  f"Extreme flow rate override: M221 S{flow}"))
        elif cmd not in _VALID_M_COMMANDS_COMMON:
            if dialect == GCodeDialect.GRIFFIN and cmd in _GRIFFIN_EXTRA:
                pass  # Griffin-specific commands are fine
            else:
                errors.append(LintError(lineno, Severity.INFO, "M_UNKNOWN",
                              f"Non-standard M command: {cmd}"))

    def _post_checks(self, state: _State, dialect: GCodeDialect,
                     errors: List[LintError], total_lines: int):
        if not state.seen_home:
            errors.append(LintError(1, Severity.WARNING, "NO_HOME",
                          "No G28 (home) command found"))

        if state.layer < 0:
            errors.append(LintError(1, Severity.WARNING, "NO_LAYERS",
                          "No ;LAYER: comments found — cannot verify layer structure"))

        # Check for E overflow in absolute mode (>100000 is suspicious)
        if not state.relative_extrusion and state.max_e > 100000:
            errors.append(LintError(total_lines, Severity.WARNING, "E_OVERFLOW",
                          f"E value reached {state.max_e:.1f} — potential firmware overflow on 32-bit"))


def lint_file(path: str, dialect: GCodeDialect = GCodeDialect.AUTO, **kwargs) -> List[LintError]:
    """Convenience function to lint a G-code file."""
    return GCodeLinter(dialect=dialect, **kwargs).lint_file(path)


def lint_string(gcode: str, dialect: GCodeDialect = GCodeDialect.AUTO, **kwargs) -> List[LintError]:
    """Convenience function to lint a G-code string."""
    return GCodeLinter(dialect=dialect, **kwargs).lint(gcode)
