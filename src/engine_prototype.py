#!/usr/bin/env python3
"""BrickLayers CuraEngine plugin prototype.

A gRPC server that implements the GCODE_PATHS_MODIFY slot (103) for CuraEngine.
Shifts alternating wall paths up by half a layer height to create interlocking
brick-like walls.

Usage:
    python3 engine_prototype.py --address 127.0.0.1 --port 50051

CuraEngine connects as gRPC client; this plugin runs as server.
"""

import argparse
import json
import logging
import sys
import os
from concurrent import futures
from dataclasses import dataclass, field
from typing import Dict, Optional

_DEBUG_LOG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "BrickLayers_debug.jsonl"
)


def _feature_name(feature_int: int) -> str:
    names = {0: "NONE", 1: "OUTERWALL", 2: "INNERWALL", 4: "SKIN", 5: "SUPPORT",
             6: "SKIRT", 7: "INFILL", 8: "MOVEUNRETRACTED", 9: "MOVERETRACTED",
             10: "PRIME", 12: "MOVEWHILERETRACTING", 13: "MOVEWHILEUNRETRACTING", 14: "STATIONARYRETRACTUNRETRACT"}
    return names.get(feature_int, f"UNKNOWN({feature_int})")


def _log_call(layer_nr, extruder_nr, req_paths, resp_paths):
    # Group input paths by (feature, mesh_name, z_offset) → count
    from collections import Counter
    in_groups = Counter(
        (_feature_name(p.feature), p.mesh_name or "", p.z_offset)
        for p in req_paths
    )
    out_groups = Counter(
        (_feature_name(p.feature), p.mesh_name or "", p.z_offset)
        for p in resp_paths
    )
    record = {
        "layer_nr": layer_nr,
        "extruder_nr": extruder_nr,
        "path_count": len(req_paths),
        "input_z_shifted": sum(1 for p in req_paths if p.z_offset != 0),
        "output_z_shifted": sum(1 for p in resp_paths if p.z_offset != 0),
        "input_groups": [
            {"feature": f, "mesh": m, "z_offset": z, "count": c}
            for (f, m, z), c in sorted(in_groups.items())
        ],
        "output_groups": [
            {"feature": f, "mesh": m, "z_offset": z, "count": c}
            for (f, m, z), c in sorted(out_groups.items())
        ],
    }
    with open(_DEBUG_LOG_PATH, "a") as f:
        f.write(json.dumps(record) + "\n")

import grpc

# Add proto output directory to path
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "proto"))

from cura.plugins.v0 import slot_id_pb2
from cura.plugins.v0 import printfeatures_pb2
from cura.plugins.v0 import gcode_path_pb2
from cura.plugins.slots.handshake.v0 import handshake_pb2, handshake_pb2_grpc
from cura.plugins.slots.broadcast.v0 import broadcast_pb2, broadcast_pb2_grpc
from cura.plugins.slots.gcode_paths.v0 import modify_pb2, modify_pb2_grpc
from google.protobuf import empty_pb2

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("bricklayers_engine")

PLUGIN_NAME = "BrickLayers"
PLUGIN_VERSION = "1.0.0"
SLOT_VERSION = "0.1.0-alpha"


@dataclass
class BrickSettings:
    """Settings for the brick pattern algorithm."""
    enabled: bool = False
    start_layer: int = 2  # 0-indexed (Cura setting is 1-indexed, subtract 1)
    end_layer: int = -1   # -1 means all layers
    apply_inner_walls: bool = True
    apply_outer_walls: bool = False
    extrusion_multiplier: float = 1.05
    layer_height: int = 0  # microns, from settings broadcast
    inset_direction: str = "outside_in"  # wall print ordering


