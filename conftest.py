# Mock UM/cura framework modules before pytest discovers the BrickLayers package.
# Only mocks the specific framework modules, NOT the 'cura' namespace itself
# (which is also used by proto stubs: cura.plugins.v0, cura.plugins.slots...).
import importlib.util
import os
import sys
from unittest.mock import MagicMock

# Add src/ and src/proto/ to path so proto stubs and engine_prototype can be imported
_REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
_SRC_DIR = os.path.join(_REPO_ROOT, "src")
_PROTO_DIR = os.path.join(_SRC_DIR, "proto")
for _p in (_SRC_DIR, _PROTO_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

_MOCK_MODULES = [
    "UM",
    "UM.Application",
    "UM.Extension",
    "UM.Logger",
    "UM.PluginRegistry",
    "UM.Platform",
    "UM.Backend",
    "UM.Backend.Backend",
    "UM.Settings",
    "UM.Settings.SettingDefinition",
    "UM.Settings.DefinitionContainer",
    "UM.Settings.ContainerRegistry",
    "UM.i18n",
    "PyQt6",
    "PyQt6.QtCore",
    # Don't mock 'cura' itself — proto stubs use cura.plugins.v0
    "cura.CuraApplication",
    "cura.BackendPlugin",
]

if importlib.util.find_spec("UM") is None:
    for mod_name in _MOCK_MODULES:
        if mod_name not in sys.modules:
            sys.modules[mod_name] = MagicMock()

    sys.modules["UM.Extension"].Extension = type(
        "Extension", (), {"__init__": lambda self: None}
    )
    sys.modules["cura.BackendPlugin"].BackendPlugin = type(
        "BackendPlugin", (), {"__init__": lambda self: None}
    )
