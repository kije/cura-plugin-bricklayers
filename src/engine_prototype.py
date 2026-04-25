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
from dataclasses import dataclass, field, replace as dataclass_replace
from typing import Dict, List, Optional

_DEBUG_LOG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "BrickLayers_debug.jsonl"
)


def _dump_config():
    """Read BRICKLAYERS_DUMP_LAYERS env var; return (layer_set, dir) or None."""
    layers_env = os.environ.get("BRICKLAYERS_DUMP_LAYERS", "")
    if not layers_env:
        return None
    try:
        layers = {int(x.strip()) for x in layers_env.split(",") if x.strip()}
    except ValueError:
        return None
    if not layers:
        return None
    default_dir = (
        os.path.expanduser("~/Library/Logs/BrickLayers")
        if sys.platform == "darwin"
        else "/tmp/bricklayers"
    )
    dump_dir = os.environ.get("BRICKLAYERS_DUMP_DIR", default_dir)
    try:
        os.makedirs(dump_dir, exist_ok=True)
    except OSError:
        pass
    return (layers, dump_dir)


_DUMP = _dump_config()


def _dump_layer(layer_nr, extruder_nr, label, paths):
    if _DUMP is None:
        return
    layers, dump_dir = _DUMP
    if layer_nr not in layers:
        return
    filename = f"layer_{layer_nr}_ext{extruder_nr}_{label}.jsonl"
    path = os.path.join(dump_dir, filename)
    try:
        with open(path, "w") as f:
            for p in paths:
                pts = []
                if p.path and p.path.path:
                    pts = [(pt.x, pt.y, pt.z) for pt in p.path.path]
                rec = {
                    "feature": p.feature,
                    "mesh": p.mesh_name,
                    "z_offset": p.z_offset,
                    "flow_ratio": p.flow_ratio,
                    "line_width": p.line_width,
                    "retract": bool(p.retract),
                    "points": pts,
                }
                f.write(json.dumps(rec) + "\n")
    except OSError:
        pass


def _feature_name(feature_int: int) -> str:
    names = {0: "NONE", 1: "OUTERWALL", 2: "INNERWALL", 4: "SKIN", 5: "SUPPORT",
             6: "SKIRT", 7: "INFILL", 8: "MOVEUNRETRACTED", 9: "MOVERETRACTED",
             10: "PRIME", 12: "MOVEWHILERETRACTING", 13: "MOVEWHILEUNRETRACTING", 14: "STATIONARYRETRACTUNRETRACT"}
    return names.get(feature_int, f"UNKNOWN({feature_int})")


