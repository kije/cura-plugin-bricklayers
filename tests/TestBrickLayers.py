import os
import sys
import unittest
from unittest.mock import MagicMock

# Add the plugin root and proto directories to the path
_plugin_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _plugin_dir)
sys.path.insert(0, os.path.join(_plugin_dir, "proto"))

from cura.plugins.v0 import printfeatures_pb2, gcode_path_pb2, point3d_pb2, slot_id_pb2
from cura.plugins.v0 import polygons_pb2
from cura.plugins.slots.handshake.v0 import handshake_pb2
from cura.plugins.slots.broadcast.v0 import broadcast_pb2
from cura.plugins.slots.gcode_paths.v0 import modify_pb2

from engine_prototype import (
    BrickSettings,
    HandshakeServicer,
    BroadcastServicer,
    GCodePathsModifyServicer,
)

INNERWALL = printfeatures_pb2.INNERWALL
OUTERWALL = printfeatures_pb2.OUTERWALL
SKIN = printfeatures_pb2.SKIN
INFILL = printfeatures_pb2.INFILL
SUPPORT = printfeatures_pb2.SUPPORT


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_path(feature, n_points=3, z_offset=0, layer_thickness=200,
               flow_ratio=1.0, line_width=400):
    """Build a GCodePath protobuf for testing."""
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


def _make_call_request(paths, layer_nr=5, extruder_nr=0):
    """Build a GCodePathsModify CallRequest."""
    return modify_pb2.CallRequest(
        gcode_paths=paths,
        layer_nr=layer_nr,
        extruder_nr=extruder_nr,
    )


def _make_servicer(**settings_overrides):
    """Create a GCodePathsModifyServicer with configured BrickSettings."""
    defaults = dict(
        enabled=True, start_layer=0, end_layer=-1,
        apply_inner_walls=True, apply_outer_walls=False,
        extrusion_multiplier=1.05, layer_height=200,
    )
    defaults.update(settings_overrides)
    settings = BrickSettings(**defaults)
    return GCodePathsModifyServicer(settings), settings


def _mock_context():
    ctx = MagicMock()
    ctx.send_initial_metadata = MagicMock()
    return ctx


# ---------------------------------------------------------------------------
# Handshake
# ---------------------------------------------------------------------------

class TestHandshake(unittest.TestCase):

    def test_returns_plugin_name_and_version(self):
        servicer = HandshakeServicer()
        req = handshake_pb2.CallRequest(
            slot_id=slot_id_pb2.GCODE_PATHS_MODIFY,
            version="0.1.0",
            plugin_name="CuraEngine",
            plugin_version="5.0",
        )
        resp = servicer.Call(req, _mock_context())
        self.assertEqual(resp.plugin_name, "BrickLayers")
        self.assertEqual(resp.plugin_version, "1.0.0")
        self.assertTrue(len(resp.slot_version_range) > 0)

    def test_subscribes_to_settings_broadcast(self):
        servicer = HandshakeServicer()
        req = handshake_pb2.CallRequest()
        resp = servicer.Call(req, _mock_context())
        self.assertIn(
            slot_id_pb2.SETTINGS_BROADCAST,
            resp.broadcast_subscriptions,
        )


# ---------------------------------------------------------------------------
# Broadcast Settings
# ---------------------------------------------------------------------------

