import QtQuick 2.4
import QtQuick.Controls 2.3

import UM 1.5 as UM
import Cura 1.0 as Cura

Item
{
    id: brickLayersSaveAreaButton
    objectName: "brickLayersSaveAreaButton"

    visible: brickLayersEnabledProvider.properties.value === "True"
    height: UM.Theme.getSize("action_button").height
    width: visible ? height : 0

    UM.SettingPropertyProvider
    {
        id: brickLayersEnabledProvider
        containerStack: Cura.MachineManager.activeMachine
        key: "brick_layers_enabled"
        watchedProperties: [ "value" ]
        storeIndex: 0
    }

    Cura.SecondaryButton
    {
        height: UM.Theme.getSize("action_button").height
        tooltip:
        {
            var tipText = "<b>BrickLayers post-processing is active.</b>";
            tipText += "<br><br>";
            tipText += "Alternating wall loops will be shifted up by half a layer height ";
            tipText += "to create interlocking brick-like walls.";
            return tipText;
        }
        toolTipContentAlignment: UM.Enums.ContentAlignment.AlignLeft
        iconSource: UM.Theme.getIcon("PrintWalls")
        fixedWidthMode: false
    }

    Cura.NotificationIcon
    {
        id: brickLayersNotificationIcon
        visible: brickLayersSaveAreaButton.visible
        anchors
        {
            horizontalCenter: parent.right
            verticalCenter: parent.top
        }
        labelText: "B"
    }
}