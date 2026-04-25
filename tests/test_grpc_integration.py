"""Tier 1 — gRPC integration tests for the BrickLayers engine plugin.

Starts the full gRPC server (all three services) in a background thread,
then exercises it through real gRPC channels using the generated stubs.
No mocking of gRPC internals — every call travels the full serialisation
and transport stack over a loopback TCP connection.

Requirements:
    grpcio >= 1.80.0   (matches the generated stub GRPC_GENERATED_VERSION)
    protobuf >= 6.31.1 (matches generated pb2 files)
    pytest

Run from the src/ directory:
    cd src && python -m pytest tests/test_grpc_integration.py -v
"""

import os
import socket
import sys
import time
from typing import Optional

import grpc
import pytest
from concurrent import futures

# ---------------------------------------------------------------------------
# Path bootstrap — mirror what engine_prototype.py does
# ---------------------------------------------------------------------------
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC_DIR = os.path.join(_REPO_ROOT, "src")
_PROTO_DIR = os.path.join(_SRC_DIR, "proto")
for _p in (_SRC_DIR, _PROTO_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# ---------------------------------------------------------------------------
# Proto / stub imports — must come after path bootstrap
# ---------------------------------------------------------------------------
from cura.plugins.v0 import (
    gcode_path_pb2,
    point3d_pb2,
    printfeatures_pb2,
    slot_id_pb2,
)
from cura.plugins.slots.handshake.v0 import handshake_pb2, handshake_pb2_grpc
from cura.plugins.slots.broadcast.v0 import broadcast_pb2, broadcast_pb2_grpc
from cura.plugins.slots.gcode_paths.v0 import modify_pb2, modify_pb2_grpc

from engine_prototype import (
    BrickSettings,
    BroadcastServicer,
    GCodePathsModifyServicer,
    HandshakeServicer,
    PLUGIN_NAME,
    PLUGIN_VERSION,
    SLOT_VERSION,
)

# ---------------------------------------------------------------------------
# Feature constants (aliases for readability in tests)
# ---------------------------------------------------------------------------
INNERWALL = printfeatures_pb2.INNERWALL
OUTERWALL = printfeatures_pb2.OUTERWALL
SKIN = printfeatures_pb2.SKIN
INFILL = printfeatures_pb2.INFILL
SUPPORT = printfeatures_pb2.SUPPORT
MOVEUNRETRACTED = printfeatures_pb2.MOVEUNRETRACTED
MOVERETRACTED = printfeatures_pb2.MOVERETRACTED


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _free_port() -> int:
    """Return an ephemeral TCP port that is free at the time of the call."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _make_path(
    feature,
    *,
    n_points: int = 3,
    z_offset: int = 0,
    layer_thickness: int = 200,
    flow_ratio: float = 1.0,
    line_width: int = 400,
    mesh_name: str = "Mesh",
) -> gcode_path_pb2.GCodePath:
    """Construct a GCodePath protobuf message for use in tests.

    Default mesh_name="Mesh" models real CuraEngine output where wall/infill
    paths always carry a non-empty mesh name.  Set mesh_name="" explicitly to
    test outside-mesh paths (skirt, brim, support, prime tower).
    """
    path = gcode_path_pb2.GCodePath(
        feature=feature,
        z_offset=z_offset,
        layer_thickness=layer_thickness,
        flow_ratio=flow_ratio,
        line_width=line_width,
        mesh_name=mesh_name,
    )
    for i in range(n_points):
        path.path.path.append(
            point3d_pb2.Point3D(x=i * 1000, y=i * 1000, z=200)
        )
    return path


def _settings_request(global_dict: dict, extruder_dicts: list = None):
    """Build a BroadcastServiceSettingsRequest from plain Python dicts."""
    global_settings = broadcast_pb2.Settings(
        settings={k: v.encode() for k, v in global_dict.items()}
    )
    extruder_settings = []
    for d in (extruder_dicts or []):
        extruder_settings.append(
            broadcast_pb2.Settings(settings={k: v.encode() for k, v in d.items()})
        )
    return broadcast_pb2.BroadcastServiceSettingsRequest(
        global_settings=global_settings,
        extruder_settings=extruder_settings,
    )


def _call_request(paths, layer_nr: int = 5, extruder_nr: int = 0):
    """Build a GCodePathsModify CallRequest."""
    return modify_pb2.CallRequest(
        gcode_paths=paths,
        layer_nr=layer_nr,
        extruder_nr=extruder_nr,
    )


def _first_shifted_wall(result):
    """Return the first shifted INNERWALL/OUTERWALL path in a plugin response,
    or None. Used to replace `result[-1]` probes in tests — under the new
    per-sub-group inline emission, shifted walls no longer sit at the end
    of the output; they're bracketed by retracted bridge travels at their
    group's anchor position.
    """
    for p in result:
        if p.z_offset > 0 and p.feature in (INNERWALL, OUTERWALL):
            return p
    return None


def _shifted_walls(result):
    """Return all shifted INNERWALL/OUTERWALL paths in a plugin response."""
    return [p for p in result if p.z_offset > 0 and p.feature in (INNERWALL, OUTERWALL)]


# ---------------------------------------------------------------------------
# Server fixture — session-scoped, one server for all tests
# ---------------------------------------------------------------------------

class _ServerBundle:
    """Holds the running gRPC server and the shared BrickSettings object."""

    def __init__(self, address: str, settings: BrickSettings):
        self.address = address
        self.settings = settings
        self._server: Optional[grpc.Server] = None

    def start(self) -> None:
        self._server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
        handshake_pb2_grpc.add_HandshakeServiceServicer_to_server(
            HandshakeServicer(), self._server
        )
        self.broadcast_servicer = BroadcastServicer(self.settings)
        broadcast_pb2_grpc.add_BroadcastServiceServicer_to_server(
            self.broadcast_servicer, self._server
        )
        modify_pb2_grpc.add_GCodePathsModifyServiceServicer_to_server(
            GCodePathsModifyServicer(self.settings, self.broadcast_servicer),
            self._server,
        )
        self._server.add_insecure_port(self.address)
        self._server.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.stop(grace=0)


@pytest.fixture(scope="session")
def grpc_server():
    """Start the BrickLayers gRPC server once for the whole test session."""
    port = _free_port()
    address = f"127.0.0.1:{port}"
    settings = BrickSettings(
        enabled=True,
        start_layer=0,
        end_layer=-1,
        apply_inner_walls=True,
        apply_outer_walls=False,
        extrusion_multiplier=1.05,
        layer_height=200,
    )
    bundle = _ServerBundle(address, settings)
    bundle.start()

    # Brief poll to confirm the port is accepting connections
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                break
        except OSError:
            time.sleep(0.05)

    yield bundle
    bundle.stop()


@pytest.fixture
def server(grpc_server):
    """Per-test fixture: reset settings to a known enabled state."""
    s = grpc_server.settings
    s.enabled = True
    s.start_layer = 0
    s.end_layer = -1
    s.apply_inner_walls = True
    s.apply_outer_walls = False
    s.extrusion_multiplier = 1.05
    s.layer_height = 200
    s.inset_direction = "outside_in"
    # Most tests use generic _make_path coordinates that the contour-break
    # heuristic would treat as distinct contours (endpoint-to-startpoint
    # distance ≈ 2.8 mm between generic 3-point paths). Set a very large
    # threshold by default so tests that don't explicitly exercise the
    # sub-grouping logic see the "one group per mesh" behaviour the other
    # assertions were written against. Tests that want to exercise the
    # sub-grouping set a smaller value themselves.
    s.contour_break_distance = 1_000_000  # 1 m — effectively disables splitting
    s.machine_height_um = 0  # unknown by default; individual tests can set
    return grpc_server


@pytest.fixture
def channel(grpc_server):
    """Open a gRPC channel to the running server, close it after the test."""
    ch = grpc.insecure_channel(grpc_server.address)
    yield ch
    ch.close()


# ---------------------------------------------------------------------------
# Stub fixtures (created fresh per test from the shared channel)
# ---------------------------------------------------------------------------

@pytest.fixture
def handshake_stub(channel):
    return handshake_pb2_grpc.HandshakeServiceStub(channel)


@pytest.fixture
def broadcast_stub(channel):
    return broadcast_pb2_grpc.BroadcastServiceStub(channel)


@pytest.fixture
def modify_stub(channel):
    return modify_pb2_grpc.GCodePathsModifyServiceStub(channel)


# ===========================================================================
# HANDSHAKE TESTS
# ===========================================================================

class TestHandshakeRPC:
    """Verify the HandshakeService.Call RPC over the wire."""

    def test_returns_plugin_name(self, server, handshake_stub):
        req = handshake_pb2.CallRequest(
            slot_id=slot_id_pb2.GCODE_PATHS_MODIFY,
            version="0.1.0",
            plugin_name="CuraEngine",
            plugin_version="5.10.0",
        )
        resp = handshake_stub.Call(req)
        assert resp.plugin_name == PLUGIN_NAME

    def test_returns_plugin_version(self, server, handshake_stub):
        req = handshake_pb2.CallRequest(
            slot_id=slot_id_pb2.GCODE_PATHS_MODIFY,
        )
        resp = handshake_stub.Call(req)
        assert resp.plugin_version == PLUGIN_VERSION

    def test_slot_version_range_non_empty(self, server, handshake_stub):
        req = handshake_pb2.CallRequest()
        resp = handshake_stub.Call(req)
        assert resp.slot_version_range == SLOT_VERSION

    def test_subscribes_to_settings_broadcast(self, server, handshake_stub):
        req = handshake_pb2.CallRequest()
        resp = handshake_stub.Call(req)
        assert slot_id_pb2.SETTINGS_BROADCAST in resp.broadcast_subscriptions

    def test_metadata_contains_slot_version(self, server, channel):
        """Initial metadata must carry cura-slot-version."""
        stub = handshake_pb2_grpc.HandshakeServiceStub(channel)
        req = handshake_pb2.CallRequest()
        call = stub.Call.with_call(req)
        response, metadata_call = call
        initial_meta = dict(metadata_call.initial_metadata())
        assert "cura-slot-version" in initial_meta
        assert initial_meta["cura-slot-version"] == SLOT_VERSION

    def test_metadata_contains_plugin_name(self, server, channel):
        stub = handshake_pb2_grpc.HandshakeServiceStub(channel)
        call = stub.Call.with_call(handshake_pb2.CallRequest())
        _, metadata_call = call
        meta = dict(metadata_call.initial_metadata())
        assert meta.get("cura-plugin-name") == PLUGIN_NAME

    def test_metadata_contains_plugin_version(self, server, channel):
        stub = handshake_pb2_grpc.HandshakeServiceStub(channel)
        call = stub.Call.with_call(handshake_pb2.CallRequest())
        _, metadata_call = call
        meta = dict(metadata_call.initial_metadata())
        assert meta.get("cura-plugin-version") == PLUGIN_VERSION


# ===========================================================================
# BROADCAST SETTINGS TESTS
# ===========================================================================

class TestBroadcastRPC:
    """Verify BroadcastService.BroadcastSettings parses settings over the wire."""

    def test_enabled_flag_parsed(self, server, broadcast_stub):
        broadcast_stub.BroadcastSettings(
            _settings_request({"brick_layers_enabled": "true"})
        )
        assert server.settings.enabled is True

    def test_disabled_flag_parsed(self, server, broadcast_stub):
        broadcast_stub.BroadcastSettings(
            _settings_request({"brick_layers_enabled": "false"})
        )
        assert server.settings.enabled is False

    def test_start_layer_converted_to_zero_indexed(self, server, broadcast_stub):
        # Cura sends 1-indexed; plugin converts to 0-indexed
        broadcast_stub.BroadcastSettings(
            _settings_request({"brick_layers_start_layer": "5"})
        )
        assert server.settings.start_layer == 4

    def test_end_layer_stored_as_sent(self, server, broadcast_stub):
        broadcast_stub.BroadcastSettings(
            _settings_request({"brick_layers_end_layer": "20"})
        )
        assert server.settings.end_layer == 20

    def test_layer_height_mm_to_microns(self, server, broadcast_stub):
        broadcast_stub.BroadcastSettings(
            _settings_request({"layer_height": "0.3"})
        )
        assert server.settings.layer_height == 300

    def test_extrusion_multiplier_parsed(self, server, broadcast_stub):
        broadcast_stub.BroadcastSettings(
            _settings_request({"brick_layers_extrusion_multiplier": "1.10"})
        )
        assert abs(server.settings.extrusion_multiplier - 1.10) < 1e-6

    def test_inner_walls_flag(self, server, broadcast_stub):
        broadcast_stub.BroadcastSettings(
            _settings_request({"brick_layers_apply_inner_walls": "false"})
        )
        assert server.settings.apply_inner_walls is False

    def test_outer_walls_flag(self, server, broadcast_stub):
        broadcast_stub.BroadcastSettings(
            _settings_request({"brick_layers_apply_outer_walls": "true"})
        )
        assert server.settings.apply_outer_walls is True

    def test_extruder_settings_are_parsed(self, server, broadcast_stub):
        broadcast_stub.BroadcastSettings(
            _settings_request(
                global_dict={},
                extruder_dicts=[{"brick_layers_enabled": "true"}],
            )
        )
        assert server.settings.enabled is True

    def test_second_broadcast_overwrites_first(self, server, broadcast_stub):
        broadcast_stub.BroadcastSettings(
            _settings_request({"brick_layers_enabled": "true"})
        )
        assert server.settings.enabled is True
        broadcast_stub.BroadcastSettings(
            _settings_request({"brick_layers_enabled": "false"})
        )
        assert server.settings.enabled is False

    def test_unknown_setting_is_ignored(self, server, broadcast_stub):
        """Unrecognised keys must not crash or alter known settings."""
        prev_enabled = server.settings.enabled
        broadcast_stub.BroadcastSettings(
            _settings_request({"completely_unknown_key": "whatever"})
        )
        assert server.settings.enabled == prev_enabled

    def test_returns_empty_response(self, server, broadcast_stub):
        """BroadcastSettings RPC must succeed (return Empty, not raise)."""
        broadcast_stub.BroadcastSettings(_settings_request({}))  # no exception


# ===========================================================================
# GCODE PATH MODIFICATION TESTS
# ===========================================================================

class TestGCodePathsModifyRPC:
    """Verify GCodePathsModifyService.Call over the wire.

    Each test reconfigures server.settings before calling the RPC, because
    the session-scoped server shares one BrickSettings instance.  The per-test
    `server` fixture resets it to a safe enabled baseline first.
    """

    # --- alternating z_offset logic ---

    def test_even_wall_paths_not_shifted(self, server, modify_stub):
        paths = [_make_path(INNERWALL), _make_path(INNERWALL)]
        resp = modify_stub.Call(_call_request(paths))
        result = list(resp.gcode_paths)
        assert result[0].z_offset == 0

    def test_odd_wall_paths_shifted_by_half_layer_height(self, server, modify_stub):
        # layer_height=200 → z_shift = 100; need 3 walls (innermost protected)
        paths = [_make_path(INNERWALL) for _ in range(3)]
        resp = modify_stub.Call(_call_request(paths))
        result = list(resp.gcode_paths)
        shifted = _first_shifted_wall(result)
        assert shifted is not None
        assert shifted.z_offset == 100

    def test_four_walls_alternating_pattern(self, server, modify_stub):
        # 4 walls: wall[3] innermost (protected)
        # Non-innermost: wall[0](c=0,YES), wall[1](c=1,no), wall[2](c=2,YES)
        # After reorder: 2 normal-Z, then transition travel(s), then 2 shifted
        paths = [_make_path(INNERWALL) for _ in range(4)]
        resp = modify_stub.Call(_call_request(paths))
        result = list(resp.gcode_paths)
        walls = [p for p in result if p.feature == INNERWALL]
        wall_offsets = [p.z_offset for p in walls]
        assert wall_offsets == [0, 0, 100, 100]

    def test_z_offset_equals_half_layer_height(self, server, modify_stub):
        server.settings.layer_height = 300
        paths = [_make_path(INNERWALL) for _ in range(3)]
        resp = modify_stub.Call(_call_request(paths))
        result = list(resp.gcode_paths)
        shifted = _first_shifted_wall(result)
        assert shifted is not None
        assert shifted.z_offset == 150  # 300 // 2

    def test_existing_z_offset_is_overwritten(self, server, modify_stub):
        # CuraEngine re-sends paths with stale z_offset values from the previous
        # layer's response. The algorithm must assign z_shift absolutely (= not +=)
        # so that stale offsets don't accumulate across layers.
        paths = [
            _make_path(INNERWALL, z_offset=50),
            _make_path(INNERWALL, z_offset=50),
            _make_path(INNERWALL, z_offset=50),
        ]
        resp = modify_stub.Call(_call_request(paths))
        result = list(resp.gcode_paths)
        assert result[0].z_offset == 50   # wall 0 (counter=0) — unchanged (not shifted)
        assert result[1].z_offset == 50   # innermost — protected, never shifted
        assert result[2].z_offset == 100  # wall 1 (counter=1): absolute z_shift, ignores stale 50

    # --- flow_ratio ---

    def test_even_wall_flow_ratio_unchanged(self, server, modify_stub):
        paths = [_make_path(INNERWALL) for _ in range(3)]
        resp = modify_stub.Call(_call_request(paths))
        result = list(resp.gcode_paths)
        assert abs(result[0].flow_ratio - 1.0) < 1e-6

    def test_odd_wall_flow_ratio_multiplied(self, server, modify_stub):
        server.settings.extrusion_multiplier = 1.10
        paths = [_make_path(INNERWALL) for _ in range(3)]
        resp = modify_stub.Call(_call_request(paths))
        result = list(resp.gcode_paths)
        shifted = _first_shifted_wall(result)
        assert shifted is not None
        assert abs(shifted.flow_ratio - 1.10) < 1e-4

    # --- non-wall paths pass through ---

    def test_skin_paths_unchanged(self, server, modify_stub):
        paths = [_make_path(SKIN), _make_path(SKIN)]
        resp = modify_stub.Call(_call_request(paths))
        for p in resp.gcode_paths:
            assert p.z_offset == 0
            assert abs(p.flow_ratio - 1.0) < 1e-6

    def test_infill_paths_unchanged(self, server, modify_stub):
        paths = [_make_path(INFILL), _make_path(INFILL)]
        resp = modify_stub.Call(_call_request(paths))
        for p in resp.gcode_paths:
            assert p.z_offset == 0

    def test_support_paths_unchanged(self, server, modify_stub):
        paths = [_make_path(SUPPORT), _make_path(SUPPORT)]
        resp = modify_stub.Call(_call_request(paths))
        for p in resp.gcode_paths:
            assert p.z_offset == 0

    def test_mixed_types_only_walls_modified(self, server, modify_stub):
        # 3 contiguous inner walls surrounded by non-wall paths
        paths = [
            _make_path(SKIN),
            _make_path(INNERWALL),
            _make_path(INNERWALL),
            _make_path(INNERWALL),
            _make_path(INFILL),
            _make_path(SUPPORT),
        ]
        resp = modify_stub.Call(_call_request(paths))
        result = list(resp.gcode_paths)
        # All original paths present; synthesized travels may be added
        original_features = sorted(p.feature for p in paths)
        result_features = sorted(p.feature for p in result if not (p.feature == MOVERETRACTED and p.retract))
        assert result_features == original_features
        # After reorder: all normal-Z first, then shifted
        shifted = [p for p in result if p.z_offset > 0 and p.feature in (INNERWALL, OUTERWALL)]
        assert len(shifted) == 1
        assert shifted[0].feature == INNERWALL
        # Non-wall, non-travel paths must all be at z_offset=0
        for p in result:
            if p.feature not in (INNERWALL, OUTERWALL, MOVERETRACTED):
                assert p.z_offset == 0

    # --- path ordering preserved ---

    def test_response_wall_count_matches_request(self, server, modify_stub):
        """All input wall paths are present; synthesized travels may be added."""
        paths = [_make_path(INNERWALL) for _ in range(6)]
        resp = modify_stub.Call(_call_request(paths))
        result = list(resp.gcode_paths)
        wall_count = sum(1 for p in result if p.feature == INNERWALL)
        assert wall_count == 6
        # Any extra paths must be synthesized retracted travels (shifted-Z or gap-fill)
        extra = [p for p in result if p.feature == MOVERETRACTED]
        assert len(result) == 6 + len(extra)

    def test_response_preserves_all_feature_types(self, server, modify_stub):
        """All input feature types are present in output (order may differ due to reorder)."""
        features = [SKIN, INNERWALL, INFILL, OUTERWALL, SUPPORT]
        paths = [_make_path(f) for f in features]
        resp = modify_stub.Call(_call_request(paths))
        result = list(resp.gcode_paths)
        result_features = sorted([p.feature for p in result])
        assert result_features == sorted(features)

    # --- wall type selection ---

    def test_inner_walls_only_outer_untouched(self, server, modify_stub):
        server.settings.apply_inner_walls = True
        server.settings.apply_outer_walls = False
        paths = [
            _make_path(OUTERWALL),
            _make_path(OUTERWALL),
            _make_path(INNERWALL),
            _make_path(INNERWALL),
            _make_path(INNERWALL),
        ]
        resp = modify_stub.Call(_call_request(paths))
        result = list(resp.gcode_paths)
        # Outer walls untouched, 1 inner wall shifted (last after reorder)
        assert all(p.z_offset == 0 for p in result if p.feature == OUTERWALL)
        shifted = [p for p in result if p.z_offset > 0 and p.feature in (INNERWALL, OUTERWALL)]
        assert len(shifted) == 1
        assert shifted[0].feature == INNERWALL

    def test_outer_walls_only_inner_untouched(self, server, modify_stub):
        server.settings.apply_inner_walls = False
        server.settings.apply_outer_walls = True
        paths = [
            _make_path(OUTERWALL),
            _make_path(OUTERWALL),
            _make_path(OUTERWALL),
            _make_path(INNERWALL),
            _make_path(INNERWALL),
        ]
        resp = modify_stub.Call(_call_request(paths))
        result = list(resp.gcode_paths)
        # Inner walls untouched, 1 outer wall shifted
        assert all(p.z_offset == 0 for p in result if p.feature == INNERWALL)
        shifted = [p for p in result if p.z_offset > 0 and p.feature in (INNERWALL, OUTERWALL)]
        assert len(shifted) == 1
        assert shifted[0].feature == OUTERWALL

    def test_both_wall_types_share_group(self, server, modify_stub):
        """When both wall types targeted, they form one contiguous group."""
        server.settings.apply_inner_walls = True
        server.settings.apply_outer_walls = True
        # 4 contiguous walls: wall[3] innermost (protected)
        # Non-innermost: [0](c=0,YES), [1](c=1,no), [2](c=2,YES)
        paths = [
            _make_path(OUTERWALL),
            _make_path(INNERWALL),
            _make_path(OUTERWALL),
            _make_path(INNERWALL),
        ]
        resp = modify_stub.Call(_call_request(paths))
        result = list(resp.gcode_paths)
        shifted = [p for p in result if p.z_offset > 0 and p.feature in (INNERWALL, OUTERWALL)]
        assert len(shifted) == 2

    def test_no_wall_types_selected_noop(self, server, modify_stub):
        server.settings.apply_inner_walls = False
        server.settings.apply_outer_walls = False
        paths = [_make_path(INNERWALL), _make_path(OUTERWALL)]
        resp = modify_stub.Call(_call_request(paths))
        for p in resp.gcode_paths:
            assert p.z_offset == 0

    # --- layer range ---

    def test_layer_below_start_passes_through(self, server, modify_stub):
        server.settings.start_layer = 5
        paths = [_make_path(INNERWALL) for _ in range(3)]
        resp = modify_stub.Call(_call_request(paths, layer_nr=3))
        result = list(resp.gcode_paths)
        assert all(p.z_offset == 0 for p in result)

    def test_layer_at_start_is_modified(self, server, modify_stub):
        server.settings.start_layer = 5
        paths = [_make_path(INNERWALL) for _ in range(3)]
        resp = modify_stub.Call(_call_request(paths, layer_nr=5))
        result = list(resp.gcode_paths)
        shifted = _first_shifted_wall(result)
        assert shifted is not None and shifted.z_offset == 100

    def test_layer_above_end_passes_through(self, server, modify_stub):
        server.settings.end_layer = 10
        paths = [_make_path(INNERWALL) for _ in range(3)]
        # end_layer=10 means last modified index is 9; layer 10 is beyond
        resp = modify_stub.Call(_call_request(paths, layer_nr=10))
        result = list(resp.gcode_paths)
        assert all(p.z_offset == 0 for p in result)

    def test_layer_at_last_index_is_modified(self, server, modify_stub):
        server.settings.end_layer = 10
        paths = [_make_path(INNERWALL) for _ in range(3)]
        resp = modify_stub.Call(_call_request(paths, layer_nr=9))
        result = list(resp.gcode_paths)
        shifted = _first_shifted_wall(result)
        assert shifted is not None and shifted.z_offset == 100

    def test_end_layer_minus_one_means_unlimited(self, server, modify_stub):
        server.settings.end_layer = -1
        paths = [_make_path(INNERWALL) for _ in range(3)]
        resp = modify_stub.Call(_call_request(paths, layer_nr=9999))
        result = list(resp.gcode_paths)
        shifted = _first_shifted_wall(result)
        assert shifted is not None and shifted.z_offset == 100

    # --- first / last brick layer multiplier ---

    def test_first_brick_layer_115x_multiplier(self, server, modify_stub):
        server.settings.start_layer = 2
        server.settings.extrusion_multiplier = 1.05
        paths = [_make_path(INNERWALL) for _ in range(3)]
        resp = modify_stub.Call(_call_request(paths, layer_nr=2))
        result = list(resp.gcode_paths)
        expected = 1.05 * 1.15
        shifted = [p for p in result if p.z_offset > 0 and p.feature == INNERWALL]
        assert shifted, "expected at least one shifted wall"
        assert abs(shifted[0].flow_ratio - expected) < 1e-4

    def test_last_brick_layer_085x_multiplier(self, server, modify_stub):
        server.settings.end_layer = 10
        server.settings.extrusion_multiplier = 1.05
        paths = [_make_path(INNERWALL) for _ in range(3)]
        resp = modify_stub.Call(_call_request(paths, layer_nr=9))  # end_layer - 1
        result = list(resp.gcode_paths)
        expected = 1.05 * 0.85
        shifted = [p for p in result if p.z_offset > 0 and p.feature == INNERWALL]
        assert shifted, "expected at least one shifted wall"
        assert abs(shifted[0].flow_ratio - expected) < 1e-4

    def test_middle_layer_uses_base_multiplier(self, server, modify_stub):
        server.settings.start_layer = 2
        server.settings.end_layer = 10
        server.settings.extrusion_multiplier = 1.05
        paths = [_make_path(INNERWALL) for _ in range(3)]
        resp = modify_stub.Call(_call_request(paths, layer_nr=5))
        result = list(resp.gcode_paths)
        shifted = [p for p in result if p.z_offset > 0 and p.feature == INNERWALL]
        assert shifted, "expected at least one shifted wall"
        assert abs(shifted[0].flow_ratio - 1.05) < 1e-4

    # --- disabled state ---

    def test_disabled_returns_paths_unmodified(self, server, modify_stub):
        server.settings.enabled = False
        paths = [_make_path(INNERWALL), _make_path(INNERWALL)]
        resp = modify_stub.Call(_call_request(paths))
        result = list(resp.gcode_paths)
        assert result[0].z_offset == 0
        assert result[1].z_offset == 0

    def test_disabled_flow_ratio_unchanged(self, server, modify_stub):
        server.settings.enabled = False
        paths = [_make_path(INNERWALL), _make_path(INNERWALL)]
        resp = modify_stub.Call(_call_request(paths))
        result = list(resp.gcode_paths)
        assert abs(result[1].flow_ratio - 1.0) < 1e-6

    # --- edge cases ---

    def test_empty_paths_returns_empty(self, server, modify_stub):
        resp = modify_stub.Call(_call_request([]))
        assert len(list(resp.gcode_paths)) == 0

    def test_single_wall_path_not_shifted(self, server, modify_stub):
        # Only one wall → wall_counter=0 (even) → no shift
        paths = [_make_path(INNERWALL)]
        resp = modify_stub.Call(_call_request(paths))
        result = list(resp.gcode_paths)
        assert result[0].z_offset == 0

    def test_no_wall_paths_all_pass_through(self, server, modify_stub):
        paths = [_make_path(SKIN), _make_path(INFILL), _make_path(SUPPORT)]
        resp = modify_stub.Call(_call_request(paths))
        for p in resp.gcode_paths:
            assert p.z_offset == 0
            assert abs(p.flow_ratio - 1.0) < 1e-6

    def test_zero_layer_height_in_settings_falls_back_to_path(self, server, modify_stub):
        server.settings.layer_height = 0
        paths = [
            _make_path(INNERWALL, layer_thickness=400),
            _make_path(INNERWALL, layer_thickness=400),
            _make_path(INNERWALL, layer_thickness=400),
        ]
        resp = modify_stub.Call(_call_request(paths))
        result = list(resp.gcode_paths)
        shifted_walls = [p for p in result if p.z_offset > 0 and p.feature == INNERWALL]
        assert shifted_walls, "expected at least one shifted wall"
        assert shifted_walls[0].z_offset == 200  # 400 // 2 from path data

    def test_zero_layer_height_everywhere_skips_modification(self, server, modify_stub):
        server.settings.layer_height = 0
        paths = [
            _make_path(INNERWALL, layer_thickness=0),
            _make_path(INNERWALL, layer_thickness=0),
        ]
        resp = modify_stub.Call(_call_request(paths))
        result = list(resp.gcode_paths)
        assert result[0].z_offset == 0
        assert result[1].z_offset == 0

    def test_wall_counter_resets_per_rpc_call(self, server, modify_stub):
        """Two separate layer calls must each start their wall counter at zero."""
        paths = [_make_path(INNERWALL) for _ in range(3)]
        resp1 = modify_stub.Call(_call_request(paths, layer_nr=5))
        paths2 = [_make_path(INNERWALL) for _ in range(3)]
        resp2 = modify_stub.Call(_call_request(paths2, layer_nr=6))
        r1 = list(resp1.gcode_paths)
        r2 = list(resp2.gcode_paths)
        # Both calls should have exactly 1 shifted wall
        shifted1 = [p for p in r1 if p.z_offset > 0 and p.feature == INNERWALL]
        shifted2 = [p for p in r2 if p.z_offset > 0 and p.feature == INNERWALL]
        assert len(shifted1) == 1 and shifted1[0].z_offset == 100
        assert len(shifted2) == 1 and shifted2[0].z_offset == 100

    def test_metadata_slot_version_present(self, server, channel):
        """GCodePathsModify response must include cura-slot-version metadata."""
        stub = modify_pb2_grpc.GCodePathsModifyServiceStub(channel)
        paths = [_make_path(INNERWALL)]
        call = stub.Call.with_call(_call_request(paths))
        _, metadata_call = call
        meta = dict(metadata_call.initial_metadata())
        assert "cura-slot-version" in meta
        assert meta["cura-slot-version"] == SLOT_VERSION

    # --- full lifecycle sequence (handshake → broadcast → modify) ---

    def test_full_plugin_lifecycle(self, server, channel):
        """Simulate the CuraEngine connection sequence end to end."""
        # Step 1: Handshake
        hs_stub = handshake_pb2_grpc.HandshakeServiceStub(channel)
        hs_resp = hs_stub.Call(handshake_pb2.CallRequest(
            slot_id=slot_id_pb2.GCODE_PATHS_MODIFY,
            version="0.1.0",
            plugin_name="CuraEngine",
            plugin_version="5.10.0",
        ))
        assert hs_resp.plugin_name == PLUGIN_NAME
        assert slot_id_pb2.SETTINGS_BROADCAST in hs_resp.broadcast_subscriptions

        # Step 2: Settings broadcast
        bc_stub = broadcast_pb2_grpc.BroadcastServiceStub(channel)
        bc_stub.BroadcastSettings(_settings_request({
            "brick_layers_enabled": "true",
            "layer_height": "0.2",
            "brick_layers_extrusion_multiplier": "1.05",
            "brick_layers_apply_inner_walls": "true",
            "brick_layers_apply_outer_walls": "false",
        }))
        assert server.settings.enabled is True
        assert server.settings.layer_height == 200

        # Step 3: Modify paths for layer 3 (3 inner walls + skin)
        mod_stub = modify_pb2_grpc.GCodePathsModifyServiceStub(channel)
        paths = [
            _make_path(INNERWALL),
            _make_path(INNERWALL),
            _make_path(INNERWALL),
            _make_path(SKIN),
        ]
        resp = mod_stub.Call(_call_request(paths, layer_nr=3))
        result = list(resp.gcode_paths)
        # 1 wall shifted, SKIN unchanged, shifted wall last (reordered)
        # Synthesized retracted travel may be added between normal and shifted
        shifted = [p for p in result if p.z_offset > 0 and p.feature in (INNERWALL, OUTERWALL)]
        assert len(shifted) == 1
        assert shifted[0].z_offset == 100
        assert all(p.z_offset == 0 for p in result if p.feature == SKIN)


# ===========================================================================
# INNERMOST WALL PROTECTION TESTS
# ===========================================================================

class TestInnermostWallProtection:
    """The innermost wall (adjacent to infill) must never be shifted.

    With outside_in ordering (default), the innermost wall is the LAST
    in each contiguous group of target wall paths.
    With inside_out ordering, it is the FIRST.

    This prevents collisions between infill and raised wall beads.
    Minimum 3 contiguous inner walls needed for any shifting to occur
    (1 innermost protected + at least 2 non-innermost for alternation).
    """

    # --- outside_in (default) ---

    def test_three_walls_outside_in_innermost_protected(self, server, modify_stub):
        """3 contiguous inner walls: wall[2] is innermost (last), protected.
        Non-innermost: wall[0] (counter=0, no), wall[1] (counter=1, YES).
        """
        paths = [_make_path(INNERWALL) for _ in range(3)]
        resp = modify_stub.Call(_call_request(paths))
        result = list(resp.gcode_paths)
        shifted = [p for p in result if p.z_offset > 0 and p.feature in (INNERWALL, OUTERWALL)]
        unshifted = [p for p in result if p.z_offset == 0 and p.feature in (INNERWALL, OUTERWALL)]
        assert len(shifted) == 1
        assert shifted[0].z_offset == 100  # layer_height=200, shift=100
        assert len(unshifted) == 2  # wall[0] + innermost wall[2]

    def test_four_walls_outside_in_two_shifted(self, server, modify_stub):
        """4 inner walls: wall[3] protected (innermost).
        Non-innermost: wall[0](c=0,YES), wall[1](c=1,no), wall[2](c=2,YES).
        """
        paths = [_make_path(INNERWALL) for _ in range(4)]
        resp = modify_stub.Call(_call_request(paths))
        result = list(resp.gcode_paths)
        shifted = [p for p in result if p.z_offset > 0 and p.feature in (INNERWALL, OUTERWALL)]
        assert len(shifted) == 2

    def test_five_walls_outside_in_two_shifted(self, server, modify_stub):
        """5 inner walls: wall[4] protected.
        Non-innermost: wall[0](c=0,no), wall[1](c=1,YES), wall[2](c=2,no), wall[3](c=3,YES).
        """
        paths = [_make_path(INNERWALL) for _ in range(5)]
        resp = modify_stub.Call(_call_request(paths))
        result = list(resp.gcode_paths)
        shifted = [p for p in result if p.z_offset > 0 and p.feature in (INNERWALL, OUTERWALL)]
        assert len(shifted) == 2

    def test_single_wall_always_protected(self, server, modify_stub):
        """1 inner wall = it IS the innermost, never shifted."""
        paths = [_make_path(INNERWALL)]
        resp = modify_stub.Call(_call_request(paths))
        result = list(resp.gcode_paths)
        assert result[0].z_offset == 0

    def test_two_walls_one_shifted(self, server, modify_stub):
        """2 inner walls: wall[1] protected (innermost).
        wall[0] is the only non-innermost, counter=0 → shifted.
        """
        paths = [_make_path(INNERWALL), _make_path(INNERWALL)]
        resp = modify_stub.Call(_call_request(paths))
        result = list(resp.gcode_paths)
        shifted = [p for p in result if p.z_offset > 0 and p.feature in (INNERWALL, OUTERWALL)]
        assert len(shifted) == 1
        assert shifted[0].z_offset == 100

    # --- inside_out ---

    def test_three_walls_inside_out_first_protected(self, server, modify_stub):
        """With inside_out, wall[0] is innermost (first), protected.
        Non-innermost: wall[1](c=0,no), wall[2](c=1,YES).
        """
        server.settings.inset_direction = "inside_out"
        paths = [_make_path(INNERWALL) for _ in range(3)]
        resp = modify_stub.Call(_call_request(paths))
        result = list(resp.gcode_paths)
        shifted = [p for p in result if p.z_offset > 0 and p.feature in (INNERWALL, OUTERWALL)]
        unshifted = [p for p in result if p.z_offset == 0 and p.feature in (INNERWALL, OUTERWALL)]
        assert len(shifted) == 1
        assert len(unshifted) == 2

    # --- realistic contour layout ---

    def test_realistic_contour_outerwall_plus_three_inner(self, server, modify_stub):
        """OUTERWALL + 3 INNERWALL + INFILL (only inner walls targeted).
        Inner wall group: indices 1,2,3. Innermost = index 3 (outside_in).
        Non-innermost: idx 1 (c=0, no), idx 2 (c=1, YES).
        Result: only wall at index 2 shifted.
        """
        paths = [
            _make_path(OUTERWALL),
            _make_path(INNERWALL),
            _make_path(INNERWALL),
            _make_path(INNERWALL),
            _make_path(INFILL),
        ]
        resp = modify_stub.Call(_call_request(paths))
        result = list(resp.gcode_paths)
        shifted = [p for p in result if p.z_offset > 0 and p.feature in (INNERWALL, OUTERWALL)]
        assert len(shifted) == 1
        assert shifted[0].z_offset == 100
        # Infill must NOT be shifted
        infill_paths = [p for p in result if p.feature == INFILL]
        assert all(p.z_offset == 0 for p in infill_paths)

    def test_two_separate_wall_groups_each_protected(self, server, modify_stub):
        """Two contiguous wall groups separated by infill.
        Each group's innermost is protected independently.
        """
        paths = [
            _make_path(INNERWALL),  # group 1: single wall → protected, no shift
            _make_path(INFILL),
            _make_path(INNERWALL),  # group 2 start
            _make_path(INNERWALL),
            _make_path(INNERWALL),  # group 2 end (innermost, protected)
        ]
        resp = modify_stub.Call(_call_request(paths))
        result = list(resp.gcode_paths)
        shifted = [p for p in result if p.z_offset > 0 and p.feature in (INNERWALL, OUTERWALL)]
        # Group 1: 1 wall → protected, no shift
        # Group 2: 3 walls → innermost protected, wall counter 0 (no), 1 (YES)
        assert len(shifted) == 1

    # --- inset_direction broadcast parsing ---

    def test_inset_direction_parsed_from_broadcast(self, server, broadcast_stub):
        """inset_direction is parsed from the CuraEngine settings broadcast."""
        broadcast_stub.BroadcastSettings(
            _settings_request({"inset_direction": "inside_out"})
        )
        assert server.settings.inset_direction == "inside_out"

    def test_inset_direction_defaults_to_outside_in(self, server, modify_stub):
        """Default inset_direction is outside_in."""
        assert server.settings.inset_direction == "outside_in"


# ===========================================================================
# TRAVEL-GAP TOLERANCE TESTS
# ===========================================================================

class TestTravelGapTolerance:
    """CuraEngine inserts travel/retraction paths between wall loops of
    the same contour.  The grouping logic must tolerate those gaps and
    still treat the surrounding walls as one group.
    """

    def test_two_walls_separated_by_travel_still_grouped(self, server, modify_stub):
        """Two INNERWALL paths with a MOVEUNRETRACTED between them should
        form a single group and one wall should be shifted."""
        paths = [
            _make_path(INNERWALL),
            _make_path(MOVEUNRETRACTED),
            _make_path(INNERWALL),
        ]
        resp = modify_stub.Call(_call_request(paths))
        offsets = [p.z_offset for p in resp.gcode_paths]
        assert any(o != 0 for o in offsets), "at least one wall must be shifted"

    def test_three_walls_with_travel_gaps(self, server, modify_stub):
        """Three INNERWALL paths separated by travel moves → one group,
        innermost protected, one wall shifted."""
        paths = [
            _make_path(INNERWALL),
            _make_path(MOVERETRACTED),
            _make_path(INNERWALL),
            _make_path(MOVEUNRETRACTED),
            _make_path(INNERWALL),
        ]
        resp = modify_stub.Call(_call_request(paths))
        result = list(resp.gcode_paths)
        wall_offsets = [p.z_offset for p in result if p.feature == INNERWALL]
        shifted = [o for o in wall_offsets if o != 0]
        assert len(shifted) == 1, f"expected 1 shifted wall, got {len(shifted)}"

    def test_realistic_cura_layer_outer_travel_inner_inner(self, server, modify_stub):
        """Realistic CuraEngine path sequence: OUTERWALL, travel, INNERWALL,
        travel, INNERWALL, SKIN, INFILL.  Inner walls should still be grouped
        and one shifted."""
        paths = [
            _make_path(OUTERWALL),
            _make_path(MOVEUNRETRACTED),
            _make_path(INNERWALL),
            _make_path(MOVEUNRETRACTED),
            _make_path(INNERWALL),
            _make_path(SKIN),
            _make_path(INFILL),
        ]
        resp = modify_stub.Call(_call_request(paths))
        result = list(resp.gcode_paths)
        inner_offsets = [p.z_offset for p in result if p.feature == INNERWALL]
        assert any(o != 0 for o in inner_offsets), "inner wall must be shifted"
        # Non-wall paths must be untouched
        for p in result:
            if p.feature in (SKIN, INFILL, MOVEUNRETRACTED):
                assert p.z_offset == 0

    def test_skin_breaks_group(self, server, modify_stub):
        """A SKIN path between two INNERWALL paths should break the group,
        resulting in two single-wall groups (both protected, neither shifted)."""
        paths = [
            _make_path(INNERWALL),
            _make_path(SKIN),
            _make_path(INNERWALL),
        ]
        resp = modify_stub.Call(_call_request(paths))
        for p in resp.gcode_paths:
            assert p.z_offset == 0, "walls in separate single-wall groups must not shift"

    def test_two_contours_with_travel_gaps(self, server, modify_stub):
        """Two separate contours (separated by INFILL), each with 2 walls
        separated by travel — each contour should have one wall shifted."""
        paths = [
            _make_path(INNERWALL),
            _make_path(MOVEUNRETRACTED),
            _make_path(INNERWALL),
            _make_path(INFILL),
            _make_path(INNERWALL),
            _make_path(MOVERETRACTED),
            _make_path(INNERWALL),
        ]
        resp = modify_stub.Call(_call_request(paths))
        result = list(resp.gcode_paths)
        inner_offsets = [p.z_offset for p in result if p.feature == INNERWALL]
        shifted = [o for o in inner_offsets if o != 0]
        assert len(shifted) == 2, f"expected 2 shifted walls (one per contour), got {len(shifted)}"


# ===========================================================================
# Z-LEVEL REORDERING TESTS
# ===========================================================================

class TestZLevelReordering:
    """After applying z_offset shifts, each (sub-)group's shifted walls are
    emitted as a contiguous block inline at that group's anchor (the last
    non-shifted wall of the group), bracketed by retracted bridge travels.

    This keeps synthesised travels between shifted walls short and inside
    one contour — preventing cross-model diagonals that Cura's preview
    would render as WALL-INNER extrusions — at the cost of one extra
    Z-hop per shifted sub-group.
    """

    def test_shifted_walls_appear_after_normal_z_paths(self, server, modify_stub):
        """Within a single-mesh group, all shifted wall paths appear after all
        non-shifted wall paths."""
        paths = [_make_path(INNERWALL) for _ in range(3)]
        resp = modify_stub.Call(_call_request(paths))
        result = list(resp.gcode_paths)
        walls = [(i, p) for i, p in enumerate(result)
                 if p.feature in (INNERWALL, OUTERWALL)]
        last_normal = max((i for i, p in walls if p.z_offset == 0), default=-1)
        first_shifted = min((i for i, p in walls if p.z_offset > 0), default=len(result))
        assert last_normal < first_shifted, (
            f"Normal-Z wall at position {last_normal} appeared after shifted "
            f"wall at position {first_shifted}"
        )

    def test_non_wall_paths_in_normal_z_group(self, server, modify_stub):
        """OUTERWALL + 3 INNERWALL + INFILL: within wall paths, non-shifted
        walls precede shifted walls.  Non-wall paths (infill) retain their
        relative position in the normal-Z portion."""
        paths = [
            _make_path(OUTERWALL),
            _make_path(INNERWALL),
            _make_path(INNERWALL),
            _make_path(INNERWALL),
            _make_path(INFILL),
        ]
        resp = modify_stub.Call(_call_request(paths))
        result = list(resp.gcode_paths)
        walls = [(i, p) for i, p in enumerate(result)
                 if p.feature in (INNERWALL, OUTERWALL)]
        last_normal = max((i for i, p in walls if p.z_offset == 0), default=-1)
        first_shifted = min((i for i, p in walls if p.z_offset > 0), default=len(result))
        assert last_normal < first_shifted, (
            f"Normal-Z wall at position {last_normal} appeared after shifted "
            f"wall at position {first_shifted}"
        )

    def test_inter_object_travel_preserved(self, server, modify_stub):
        """Shifted walls from different objects must be separated by a travel
        path in the shifted sub-sequence — otherwise the nozzle extrudes while
        crossing between objects.
        The shifted sub-sequence is global (all objects): normal-Z walls of all
        objects print first, then shifted walls of all objects, with inter-object
        travel cloned (at shifted Z) between them."""
        paths = [
            _make_path(OUTERWALL),      # obj1 outer
            _make_path(INNERWALL),      # obj1 inner (shifted)
            _make_path(INNERWALL),      # obj1 inner (innermost)
            _make_path(MOVERETRACTED),  # travel between objects
            _make_path(OUTERWALL),      # obj2 outer
            _make_path(INNERWALL),      # obj2 inner (shifted)
            _make_path(INNERWALL),      # obj2 inner (innermost)
        ]
        resp = modify_stub.Call(_call_request(paths))
        result = list(resp.gcode_paths)

        # Two shifted WALL paths (one per object) — exclude elevated MOVE clones
        shifted_walls = [
            (i, p) for i, p in enumerate(result)
            if p.z_offset > 0 and p.feature in (INNERWALL, OUTERWALL)
        ]
        assert len(shifted_walls) == 2, (
            f"Expected 2 shifted wall paths, got {len(shifted_walls)}: "
            f"{[(i, p.feature, p.z_offset) for i, p in shifted_walls]}"
        )

        # A travel path must exist between the two shifted walls in the output
        travel_between = next(
            (i for i, p in enumerate(result)
             if p.feature == MOVERETRACTED
             and shifted_walls[0][0] < i < shifted_walls[1][0]),
            None,
        )
        assert travel_between is not None, (
            f"No travel path found between shifted walls at positions "
            f"{shifted_walls[0][0]} and {shifted_walls[1][0]}; "
            f"result features: {[p.feature for p in result]}"
        )

    def test_relative_order_preserved_within_normal_z(self, server, modify_stub):
        """Normal-Z paths preserve their original relative order."""
        paths = [
            _make_path(OUTERWALL),   # 0: normal
            _make_path(INNERWALL),   # 1: normal (c=0, not shifted)
            _make_path(INNERWALL),   # 2: SHIFTED (c=1)
            _make_path(INNERWALL),   # 3: innermost, protected
            _make_path(INFILL),      # 4: normal
        ]
        resp = modify_stub.Call(_call_request(paths))
        result = list(resp.gcode_paths)
        # Normal-Z group: OUTERWALL, INNERWALL, INNERWALL(innermost), INFILL
        # (gap-fill MOVERETRACTED travels at z_offset=0 are excluded)
        normal_z = [p for p in result if p.z_offset == 0 and p.feature != MOVERETRACTED]
        features = [p.feature for p in normal_z]
        assert features == [OUTERWALL, INNERWALL, INNERWALL, INFILL]

    def test_original_paths_preserved_after_reordering(self, server, modify_stub):
        """All original paths present; only synthesized travels added."""
        paths = [
            _make_path(OUTERWALL),
            _make_path(INNERWALL),
            _make_path(INNERWALL),
            _make_path(INNERWALL),
            _make_path(INFILL),
            _make_path(SKIN),
        ]
        resp = modify_stub.Call(_call_request(paths))
        result = list(resp.gcode_paths)
        # All original feature types preserved
        original_features = sorted(p.feature for p in paths)
        result_non_travel = sorted(
            p.feature for p in result
            if not (p.feature == MOVERETRACTED and p.retract)
        )
        assert result_non_travel == original_features

    def test_shifted_group_preserves_relative_order(self, server, modify_stub):
        """If multiple walls are shifted, their relative order is preserved."""
        # 5 inner walls: walls at counter 1 and 3 get shifted
        paths = [_make_path(INNERWALL) for _ in range(5)]
        resp = modify_stub.Call(_call_request(paths))
        result = list(resp.gcode_paths)
        shifted = [p for p in result if p.z_offset > 0 and p.feature in (INNERWALL, OUTERWALL)]
        assert len(shifted) == 2
        # Both should have the same z_offset (100)
        assert all(p.z_offset == 100 for p in shifted)

    def test_no_shifting_means_no_reordering(self, server, modify_stub):
        """When nothing is shifted, path order is unchanged."""
        paths = [_make_path(SKIN), _make_path(INFILL), _make_path(SUPPORT)]
        resp = modify_stub.Call(_call_request(paths))
        result = list(resp.gcode_paths)
        assert [p.feature for p in result] == [SKIN, INFILL, SUPPORT]

    def test_open_arc_shift_redirects_following_travel(self, server, modify_stub):
        """Case C: when a shifted wall is an open arc (start ≠ end), the travel
        that originally followed it must have its first point redirected to
        where the previous travel ends. Otherwise CuraEngine bridges the gap
        with an implicit extrusion across the arc's chord — producing the
        cross-cutting lines seen in curved narrow-wall geometries.

        Sequence: T_pre → IW1(open arc, shifted) → T_post → IW2(innermost).
        After IW1 is removed from normal-Z, T_pre and T_post are adjacent
        with mismatched endpoints (T_pre ends at arc_start, T_post starts
        at arc_end). The fix redirects T_post to start at arc_start.
        """
        # Scaled to realistic µm coordinates so walls pass the min-extent
        # filter (400 µm) the algorithm uses to reject gap-fill fragments.
        # Also: IW2's bbox overlaps the arc's bbox so the new bbox-overlap
        # contour clustering unites them into one sub-group (concentric
        # innermost wall of the same contour).
        ARC_START = (1000, 1000, 200)
        ARC_MID =   (1500, 2000, 200)
        ARC_END =   (2000, 1000, 200)   # open arc: start ≠ end
        IW2_START = (1100, 1200, 200)   # innermost inset of the arc contour
        IW2_END =   (1900, 1200, 200)   # 0.8 mm long — overlaps arc bbox
        PRE_START = ( 500,  500, 200)

        def mkpath(feature, pts):
            p = gcode_path_pb2.GCodePath(
                feature=feature, mesh_name="Mesh",
                layer_thickness=200, flow_ratio=1.0, line_width=400,
            )
            for (x, y, z) in pts:
                p.path.path.append(point3d_pb2.Point3D(x=x, y=y, z=z))
            return p

        paths = [
            mkpath(MOVERETRACTED, [PRE_START, ARC_START]),            # T_pre → arc_start
            mkpath(INNERWALL,     [ARC_START, ARC_MID, ARC_END]),     # IW1 open arc (shifted)
            mkpath(MOVERETRACTED, [ARC_END, IW2_START]),              # T_post: arc_end → IW2_start
            mkpath(INNERWALL,     [IW2_START, IW2_END]),              # IW2 innermost (protected)
        ]

        resp = modify_stub.Call(_call_request(paths))
        result = list(resp.gcode_paths)

        # IW1 shifted (counter=0, not innermost); IW2 protected.
        inner_shifted = [p for p in result
                         if p.z_offset > 0 and p.feature == INNERWALL]
        assert len(inner_shifted) == 1, (
            f"Expected exactly 1 shifted inner wall, got {len(inner_shifted)}"
        )

        # Locate T_post in normal-Z (z_offset=0): the input travel ending at IW2_START.
        t_post = next(
            p for p in result
            if p.feature == MOVERETRACTED and p.z_offset == 0
            and not p.retract   # distinguishes input travels from gap-fills
            and p.path.path
            and p.path.path[-1].x == IW2_START[0]
            and p.path.path[-1].y == IW2_START[1]
        )

        # The fix: T_post's first point is redirected from ARC_END to ARC_START.
        first_pt = t_post.path.path[0]
        assert (first_pt.x, first_pt.y) == (ARC_START[0], ARC_START[1]), (
            f"T_post.path[0] should be ARC_START {ARC_START[:2]} (where T_pre ends) "
            f"after the fix, got ({first_pt.x}, {first_pt.y}). Without the fix, "
            f"CuraEngine bridges ARC_START → ARC_END as implicit extrusion."
        )

        # Verify the FULL output has no endpoint gaps: every consecutive pair
        # of paths must share an endpoint coordinate. Under per-sub-group
        # inline emission the shifted run of this group is emitted right
        # after the anchor and bracketed by bridge_UP + bridge_DOWN travels,
        # so every path in the result — including shifted walls — should be
        # endpoint-continuous with the next path.
        paths_with_geom = [p for p in result if p.path.path]
        for i in range(len(paths_with_geom) - 1):
            end_i = paths_with_geom[i].path.path[-1]
            start_j = paths_with_geom[i + 1].path.path[0]
            assert (end_i.x, end_i.y) == (start_j.x, start_j.y), (
                f"Gap between path {i} (feature={paths_with_geom[i].feature}, "
                f"z={paths_with_geom[i].z_offset}, ends at ({end_i.x},{end_i.y})) "
                f"and path {i+1} (feature={paths_with_geom[i+1].feature}, "
                f"z={paths_with_geom[i+1].z_offset}, starts at ({start_j.x},{start_j.y}))"
            )


# ===========================================================================
# MESH-NAME BOUNDARY DETECTION TESTS
# ===========================================================================

class TestMeshNameBoundary:
    """Phase 1 grouping must use mesh_name to detect inter-object boundaries.

    With GroupOuter=True, CuraEngine groups outer walls from all objects
    together, then inner walls. The inter-object MOVERETRACTED between
    inner wall groups carries the destination mesh's name. Without
    mesh_name tracking, all inner walls merge into one group and Phase 3
    emits shifted walls with no inter-object travel (the green-lines bug).
    """

    def test_group_outer_two_objects_correct_partition(self, server, modify_stub):
        """GroupOuter=True: two objects, each with outer + 2 inner walls.
        Inter-object MOVERETRACTED carries destination mesh_name.
        Expected: two separate inner wall groups → travel clone in shifted pass."""
        server.settings.apply_outer_walls = True
        server.settings.apply_inner_walls = True
        server.settings.inset_direction = "outside_in"
        paths = [
            _make_path(OUTERWALL, mesh_name="A"),      # obj1 outer
            _make_path(MOVERETRACTED, mesh_name="A"),   # intra-obj travel
            _make_path(OUTERWALL, mesh_name="B"),       # obj2 outer
            _make_path(MOVERETRACTED, mesh_name="B"),   # intra-obj travel
            _make_path(INNERWALL, mesh_name="A"),       # obj1 inner (will shift)
            _make_path(INNERWALL, mesh_name="A"),       # obj1 inner (innermost)
            _make_path(MOVERETRACTED, mesh_name="B"),   # inter-object travel → dest=B
            _make_path(INNERWALL, mesh_name="B"),       # obj2 inner (will shift)
            _make_path(INNERWALL, mesh_name="B"),       # obj2 inner (innermost)
        ]
        resp = modify_stub.Call(_call_request(paths))
        result = list(resp.gcode_paths)

        # Shifted wall paths (exclude elevated MOVE clones)
        shifted_walls = [
            (i, p) for i, p in enumerate(result)
            if p.z_offset > 0 and p.feature in (INNERWALL, OUTERWALL)
        ]
        # Two outer walls shifted + two inner walls shifted = depends on grouping
        # With outside_in: outer group has [O_A, O_B] but they are different meshes
        # → split into two single-wall groups → no shift (len < 2)
        # Inner groups: [I_A, I_A] → 1 shifted, [I_B, I_B] → 1 shifted
        inner_shifted = [(i, p) for i, p in shifted_walls if p.feature == INNERWALL]
        assert len(inner_shifted) == 2, (
            f"Expected 2 shifted inner walls, got {len(inner_shifted)}"
        )

        # A travel path must exist between the two shifted inner walls
        if len(inner_shifted) == 2:
            travel_between = [
                (i, p) for i, p in enumerate(result)
                if p.feature == MOVERETRACTED
                and inner_shifted[0][0] < i < inner_shifted[1][0]
                and p.z_offset > 0
            ]
            assert len(travel_between) >= 1, (
                "No elevated travel between shifted inner walls from different objects"
            )

    def test_empty_mesh_name_move_breaks_group(self, server, modify_stub):
        """A MOVERETRACTED with mesh_name='' inside an active group breaks it.
        Empty mesh_name means outside the per-mesh context (conservative guard)."""
        paths = [
            _make_path(INNERWALL, mesh_name="A"),       # group 1
            _make_path(INNERWALL, mesh_name="A"),       # group 1 (innermost)
            _make_path(MOVERETRACTED, mesh_name=""),    # outside-mesh travel → break
            _make_path(INNERWALL, mesh_name="B"),       # group 2
            _make_path(INNERWALL, mesh_name="B"),       # group 2 (innermost)
        ]
        resp = modify_stub.Call(_call_request(paths))
        result = list(resp.gcode_paths)

        # Two groups of 2 walls each → 1 shifted per group
        shifted = [p for p in result if p.z_offset > 0 and p.feature == INNERWALL]
        assert len(shifted) == 2, f"Expected 2 shifted walls, got {len(shifted)}"

    def test_empty_mesh_name_wall_not_shifted(self, server, modify_stub):
        """A target-wall path with mesh_name='' must not be shifted (passthrough).
        In real CuraEngine output this cannot happen (wall paths always have
        non-empty mesh_name), but it's a defensive guard."""
        paths = [
            _make_path(INNERWALL, mesh_name="A"),       # group member
            _make_path(INNERWALL, mesh_name=""),        # outside-mesh wall → flushes group
            _make_path(INNERWALL, mesh_name="A"),       # new group start
            _make_path(INNERWALL, mesh_name="A"),       # same group (innermost)
        ]
        resp = modify_stub.Call(_call_request(paths))
        result = list(resp.gcode_paths)

        # The empty mesh_name wall must not be shifted
        ghost_walls = [p for p in result if p.mesh_name == "" and p.feature == INNERWALL]
        assert len(ghost_walls) == 1
        assert ghost_walls[0].z_offset == 0, "Wall with empty mesh_name must not be shifted"

    def test_same_mesh_travel_tolerated(self, server, modify_stub):
        """MOVERETRACTED with same mesh_name as current group is tolerated."""
        paths = [
            _make_path(INNERWALL, mesh_name="A"),
            _make_path(MOVERETRACTED, mesh_name="A"),   # same mesh → tolerate
            _make_path(INNERWALL, mesh_name="A"),
            _make_path(INNERWALL, mesh_name="A"),       # innermost
        ]
        resp = modify_stub.Call(_call_request(paths))
        result = list(resp.gcode_paths)

        # All 3 walls form one group → 1 shifted
        shifted = [p for p in result if p.z_offset > 0 and p.feature == INNERWALL]
        assert len(shifted) == 1

    def test_inter_object_move_breaks_group(self, server, modify_stub):
        """MOVERETRACTED with different mesh_name breaks the group."""
        paths = [
            _make_path(INNERWALL, mesh_name="A"),
            _make_path(INNERWALL, mesh_name="A"),       # innermost for group 1
            _make_path(MOVERETRACTED, mesh_name="B"),   # inter-object → break
            _make_path(INNERWALL, mesh_name="B"),
            _make_path(INNERWALL, mesh_name="B"),       # innermost for group 2
        ]
        resp = modify_stub.Call(_call_request(paths))
        result = list(resp.gcode_paths)

        # Two groups → 2 shifted walls
        shifted = [p for p in result if p.z_offset > 0 and p.feature == INNERWALL]
        assert len(shifted) == 2

    def test_wall_mesh_change_without_travel_breaks_group(self, server, modify_stub):
        """Consecutive target walls with different mesh_name must form separate groups."""
        paths = [
            _make_path(INNERWALL, mesh_name="A"),
            _make_path(INNERWALL, mesh_name="A"),       # innermost group 1
            _make_path(INNERWALL, mesh_name="B"),       # different mesh → new group
            _make_path(INNERWALL, mesh_name="B"),       # innermost group 2
        ]
        resp = modify_stub.Call(_call_request(paths))
        result = list(resp.gcode_paths)

        shifted = [p for p in result if p.z_offset > 0 and p.feature == INNERWALL]
        assert len(shifted) == 2


