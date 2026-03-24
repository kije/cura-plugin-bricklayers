# Copyright (c) 2024
# BrickLayers plugin is released under the terms of the LGPLv3 or higher.

from . import BrickLayers


def getMetaData():
    return {}


def register(app):
    return {"extension": BrickLayers.BrickLayers()}
