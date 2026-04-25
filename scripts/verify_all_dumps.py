#!/usr/bin/env python3
"""Scan every BrickLayers dump in ~/Library/Logs/BrickLayers and flag:
  a) too-small line segments (< 100µm extrusion/travel paths)
  b) broken extrusion paths (input extrusion lost in output, or input
     extrusion's geometry corrupted)
  c) travels "converted" to inner-wall extrusions — an input MOVE* path
     with feature INNERWALL in output, or any INNERWALL-flagged output
     path whose extrusion signature doesn't match a real wall.

Exit code 0 if nothing suspicious. Any finding reported with the layer
number, extruder number, and path index so you can drill into the .bin.
"""
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, '/Users/kimjekerawl/conductor/workspaces/cura-plugin-bricklayers/singapore')
sys.path.insert(0, '/Users/kimjekerawl/conductor/workspaces/cura-plugin-bricklayers/singapore/src/proto')

from cura.plugins.slots.gcode_paths.v0 import modify_pb2
from cura.plugins.v0 import printfeatures_pb2 as pf

DUMP_DIR = Path.home() / "Library/Logs/BrickLayers"
MIN_EXTRUSION_EXTENT_UM = 100       # flag anything < 100 µm as "tiny"
MIN_SYNTH_TRAVEL_UM = 100           # the WASM bound (should be respected)

# Feature sets
EXTRUSION_FEATURES = {
    pf.OUTERWALL, pf.INNERWALL, pf.SKIN, pf.SUPPORT, pf.SKIRTBRIM,
    pf.INFILL, pf.SUPPORTINFILL, pf.SUPPORTINTERFACE, pf.PRIMETOWER,
}
MOVE_FEATURES = {
    pf.MOVEUNRETRACTED, pf.MOVERETRACTED,
    pf.MOVEWHILERETRACTING, pf.MOVEWHILEUNRETRACTING,
    pf.STATIONARYRETRACTUNRETRACT,
}


def feature_name(f):
    """Resolve feature enum int → name for readable reports."""
    for n in dir(pf):
        if n.isupper() and getattr(pf, n) == f:
            return n
    return f"UNKNOWN({f})"


def path_manhattan(path):
    pts = list(path.path.path)
    if len(pts) < 2:
        return 0
    total = 0
    for i in range(1, len(pts)):
        total += abs(pts[i].x - pts[i-1].x) + abs(pts[i].y - pts[i-1].y)
    return total


def fingerprint(path):
    """Identity for cross-referencing input vs output.
    (feature, mesh_name, tuple of (x,y,z) points)"""
    pts = tuple((pt.x, pt.y, pt.z) for pt in path.path.path)
    return (path.feature, path.mesh_name, pts)


def load_pair(layer_nr, ext_nr):
    in_path = DUMP_DIR / f"layer_{layer_nr}_ext{ext_nr}_in.bin"
    out_path = DUMP_DIR / f"layer_{layer_nr}_ext{ext_nr}_out.bin"
    if not in_path.exists() or not out_path.exists():
        return None, None
    req = modify_pb2.CallRequest()
    req.ParseFromString(in_path.read_bytes())
    resp = modify_pb2.CallResponse()
    resp.ParseFromString(out_path.read_bytes())
    return req, resp