class TestBroadcastSettings(unittest.TestCase):

    def _broadcast(self, settings, settings_dict):
        """Send a settings broadcast with the given key-value pairs."""
        servicer = BroadcastServicer(settings)
        global_settings = broadcast_pb2.Settings(
            settings={k: v.encode() for k, v in settings_dict.items()}
        )
        req = broadcast_pb2.BroadcastServiceSettingsRequest(
            global_settings=global_settings,
        )
        servicer.BroadcastSettings(req, _mock_context())

    def test_parses_brick_layers_enabled(self):
        s = BrickSettings()
        self.assertFalse(s.enabled)
        self._broadcast(s, {"brick_layers_enabled": "true"})
        self.assertTrue(s.enabled)

    def test_parses_start_end_layer(self):
        s = BrickSettings()
        self._broadcast(s, {
            "brick_layers_start_layer": "5",
            "brick_layers_end_layer": "20",
        })
        self.assertEqual(s.start_layer, 4)  # 1-indexed → 0-indexed
        self.assertEqual(s.end_layer, 20)

    def test_parses_wall_type_flags(self):
        s = BrickSettings()
        self._broadcast(s, {
            "brick_layers_apply_inner_walls": "false",
            "brick_layers_apply_outer_walls": "true",
        })
        self.assertFalse(s.apply_inner_walls)
        self.assertTrue(s.apply_outer_walls)

    def test_parses_extrusion_multiplier(self):
        s = BrickSettings()
        self._broadcast(s, {"brick_layers_extrusion_multiplier": "1.10"})
        self.assertAlmostEqual(s.extrusion_multiplier, 1.10)

    def test_parses_layer_height_mm_to_microns(self):
        s = BrickSettings()
        self._broadcast(s, {"layer_height": "0.2"})
        self.assertEqual(s.layer_height, 200)  # 0.2mm = 200 microns

    def test_missing_settings_use_defaults(self):
        s = BrickSettings()
        self._broadcast(s, {"unrelated_setting": "value"})
        self.assertFalse(s.enabled)
        self.assertEqual(s.start_layer, 2)
        self.assertTrue(s.apply_inner_walls)

    def test_multiple_broadcasts_update_settings(self):
        s = BrickSettings()
        self._broadcast(s, {"brick_layers_enabled": "true"})
        self.assertTrue(s.enabled)
        self._broadcast(s, {"brick_layers_enabled": "false"})
        self.assertFalse(s.enabled)


# ---------------------------------------------------------------------------
# Core Brick Pattern Algorithm
# ---------------------------------------------------------------------------

