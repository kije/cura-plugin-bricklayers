import QtQuick 2.4
import QtQuick.Controls 2.3

import UM 1.5 as UM
import Cura 1.0 as Cura

Item
{
    id: brickLayersSaveAreaButton
    objectName: "brickLayersSaveAreaButton"

    visible: brickLayersEnabled
    height: UM.Theme.getSize("action_button").height
    width: visible ? height : 0

    property bool brickLayersEnabled:
    {
        var globalStack = Cura.MachineManager.activeMachine;
        if (globalStack === null) return false;
        return globalStack.getProperty("brick_layers_enabled", "value") === true;
    }

    Connections
    {
        target: Cura.MachineManager
        function onActiveMachineChanged() { brickLayersEnabled = Qt.binding(function() {
            var globalStack = Cura.MachineManager.activeMachine;
            if (globalStack === null) return false;
            return globalStack.getProperty("brick_layers_enabled", "value") === true;
        })}
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
            tipText += "<br><br>";
            tipText += "<i>Note: The layer preview shows the original slicer output. ";
            tipText += "The BrickLayers transformation is applied when saving the G-code file.</i>";
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
