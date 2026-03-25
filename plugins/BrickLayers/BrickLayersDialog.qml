import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.1

import UM 1.5 as UM
import Cura 1.1 as Cura

UM.Dialog
{
    id: dialog
    title: "Brick Layers Settings"
    width: 450 * screenScaleFactor
    height: 420 * screenScaleFactor
    minimumWidth: 350 * screenScaleFactor
    minimumHeight: 350 * screenScaleFactor
    backgroundColor: UM.Theme.getColor("main_background")

    UM.I18nCatalog { id: catalog; name: "cura" }

    Item
    {
        anchors.fill: parent
        anchors.margins: UM.Theme.getSize("default_margin").width

        ColumnLayout
        {
            anchors.fill: parent
            spacing: UM.Theme.getSize("default_margin").height

            // Enable/Disable toggle
            RowLayout
            {
                Layout.fillWidth: true
                spacing: UM.Theme.getSize("default_margin").width

                UM.Label
                {
                    text: "Enable Brick Layers"
                    font: UM.Theme.getFont("medium_bold")
                    Layout.fillWidth: true
                }

                Cura.CheckBox
                {
                    id: enabledCheckBox
                    checked: manager.enabled
                    onClicked: manager.setEnabled(checked)
                }
            }

            // Separator
            Rectangle
            {
                Layout.fillWidth: true
                height: UM.Theme.getSize("default_lining").height
                color: UM.Theme.getColor("lining")
            }

            // Layer Height
            RowLayout
            {
                Layout.fillWidth: true
                enabled: enabledCheckBox.checked
                opacity: enabled ? 1.0 : 0.5

                UM.Label
                {
                    text: "Layer Height"
                    Layout.fillWidth: true
                }

                Cura.TextField
                {
                    id: layerHeightField
                    Layout.preferredWidth: 80 * screenScaleFactor
                    text: manager.layerHeight.toFixed(2)
                    validator: DoubleValidator { bottom: 0.04; top: 1.0; decimals: 3 }
                    onEditingFinished: manager.setLayerHeight(parseFloat(text))
                }

                UM.Label { text: "mm" }
            }

            // Extrusion Multiplier
            RowLayout
            {
                Layout.fillWidth: true
                enabled: enabledCheckBox.checked
                opacity: enabled ? 1.0 : 0.5

                UM.Label
                {
                    text: "Extrusion Multiplier"
                    Layout.fillWidth: true
                }

                Cura.TextField
                {
                    id: extrusionMultiplierField
                    Layout.preferredWidth: 80 * screenScaleFactor
                    text: manager.extrusionMultiplier.toFixed(2)
                    validator: DoubleValidator { bottom: 0.5; top: 2.0; decimals: 3 }
                    onEditingFinished: manager.setExtrusionMultiplier(parseFloat(text))
                }
            }

            // Start Layer
            RowLayout
            {
                Layout.fillWidth: true
                enabled: enabledCheckBox.checked
                opacity: enabled ? 1.0 : 0.5

                UM.Label
                {
                    text: "Start Layer"
                    Layout.fillWidth: true
                }

                Cura.TextField
                {
                    id: startLayerField
                    Layout.preferredWidth: 80 * screenScaleFactor
                    text: manager.startLayer
                    validator: IntValidator { bottom: 1 }
                    onEditingFinished: manager.setStartLayer(parseInt(text))
                }
            }

            // End Layer
            RowLayout
            {
                Layout.fillWidth: true
                enabled: enabledCheckBox.checked
                opacity: enabled ? 1.0 : 0.5

                UM.Label
                {
                    text: "End Layer (-1 = all)"
                    Layout.fillWidth: true
                }

                Cura.TextField
                {
                    id: endLayerField
                    Layout.preferredWidth: 80 * screenScaleFactor
                    text: manager.endLayer
                    validator: IntValidator { bottom: -1 }
                    onEditingFinished: manager.setEndLayer(parseInt(text))
                }
            }

            // Separator
            Rectangle
            {
                Layout.fillWidth: true
                height: UM.Theme.getSize("default_lining").height
                color: UM.Theme.getColor("lining")
            }

            // Wall type selection
            UM.Label
            {
                text: "Apply to:"
                font: UM.Theme.getFont("medium_bold")
                enabled: enabledCheckBox.checked
                opacity: enabled ? 1.0 : 0.5
            }

            RowLayout
            {
                Layout.fillWidth: true
                enabled: enabledCheckBox.checked
                opacity: enabled ? 1.0 : 0.5
                spacing: UM.Theme.getSize("default_margin").width

                Cura.CheckBox
                {
                    id: innerWallsCheckBox
                    text: "Inner Walls"
                    checked: manager.applyToInnerWalls
                    onClicked: manager.setApplyToInnerWalls(checked)
                }

                Cura.CheckBox
                {
                    id: outerWallsCheckBox
                    text: "Outer Walls"
                    checked: manager.applyToOuterWalls
                    onClicked: manager.setApplyToOuterWalls(checked)
                }
            }

            // Warning for outer walls
            UM.Label
            {
                Layout.fillWidth: true
                visible: outerWallsCheckBox.checked
                text: "⚠ Outer walls: affects dimensional accuracy and surface finish."
                color: UM.Theme.getColor("warning")
                wrapMode: Text.WordWrap
                font: UM.Theme.getFont("default_italic")
            }

            // Status indicator
            UM.Label
            {
                Layout.fillWidth: true
                Layout.alignment: Qt.AlignBottom
                text: enabledCheckBox.checked ? "✓ Brick Layers will be applied when slicing" : "Brick Layers is disabled"
                color: enabledCheckBox.checked ? UM.Theme.getColor("success") : UM.Theme.getColor("text_inactive")
                font: UM.Theme.getFont("default_italic")
            }

            // Spacer
            Item { Layout.fillHeight: true }
        }
    }

    rightButtons:
    [
        Cura.PrimaryButton
        {
            text: "Close"
            onClicked: dialog.accept()
        }
    ]
}