class HandshakeServicer(handshake_pb2_grpc.HandshakeServiceServicer):
    """Handles the initial handshake with CuraEngine."""

    def Call(self, request, context):
        logger.info(
            "Handshake: slot=%s engine_plugin=%s v=%s",
            request.slot_id, request.plugin_name, request.version,
        )
        context.send_initial_metadata((
            ("cura-slot-version", SLOT_VERSION),
            ("cura-plugin-name", PLUGIN_NAME),
            ("cura-plugin-version", PLUGIN_VERSION),
        ))
        return handshake_pb2.CallResponse(
            slot_version_range=SLOT_VERSION,
            plugin_name=PLUGIN_NAME,
            plugin_version=PLUGIN_VERSION,
            broadcast_subscriptions=[slot_id_pb2.SETTINGS_BROADCAST],
        )


class BroadcastServicer(broadcast_pb2_grpc.BroadcastServiceServicer):
    """Receives settings broadcast from CuraEngine."""

    def __init__(self, settings: BrickSettings) -> None:
        self._settings = settings

    def BroadcastSettings(self, request, context):
        logger.info("Received settings broadcast")
        self._parse_settings(request.global_settings)
        for ext_settings in request.extruder_settings:
            self._parse_settings(ext_settings)
        logger.info(
            "BrickSettings: enabled=%s start=%d end=%d inner=%s outer=%s "
            "multiplier=%.2f layer_height=%d",
            self._settings.enabled, self._settings.start_layer,
            self._settings.end_layer, self._settings.apply_inner_walls,
            self._settings.apply_outer_walls, self._settings.extrusion_multiplier,
            self._settings.layer_height,
        )
        return empty_pb2.Empty()

    def _parse_settings(self, settings_msg) -> None:
        if settings_msg is None:
            return
        # Settings is a map<string, bytes> in the upstream proto
        settings_map = settings_msg.settings
        for name, value_bytes in settings_map.items():
            val = value_bytes.decode("utf-8", errors="replace").strip()
            if name == "brick_layers_enabled":
                self._settings.enabled = val.lower() in ("true", "1", "yes")
            elif name == "brick_layers_start_layer":
                try:
                    self._settings.start_layer = max(0, int(float(val)) - 1)
                except ValueError:
                    pass
            elif name == "brick_layers_end_layer":
                try:
                    self._settings.end_layer = int(float(val))
                except ValueError:
                    pass
            elif name == "brick_layers_apply_inner_walls":
                self._settings.apply_inner_walls = val.lower() in ("true", "1", "yes")
            elif name == "brick_layers_apply_outer_walls":
                self._settings.apply_outer_walls = val.lower() in ("true", "1", "yes")
            elif name == "brick_layers_extrusion_multiplier":
                try:
                    self._settings.extrusion_multiplier = float(val)
                except ValueError:
                    pass
            elif name == "layer_height":
                try:
                    # Cura sends in mm, CuraEngine uses microns
                    self._settings.layer_height = int(float(val) * 1000)
                except ValueError:
                    pass
            elif name == "inset_direction":
                self._settings.inset_direction = val.lower().strip()