# ===========================================================================
# ALL 8 PERMUTATION TESTS (Wall Ordering × InfillFirst × GroupOuter)
# ===========================================================================

class TestToolpathPermutations:
    """Validate correct path ordering for all permutations of:
    - Wall Ordering: outside_in / inside_out
    - Print Infill before walls: True / False
    - Group outer walls (apply_outer_walls): True / False

    Two objects on the plate, each with 1 outer + 2 inner walls.
    Key assertion: all normal-Z paths print before any shifted-Z path,
    and inter-object travel is preserved as an elevated clone in the
    shifted pass.
    """

    def _assert_z_monotonic(self, result):
        """Per-mesh INNERWALL Z monotonicity: within each mesh's inner-wall
        sequence, all non-shifted inner walls precede all shifted inner walls.

        The permutation tests only target INNERWALL (apply_outer_walls=False),
        so OUTERWALL and non-wall paths are irrelevant to the brick pattern.
        Outer walls legitimately appear at normal Z throughout the output;
        they're not part of the per-mesh brick ordering invariant.

        With per-sub-group inline emission, shifted walls are no longer a
        single global block at the end — mesh B's normal-Z walls
        legitimately appear after mesh A's shifted walls — but within each
        mesh individually, the brick pattern still produces the same
        monotonic inner-wall ordering it always did.
        """
        per_mesh_saw_shifted = {}
        for p in result:
            if p.feature != INNERWALL:
                continue
            mesh = p.mesh_name
            if p.z_offset > 0:
                per_mesh_saw_shifted[mesh] = True
            elif per_mesh_saw_shifted.get(mesh, False):
                return False
        return True

    def _build_outside_in_infill_first_no_group_outer(self):
        """Permutation 1: O→I, Infill first, GroupOuter=False.
        CuraEngine order: INFILL, O₁, I₁_s, I₁_i, T, O₂, I₂_s, I₂_i"""
        return [
            _make_path(INFILL, mesh_name="A"),
            _make_path(OUTERWALL, mesh_name="A"),
            _make_path(INNERWALL, mesh_name="A"),       # will shift
            _make_path(INNERWALL, mesh_name="A"),       # innermost
            _make_path(MOVERETRACTED, mesh_name="B"),   # inter-object travel
            _make_path(OUTERWALL, mesh_name="B"),
            _make_path(INNERWALL, mesh_name="B"),       # will shift
            _make_path(INNERWALL, mesh_name="B"),       # innermost
        ]

    def _build_outside_in_infill_last_no_group_outer(self):
        """Permutation 2: O→I, Infill last, GroupOuter=False.
        CuraEngine order: O₁, I₁_s, I₁_i, T, O₂, I₂_s, I₂_i, INFILL"""
        return [
            _make_path(OUTERWALL, mesh_name="A"),
            _make_path(INNERWALL, mesh_name="A"),
            _make_path(INNERWALL, mesh_name="A"),
            _make_path(MOVERETRACTED, mesh_name="B"),
            _make_path(OUTERWALL, mesh_name="B"),
            _make_path(INNERWALL, mesh_name="B"),
            _make_path(INNERWALL, mesh_name="B"),
            _make_path(INFILL, mesh_name="B"),
        ]

    def _build_inside_out_infill_first_no_group_outer(self):
        """Permutation 3: I→O, Infill first, GroupOuter=False.
        CuraEngine order: INFILL, I₁_i, I₁_s, O₁, T, I₂_i, I₂_s, O₂"""
        return [
            _make_path(INFILL, mesh_name="A"),
            _make_path(INNERWALL, mesh_name="A"),       # innermost (first in I→O)
            _make_path(INNERWALL, mesh_name="A"),       # will shift
            _make_path(OUTERWALL, mesh_name="A"),
            _make_path(MOVERETRACTED, mesh_name="B"),
            _make_path(INNERWALL, mesh_name="B"),       # innermost
            _make_path(INNERWALL, mesh_name="B"),       # will shift
            _make_path(OUTERWALL, mesh_name="B"),
        ]

    def _build_inside_out_infill_last_no_group_outer(self):
        """Permutation 4: I→O, Infill last, GroupOuter=False.
        CuraEngine order: I₁_i, I₁_s, O₁, T, I₂_i, I₂_s, O₂, INFILL"""
        return [
            _make_path(INNERWALL, mesh_name="A"),
            _make_path(INNERWALL, mesh_name="A"),
            _make_path(OUTERWALL, mesh_name="A"),
            _make_path(MOVERETRACTED, mesh_name="B"),
            _make_path(INNERWALL, mesh_name="B"),
            _make_path(INNERWALL, mesh_name="B"),
            _make_path(OUTERWALL, mesh_name="B"),
            _make_path(INFILL, mesh_name="B"),
        ]

    def _build_outside_in_infill_first_group_outer(self):
        """Permutation 5: O→I, Infill first, GroupOuter=True.
        CuraEngine groups outers: INFILL, O₁, T₁, O₂, T₂, I₁_s, I₁_i, T₃, I₂_s, I₂_i"""
        return [
            _make_path(INFILL, mesh_name="A"),
            _make_path(OUTERWALL, mesh_name="A"),
            _make_path(MOVERETRACTED, mesh_name="B"),   # T₁ → dest=B
            _make_path(OUTERWALL, mesh_name="B"),
            _make_path(MOVERETRACTED, mesh_name="A"),   # T₂ → back to A's inners
            _make_path(INNERWALL, mesh_name="A"),       # will shift
            _make_path(INNERWALL, mesh_name="A"),       # innermost
            _make_path(MOVERETRACTED, mesh_name="B"),   # T₃ → inter-object
            _make_path(INNERWALL, mesh_name="B"),       # will shift
            _make_path(INNERWALL, mesh_name="B"),       # innermost
        ]

    def _build_outside_in_infill_last_group_outer(self):
        """Permutation 6: O→I, Infill last, GroupOuter=True.
        CuraEngine: O₁, T₁, O₂, T₂, I₁_s, I₁_i, T₃, I₂_s, I₂_i, INFILL"""
        return [
            _make_path(OUTERWALL, mesh_name="A"),
            _make_path(MOVERETRACTED, mesh_name="B"),
            _make_path(OUTERWALL, mesh_name="B"),
            _make_path(MOVERETRACTED, mesh_name="A"),
            _make_path(INNERWALL, mesh_name="A"),
            _make_path(INNERWALL, mesh_name="A"),
            _make_path(MOVERETRACTED, mesh_name="B"),
            _make_path(INNERWALL, mesh_name="B"),
            _make_path(INNERWALL, mesh_name="B"),
            _make_path(INFILL, mesh_name="B"),
        ]

    def _build_inside_out_infill_first_group_outer(self):
        """Permutation 7: I→O, Infill first, GroupOuter=True.
        CuraEngine: INFILL, O₁, T₁, O₂, T₂, I₁_i, I₁_s, T₃, I₂_i, I₂_s"""
        return [
            _make_path(INFILL, mesh_name="A"),
            _make_path(OUTERWALL, mesh_name="A"),
            _make_path(MOVERETRACTED, mesh_name="B"),
            _make_path(OUTERWALL, mesh_name="B"),
            _make_path(MOVERETRACTED, mesh_name="A"),
            _make_path(INNERWALL, mesh_name="A"),       # innermost (first in I→O)
            _make_path(INNERWALL, mesh_name="A"),       # will shift
            _make_path(MOVERETRACTED, mesh_name="B"),
            _make_path(INNERWALL, mesh_name="B"),       # innermost
            _make_path(INNERWALL, mesh_name="B"),       # will shift
        ]

    def _build_inside_out_infill_last_group_outer(self):
        """Permutation 8: I→O, Infill last, GroupOuter=True.
        CuraEngine: O₁, T₁, O₂, T₂, I₁_i, I₁_s, T₃, I₂_i, I₂_s, INFILL"""
        return [
            _make_path(OUTERWALL, mesh_name="A"),
            _make_path(MOVERETRACTED, mesh_name="B"),
            _make_path(OUTERWALL, mesh_name="B"),
            _make_path(MOVERETRACTED, mesh_name="A"),
            _make_path(INNERWALL, mesh_name="A"),
            _make_path(INNERWALL, mesh_name="A"),
            _make_path(MOVERETRACTED, mesh_name="B"),
            _make_path(INNERWALL, mesh_name="B"),
            _make_path(INNERWALL, mesh_name="B"),
            _make_path(INFILL, mesh_name="B"),
        ]

    # --- GroupOuter=False (permutations 1–4) ---

    def test_perm1_outside_in_infill_first_no_group_outer(self, server, modify_stub):
        """O→I, Infill first, GroupOuter=False."""
        server.settings.apply_inner_walls = True
        server.settings.apply_outer_walls = False
        server.settings.inset_direction = "outside_in"
        paths = self._build_outside_in_infill_first_no_group_outer()
        resp = modify_stub.Call(_call_request(paths))
        result = list(resp.gcode_paths)

        shifted_walls = [p for p in result if p.z_offset > 0 and p.feature == INNERWALL]
        assert len(shifted_walls) == 2, f"Expected 2 shifted inner walls, got {len(shifted_walls)}"
        assert self._assert_z_monotonic(result), "Z not monotonic: normal-Z path after shifted"

        # Verify elevated travel clone between shifted walls
        shifted_positions = [i for i, p in enumerate(result) if p.z_offset > 0 and p.feature == INNERWALL]
        if len(shifted_positions) == 2:
            between = result[shifted_positions[0]+1:shifted_positions[1]]
            elevated_moves = [p for p in between if p.z_offset > 0 and p.feature == MOVERETRACTED]
            assert len(elevated_moves) >= 1, "Missing elevated travel between shifted walls"

    def test_perm2_outside_in_infill_last_no_group_outer(self, server, modify_stub):
        """O→I, Infill last, GroupOuter=False."""
        server.settings.apply_inner_walls = True
        server.settings.apply_outer_walls = False
        server.settings.inset_direction = "outside_in"
        paths = self._build_outside_in_infill_last_no_group_outer()
        resp = modify_stub.Call(_call_request(paths))
        result = list(resp.gcode_paths)

        shifted_walls = [p for p in result if p.z_offset > 0 and p.feature == INNERWALL]
        assert len(shifted_walls) == 2
        assert self._assert_z_monotonic(result)

    def test_perm3_inside_out_infill_first_no_group_outer(self, server, modify_stub):
        """I→O, Infill first, GroupOuter=False."""
        server.settings.apply_inner_walls = True
        server.settings.apply_outer_walls = False
        server.settings.inset_direction = "inside_out"
        paths = self._build_inside_out_infill_first_no_group_outer()
        resp = modify_stub.Call(_call_request(paths))
        result = list(resp.gcode_paths)

        shifted_walls = [p for p in result if p.z_offset > 0 and p.feature == INNERWALL]
        assert len(shifted_walls) == 2
        assert self._assert_z_monotonic(result)

    def test_perm4_inside_out_infill_last_no_group_outer(self, server, modify_stub):
        """I→O, Infill last, GroupOuter=False."""
        server.settings.apply_inner_walls = True
        server.settings.apply_outer_walls = False
        server.settings.inset_direction = "inside_out"
        paths = self._build_inside_out_infill_last_no_group_outer()
        resp = modify_stub.Call(_call_request(paths))
        result = list(resp.gcode_paths)

        shifted_walls = [p for p in result if p.z_offset > 0 and p.feature == INNERWALL]
        assert len(shifted_walls) == 2
        assert self._assert_z_monotonic(result)

    # --- GroupOuter=True (permutations 5–8) ---

    def test_perm5_outside_in_infill_first_group_outer(self, server, modify_stub):
        """O→I, Infill first, GroupOuter=True.
        Expected: INFILL, O₁, T₁, O₂, T₂, I₁_i, T₃, I₂_i, [I₁_s↑, T₃↑, I₂_s↑]"""
        server.settings.apply_inner_walls = True
        server.settings.apply_outer_walls = True
        server.settings.inset_direction = "outside_in"
        paths = self._build_outside_in_infill_first_group_outer()
        resp = modify_stub.Call(_call_request(paths))
        result = list(resp.gcode_paths)

        # Inner walls: 2 shifted (one per object)
        shifted_inner = [p for p in result if p.z_offset > 0 and p.feature == INNERWALL]
        assert len(shifted_inner) == 2, f"Expected 2 shifted inner walls, got {len(shifted_inner)}"
        assert self._assert_z_monotonic(result)

        # Elevated travel between shifted inner walls
        shifted_positions = [i for i, p in enumerate(result) if p.z_offset > 0 and p.feature == INNERWALL]
        if len(shifted_positions) == 2:
            between = result[shifted_positions[0]+1:shifted_positions[1]]
            elevated_moves = [p for p in between if p.z_offset > 0]
            assert len(elevated_moves) >= 1, "Missing elevated travel between shifted inner walls"

    def test_perm6_outside_in_infill_last_group_outer(self, server, modify_stub):
        """O→I, Infill last, GroupOuter=True."""
        server.settings.apply_inner_walls = True
        server.settings.apply_outer_walls = True
        server.settings.inset_direction = "outside_in"
        paths = self._build_outside_in_infill_last_group_outer()
        resp = modify_stub.Call(_call_request(paths))
        result = list(resp.gcode_paths)

        shifted_inner = [p for p in result if p.z_offset > 0 and p.feature == INNERWALL]
        assert len(shifted_inner) == 2
        assert self._assert_z_monotonic(result)

    def test_perm7_inside_out_infill_first_group_outer(self, server, modify_stub):
        """I→O, Infill first, GroupOuter=True."""
        server.settings.apply_inner_walls = True
        server.settings.apply_outer_walls = True
        server.settings.inset_direction = "inside_out"
        paths = self._build_inside_out_infill_first_group_outer()
        resp = modify_stub.Call(_call_request(paths))
        result = list(resp.gcode_paths)

        shifted_inner = [p for p in result if p.z_offset > 0 and p.feature == INNERWALL]
        assert len(shifted_inner) == 2
        assert self._assert_z_monotonic(result)

    def test_perm8_inside_out_infill_last_group_outer(self, server, modify_stub):
        """I→O, Infill last, GroupOuter=True."""
        server.settings.apply_inner_walls = True
        server.settings.apply_outer_walls = True
        server.settings.inset_direction = "inside_out"
        paths = self._build_inside_out_infill_last_group_outer()
        resp = modify_stub.Call(_call_request(paths))
        result = list(resp.gcode_paths)

        shifted_inner = [p for p in result if p.z_offset > 0 and p.feature == INNERWALL]
        assert len(shifted_inner) == 2
        assert self._assert_z_monotonic(result)

    # --- Shared assertions for all permutations ---

    def test_all_perms_infill_never_shifted(self, server, modify_stub):
        """Across all 8 permutations, INFILL paths must never be shifted."""
        builders = [
            self._build_outside_in_infill_first_no_group_outer,
            self._build_outside_in_infill_last_no_group_outer,
            self._build_inside_out_infill_first_no_group_outer,
            self._build_inside_out_infill_last_no_group_outer,
            self._build_outside_in_infill_first_group_outer,
            self._build_outside_in_infill_last_group_outer,
            self._build_inside_out_infill_first_group_outer,
            self._build_inside_out_infill_last_group_outer,
        ]
        for idx, builder in enumerate(builders):
            # GroupOuter=True for permutations 5-8
            is_group_outer = idx >= 4
            server.settings.apply_outer_walls = is_group_outer
            server.settings.apply_inner_walls = True
            server.settings.inset_direction = (
                "inside_out" if idx in (2, 3, 6, 7) else "outside_in"
            )
            paths = builder()
            resp = modify_stub.Call(_call_request(paths))
            result = list(resp.gcode_paths)
            for p in result:
                if p.feature == INFILL:
                    assert p.z_offset == 0, (
                        f"Permutation {idx+1}: INFILL path shifted (z_offset={p.z_offset})"
                    )

    def test_all_perms_outer_walls_never_shifted_when_not_targeted(self, server, modify_stub):
        """With GroupOuter=False, outer walls must never be shifted."""
        builders = [
            self._build_outside_in_infill_first_no_group_outer,
            self._build_outside_in_infill_last_no_group_outer,
            self._build_inside_out_infill_first_no_group_outer,
            self._build_inside_out_infill_last_no_group_outer,
        ]
        for idx, builder in enumerate(builders):
            server.settings.apply_outer_walls = False
            server.settings.apply_inner_walls = True
            server.settings.inset_direction = (
                "inside_out" if idx in (2, 3) else "outside_in"
            )
            paths = builder()
            resp = modify_stub.Call(_call_request(paths))
            result = list(resp.gcode_paths)
            for p in result:
                if p.feature == OUTERWALL:
                    assert p.z_offset == 0, (
                        f"Permutation {idx+1}: OUTERWALL shifted when not targeted"
                    )


