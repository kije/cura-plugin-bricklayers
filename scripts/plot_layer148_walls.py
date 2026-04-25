#!/usr/bin/env python3
"""Plot BR MOTOR HOLDER WALL-INNER extrusion paths at layer 148,
coloured by Z level. Outputs an SVG we can inspect visually.

If the brick pattern is correct, every physical wall line should appear at
EITHER Z=22.39 or Z=22.48 (not both, never neither). Broken walls would
show as visible gaps in the union of both Z levels.
"""
import re

GCODE = "/tmp/bricklayers_investigation/layer_148.gcode"
BMH_START = 5591
BMH_END = 6868

EXTRUDE_RE = re.compile(
    r"^G1(?:\s+F[0-9.]+)?\s+X(-?[0-9.]+)\s+Y(-?[0-9.]+)(?:\s+E(-?[0-9.]+))?"
)
TRAVEL_RE = re.compile(r"^G0(?:\s+F[0-9.]+)?\s+X(-?[0-9.]+)\s+Y(-?[0-9.]+)")
Z_RE = re.compile(r"^G[01](?:\s+F[0-9.]+)?\s+Z([0-9.]+)$")
G1_E_ONLY_RE = re.compile(r"^G1\s+F[0-9.]+\s+E(-?[0-9.]+)$")


def main():
    segments = {"22.39": [], "22.48": []}
    current_z = None
    last_x = last_y = None
    last_e = None

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

        m = TRAVEL_RE.match(line)
        if m:
            last_x = float(m.group(1))
            last_y = float(m.group(2))
            e_m = re.search(r"E(-?[0-9.]+)", line)
            if e_m:
                last_e = float(e_m.group(1))
            continue

        m = G1_E_ONLY_RE.match(line)
        if m:
            last_e = float(m.group(1))
            continue

        m = EXTRUDE_RE.match(line)
        if m:
            x = float(m.group(1))
            y = float(m.group(2))
            e = float(m.group(3)) if m.group(3) else None
            if (last_x is not None and last_y is not None and current_z is not None
                    and e is not None and last_e is not None):
                de = e - last_e
                if de > 0.0005 and current_z in segments:
                    segments[current_z].append((last_x, last_y, x, y))
            last_x, last_y = x, y
            if e is not None:
                last_e = e

    # Compute bounding box for SVG viewport
    all_pts = []
    for segs in segments.values():
        for s in segs:
            all_pts.extend([(s[0], s[1]), (s[2], s[3])])
    if not all_pts:
        print("No extrusion segments found.")
        return

    xs = [p[0] for p in all_pts]
    ys = [p[1] for p in all_pts]
    xmin, xmax = min(xs), max(xs)
    ymin, ymax = min(ys), max(ys)
    pad = 1.0
    w = (xmax - xmin + 2*pad) * 10  # 10 px per mm
    h = (ymax - ymin + 2*pad) * 10

    # Z=22.39 = blue (normal), Z=22.48 = red (shifted)
    color_of = {"22.39": "blue", "22.48": "red"}

    svg_lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w}" height="{h}">',
        '<rect width="100%" height="100%" fill="white"/>',
        f'<g transform="translate({pad*10},{h-pad*10}) scale(10,-10) translate({-xmin},{-ymin})">',
    ]
    for z, segs in segments.items():
        color = color_of[z]
        for (x1, y1, x2, y2) in segs:
            svg_lines.append(
                f'<line x1="{x1:.3f}" y1="{y1:.3f}" x2="{x2:.3f}" y2="{y2:.3f}" '
                f'stroke="{color}" stroke-width="0.08" opacity="0.8"/>'
            )
    svg_lines.append('</g>')
    svg_lines.append(
        f'<text x="10" y="20" font-family="sans-serif" font-size="14">'
        f'BR MOTOR HOLDER WALL-INNER layer 148: blue=Z22.39 ({len(segments["22.39"])} segs), '
        f'red=Z22.48 ({len(segments["22.48"])} segs)</text>'
    )
    svg_lines.append('</svg>')

    out = "/tmp/bricklayers_investigation/bmh_layer148_walls.svg"
    with open(out, "w") as f:
        f.write("\n".join(svg_lines))
    print(f"Wrote {out}")
    print(f"  Z=22.39 (blue/normal): {len(segments['22.39'])} segments")
    print(f"  Z=22.48 (red/shifted): {len(segments['22.48'])} segments")
    print(f"  X range: [{xmin:.2f}, {xmax:.2f}], Y range: [{ymin:.2f}, {ymax:.2f}]")

if __name__ == "__main__":
    main()