class TestBrickPattern(unittest.TestCase):

    def _call(self, servicer, paths, layer_nr=5, extruder_nr=0):
        req = _make_call_request(paths, layer_nr, extruder_nr)
        resp = servicer.Call(req, _mock_context())
        return list(resp.gcode_paths)

    def test_alternating_walls_shifted(self):
        servicer, _ = _make_servicer()
        paths = [_make_path(INNERWALL) for _ in range(4)]
        result = self._call(servicer, paths)
        # Even indices (0, 2) unchanged, odd (1, 3) shifted
        self.assertEqual(result[0].z_offset, 0)
        self.assertEqual(result[1].z_offset, 100)  # 200/2
        self.assertEqual(result[2].z_offset, 0)
        self.assertEqual(result[3].z_offset, 100)

    def test_even_walls_unchanged(self):
        servicer, _ = _make_servicer()
        paths = [_make_path(INNERWALL) for _ in range(4)]
        result = self._call(servicer, paths)
        self.assertAlmostEqual(result[0].flow_ratio, 1.0)
        self.assertAlmostEqual(result[2].flow_ratio, 1.0)

    def test_z_offset_equals_half_layer_thickness(self):
        servicer, _ = _make_servicer(layer_height=300)
        paths = [_make_path(INNERWALL), _make_path(INNERWALL)]
        result = self._call(servicer, paths)
        self.assertEqual(result[1].z_offset, 150)  # 300/2

    def test_flow_ratio_multiplied(self):
        servicer, _ = _make_servicer(extrusion_multiplier=1.10)
        paths = [_make_path(INNERWALL), _make_path(INNERWALL)]
        result = self._call(servicer, paths)
        self.assertAlmostEqual(result[0].flow_ratio, 1.0)
        self.assertAlmostEqual(result[1].flow_ratio, 1.10)

    def test_non_wall_paths_unchanged(self):
        servicer, _ = _make_servicer()
        paths = [
            _make_path(SKIN),
            _make_path(INFILL),
            _make_path(SUPPORT),
        ]
        result = self._call(servicer, paths)
        for i, p in enumerate(result):
            self.assertEqual(p.z_offset, 0, f"Path {i} ({p.feature}) should be unchanged")
            self.assertAlmostEqual(p.flow_ratio, 1.0)

    def test_inner_walls_only(self):
        servicer, _ = _make_servicer(apply_inner_walls=True, apply_outer_walls=False)
        paths = [
            _make_path(OUTERWALL),
            _make_path(OUTERWALL),
            _make_path(INNERWALL),
            _make_path(INNERWALL),
        ]
        result = self._call(servicer, paths)
        # Outer walls untouched
        self.assertEqual(result[0].z_offset, 0)
        self.assertEqual(result[1].z_offset, 0)
        # Inner walls: index 0 (even) unchanged, index 1 (odd) shifted
        self.assertEqual(result[2].z_offset, 0)
        self.assertEqual(result[3].z_offset, 100)

    def test_outer_walls_only(self):
        servicer, _ = _make_servicer(apply_inner_walls=False, apply_outer_walls=True)
        paths = [
            _make_path(OUTERWALL),
            _make_path(OUTERWALL),
            _make_path(INNERWALL),
            _make_path(INNERWALL),
        ]
        result = self._call(servicer, paths)
        self.assertEqual(result[0].z_offset, 0)
        self.assertEqual(result[1].z_offset, 100)
        self.assertEqual(result[2].z_offset, 0)
        self.assertEqual(result[3].z_offset, 0)

    def test_both_wall_types(self):
        servicer, _ = _make_servicer(apply_inner_walls=True, apply_outer_walls=True)
        paths = [
            _make_path(OUTERWALL),
            _make_path(INNERWALL),
            _make_path(OUTERWALL),
            _make_path(INNERWALL),
        ]
        result = self._call(servicer, paths)
        # All walls share one counter: 0=even, 1=odd, 2=even, 3=odd
        self.assertEqual(result[0].z_offset, 0)
        self.assertEqual(result[1].z_offset, 100)
        self.assertEqual(result[2].z_offset, 0)
        self.assertEqual(result[3].z_offset, 100)

    def test_no_wall_types_selected_noop(self):
        servicer, _ = _make_servicer(apply_inner_walls=False, apply_outer_walls=False)
        paths = [_make_path(INNERWALL), _make_path(OUTERWALL)]
        result = self._call(servicer, paths)
        for p in result:
            self.assertEqual(p.z_offset, 0)

    def test_single_wall_path(self):
        servicer, _ = _make_servicer()
        paths = [_make_path(INNERWALL)]
        result = self._call(servicer, paths)
        # Index 0 (even) — not shifted
        self.assertEqual(result[0].z_offset, 0)

    def test_mixed_path_types_preserve_order(self):
        servicer, _ = _make_servicer()
        paths = [
            _make_path(SKIN),
            _make_path(INNERWALL),
            _make_path(INFILL),
            _make_path(INNERWALL),
            _make_path(SUPPORT),
        ]
        result = self._call(servicer, paths)
        self.assertEqual(len(result), 5)
        self.assertEqual(result[0].feature, SKIN)
        self.assertEqual(result[1].feature, INNERWALL)
        self.assertEqual(result[2].feature, INFILL)
        self.assertEqual(result[3].feature, INNERWALL)
        self.assertEqual(result[4].feature, SUPPORT)
        # Wall counter: first INNERWALL=0 (even), second=1 (odd, shifted)
        self.assertEqual(result[1].z_offset, 0)
        self.assertEqual(result[3].z_offset, 100)


# ---------------------------------------------------------------------------
# Layer Range Filtering
# ---------------------------------------------------------------------------

