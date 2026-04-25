"""Shared helpers for BrickLayers E2E / geometry-grounded tests.

Provides:
  - Rectangular wall path builders (GCodePath from geometry bounds)
  - Cross-model travel builder
  - Two-cube scene constructor (M8_two_cubes dimensions)
  - GCodePath → G-code emitter (for GCodeLinter validation)
  - CuraEngine G-code → GCodePath parser (for Docker E2E)
  - Pure-Python binary STL cube generator (for Docker E2E)
"""

import math
import os
import struct
import sys

# ---------------------------------------------------------------------------
# Path bootstrap — same as test_grpc_integration.py / conftest.py
# ---------------------------------------------------------------------------
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC_DIR = os.path.join(_REPO_ROOT, "src")
_PROTO_DIR = os.path.join(_SRC_DIR, "proto")
for _p in (_SRC_DIR, _PROTO_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from cura.plugins.v0 import (
    gcode_path_pb2,
    point3d_pb2,
    polygons_pb2,
    printfeatures_pb2,
)

# ---------------------------------------------------------------------------
# Feature constants
# ---------------------------------------------------------------------------
INNERWALL = printfeatures_pb2.INNERWALL
OUTERWALL = printfeatures_pb2.OUTERWALL
SKIN = printfeatures_pb2.SKIN
INFILL = printfeatures_pb2.INFILL
MOVERETRACTED = printfeatures_pb2.MOVERETRACTED
MOVEUNRETRACTED = printfeatures_pb2.MOVEUNRETRACTED

_MOVE_FEATURES = {
    MOVERETRACTED,
    MOVEUNRETRACTED,
    printfeatures_pb2.NONETYPE,
    printfeatures_pb2.MOVEWHILERETRACTING,
    printfeatures_pb2.MOVEWHILEUNRETRACTING,
    printfeatures_pb2.STATIONARYRETRACTUNRETRACT,
}

# ---------------------------------------------------------------------------
# Geometry constants — M8_two_cubes.stl dimensions
# ---------------------------------------------------------------------------
MM = 1000  # microns per mm

CUBE_SIDE = 15 * MM        # 15 000 µm
GAP = 10 * MM              # 10 000 µm
WALL_W = 400               # 400 µm wall width

A_XMIN, A_XMAX = 0, CUBE_SIDE                          # [0, 15000]
B_XMIN, B_XMAX = CUBE_SIDE + GAP, 2 * CUBE_SIDE + GAP  # [25000, 40000]
YMIN, YMAX = 0, CUBE_SIDE                               # [0, 15000]

LAYER_HEIGHT_UM = 200
Z_SHIFT_UM = LAYER_HEIGHT_UM // 2  # 100
LAYER_Z_UM = 200  # z coordinate of point data (first layer at 0.2 mm)

FILAMENT_DIA_MM = 1.75
FILAMENT_AREA_MM2 = math.pi * (FILAMENT_DIA_MM / 2) ** 2  # ~2.405 mm²


# ═══════════════════════════════════════════════════════════════════════════
# GCodePath builders
# ═══════════════════════════════════════════════════════════════════════════

def make_rect_wall(
    feature,
    x_min: int,
    x_max: int,
    y_min: int,
    y_max: int,
    mesh_name: str,
    *,
    layer_thickness: int = LAYER_HEIGHT_UM,
    line_width: int = WALL_W,
    z: int = LAYER_Z_UM,
) -> gcode_path_pb2.GCodePath:
    """Five-point closed rectangle GCodePath (4 corners + return-to-start)."""
    path = gcode_path_pb2.GCodePath(
        feature=feature,
        layer_thickness=layer_thickness,
        line_width=line_width,
        mesh_name=mesh_name,
    )
    corners = [
        (x_min, y_min),
        (x_max, y_min),
        (x_max, y_max),
        (x_min, y_max),
        (x_min, y_min),  # close the rectangle
    ]
    for x, y in corners:
        path.path.path.append(point3d_pb2.Point3D(x=x, y=y, z=z))
    return path


def make_cross_travel(
    from_x: int,
    from_y: int,
    to_x: int,
    to_y: int,
    mesh_name: str,
    *,
    z: int = LAYER_Z_UM,
    layer_thickness: int = LAYER_HEIGHT_UM,
) -> gcode_path_pb2.GCodePath:
    """Two-point MOVERETRACTED path for cross-model travel.

    retract defaults to False (protobuf default), which distinguishes input
    travels from synthesized gap-fill travels (retract=True) in assertions.
    """
    path = gcode_path_pb2.GCodePath(
        feature=MOVERETRACTED,
        mesh_name=mesh_name,
        layer_thickness=layer_thickness,
    )
    path.path.path.append(point3d_pb2.Point3D(x=from_x, y=from_y, z=z))
    path.path.path.append(point3d_pb2.Point3D(x=to_x, y=to_y, z=z))
    return path


def build_two_cube_paths(n_inner: int = 5) -> list:
    """Build a GroupOuter-style GCodePath sequence for two 15 mm cubes, 10 mm apart.

    Returns 2 + 2 + 2*n_inner + 1 = 15 paths (for n_inner=5):
      [0]     OUTERWALL A
      [1]     MOVERETRACTED A  (travel within A)
      [2]     OUTERWALL B
      [3]     MOVERETRACTED B  (travel within B)
      [4..8]  INNERWALL A × n_inner  (insets WALL_W … n_inner×WALL_W)
      [9]     MOVERETRACTED B  (inter-model travel, crosses gap)
      [10..14] INNERWALL B × n_inner

    With outside_in, counter=0 and counter=2 are shifted; innermost (last) is
    protected. For n_inner=5 this gives 2 shifted + 2 normal + 1 protected
    per model, triggering Phase 3b gap-fill synthesis.
    """
    paths = []

    # --- Outer walls (GroupOuter: all outers first) ---
    paths.append(make_rect_wall(OUTERWALL, A_XMIN, A_XMAX, YMIN, YMAX, "A"))

    # Travel within A section (before outer B)
    paths.append(make_cross_travel(A_XMAX, YMIN, A_XMAX, YMAX, "A"))

    paths.append(make_rect_wall(OUTERWALL, B_XMIN, B_XMAX, YMIN, YMAX, "B"))

    # Travel within B section (after outer B, before inner section)
    paths.append(make_cross_travel(B_XMAX, YMIN, B_XMAX, YMAX, "B"))

    # --- Inner walls for Cube A (outside-in order) ---
    for i in range(n_inner):
        inset = (i + 1) * WALL_W
        paths.append(make_rect_wall(
            INNERWALL,
            A_XMIN + inset, A_XMAX - inset,
            YMIN + inset, YMAX - inset,
            "A",
        ))

    # Inter-model travel (A → B, crosses the gap)
    paths.append(make_cross_travel(
        A_XMAX - n_inner * WALL_W, YMIN + n_inner * WALL_W,
        B_XMIN + WALL_W, YMIN + WALL_W,
        "B",
    ))

    # --- Inner walls for Cube B (outside-in order) ---
    for i in range(n_inner):
        inset = (i + 1) * WALL_W
        paths.append(make_rect_wall(
            INNERWALL,
            B_XMIN + inset, B_XMAX - inset,
            YMIN + inset, YMAX - inset,
            "B",
        ))

    return paths


# ═══════════════════════════════════════════════════════════════════════════
# GCodePath → G-code emitter
# ═══════════════════════════════════════════════════════════════════════════

_FEATURE_TYPE_COMMENT = {
    OUTERWALL: ";TYPE:WALL-OUTER",
    INNERWALL: ";TYPE:WALL-INNER",
    SKIN: ";TYPE:SKIN",
    INFILL: ";TYPE:FILL",
}


def emit_gcode(
    paths: list,
    *,
    layer_z_mm: float = 0.2,
    layer_nr: int = 0,
) -> str:
    """Convert GCodePath list to a minimal valid Marlin G-code string.

    Produces E-monotonic output suitable for GCodeLinter validation.
    """
    lines = [
        ";Generated by BrickLayers E2E geometry test",
        ";FLAVOR:Marlin",
        ";LAYER_COUNT:1",
        f";Layer height: {layer_z_mm}",
        "G21 ;metric",
        "G90 ;absolute positioning",
        "M82 ;absolute extrusion mode",
        "G28 ;home",
        f";LAYER:{layer_nr}",
    ]

    e_acc = 0.0
    cur_x, cur_y = 0.0, 0.0

    for path in paths:
        z_mm = layer_z_mm + path.z_offset / 1000.0
        is_travel = path.feature in _MOVE_FEATURES
        lw_mm = (path.line_width or WALL_W) / 1000.0
        lt_mm = (path.layer_thickness or LAYER_HEIGHT_UM) / 1000.0
        fr = path.flow_ratio if path.flow_ratio != 0.0 else 1.0

        # Emit ;TYPE: comment
        type_comment = _FEATURE_TYPE_COMMENT.get(path.feature)
        if type_comment:
            lines.append(type_comment)
        elif is_travel:
            lines.append(";TYPE:TRAVEL")

        if not path.path or not path.path.path:
            continue

        for pt in path.path.path:
            x_mm = pt.x / 1000.0
            y_mm = pt.y / 1000.0

            if is_travel:
                lines.append(f"G0 F3600 X{x_mm:.3f} Y{y_mm:.3f} Z{z_mm:.3f}")
            else:
                dist = math.sqrt((x_mm - cur_x) ** 2 + (y_mm - cur_y) ** 2)
                de = dist * lw_mm * lt_mm * fr / FILAMENT_AREA_MM2
                e_acc += de
                lines.append(
                    f"G1 F1800 X{x_mm:.3f} Y{y_mm:.3f} Z{z_mm:.3f} E{e_acc:.5f}"
                )

            cur_x, cur_y = x_mm, y_mm

    return "\n".join(lines) + "\n"


# ═══════════════════════════════════════════════════════════════════════════
# CuraEngine G-code → GCodePath parser  (Docker E2E)
# ═══════════════════════════════════════════════════════════════════════════

_GCODE_TYPE_MAP = {
    "WALL-OUTER": OUTERWALL,
    "WALL-INNER": INNERWALL,
    "TRAVEL": MOVERETRACTED,
    "SKIN": SKIN,
    "FILL": INFILL,
    "INFILL": INFILL,
    "SUPPORT": printfeatures_pb2.SUPPORT,
}


def parse_curaengine_gcode(gcode_text: str) -> list:
    """Parse CuraEngine G-code into a list of GCodePath protobuf objects.

    Splits on (TYPE, MESH) boundaries — each section becomes one GCodePath.
    Coordinates are converted from mm to microns (×1000, rounded to int).
    """
    paths = []
    cur_feature = MOVERETRACTED
    cur_mesh = ""
    cur_path = None
    cur_x = cur_y = cur_z = 0.0

    def _flush():
        nonlocal cur_path
        if cur_path is not None and cur_path.path and cur_path.path.path:
            paths.append(cur_path)
        cur_path = None

    def _ensure_path():
        nonlocal cur_path
        if cur_path is None:
            cur_path = gcode_path_pb2.GCodePath(
                feature=cur_feature,
                mesh_name=cur_mesh,
                layer_thickness=LAYER_HEIGHT_UM,
                line_width=WALL_W,
            )

    for line in gcode_text.splitlines():
        line = line.strip()
        if not line:
            continue

        if line.startswith(";MESH:"):
            mesh = line[6:].strip()
            if mesh and mesh != "NONMESH" and mesh != cur_mesh:
                _flush()
                cur_mesh = mesh

        elif line.startswith(";TYPE:"):
            type_str = line[6:].strip().upper()
            feature = _GCODE_TYPE_MAP.get(type_str, MOVERETRACTED)
            if feature != cur_feature or cur_path is None:
                _flush()
                cur_feature = feature

        elif line.startswith(("G0 ", "G1 ", "G0\t", "G1\t")):
            # Parse coordinate fields
            parts = {}
            for token in line.split()[1:]:
                if token and token[0] in "XYZEFxyzef":
                    try:
                        parts[token[0].upper()] = float(token[1:])
                    except ValueError:
                        pass
            x = parts.get("X", cur_x)
            y = parts.get("Y", cur_y)
            z = parts.get("Z", cur_z)
            cur_x, cur_y, cur_z = x, y, z

            _ensure_path()
            cur_path.path.path.append(point3d_pb2.Point3D(
                x=round(x * 1000),
                y=round(y * 1000),
                z=round(z * 1000),
            ))

    _flush()
    return paths


# ═══════════════════════════════════════════════════════════════════════════
# Pure-Python binary STL generator  (Docker E2E)
# ═══════════════════════════════════════════════════════════════════════════

def generate_cube_stl_bytes(
    side_mm: float,
    x_offset_mm: float = 0.0,
    y_offset_mm: float = 0.0,
) -> bytes:
    """Generate a binary STL for a cube.  12 triangles, 684 bytes.  No deps.

    The cube spans [x_offset, x_offset+side] × [y_offset, y_offset+side] × [0, side].
    """
    s = side_mm
    x0, y0 = x_offset_mm, y_offset_mm

    quads = [
        # bottom z=0
        [(x0, y0, 0), (x0 + s, y0, 0), (x0 + s, y0 + s, 0), (x0, y0 + s, 0)],
        # top z=s
        [(x0, y0, s), (x0, y0 + s, s), (x0 + s, y0 + s, s), (x0 + s, y0, s)],
        # front y=y0
        [(x0, y0, 0), (x0 + s, y0, 0), (x0 + s, y0, s), (x0, y0, s)],
        # back y=y0+s
        [(x0, y0 + s, 0), (x0, y0 + s, s), (x0 + s, y0 + s, s), (x0 + s, y0 + s, 0)],
        # left x=x0
        [(x0, y0, 0), (x0, y0, s), (x0, y0 + s, s), (x0, y0 + s, 0)],
        # right x=x0+s
        [(x0 + s, y0, 0), (x0 + s, y0 + s, 0), (x0 + s, y0 + s, s), (x0 + s, y0, s)],
    ]

    tris = []
    for q in quads:
        tris.append((q[0], q[1], q[2]))
        tris.append((q[0], q[2], q[3]))

    header = b"\x00" * 80
    count = struct.pack("<I", len(tris))
    body = b"".join(
        struct.pack("<fff", 0.0, 0.0, 0.0)
        + b"".join(struct.pack("<fff", *v) for v in tri)
        + struct.pack("<H", 0)
        for tri in tris
    )
    return header + count + body