# ===========================================================================
# INTER-MODEL TRAVEL SYNTHESIS TESTS (regression: green-line extrusion bug)
# ===========================================================================

def _make_path_at(
    feature,
    *,
    x_offset: int = 0,
    y_offset: int = 0,
    mesh_name: str = "Mesh",
    layer_thickness: int = 200,
    flow_ratio: float = 1.0,
    line_width: int = 400,
) -> gcode_path_pb2.GCodePath:
    """Like _make_path but with configurable XY position for spatial tests."""
    path = gcode_path_pb2.GCodePath(
        feature=feature,
        layer_thickness=layer_thickness,
        flow_ratio=flow_ratio,
        line_width=line_width,
        mesh_name=mesh_name,
    )
    for i in range(3):
        path.path.path.append(
            point3d_pb2.Point3D(
                x=x_offset + i * 1000,
                y=y_offset + i * 1000,
                z=200,
            )
        )
    return path


class TestInterModelTravelSynthesis:
    """Regression tests for the inter-model extrusion bug.

    When Phase 3 reorders paths (normal-Z first, shifted-Z second),
    transitions between walls from different objects must use synthesized
    retracted travel moves — never bare extrusion paths.
    """

    def test_no_extrusion_between_objects_in_shifted_sequence(self, server, modify_stub):
        """Two objects with distinct coordinates: shifted walls from different
        objects must be separated by a MOVERETRACTED travel, not consecutive
        extrusion paths."""
        paths = [
            _make_path_at(OUTERWALL, x_offset=0, y_offset=0, mesh_name="A"),
            _make_path_at(INNERWALL, x_offset=0, y_offset=0, mesh_name="A"),
            _make_path_at(INNERWALL, x_offset=0, y_offset=0, mesh_name="A"),  # innermost
            _make_path_at(MOVERETRACTED, x_offset=50000, y_offset=50000, mesh_name="B"),
            _make_path_at(OUTERWALL, x_offset=50000, y_offset=50000, mesh_name="B"),
            _make_path_at(INNERWALL, x_offset=50000, y_offset=50000, mesh_name="B"),
            _make_path_at(INNERWALL, x_offset=50000, y_offset=50000, mesh_name="B"),  # innermost
        ]
        resp = modify_stub.Call(_call_request(paths))
        result = list(resp.gcode_paths)

        # Verify no consecutive extrusion paths from different objects
        for i in range(len(result) - 1):
            cur = result[i]
            nxt = result[i + 1]
            if (cur.feature in (INNERWALL, OUTERWALL) and
                    nxt.feature in (INNERWALL, OUTERWALL)):
                assert cur.mesh_name == nxt.mesh_name or cur.mesh_name == "" or nxt.mesh_name == "", (
                    f"Consecutive extrusion paths from different objects "
                    f"at indices {i},{i+1}: mesh={cur.mesh_name!r} → {nxt.mesh_name!r}; "
                    f"missing travel move between objects"
                )

    def test_transition_travel_between_normal_and_shifted(self, server, modify_stub):
        """A retracted travel must exist between the last normal-Z path and
        the first shifted-Z path to prevent extrusion across the plate."""
        paths = [
            _make_path_at(OUTERWALL, x_offset=0, mesh_name="A"),
            _make_path_at(INNERWALL, x_offset=0, mesh_name="A"),
            _make_path_at(INNERWALL, x_offset=0, mesh_name="A"),  # innermost
            _make_path_at(INFILL, x_offset=0, mesh_name="A"),
        ]
        resp = modify_stub.Call(_call_request(paths))
        result = list(resp.gcode_paths)

        shifted_walls = [p for p in result if p.z_offset > 0 and p.feature == INNERWALL]
        assert len(shifted_walls) == 1

        # Find the transition point: last z_offset=0 path before first shifted wall
        first_shifted_idx = next(
            i for i, p in enumerate(result)
            if p.z_offset > 0 and p.feature == INNERWALL
        )
        assert first_shifted_idx > 0, "Shifted wall cannot be first in result"
        prev = result[first_shifted_idx - 1]
        assert prev.feature == MOVERETRACTED and prev.z_offset > 0 and prev.retract, (
            f"Expected retracted travel before first shifted wall, got "
            f"feature={prev.feature} z_offset={prev.z_offset} retract={prev.retract}"
        )

    def test_synthesized_travel_connects_correct_positions(self, server, modify_stub):
        """Synthesized travel's endpoints must match the actual wall positions,
        not cloned positions from the original path order."""
        # Object A at (0,0), Object B at (100000,100000) — far apart
        paths = [
            _make_path_at(INNERWALL, x_offset=0, y_offset=0, mesh_name="A"),
            _make_path_at(INNERWALL, x_offset=0, y_offset=0, mesh_name="A"),  # innermost
            _make_path_at(MOVERETRACTED, x_offset=100000, y_offset=100000, mesh_name="B"),
            _make_path_at(INNERWALL, x_offset=100000, y_offset=100000, mesh_name="B"),
            _make_path_at(INNERWALL, x_offset=100000, y_offset=100000, mesh_name="B"),  # innermost
        ]
        resp = modify_stub.Call(_call_request(paths))
        result = list(resp.gcode_paths)

        # Find synthesized travels (MOVERETRACTED with z_offset > 0)
        travels = [
            (i, p) for i, p in enumerate(result)
            if p.feature == MOVERETRACTED and p.z_offset > 0 and p.retract
        ]
        assert len(travels) >= 1, "No synthesized travel found"

        # Each synthesized travel must start near the previous path's end
        # and end near the next path's start
        for ti, (idx, travel) in enumerate(travels):
            if idx == 0 or idx == len(result) - 1:
                continue
            prev_path = result[idx - 1]
            next_path = result[idx + 1]
            if not prev_path.path or not prev_path.path.path:
                continue
            if not next_path.path or not next_path.path.path:
                continue
            prev_end = prev_path.path.path[-1]
            next_start = next_path.path.path[0]
            travel_start = travel.path.path[0]
            travel_end = travel.path.path[-1]
            assert travel_start.x == prev_end.x and travel_start.y == prev_end.y, (
                f"Travel {ti} start ({travel_start.x},{travel_start.y}) != "
                f"prev end ({prev_end.x},{prev_end.y})"
            )
            assert travel_end.x == next_start.x and travel_end.y == next_start.y, (
                f"Travel {ti} end ({travel_end.x},{travel_end.y}) != "
                f"next start ({next_start.x},{next_start.y})"
            )


