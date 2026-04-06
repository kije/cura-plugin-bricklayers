# Copyright (c) 2024
# BrickLayers plugin is released under the terms of the LGPLv3 or higher.
#
# QML bridge for BrickLayersView — registered as singleton UM.BrickLayersView.
# Adapted from SimulationViewProxy.

from typing import TYPE_CHECKING

from PyQt6.QtCore import QObject, pyqtSignal, pyqtProperty
from UM.FlameProfiler import pyqtSlot
from UM.Application import Application

if TYPE_CHECKING:
    from .BrickLayersView import BrickLayersView


class BrickLayersViewProxy(QObject):
    def __init__(self, view: "BrickLayersView", parent=None) -> None:
        super().__init__(parent)
        self._view = view
        self._controller = Application.getInstance().getController()
        self._controller.activeViewChanged.connect(self._onActiveViewChanged)
        self._connected = False
        self._onActiveViewChanged()

    currentLayerChanged = pyqtSignal()
    currentPathChanged = pyqtSignal()
    maxLayersChanged = pyqtSignal()
    maxPathsChanged = pyqtSignal()
    activityChanged = pyqtSignal()
    globalStackChanged = pyqtSignal()
    preferencesChanged = pyqtSignal()
    busyChanged = pyqtSignal()
    colorSchemeLimitsChanged = pyqtSignal()

    # ---- Properties ----

    @pyqtProperty(bool, notify=activityChanged)
    def layerActivity(self):
        return self._view.getActivity()

    @pyqtProperty(int, notify=maxLayersChanged)
    def numLayers(self):
        return self._view.getMaxLayers()

    @pyqtProperty(int, notify=currentLayerChanged)
    def currentLayer(self):
        return self._view.getCurrentLayer()

    @pyqtProperty(int, notify=currentLayerChanged)
    def minimumLayer(self):
        return self._view.getMinimumLayer()

    @pyqtProperty(float, notify=currentLayerChanged)
    def currentLayerHeight(self):
        return self._view.getCurrentLayerHeight()

    @pyqtProperty(float, notify=currentLayerChanged)
    def minimumLayerHeight(self):
        return self._view.getMinimumLayerHeight()

    @pyqtProperty(int, notify=maxPathsChanged)
    def numPaths(self):
        return self._view.getMaxPaths()

    @pyqtProperty(float, notify=currentPathChanged)
    def currentPath(self):
        return self._view.getCurrentPath()

    @pyqtProperty(int, notify=currentPathChanged)
    def minimumPath(self):
        return self._view.getMinimumPath()

    @pyqtProperty(bool, notify=busyChanged)
    def busy(self):
        return self._view.isBusy()

    @pyqtProperty(bool, notify=preferencesChanged)
    def compatibilityMode(self):
        return self._view.getCompatibilityMode()

    @pyqtProperty(int, notify=globalStackChanged)
    def extruderCount(self):
        return self._view.getExtruderCount()

    @pyqtProperty(float, notify=colorSchemeLimitsChanged)
    def minFeedrate(self):
        return self._view.getMinFeedrate()

    @pyqtProperty(float, notify=colorSchemeLimitsChanged)
    def maxFeedrate(self):
        return self._view.getMaxFeedrate()

    @pyqtProperty(float, notify=colorSchemeLimitsChanged)
    def minThickness(self):
        return self._view.getMinThickness()

    @pyqtProperty(float, notify=colorSchemeLimitsChanged)
    def maxThickness(self):
        return self._view.getMaxThickness()

    @pyqtProperty(float, notify=colorSchemeLimitsChanged)
    def maxLineWidth(self):
        return self._view.getMaxLineWidth()

    @pyqtProperty(float, notify=colorSchemeLimitsChanged)
    def minLineWidth(self):
        return self._view.getMinLineWidth()

    @pyqtProperty(float, notify=colorSchemeLimitsChanged)
    def maxFlowRate(self):
        return self._view.getMaxFlowRate()

    @pyqtProperty(float, notify=colorSchemeLimitsChanged)
    def minFlowRate(self):
        return self._view.getMinFlowRate()

    # ---- Slots ----

    @pyqtSlot(float)
    def advanceTime(self, duration: float) -> None:
        self._view.advanceTime(duration)

    @pyqtSlot(int)
    def setCurrentLayer(self, layer_num):
        self._view.setLayer(layer_num)

    @pyqtSlot(int)
    def setMinimumLayer(self, layer_num):
        self._view.setMinimumLayer(layer_num)

    @pyqtSlot(float)
    def setCurrentPath(self, path_num: float):
        self._view.setPath(path_num)

    @pyqtSlot(int)
    def setMinimumPath(self, path_num):
        self._view.setMinimumPath(path_num)

    @pyqtSlot(int)
    def setSimulationViewType(self, layer_view_type):
        self._view.setSimulationViewType(layer_view_type)

    @pyqtSlot(result=int)
    def getSimulationViewType(self):
        return self._view.getSimulationViewType()

    @pyqtSlot(bool)
    def setSimulationRunning(self, running):
        self._view.setSimulationRunning(running)

    @pyqtSlot(result=bool)
    def getSimulationRunning(self):
        return self._view.isSimulationRunning()

    @pyqtSlot(int, float)
    def setExtruderOpacity(self, extruder_nr, opacity):
        self._view.setExtruderOpacity(extruder_nr, opacity)

    @pyqtSlot(int)
    def setShowTravelMoves(self, show):
        self._view.setShowTravelMoves(show)

    @pyqtSlot(int)
    def setShowHelpers(self, show):
        self._view.setShowHelpers(show)

    @pyqtSlot(int)
    def setShowSkin(self, show):
        self._view.setShowSkin(show)

    @pyqtSlot(int)
    def setShowInfill(self, show):
        self._view.setShowInfill(show)

    # ---- Signal forwarding ----

    def _onLayerChanged(self):
        self.currentLayerChanged.emit()
        self.activityChanged.emit()

    def _onColorSchemeLimitsChanged(self):
        self.colorSchemeLimitsChanged.emit()

    def _onPathChanged(self):
        self.currentPathChanged.emit()
        self.activityChanged.emit()
        scene = Application.getInstance().getController().getScene()
        scene.sceneChanged.emit(scene.getRoot())

    def _onMaxLayersChanged(self):
        self.maxLayersChanged.emit()

    def _onMaxPathsChanged(self):
        self.maxPathsChanged.emit()

    def _onBusyChanged(self):
        self.busyChanged.emit()

    def _onActivityChanged(self):
        self.activityChanged.emit()

    def _onGlobalStackChanged(self):
        self.globalStackChanged.emit()

    def _onPreferencesChanged(self):
        self.preferencesChanged.emit()

    def _onActiveViewChanged(self):
        active_view = self._controller.getActiveView()
        if active_view == self._view:
            self._view.currentLayerNumChanged.connect(self._onLayerChanged)
            self._view.colorSchemeLimitsChanged.connect(self._onColorSchemeLimitsChanged)
            self._view.currentPathNumChanged.connect(self._onPathChanged)
            self._view.maxLayersChanged.connect(self._onMaxLayersChanged)
            self._view.maxPathsChanged.connect(self._onMaxPathsChanged)
            self._view.busyChanged.connect(self._onBusyChanged)
            self._view.activityChanged.connect(self._onActivityChanged)
            self._view.globalStackChanged.connect(self._onGlobalStackChanged)
            self._view.preferencesChanged.connect(self._onPreferencesChanged)
            self._connected = True
        elif self._connected:
            self._connected = False
            self._view.currentLayerNumChanged.disconnect(self._onLayerChanged)
            self._view.colorSchemeLimitsChanged.disconnect(self._onColorSchemeLimitsChanged)
            self._view.currentPathNumChanged.disconnect(self._onPathChanged)
            self._view.maxLayersChanged.disconnect(self._onMaxLayersChanged)
            self._view.maxPathsChanged.disconnect(self._onMaxPathsChanged)
            self._view.busyChanged.disconnect(self._onBusyChanged)
            self._view.activityChanged.disconnect(self._onActivityChanged)
            self._view.globalStackChanged.disconnect(self._onGlobalStackChanged)
            self._view.preferencesChanged.disconnect(self._onPreferencesChanged)
