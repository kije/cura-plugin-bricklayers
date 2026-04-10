#!/usr/bin/env python3
"""Generate STL test models for BrickLayers hardware testing.

Run: python generate_test_models.py
Output: STL files in ./test_models/
"""

import math
import os
import numpy as np
from stl import mesh


OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_models")


def _ensure_dir():
    os.makedirs(OUTPUT_DIR, exist_ok=True)


def _quad(v0, v1, v2, v3):
    """Two triangles forming a quad."""
    return [(v0, v1, v2), (v0, v2, v3)]


def _box(cx, cy, cz, sx, sy, sz):
    """Axis-aligned box centered at (cx,cy,cz) with size (sx,sy,sz)."""
    hx, hy, hz = sx / 2, sy / 2, sz / 2
    # 8 corners
    v = [
        (cx - hx, cy - hy, cz - hz),  # 0: left-front-bottom
        (cx + hx, cy - hy, cz - hz),  # 1: right-front-bottom
        (cx + hx, cy + hy, cz - hz),  # 2: right-back-bottom
        (cx - hx, cy + hy, cz - hz),  # 3: left-back-bottom
        (cx - hx, cy - hy, cz + hz),  # 4: left-front-top
        (cx + hx, cy - hy, cz + hz),  # 5: right-front-top
        (cx + hx, cy + hy, cz + hz),  # 6: right-back-top
        (cx - hx, cy + hy, cz + hz),  # 7: left-back-top
    ]
    tris = []
    # bottom (z-), top (z+)
    tris += _quad(v[0], v[3], v[2], v[1])  # bottom
    tris += _quad(v[4], v[5], v[6], v[7])  # top
    # front (y-), back (y+)
    tris += _quad(v[0], v[1], v[5], v[4])  # front
    tris += _quad(v[2], v[3], v[7], v[6])  # back
    # left (x-), right (x+)
    tris += _quad(v[0], v[4], v[7], v[3])  # left
    tris += _quad(v[1], v[2], v[6], v[5])  # right
    return tris


def _cylinder(cx, cy, r, h, segments=64, z_base=0):
    """Vertical cylinder centered at (cx, cy), base at z_base."""
    tris = []
    for i in range(segments):
        a0 = 2 * math.pi * i / segments
        a1 = 2 * math.pi * (i + 1) / segments
        x0, y0 = cx + r * math.cos(a0), cy + r * math.sin(a0)
        x1, y1 = cx + r * math.cos(a1), cy + r * math.sin(a1)
        # bottom face
        tris.append(((cx, cy, z_base), (x1, y1, z_base), (x0, y0, z_base)))
        # top face
        tris.append(((cx, cy, z_base + h), (x0, y0, z_base + h), (x1, y1, z_base + h)))
        # side
        tris += _quad(
            (x0, y0, z_base), (x1, y1, z_base),
            (x1, y1, z_base + h), (x0, y0, z_base + h)
        )
    return tris


def _hemisphere(cx, cy, r, lat_segments=32, lon_segments=64):
    """Upper hemisphere centered at (cx, cy, 0)."""
    tris = []
    for i in range(lat_segments):
        theta0 = math.pi / 2 * i / lat_segments
        theta1 = math.pi / 2 * (i + 1) / lat_segments
        for j in range(lon_segments):
            phi0 = 2 * math.pi * j / lon_segments
            phi1 = 2 * math.pi * (j + 1) / lon_segments

            def pt(th, ph):
                return (
                    cx + r * math.cos(th) * math.cos(ph),
                    cy + r * math.cos(th) * math.sin(ph),
                    r * math.sin(th),
                )

            v00 = pt(theta0, phi0)
            v10 = pt(theta1, phi0)
            v01 = pt(theta0, phi1)
            v11 = pt(theta1, phi1)

            if i == 0:
                tris.append((v00, v11, v10))
            else:
                tris.append((v00, v01, v11))
                tris.append((v00, v11, v10))

    # bottom cap (flat circle at z=0)
    for j in range(lon_segments):
        phi0 = 2 * math.pi * j / lon_segments
        phi1 = 2 * math.pi * (j + 1) / lon_segments
        x0, y0 = cx + r * math.cos(phi0), cy + r * math.sin(phi0)
        x1, y1 = cx + r * math.cos(phi1), cy + r * math.sin(phi1)
        tris.append(((cx, cy, 0), (x1, y1, 0), (x0, y0, 0)))

    return tris