class TestLayerRange(unittest.TestCase):

    def _call(self, servicer, layer_nr):
        paths = [_make_path(INNERWALL), _make_path(INNERWALL)]
        req = _make_call_request(paths, layer_nr=layer_nr)
        resp = servicer.Call(req, _mock_context())
        return list(resp.gcode_paths)

    def test_below_start_layer_passthrough(self):
        servicer, _ = _make_servicer(start_layer=5)
        result = self._call(servicer, layer_nr=3)
        self.assertEqual(result[1].z_offset, 0)

    def test_above_end_layer_passthrough(self):
        servicer, _ = _make_servicer(end_layer=10)
        result = self._call(servicer, layer_nr=10)  # end_layer-1 = 9 is last
        self.assertEqual(result[1].z_offset, 0)

    def test_within_range_modified(self):
        servicer, _ = _make_servicer(start_layer=2, end_layer=10)
        result = self._call(servicer, layer_nr=5)
        self.assertEqual(result[1].z_offset, 100)

    def test_end_layer_minus_one_means_all(self):
        servicer, _ = _make_servicer(end_layer=-1)
        result = self._call(servicer, layer_nr=999)
        self.assertEqual(result[1].z_offset, 100)

    def test_start_layer_one_indexed_to_zero_indexed(self):
        """Cura setting 'start_layer=3' means 0-indexed layer 2."""
        s = BrickSettings()
        servicer = BroadcastServicer(s)
        req = broadcast_pb2.BroadcastServiceSettingsRequest(
            global_settings=broadcast_pb2.Settings(
                settings={"brick_layers_start_layer": b"3"}
            ),
        )
        servicer.BroadcastSettings(req, _mock_context())
        self.assertEqual(s.start_layer, 2)


# ---------------------------------------------------------------------------
# First / Last Brick Layer Multiplier
# ---------------------------------------------------------------------------

class TestFirstLastLayer(unittest.TestCase):

    def _call(self, servicer, layer_nr):
        paths = [_make_path(INNERWALL), _make_path(INNERWALL)]
        req = _make_call_request(paths, layer_nr=layer_nr)
        resp = servicer.Call(req, _mock_context())
        return list(resp.gcode_paths)

    def test_first_brick_layer_115x_multiplier(self):
        servicer, _ = _make_servicer(start_layer=2, extrusion_multiplier=1.05)
        result = self._call(servicer, layer_nr=2)
        expected = 1.05 * 1.15
        self.assertAlmostEqual(result[1].flow_ratio, expected, places=4)

    def test_last_brick_layer_085x_multiplier(self):
        servicer, _ = _make_servicer(end_layer=10, extrusion_multiplier=1.05)
        result = self._call(servicer, layer_nr=9)  # end_layer - 1
        expected = 1.05 * 0.85
        self.assertAlmostEqual(result[1].flow_ratio, expected, places=4)

    def test_middle_layer_normal_multiplier(self):
        servicer, _ = _make_servicer(start_layer=2, end_layer=10, extrusion_multiplier=1.05)
        result = self._call(servicer, layer_nr=5)
        self.assertAlmostEqual(result[1].flow_ratio, 1.05, places=4)


# ---------------------------------------------------------------------------
# Disabled Plugin
# ---------------------------------------------------------------------------

class TestDisabled(unittest.TestCase):

    def test_disabled_returns_unmodified(self):
        servicer, _ = _make_servicer(enabled=False)
        paths = [_make_path(INNERWALL), _make_path(INNERWALL)]
        req = _make_call_request(paths)
        resp = servicer.Call(req, _mock_context())
        result = list(resp.gcode_paths)
        self.assertEqual(result[0].z_offset, 0)
        self.assertEqual(result[1].z_offset, 0)

    def test_enabled_modifies_paths(self):
        servicer, _ = _make_servicer(enabled=True)
        paths = [_make_path(INNERWALL), _make_path(INNERWALL)]
        req = _make_call_request(paths)
        resp = servicer.Call(req, _mock_context())
        result = list(resp.gcode_paths)
        self.assertEqual(result[1].z_offset, 100)


# ---------------------------------------------------------------------------
# Layer Thickness Fallback
# ---------------------------------------------------------------------------

class TestLayerThicknessFallback(unittest.TestCase):

    def _call(self, servicer, paths):
        req = _make_call_request(paths)
        resp = servicer.Call(req, _mock_context())
        return list(resp.gcode_paths)

    def test_from_settings_broadcast(self):
        servicer, _ = _make_servicer(layer_height=300)
        paths = [_make_path(INNERWALL, layer_thickness=0),
                 _make_path(INNERWALL, layer_thickness=0)]
        result = self._call(servicer, paths)
        self.assertEqual(result[1].z_offset, 150)  # 300/2 from settings

    def test_fallback_to_path_data(self):
        servicer, _ = _make_servicer(layer_height=0)
        paths = [_make_path(INNERWALL, layer_thickness=400),
                 _make_path(INNERWALL, layer_thickness=400)]
        result = self._call(servicer, paths)
        self.assertEqual(result[1].z_offset, 200)  # 400/2 from path

    def test_zero_thickness_skips(self):
        servicer, _ = _make_servicer(layer_height=0)
        paths = [_make_path(INNERWALL, layer_thickness=0),
                 _make_path(INNERWALL, layer_thickness=0)]
        result = self._call(servicer, paths)
        # No modification when thickness unknown
        self.assertEqual(result[1].z_offset, 0)