# ===========================================================================
# TWO-MODEL INTER-MODEL CORRECTNESS TESTS (5 inner walls, gap-fill trigger)
# ===========================================================================

def _make_inter_travel(from_x, from_y, to_x, to_y, mesh_name, z=200):
    """2-point MOVERETRACTED for realistic cross-model travels.
    retract defaults to False (proto default) — distinguishes from synthesized
    gap-fill travels which have retract=True."""
    path = gcode_path_pb2.GCodePath(
        feature=MOVERETRACTED, mesh_name=mesh_name, layer_thickness=200)
    for x, y in [(from_x, from_y), (to_x, to_y)]:
        path.path.path.append(point3d_pb2.Point3D(x=x, y=y, z=z))
    return path


class TestTwoModelIntermodelCorrectness:
    """Correctness tests for two spatially separated models with inner wall
    shifting.  Verifies boundary detection, gap-fill synthesis, shifted-Z
    reordering, and bridge travels all work correctly when paths from two
    distinct meshes are interleaved with a real inter-model travel move.

    Scenario: 2 models, 5 inner walls each.
      Model A: x=[0, 2000] (x_offset=0, _make_path_at generates 3 points
               at (0,0), (1000,1000), (2000,2000))
      Model B: x=[10000, 12000] (x_offset=10000)
      Gap:     x=[2001, 9999]

    With outside_in and 5 walls per group:
      shifted_indices = {counter 0, counter 2} per group = 4 total
      Phase 3b gap-fill triggers between counter 1 and counter 3 (gap of 2)
    """

    MODEL_A_MAX_X = 2000
    MODEL_B_MIN_X = 10000
    GAP_LO = 2001
    GAP_HI = 9999
    Z_SHIFT = 100

    def _build_paths(self):
        """Return the canonical 15-path input sequence."""
        return [
            _make_path_at(OUTERWALL, x_offset=0, mesh_name="A"),           # [0]
            _make_path_at(MOVERETRACTED, x_offset=0, mesh_name="A"),       # [1]
            _make_path_at(OUTERWALL, x_offset=10000, mesh_name="B"),       # [2]
            _make_path_at(MOVERETRACTED, x_offset=10000, mesh_name="B"),   # [3]
            _make_path_at(INNERWALL, x_offset=0, mesh_name="A"),           # [4]  IW_A0 → SHIFTED
            _make_path_at(INNERWALL, x_offset=0, mesh_name="A"),           # [5]  IW_A1 → normal
            _make_path_at(INNERWALL, x_offset=0, mesh_name="A"),           # [6]  IW_A2 → SHIFTED
            _make_path_at(INNERWALL, x_offset=0, mesh_name="A"),           # [7]  IW_A3 → normal
            _make_path_at(INNERWALL, x_offset=0, mesh_name="A"),           # [8]  IW_A4 → innermost
            _make_inter_travel(2000, 2000, 10000, 10000, mesh_name="B"),   # [9]  inter-model
            _make_path_at(INNERWALL, x_offset=10000, mesh_name="B"),       # [10] IW_B0 → SHIFTED
            _make_path_at(INNERWALL, x_offset=10000, mesh_name="B"),       # [11] IW_B1 → normal
            _make_path_at(INNERWALL, x_offset=10000, mesh_name="B"),       # [12] IW_B2 → SHIFTED
            _make_path_at(INNERWALL, x_offset=10000, mesh_name="B"),       # [13] IW_B3 → normal
            _make_path_at(INNERWALL, x_offset=10000, mesh_name="B"),       # [14] IW_B4 → innermost
        ]

    def test_all_original_wall_paths_preserved(self, server, modify_stub):
        """Output must contain all 10 inner walls and 2 outer walls."""
        paths = self._build_paths()
        result = list(modify_stub.Call(_call_request(paths)).gcode_paths)

        inner_walls = [p for p in result if p.feature == INNERWALL]
        outer_walls = [p for p in result if p.feature == OUTERWALL]
        assert len(inner_walls) == 10, f"Expected 10 inner walls, got {len(inner_walls)}"
        assert len(outer_walls) == 2, f"Expected 2 outer walls, got {len(outer_walls)}"
        assert len([p for p in inner_walls if p.mesh_name == "A"]) == 5
        assert len([p for p in inner_walls if p.mesh_name == "B"]) == 5

    def test_exactly_four_walls_shifted(self, server, modify_stub):
        """Exactly 4 inner walls must have z_offset == Z_SHIFT (2 per model)."""
        paths = self._build_paths()
        result = list(modify_stub.Call(_call_request(paths)).gcode_paths)

        shifted = [p for p in result if p.feature == INNERWALL and p.z_offset == self.Z_SHIFT]
        assert len(shifted) == 4, f"Expected 4 shifted walls, got {len(shifted)}"
        assert len([p for p in shifted if p.mesh_name == "A"]) == 2
        assert len([p for p in shifted if p.mesh_name == "B"]) == 2

    def test_normal_z_walls_precede_shifted_z_walls(self, server, modify_stub):
        """Per-mesh: within each mesh's inner-wall sequence, all normal-Z
        walls precede all shifted-Z walls.

        The per-sub-group inline emission interleaves mesh A's shifted run
        with mesh B's normal-Z walls — legitimate under the new structure —
        so the global ordering ``max(normal) < min(shifted)`` no longer
        holds. Within each mesh, though, the brick pattern still produces
        the same monotonic inner-wall order it always did.
        """
        paths = self._build_paths()
        result = list(modify_stub.Call(_call_request(paths)).gcode_paths)

        for mesh in ("A", "B"):
            normal_idx = [i for i, p in enumerate(result)
                          if p.feature == INNERWALL and p.mesh_name == mesh
                          and p.z_offset == 0]
            shifted_idx = [i for i, p in enumerate(result)
                           if p.feature == INNERWALL and p.mesh_name == mesh
                           and p.z_offset > 0]
            assert normal_idx and shifted_idx, f"mesh {mesh} missing normal or shifted walls"
            assert max(normal_idx) < min(shifted_idx), (
                f"Mesh {mesh}: last normal-Z inner wall at position {max(normal_idx)}, "
                f"first shifted at {min(shifted_idx)}"
            )

    def test_gap_fill_travels_synthesized(self, server, modify_stub):
        """At least one gap-fill z=0 retracted travel per mesh: between
        non-adjacent normal-Z inner walls (counter 1 → 3). Bridge-DOWN
        travels from shifted Z back to the anchor are conditional on the
        shifted sub-sequence ending at a different XY than the anchor;
        with these synthetic paths sharing identical XY coords, the
        bridge-DOWNs have zero length and are skipped by the min-travel
        filter — that skip is correct behaviour (sub-pixel travels were
        mis-rendered as inner walls in Cura's preview).
        """
        paths = self._build_paths()
        result = list(modify_stub.Call(_call_request(paths)).gcode_paths)

        synth_z0 = [p for p in result
                    if p.feature == MOVERETRACTED and p.z_offset == 0 and p.retract]
        # At least two: one gap-fill synthesised per mesh.
        assert len(synth_z0) >= 2, (
            f"Expected ≥2 synthesised z=0 retracted travels (one gap-fill per mesh), "
            f"got {len(synth_z0)}"
        )
        assert {p.mesh_name for p in synth_z0} == {"A", "B"}

    def test_gap_fill_travels_within_model_bounds(self, server, modify_stub):
        """Gap fill A: all x <= MODEL_A_MAX_X; gap fill B: all x >= MODEL_B_MIN_X."""
        paths = self._build_paths()
        result = list(modify_stub.Call(_call_request(paths)).gcode_paths)

        gap_fills = [p for p in result
                     if p.feature == MOVERETRACTED and p.z_offset == 0 and p.retract]
        for gf in gap_fills:
            for pt in gf.path.path:
                if gf.mesh_name == "A":
                    assert pt.x <= self.MODEL_A_MAX_X, (
                        f"Gap fill A point x={pt.x} > {self.MODEL_A_MAX_X}")
                elif gf.mesh_name == "B":
                    assert pt.x >= self.MODEL_B_MIN_X, (
                        f"Gap fill B point x={pt.x} < {self.MODEL_B_MIN_X}")

    def test_no_extrusion_in_inter_model_gap(self, server, modify_stub):
        """No INNERWALL/OUTERWALL point has GAP_LO <= x <= GAP_HI."""
        paths = self._build_paths()
        result = list(modify_stub.Call(_call_request(paths)).gcode_paths)

        for p in result:
            if p.feature not in (INNERWALL, OUTERWALL):
                continue
            for pt in p.path.path:
                assert not (self.GAP_LO <= pt.x <= self.GAP_HI), (
                    f"{p.feature} mesh={p.mesh_name!r} point x={pt.x} "
                    f"in gap [{self.GAP_LO},{self.GAP_HI}]"
                )

    def test_all_gap_spanning_paths_are_travel(self, server, modify_stub):
        """Any path with points in both model regions must be MOVERETRACTED."""
        paths = self._build_paths()
        result = list(modify_stub.Call(_call_request(paths)).gcode_paths)

        for p in result:
            xs = [pt.x for pt in p.path.path]
            if not xs:
                continue
            in_a = any(x <= self.MODEL_A_MAX_X for x in xs)
            in_b = any(x >= self.MODEL_B_MIN_X for x in xs)
            if in_a and in_b:
                assert p.feature == MOVERETRACTED, (
                    f"Non-travel path spans gap: feature={p.feature} "
                    f"mesh={p.mesh_name!r} x=[{min(xs)},{max(xs)}]"
                )

    def test_shifted_walls_model_a_before_model_b(self, server, modify_stub):
        """In shifted-Z, all model-A walls precede all model-B walls."""
        paths = self._build_paths()
        result = list(modify_stub.Call(_call_request(paths)).gcode_paths)

        a_pos = [i for i, p in enumerate(result)
                 if p.feature == INNERWALL and p.z_offset > 0 and p.mesh_name == "A"]
        b_pos = [i for i, p in enumerate(result)
                 if p.feature == INNERWALL and p.z_offset > 0 and p.mesh_name == "B"]
        assert a_pos and b_pos
        assert max(a_pos) < min(b_pos), (
            f"A shifted (max idx {max(a_pos)}) not before B shifted (min idx {min(b_pos)})"
        )

    def test_normal_z_wall_ordering_within_models(self, server, modify_stub):
        """Normal-Z: all A inner walls before B; positions monotonic within each."""
        paths = self._build_paths()
        result = list(modify_stub.Call(_call_request(paths)).gcode_paths)

        a_pos = [i for i, p in enumerate(result)
                 if p.feature == INNERWALL and p.z_offset == 0 and p.mesh_name == "A"]
        b_pos = [i for i, p in enumerate(result)
                 if p.feature == INNERWALL and p.z_offset == 0 and p.mesh_name == "B"]
        assert a_pos and b_pos
        assert max(a_pos) < min(b_pos)
        assert a_pos == sorted(a_pos), "A normal-Z order not monotonic"
        assert b_pos == sorted(b_pos), "B normal-Z order not monotonic"

    def test_bridge_travel_connects_normal_z_to_shifted_z(self, server, modify_stub):
        """Path before first shifted wall is MOVERETRACTED(retract=True, z_offset>0)
        whose endpoint matches the first shifted wall's startpoint."""
        paths = self._build_paths()
        result = list(modify_stub.Call(_call_request(paths)).gcode_paths)

        first_shifted_idx = next(
            i for i, p in enumerate(result)
            if p.feature == INNERWALL and p.z_offset > 0
        )
        assert first_shifted_idx > 0

        bridge = result[first_shifted_idx - 1]
        assert bridge.feature == MOVERETRACTED
        assert bridge.retract, "Bridge must have retract=True"
        assert bridge.z_offset > 0, f"Bridge z_offset={bridge.z_offset}, expected >0"

        bridge_end = bridge.path.path[-1]
        shifted_start = result[first_shifted_idx].path.path[0]
        assert bridge_end.x == shifted_start.x and bridge_end.y == shifted_start.y, (
            f"Bridge end ({bridge_end.x},{bridge_end.y}) != "
            f"shifted start ({shifted_start.x},{shifted_start.y})"
        )


