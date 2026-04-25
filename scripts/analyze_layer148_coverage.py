#!/usr/bin/env python3
"""Analyze BR MOTOR HOLDER WALL-INNER coverage at layer 148.

Correctly tracks E values across retract/prime cycles: only counts E delta
for an extrusion move if the previous line is a "continuous" operation
(same G1 mode, no retract). A retract-prime cycle injects 10mm of E change
that isn't an actual XY extrusion.
"""
import re
from collections import defaultdict

GCODE = "/tmp/bricklayers_investigation/layer_148.gcode"
BMH_START = 5591
BMH_END = 6868

G1_XY_RE = re.compile(
    r"^G1(?:\s+F[0-9.]+)?\s+X(-?[0-9.]+)\s+Y(-?[0-9.]+)(?:\s+E(-?[0-9.]+))?"
)
G1_XY_CONT_RE = re.compile(
    r"^G1\s+X(-?[0-9.]+)\s+Y(-?[0-9.]+)(?:\s+E(-?[0-9.]+))?"
)
G1_E_ONLY_RE = re.compile(r"^G1\s+F[0-9.]+\s+E(-?[0-9.]+)$")
G0_XY_RE = re.compile(
    r"^G0(?:\s+F[0-9.]+)?\s+X(-?[0-9.]+)\s+Y(-?[0-9.]+)(?:\s+E(-?[0-9.]+))?"
)
Z_RE = re.compile(r"^G[01](?:\s+F[0-9.]+)?\s+Z([0-9.]+)$")


def parse_layer():
    segments = defaultdict(list)  # z -> [(x1,y1,x2,y2,de)]
    current_z = None
    last_x = last_y = None
    last_e = None  # absolute E after previous command

    with open(GCODE) as f:
        lines = f.readlines()

    for i, line in enumerate(lines, 1):
        if i < BMH_START or i > BMH_END:
            continue
        line = line.rstrip()

        m = Z_RE.match(line)
        if m:
            current_z = m.group(1)
            continue

        m = G0_XY_RE.match(line)
        if m:
            # Travel — updates position; if E provided, updates E (retract)
            last_x = float(m.group(1))
            last_y = float(m.group(2))
            if m.group(3):
                last_e = float(m.group(3))
            continue

        m = G1_E_ONLY_RE.match(line)
        if m:
            # Prime/retract only — updates E
            last_e = float(m.group(1))
            continue

        m = G1_XY_RE.match(line)
        if m:
            x = float(m.group(1))
            y = float(m.group(2))
            e = float(m.group(3)) if m.group(3) else None
            if (last_x is not None and last_y is not None and current_z is not None
                    and e is not None and last_e is not None):
                de = e - last_e
                # Segment
                if de > 0.0005:  # actual extrusion (filter out prime artifacts)
                    segments[current_z].append((last_x, last_y, x, y, de))
            last_x = x
            last_y = y
            if e is not None:
                last_e = e
            continue

    return segments


def main():
    segments = parse_layer()

    for z, segs in sorted(segments.items()):
        total_xy = sum(((s[2]-s[0])**2 + (s[3]-s[1])**2)**0.5 for s in segs)
        total_e = sum(s[4] for s in segs)
        rate = total_e / total_xy if total_xy > 0 else 0
        print(f"Z={z}: {len(segs)} segs, XY={total_xy:.2f}mm, E={total_e:.4f}mm, rate={rate:.4f} mm/mm")
        xs = [s[0] for s in segs] + [s[2] for s in segs]
        ys = [s[1] for s in segs] + [s[3] for s in segs]
        if xs:
            print(f"  X=[{min(xs):.2f}, {max(xs):.2f}]  Y=[{min(ys):.2f}, {max(ys):.2f}]")

    # Grid coverage 1mm
    cells_norm, cells_shift = set(), set()
    for z, segs in segments.items():
        cells = cells_norm if z == "22.39" else cells_shift if z == "22.48" else None
        if cells is None:
            continue
        for x1, y1, x2, y2, _ in segs:
            ds = ((x2-x1)**2 + (y2-y1)**2)**0.5
            n = max(2, int(ds) + 1)
            for t in range(n + 1):
                fx = t / n
                cells.add((int((x1 + fx*(x2-x1))), int((y1 + fx*(y2-y1)))))

    print(f"\n=== 1mm-grid coverage ===")
    print(f"  Normal-Z cells: {len(cells_norm)}")
    print(f"  Shifted-Z cells: {len(cells_shift)}")
    print(f"  Both:            {len(cells_norm & cells_shift)}")
    print(f"  Normal-only:     {len(cells_norm - cells_shift)}")
    print(f"  Shifted-only:    {len(cells_shift - cells_norm)}")

    # Expected extrusion rate for 0.6mm nozzle, 0.55mm line width, 0.18mm layer, 2.85mm filament:
    # E_per_mm = line_width * layer_height / filament_cross_section
    expected = (0.55 * 0.18) / (3.14159 * (2.85/2)**2)
    print(f"\nExpected extrusion rate (0.55mm line, 0.18mm layer, 2.85mm filament): {expected:.4f} mm/mm")

if __name__ == "__main__":
    main()