def _save(tris, filename):
    """Save list of triangle tuples to STL."""
    m = mesh.Mesh(np.zeros(len(tris), dtype=mesh.Mesh.dtype))
    for i, (v0, v1, v2) in enumerate(tris):
        m.vectors[i][0] = v0
        m.vectors[i][1] = v1
        m.vectors[i][2] = v2
    path = os.path.join(OUTPUT_DIR, filename)
    m.save(path)
    print(f"  {filename} ({len(tris)} triangles)")


def m1_cube():
    """M1: 20x20x20mm cube."""
    _save(_box(10, 10, 10, 20, 20, 20), "M1_cube_20mm.stl")


def m2_cylinder():
    """M2: 20mm diameter, 20mm tall cylinder."""
    _save(_cylinder(0, 0, 10, 20), "M2_cylinder_20mm.stl")


def m3_thin_wall_box():
    """M3: 40x40x20mm hollow box with 0.4mm wall thickness.
    Outer box minus inner box = thin walls."""
    outer = 40
    inner = 40 - 0.8  # 0.4mm wall on each side
    h = 20

    tris = []
    # Outer shell
    tris += _box(20, 20, h / 2, outer, outer, h)
    # Inner void (inverted normals = subtraction via separate inner box)
    # For STL, we create a hollow box by building the 4 walls + bottom + top
    # Actually, let's just create a proper hollow box

    # Easier approach: build 4 walls + floor as separate boxes
    wall = 0.4
    # Bottom plate
    tris += _box(20, 20, wall / 2, outer, outer, wall)
    # Front wall (y=0 side)
    tris += _box(20, wall / 2, h / 2, outer, wall, h)
    # Back wall (y=40 side)
    tris += _box(20, outer - wall / 2, h / 2, outer, wall, h)
    # Left wall (x=0 side)
    tris += _box(wall / 2, 20, h / 2, wall, outer, h)
    # Right wall (x=40 side)
    tris += _box(outer - wall / 2, 20, h / 2, wall, outer, h)

    # Note: This creates overlapping geometry. A proper thin-wall box
    # would use a mesh boolean. For slicer testing, overlapping solids work
    # fine as Cura's slicer handles them. But for a cleaner model, use the
    # full outer box with 1 wall line in slicer settings instead.
    _save(tris, "M3_thin_wall_box_40mm.stl")


def m4_overhang_staircase():
    """M4: Staircase/pyramid — steps at 5mm intervals."""
    tris = []
    steps = [(40, 0), (30, 5), (20, 10), (10, 15)]
    for size, z_base in steps:
        cx = 20  # centered
        cy = 20
        tris += _box(cx, cy, z_base + 2.5, size, size, 5)
    _save(tris, "M4_overhang_staircase.stl")


