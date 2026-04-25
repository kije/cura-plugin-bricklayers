r"""Helpers for loading BRICKLAYERS_DUMP_LAYERS dumps as pytest fixtures.

Usage:
    # 1. Run Cura with the plugin, with dumping enabled:
    #    launchctl setenv BRICKLAYERS_DUMP_LAYERS 148,150
    #    open /Applications/Ultimaker\ Cura.app   # quit + relaunch
    #    ... slice a model ...
    #
    # 2. In a pytest test, load the captured input:
    #    from tests.fixture_loader import load_request
    #    req = load_request("layer_148_ext0_in.bin")
    #
    # 3. Feed `req.gcode_paths` into the gRPC modify stub:
    #    resp = modify_stub.Call(req)
    #    # assertions...
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable, Optional

from cura.plugins.slots.gcode_paths.v0 import modify_pb2
from cura.plugins.slots.broadcast.v0 import broadcast_pb2


def _dump_dir(explicit: Optional[Path] = None) -> Path:
    """Resolve the dump directory — explicit path wins, else use env var, else
    the platform default (matches main.rs' resolution order)."""
    if explicit is not None:
        return Path(explicit)
    env_dir = os.environ.get("BRICKLAYERS_DUMP_DIR")
    if env_dir:
        return Path(env_dir)
    home = Path(os.path.expanduser("~"))
    macos_dir = home / "Library" / "Logs" / "BrickLayers"
    if macos_dir.exists():
        return macos_dir
    return Path("/tmp/bricklayers")


def load_request(filename: str, dump_dir: Optional[Path] = None) -> modify_pb2.CallRequest:
    """Load a single `layer_N_extK_in.bin` dump as a `CallRequest` proto.

    Call this once per fixture; the returned object can be passed directly to
    the modify stub's Call() method.
    """
    base = _dump_dir(dump_dir)
    path = base / filename
    if not path.exists():
        raise FileNotFoundError(
            f"dump not found: {path}. Did you run Cura with "
            f"BRICKLAYERS_DUMP_LAYERS set? "
            f"Files present: {sorted(p.name for p in base.glob('*.bin')) if base.exists() else 'dir missing'}"
        )
    req = modify_pb2.CallRequest()
    req.ParseFromString(path.read_bytes())
    return req


def load_response(filename: str, dump_dir: Optional[Path] = None) -> modify_pb2.CallResponse:
    """Load a `layer_N_extK_out.bin` dump as a `CallResponse` proto."""
    base = _dump_dir(dump_dir)
    path = base / filename
    if not path.exists():
        raise FileNotFoundError(f"dump not found: {path}")
    resp = modify_pb2.CallResponse()
    resp.ParseFromString(path.read_bytes())
    return resp


def load_broadcast(dump_dir: Optional[Path] = None) -> broadcast_pb2.BroadcastServiceSettingsRequest:
    """Load the `broadcast_settings.bin` dump as the original settings message.

    Useful for replaying the exact settings context Cura produced (including
    every extruder's overrides, object settings, etc.).
    """
    base = _dump_dir(dump_dir)
    path = base / "broadcast_settings.bin"
    if not path.exists():
        raise FileNotFoundError(f"broadcast dump not found: {path}")
    msg = broadcast_pb2.BroadcastServiceSettingsRequest()
    msg.ParseFromString(path.read_bytes())
    return msg


def list_dumps(dump_dir: Optional[Path] = None) -> list[str]:
    """Return all `.bin` filenames present in the dump directory, sorted."""
    base = _dump_dir(dump_dir)
    if not base.exists():
        return []
    return sorted(p.name for p in base.glob("*.bin"))


def filename_for(layer_nr: int, extruder_nr: int, direction: str = "in") -> str:
    """Construct the canonical dump filename for a (layer, extruder, direction)
    triple matching what the Rust host writes."""
    assert direction in ("in", "out"), f"direction must be 'in' or 'out', got {direction!r}"
    return f"layer_{layer_nr}_ext{extruder_nr}_{direction}.bin"


def iter_layer_dumps(dump_dir: Optional[Path] = None,
                     direction: str = "in") -> Iterable[tuple[int, int, Path]]:
    """Yield (layer_nr, extruder_nr, path) for every dump matching `direction`.

    Ordered by layer_nr, then extruder_nr — convenient for sweep tests that
    replay an entire slice through the plugin.
    """
    base = _dump_dir(dump_dir)
    if not base.exists():
        return
    results = []
    for p in base.glob(f"layer_*_ext*_{direction}.bin"):
        # Expect "layer_NNN_extK_{direction}.bin"
        stem = p.stem  # no extension
        try:
            parts = stem.split("_")
            # layer, NNN, ext+K, direction
            layer_nr = int(parts[1])
            extruder_nr = int(parts[2].removeprefix("ext"))
        except (IndexError, ValueError):
            continue
        results.append((layer_nr, extruder_nr, p))
    results.sort(key=lambda t: (t[0], t[1]))
    yield from results
