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
) -> gcode_path_pb2.GCodePath:
    """Construct a GCodePath protobuf message for use in tests."""
    path = gcode_path_pb2.GCodePath(
        feature=feature,
        z_offset=z_offset,
        layer_thickness=layer_thickness,
        flow_ratio=flow_ratio,
        line_width=line_width,
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
        # After reorder: 2 normal-Z then 2 shifted
        paths = [_make_path(INNERWALL) for _ in range(4)]
        resp = modify_stub.Call(_call_request(paths))
        result = list(resp.gcode_paths)
        offsets = [p.z_offset for p in result]
        assert offsets == [0, 0, 100, 100]

    def test_z_offset_equals_half_layer_height(self, server, modify_stub):
        server.settings.layer_height = 300
        paths = [_make_path(INNERWALL) for _ in range(3)]
        resp = modify_stub.Call(_call_request(paths))
        result = list(resp.gcode_paths)
        assert result[-1].z_offset == 150  # 300 // 2

    def test_existing_z_offset_is_additive(self, server, modify_stub):
        paths = [
            _make_path(INNERWALL, z_offset=50),
            _make_path(INNERWALL, z_offset=50),
            _make_path(INNERWALL, z_offset=50),
        ]
        resp = modify_stub.Call(_call_request(paths))
        result = list(resp.gcode_paths)
        assert result[0].z_offset == 50   # wall 0 (counter=0) — unchanged
        assert result[1].z_offset == 50   # innermost — protected
        assert result[2].z_offset == 150  # wall 1 (counter=1): 50 + 100

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
        assert len(result) == 6
        # After reorder: all normal-Z first, then shifted
        shifted = [p for p in result if p.z_offset > 0]
        assert len(shifted) == 1
        assert shifted[0].feature == INNERWALL
        # Non-wall paths must all be at z_offset=0
        for p in result:
            if p.feature not in (INNERWALL, OUTERWALL):
                assert p.z_offset == 0

    # --- path ordering preserved ---

    def test_response_path_count_matches_request(self, server, modify_stub):
        paths = [_make_path(INNERWALL) for _ in range(6)]
        resp = modify_stub.Call(_call_request(paths))
        assert len(list(resp.gcode_paths)) == 6

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
        shifted = [p for p in result if p.z_offset > 0]
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
        shifted = [p for p in result if p.z_offset > 0]
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
        shifted = [p for p in result if p.z_offset > 0]
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
        assert len(result) == 4
        # 1 wall shifted, SKIN unchanged, shifted wall last (reordered)
        shifted = [p for p in result if p.z_offset > 0]
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
        shifted = [p for p in result if p.z_offset > 0]
        unshifted = [p for p in result if p.z_offset == 0]
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
        shifted = [p for p in result if p.z_offset > 0]
        assert len(shifted) == 2

    def test_five_walls_outside_in_two_shifted(self, server, modify_stub):
        """5 inner walls: wall[4] protected.
        Non-innermost: wall[0](c=0,no), wall[1](c=1,YES), wall[2](c=2,no), wall[3](c=3,YES).
        """
        paths = [_make_path(INNERWALL) for _ in range(5)]
        resp = modify_stub.Call(_call_request(paths))
        result = list(resp.gcode_paths)
        shifted = [p for p in result if p.z_offset > 0]
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
        shifted = [p for p in result if p.z_offset > 0]
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
        shifted = [p for p in result if p.z_offset > 0]
        unshifted = [p for p in result if p.z_offset == 0]
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
        shifted = [p for p in result if p.z_offset > 0]
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
        shifted = [p for p in result if p.z_offset > 0]
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
        """OUTERWALL + 3 INNERWALL + INFILL: infill must appear before shifted walls."""
        paths = [
            _make_path(OUTERWALL),
            _make_path(INNERWALL),
            _make_path(INNERWALL),
            _make_path(INNERWALL),
            _make_path(INFILL),
        ]
        resp = modify_stub.Call(_call_request(paths))
        result = list(resp.gcode_paths)
        # All non-shifted paths (including infill) must come before shifted
        saw_shifted = False
        for p in result:
            if p.z_offset > 0:
                saw_shifted = True
            elif saw_shifted:
                assert False, f"Non-shifted path (feature={p.feature}) after shifted path"

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
        normal_z = [p for p in result if p.z_offset == 0]
        features = [p.feature for p in normal_z]
        assert features == [OUTERWALL, INNERWALL, INNERWALL, INFILL]

    def test_path_count_preserved_after_reordering(self, server, modify_stub):
        """Total number of paths must not change."""
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
        assert len(result) == 6

    def test_shifted_group_preserves_relative_order(self, server, modify_stub):
        """If multiple walls are shifted, their relative order is preserved."""
        # 5 inner walls: walls at counter 1 and 3 get shifted
        paths = [_make_path(INNERWALL) for _ in range(5)]
        resp = modify_stub.Call(_call_request(paths))
        result = list(resp.gcode_paths)
        shifted = [p for p in result if p.z_offset > 0]
        assert len(shifted) == 2
        # Both should have the same z_offset (100)
        assert all(p.z_offset == 100 for p in shifted)

    def test_no_shifting_means_no_reordering(self, server, modify_stub):
        """When nothing is shifted, path order is unchanged."""
        paths = [_make_path(SKIN), _make_path(INFILL), _make_path(SUPPORT)]
        resp = modify_stub.Call(_call_request(paths))
        result = list(resp.gcode_paths)
        assert [p.feature for p in result] == [SKIN, INFILL, SUPPORT]