# ---------------------------------------------------------------------------
# Multi-Extruder Behavior
# ---------------------------------------------------------------------------

class TestMultiExtruder(unittest.TestCase):

    def test_only_target_feature_paths_modified(self):
        """Paths with non-wall features (from any extruder) are untouched."""
        servicer, _ = _make_servicer()
        paths = [
            _make_path(INNERWALL),
            _make_path(SUPPORT),
            _make_path(INNERWALL),
        ]
        req = _make_call_request(paths, extruder_nr=0)
        resp = servicer.Call(req, _mock_context())
        result = list(resp.gcode_paths)
        self.assertEqual(result[0].z_offset, 0)  # wall 0 (even)
        self.assertEqual(result[1].z_offset, 0)  # support untouched
        self.assertEqual(result[2].z_offset, 100)  # wall 1 (odd)


# ---------------------------------------------------------------------------
# Multiple Layer Calls
# ---------------------------------------------------------------------------

class TestMultipleLayerCalls(unittest.TestCase):

    def test_wall_counter_resets_per_layer(self):
        """Each Call() (new layer) starts with a fresh wall counter."""
        servicer, _ = _make_servicer()
        paths = [_make_path(INNERWALL), _make_path(INNERWALL)]

        # Layer 5
        req1 = _make_call_request(paths, layer_nr=5)
        resp1 = servicer.Call(req1, _mock_context())
        r1 = list(resp1.gcode_paths)
        self.assertEqual(r1[0].z_offset, 0)
        self.assertEqual(r1[1].z_offset, 100)

        # Layer 6 — counter should reset
        req2 = _make_call_request(paths, layer_nr=6)
        resp2 = servicer.Call(req2, _mock_context())
        r2 = list(resp2.gcode_paths)
        self.assertEqual(r2[0].z_offset, 0)
        self.assertEqual(r2[1].z_offset, 100)

    def test_settings_persist_across_calls(self):
        """Settings from broadcast persist for all subsequent layer calls."""
        settings = BrickSettings(enabled=True, layer_height=200,
                                 apply_inner_walls=True)
        servicer = GCodePathsModifyServicer(settings)

        # First call works
        paths1 = [_make_path(INNERWALL), _make_path(INNERWALL)]
        req1 = _make_call_request(paths1, layer_nr=5)
        resp1 = servicer.Call(req1, _mock_context())
        r1 = list(resp1.gcode_paths)
        self.assertEqual(r1[1].z_offset, 100)

        # Change settings
        settings.enabled = False

        # Second call with fresh paths reflects changed settings
        paths2 = [_make_path(INNERWALL), _make_path(INNERWALL)]
        req2 = _make_call_request(paths2, layer_nr=6)
        resp2 = servicer.Call(req2, _mock_context())
        r2 = list(resp2.gcode_paths)
        self.assertEqual(r2[1].z_offset, 0)


# ---------------------------------------------------------------------------
# Existing z_offset is preserved (additive)
# ---------------------------------------------------------------------------

class TestExistingZOffset(unittest.TestCase):

    def test_z_offset_is_additive(self):
        """If a path already has z_offset, the shift is added to it."""
        servicer, _ = _make_servicer(layer_height=200)
        paths = [
            _make_path(INNERWALL, z_offset=50),
            _make_path(INNERWALL, z_offset=50),
        ]
        req = _make_call_request(paths)
        resp = servicer.Call(req, _mock_context())
        result = list(resp.gcode_paths)
        self.assertEqual(result[0].z_offset, 50)   # even, unchanged
        self.assertEqual(result[1].z_offset, 150)  # 50 + 100


if __name__ == "__main__":
    unittest.main()
