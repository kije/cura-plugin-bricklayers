from collections import OrderedDict
import json
import os
from typing import Dict, List, Any, cast

from UM.Application import Application
from UM.Extension import Extension
from UM.Logger import Logger
from UM.PluginRegistry import PluginRegistry
from UM.Settings.SettingDefinition import SettingDefinition
from UM.Settings.DefinitionContainer import DefinitionContainer
from UM.Settings.ContainerRegistry import ContainerRegistry

from cura.CuraApplication import CuraApplication


class BrickLayers(Extension):

    def __init__(self) -> None:
        super().__init__()

        self._application = Application.getInstance()

        # Load settings definition from JSON
        settings_definition_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "brick_layers_settings.def.json"
        )
        try:
            with open(settings_definition_path, "r", encoding="utf-8") as f:
                self._settings_dict = json.load(
                    f, object_pairs_hook=OrderedDict
                )["settings"]
        except Exception:
            Logger.logException("e", "Could not load brick layers settings definition")
            return

        self._application.getPreferences().addPreference(
            "bricklayers/settings_made_visible", False
        )

        ContainerRegistry.getInstance().containerLoadComplete.connect(
            self._onContainerLoadComplete
        )

        # Save-area indicator (like PostProcessing plugin)
        self._indicator_view = None
        CuraApplication.getInstance().mainWindowChanged.connect(
            self._createIndicator
        )

    # ------------------------------------------------------------------ #
    # Save-area indicator
    # ------------------------------------------------------------------ #

    def _createIndicator(self) -> None:
        """Create the save-area button that shows when BrickLayers is active."""
        if self._indicator_view is not None:
            return  # Already created

        from PyQt6.QtCore import QObject

        plugin_path = PluginRegistry.getInstance().getPluginPath("BrickLayers")
        if plugin_path is None:
            return

        qml_path = os.path.join(
            cast(str, plugin_path), "BrickLayersSaveAreaButton.qml"
        )
        self._indicator_view = CuraApplication.getInstance().createQmlComponent(
            qml_path, {}
        )
        if self._indicator_view is None:
            Logger.log("w", "BrickLayers: Failed to create save-area indicator QML")
            return

        # createQmlComponent returns the root Item itself, which is the button
        button = self._indicator_view.findChild(
            QObject, "brickLayersSaveAreaButton"
        )
        if button is None:
            # Root object IS the button — use it directly
            button = self._indicator_view

        CuraApplication.getInstance().addAdditionalComponent(
            "saveButton", button
        )
        Logger.log("d", "BrickLayers: Save-area indicator registered")

    # ------------------------------------------------------------------ #
    # Settings injection (same pattern as ArcWelder plugin)
    # ------------------------------------------------------------------ #

    def _onContainerLoadComplete(self, container_id: str) -> None:
        if not ContainerRegistry.getInstance().isLoaded(container_id):
            return

        try:
            container = ContainerRegistry.getInstance().findContainers(
                id=container_id
            )[0]
        except IndexError:
            return

        if not isinstance(container, DefinitionContainer):
            return
        if container.getMetaDataEntry("type") == "extruder":
            return

        # Add settings under the "experimental" category
        try:
            category = container.findDefinitions(key="experimental")[0]
        except IndexError:
            Logger.log("e", "BrickLayers: Could not find 'experimental' category")
            return

        for setting_key in self._settings_dict.keys():
            # Skip if this setting already exists in the container
            if setting_key in container._definition_cache:
                continue

            setting_definition = SettingDefinition(
                setting_key, container, category, None
            )
            setting_definition.deserialize(self._settings_dict[setting_key])

            category._children.append(setting_definition)
            container._definition_cache[setting_key] = setting_definition

            self._expanded_categories = self._application.expandedCategories.copy()
            self._updateAddedChildren(container, setting_definition)
            self._application.setExpandedCategories(self._expanded_categories)
            self._expanded_categories.clear()
            container._updateRelations(setting_definition)

        # Make settings visible in the sidebar (only once)
        preferences = self._application.getPreferences()
        if not preferences.getValue("bricklayers/settings_made_visible"):
            setting_keys = self._getAllSettingKeys(self._settings_dict)

            visible_settings = preferences.getValue("general/visible_settings")
            visible_settings_changed = False
            for key in setting_keys:
                if key not in visible_settings:
                    visible_settings += ";%s" % key
                    visible_settings_changed = True

            if visible_settings_changed:
                preferences.setValue("general/visible_settings", visible_settings)

            preferences.setValue("bricklayers/settings_made_visible", True)

    def _updateAddedChildren(
        self, container: DefinitionContainer, setting_definition: SettingDefinition
    ) -> None:
        children = setting_definition.children
        if not children or not setting_definition.parent:
            return

        if setting_definition.parent.key in self._expanded_categories:
            self._expanded_categories.append(setting_definition.key)

        for child in children:
            container._definition_cache[child.key] = child
            self._updateAddedChildren(container, child)

    def _getAllSettingKeys(self, definition: Dict[str, Any]) -> List[str]:
        children = []
        for key in definition:
            children.append(key)
            if "children" in definition[key]:
                children.extend(self._getAllSettingKeys(definition[key]["children"]))
        return children