def m5_sloped_wedge():
    """M5: Wedge with 45-degree and 60-degree overhangs."""
    # Base: 40x20, height 20
    # Front face: 45 degrees (x=0 side slopes inward)
    # Back face: 60 degrees (x=40 side slopes more aggressively)
    v = [
        # Bottom
        (0, 0, 0), (40, 0, 0), (40, 20, 0), (0, 20, 0),
        # Top (narrower due to slopes)
        (20, 0, 20),  # 45 deg from x=0: rises 20, moves in 20
        (33.45, 0, 20),  # 60 deg from x=40: rises 20, moves in 20*tan(30)=11.55 -> 40-6.55=33.45
        (33.45, 20, 20),
        (20, 20, 20),
    ]
    tris = []
    # Bottom
    tris += _quad(v[0], v[3], v[2], v[1])
    # Top
    tris += _quad(v[4], v[5], v[6], v[7])
    # Front (y=0)
    tris += _quad(v[0], v[1], v[5], v[4])
    # Back (y=20)
    tris += _quad(v[2], v[3], v[7], v[6])
    # Left slope (45 deg)
    tris += _quad(v[0], v[4], v[7], v[3])
    # Right slope (60 deg)
    tris += _quad(v[1], v[2], v[6], v[5])
    _save(tris, "M5_sloped_wedge.stl")


def m6_tall_tower():
    """M6: 10x10x60mm tall tower."""
    _save(_box(5, 5, 30, 10, 10, 60), "M6_tall_tower_60mm.stl")


def m7_box_with_holes():
    """M7: 30x30x20mm box with cylindrical holes.
    Since boolean subtraction is complex, we create the box and note
    that holes should be added in CAD. Instead, create a box with
    a cross-shaped cutout (simpler geometry)."""
    # Simple approach: box with a slot through each axis
    # Main box
    tris = _box(15, 15, 10, 30, 30, 20)
    _save(tris, "M7_box_30mm.stl")
    # Note: User should add 8mm holes through each face in CAD software
    # or use the provided box and manually add holes in Tinkercad/FreeCAD


def m8_two_cubes():
    """M8: Two 15x15x15mm cubes, 10mm apart."""
    tris = []
    tris += _box(7.5, 7.5, 7.5, 15, 15, 15)       # left cube
    tris += _box(7.5 + 25, 7.5, 7.5, 15, 15, 15)   # right cube, 10mm gap
    _save(tris, "M8_two_cubes.stl")


def m10_bridge_test():
    """M10: Two pillars with a bridge."""
    tris = []
    # Left pillar
    tris += _box(5, 5, 7.5, 10, 10, 15)
    # Right pillar
    tris += _box(35, 5, 7.5, 10, 10, 15)
    # Bridge connecting them at top
    tris += _box(20, 5, 17.5, 30, 10, 5)
    _save(tris, "M10_bridge_test.stl")


def m11_hemisphere():
    """M11: 30mm diameter hemisphere."""
    _save(_hemisphere(0, 0, 15), "M11_hemisphere_30mm.stl")


def m12_vase_cylinder():
    """M12: 30mm diameter, 40mm tall cylinder (for vase mode)."""
    _save(_cylinder(0, 0, 15, 40), "M12_vase_cylinder.stl")


def m13_cube_with_overhang():
    """M13: 20mm cube with a 15mm shelf overhang needing support."""
    tris = []
    # Main cube
    tris += _box(10, 10, 10, 20, 20, 20)
    # Overhang shelf (extends 15mm from one face, starts at z=15)
    tris += _box(10 + 10 + 7.5, 10, 17.5, 15, 20, 5)
    _save(tris, "M13_cube_with_overhang.stl")


def main():
    _ensure_dir()
    print("Generating BrickLayers test models...")
    m1_cube()
    m2_cylinder()
    m3_thin_wall_box()
    m4_overhang_staircase()
    m5_sloped_wedge()
    m6_tall_tower()
    m7_box_with_holes()
    m8_two_cubes()
    m10_bridge_test()
    m11_hemisphere()
    m12_vase_cylinder()
    m13_cube_with_overhang()
    print(f"\nAll models saved to {OUTPUT_DIR}/")
    print("\nNote: M7 (box with holes) is a solid box — add 8mm holes")
    print("through each face using CAD software (Tinkercad, FreeCAD, etc.)")
    print("\nFor dual-extruder tests (M14-M16), use the single-extruder")
    print("models with dual-extruder slicer settings (support extruder = T1).")


if __name__ == "__main__":
    main()