def _log_call(layer_nr, extruder_nr, req_paths, resp_paths):
    """Best-effort diagnostic logging — never fatal."""
    try:
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
    except Exception:
        pass

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
    """Settings for the brick pattern algorithm.

    ``end_layer`` holds the raw value broadcast by Cura and accepts
    Python-style negative indices (``-3`` = leave the top three layers
    plain). Callers should invoke :meth:`effective_end_layer` to resolve
    negative values to a concrete 1-indexed end (this requires
    ``machine_height_um`` and ``layer_height`` to be known; otherwise the
    resolver returns ``-1`` = "no cap" so the plugin falls back to
    "apply to all layers from start onwards").
    """
    enabled: bool = False
    start_layer: int = 2  # 0-indexed (Cura setting is 1-indexed, subtract 1)
    end_layer: int = -1   # raw — see effective_end_layer()
    apply_inner_walls: bool = True
    apply_outer_walls: bool = False
    extrusion_multiplier: float = 1.05
    layer_height: int = 0  # microns, from settings broadcast
    # Match Cura's fdmprinter default so that, if a printer doesn't broadcast
    # inset_direction (or broadcasts only overrides), we agree with what
    # CuraEngine actually does. Most modern Cura profiles use "inside_out".
    # Note: even with the correct default, the real safety net is the
    # geometry-based innermost detection in _apply_brick_pattern — this
    # setting is only used as a tiebreaker when walls share identical
    # bounding boxes.
    inset_direction: str = "inside_out"  # wall print ordering
    # XY distance (microns) above which consecutive wall paths are treated as
    # different spatial contours — each contour's innermost wall is protected
    # and the brick-pattern counter resets per contour.
    contour_break_distance: int = 2000  # 2 mm — matches Cura setting default
    # Build volume height (µm), used only to resolve negative ``end_layer``
    # values into a concrete layer index.
    machine_height_um: int = 0

    def effective_end_layer(self) -> int:
        """Resolve negative / sentinel values into a concrete 1-indexed end.

        * ``>0``           → user-provided 1-indexed last layer, returned verbatim.
        * ``0`` or ``-1``  → "apply to all layers" sentinel, returned as ``-1``.
        * ``<-1``          → python-style offset from the top: ``-3`` yields
          ``(machine_height_um // layer_height) - 3``. When
          ``machine_height_um`` or ``layer_height`` is unknown (0), falls
          back to ``-1`` (no cap) so the plugin degrades to legacy behaviour
          instead of silently clamping away the whole print.
        """
        if self.end_layer > 0:
            return self.end_layer
        if self.end_layer in (0, -1):
            return -1
        if self.layer_height > 0 and self.machine_height_um > 0:
            total = self.machine_height_um // self.layer_height
            return max(1, total + self.end_layer)
        return -1


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
        # ``_settings`` is the baseline (global) used by the GCodePathsModify
        # servicer when no extruder-specific override exists. For
        # single-extruder tests this is also the effective settings.
        self._settings = settings
        # Per-extruder overrides: index = extruder number. Populated from
        # ``extruder_settings`` in each broadcast; empty means "no per-extruder
        # overrides — fall back to global".
        self._per_extruder: List[BrickSettings] = []
        # Per-mesh overrides keyed by mesh_name. CuraEngine populates each
        # ``object_settings`` entry's map with ``mesh_name → <name>`` so we
        # can resolve per-path at modify time. Empty when no per-mesh
        # overrides are present (or when mesh_name wasn't broadcast).
        self._per_mesh: Dict[str, BrickSettings] = {}
        # Set of mesh_names whose object_settings map EXPLICITLY contained
        # brick_layers_enabled=false (user intent to disable bricks on that
        # model). Distinct from inherited defaults — see BroadcastSettings.
        self._explicitly_disabled_meshes: set = set()

    def BroadcastSettings(self, request, context):
        logger.info("Received settings broadcast")
        # Parse global_settings into the baseline in place (keeps fixture
        # refs alive for tests).
        self._parse_settings(self._settings, request.global_settings)
        # Build per-extruder list: start from a snapshot of the (freshly
        # parsed) global and overlay each extruder_settings entry.
        self._per_extruder = []
        for ext_msg in request.extruder_settings:
            ext_settings = dataclass_replace(self._settings)
            self._parse_settings(ext_settings, ext_msg)
            self._per_extruder.append(ext_settings)
        # Per-mesh overrides keyed by mesh_name. CuraEngine stores the mesh
        # name as a setting key inside each object_settings map (newer Cura
        # versions; older ones leave it blank and we can't key the entry).
        #
        # Cura sends a value for every ``settable_per_mesh`` setting — even
        # when the user never overrode it — because the per-mesh container
        # resolves against the global stack default. To avoid silently
        # disabling bricks on every mesh (default ``brick_layers_enabled``
        # is false), we track which keys are PHYSICALLY present in each
        # object_settings map as the only signal of user intent.
        self._per_mesh = {}
        self._explicitly_disabled_meshes = set()
        for obj_msg in getattr(request, "object_settings", []) or []:
            mesh_name_bytes = obj_msg.settings.get("mesh_name", b"")
            mesh_name = mesh_name_bytes.decode("utf-8", errors="replace").strip()
            mesh_settings = dataclass_replace(self._settings)
            self._parse_settings(mesh_settings, obj_msg)
            # Explicit-disable: key PRESENT in the map AND parsed to a false-ish value
            explicit_off = False
            raw = obj_msg.settings.get("brick_layers_enabled")
            if raw is not None:
                val = raw.decode("utf-8", errors="replace").strip().lower()
                explicit_off = val in ("false", "0", "no")
            if mesh_name:
                self._per_mesh[mesh_name] = mesh_settings
                if explicit_off:
                    self._explicitly_disabled_meshes.add(mesh_name)
        logger.info(
            "BrickSettings(global): enabled=%s start=%d end=%d inner=%s outer=%s "
            "multiplier=%.2f layer_height=%d inset_direction=%s contour_break=%dum "
            "machine_height=%dum extruders=%d",
            self._settings.enabled, self._settings.start_layer,
            self._settings.end_layer, self._settings.apply_inner_walls,
            self._settings.apply_outer_walls, self._settings.extrusion_multiplier,
            self._settings.layer_height, self._settings.inset_direction,
            self._settings.contour_break_distance, self._settings.machine_height_um,
            len(self._per_extruder),
        )
        for i, s in enumerate(self._per_extruder):
            logger.info(
                "BrickSettings(ext %d): enabled=%s start=%d end=%d (→eff=%d) "
                "inner=%s outer=%s multiplier=%.2f",
                i, s.enabled, s.start_layer, s.end_layer, s.effective_end_layer(),
                s.apply_inner_walls, s.apply_outer_walls, s.extrusion_multiplier,
            )
        return empty_pb2.Empty()

    def _parse_settings(self, target: BrickSettings, settings_msg) -> None:
        if settings_msg is None:
            return
        # Settings is a map<string, bytes> in the upstream proto
        settings_map = settings_msg.settings
        for name, value_bytes in settings_map.items():
            val = value_bytes.decode("utf-8", errors="replace").strip()
            if name == "brick_layers_enabled":
                target.enabled = val.lower() in ("true", "1", "yes")
            elif name == "brick_layers_start_layer":
                try:
                    target.start_layer = max(0, int(float(val)) - 1)
                except ValueError:
                    pass
            elif name == "brick_layers_end_layer":
                try:
                    target.end_layer = int(float(val))
                except ValueError:
                    pass
            elif name == "machine_height":
                try:
                    # Cura sends mm; plugin uses microns.
                    target.machine_height_um = max(0, int(float(val) * 1000))
                except ValueError:
                    pass
            elif name == "brick_layers_apply_inner_walls":
                target.apply_inner_walls = val.lower() in ("true", "1", "yes")
            elif name == "brick_layers_apply_outer_walls":
                target.apply_outer_walls = val.lower() in ("true", "1", "yes")
            elif name == "brick_layers_extrusion_multiplier":
                try:
                    target.extrusion_multiplier = float(val)
                except ValueError:
                    pass
            elif name == "layer_height":
                try:
                    # Cura sends in mm, CuraEngine uses microns
                    target.layer_height = int(float(val) * 1000)
                except ValueError:
                    pass
            elif name == "inset_direction":
                target.inset_direction = val.lower().strip()
            elif name == "brick_layers_contour_break_distance":
                try:
                    # Cura sends millimetres; plugin uses microns.
                    target.contour_break_distance = max(0, int(float(val) * 1000))
                except ValueError:
                    pass