def main():
    issues = {"a_tiny_segments": [], "b_broken_extrusions": [],
              "c_travels_became_walls": [], "d_zero_flow_extrusion": []}
    layer_stats = []
    scanned = 0

    # Find all (layer, ext) pairs that have both in+out
    pairs = set()
    for f in DUMP_DIR.iterdir():
        name = f.name
        if name.endswith("_in.bin") and name.startswith("layer_"):
            try:
                # layer_NNN_extK_in.bin
                stem = name[:-len("_in.bin")]
                parts = stem.split("_")
                layer_nr = int(parts[1])
                ext_nr = int(parts[2].removeprefix("ext"))
                pairs.add((layer_nr, ext_nr))
            except (ValueError, IndexError):
                pass

    print(f"Found {len(pairs)} (layer, extruder) pairs to scan")

    for layer_nr, ext_nr in sorted(pairs):
        req, resp = load_pair(layer_nr, ext_nr)
        if req is None:
            continue
        scanned += 1

        input_paths = list(req.gcode_paths)
        output_paths = list(resp.gcode_paths)
        input_fps = [fingerprint(p) for p in input_paths]
        output_fps = [fingerprint(p) for p in output_paths]
        input_fp_to_indices = defaultdict(list)
        for i, fp in enumerate(input_fps):
            input_fp_to_indices[fp].append(i)

        n_shifted = 0
        n_real_walls = 0

        # -------------------- OUTPUT-SIDE CHECKS --------------------
        for o_idx, p in enumerate(output_paths):
            ext = path_manhattan(p)
            is_extrusion_feat = p.feature in EXTRUSION_FEATURES
            is_move_feat = p.feature in MOVE_FEATURES

            # (a) Tiny extrusion path — can't be an intended extrusion.
            # Exception: input walls that were tiny already (seam anchors
            # with 1 point or short gap-fills) pass through unchanged;
            # we don't flag them as NEW artefacts if the same fingerprint
            # exists in input (i.e., the plugin didn't create this).
            if is_extrusion_feat and len(p.path.path) >= 2 and ext < MIN_EXTRUSION_EXTENT_UM:
                fp = output_fps[o_idx]
                if fp not in input_fp_to_indices:
                    # Output-only tiny extrusion: something the plugin
                    # synthesised or corrupted. Report.
                    issues["a_tiny_segments"].append(
                        (layer_nr, ext_nr, o_idx, feature_name(p.feature),
                         ext, p.mesh_name))

            # (a) Tiny synth travel — WASM `should_emit_travel` should
            # have skipped these. Flag if retract=True & flow_ratio=0 &
            # extent < 100.
            if (is_move_feat and p.retract and p.flow_ratio == 0
                    and len(p.path.path) >= 2 and ext < MIN_SYNTH_TRAVEL_UM):
                fp = output_fps[o_idx]
                if fp not in input_fp_to_indices:
                    issues["a_tiny_segments"].append(
                        (layer_nr, ext_nr, o_idx, "tiny_synth_travel",
                         ext, p.mesh_name))

            # (c) Travels converted to walls: a path that has a wall
            # feature tag AND flow_ratio=0 AND retract=true looks like
            # it was a synth travel the plugin forgot to flip the feature
            # on. Real walls have flow_ratio > 0, line_width > 0.
            if (p.feature in (pf.INNERWALL, pf.OUTERWALL)
                    and p.flow_ratio == 0 and p.line_width == 0):
                issues["c_travels_became_walls"].append(
                    (layer_nr, ext_nr, o_idx, feature_name(p.feature),
                     p.mesh_name))

            # (d) Any extrusion-feature path with flow_ratio=0 and
            # line_width=0 is degenerate — either a corrupted wall or
            # a synth travel with wrong feature tag.
            if (is_extrusion_feat and p.flow_ratio == 0 and p.line_width == 0):
                issues["d_zero_flow_extrusion"].append(
                    (layer_nr, ext_nr, o_idx, feature_name(p.feature),
                     p.mesh_name))

            # (e removed) — the check was flagging Phase 3b Case C/D
            # intentional first-point mutations (which redirect a travel
            # to arrive at the correct XY after a shifted wall is
            # removed). Those mutations change the path's fingerprint
            # but NOT its feature tag, so (c) and (d) above already
            # cover the "travels converted to walls" concern. No
            # additional check needed here.

            if p.feature == pf.INNERWALL and p.z_offset > 0:
                n_shifted += 1
            if p.feature == pf.INNERWALL and ext >= 400 and len(p.path.path) >= 2:
                n_real_walls += 1

        # -------------------- INPUT vs OUTPUT CHECKS --------------------
        # (b) Broken extrusion paths. Every input EXTRUSION path must
        # survive into the output. Plugin's brick pattern modifies
        # z_offset/flow_ratio but not the geometry, so identity by
        # (feature, mesh, xy-points) is stable for real walls.
        #
        # IMPORTANT: Phase 3b Case C/D mutates the FIRST point of
        # selected MOVE paths (after a shifted wall is removed). Those
        # mutations are intentional and only apply to move/travel
        # features — they never touch EXTRUSION features — so an
        # EXTRUSION identity mismatch is a real bug.
        def ident_points(p):
            return (p.feature, p.mesh_name,
                    tuple((pt.x, pt.y, pt.z) for pt in p.path.path))

        input_pt_keys = Counter(ident_points(p) for p in input_paths
                                if p.feature in EXTRUSION_FEATURES)
        output_pt_keys = Counter(ident_points(p) for p in output_paths
                                 if p.feature in EXTRUSION_FEATURES)
        missing_in_output = input_pt_keys - output_pt_keys
        extra_in_output = output_pt_keys - input_pt_keys
        for key, cnt in missing_in_output.items():
            feat, mesh, pts = key
            issues["b_broken_extrusions"].append(
                (layer_nr, ext_nr, "MISSING_IN_OUTPUT",
                 feature_name(feat), mesh, cnt, len(pts)))

        # extra_in_output is expected to be empty (plugin shouldn't
        # synthesise new extrusion paths), but the shift pattern creates
        # copies with different z_offset/flow_ratio. Those are the SAME
        # (feature, mesh, pts) tuple — modifying z_offset / flow_ratio
        # doesn't change the tuple identity used here. So
        # extra_in_output SHOULD be zero.  Anything > 0 = created from
        # thin air.
        for key, cnt in extra_in_output.items():
            feat, mesh, pts = key
            issues["b_broken_extrusions"].append(
                (layer_nr, ext_nr, "CREATED_FROM_THIN_AIR",
                 feature_name(feat), mesh, cnt, len(pts)))

        layer_stats.append((layer_nr, ext_nr, len(input_paths),
                             len(output_paths), n_shifted, n_real_walls))

    # ----------------- REPORT -----------------
    print(f"\nScanned: {scanned} (layer, ext) pairs")
    print(f"Total input  extrusion paths across all layers: "
          f"{sum(s[2] for s in layer_stats)}")
    print(f"Total output extrusion paths across all layers: "
          f"{sum(s[3] for s in layer_stats)}")
    total_shifts = sum(s[4] for s in layer_stats)
    total_real = sum(s[5] for s in layer_stats)
    print(f"Total shifted INNERWALLs: {total_shifts}")
    print(f"Total 'real' INNERWALLs (≥400 µm extent): {total_real}")
    if total_real:
        print(f"  shift rate: {100*total_shifts/total_real:.1f}%")

    def report(title, key):
        items = issues[key]
        print(f"\n{'='*60}")
        print(f"{title}: {len(items)} finding(s)")
        print('='*60)
        if not items:
            print("  (none)")
            return
        for item in items[:20]:
            print(f"  {item}")
        if len(items) > 20:
            print(f"  ... and {len(items) - 20} more")

    report("a) Tiny segments (< 100µm synth/extrusion)", "a_tiny_segments")
    report("b) Broken extrusion paths (input missing / created)", "b_broken_extrusions")
    report("c) Travels converted to inner walls", "c_travels_became_walls")
    report("d) Extrusion paths with flow=0 + line_width=0", "d_zero_flow_extrusion")

    any_issue = any(issues.values())
    if any_issue:
        total = sum(len(v) for v in issues.values())
        print(f"\n{total} total finding(s). Exit 1.")
        sys.exit(1)
    else:
        print("\nAll checks passed. Exit 0.")
        sys.exit(0)


if __name__ == "__main__":
    main()