# ===========================================================================
# PHASE 1.5 SUB-GROUPING TESTS
# ===========================================================================


def _rect_wall(xmin, xmax, ymin, ymax, *, mesh="Mesh", feature=INNERWALL, z=200):
    """Build a closed rectangular wall GCodePath. Produces a 5-point closed
    loop whose start and end coincide (typical CuraEngine wall representation).
    """
    path = gcode_path_pb2.GCodePath(
        feature=feature, mesh_name=mesh,
        layer_thickness=200, flow_ratio=1.0, line_width=400,
    )
    corners = [
        (xmin, ymin), (xmax, ymin), (xmax, ymax), (xmin, ymax), (xmin, ymin),
    ]
    for (x, y) in corners:
        path.path.path.append(point3d_pb2.Point3D(x=x, y=y, z=z))
    return path


class TestContourSubGrouping:
    """Phase 1.5 splits each mesh-level group into spatially-contiguous
    sub-groups by XY distance between consecutive wall endpoints. Without
    this, walls-only geometry with no infill/skin boundary collapses all
    contours of a mesh into one group — and the alternating brick counter
    then spans multiple contours, shifting some contours' local innermost
    walls incorrectly.
    """

    def test_two_distant_contours_split_into_two_sub_groups(self, server, modify_stub):
        """Two square contours 5 mm apart, same mesh, no infill between —
        each contour's local innermost wall must be protected."""
        server.settings.contour_break_distance = 2000  # 2 mm
        # Contour A: walls at x=[0..2000], 3 inner walls nested inward
        a_walls = [
            _rect_wall(0, 2000, 0, 2000),      # IW1_A  (counter=0, SHIFTED)
            _rect_wall(400, 1600, 400, 1600),  # IW2_A  (counter=1, keep)
            _rect_wall(800, 1200, 800, 1200),  # IW3_A  (innermost of A, protected)
        ]
        # Contour B: 7 mm to the right; same three inner walls nested inward
        b_walls = [
            _rect_wall(7000, 9000, 0, 2000),      # IW1_B  (counter=0 in sub-group, SHIFTED)
            _rect_wall(7400, 8600, 400, 1600),    # IW2_B  (keep)
            _rect_wall(7800, 8200, 800, 1200),    # IW3_B  (innermost of B, protected)
        ]
        paths = a_walls + b_walls
        resp = modify_stub.Call(_call_request(paths))
        result = list(resp.gcode_paths)

        inner_shifted = [p for p in result
                         if p.feature == INNERWALL and p.z_offset > 0]
        # One shifted wall per contour — NOT two shifted in A with B's innermost
        # also shifted (which is what the old single-group algorithm would do).
        assert len(inner_shifted) == 2, (
            f"Expected 2 shifted inner walls (one per contour), got {len(inner_shifted)}"
        )

        # The innermost walls (smallest rectangles) must NOT be shifted.
        innermost_a = next(p for p in result
                           if p.feature == INNERWALL and p.path.path
                           and p.path.path[0].x == 800)
        innermost_b = next(p for p in result
                           if p.feature == INNERWALL and p.path.path
                           and p.path.path[0].x == 7800)
        assert innermost_a.z_offset == 0, "Contour A innermost wall was shifted"
        assert innermost_b.z_offset == 0, "Contour B innermost wall was shifted"

    def test_close_contours_stay_in_one_sub_group(self, server, modify_stub):
        """Consecutive walls closer than break_distance stay in the same
        sub-group — the 3-inner-wall alternating pattern applies globally."""
        server.settings.contour_break_distance = 2000  # 2 mm
        # Three nested walls, each 0.4 mm inset (400 µm < 2000 µm threshold)
        paths = [
            _rect_wall(0, 2000, 0, 2000),      # IW1 (counter=0, SHIFTED)
            _rect_wall(400, 1600, 400, 1600),  # IW2 (counter=1, keep)
            _rect_wall(800, 1200, 800, 1200),  # IW3 (innermost, protected)
        ]
        resp = modify_stub.Call(_call_request(paths))
        result = list(resp.gcode_paths)

        inner_shifted = [p for p in result
                         if p.feature == INNERWALL and p.z_offset > 0]
        assert len(inner_shifted) == 1
        # Shifted one must be the outermost (IW1, starts at x=0)
        assert inner_shifted[0].path.path[0].x == 0

    def test_break_distance_zero_still_clusters_by_bbox(self, server, modify_stub):
        """contour_break_distance=0 disables the DISTANCE-fallback, but the
        primary bbox-overlap union-find clustering always runs. Two
        spatially-disjoint contours are recognised as separate clusters
        regardless of the contour_break_distance setting.
        """
        server.settings.contour_break_distance = 0
        a_walls = [
            _rect_wall(0, 2000, 0, 2000),
            _rect_wall(400, 1600, 400, 1600),
            _rect_wall(800, 1200, 800, 1200),  # A innermost
        ]
        b_walls = [
            _rect_wall(7000, 9000, 0, 2000),
            _rect_wall(7400, 8600, 400, 1600),
            _rect_wall(7800, 8200, 800, 1200),  # B innermost
        ]
        paths = a_walls + b_walls
        resp = modify_stub.Call(_call_request(paths))
        result = list(resp.gcode_paths)

        inner_shifted = [p for p in result
                         if p.feature == INNERWALL and p.z_offset > 0]
        # Two disjoint contours → two clusters of 3 walls each. Per cluster:
        # innermost protected, 2 remaining → counter 0 shifts, counter 1
        # keeps. That's 1 shift per contour × 2 contours = 2 shifts total.
        assert len(inner_shifted) == 2, (
            f"bbox-clustering should split disjoint contours: expected 2 "
            f"shifted walls (one per contour), got {len(inner_shifted)}"
        )

    def test_per_sub_group_shifted_emission_is_contiguous(self, server, modify_stub):
        """Each sub-group's shifted walls are emitted inline as a contiguous
        block right after that sub-group's anchor, bracketed by bridge
        travels — not concatenated into one global shifted run at the end
        of the output.
        """
        server.settings.contour_break_distance = 2000
        # 2 contours × 4 inner walls each; 2 shifted per contour
        def contour(x0):
            return [
                _rect_wall(x0,        x0 + 2000, 0,    2000),    # counter 0 SHIFT
                _rect_wall(x0 + 400,  x0 + 1600, 400,  1600),    # counter 1
                _rect_wall(x0 + 800,  x0 + 1200, 800,  1200),    # counter 2 SHIFT
                _rect_wall(x0 + 900,  x0 + 1100, 900,  1100),    # innermost, keep
            ]
        paths = contour(0) + contour(7000)
        resp = modify_stub.Call(_call_request(paths))
        result = list(resp.gcode_paths)

        inner_shifted_indices = [
            i for i, p in enumerate(result)
            if p.feature == INNERWALL and p.z_offset > 0
        ]
        # 4 shifted walls (2 per contour)
        assert len(inner_shifted_indices) == 4

        # A's shifted walls (x starts with 0) are all contiguous;
        # B's shifted walls (x starts with 7000) are all contiguous;
        # A's block precedes B's block.
        a_shifted = [i for i in inner_shifted_indices
                     if result[i].path.path[0].x < 3500]
        b_shifted = [i for i in inner_shifted_indices
                     if result[i].path.path[0].x >= 3500]
        assert len(a_shifted) == 2 and len(b_shifted) == 2
        # Each block's indices are consecutive in the result (allowing for
        # a single intra-block synth travel between the two shifted walls).
        assert max(a_shifted) - min(a_shifted) <= 2, (
            f"A's shifted walls are not contiguous: {a_shifted}"
        )
        assert max(b_shifted) - min(b_shifted) <= 2, (
            f"B's shifted walls are not contiguous: {b_shifted}"
        )
        # A's block is entirely before B's block
        assert max(a_shifted) < min(b_shifted)

    def test_bridge_travels_bracket_each_shifted_run(self, server, modify_stub):
        """Each sub-group's shifted run is bracketed by a bridge_UP travel
        (z_offset>0, retract=True) immediately before the first shifted wall
        and a bridge_DOWN travel (z_offset=0, retract=True) immediately
        after the last shifted wall. Ensures the nozzle returns to layer Z
        at a known XY position before the next normal-Z path executes.
        """
        server.settings.contour_break_distance = 2000
        def contour(x0):
            return [
                _rect_wall(x0,       x0 + 2000, 0,   2000),
                _rect_wall(x0 + 400, x0 + 1600, 400, 1600),
                _rect_wall(x0 + 800, x0 + 1200, 800, 1200),  # innermost
            ]
        paths = contour(0) + contour(7000)
        resp = modify_stub.Call(_call_request(paths))
        result = list(resp.gcode_paths)

        shifted_wall_indices = [
            i for i, p in enumerate(result)
            if p.feature == INNERWALL and p.z_offset > 0
        ]
        assert len(shifted_wall_indices) == 2

        for sw_idx in shifted_wall_indices:
            before = result[sw_idx - 1]
            assert before.feature == MOVERETRACTED and before.retract, (
                f"Path before shifted wall at {sw_idx} should be retracted travel, "
                f"got feature={before.feature}, retract={before.retract}"
            )
            assert before.z_offset > 0, (
                f"Bridge_UP before shifted wall at {sw_idx} should have z_offset>0, "
                f"got {before.z_offset}"
            )

        # After each last shifted wall of a sub-group there's a bridge_DOWN
        # (z_offset=0). For this test with only 1 shifted wall per sub-group,
        # the immediate follower is the bridge_DOWN.
        for sw_idx in shifted_wall_indices:
            after = result[sw_idx + 1]
            assert after.feature == MOVERETRACTED and after.retract, (
                f"Path after shifted wall at {sw_idx} should be retracted travel"
            )
            assert after.z_offset == 0, (
                f"Bridge_DOWN after shifted wall at {sw_idx} should have z_offset=0, "
                f"got {after.z_offset}"
            )

    def test_synth_travels_have_zero_extrusion_fields(self, server, modify_stub):
        """All synthesised travels (bridges + Case-A gap-fills) must have
        flow=0, flow_ratio=0, line_width=0, width_factor=0 explicitly set —
        no ambiguity that CuraEngine or the Cura preview could interpret
        as extrusion. This is the defence against the bright-green
        WALL-INNER diagonals the user reported.
        """
        server.settings.contour_break_distance = 2000
        paths = [
            _rect_wall(0, 2000, 0, 2000),
            _rect_wall(400, 1600, 400, 1600),
            _rect_wall(800, 1200, 800, 1200),
        ]
        resp = modify_stub.Call(_call_request(paths))
        result = list(resp.gcode_paths)

        synth_travels = [p for p in result
                         if p.feature == MOVERETRACTED and p.retract]
        assert synth_travels, "Expected at least one synthesised travel"
        for t in synth_travels:
            assert t.flow == 0.0, f"synth travel has flow={t.flow}, expected 0"
            assert t.flow_ratio == 0.0, (
                f"synth travel has flow_ratio={t.flow_ratio}, expected 0 — "
                f"non-zero flow_ratio risks being treated as extrusion"
            )
            assert t.line_width == 0, (
                f"synth travel has line_width={t.line_width}, expected 0"
            )
            assert t.width_factor == 0.0, (
                f"synth travel has width_factor={t.width_factor}, expected 0"
            )


