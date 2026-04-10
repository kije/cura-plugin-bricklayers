from . import BrickLayers
from .BrickLayersEnginePlugin import BrickLayersEnginePlugin


def getMetaData():
    return {}


def register(app):
    return {
        "extension": BrickLayers.BrickLayers(),
        "backend_plugin": BrickLayersEnginePlugin(),
    }
