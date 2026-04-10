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
        # layer_height=200 → z_shift = 100
        paths = [_make_path(INNERWALL), _make_path(INNERWALL)]
        resp = modify_stub.Call(_call_request(paths))
        result = list(resp.gcode_paths)
        assert result[1].z_offset == 100

    def test_four_walls_alternating_pattern(self, server, modify_stub):
        paths = [_make_path(INNERWALL) for _ in range(4)]
        resp = modify_stub.Call(_call_request(paths))
        result = list(resp.gcode_paths)
        assert result[0].z_offset == 0
        assert result[1].z_offset == 100
        assert result[2].z_offset == 0
        assert result[3].z_offset == 100

    def test_z_offset_equals_half_layer_height(self, server, modify_stub):
        server.settings.layer_height = 300
        paths = [_make_path(INNERWALL), _make_path(INNERWALL)]
        resp = modify_stub.Call(_call_request(paths))
        result = list(resp.gcode_paths)
        assert result[1].z_offset == 150  # 300 // 2

    def test_existing_z_offset_is_additive(self, server, modify_stub):
        paths = [
            _make_path(INNERWALL, z_offset=50),
            _make_path(INNERWALL, z_offset=50),
        ]
        resp = modify_stub.Call(_call_request(paths))
        result = list(resp.gcode_paths)
        assert result[0].z_offset == 50   # even — unchanged
        assert result[1].z_offset == 150  # 50 + 100

    # --- flow_ratio ---

    def test_even_wall_flow_ratio_unchanged(self, server, modify_stub):
        paths = [_make_path(INNERWALL), _make_path(INNERWALL)]
        resp = modify_stub.Call(_call_request(paths))
        result = list(resp.gcode_paths)
        assert abs(result[0].flow_ratio - 1.0) < 1e-6

    def test_odd_wall_flow_ratio_multiplied(self, server, modify_stub):
        server.settings.extrusion_multiplier = 1.10
        paths = [_make_path(INNERWALL), _make_path(INNERWALL)]
        resp = modify_stub.Call(_call_request(paths))
        result = list(resp.gcode_paths)
        assert abs(result[1].flow_ratio - 1.10) < 1e-4

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
        paths = [
            _make_path(SKIN),
            _make_path(INNERWALL),
            _make_path(INFILL),
            _make_path(INNERWALL),
            _make_path(SUPPORT),
        ]
        resp = modify_stub.Call(_call_request(paths))
        result = list(resp.gcode_paths)
        assert len(result) == 5
        assert result[0].z_offset == 0    # SKIN
        assert result[1].z_offset == 0    # wall 0 (even)
        assert result[2].z_offset == 0    # INFILL
        assert result[3].z_offset == 100  # wall 1 (odd)
        assert result[4].z_offset == 0    # SUPPORT

    # --- path ordering preserved ---

    def test_response_path_count_matches_request(self, server, modify_stub):
        paths = [_make_path(INNERWALL) for _ in range(6)]
        resp = modify_stub.Call(_call_request(paths))
        assert len(list(resp.gcode_paths)) == 6

    def test_response_preserves_feature_types(self, server, modify_stub):
        features = [SKIN, INNERWALL, INFILL, OUTERWALL, SUPPORT]
        paths = [_make_path(f) for f in features]
        resp = modify_stub.Call(_call_request(paths))
        result = list(resp.gcode_paths)
        for i, f in enumerate(features):
            assert result[i].feature == f

    # --- wall type selection ---

    def test_inner_walls_only_outer_untouched(self, server, modify_stub):
        server.settings.apply_inner_walls = True
        server.settings.apply_outer_walls = False
        paths = [
            _make_path(OUTERWALL),
            _make_path(OUTERWALL),
            _make_path(INNERWALL),
            _make_path(INNERWALL),
        ]
        resp = modify_stub.Call(_call_request(paths))
        result = list(resp.gcode_paths)
        assert result[0].z_offset == 0  # outer (not targeted)
        assert result[1].z_offset == 0  # outer
        assert result[2].z_offset == 0  # inner wall 0 (even)
        assert result[3].z_offset == 100  # inner wall 1 (odd)

    def test_outer_walls_only_inner_untouched(self, server, modify_stub):
        server.settings.apply_inner_walls = False
        server.settings.apply_outer_walls = True
        paths = [
            _make_path(OUTERWALL),
            _make_path(OUTERWALL),
            _make_path(INNERWALL),
            _make_path(INNERWALL),
        ]
        resp = modify_stub.Call(_call_request(paths))
        result = list(resp.gcode_paths)
        assert result[0].z_offset == 0   # outer wall 0 (even)
        assert result[1].z_offset == 100  # outer wall 1 (odd)
        assert result[2].z_offset == 0   # inner (not targeted)
        assert result[3].z_offset == 0   # inner

    def test_both_wall_types_share_counter(self, server, modify_stub):
        server.settings.apply_inner_walls = True
        server.settings.apply_outer_walls = True
        paths = [
            _make_path(OUTERWALL),
            _make_path(INNERWALL),
            _make_path(OUTERWALL),
            _make_path(INNERWALL),
        ]
        resp = modify_stub.Call(_call_request(paths))
        result = list(resp.gcode_paths)
        assert result[0].z_offset == 0
        assert result[1].z_offset == 100
        assert result[2].z_offset == 0
        assert result[3].z_offset == 100

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
        paths = [_make_path(INNERWALL), _make_path(INNERWALL)]
        resp = modify_stub.Call(_call_request(paths, layer_nr=3))
        result = list(resp.gcode_paths)
        assert result[1].z_offset == 0

    def test_layer_at_start_is_modified(self, server, modify_stub):
        server.settings.start_layer = 5
        paths = [_make_path(INNERWALL), _make_path(INNERWALL)]
        resp = modify_stub.Call(_call_request(paths, layer_nr=5))
        result = list(resp.gcode_paths)
        assert result[1].z_offset == 100

    def test_layer_above_end_passes_through(self, server, modify_stub):
        server.settings.end_layer = 10
        paths = [_make_path(INNERWALL), _make_path(INNERWALL)]
        # end_layer=10 means last modified index is 9; layer 10 is beyond
        resp = modify_stub.Call(_call_request(paths, layer_nr=10))
        result = list(resp.gcode_paths)
        assert result[1].z_offset == 0

    def test_layer_at_last_index_is_modified(self, server, modify_stub):
        server.settings.end_layer = 10
        paths = [_make_path(INNERWALL), _make_path(INNERWALL)]
        resp = modify_stub.Call(_call_request(paths, layer_nr=9))
        result = list(resp.gcode_paths)
        assert result[1].z_offset == 100

    def test_end_layer_minus_one_means_unlimited(self, server, modify_stub):
        server.settings.end_layer = -1
        paths = [_make_path(INNERWALL), _make_path(INNERWALL)]
        resp = modify_stub.Call(_call_request(paths, layer_nr=9999))
        result = list(resp.gcode_paths)
        assert result[1].z_offset == 100

    # --- first / last brick layer multiplier ---

    def test_first_brick_layer_115x_multiplier(self, server, modify_stub):
        server.settings.start_layer = 2
        server.settings.extrusion_multiplier = 1.05
        paths = [_make_path(INNERWALL), _make_path(INNERWALL)]
        resp = modify_stub.Call(_call_request(paths, layer_nr=2))
        result = list(resp.gcode_paths)
        expected = 1.05 * 1.15
        assert abs(result[1].flow_ratio - expected) < 1e-4

    def test_last_brick_layer_085x_multiplier(self, server, modify_stub):
        server.settings.end_layer = 10
        server.settings.extrusion_multiplier = 1.05
        paths = [_make_path(INNERWALL), _make_path(INNERWALL)]
        resp = modify_stub.Call(_call_request(paths, layer_nr=9))  # end_layer - 1
        result = list(resp.gcode_paths)
        expected = 1.05 * 0.85
        assert abs(result[1].flow_ratio - expected) < 1e-4

    def test_middle_layer_uses_base_multiplier(self, server, modify_stub):
        server.settings.start_layer = 2
        server.settings.end_layer = 10
        server.settings.extrusion_multiplier = 1.05
        paths = [_make_path(INNERWALL), _make_path(INNERWALL)]
        resp = modify_stub.Call(_call_request(paths, layer_nr=5))
        result = list(resp.gcode_paths)
        assert abs(result[1].flow_ratio - 1.05) < 1e-4

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
        ]
        resp = modify_stub.Call(_call_request(paths))
        result = list(resp.gcode_paths)
        assert result[1].z_offset == 200  # 400 // 2 from path data

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
        paths = [_make_path(INNERWALL), _make_path(INNERWALL)]
        resp1 = modify_stub.Call(_call_request(paths, layer_nr=5))
        resp2 = modify_stub.Call(_call_request(paths, layer_nr=6))
        r1 = list(resp1.gcode_paths)
        r2 = list(resp2.gcode_paths)
        # Both calls should shift the second wall — counter reset each time
        assert r1[1].z_offset == 100
        assert r2[1].z_offset == 100

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

        # Step 3: Modify paths for layer 3
        mod_stub = modify_pb2_grpc.GCodePathsModifyServiceStub(channel)
        paths = [_make_path(INNERWALL), _make_path(INNERWALL), _make_path(SKIN)]
        resp = mod_stub.Call(_call_request(paths, layer_nr=3))
        result = list(resp.gcode_paths)
        assert result[0].z_offset == 0    # wall 0 (even)
        assert result[1].z_offset == 100  # wall 1 (odd)
        assert result[2].z_offset == 0    # SKIN unchanged
