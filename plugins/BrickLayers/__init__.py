# Copyright (c) 2024
# BrickLayers plugin is released under the terms of the LGPLv3 or higher.

from PyQt6.QtQml import qmlRegisterSingletonType

from UM.i18n import i18nCatalog
from . import BrickLayers
from . import BrickLayersView, BrickLayersViewProxy

catalog = i18nCatalog("cura")


def getMetaData():
    return {
        "view": {
            "name": catalog.i18nc("@item:inlistbox", "BrickLayers view"),
            "weight": 1
        }
    }


def register(app):
    brick_layers_view = BrickLayersView.BrickLayersView()
    qmlRegisterSingletonType(
        BrickLayersViewProxy.BrickLayersViewProxy,
        "UM", 1, 0,
        brick_layers_view.getProxy,
        "BrickLayersView",
    )
    return {
        "extension": BrickLayers.BrickLayers(),
        "view": brick_layers_view,
    }
