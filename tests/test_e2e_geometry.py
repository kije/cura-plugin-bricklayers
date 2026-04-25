"""Geometry-grounded E2E tests for the BrickLayers engine plugin.

Uses exact M8_two_cubes.stl dimensions to verify the algorithm's behaviour
against known geometric constraints:
  - Cube A: x=[0, 15000] um, y=[0, 15000] um
  - Cube B: x=[25000, 40000] um, y=[0, 15000] um
  - Gap:    x=[15001, 24999] um (10 mm)
  - 5 inner walls per cube to trigger Phase 3b gap-fill synthesis

All gRPC server fixtures are duplicated here (not imported) to keep this
module fully self-contained.
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
_TESTS_DIR = os.path.join(_REPO_ROOT, "tests")
for _p in (_SRC_DIR, _PROTO_DIR, _TESTS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# ---------------------------------------------------------------------------
# Proto / stub imports — must come after path bootstrap
# ---------------------------------------------------------------------------
from cura.plugins.slots.handshake.v0 import handshake_pb2_grpc
from cura.plugins.slots.broadcast.v0 import broadcast_pb2_grpc
from cura.plugins.slots.gcode_paths.v0 import modify_pb2, modify_pb2_grpc

from engine_prototype import (
    BrickSettings,
    BroadcastServicer,
    GCodePathsModifyServicer,
    HandshakeServicer,
)

# ---------------------------------------------------------------------------
# E2E geometry helpers
# ---------------------------------------------------------------------------
from e2e_helpers import (
    build_two_cube_paths,
    emit_gcode,
    INNERWALL,
    OUTERWALL,
    MOVERETRACTED,
    A_XMIN,
    A_XMAX,
    B_XMIN,
    B_XMAX,
    YMIN,
    YMAX,
    Z_SHIFT_UM,
    _MOVE_FEATURES,
)

from gcode_lint import GCodeLinter, GCodeDialect, Severity


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _call_request(paths, layer_nr: int = 5, extruder_nr: int = 0):
    return modify_pb2.CallRequest(
        gcode_paths=paths, layer_nr=layer_nr, extruder_nr=extruder_nr,
    )


# ---------------------------------------------------------------------------
# Server fixture — session-scoped, one server for all tests
# ---------------------------------------------------------------------------

class _ServerBundle:
    def __init__(self, address: str, settings: BrickSettings):
        self.address = address
        self.settings = settings
        self._server: Optional[grpc.Server] = None

    def start(self) -> None:
        self._server = grpc.server(futures.ThreadPoolExecutor(max_workers=2))
        handshake_pb2_grpc.add_HandshakeServiceServicer_to_server(
            HandshakeServicer(), self._server)
        broadcast_pb2_grpc.add_BroadcastServiceServicer_to_server(
            BroadcastServicer(self.settings), self._server)
        modify_pb2_grpc.add_GCodePathsModifyServiceServicer_to_server(
            GCodePathsModifyServicer(self.settings), self._server)
        self._server.add_insecure_port(self.address)
        self._server.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.stop(grace=0)


@pytest.fixture(scope="session")
def grpc_server():
    port = _free_port()
    address = f"127.0.0.1:{port}"
    settings = BrickSettings(
        enabled=True, start_layer=0, end_layer=-1,
        apply_inner_walls=True, apply_outer_walls=False,
        extrusion_multiplier=1.05, layer_height=200,
    )
    bundle = _ServerBundle(address, settings)
    bundle.start()
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
    s = grpc_server.settings
    s.enabled = True
    s.start_layer = 0
    s.end_layer = -1
    s.apply_inner_walls = True
    s.apply_outer_walls = False
    s.extrusion_multiplier = 1.05
    s.layer_height = 200
    s.inset_direction = "outside_in"
    # Default 2 mm is safe for the two-cube geometry (inner walls are 0.4 mm
    # apart within each cube; meshes are split by mesh_name in Phase 1, not
    # distance, so the inter-cube gap never triggers a sub-group break).
    s.contour_break_distance = 2000
    return grpc_server


@pytest.fixture
def channel(grpc_server):
    ch = grpc.insecure_channel(grpc_server.address)
    yield ch
    ch.close()


@pytest.fixture
def modify_stub(channel):
    return modify_pb2_grpc.GCodePathsModifyServiceStub(channel)


# ===========================================================================
# GEOMETRY-GROUNDED E2E TESTS (M8_two_cubes dimensions)
# ===========================================================================

class TestTwoModelGeometryE2E:
    """Geometry-grounded E2E tests using M8_two_cubes.stl exact dimensions.

    Scene:
      - Cube A: x=[0, 15000] um, y=[0, 15000] um
      - Cube B: x=[25000, 40000] um, y=[0, 15000] um
      - Gap:    x=[15001, 24999] um (10 mm)
      - 5 inner walls per cube trigger Phase 3b gap-fill synthesis.

    With outside_in ordering and n_inner=5, the algorithm:
      - Protects the innermost wall of each group (index 4)
      - Shifts walls at counter%2==0 (indices 0 and 2 -> 2 shifted per cube)
      - Synthesizes 1 MOVERETRACTED gap-fill travel per mesh
    """

    def test_wall_count_preserved(self, server, modify_stub):
        """Result must contain exactly 10 INNERWALL and 2 OUTERWALL paths."""
        paths = build_two_cube_paths(n_inner=5)
        result = list(modify_stub.Call(_call_request(paths)).gcode_paths)

        inner_count = sum(1 for p in result if p.feature == INNERWALL)
        outer_count = sum(1 for p in result if p.feature == OUTERWALL)
        assert inner_count == 10, f"Expected 10 INNERWALLs, got {inner_count}"
        assert outer_count == 2, f"Expected 2 OUTERWALLs, got {outer_count}"

    def test_exact_shift_count(self, server, modify_stub):
        """Exactly 4 INNERWALL paths must have z_offset == Z_SHIFT_UM (2 per cube)."""
        paths = build_two_cube_paths(n_inner=5)
        result = list(modify_stub.Call(_call_request(paths)).gcode_paths)

        shifted = [p for p in result if p.feature == INNERWALL and p.z_offset == Z_SHIFT_UM]
        assert len(shifted) == 4, (
            f"Expected 4 shifted inner walls (z_offset={Z_SHIFT_UM}), got {len(shifted)}"
        )

    def test_normal_z_precedes_shifted_z(self, server, modify_stub):
        """Per-mesh: within each cube's inner-wall sequence, all normal-Z
        inner walls precede all shifted-Z inner walls.

        Under per-sub-group inline emission, cube A's shifted walls are
        emitted inline at cube A's anchor, before cube B's normal-Z walls
        — so the global ordering no longer holds. Within each cube,
        though, the brick pattern still produces the same monotonic
        inner-wall order.
        """
        paths = build_two_cube_paths(n_inner=5)
        result = list(modify_stub.Call(_call_request(paths)).gcode_paths)

        for mesh in ("A", "B"):
            normal_indices = [
                i for i, p in enumerate(result)
                if p.feature == INNERWALL and p.mesh_name == mesh and p.z_offset == 0
            ]
            shifted_indices = [
                i for i, p in enumerate(result)
                if p.feature == INNERWALL and p.mesh_name == mesh
                and p.z_offset == Z_SHIFT_UM
            ]
            assert normal_indices and shifted_indices, (
                f"Mesh {mesh} missing normal or shifted walls"
            )
            assert max(normal_indices) < min(shifted_indices), (
                f"Mesh {mesh}: last normal-Z inner wall at position "
                f"{max(normal_indices)}, first shifted at {min(shifted_indices)}"
            )

    def test_gap_fill_synthesized_per_model(self, server, modify_stub):
        """Exactly 4 synthesised MOVERETRACTED(z_offset=0, retract=True), 2 per mesh:
        1 Case-A gap-fill between non-adjacent normal-Z inner walls
        (counter 1 → counter 3) and 1 bridge-DOWN from each mesh's shifted
        run back to that mesh's anchor at layer Z.
        """
        paths = build_two_cube_paths(n_inner=5)
        result = list(modify_stub.Call(_call_request(paths)).gcode_paths)

        synthesized = [
            p for p in result
            if p.feature == MOVERETRACTED and p.retract and p.z_offset == 0
        ]
        assert len(synthesized) == 4, (
            f"Expected 4 synthesised z=0 retracted travels "
            f"(2 gap-fill + 2 bridge-DOWN), got {len(synthesized)}"
        )
        mesh_names = {p.mesh_name for p in synthesized}
        assert "A" in mesh_names and "B" in mesh_names, (
            f"Expected synthesised travels for both A and B, got meshes={mesh_names}"
        )

    def test_no_extrusion_outside_cube_footprints(self, server, modify_stub):
        """Every INNERWALL/OUTERWALL point must be within Cube A or Cube B bounds."""
        paths = build_two_cube_paths(n_inner=5)
        result = list(modify_stub.Call(_call_request(paths)).gcode_paths)

        for p in result:
            if p.feature not in (INNERWALL, OUTERWALL):
                continue
            for pt in p.path.path:
                in_a = A_XMIN <= pt.x <= A_XMAX
                in_b = B_XMIN <= pt.x <= B_XMAX
                assert in_a or in_b, (
                    f"Extrusion point x={pt.x} outside both cube footprints "
                    f"(A:[{A_XMIN},{A_XMAX}], B:[{B_XMIN},{B_XMAX}])"
                )
                assert YMIN <= pt.y <= YMAX, (
                    f"Extrusion point y={pt.y} outside y-bounds [{YMIN},{YMAX}]"
                )

    def test_no_extrusion_in_inter_cube_gap(self, server, modify_stub):
        """No INNERWALL/OUTERWALL point may have x in the gap (A_XMAX, B_XMIN)."""
        paths = build_two_cube_paths(n_inner=5)
        result = list(modify_stub.Call(_call_request(paths)).gcode_paths)

        for p in result:
            if p.feature not in (INNERWALL, OUTERWALL):
                continue
            for pt in p.path.path:
                assert not (A_XMAX < pt.x < B_XMIN), (
                    f"Extrusion point x={pt.x} in inter-cube gap "
                    f"({A_XMAX} < x < {B_XMIN})"
                )

    def test_gap_crossing_paths_are_travel(self, server, modify_stub):
        """Any path whose points span both model x-regions must be MOVERETRACTED."""
        paths = build_two_cube_paths(n_inner=5)
        result = list(modify_stub.Call(_call_request(paths)).gcode_paths)

        for p in result:
            xs = [pt.x for pt in p.path.path]
            if not xs:
                continue
            touches_a = any(x <= A_XMAX for x in xs)
            touches_b = any(x >= B_XMIN for x in xs)
            if touches_a and touches_b:
                assert p.feature in _MOVE_FEATURES, (
                    f"Path feature={p.feature} spans both models but is not travel"
                )

    def test_shifted_z_ordering_a_before_b(self, server, modify_stub):
        """All shifted walls for Cube A must appear before those for Cube B."""
        paths = build_two_cube_paths(n_inner=5)
        result = list(modify_stub.Call(_call_request(paths)).gcode_paths)

        a_shifted = [
            i for i, p in enumerate(result)
            if p.feature == INNERWALL and p.z_offset == Z_SHIFT_UM and p.mesh_name == "A"
        ]
        b_shifted = [
            i for i, p in enumerate(result)
            if p.feature == INNERWALL and p.z_offset == Z_SHIFT_UM and p.mesh_name == "B"
        ]
        assert a_shifted and b_shifted
        assert max(a_shifted) < min(b_shifted), (
            f"A shifted walls (last={max(a_shifted)}) should precede "
            f"B shifted walls (first={min(b_shifted)})"
        )

    def test_normal_z_ordering_within_models(self, server, modify_stub):
        """All Cube A normal-Z inner walls must precede Cube B normal-Z inner walls."""
        paths = build_two_cube_paths(n_inner=5)
        result = list(modify_stub.Call(_call_request(paths)).gcode_paths)

        a_normal = [
            i for i, p in enumerate(result)
            if p.feature == INNERWALL and p.z_offset == 0 and p.mesh_name == "A"
        ]
        b_normal = [
            i for i, p in enumerate(result)
            if p.feature == INNERWALL and p.z_offset == 0 and p.mesh_name == "B"
        ]
        assert a_normal and b_normal
        assert max(a_normal) < min(b_normal), (
            f"A normal-Z walls (last={max(a_normal)}) should precede "
            f"B normal-Z walls (first={min(b_normal)})"
        )
        assert a_normal == sorted(a_normal), "Cube A normal-Z wall indices not monotonic"
        assert b_normal == sorted(b_normal), "Cube B normal-Z wall indices not monotonic"

    def test_bridge_travel_correctness(self, server, modify_stub):
        """Path before first shifted wall must be MOVERETRACTED(retract=True, z_offset>0)
        with its last point matching the shifted wall's first point."""
        paths = build_two_cube_paths(n_inner=5)
        result = list(modify_stub.Call(_call_request(paths)).gcode_paths)

        first_shifted_idx = next(
            (i for i, p in enumerate(result)
             if p.feature == INNERWALL and p.z_offset == Z_SHIFT_UM),
            None,
        )
        assert first_shifted_idx is not None and first_shifted_idx > 0

        bridge = result[first_shifted_idx - 1]
        assert bridge.feature in _MOVE_FEATURES
        assert bridge.retract, "Bridge travel must have retract=True"
        assert bridge.z_offset > 0, f"Bridge travel z_offset={bridge.z_offset}, expected >0"

        bridge_last = bridge.path.path[-1]
        shifted_first = result[first_shifted_idx].path.path[0]
        assert bridge_last.x == shifted_first.x and bridge_last.y == shifted_first.y, (
            f"Bridge end ({bridge_last.x},{bridge_last.y}) != "
            f"shifted start ({shifted_first.x},{shifted_first.y})"
        )

    def test_gcode_linter_passes(self, server, modify_stub):
        """emit_gcode(result) must pass GCodeLinter with no ERROR severity issues."""
        paths = build_two_cube_paths(n_inner=5)
        result = list(modify_stub.Call(_call_request(paths)).gcode_paths)

        gcode = emit_gcode(result)
        linter = GCodeLinter(dialect=GCodeDialect.MARLIN)
        issues = linter.lint(gcode)

        errors = [i for i in issues if i.severity == Severity.ERROR]
        assert not errors, (
            f"GCodeLinter reported {len(errors)} ERROR(s):\n"
            + "\n".join(f"  Line {e.line}: {e.message}" for e in errors)
        )