class TestContourBreakDistanceBroadcast:
    """The new bricklayers_contour_break_distance setting must be parsed
    from Cura's settings broadcast as millimetres and stored as microns.
    """

    def test_contour_break_distance_parsed_mm_to_microns(self, server, broadcast_stub):
        req = _settings_request({
            "brick_layers_enabled": "True",
            "brick_layers_contour_break_distance": "3.5",  # mm
        })
        broadcast_stub.BroadcastSettings(req)
        assert server.settings.contour_break_distance == 3500

    def test_contour_break_distance_accepts_zero(self, server, broadcast_stub):
        req = _settings_request({
            "brick_layers_enabled": "True",
            "brick_layers_contour_break_distance": "0",
        })
        broadcast_stub.BroadcastSettings(req)
        assert server.settings.contour_break_distance == 0

    def test_contour_break_distance_default_when_absent(self, server, broadcast_stub):
        # Reset to a known non-default value, then broadcast WITHOUT the
        # setting key — the prototype BroadcastServicer only overwrites
        # fields whose key is present, so the pre-broadcast value persists.
        server.settings.contour_break_distance = 12345
        req = _settings_request({"brick_layers_enabled": "True"})
        broadcast_stub.BroadcastSettings(req)
        assert server.settings.contour_break_distance == 12345

    def test_contour_break_distance_rejects_invalid(self, server, broadcast_stub):
        """Malformed values are silently ignored; setting keeps its prior value."""
        server.settings.contour_break_distance = 4321
        req = _settings_request({
            "brick_layers_enabled": "True",
            "brick_layers_contour_break_distance": "not-a-number",
        })
        broadcast_stub.BroadcastSettings(req)
        assert server.settings.contour_break_distance == 4321


class TestGeometryBasedInnermostDetection:
    """The innermost wall of each sub-group is identified geometrically
    (smallest XY bounding box) rather than by path-list position.

    This is the fix for the "missing walls" symptom on Ultimaker S5 and
    other printers whose fdmprinter default ``inset_direction`` is
    ``inside_out`` — or any printer where ``material_alternate_walls=True``
    reverses the wall order on alternating layers. Using path-list
    position alone, the algorithm would shift the *actual* innermost
    wall (adjacent to skin/infill) to a half-layer Z, where Cura's
    preview doesn't render it at the current layer.
    """

    def test_smallest_bbox_wall_protected_regardless_of_list_order(self, server, modify_stub):
        """Three nested rectangles, but in reverse print order (innermost
        first, outermost last). With the OLD positional rule + the default
        fixture setting of ``inset_direction = outside_in``, the algorithm
        would protect the LAST wall (outermost) and shift the FIRST wall
        (actual innermost). The geometry-based rule correctly identifies
        the smallest bbox as the innermost and protects it."""
        # Outside-in means "innermost = last"; we ARE using outside_in in
        # the fixture, but the paths arrive reversed (smallest first), so
        # the positional rule would mis-identify.
        server.settings.inset_direction = "outside_in"
        server.settings.contour_break_distance = 2000
        paths = [
            _rect_wall(800, 1200, 800, 1200),   # idx 0: smallest (TRUE innermost)
            _rect_wall(400, 1600, 400, 1600),   # idx 1: middle
            _rect_wall(  0, 2000,   0, 2000),   # idx 2: largest (outer inner)
        ]
        resp = modify_stub.Call(_call_request(paths))
        result = list(resp.gcode_paths)

        # The smallest rectangle (x starts at 800) must NOT be shifted.
        innermost_wall = next(
            p for p in result if p.feature == INNERWALL
            and p.path.path and p.path.path[0].x == 800
        )
        assert innermost_wall.z_offset == 0, (
            "The geometrically-innermost wall (smallest bbox) must be "
            "protected regardless of its position in the input path list."
        )
        # Exactly one wall shifted. With 3 walls [innermost, middle, outer]
        # in that group iteration order, the first non-innermost wall
        # (counter=0) is SHIFTED. That's the MIDDLE rectangle (x=400),
        # adjacent to the innermost — the pattern most likely to form a
        # strong interlock with the next layer's shifted walls.
        inner_shifted = [p for p in result
                         if p.feature == INNERWALL and p.z_offset > 0]
        assert len(inner_shifted) == 1
        assert inner_shifted[0].path.path[0].x == 400

    def test_inside_out_direction_also_uses_geometry(self, server, modify_stub):
        """Same nested rectangles but with inside_out configured — the
        geometry-based rule still identifies the smallest-bbox wall as
        innermost; the inset_direction setting only matters as a tiebreaker
        for walls with identical bboxes."""
        server.settings.inset_direction = "inside_out"
        server.settings.contour_break_distance = 2000
        # Here the path list happens to be innermost-first (matches inside_out):
        paths = [
            _rect_wall(800, 1200, 800, 1200),   # innermost
            _rect_wall(400, 1600, 400, 1600),
            _rect_wall(  0, 2000,   0, 2000),
        ]
        resp = modify_stub.Call(_call_request(paths))
        result = list(resp.gcode_paths)
        innermost_wall = next(
            p for p in result if p.feature == INNERWALL
            and p.path.path and p.path.path[0].x == 800
        )
        assert innermost_wall.z_offset == 0

    def test_all_tied_bboxes_fall_back_to_inset_direction(self, server, modify_stub):
        """When all walls have identical bboxes (generic _make_path coords),
        the geometry-based rule delegates to inset_direction for the choice.
        Confirms backwards compatibility with the existing test suite."""
        server.settings.inset_direction = "outside_in"
        server.settings.contour_break_distance = 1_000_000  # disabled
        paths = [_make_path(INNERWALL) for _ in range(3)]
        resp = modify_stub.Call(_call_request(paths))
        result = list(resp.gcode_paths)
        # For outside_in, the LAST wall is the innermost (positional fallback).
        # With 3 walls and alternating counter (0 SHIFT, 1 keep, innermost keep),
        # exactly one should be shifted.
        shifted = _shifted_walls(result)
        assert len(shifted) == 1

    def test_material_alternate_walls_simulated_by_reversed_input(self, server, modify_stub):
        """``material_alternate_walls = True`` on Cura's side effectively
        reverses the wall order on alternating layers *without* updating
        the broadcast ``inset_direction``. Simulate that: configure
        ``outside_in`` but feed the walls in reversed (innermost-first)
        order. The geometry-based rule still picks the correct innermost.
        """
        server.settings.inset_direction = "outside_in"  # what plugin was told
        server.settings.contour_break_distance = 2000
        # ...but Cura actually reversed the order for this layer:
        paths = [
            _rect_wall(800, 1200, 800, 1200),   # innermost first
            _rect_wall(  0, 2000,   0, 2000),   # outermost inner last
        ]
        resp = modify_stub.Call(_call_request(paths))
        result = list(resp.gcode_paths)
        innermost = next(p for p in result
                         if p.feature == INNERWALL
                         and p.path.path and p.path.path[0].x == 800)
        outermost = next(p for p in result
                         if p.feature == INNERWALL
                         and p.path.path and p.path.path[0].x == 0)
        assert innermost.z_offset == 0, "Actual innermost must not be shifted"
        assert outermost.z_offset > 0, "Outermost inner wall must be shifted"


# ===========================================================================
# RETRACTED-TRAVEL CONTOUR BOUNDARY TESTS
# ===========================================================================
#
# CuraEngine emits a retracted travel between different-contour wall loops
# when the travel distance exceeds ``retraction_min_travel`` (commonly 1–10 mm).
# Intra-contour combing travels between nested wall insets are shorter and
# non-retracted.
#
# Phase 1.5 uses ``retract == True`` as the authoritative contour-boundary
# signal. This is the primary safety net when the ``contour_break_distance``
# threshold is mis-configured (e.g. set higher than actual inter-contour
# spacing in a complex part — the exact failure mode BR MOTOR HOLDER
# layer 148 exhibited).


def _retract_travel(from_pt, to_pt, mesh="Mesh"):
    """Build a retracted MOVERETRACTED travel path between two points.

    Models the CuraEngine-inserted retraction at contour boundaries.
    """
    path = gcode_path_pb2.GCodePath(
        feature=MOVERETRACTED,
        retract=True,
        mesh_name=mesh,
        layer_thickness=200,
        flow_ratio=1.0,
        line_width=0,
    )
    path.path.path.append(point3d_pb2.Point3D(x=from_pt[0], y=from_pt[1], z=200))
    path.path.path.append(point3d_pb2.Point3D(x=to_pt[0], y=to_pt[1], z=200))
    return path


def _combing_travel(from_pt, to_pt, mesh="Mesh"):
    """Build a NON-retracted same-mesh travel (intra-contour combing)."""
    path = gcode_path_pb2.GCodePath(
        feature=MOVEUNRETRACTED,
        retract=False,
        mesh_name=mesh,
        layer_thickness=200,
        flow_ratio=1.0,
        line_width=0,
    )
    path.path.path.append(point3d_pb2.Point3D(x=from_pt[0], y=from_pt[1], z=200))
    path.path.path.append(point3d_pb2.Point3D(x=to_pt[0], y=to_pt[1], z=200))
    return path


