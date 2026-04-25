"""Docker E2E test: real CuraEngine slice → BrickLayers gRPC → G-code validation.

Exercises the full chain:
  1. Generate two separate 15mm cube STL files at known positions (pure Python)
  2. Copy them into the running Cura Docker container
  3. Run CuraEngine headlessly to slice both cubes
  4. Parse the output G-code into GCodePath protobuf sequences
  5. Send the GCodePaths through the BrickLayers gRPC plugin
  6. Validate: spatial bounds, ordering, gap-fill, GCodeLinter

Requires:
  - Colima running with DOCKER_HOST set
  - ``docker compose up -d`` (container: cura-bricklayers-test)

Skip condition: tests are skipped gracefully when Docker or the container is
unavailable, so they never fail in CI without Docker.

Run:
    DOCKER_HOST=unix://$HOME/.config/colima/default/docker.sock \\
      python -m pytest tests/test_e2e_docker_slice.py -v -m e2e
"""

import os
import socket
import subprocess
import sys
import time
from concurrent import futures
from typing import Optional

import grpc
import pytest

# ---------------------------------------------------------------------------
# Path bootstrap
# ---------------------------------------------------------------------------
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC_DIR = os.path.join(_REPO_ROOT, "src")
_PROTO_DIR = os.path.join(_SRC_DIR, "proto")
for _p in (_SRC_DIR, _PROTO_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from cura.plugins.v0 import gcode_path_pb2, printfeatures_pb2
from cura.plugins.slots.gcode_paths.v0 import modify_pb2, modify_pb2_grpc
from cura.plugins.slots.handshake.v0 import handshake_pb2_grpc
from cura.plugins.slots.broadcast.v0 import broadcast_pb2_grpc

from engine_prototype import (
    BrickSettings,
    BroadcastServicer,
    GCodePathsModifyServicer,
    HandshakeServicer,
)

from e2e_helpers import (
    parse_curaengine_gcode,
    emit_gcode,
    generate_cube_stl_bytes,
    INNERWALL,
    OUTERWALL,
    MOVERETRACTED,
    LAYER_HEIGHT_UM,
    Z_SHIFT_UM,
    WALL_W,
    _MOVE_FEATURES,
)

from gcode_lint import GCodeLinter, GCodeDialect, Severity

# ---------------------------------------------------------------------------
# Docker configuration
# ---------------------------------------------------------------------------
DOCKER_HOST = os.environ.get(
    "DOCKER_HOST",
    f"unix://{os.environ.get('HOME', '/root')}/.config/colima/default/docker.sock",
)
CONTAINER = "cura-bricklayers-test"

# Cube geometry (mm)
CUBE_SIDE_MM = 15.0
CUBE_GAP_MM = 10.0
CUBE_A_X_OFFSET = 0.0
CUBE_B_X_OFFSET = CUBE_SIDE_MM + CUBE_GAP_MM  # 25.0 mm


# ---------------------------------------------------------------------------
# Docker helpers
# ---------------------------------------------------------------------------

def _docker_env():
    """Return os.environ with DOCKER_HOST set."""
    env = dict(os.environ)
    env["DOCKER_HOST"] = DOCKER_HOST
    return env


def _docker_available() -> bool:
    try:
        r = subprocess.run(
            ["docker", "--version"],
            capture_output=True,
            env=_docker_env(),
            timeout=5,
        )
        return r.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _container_running() -> bool:
    try:
        r = subprocess.run(
            ["docker", "inspect", "--format={{.State.Running}}", CONTAINER],
            capture_output=True,
            env=_docker_env(),
            timeout=10,
        )
        return r.returncode == 0 and r.stdout.strip() == b"true"
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _docker_exec_args(*args, timeout=60) -> subprocess.CompletedProcess:
    """Run a command inside the container."""
    return subprocess.run(
        ["docker", "exec", CONTAINER, *args],
        capture_output=True,
        env=_docker_env(),
        timeout=timeout,
    )


def _docker_cp_to(local_path: str, container_path: str):
    """Copy a local file into the container."""
    subprocess.run(
        ["docker", "cp", local_path, f"{CONTAINER}:{container_path}"],
        check=True,
        capture_output=True,
        env=_docker_env(),
        timeout=15,
    )


def _docker_cp_from(container_path: str, local_path: str):
    """Copy a file from the container to local."""
    subprocess.run(
        ["docker", "cp", f"{CONTAINER}:{container_path}", local_path],
        check=True,
        capture_output=True,
        env=_docker_env(),
        timeout=15,
    )


def _find_in_container(name: str, search_dir: str = "/app") -> Optional[str]:
    """Locate a file by name inside the container."""
    r = _docker_exec_args("find", search_dir, "-name", name, "-type", "f", timeout=30)
    if r.returncode != 0:
        return None
    paths = r.stdout.decode().strip().splitlines()
    return paths[0] if paths else None


# ---------------------------------------------------------------------------
# gRPC helpers (lightweight — just enough to process paths through BrickLayers)
# ---------------------------------------------------------------------------

def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _call_request(paths, layer_nr=5, extruder_nr=0):
    return modify_pb2.CallRequest(
        gcode_paths=paths,
        layer_nr=layer_nr,
        extruder_nr=extruder_nr,
    )


class _GrpcContext:
    """Manages a BrickLayers gRPC server for the duration of a test session."""

    def __init__(self):
        self.settings = BrickSettings(
            enabled=True,
            start_layer=0,
            end_layer=-1,
            apply_inner_walls=True,
            apply_outer_walls=False,
            extrusion_multiplier=1.05,
            layer_height=200,
        )
        self.settings.inset_direction = "outside_in"
        self._port = _free_port()
        self._address = f"127.0.0.1:{self._port}"
        self._server = None
        self._channel = None

    def start(self):
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
        self._server.add_insecure_port(self._address)
        self._server.start()
        # Wait for port
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", self._port), timeout=0.1):
                    break
            except OSError:
                time.sleep(0.05)
        self._channel = grpc.insecure_channel(self._address)

    def stop(self):
        if self._channel:
            self._channel.close()
        if self._server:
            self._server.stop(grace=0)

    @property
    def modify_stub(self):
        return modify_pb2_grpc.GCodePathsModifyServiceStub(self._channel)


# ---------------------------------------------------------------------------
# pytest markers & prerequisites
# ---------------------------------------------------------------------------

pytestmark = pytest.mark.e2e


@pytest.fixture(scope="module")
def docker_env():
    """Skip the entire module if Docker or the container is unavailable."""
    if not _docker_available():
        pytest.skip("Docker not available")
    if not _container_running():
        pytest.skip(
            f"Container {CONTAINER!r} not running — "
            "start with: docker compose up -d"
        )
    curaengine = _find_in_container("CuraEngine")
    if not curaengine:
        pytest.skip("CuraEngine binary not found in container")
    printer_def = _find_in_container("fdmprinter.def.json")
    if not printer_def:
        pytest.skip("fdmprinter.def.json not found in container")
    return {"curaengine": curaengine, "printer_def": printer_def}


@pytest.fixture(scope="module")
def raw_gcode(docker_env, tmp_path_factory):
    """Slice two 15 mm cubes with CuraEngine and return the raw G-code text."""
    tmp = tmp_path_factory.mktemp("e2e_docker")

    # Generate STL files
    stl_a = tmp / "cube_a.stl"
    stl_b = tmp / "cube_b.stl"
    stl_a.write_bytes(generate_cube_stl_bytes(CUBE_SIDE_MM, x_offset_mm=CUBE_A_X_OFFSET))
    stl_b.write_bytes(generate_cube_stl_bytes(CUBE_SIDE_MM, x_offset_mm=CUBE_B_X_OFFSET))

    # Copy into container
    _docker_cp_to(str(stl_a), "/tmp/cube_a.stl")
    _docker_cp_to(str(stl_b), "/tmp/cube_b.stl")

    # Invoke CuraEngine
    ce = docker_env["curaengine"]
    pd = docker_env["printer_def"]
    r = _docker_exec_args(
        ce, "slice",
        "-j", pd,
        "-s", "layer_height=0.2",
        "-s", "wall_line_count=6",
        "-s", "infill_sparse_density=0",
        "-s", "top_layers=0",
        "-s", "bottom_layers=0",
        "-l", "/tmp/cube_a.stl",
        "-l", "/tmp/cube_b.stl",
        "-o", "/tmp/e2e_output.gcode",
        timeout=120,
    )
    if r.returncode != 0:
        stderr = r.stderr.decode(errors="replace")[:2000]
        pytest.fail(f"CuraEngine slice failed (rc={r.returncode}):\n{stderr}")

    # Retrieve output
    local_gcode = tmp / "e2e_output.gcode"
    _docker_cp_from("/tmp/e2e_output.gcode", str(local_gcode))
    return local_gcode.read_text(encoding="utf-8", errors="replace")


@pytest.fixture(scope="module")
def parsed_paths(raw_gcode):
    """Parse the raw CuraEngine G-code into GCodePath protobuf objects."""
    paths = parse_curaengine_gcode(raw_gcode)
    assert len(paths) > 0, "G-code parser returned no paths"
    return paths


@pytest.fixture(scope="module")
def grpc_ctx():
    """Module-scoped BrickLayers gRPC server."""
    ctx = _GrpcContext()
    ctx.start()
    yield ctx
    ctx.stop()


@pytest.fixture(scope="module")
def modified_result(parsed_paths, grpc_ctx):
    """Send parsed CuraEngine GCodePaths through BrickLayers and return the result."""
    stub = grpc_ctx.modify_stub
    resp = stub.Call(_call_request(parsed_paths))
    return list(resp.gcode_paths)


@pytest.fixture(scope="module")
def model_bounds(raw_gcode):
    """Parse the actual model XY bounds from the raw CuraEngine G-code.

    Returns dict with 'cube_a' and 'cube_b' sub-dicts, each containing
    'x_min', 'x_max', 'y_min', 'y_max' in microns and 'mesh_name'.
    """
    paths = parse_curaengine_gcode(raw_gcode)
    bounds = {}
    for p in paths:
        if p.feature not in (OUTERWALL, INNERWALL):
            continue
        mesh = p.mesh_name
        if not mesh or mesh == "NONMESH":
            continue
        if mesh not in bounds:
            bounds[mesh] = {
                "x_min": float("inf"),
                "x_max": float("-inf"),
                "y_min": float("inf"),
                "y_max": float("-inf"),
            }
        for pt in p.path.path:
            b = bounds[mesh]
            b["x_min"] = min(b["x_min"], pt.x)
            b["x_max"] = max(b["x_max"], pt.x)
            b["y_min"] = min(b["y_min"], pt.y)
            b["y_max"] = max(b["y_max"], pt.y)

    if len(bounds) < 2:
        pytest.skip(
            f"Expected 2 mesh regions in CuraEngine output, got {len(bounds)}: "
            f"{list(bounds.keys())}"
        )
    # Sort by x_min to identify cube A (left) and cube B (right)
    sorted_meshes = sorted(bounds.items(), key=lambda kv: kv[1]["x_min"])
    return {
        "cube_a": {"mesh_name": sorted_meshes[0][0], **sorted_meshes[0][1]},
        "cube_b": {"mesh_name": sorted_meshes[1][0], **sorted_meshes[1][1]},
    }


# ===========================================================================
# TESTS
# ===========================================================================


class TestDockerTwoModelE2ESlice:
    """Full E2E: CuraEngine slices two 15 mm cubes -> BrickLayers modifies
    paths -> validate spatial/ordering correctness of the output."""

    def test_gcode_linter_baseline_passes(self, raw_gcode):
        """The raw CuraEngine output (before BrickLayers) must be valid G-code."""
        linter = GCodeLinter(dialect=GCodeDialect.AUTO)
        errors = linter.lint(raw_gcode)
        critical = [e for e in errors if e.severity == Severity.ERROR]
        assert critical == [], (
            f"CuraEngine baseline G-code has {len(critical)} errors: "
            + "; ".join(str(e) for e in critical[:5])
        )

    def test_wall_paths_preserved(self, parsed_paths, modified_result):
        """BrickLayers must not drop any INNERWALL or OUTERWALL paths."""
        in_inner = sum(1 for p in parsed_paths if p.feature == INNERWALL)
        in_outer = sum(1 for p in parsed_paths if p.feature == OUTERWALL)
        out_inner = sum(1 for p in modified_result if p.feature == INNERWALL)
        out_outer = sum(1 for p in modified_result if p.feature == OUTERWALL)

        assert out_inner == in_inner, (
            f"INNERWALL count changed: {in_inner} -> {out_inner}"
        )
        assert out_outer == in_outer, (
            f"OUTERWALL count changed: {in_outer} -> {out_outer}"
        )

    def test_walls_shifted(self, modified_result, model_bounds):
        """At least 2 inner walls per mesh are shifted to z_offset > 0."""
        shifted = [
            p for p in modified_result
            if p.z_offset > 0 and p.feature == INNERWALL
        ]
        assert len(shifted) >= 4, (
            f"Expected >=4 shifted inner walls, got {len(shifted)}"
        )
        # Each mesh must contribute at least 2
        a_mesh = model_bounds["cube_a"]["mesh_name"]
        b_mesh = model_bounds["cube_b"]["mesh_name"]
        a_shifted = [p for p in shifted if p.mesh_name == a_mesh]
        b_shifted = [p for p in shifted if p.mesh_name == b_mesh]
        assert len(a_shifted) >= 2, (
            f"Cube A ({a_mesh!r}): expected >=2 shifted walls, got {len(a_shifted)}"
        )
        assert len(b_shifted) >= 2, (
            f"Cube B ({b_mesh!r}): expected >=2 shifted walls, got {len(b_shifted)}"
        )
        # z_offset must be exactly half the layer height
        for p in shifted:
            assert p.z_offset == Z_SHIFT_UM, (
                f"Shifted wall z_offset={p.z_offset}, expected {Z_SHIFT_UM}"
            )

    def test_no_extrusion_outside_model_bounds(self, modified_result, model_bounds):
        """Every INNERWALL/OUTERWALL point must fall within one of the two
        model footprints.  Uses detected bounds with wall-width tolerance."""
        tol = WALL_W  # 400 um tolerance for wall thickness
        a = model_bounds["cube_a"]
        b = model_bounds["cube_b"]

        for p in modified_result:
            if p.feature not in (INNERWALL, OUTERWALL):
                continue
            for pt in p.path.path:
                in_a = (
                    a["x_min"] - tol <= pt.x <= a["x_max"] + tol
                    and a["y_min"] - tol <= pt.y <= a["y_max"] + tol
                )
                in_b = (
                    b["x_min"] - tol <= pt.x <= b["x_max"] + tol
                    and b["y_min"] - tol <= pt.y <= b["y_max"] + tol
                )
                assert in_a or in_b, (
                    f"Wall point ({pt.x}, {pt.y}) is outside both model bounds "
                    f"(A: x=[{a['x_min']},{a['x_max']}] y=[{a['y_min']},{a['y_max']}], "
                    f"B: x=[{b['x_min']},{b['x_max']}] y=[{b['y_min']},{b['y_max']}], "
                    f"tolerance={tol})"
                )

    def test_no_extrusion_in_inter_model_gap(self, modified_result, model_bounds):
        """No INNERWALL or OUTERWALL point may fall in the gap between the cubes."""
        a_xmax = model_bounds["cube_a"]["x_max"]
        b_xmin = model_bounds["cube_b"]["x_min"]
        gap_lo = a_xmax + 1
        gap_hi = b_xmin - 1

        if gap_lo >= gap_hi:
            pytest.skip("No detectable gap between the two models")

        for p in modified_result:
            if p.feature not in (INNERWALL, OUTERWALL):
                continue
            for pt in p.path.path:
                assert not (gap_lo <= pt.x <= gap_hi), (
                    f"Wall point ({pt.x}, {pt.y}) is in the inter-model gap "
                    f"[{gap_lo}, {gap_hi}]"
                )

    def test_all_gap_crossing_paths_are_travel(self, modified_result, model_bounds):
        """Every path whose points span both model x-regions must be MOVERETRACTED."""
        a_xmax = model_bounds["cube_a"]["x_max"]
        b_xmin = model_bounds["cube_b"]["x_min"]

        for p in modified_result:
            if not p.path or not p.path.path:
                continue
            xs = [pt.x for pt in p.path.path]
            spans = any(x <= a_xmax for x in xs) and any(x >= b_xmin for x in xs)
            if spans:
                assert p.feature in _MOVE_FEATURES, (
                    f"Non-travel path spans inter-model gap: "
                    f"feature={p.feature} mesh={p.mesh_name!r} "
                    f"x_range=[{min(xs)}, {max(xs)}]"
                )

    def test_normal_z_precedes_shifted_z(self, modified_result):
        """Global partition: all normal-Z wall paths before any shifted-Z wall path."""
        wall_features = (INNERWALL, OUTERWALL)
        normal_indices = [
            i for i, p in enumerate(modified_result)
            if p.z_offset == 0 and p.feature in wall_features
        ]
        shifted_indices = [
            i for i, p in enumerate(modified_result)
            if p.z_offset > 0 and p.feature in wall_features
        ]
        if not normal_indices or not shifted_indices:
            pytest.skip("Missing normal-Z or shifted-Z walls in result")
        assert max(normal_indices) < min(shifted_indices), (
            f"Last normal-Z wall at index {max(normal_indices)} is not before "
            f"first shifted wall at index {min(shifted_indices)}"
        )

    def test_output_gcode_linter_passes(self, modified_result):
        """The BrickLayers-modified output, emitted as G-code, must pass the linter."""
        gcode = emit_gcode(modified_result, layer_z_mm=0.2)
        linter = GCodeLinter(dialect=GCodeDialect.MARLIN)
        errors = linter.lint(gcode)
        critical = [e for e in errors if e.severity == Severity.ERROR]
        assert critical == [], (
            f"Modified G-code has {len(critical)} errors: "
            + "; ".join(str(e) for e in critical[:5])
        )

    def test_synthesized_travels_have_correct_endpoints(self, modified_result):
        """Synthesized travels (MOVERETRACTED with z_offset>0 and retract=True)
        must start where the previous path ends and end where the next starts."""
        travels = [
            (i, p)
            for i, p in enumerate(modified_result)
            if p.feature == MOVERETRACTED and p.z_offset > 0 and p.retract
        ]
        for idx, travel in travels:
            if idx == 0 or idx == len(modified_result) - 1:
                continue
            prev_path = modified_result[idx - 1]
            next_path = modified_result[idx + 1]
            if not prev_path.path or not prev_path.path.path:
                continue
            if not next_path.path or not next_path.path.path:
                continue

            prev_end = prev_path.path.path[-1]
            next_start = next_path.path.path[0]
            travel_start = travel.path.path[0]
            travel_end = travel.path.path[-1]

            assert travel_start.x == prev_end.x and travel_start.y == prev_end.y, (
                f"Travel at index {idx} start ({travel_start.x},{travel_start.y}) != "
                f"prev end ({prev_end.x},{prev_end.y})"
            )
            assert travel_end.x == next_start.x and travel_end.y == next_start.y, (
                f"Travel at index {idx} end ({travel_end.x},{travel_end.y}) != "
                f"next start ({next_start.x},{next_start.y})"
            )
