#!/usr/bin/env python3
"""Render the layer-148 walls as PNG using matplotlib."""
import re
import sys

GCODE = "/tmp/bricklayers_investigation/layer_148.gcode"
BMH_START = 5591
BMH_END = 6868

EXTRUDE_RE = re.compile(r"^G1(?:\s+F[0-9.]+)?\s+X(-?[0-9.]+)\s+Y(-?[0-9.]+)(?:\s+E(-?[0-9.]+))?")
TRAVEL_RE = re.compile(r"^G0(?:\s+F[0-9.]+)?\s+X(-?[0-9.]+)\s+Y(-?[0-9.]+)")
Z_RE = re.compile(r"^G[01](?:\s+F[0-9.]+)?\s+Z([0-9.]+)$")
G1_E_ONLY_RE = re.compile(r"^G1\s+F[0-9.]+\s+E(-?[0-9.]+)$")


def main():
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib not available")
        sys.exit(1)

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

    fig, axes = plt.subplots(1, 3, figsize=(18, 10))
    titles = [
        f"Z=22.39 normal ({len(segments['22.39'])} segs)",
        f"Z=22.48 shifted ({len(segments['22.48'])} segs)",
        "Both Z combined (union)",
    ]
    colours = [("blue", ["22.39"]), ("red", ["22.48"]), (None, ["22.39", "22.48"])]

    for ax, title, (single_color, zs) in zip(axes, titles, colours):
        for z in zs:
            c = single_color if single_color else ("blue" if z == "22.39" else "red")
            for (x1, y1, x2, y2) in segments[z]:
                ax.plot([x1, x2], [y1, y2], color=c, linewidth=0.8, alpha=0.85)
        ax.set_aspect("equal")
        ax.set_title(title, fontsize=12)
        ax.set_xlabel("X (mm)")
        ax.set_ylabel("Y (mm)")
        ax.grid(True, alpha=0.3)

    fig.suptitle("BR MOTOR HOLDER WALL-INNER at layer 148 — extrusion paths", fontsize=14)
    fig.tight_layout()
    out = "/tmp/bricklayers_investigation/bmh_layer148_walls.png"
    fig.savefig(out, dpi=140, bbox_inches="tight")
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
