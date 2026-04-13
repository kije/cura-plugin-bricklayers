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
        broadcast_pb2_grpc.add_BroadcastServiceServicer_to_server(
            BroadcastServicer(self.settings), self._server
        )
        modify_pb2_grpc.add_GCodePathsModifyServiceServicer_to_server(
            GCodePathsModifyServicer(self.settings), self._server
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
        # After reorder: shifted wall is last
        assert result[-1].z_offset == 100

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
        assert result[-1].z_offset == 150  # 300 // 2

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
        # Shifted wall is last after reorder
        assert abs(result[-1].flow_ratio - 1.10) < 1e-4

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
        assert result[-1].z_offset == 100

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
        assert result[-1].z_offset == 100

    def test_end_layer_minus_one_means_unlimited(self, server, modify_stub):
        server.settings.end_layer = -1
        paths = [_make_path(INNERWALL) for _ in range(3)]
        resp = modify_stub.Call(_call_request(paths, layer_nr=9999))
        result = list(resp.gcode_paths)
        assert result[-1].z_offset == 100

    # --- first / last brick layer multiplier ---

    def test_first_brick_layer_115x_multiplier(self, server, modify_stub):
        server.settings.start_layer = 2
        server.settings.extrusion_multiplier = 1.05
        paths = [_make_path(INNERWALL) for _ in range(3)]
        resp = modify_stub.Call(_call_request(paths, layer_nr=2))
        result = list(resp.gcode_paths)
        expected = 1.05 * 1.15
        assert abs(result[-1].flow_ratio - expected) < 1e-4

    def test_last_brick_layer_085x_multiplier(self, server, modify_stub):
        server.settings.end_layer = 10
        server.settings.extrusion_multiplier = 1.05
        paths = [_make_path(INNERWALL) for _ in range(3)]
        resp = modify_stub.Call(_call_request(paths, layer_nr=9))  # end_layer - 1
        result = list(resp.gcode_paths)
        expected = 1.05 * 0.85
        assert abs(result[-1].flow_ratio - expected) < 1e-4

    def test_middle_layer_uses_base_multiplier(self, server, modify_stub):
        server.settings.start_layer = 2
        server.settings.end_layer = 10
        server.settings.extrusion_multiplier = 1.05
        paths = [_make_path(INNERWALL) for _ in range(3)]
        resp = modify_stub.Call(_call_request(paths, layer_nr=5))
        result = list(resp.gcode_paths)
        assert abs(result[-1].flow_ratio - 1.05) < 1e-4

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
        assert result[-1].z_offset == 200  # 400 // 2 from path data

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
        assert r1[-1].z_offset == 100
        assert r2[-1].z_offset == 100

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
    """After applying z_offset shifts, paths must be stably partitioned:
    all normal-Z paths first, then all shifted paths.
    This minimises nozzle Z oscillation within a layer.
    """

    def test_shifted_walls_appear_after_normal_z_paths(self, server, modify_stub):
        """In a 3-wall group, the shifted wall must come last in output."""
        paths = [_make_path(INNERWALL) for _ in range(3)]
        resp = modify_stub.Call(_call_request(paths))
        result = list(resp.gcode_paths)
        # Find the transition point: once we see a shifted path,
        # all remaining paths must also be shifted (or there are none left)
        saw_shifted = False
        for p in result:
            if p.z_offset > 0:
                saw_shifted = True
            elif saw_shifted:
                # Normal-Z path after a shifted path = ordering violation
                assert False, "Normal-Z path found after shifted path"

    def test_non_wall_paths_in_normal_z_group(self, server, modify_stub):
        """OUTERWALL + 3 INNERWALL + INFILL: within wall paths, normal-Z walls
        precede shifted walls.  Non-wall paths (infill) retain original position."""
        paths = [
            _make_path(OUTERWALL),
            _make_path(INNERWALL),
            _make_path(INNERWALL),
            _make_path(INNERWALL),
            _make_path(INFILL),
        ]
        resp = modify_stub.Call(_call_request(paths))
        result = list(resp.gcode_paths)
        # Within wall paths, normal-Z must precede shifted
        saw_shifted_wall = False
        for p in result:
            is_wall = p.feature in (INNERWALL, OUTERWALL)
            if is_wall and p.z_offset > 0:
                saw_shifted_wall = True
            elif is_wall and saw_shifted_wall:
                assert False, f"Normal-Z wall after shifted wall"

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
        """All normal-Z paths must precede all shifted-Z paths."""
        saw_shifted = False
        for p in result:
            if p.z_offset > 0:
                saw_shifted = True
            elif saw_shifted and p.feature not in (
                MOVERETRACTED, MOVEUNRETRACTED,
            ):
                # Allow move paths at z=0 in normal pass before shifted starts
                # But no wall/infill/skin at z=0 after a shifted wall
                if p.feature in (INNERWALL, OUTERWALL, INFILL, SKIN):
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
