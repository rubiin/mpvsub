"""The results list: a Gtk.ColumnView over a Gio.ListStore.

Rows are wrapped in a small GObject (:class:`ResultItem`) because
``Gio.ListStore`` requires GObject instances.  A single Subtitle Name
column that expands to the full width.
"""

from __future__ import annotations

from typing import Callable, Optional

from gi.repository import Gio, GObject, Gtk

from models import SubtitleResult
from ui import row as row_cells


class ResultItem(GObject.Object):
    """GObject wrapper so results can live in a Gio.ListStore."""

    __gtype_name__ = "MpvuiResultItem"

    def __init__(self, subtitle: SubtitleResult) -> None:
        super().__init__()
        self.subtitle = subtitle


class ResultList:
    """Owns the store, selection model and column view."""

    def __init__(
        self,
        on_activate: Callable[[int], None],
        on_selection: Callable[[], None],
    ) -> None:
        self._on_activate = on_activate
        self._on_selection = on_selection
        self._visible: list[SubtitleResult] = []

        self.store = Gio.ListStore.new(ResultItem)
        self.selection = Gtk.SingleSelection(model=self.store)
        self.selection.connect("selection-changed", self._selection_changed)

        self.view = Gtk.ColumnView(model=self.selection)
        self.view.add_css_class("result-list")
        self.view.connect("activate", self._activate)

        self._build_columns()

        # GtkColumnView does not scroll on its own: without a scrolled
        # window it grows to the full content height.  Wrapping it keeps the
        # popup compact (~5-6 rows visible) with the rest scrollable, like
        # VLC's dialog.
        scroller = Gtk.ScrolledWindow()
        scroller.set_child(self.view)
        scroller.set_vexpand(True)
        self._widget = scroller

    # -- columns ------------------------------------------------------------

    def _add_column(
        self,
        title: str,
        factory: Gtk.SignalListItemFactory,
        expand: bool = False,
    ) -> None:
        column = Gtk.ColumnViewColumn(title=title, factory=factory)
        column.set_expand(expand)
        self.view.append_column(column)

    def _build_columns(self) -> None:
        name_factory, _ = row_cells.name_cell()
        self._add_column("Subtitle Name", name_factory, expand=True)

    # -- data ---------------------------------------------------------------

    def set_items(self, items: list[SubtitleResult]) -> None:
        """Replace the visible list, keeping selection on the first row."""
        self._visible = items
        self.store.remove_all()
        for item in items:
            self.store.append(ResultItem(item))
        if items:
            self.selection.set_selected(0)
        else:
            self.selection.set_selected(Gtk.INVALID_LIST_POSITION)

    def selected_index(self) -> int:
        index = self.selection.get_selected()
        return index if index != Gtk.INVALID_LIST_POSITION else -1

    def selected_result(self) -> Optional[SubtitleResult]:
        index = self.selected_index()
        if 0 <= index < len(self._visible):
            return self._visible[index]
        return None

    def widget(self) -> Gtk.Widget:
        return self._widget

    # -- handlers -----------------------------------------------------------

    def _selection_changed(self, _selection, _pos, _n) -> None:
        self._on_selection()

    def _activate(self, _view, position: int) -> None:
        if 0 <= position < len(self._visible):
            self._on_activate(position)