class TestRetractedTravelContourBoundary:
    """Phase 1.5 treats a retracted same-mesh travel between two target walls
    as an authoritative contour-boundary signal, independent of the
    distance threshold. This is the primary fix for the BR MOTOR HOLDER
    layer 148 regression where contours < user's ``contour_break_distance``
    apart silently merged into one sub-group, causing multiple contours'
    innermost walls to be shifted instead of protected.
    """

    def test_close_contours_with_retract_are_split(self, server, modify_stub):
        """Two contours 0.6 mm apart (well inside the default 2 mm threshold)
        are still split into separate sub-groups when a retracted travel
        sits between them. Each contour's innermost wall must be protected.
        """
        server.settings.contour_break_distance = 2000  # 2 mm default
        a_walls = [
            _rect_wall(0,   2000, 0,   2000),   # outermost inner of A
            _rect_wall(400, 1600, 400, 1600),   # mid inner of A
            _rect_wall(800, 1200, 800, 1200),   # innermost of A (protected)
        ]
        b_walls = [
            _rect_wall(2600, 4600, 0,   2000),   # outermost inner of B
            _rect_wall(3000, 4200, 400, 1600),   # mid inner of B
            _rect_wall(3400, 3800, 800, 1200),   # innermost of B (protected)
        ]
        # Retracted travel from A's innermost end (800, 2000 — closed loop
        # back to start) to B's outermost start (2600, 0). XY distance
        # is sqrt(1800² + 2000²) ≈ 2690 µm — above the 2 mm threshold,
        # but we want to verify retract alone is enough to split.
        retract = _retract_travel(
            from_pt=(800, 2000),   # A innermost end
            to_pt=(2600, 0),        # B outermost start
        )
        paths = a_walls + [retract] + b_walls
        resp = modify_stub.Call(_call_request(paths))
        result = list(resp.gcode_paths)

        shifted = [p for p in result
                   if p.feature == INNERWALL and p.z_offset > 0]
        # With two sub-groups of 3 walls each: each sub-group shifts its
        # outermost (counter=0) and protects its innermost. So exactly
        # 2 shifted walls total.
        assert len(shifted) == 2, (
            f"Expected 2 shifted walls (one per contour), got {len(shifted)}"
        )
        innermost_a = next(p for p in result
                           if p.feature == INNERWALL and p.path.path
                           and p.path.path[0].x == 800)
        innermost_b = next(p for p in result
                           if p.feature == INNERWALL and p.path.path
                           and p.path.path[0].x == 3400)
        assert innermost_a.z_offset == 0, "Contour A innermost must not be shifted"
        assert innermost_b.z_offset == 0, "Contour B innermost must not be shifted"

    def test_retract_splits_even_when_distance_below_threshold(self, server, modify_stub):
        """High ``contour_break_distance`` (10 mm — user's BR MOTOR HOLDER
        profile) would normally merge nearby contours. A retracted travel
        between them still splits them correctly.
        """
        server.settings.contour_break_distance = 10_000  # 10 mm — user's real setting
        a_walls = [
            _rect_wall(0,   2000, 0,   2000),
            _rect_wall(400, 1600, 400, 1600),
            _rect_wall(800, 1200, 800, 1200),   # innermost of A
        ]
        # Contour B sits just 2 mm from A — well within 10 mm threshold.
        b_walls = [
            _rect_wall(4000, 6000, 0,   2000),
            _rect_wall(4400, 5600, 400, 1600),
            _rect_wall(4800, 5200, 800, 1200),   # innermost of B
        ]
        retract = _retract_travel(
            from_pt=(800, 2000),
            to_pt=(4000, 0),
        )
        paths = a_walls + [retract] + b_walls
        resp = modify_stub.Call(_call_request(paths))
        result = list(resp.gcode_paths)

        shifted = [p for p in result
                   if p.feature == INNERWALL and p.z_offset > 0]
        assert len(shifted) == 2, (
            f"Retract should override distance threshold and split, "
            f"got {len(shifted)} shifted (expected 2 — one per contour)"
        )
        # Both innermost walls protected
        innermost_a = next(p for p in result
                           if p.feature == INNERWALL and p.path.path
                           and p.path.path[0].x == 800)
        innermost_b = next(p for p in result
                           if p.feature == INNERWALL and p.path.path
                           and p.path.path[0].x == 4800)
        assert innermost_a.z_offset == 0
        assert innermost_b.z_offset == 0

    def test_non_retracted_travel_does_not_force_split(self, server, modify_stub):
        """A non-retracted same-mesh travel (combing between intra-contour
        wall loops) does NOT trigger a sub-group break. The distance-based
        fallback still applies normally.
        """
        server.settings.contour_break_distance = 2000  # 2 mm
        # Three nested walls — 0.4 mm spacing, well within threshold.
        paths_walls = [
            _rect_wall(0,   2000, 0,   2000),   # IW1
            _rect_wall(400, 1600, 400, 1600),   # IW2
            _rect_wall(800, 1200, 800, 1200),   # innermost
        ]
        # Short combing travel between IW1 and IW2 (0.4 mm — typical intra-
        # contour combing). NOT retracted — CuraEngine doesn't retract for
        # such short travels.
        combing = _combing_travel(from_pt=(2000, 0), to_pt=(400, 400))
        paths = [paths_walls[0], combing, paths_walls[1], paths_walls[2]]
        resp = modify_stub.Call(_call_request(paths))
        result = list(resp.gcode_paths)

        shifted = [p for p in result
                   if p.feature == INNERWALL and p.z_offset > 0]
        # Single sub-group of 3 walls: IW1 shifted (counter=0), IW2 not
        # (counter=1), innermost protected. Exactly 1 shifted.
        assert len(shifted) == 1, (
            f"Combing travel must not split; expected 1 shifted (outermost), "
            f"got {len(shifted)}"
        )
        # IW1 (outermost, starts at x=0) is the shifted one.
        assert shifted[0].path.path[0].x == 0

    def test_br_motor_holder_scenario_10_contours_with_retracts(self, server, modify_stub):
        """BR MOTOR HOLDER at layer 148 failing-case reproducer.

        10 small rectangular contours within a single mesh, spaced 3–5 mm
        apart (all well inside the user's 10 mm ``contour_break_distance``),
        each contour separated from the next by a retracted travel. Each
        contour has 3 inner walls; the innermost of each must be protected.

        Before the retract-signal fix: all 10 contours collapsed into one
        sub-group, only one wall globally was detected as innermost, the
        other 9 contours' innermost walls were shifted to half-layer Z,
        and Cura's layer preview showed gaps where those shifted walls
        would be rendered at layer N+0.5 rather than layer N.

        After the fix: 10 sub-groups, each with its own protected innermost
        and locally-alternating brick counter.
        """
        server.settings.contour_break_distance = 10_000  # 10 mm — user's profile value
        server.settings.inset_direction = "outside_in"

        contour_specs = [
            (0,     2000),    # contour 0 at x=[0..2000]
            (3000,  5000),    # contour 1 at x=[3000..5000] — 1 mm away from contour 0's edge
            (6000,  8000),    # ...
            (9000,  11000),
            (12000, 14000),
            (15000, 17000),
            (18000, 20000),
            (21000, 23000),
            (24000, 26000),
            (27000, 29000),   # contour 9
        ]
        paths = []
        prev_end = None
        for (xmin, xmax) in contour_specs:
            # Three nested inner walls per contour
            outer = _rect_wall(xmin,         xmax,         0,    2000)
            mid   = _rect_wall(xmin + 400,   xmax - 400,   400,  1600)
            inner = _rect_wall(xmin + 800,   xmax - 800,   800,  1200)
            # Retracted travel from previous contour's innermost end to
            # this contour's outermost start — CuraEngine does this.
            if prev_end is not None:
                paths.append(_retract_travel(
                    from_pt=prev_end,
                    to_pt=(xmin, 0),
                ))
            paths.extend([outer, mid, inner])
            prev_end = (inner.path.path[-1].x, inner.path.path[-1].y)

        resp = modify_stub.Call(_call_request(paths))
        result = list(resp.gcode_paths)

        shifted = [p for p in result
                   if p.feature == INNERWALL and p.z_offset > 0]
        # 10 contours × 1 shifted per contour (3 walls each: outermost=shift,
        # mid=keep, innermost=protected) = 10 shifted total.
        assert len(shifted) == 10, (
            f"Expected 10 shifted walls (one per contour), got {len(shifted)}. "
            f"If < 10, sub-grouping failed and some contours' innermost were "
            f"wrongly shifted."
        )

        # Every contour's innermost wall (the smallest one, x=xmin+800) must be protected.
        for (xmin, _) in contour_specs:
            innermost = next(
                p for p in result
                if p.feature == INNERWALL and p.path.path
                and p.path.path[0].x == xmin + 800
            )
            assert innermost.z_offset == 0, (
                f"Contour starting at x={xmin}: innermost wall was shifted "
                f"(expected protected)"
            )

    def test_retract_break_with_contour_break_distance_disabled(self, server, modify_stub):
        """When ``contour_break_distance == 0`` (sub-grouping-by-distance
        disabled), retracted travels still split — retract is the primary
        signal, distance is the fallback.
        """
        server.settings.contour_break_distance = 0  # distance-based disabled
        a_walls = [
            _rect_wall(0,   2000, 0,   2000),
            _rect_wall(400, 1600, 400, 1600),
            _rect_wall(800, 1200, 800, 1200),   # innermost A
        ]
        b_walls = [
            _rect_wall(3000, 5000, 0,   2000),
            _rect_wall(3400, 4600, 400, 1600),
            _rect_wall(3800, 4200, 800, 1200),   # innermost B
        ]
        retract = _retract_travel(from_pt=(800, 2000), to_pt=(3000, 0))
        paths = a_walls + [retract] + b_walls
        resp = modify_stub.Call(_call_request(paths))
        result = list(resp.gcode_paths)

        shifted = [p for p in result
                   if p.feature == INNERWALL and p.z_offset > 0]
        # Two sub-groups of 3 walls → 2 shifted total.
        assert len(shifted) == 2, (
            f"Retract-based split should work even with distance disabled; "
            f"got {len(shifted)} shifted"
        )


# ===========================================================================
# LINE-WIDTH SAFETY CAP TESTS
# ===========================================================================
#
# When a user configures ``contour_break_distance`` significantly higher
# than the realistic inter-contour spacing (e.g. 10 mm for a part whose
# ribs are 3–5 mm apart), the distance-only split silently merges
# distinct contours into one sub-group. The plugin applies a ``3 × line_width``
# safety cap to the distance threshold so a mis-configured user setting
# can't defeat per-contour innermost-wall protection.


def _rect_wall_lw(xmin, xmax, ymin, ymax, *, line_width, mesh="Mesh",
                  feature=INNERWALL, z=200):
    """Like ``_rect_wall`` but with an explicit line_width (µm).

    The safety cap in Phase 1.5 uses ``line_width × 3`` as the effective
    sub-group split threshold when the user's distance is larger, so tests
    exercising the cap need a realistic line_width value.
    """
    path = gcode_path_pb2.GCodePath(
        feature=feature, mesh_name=mesh,
        layer_thickness=200, flow_ratio=1.0, line_width=line_width,
    )
    for (x, y) in [(xmin, ymin), (xmax, ymin), (xmax, ymax),
                   (xmin, ymax), (xmin, ymin)]:
        path.path.path.append(point3d_pb2.Point3D(x=x, y=y, z=z))
    return path


class TestLineWidthSafetyCap:
    """Phase 1.5 caps the distance threshold at ``3 × line_width`` so a
    user-configured ``contour_break_distance`` far above the inter-contour
    spacing can't merge distinct contours into a single sub-group.

    This is the direct failure mode the BR MOTOR HOLDER regression exposed:
    user had ``contour_break_distance = 10 mm`` on a part with ribs 3–5 mm
    apart, and the plugin's distance-only check merged everything into one
    sub-group, leaving 9 of 10 contours with their innermost wall wrongly
    shifted to half-layer Z.
    """

    def test_safety_cap_splits_close_contours_despite_high_user_distance(
        self, server, modify_stub
    ):
        """User's 10 mm threshold would normally merge contours 5 mm apart
        AND without a retracted travel between them. The line_width safety
        cap (3 × 550 µm = 1.65 mm) kicks in and splits them."""
        server.settings.contour_break_distance = 10_000  # 10 mm — user's real value
        a_walls = [
            _rect_wall_lw(0,     2000, 0,    2000, line_width=550),
            _rect_wall_lw(500,   1500, 500,  1500, line_width=550),   # innermost A
        ]
        # Contour B: 5 mm from A, NO retracted travel between them
        b_walls = [
            _rect_wall_lw(5000,  7000, 0,    2000, line_width=550),
            _rect_wall_lw(5500,  6500, 500,  1500, line_width=550),   # innermost B
        ]
        paths = a_walls + b_walls
        resp = modify_stub.Call(_call_request(paths))
        result = list(resp.gcode_paths)

        shifted = [p for p in result
                   if p.feature == INNERWALL and p.z_offset > 0]
        # Two sub-groups of 2 walls each: 1 shifted per contour = 2 total.
        # Without the safety cap, the 10 mm threshold would merge both
        # contours into one sub-group of 4 walls, shifting 1 wall and
        # protecting only the globally-smallest innermost — leaving the
        # other contour's innermost wall unprotected.
        assert len(shifted) == 2, (
            f"Safety cap should override mis-configured 10 mm distance; "
            f"got {len(shifted)} shifted (expected 2, one per contour)"
        )
        innermost_a = next(p for p in result
                           if p.feature == INNERWALL and p.path.path
                           and p.path.path[0].x == 500)
        innermost_b = next(p for p in result
                           if p.feature == INNERWALL and p.path.path
                           and p.path.path[0].x == 5500)
        assert innermost_a.z_offset == 0
        assert innermost_b.z_offset == 0

    def test_safety_cap_br_motor_holder_10_tight_contours(self, server, modify_stub):
        """Full reproducer: 10 contours 3 mm apart (edge-to-edge), each 2 mm
        wide, with realistic 550 µm line_width — the real-world BR MOTOR
        HOLDER layer 148 geometry. User's 10 mm setting would merge all 10
        into one sub-group; safety cap of 1.65 mm forces a split per contour.
        """
        server.settings.contour_break_distance = 10_000  # 10 mm (user profile)
        server.settings.inset_direction = "outside_in"

        paths = []
        for i in range(10):
            x0 = i * 5000  # 5 mm centre-to-centre → 3 mm edge-to-edge
            paths.extend([
                _rect_wall_lw(x0,        x0 + 2000, 0,    2000, line_width=550),
                _rect_wall_lw(x0 + 400,  x0 + 1600, 400,  1600, line_width=550),
                _rect_wall_lw(x0 + 800,  x0 + 1200, 800,  1200, line_width=550),
            ])

        resp = modify_stub.Call(_call_request(paths, layer_nr=148))
        result = list(resp.gcode_paths)

        shifted = [p for p in result
                   if p.feature == INNERWALL and p.z_offset > 0]
        # 10 contours × 1 shifted per contour (3 walls: outer SHIFT, mid keep,
        # innermost protected) = 10 shifted total.
        assert len(shifted) == 10, (
            f"Expected 10 shifted (one per contour), got {len(shifted)}. "
            f"This is the EXACT failure mode the safety cap must prevent — "
            f"a lower count means contours merged and their innermost walls "
            f"were wrongly shifted to half-layer Z, producing Cura's 'broken "
            f"inner wall' preview pattern."
        )
        for i in range(10):
            xmin = i * 5000
            innermost = next(
                p for p in result
                if p.feature == INNERWALL and p.path.path
                and p.path.path[0].x == xmin + 800
            )
            assert innermost.z_offset == 0, (
                f"Contour {i} (x={xmin}): innermost wall was shifted "
                f"— safety cap failed for this geometry"
            )

    def test_safety_cap_bypassed_by_test_sentinel(self, server, modify_stub):
        """Setting contour_break_distance ≥ 100 mm (test sentinel) disables
        the distance FALLBACK — but bbox-overlap clustering always runs and
        is the primary signal now. Three spatially-disjoint rectangles are
        clustered into three single-wall sub-groups, each too small (< 2)
        to produce shifts.
        """
        server.settings.contour_break_distance = 1_000_000  # test sentinel
        paths = [
            _rect_wall_lw(0,      2000,  0,    2000, line_width=550),
            _rect_wall_lw(10_000, 12_000, 0,   2000, line_width=550),
            _rect_wall_lw(20_000, 22_000, 0,   2000, line_width=550),
        ]
        resp = modify_stub.Call(_call_request(paths))
        result = list(resp.gcode_paths)

        shifted = [p for p in result
                   if p.feature == INNERWALL and p.z_offset > 0]
        # Three disjoint-bbox clusters, each size 1 → no shifts possible.
        assert len(shifted) == 0, (
            f"Disjoint contours with 1 wall each must not shift; "
            f"got {len(shifted)}"
        )

    def test_safety_cap_does_not_split_concentric_walls(self, server, modify_stub):
        """Walls within a single contour (spaced at line_width) stay in the
        same sub-group — the safety cap of 3 × line_width has enough headroom.
        """
        server.settings.contour_break_distance = 10_000
        # Single contour with 3 nested walls spaced at line_width (550 µm).
        paths = [
            _rect_wall_lw(0,     2000, 0,    2000, line_width=550),  # outer
            _rect_wall_lw(550,   1450, 550,  1450, line_width=550),  # mid
            _rect_wall_lw(1100,  900,  1100, 900,  line_width=550),  # innermost (degenerate but bounded bbox smaller)
        ]
        # The endpoint-to-start distance between consecutive inner walls:
        #   wall 0 end (0, 2000) → wall 1 start (550, 550): √(0.3025+2.1025) = 1.55 mm
        #   wall 1 end (550, 1450) → wall 2 start (1100, 1100): √(0.3025+0.1225) = 0.65 mm
        # 1.55 mm < safety cap 3 × 550 = 1.65 mm, so walls stay together.
        resp = modify_stub.Call(_call_request(paths))
        result = list(resp.gcode_paths)

        shifted = [p for p in result
                   if p.feature == INNERWALL and p.z_offset > 0]
        # Single sub-group, 1 innermost protected, 2 remaining: counter 0 SHIFT,
        # counter 1 keep → exactly 1 shifted.
        assert len(shifted) == 1, (
            f"Concentric walls within one contour must not split; "
            f"got {len(shifted)} shifted (expected 1)"
        )


# ===========================================================================
# NEGATIVE END_LAYER (python-style indexing)
# ===========================================================================


class TestNegativeEndLayer:
    """``brick_layers_end_layer`` accepts Python-style negative indexing.

    -1 (legacy) = no cap (apply to all layers).
    -3 = leave the top 3 layers plain. Resolved via
    ``machine_height_um // layer_height`` at modify time.
    """

    def _paths_three_inner_walls(self):
        return [
            _rect_wall_lw(0,    2000, 0,    2000, line_width=400),
            _rect_wall_lw(400,  1600, 400,  1600, line_width=400),
            _rect_wall_lw(800,  1200, 800,  1200, line_width=400),
        ]

    def test_minus_one_disables_upper_cap(self, server, modify_stub):
        server.settings.end_layer = -1
        server.settings.layer_height = 180
        server.settings.machine_height_um = 300_000  # 300 mm
        paths = self._paths_three_inner_walls()
        # A very high layer should still be bricked.
        resp = modify_stub.Call(_call_request(paths, layer_nr=1000))
        shifted = [p for p in resp.gcode_paths if p.feature == INNERWALL and p.z_offset > 0]
        assert len(shifted) == 1  # 3 walls → 1 shift per our algorithm

    def test_zero_also_disables_upper_cap(self, server, modify_stub):
        """0 is a legacy "apply to all" sentinel, same as -1."""
        server.settings.end_layer = 0
        server.settings.layer_height = 180
        server.settings.machine_height_um = 300_000
        resp = modify_stub.Call(_call_request(
            self._paths_three_inner_walls(), layer_nr=1000,
        ))
        shifted = [p for p in resp.gcode_paths if p.feature == INNERWALL and p.z_offset > 0]
        assert len(shifted) == 1

    def test_positive_value_caps_inclusive(self, server, modify_stub):
        server.settings.end_layer = 5   # inclusive; layer_nr 0..4 get bricked
        server.settings.layer_height = 180
        server.settings.machine_height_um = 300_000

        resp_in = modify_stub.Call(_call_request(
            self._paths_three_inner_walls(), layer_nr=4,
        ))
        in_shifted = [p for p in resp_in.gcode_paths
                      if p.feature == INNERWALL and p.z_offset > 0]
        assert len(in_shifted) == 1, "layer_nr 4 should still be bricked (end=5 → idx≤4)"

        resp_out = modify_stub.Call(_call_request(
            self._paths_three_inner_walls(), layer_nr=5,
        ))
        out_shifted = [p for p in resp_out.gcode_paths
                       if p.feature == INNERWALL and p.z_offset > 0]
        assert len(out_shifted) == 0, "layer_nr 5 must be skipped"

    def test_minus_three_leaves_top_three_layers_plain(self, server, modify_stub):
        """Python-style: -3 = leave top 3 layers plain. With 300 mm build height
        and 0.18 mm layers, total ≈ 1666 layers → effective end ≈ 1663. So
        layer_nr 1662 gets bricked, 1663 (and later) must be skipped.

        (end_layer is 1-indexed inclusive, the plugin uses end-1 as the last
        bricked 0-indexed layer, so effective=1663 ⇒ last bricked idx = 1662.)
        """
        server.settings.end_layer = -3
        server.settings.layer_height = 180
        server.settings.machine_height_um = 300_000  # 300 mm → 1666 layers
        # effective = 1666 - 3 = 1663
        resp_in = modify_stub.Call(_call_request(
            self._paths_three_inner_walls(), layer_nr=1662,
        ))
        in_shifted = [p for p in resp_in.gcode_paths
                      if p.feature == INNERWALL and p.z_offset > 0]
        assert len(in_shifted) == 1, "layer_nr 1662 should still be bricked"

        resp_out = modify_stub.Call(_call_request(
            self._paths_three_inner_walls(), layer_nr=1663,
        ))
        out_shifted = [p for p in resp_out.gcode_paths
                       if p.feature == INNERWALL and p.z_offset > 0]
        assert len(out_shifted) == 0, (
            "layer_nr 1663 must be skipped (in the top 3 plain layers)"
        )

    def test_negative_without_machine_height_degrades_to_no_cap(
        self, server, modify_stub
    ):
        """When machine_height_um is unknown (0), negative values can't be
        resolved. The plugin falls back to "no cap" so the print still works
        (just applies bricks to all layers, same as legacy behaviour)."""
        server.settings.end_layer = -3
        server.settings.layer_height = 180
        server.settings.machine_height_um = 0  # unknown
        resp = modify_stub.Call(_call_request(
            self._paths_three_inner_walls(), layer_nr=10_000,
        ))
        shifted = [p for p in resp.gcode_paths
                   if p.feature == INNERWALL and p.z_offset > 0]
        assert len(shifted) == 1, (
            "Negative end_layer with unknown build height should fall back "
            "to 'apply everywhere' rather than silently disabling the plugin"
        )

    def test_effective_end_layer_unit(self):
        """Unit-test the resolver independent of the gRPC server."""
        s = BrickSettings(layer_height=180, machine_height_um=300_000)
        s.end_layer = -1
        assert s.effective_end_layer() == -1
        s.end_layer = 0
        assert s.effective_end_layer() == -1
        s.end_layer = 42
        assert s.effective_end_layer() == 42
        s.end_layer = -3
        # 300_000 // 180 = 1666; 1666 - 3 = 1663
        assert s.effective_end_layer() == 1663
        s.end_layer = -1000  # 1666 - 1000 = 666 (still positive, unchanged)
        assert s.effective_end_layer() == 666
        s.end_layer = -5000  # 1666 - 5000 = -3334 → clamped to 1 (never ≤ 0)
        assert s.effective_end_layer() == 1
        # No layer_height → fallback to -1
        s2 = BrickSettings(end_layer=-3, machine_height_um=300_000)
        assert s2.effective_end_layer() == -1


class TestMachineHeightBroadcast:
    """The host parses machine_height from the settings broadcast so it can
    resolve python-style negative end_layer values at modify time."""

    def test_machine_height_parsed_mm_to_microns(self, server, broadcast_stub):
        req = _settings_request({
            "brick_layers_enabled": "True",
            "machine_height": "300",  # mm
        })
        broadcast_stub.BroadcastSettings(req)
        assert server.settings.machine_height_um == 300_000

    def test_machine_height_absent_leaves_default(self, server, broadcast_stub):
        server.settings.machine_height_um = 42_000  # arbitrary non-default
        req = _settings_request({"brick_layers_enabled": "True"})
        broadcast_stub.BroadcastSettings(req)
        # Missing key → value not overwritten by the prototype parser
        assert server.settings.machine_height_um == 42_000

    def test_machine_height_rejects_invalid(self, server, broadcast_stub):
        server.settings.machine_height_um = 111_000
        req = _settings_request({
            "brick_layers_enabled": "True",
            "machine_height": "not-a-number",
        })
        broadcast_stub.BroadcastSettings(req)
        # Default stays unchanged
        assert server.settings.machine_height_um == 111_000


