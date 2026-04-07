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
import logging
import sys
import os
from concurrent import futures
from dataclasses import dataclass, field
from typing import Dict, Optional

import grpc

# Add proto output directory to path
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "proto"))

from cura.plugins.v0 import slot_id_pb2
from cura.plugins.v0 import printfeatures_pb2
from cura.plugins.v0 import gcode_path_pb2
from cura.plugins.v0 import settings_pb2
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
SLOT_VERSION = "0.1.0-alpha.1"


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
            return modify_pb2.CallResponse(gcode_paths=paths)

        # Check layer range
        if layer_nr < self._settings.start_layer:
            return modify_pb2.CallResponse(gcode_paths=paths)
        if self._settings.end_layer > 0:
            end_layer_idx = self._settings.end_layer - 1
            if layer_nr > end_layer_idx:
                return modify_pb2.CallResponse(gcode_paths=paths)

        modified = self._apply_brick_pattern(paths, layer_nr)
        return modify_pb2.CallResponse(gcode_paths=modified)

    def _apply_brick_pattern(self, paths, layer_nr):
        """Shift alternating wall paths up by half a layer height.

        Each wall GCodePath is treated as a separate loop. Odd-numbered
        wall paths get their z_offset increased by layer_height/2.
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

        # Count wall paths and shift alternating ones
        wall_counter = 0
        modified_count = 0
        result = []

        for path in paths:
            if path.feature in target_features:
                if wall_counter % 2 == 1:
                    # Shift this wall path up by half layer height
                    path.z_offset += z_shift
                    path.flow_ratio *= effective_multiplier
                    modified_count += 1
                wall_counter += 1
            result.append(path)

        if modified_count > 0:
            logger.debug(
                "Layer %d: shifted %d/%d wall paths (z_shift=%d um, multiplier=%.3f)",
                layer_nr, modified_count, wall_counter, z_shift, effective_multiplier,
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