class GCodePathsModifyServicer(modify_pb2_grpc.GCodePathsModifyServiceServicer):
    """Modifies GCode paths to implement the brick-layer wall pattern.

    For alternating wall paths, shifts Z up by half a layer height using
    the z_offset field. CuraEngine applies this offset when generating
    G-code and handles extrusion/retraction/travel automatically.
    """

    def __init__(self, settings: BrickSettings) -> None:
        self._settings = settings

    def Call(self, request, context):
        context.send_initial_metadata((
            ("cura-slot-version", SLOT_VERSION),
        ))

        layer_nr = request.layer_nr
        extruder_nr = request.extruder_nr
        paths = list(request.gcode_paths)

        if not self._settings.enabled:
            _log_call(layer_nr, extruder_nr, paths, paths)
            return modify_pb2.CallResponse(gcode_paths=paths)

        # Check layer range
        if layer_nr < self._settings.start_layer:
            _log_call(layer_nr, extruder_nr, paths, paths)
            return modify_pb2.CallResponse(gcode_paths=paths)
        if self._settings.end_layer > 0:
            end_layer_idx = self._settings.end_layer - 1
            if layer_nr > end_layer_idx:
                _log_call(layer_nr, extruder_nr, paths, paths)
                return modify_pb2.CallResponse(gcode_paths=paths)

        modified = self._apply_brick_pattern(paths, layer_nr)
        _log_call(layer_nr, extruder_nr, paths, modified)
        return modify_pb2.CallResponse(gcode_paths=modified)

    def _apply_brick_pattern(self, paths, layer_nr):
        """Shift alternating wall paths up by half a layer height.

        For each contiguous group of target wall paths:
        1. The innermost wall (adjacent to infill) is never shifted,
           determined by the inset_direction setting.
        2. Remaining walls are shifted on alternating indices.
        3. After modification, paths are reordered: all normal-Z paths
           first, then all shifted paths — minimising Z oscillation.
        """
        settings = self._settings
        layer_thickness = settings.layer_height
        if layer_thickness <= 0:
            # Fallback: try to get from path data
            for p in paths:
                if p.layer_thickness > 0:
                    layer_thickness = p.layer_thickness
                    break
            if layer_thickness <= 0:
                logger.warning("Layer %d: No layer thickness available, skipping", layer_nr)
                return paths

        z_shift = layer_thickness // 2  # microns

        # Determine target features
        target_features = set()
        if settings.apply_inner_walls:
            target_features.add(printfeatures_pb2.INNERWALL)
        if settings.apply_outer_walls:
            target_features.add(printfeatures_pb2.OUTERWALL)

        if not target_features:
            return paths

        # Determine multiplier adjustments for first/last brick layers
        is_first_brick = (layer_nr == settings.start_layer)
        if settings.end_layer > 0:
            is_last_brick = (layer_nr == settings.end_layer - 1)
        else:
            is_last_brick = False  # Can't know without total layer count

        if is_first_brick:
            effective_multiplier = settings.extrusion_multiplier * 1.15
        elif is_last_brick:
            effective_multiplier = settings.extrusion_multiplier * 0.85
        else:
            effective_multiplier = settings.extrusion_multiplier

        # Phase 1: Collect groups of target wall path indices.
        #
        # CuraEngine inserts travel/retraction paths between wall loops of
        # the same contour. We tolerate those gaps: a group is a maximal
        # run of target-wall indices separated only by move-type paths
        # *within the same mesh*. A change in mesh_name signals an
        # inter-object boundary and breaks the group. Paths with empty
        # mesh_name are outside the per-mesh context (skirt, brim, support,
        # prime tower) and must not be grouped.
        move_features = {
            printfeatures_pb2.NONETYPE,
            printfeatures_pb2.MOVEUNRETRACTED,
            printfeatures_pb2.MOVERETRACTED,
            printfeatures_pb2.MOVEWHILERETRACTING,
            printfeatures_pb2.MOVEWHILEUNRETRACTING,
            printfeatures_pb2.STATIONARYRETRACTUNRETRACT,
        }

        groups = []          # each group is a list of indices into `paths`
        current_group = []
        current_group_mesh = ""
        for i, p in enumerate(paths):
            if p.feature in target_features:
                if not p.mesh_name:
                    # Outside-mesh wall — flush and skip (passthrough)
                    if current_group:
                        groups.append(current_group)
                        current_group = []
                        current_group_mesh = ""
                elif current_group and p.mesh_name != current_group_mesh:
                    # Wall from a different mesh → break group, start new one
                    groups.append(current_group)
                    current_group = [i]
                    current_group_mesh = p.mesh_name
                else:
                    # Same mesh (or first wall in new group)
                    current_group.append(i)
                    if not current_group_mesh:
                        current_group_mesh = p.mesh_name
            elif p.feature in move_features:
                if current_group:
                    if not p.mesh_name:
                        # Outside-mesh travel inside active group → break
                        groups.append(current_group)
                        current_group = []
                        current_group_mesh = ""
                    elif p.mesh_name != current_group_mesh:
                        # Inter-object travel → break
                        groups.append(current_group)
                        current_group = []
                        current_group_mesh = ""
                    # else: same-mesh move, tolerate
            else:
                # Any other feature → break
                if current_group:
                    groups.append(current_group)
                    current_group = []
                    current_group_mesh = ""
        if current_group:
            groups.append(current_group)

        # Phase 2: Apply shifts per group, protecting innermost wall
        inside_out = settings.inset_direction == "inside_out"
        shifted_indices = set()

        for group in groups:
            if len(group) < 2:
                continue  # single wall in contour → always protected

            # Innermost wall: first in group for inside-out, last for outside-in
            innermost_idx = group[0] if inside_out else group[-1]

            # Apply alternating shift to non-innermost walls
            wall_counter = 0
            for idx in group:
                if idx == innermost_idx:
                    continue
                if wall_counter % 2 == 0:
                    paths[idx].z_offset = z_shift
                    base = paths[idx].flow_ratio if paths[idx].flow_ratio != 0.0 else 1.0
                    paths[idx].flow_ratio = base * effective_multiplier
                    shifted_indices.add(idx)
                wall_counter += 1

        # Phase 3: Global partition — normal-Z first (all objects), shifted-Z second.
        #
        # All layer-Z paths across all objects print together, then all shifted paths.
        # This yields one Z lift per layer (not one per object).
        #
        # The shifted sub-sequence needs inter-object MOVE paths cloned from the
        # original, elevated by z_shift, so the nozzle stays up during inter-object
        # travel and doesn't extrude across the build plate.
        groups_with_shifts = [
            gi for gi, g in enumerate(groups)
            if any(i in shifted_indices for i in g)
        ]

        shifted_sequence = []
        for part_idx, gi in enumerate(groups_with_shifts):
            group = groups[gi]
            for idx in group:
                if idx in shifted_indices:
                    shifted_sequence.append(paths[idx])
            if part_idx + 1 < len(groups_with_shifts):
                next_gi = groups_with_shifts[part_idx + 1]
                cur_last = group[-1]
                next_first = groups[next_gi][0]
                # Clone MOVE paths between the two groups, elevated to shifted Z.
                for i in range(cur_last + 1, next_first):
                    if paths[i].feature in move_features:
                        clone = gcode_path_pb2.GCodePath()
                        clone.CopyFrom(paths[i])
                        clone.z_offset = z_shift
                        shifted_sequence.append(clone)

        result = [p for i, p in enumerate(paths) if i not in shifted_indices]
        result.extend(shifted_sequence)

        if shifted_indices:
            normal_count = len(result) - len(shifted_sequence)
            logger.debug(
                "Layer %d: shifted %d wall paths (z_shift=%d um, multiplier=%.3f), "
                "protected %d innermost, reordered %d+%d",
                layer_nr, len(shifted_indices), z_shift, effective_multiplier,
                len(groups), normal_count, len(shifted_indices),
            )

        return result


def serve(address: str, port: int) -> None:
    settings = BrickSettings()

    server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))

    handshake_pb2_grpc.add_HandshakeServiceServicer_to_server(
        HandshakeServicer(), server
    )
    broadcast_pb2_grpc.add_BroadcastServiceServicer_to_server(
        BroadcastServicer(settings), server
    )
    modify_pb2_grpc.add_GCodePathsModifyServiceServicer_to_server(
        GCodePathsModifyServicer(settings), server
    )

    listen_addr = f"{address}:{port}"
    server.add_insecure_port(listen_addr)
    server.start()
    logger.info("BrickLayers engine plugin listening on %s", listen_addr)

    server.wait_for_termination()


def main():
    parser = argparse.ArgumentParser(description="BrickLayers CuraEngine plugin")
    parser.add_argument("--address", default="127.0.0.1", help="Listen address")
    parser.add_argument("--port", type=int, required=True, help="Listen port")
    args = parser.parse_args()

    serve(args.address, args.port)


if __name__ == "__main__":
    main()