class GCodePathsModifyServicer(modify_pb2_grpc.GCodePathsModifyServiceServicer):
    """Modifies GCode paths to implement the brick-layer wall pattern.

    For alternating wall paths, shifts Z up by half a layer height using
    the z_offset field. CuraEngine applies this offset when generating
    G-code and handles extrusion/retraction/travel automatically.
    """

    def __init__(self, settings: BrickSettings,
                 broadcast_servicer: Optional["BroadcastServicer"] = None) -> None:
        # ``_settings`` is the global baseline. When a broadcast servicer is
        # supplied, its ``_per_extruder`` list is consulted per-call to resolve
        # extruder-specific overrides. For tests that construct this servicer
        # standalone (no broadcast), the baseline is used regardless of
        # extruder_nr.
        self._settings = settings
        self._broadcast_servicer = broadcast_servicer

    def _settings_for_extruder(self, extruder_nr: int) -> BrickSettings:
        if self._broadcast_servicer is not None and extruder_nr >= 0:
            pe = self._broadcast_servicer._per_extruder
            if extruder_nr < len(pe):
                return pe[extruder_nr]
        return self._settings

    @property
    def _disabled_meshes(self) -> set:
        """Set of mesh_name values whose per-mesh override EXPLICITLY
        disables bricks. Excludes defaults inherited via the stack — see
        ``BroadcastServicer.BroadcastSettings``.
        """
        if self._broadcast_servicer is None:
            return set()
        return set(getattr(self._broadcast_servicer, "_explicitly_disabled_meshes", set()))

    def Call(self, request, context):
        context.send_initial_metadata((
            ("cura-slot-version", SLOT_VERSION),
            ("cura-plugin-name", PLUGIN_NAME),
            ("cura-plugin-version", PLUGIN_VERSION),
        ))

        layer_nr = request.layer_nr
        extruder_nr = request.extruder_nr
        paths = list(request.gcode_paths)

        _dump_layer(layer_nr, extruder_nr, "in", paths)

        settings = self._settings_for_extruder(extruder_nr)

        if not settings.enabled:
            _log_call(layer_nr, extruder_nr, paths, paths)
            _dump_layer(layer_nr, extruder_nr, "out", paths)
            return modify_pb2.CallResponse(gcode_paths=paths)

        # Check layer range
        if layer_nr < settings.start_layer:
            _log_call(layer_nr, extruder_nr, paths, paths)
            _dump_layer(layer_nr, extruder_nr, "out", paths)
            return modify_pb2.CallResponse(gcode_paths=paths)
        # Resolve raw end_layer → concrete 1-indexed last layer. Handles
        # positive values (direct), 0/-1 sentinel ("no cap"), and python-style
        # negative offsets (e.g. ``-3`` = leave top 3 layers plain).
        eff_end = settings.effective_end_layer()
        if eff_end > 0:
            end_layer_idx = eff_end - 1
            if layer_nr > end_layer_idx:
                _log_call(layer_nr, extruder_nr, paths, paths)
                _dump_layer(layer_nr, extruder_nr, "out", paths)
                return modify_pb2.CallResponse(gcode_paths=paths)

        try:
            modified = self._apply_brick_pattern(paths, layer_nr, settings)
        except Exception as e:
            logger.warning("Layer %d: brick pattern failed: %s, returning unchanged", layer_nr, e)
            modified = paths
        _log_call(layer_nr, extruder_nr, paths, modified)
        _dump_layer(layer_nr, extruder_nr, "out", modified)
        return modify_pb2.CallResponse(gcode_paths=modified)

    def _apply_brick_pattern(self, paths, layer_nr, settings: Optional[BrickSettings] = None):
        """Shift alternating wall paths up by half a layer height.

        For each contiguous group of target wall paths:
        1. The innermost wall (adjacent to infill) is never shifted,
           determined by the inset_direction setting.
        2. Remaining walls are shifted on alternating indices.
        3. After modification, paths are reordered: all normal-Z paths
           first, then all shifted paths — minimising Z oscillation.
        """
        if settings is None:
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
        eff_end = settings.effective_end_layer()
        if eff_end > 0:
            is_last_brick = (layer_nr == eff_end - 1)
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

        # Each group entry is (path_idx, preceded_by_retract). The
        # retract flag is set when a retracted move was tolerated between
        # this wall and the previous wall in the group. Phase 1.5 uses
        # this as the authoritative contour-boundary signal — immune to
        # user-tuned distance thresholds.

        # Minimum XY extent (µm) for a wall path to count as a "real" wall.
        # Shorter INNERWALL/OUTERWALL paths are gap-fills / dense-infill
        # bridges / seam fragments — shifting them to half-layer Z produces
        # the "scattered 0.1 mm wall fragments" artefact seen in Cura preview.
        _WALL_MIN_EXTENT_UM = 400

        # Per-mesh disabled set: if a mesh's per-mesh override sets
        # brick_layers_enabled=false, its walls are treated as non-target
        # (passthrough). The prototype reads this from the broadcast
        # servicer's per_mesh map when available; tests that don't wire a
        # broadcast get an empty set.
        disabled_meshes = getattr(self, "_disabled_meshes", set())

        def _wall_extent(p):
            if not p.path or len(p.path.path) < 2:
                return 0
            total = 0
            pts = p.path.path
            for j in range(1, len(pts)):
                total += abs(pts[j].x - pts[j - 1].x) + abs(pts[j].y - pts[j - 1].y)
            return total

        def _target_feature(p):
            return p.feature in target_features

        def _is_target(p):
            if not _target_feature(p):
                return False
            if p.mesh_name in disabled_meshes:
                return False
            if _wall_extent(p) < _WALL_MIN_EXTENT_UM:
                return False
            return True

        def _is_tolerated(p):
            """Wall-feature-tagged but not a shiftable wall (too short, mesh
            disabled, single-point seam anchor). Tolerate without shifting
            and without breaking the surrounding group."""
            return _target_feature(p) and not _is_target(p)

        groups = []
        current_group = []
        current_group_mesh = ""
        pending_retract = False
        for i, p in enumerate(paths):
            if _is_target(p):
                if not p.mesh_name:
                    # Outside-mesh wall — flush and skip (passthrough)
                    if current_group:
                        groups.append(current_group)
                        current_group = []
                        current_group_mesh = ""
                    pending_retract = False
                elif current_group and p.mesh_name != current_group_mesh:
                    # Wall from a different mesh → break group, start new one
                    groups.append(current_group)
                    current_group = [(i, False)]
                    current_group_mesh = p.mesh_name
                    pending_retract = False
                else:
                    # Same mesh (or first wall in new group). Carry any
                    # "pending retract" from intervening travels.
                    is_first_in_group = not current_group
                    current_group.append((i, pending_retract and not is_first_in_group))
                    if not current_group_mesh:
                        current_group_mesh = p.mesh_name
                    pending_retract = False
            elif p.feature in move_features:
                if current_group:
                    if not p.mesh_name:
                        # Outside-mesh travel inside active group → break
                        groups.append(current_group)
                        current_group = []
                        current_group_mesh = ""
                        pending_retract = False
                    elif p.mesh_name != current_group_mesh:
                        # Inter-object travel → break
                        groups.append(current_group)
                        current_group = []
                        current_group_mesh = ""
                        pending_retract = False
                    else:
                        # Same-mesh move, tolerate — but remember if it
                        # was retracted. CuraEngine retracts on travels
                        # exceeding retraction_min_travel, much larger
                        # than intra-contour wall spacing. A retracted
                        # same-mesh travel is the canonical contour
                        # boundary signal.
                        if p.retract:
                            pending_retract = True
            elif _is_tolerated(p):
                # Wall-feature path that failed _is_target — 1-point seam
                # anchor, sub-line-width fragment, or a per-mesh-disabled
                # wall. Do NOT add to the group (we don't shift it) and do
                # NOT break the group (we don't want real walls split into
                # singletons by interleaved seam markers).  ~75% of
                # CuraEngine's broadcast INNERWALLs are single-point seam
                # anchors; if we treated them as "other feature" we'd
                # destroy the grouping and emit 0 shifts.
                pass
            else:
                # Any other feature (INFILL, SKIN, SUPPORT, …) → break
                if current_group:
                    groups.append(current_group)
                    current_group = []
                    current_group_mesh = ""
                pending_retract = False
        if current_group:
            groups.append(current_group)

        # Phase 1.5: Split each group into per-contour sub-groups.
        #
        # Cluster by GLOBAL bbox-overlap union-find (not stream-adjacency).
        # CuraEngine orders walls by spatial-traversal optimisation, so
        # concentric walls of one contour can be 20+ indices apart with
        # walls from other contours interleaved between them. Any
        # stream-adjacent splitting (distance, retract, bbox-of-neighbours)
        # misses these pairs. A quadratic all-pairs bbox test correctly
        # unites every pair whose bboxes overlap into one cluster.

        def _bbox(idx):
            pts = paths[idx].path.path if paths[idx].path else []
            if not pts:
                return None
            xmin = min(p.x for p in pts)
            xmax = max(p.x for p in pts)
            ymin = min(p.y for p in pts)
            ymax = max(p.y for p in pts)
            return (xmin, xmax, ymin, ymax)

        def _bbox_overlap(a, b):
            return a[0] <= b[1] and a[1] >= b[0] and a[2] <= b[3] and a[3] >= b[2]

        def _uf_find(parent, x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def _uf_union(parent, a, b):
            ra = _uf_find(parent, a)
            rb = _uf_find(parent, b)
            if ra != rb:
                parent[ra] = rb

        sub_groups = []
        for group in groups:
            n = len(group)
            if n <= 1:
                sub_groups.append([idx for idx, _ in group])
                continue
            items = [(idx, r, _bbox(idx)) for idx, r in group]
            parent = list(range(n))
            for i in range(n):
                for j in range(i + 1, n):
                    bi = items[i][2]
                    bj = items[j][2]
                    if bi is not None and bj is not None and _bbox_overlap(bi, bj):
                        # Retract flag: adjacent-pair hard boundary — even
                        # if bboxes overlap, a retract between two adjacent
                        # walls indicates an intentional contour boundary.
                        if j == i + 1 and items[j][1]:
                            continue
                        _uf_union(parent, i, j)
            # Collect by cluster root, preserving stream order.
            clusters = {}
            first_seen = {}
            for i, (idx, _, _) in enumerate(items):
                root = _uf_find(parent, i)
                first_seen.setdefault(root, i)
                clusters.setdefault(root, []).append(idx)
            for root in sorted(clusters, key=lambda r: first_seen[r]):
                sub_groups.append(clusters[root])
        groups = sub_groups

        # Phase 2: Apply shifts per (sub-)group, protecting the innermost wall.
        #
        # Innermost wall determination is *geometry-based*: the innermost wall
        # is the one with the smallest XY bounding-box extent. This is robust
        # to inset_direction being set differently per printer (fdmprinter's
        # default is inside_out, which we can't assume is what Cura broadcasts),
        # to material_alternate_walls reversing the wall order on alternating
        # layers without updating inset_direction, and to any other quirk that
        # changes the path-list position of the spatial innermost wall.
        #
        # Fallback: when all walls in the sub-group tie for smallest extent
        # (degenerate geometry or generic test paths with identical coords),
        # use the configured inset_direction for backwards compatibility.
        inside_out = settings.inset_direction == "inside_out"
        shifted_indices = set()

        def _bbox_extent(path_obj):
            if not path_obj.path or not path_obj.path.path:
                return 1 << 62  # effectively "infinite" so it never wins
            xs = [pt.x for pt in path_obj.path.path]
            ys = [pt.y for pt in path_obj.path.path]
            return (max(xs) - min(xs)) + (max(ys) - min(ys))

        def _find_innermost(group):
            extents = [(idx, _bbox_extent(paths[idx])) for idx in group]
            min_ext = min(e for _, e in extents)
            candidates = [i for i, e in extents if e == min_ext]
            if len(candidates) == 1:
                return candidates[0]
            # Multiple walls tie → fall back to configured direction.
            return candidates[0] if inside_out else candidates[-1]

        for group in groups:
            if len(group) < 2:
                continue  # single wall in contour → always protected

            innermost_idx = _find_innermost(group)

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
        # Between consecutive shifted walls we synthesize retracted travel moves
        # from the previous wall's endpoint to the next wall's start-point.
        # This prevents extrusion lines between objects (or non-adjacent walls
        # within the same object) when the reordering removes intermediate paths.
        from cura.plugins.v0 import point3d_pb2, polygons_pb2

        def _path_last_point(p):
            if p.path and p.path.path:
                return p.path.path[-1]
            return None

        def _path_first_point(p):
            if p.path and p.path.path:
                return p.path.path[0]
            return None

        # Find a representative travel path from the input to copy speed and
        # metadata fields from. CuraEngine crashes if synthesized paths lack
        # speed_derivatives, speed_factor, or mesh_name.
        ref_travel = next(
            (p for p in paths if p.feature in move_features and p.HasField('speed_derivatives')),
            paths[0] if paths else None,
        )
        ref_speed_factor = ref_travel.speed_factor if (ref_travel and ref_travel.speed_factor != 0.0) else 1.0

        # Minimum XY distance (µm) between two points to warrant emitting
        # a synth travel path. Mirrors TRAVEL_MIN_UM in lib.rs: below this
        # threshold we skip the travel because the nozzle is effectively
        # at the destination and a sub-pixel travel can be mis-rendered as
        # a wall fragment by Cura's preview.
        _TRAVEL_MIN_UM = 100

        def _should_emit_travel(from_pt, to_pt):
            dx = abs(to_pt.x - from_pt.x)
            dy = abs(to_pt.y - from_pt.y)
            return (dx + dy) >= _TRAVEL_MIN_UM

        def _make_travel(from_pt, to_pt, dest_mesh="", *, z_offset=None):
            """Build a retracted travel path.

            All extrusion-related fields are set explicitly to zero (not
            relying on proto defaults) to guarantee the downstream G-code
            emitter cannot interpret the path as extrusion. This is our
            defence against Cura's layer preview rendering synth travels as
            bright-green ``WALL-INNER`` lines.
            """
            t = gcode_path_pb2.GCodePath()
            t.feature = printfeatures_pb2.MOVERETRACTED
            t.retract = True
            t.z_offset = z_shift if z_offset is None else z_offset
            t.speed_factor = ref_speed_factor
            t.layer_thickness = layer_thickness
            t.mesh_name = dest_mesh
            # Explicit non-extrusion contract
            t.flow = 0.0
            t.flow_ratio = 0.0
            t.line_width = 0
            t.width_factor = 0.0
            if ref_travel and ref_travel.HasField('speed_derivatives'):
                t.speed_derivatives.CopyFrom(ref_travel.speed_derivatives)
            t.path.CopyFrom(polygons_pb2.OpenPath(path=[
                point3d_pb2.Point3D(x=from_pt.x, y=from_pt.y, z=from_pt.z),
                point3d_pb2.Point3D(x=to_pt.x, y=to_pt.y, z=to_pt.z),
            ]))
            return t

        # Build one shifted sub-sequence PER (sub-)group, plus an anchor map
        # (orig_idx → group index). The Phase 3b loop inlines each group's
        # shifted run at its anchor (the last non-shifted wall of that group),
        # bracketed by bridge travels up and down. This keeps the synthesised
        # travels between shifted walls short and *inside one contour* — the
        # old global shifted run produced long cross-model diagonals between
        # shifted walls of unrelated contours that Cura's preview rendered as
        # WALL-INNER extrusions.
        shifted_per_group = [[] for _ in groups]
        for gi, group in enumerate(groups):
            seq = []
            for idx in group:
                if idx in shifted_indices:
                    if seq:
                        prev = seq[-1]
                        if prev.feature not in move_features:
                            pe = _path_last_point(prev)
                            cs = _path_first_point(paths[idx])
                            if pe and cs and _should_emit_travel(pe, cs):
                                seq.append(_make_travel(pe, cs, paths[idx].mesh_name))
                    seq.append(paths[idx])
            shifted_per_group[gi] = seq

        # Anchor = last non-shifted wall of each group that has shifts.
        anchor_of = {}  # orig_idx → gi
        for gi, group in enumerate(groups):
            if not shifted_per_group[gi]:
                continue
            for idx in reversed(group):
                if idx not in shifted_indices:
                    anchor_of[idx] = gi
                    break

        # Normal-Z sub-sequence: retain original indices to detect gaps.
        normal_z_indexed = [(i, p) for i, p in enumerate(paths) if i not in shifted_indices]

        # Phase 3b: Fill gaps in normal-Z created by removing shifted walls.
        #
        # When CuraEngine places two extrusion paths with no explicit travel between
        # them (combing / no z-hop), and a shifted wall sat between them in the
        # original sequence, removing that wall leaves the two paths adjacent in
        # normal-Z with mismatched endpoints → CuraEngine extrudes the gap.
        #
        # Detect by original-index distance: if consecutive normal-Z paths have
        # original indices i and j with j > i+1, a shifted wall was removed between
        # them. Insert MOVERETRACTED (z_offset=0) to bridge mismatched endpoints.
        # If originally adjacent (j == i+1), CuraEngine placed them back-to-back
        # on purpose (combing) — do NOT insert a spurious travel.
        result = []
        prev_state = None  # (orig_idx, is_extrusion, last_point)
        for orig_idx, path in normal_z_indexed:
            if prev_state is not None:
                prev_orig_idx, prev_is_extr, prev_end = prev_state
                if orig_idx > prev_orig_idx + 1:   # a shifted wall was removed between them
                    if prev_is_extr and path.feature not in move_features:
                        # Case A: extrusion → gap → extrusion — synthesize travel.
                        # Explicit non-extrusion contract — see _make_travel.
                        cs = _path_first_point(path)
                        if prev_end and cs and (prev_end.x != cs.x or prev_end.y != cs.y):
                            gap_travel = gcode_path_pb2.GCodePath()
                            gap_travel.feature = printfeatures_pb2.MOVERETRACTED
                            gap_travel.retract = True
                            gap_travel.z_offset = 0
                            gap_travel.speed_factor = ref_speed_factor
                            gap_travel.layer_thickness = layer_thickness
                            gap_travel.mesh_name = path.mesh_name
                            gap_travel.flow = 0.0
                            gap_travel.flow_ratio = 0.0
                            gap_travel.line_width = 0
                            gap_travel.width_factor = 0.0
                            if ref_travel and ref_travel.HasField('speed_derivatives'):
                                gap_travel.speed_derivatives.CopyFrom(ref_travel.speed_derivatives)
                            gap_travel.path.CopyFrom(polygons_pb2.OpenPath(path=[
                                point3d_pb2.Point3D(x=prev_end.x, y=prev_end.y, z=prev_end.z),
                                point3d_pb2.Point3D(x=cs.x, y=cs.y, z=cs.z),
                            ]))
                            result.append(gap_travel)
                    elif not prev_is_extr and path.feature not in move_features:
                        # Case B: travel → gap → extrusion
                        # The travel's last point was the shifted wall's start; redirect
                        # it to the next extrusion's start so the head arrives correctly.
                        cs = _path_first_point(path)
                        if (result and cs and prev_end
                                and (prev_end.x != cs.x or prev_end.y != cs.y)):
                            if result[-1].path.path:
                                result[-1].path.path[-1].CopyFrom(cs)
                    elif path.feature in move_features:
                        # Case C (travel → gap → travel) or Case D (extrusion → gap → travel):
                        # The travel's first point was aimed at the removed shifted wall's
                        # end position. For open-arc walls (start ≠ end), this leaves a gap
                        # between where the nozzle is (prev_end) and where the travel begins,
                        # which CuraEngine fills with an implicit extrusion crossing the wall.
                        # Redirect the travel's start to prev_end so they connect cleanly.
                        if prev_end and path.path.path:
                            fs = path.path.path[0]
                            if fs.x != prev_end.x or fs.y != prev_end.y:
                                path.path.path[0].x = prev_end.x
                                path.path.path[0].y = prev_end.y
                                path.path.path[0].z = prev_end.z
            new_end = _path_last_point(path)
            is_extr = path.feature not in move_features
            prev_state = (orig_idx, is_extr, new_end)
            result.append(path)

            # Anchor: inline this group's shifted run right here, bracketed by
            # bridge travels (up at z_shift to the first shifted wall, then
            # down at z_offset=0 back to the anchor's XY endpoint). Ensures
            # the nozzle returns to a known normal-Z position before the next
            # normal-Z path executes, so CuraEngine never needs to resolve a
            # mid-layer Z change implicitly.
            gi = anchor_of.get(orig_idx)
            if gi is not None and shifted_per_group[gi]:
                shifted_seq = shifted_per_group[gi]
                shifted_per_group[gi] = []  # consume
                first_start = _path_first_point(shifted_seq[0])
                last_end = _path_last_point(shifted_seq[-1])
                first_mesh = shifted_seq[0].mesh_name
                # Bridge UP
                if (new_end and first_start
                        and _should_emit_travel(new_end, first_start)):
                    result.append(_make_travel(new_end, first_start, first_mesh))
                result.extend(shifted_seq)
                # Bridge DOWN to anchor's XY endpoint at z_offset=0
                if (last_end and new_end
                        and _should_emit_travel(last_end, new_end)):
                    result.append(_make_travel(last_end, new_end, first_mesh, z_offset=0))

        # No global bridge + shifted-sequence append — Phase 3b emitted every
        # group's shifted run inline at its anchor.

        if shifted_indices:
            logger.debug(
                "Layer %d: shifted %d wall paths (z_shift=%d um, multiplier=%.3f), "
                "protected %d innermost, %d sub-groups",
                layer_nr, len(shifted_indices), z_shift, effective_multiplier,
                len(groups), len(groups),
            )

        return result


def serve(address: str, port: int) -> None:
    settings = BrickSettings()

    server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=4),
        options=[
            ('grpc.max_receive_message_length', 100 * 1024 * 1024),  # 100 MB
            ('grpc.max_send_message_length', 100 * 1024 * 1024),     # 100 MB
        ],
    )

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