# ===========================================================================
# PER-EXTRUDER SETTINGS RESOLUTION
# ===========================================================================


class TestPerExtruderOverrides:
    """The broadcast contains a `extruder_settings` list ordered by extruder
    index. The plugin parses global_settings as a baseline, overlays each
    extruder's overrides on top, and at modify time picks the resolved
    BrickSettings for the request's `extruder_nr`.
    """

    def test_extruder_override_changes_enabled_for_that_extruder_only(
        self, grpc_server, broadcast_stub, modify_stub
    ):
        # Global: bricks OFF. Extruder 0: override ON. Extruder 1: no override.
        global_settings = broadcast_pb2.Settings(settings={
            "brick_layers_enabled": b"false",
            "layer_height": b"0.2",
        })
        ext0 = broadcast_pb2.Settings(settings={
            "brick_layers_enabled": b"true",
        })
        ext1 = broadcast_pb2.Settings(settings={})
        req = broadcast_pb2.BroadcastServiceSettingsRequest(
            global_settings=global_settings,
            extruder_settings=[ext0, ext1],
        )
        broadcast_stub.BroadcastSettings(req)

        bcast = grpc_server.broadcast_servicer
        assert len(bcast._per_extruder) == 2
        assert bcast._per_extruder[0].enabled is True, "extruder 0 should see its ON override"
        assert bcast._per_extruder[1].enabled is False, "extruder 1 inherits the global OFF"

    def test_extruder_specific_end_layer_respected_at_modify_time(
        self, grpc_server, broadcast_stub, modify_stub
    ):
        global_settings = broadcast_pb2.Settings(settings={
            "brick_layers_enabled": b"true",
            "brick_layers_end_layer": b"-1",
            "layer_height": b"0.2",
        })
        # Extruder 0: apply only up to layer 3 (inclusive).
        ext0 = broadcast_pb2.Settings(settings={
            "brick_layers_end_layer": b"3",
        })
        # Extruder 1: unchanged → inherits -1 (no cap).
        ext1 = broadcast_pb2.Settings(settings={})
        req = broadcast_pb2.BroadcastServiceSettingsRequest(
            global_settings=global_settings,
            extruder_settings=[ext0, ext1],
        )
        broadcast_stub.BroadcastSettings(req)

        paths = [
            _rect_wall_lw(0,    2000, 0,    2000, line_width=400),
            _rect_wall_lw(400,  1600, 400,  1600, line_width=400),
            _rect_wall_lw(800,  1200, 800,  1200, line_width=400),
        ]

        # extruder 0 at layer_nr=2 (< end=3) → bricked
        resp_ext0_in = modify_stub.Call(modify_pb2.CallRequest(
            gcode_paths=paths, layer_nr=2, extruder_nr=0,
        ))
        shifted_ext0_in = [p for p in resp_ext0_in.gcode_paths
                           if p.feature == INNERWALL and p.z_offset > 0]
        assert len(shifted_ext0_in) >= 1, "extruder 0 layer 2 should be bricked"

        # extruder 0 at layer_nr=5 (> end=3) → passthrough
        resp_ext0_out = modify_stub.Call(modify_pb2.CallRequest(
            gcode_paths=paths, layer_nr=5, extruder_nr=0,
        ))
        shifted_ext0_out = [p for p in resp_ext0_out.gcode_paths
                            if p.feature == INNERWALL and p.z_offset > 0]
        assert len(shifted_ext0_out) == 0, "extruder 0 layer 5 must passthrough (end=3)"

        # extruder 1 at layer_nr=5 (no cap) → still bricked
        resp_ext1 = modify_stub.Call(modify_pb2.CallRequest(
            gcode_paths=paths, layer_nr=5, extruder_nr=1,
        ))
        shifted_ext1 = [p for p in resp_ext1.gcode_paths
                        if p.feature == INNERWALL and p.z_offset > 0]
        assert len(shifted_ext1) >= 1, (
            "extruder 1 inherits global end=-1 (no cap), should still brick at layer 5"
        )


# ===========================================================================
# FIXTURE LOADER SMOKE TEST
# ===========================================================================


class TestFixtureLoader:
    """Validate the tests.fixture_loader helpers: construct a synthetic
    dump in a temp directory, round-trip it via the loader, assert field
    equality. Real-world usage: run Cura with BRICKLAYERS_DUMP_LAYERS set,
    then load the produced .bin files into regression tests.
    """

    def test_round_trip_request(self, tmp_path):
        from tests.fixture_loader import load_request, filename_for

        req = modify_pb2.CallRequest(
            layer_nr=148, extruder_nr=0,
            gcode_paths=[_rect_wall_lw(0, 2000, 0, 2000, line_width=550)],
        )
        fname = filename_for(148, 0, "in")
        (tmp_path / fname).write_bytes(req.SerializeToString())

        loaded = load_request(fname, dump_dir=tmp_path)
        assert loaded.layer_nr == 148
        assert loaded.extruder_nr == 0
        assert len(loaded.gcode_paths) == 1
        assert loaded.gcode_paths[0].line_width == 550

    def test_iter_layer_dumps_sorted(self, tmp_path):
        from tests.fixture_loader import iter_layer_dumps

        # Create dumps for (150, 0), (148, 0), (148, 1).
        for ln, ext in [(150, 0), (148, 0), (148, 1)]:
            req = modify_pb2.CallRequest(layer_nr=ln, extruder_nr=ext)
            (tmp_path / f"layer_{ln}_ext{ext}_in.bin").write_bytes(req.SerializeToString())

        pairs = [(ln, ext) for ln, ext, _ in iter_layer_dumps(dump_dir=tmp_path)]
        assert pairs == [(148, 0), (148, 1), (150, 0)], (
            "iter_layer_dumps must yield in (layer, extruder) ascending order"
        )


# ===========================================================================
# PER-MESH DISABLE (per-path brick_layers_enabled override)
# ===========================================================================


class TestPerMeshEnabledOverride:
    """When a mesh has ``brick_layers_enabled = false`` in its per-mesh
    override, the plugin passes through walls belonging to that mesh even
    though the global setting is enabled. Walls from other meshes are
    bricked normally.
    """

    def test_per_mesh_disabled_skips_that_mesh_only(
        self, grpc_server, broadcast_stub, modify_stub
    ):
        # Global: bricks ON. Mesh "B" overrides: bricks OFF.
        global_settings = broadcast_pb2.Settings(settings={
            "brick_layers_enabled": b"true",
            "layer_height": b"0.2",
        })
        obj_a = broadcast_pb2.Settings(settings={"mesh_name": b"MeshA"})
        obj_b = broadcast_pb2.Settings(settings={
            "mesh_name": b"MeshB",
            "brick_layers_enabled": b"false",
        })
        req = broadcast_pb2.BroadcastServiceSettingsRequest(
            global_settings=global_settings,
            extruder_settings=[],
            object_settings=[obj_a, obj_b],
        )
        broadcast_stub.BroadcastSettings(req)

        # Two contours: one per mesh, each 3 walls that are large enough
        # (5 mm) to pass the wall-extent filter.
        paths_a = [
            _rect_wall_lw(0,    5000, 0,    5000, line_width=400, mesh="MeshA"),
            _rect_wall_lw(400,  4600, 400,  4600, line_width=400, mesh="MeshA"),
            _rect_wall_lw(800,  4200, 800,  4200, line_width=400, mesh="MeshA"),
        ]
        paths_b = [
            _rect_wall_lw(20000, 25000, 0,    5000, line_width=400, mesh="MeshB"),
            _rect_wall_lw(20400, 24600, 400,  4600, line_width=400, mesh="MeshB"),
            _rect_wall_lw(20800, 24200, 800,  4200, line_width=400, mesh="MeshB"),
        ]
        resp = modify_stub.Call(_call_request(paths_a + paths_b))
        result = list(resp.gcode_paths)

        shifted_a = [p for p in result
                     if p.feature == INNERWALL and p.mesh_name == "MeshA"
                     and p.z_offset > 0]
        shifted_b = [p for p in result
                     if p.feature == INNERWALL and p.mesh_name == "MeshB"
                     and p.z_offset > 0]
        assert len(shifted_a) >= 1, "MeshA should be bricked (global enabled)"
        assert len(shifted_b) == 0, (
            "MeshB should passthrough (per-mesh brick_layers_enabled=false), "
            f"got {len(shifted_b)} shifted walls"
        )

    def test_default_inherited_enabled_false_does_not_disable_mesh(
        self, grpc_server, broadcast_stub, modify_stub
    ):
        """REGRESSION (2026-04-20): Cura's per-mesh container resolves every
        ``settable_per_mesh`` setting to a value — even when the user never
        overrode it — using the global stack default. Since
        ``brick_layers_enabled``'s default is ``false``, a naive
        "per-mesh enabled=false means disabled" rule silenced bricks on
        every mesh the moment we flipped ``settable_per_mesh: true``.

        Fix: only treat a mesh as disabled when its object_settings map
        PHYSICALLY contains ``brick_layers_enabled`` with a false value.
        A map that only has ``mesh_name`` (which Cura broadcasts when the
        user has merely opened the Per Model Settings dialog for that mesh)
        must NOT count as an explicit disable.
        """
        # Global: bricks ON. Two meshes, neither has an explicit per-mesh
        # override — object_settings only carries mesh_name.
        global_settings = broadcast_pb2.Settings(settings={
            "brick_layers_enabled": b"true",
            "layer_height": b"0.2",
        })
        obj_a = broadcast_pb2.Settings(settings={"mesh_name": b"MeshA"})
        obj_b = broadcast_pb2.Settings(settings={"mesh_name": b"MeshB"})
        req = broadcast_pb2.BroadcastServiceSettingsRequest(
            global_settings=global_settings, object_settings=[obj_a, obj_b],
        )
        broadcast_stub.BroadcastSettings(req)

        assert grpc_server.broadcast_servicer._explicitly_disabled_meshes == set(), (
            "No mesh has an explicit brick_layers_enabled override — none "
            "should appear in the disabled set"
        )

        paths = [
            _rect_wall_lw(0,    5000, 0,    5000, line_width=400, mesh="MeshA"),
            _rect_wall_lw(400,  4600, 400,  4600, line_width=400, mesh="MeshA"),
            _rect_wall_lw(800,  4200, 800,  4200, line_width=400, mesh="MeshA"),
        ]
        resp = modify_stub.Call(_call_request(paths))
        result = list(resp.gcode_paths)
        shifted = [p for p in result
                   if p.feature == INNERWALL and p.z_offset > 0]
        assert len(shifted) >= 1, (
            "Bricks should still be applied when no explicit per-mesh "
            "override is set — the global brick_layers_enabled=true wins"
        )

    def test_explicit_enabled_false_with_default_value_still_disables(
        self, grpc_server, broadcast_stub, modify_stub
    ):
        """Edge case: Cura's default for brick_layers_enabled is false. A
        per-mesh object_settings entry that explicitly contains
        ``brick_layers_enabled=false`` IS user intent — the fact that the
        value matches the default doesn't weaken the signal. We must still
        disable that mesh.
        """
        global_settings = broadcast_pb2.Settings(settings={
            "brick_layers_enabled": b"true",
            "layer_height": b"0.2",
        })
        # Explicitly false — the key is physically present.
        obj_b = broadcast_pb2.Settings(settings={
            "mesh_name": b"MeshB",
            "brick_layers_enabled": b"false",
        })
        req = broadcast_pb2.BroadcastServiceSettingsRequest(
            global_settings=global_settings, object_settings=[obj_b],
        )
        broadcast_stub.BroadcastSettings(req)
        assert "MeshB" in grpc_server.broadcast_servicer._explicitly_disabled_meshes

    def test_per_mesh_disable_preserves_wall_count(
        self, grpc_server, broadcast_stub, modify_stub
    ):
        """Disabled-mesh walls must still appear unchanged in the output —
        they're filtered out of target grouping, not out of the result."""
        global_settings = broadcast_pb2.Settings(settings={
            "brick_layers_enabled": b"true",
            "layer_height": b"0.2",
        })
        obj_b = broadcast_pb2.Settings(settings={
            "mesh_name": b"MeshB",
            "brick_layers_enabled": b"false",
        })
        req = broadcast_pb2.BroadcastServiceSettingsRequest(
            global_settings=global_settings, object_settings=[obj_b],
        )
        broadcast_stub.BroadcastSettings(req)

        paths_b = [
            _rect_wall_lw(20000, 25000, 0,    5000, line_width=400, mesh="MeshB"),
            _rect_wall_lw(20400, 24600, 400,  4600, line_width=400, mesh="MeshB"),
            _rect_wall_lw(20800, 24200, 800,  4200, line_width=400, mesh="MeshB"),
        ]
        resp = modify_stub.Call(_call_request(paths_b))
        result = list(resp.gcode_paths)

        inner_walls = [p for p in result
                       if p.feature == INNERWALL and p.mesh_name == "MeshB"]
        assert len(inner_walls) == 3, (
            f"Disabled-mesh walls must pass through unchanged; expected 3, "
            f"got {len(inner_walls)}"
        )
        # All at normal Z (no shift).
        assert all(p.z_offset == 0 for p in inner_walls)


# ===========================================================================
# WALL-EXTENT FILTER (0.1 mm fragments)
# ===========================================================================


class TestTinyWallFilter:
    """Paths tagged as INNERWALL/OUTERWALL but with XY extent below
    ~0.4 mm are CuraEngine gap-fill/dense-infill fragments. The plugin
    excludes them from target grouping so they pass through at normal Z,
    instead of being shifted to half-layer Z where they appear as
    scattered 0.1 mm green fragments in Cura's preview.
    """

    def test_tiny_inner_wall_passthrough_not_shifted(self, server, modify_stub):
        """A 100µm gap-fill fragment must not be shifted, even when
        surrounded by normal walls in the same contour."""
        server.settings.contour_break_distance = 1_000_000
        paths = [
            _rect_wall_lw(0,    5000, 0,    5000, line_width=400),  # outermost, 20mm perim
            # Tiny gap-fill: 2 points 100µm apart — below 400µm threshold
            gcode_path_pb2.GCodePath(
                feature=INNERWALL, mesh_name="Mesh",
                layer_thickness=200, flow_ratio=1.0, line_width=400,
            ),
            _rect_wall_lw(400,  4600, 400,  4600, line_width=400),
            _rect_wall_lw(800,  4200, 800,  4200, line_width=400),
        ]
        paths[1].path.path.append(point3d_pb2.Point3D(x=100, y=100, z=200))
        paths[1].path.path.append(point3d_pb2.Point3D(x=150, y=150, z=200))

        resp = modify_stub.Call(_call_request(paths))
        result = list(resp.gcode_paths)

        # The tiny 100µm fragment must exist in output at normal Z.
        tiny = next(
            (p for p in result
             if p.feature == INNERWALL and p.path.path
             and p.path.path[0].x == 100 and p.path.path[0].y == 100),
            None,
        )
        assert tiny is not None, "tiny INNERWALL fragment must pass through"
        assert tiny.z_offset == 0, (
            f"tiny 100µm fragment must stay at normal Z (0), "
            f"got z_offset={tiny.z_offset}"
        )

    def test_tiny_synth_travel_skipped(self, server, modify_stub):
        """Bridge-DOWN between a shifted sub-sequence's last point and the
        anchor endpoint MUST be skipped when the two points are < 100 µm
        apart (sub-pixel Cura mis-renders those as inner walls)."""
        # Same XY for all walls → zero-length bridge-DOWN should be skipped.
        server.settings.contour_break_distance = 1_000_000
        paths = [
            _rect_wall_lw(0,    5000, 0,    5000, line_width=400),
            _rect_wall_lw(0,    5000, 0,    5000, line_width=400),   # identical
            _rect_wall_lw(400,  4600, 400,  4600, line_width=400),   # innermost
        ]
        resp = modify_stub.Call(_call_request(paths))
        result = list(resp.gcode_paths)

        # Any synth retracted travel emitted must have XY delta ≥ 100µm.
        for p in result:
            if p.feature == MOVERETRACTED and p.retract and len(p.path.path) == 2:
                a, b = p.path.path
                dx = abs(b.x - a.x)
                dy = abs(b.y - a.y)
                assert dx + dy >= 100, (
                    f"synth travel shorter than 100µm emitted: ({a.x},{a.y}) → ({b.x},{b.y})"
                )


# ===========================================================================
# REAL-WORLD FIXTURE: BR MOTOR HOLDER layer 148 extruder 0
# ===========================================================================


class TestBRMotorHolderLayer148Fixture:
    """Regression test using a real dump captured from Cura for layer 148,
    extruder 0 of the BR MOTOR HOLDER print.

    The fixture contains 821 paths, 243 INNERWALLs — 183 of which are
    single-point seam-anchor markers CuraEngine intersperses between real
    walls. Before the "tolerate filtered wall features" fix, these
    single-point paths split real walls into singleton groups and only 8
    shifts were emitted for the whole layer (vs the ~100+ expected).
    """

    FIXTURE = "tests/fixtures/layer_148_ext0_in.bin"

    def _load(self):
        import os
        from cura.plugins.slots.gcode_paths.v0 import modify_pb2
        path = os.path.join(os.path.dirname(__file__), "fixtures/layer_148_ext0_in.bin")
        if not os.path.exists(path):
            pytest.skip(f"fixture missing: {path}")
        req = modify_pb2.CallRequest()
        req.ParseFromString(open(path, "rb").read())
        return req

    def test_br_motor_holder_layer_148_produces_expected_shift_count(
        self, grpc_server, broadcast_stub, modify_stub
    ):
        """With the real input, the plugin should shift at least ~30 walls
        (roughly half of the 60 real multi-point inner walls — the other
        half is innermost-protected or between-counter). The pre-fix
        behaviour emitted only 8 shifts for the whole layer."""
        # Match the profile used when the fixture was captured:
        global_settings = broadcast_pb2.Settings(settings={
            "brick_layers_enabled": b"true",
            "layer_height": b"0.18",
            "inset_direction": b"outside_in",
            "brick_layers_apply_inner_walls": b"true",
            "brick_layers_apply_outer_walls": b"false",
            "brick_layers_extrusion_multiplier": b"1.04",
            "brick_layers_start_layer": b"3",
            "brick_layers_end_layer": b"-1",
            "brick_layers_contour_break_distance": b"2.0",
            "machine_height": b"300",
        })
        req = broadcast_pb2.BroadcastServiceSettingsRequest(
            global_settings=global_settings,
        )
        broadcast_stub.BroadcastSettings(req)

        call_req = self._load()
        resp = modify_stub.Call(call_req)
        result = list(resp.gcode_paths)

        shifted = [p for p in result
                   if p.feature == INNERWALL and p.z_offset > 0]
        assert len(shifted) >= 20, (
            f"BR MOTOR HOLDER layer 148 should yield ≥20 shifted inner walls, "
            f"got {len(shifted)}. The 'tolerate single-point seam anchors' "
            f"fix in Phase 1 is what makes multi-wall groups stay together; "
            f"if this count is 0-10, that fix regressed."
        )

    def test_br_motor_holder_layer_148_output_preserves_all_inputs(
        self, grpc_server, broadcast_stub, modify_stub
    ):
        """Every non-shifted input path must still appear in the output."""
        global_settings = broadcast_pb2.Settings(settings={
            "brick_layers_enabled": b"true",
            "layer_height": b"0.18",
            "inset_direction": b"outside_in",
        })
        req = broadcast_pb2.BroadcastServiceSettingsRequest(global_settings=global_settings)
        broadcast_stub.BroadcastSettings(req)

        call_req = self._load()
        resp = modify_stub.Call(call_req)
        result = list(resp.gcode_paths)

        # Input innerwall count == output normal-Z innerwall count + shifted count.
        input_iw = sum(1 for p in call_req.gcode_paths if p.feature == INNERWALL)
        output_iw = sum(1 for p in result if p.feature == INNERWALL)
        assert output_iw == input_iw, (
            f"INNERWALL count preserved: input={input_iw}, output={output_iw}"
        )
