"""TF LabControl component kit (graphite theme).

Every page builds from these pieces instead of styling widgets locally:

- ``base``: status ``Dot`` and ``Pill``, ``IconButton``, ``IconTile``, ``ElideLabel``, ``Separator``,
  sidebar ``NavButton`` / ``SubNavButton``, ``repolish`` / ``set_property``.
- ``controls``: ``button`` / ``style_button`` variants (primary, danger, ghost, link; sizes sm/lg),
  ``Switch`` (a QCheckBox), ``Segmented``, ``FieldBox`` (value + unit in one frame), ``SearchBox``.
- ``layout``: ``Card`` (header / body / footer), ``Section``, ``EmptyState``, ``Hint``.
- ``readouts``: ``ReadoutStrip`` for chart headers.
- ``instrument``: ``paint_device_card`` (used by the device selector), ``DeviceChips``, ``PageHeader``.
- ``icons``: inline SVG line icons.

Colors come from ``ui/styles/tokens.py``; sizes and states live in ``card_workspace.qss`` under the
"Component kit" section. ``python -m lab_control.ui.kit.gallery`` shows every component.
"""

from lab_control.ui.kit.base import (
    Dot,
    ElideLabel,
    IconButton,
    IconTile,
    NavButton,
    Pill,
    Separator,
    SubNavButton,
    label,
    repolish,
    set_property,
)
from lab_control.ui.kit.controls import FieldBox, SearchBox, Segmented, Switch, button, style_button
from lab_control.ui.kit.instrument import DeviceChips, PageHeader, paint_device_card
from lab_control.ui.kit.layout import Card, EmptyState, Hint, Section
from lab_control.ui.kit.readouts import ReadoutCell, ReadoutStrip

__all__ = [
    "Card",
    "DeviceChips",
    "Dot",
    "ElideLabel",
    "EmptyState",
    "FieldBox",
    "Hint",
    "IconButton",
    "IconTile",
    "NavButton",
    "PageHeader",
    "Pill",
    "ReadoutCell",
    "ReadoutStrip",
    "SearchBox",
    "Section",
    "Segmented",
    "Separator",
    "SubNavButton",
    "Switch",
    "button",
    "label",
    "paint_device_card",
    "repolish",
    "set_property",
    "style_button",
]
