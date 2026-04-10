# Mock all UM/cura modules before pytest discovers the BrickLayers package.
# This allows the test module to import BrickLayers.py without needing
# the full Cura/Uranium framework installed.
import importlib.util
import sys
from unittest.mock import MagicMock

_MOCK_MODULES = [
    "UM",
    "UM.Application",
    "UM.Extension",
    "UM.Logger",
    "UM.PluginRegistry",
    "UM.Settings",
    "UM.Settings.SettingDefinition",
    "UM.Settings.DefinitionContainer",
    "UM.Settings.ContainerRegistry",
    "UM.i18n",
    "cura",
    "cura.CuraApplication",
]

# Only mock if the real modules are NOT available
if importlib.util.find_spec("UM") is None:
    for mod_name in _MOCK_MODULES:
        if mod_name not in sys.modules:
            sys.modules[mod_name] = MagicMock()

    # Make Extension a real base class so BrickLayers can inherit from it
    sys.modules["UM.Extension"].Extension = type(
        "Extension", (), {"__init__": lambda self: None}
    )
