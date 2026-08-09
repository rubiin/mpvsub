"""Cell widget builders for the results list rows.

Each function returns the widget construction + bind steps used by the
:class:`~ui.result_list.ResultList` column factories.
"""

from __future__ import annotations

from typing import Callable

from gi.repository import Gtk

from models import SubtitleResult


def make_label(
    halign: Gtk.Align = Gtk.Align.START,
    ellipsize: bool = True,
    css_class: str = "",
) -> Gtk.Label:
    label = Gtk.Label(
        halign=halign,
        valign=Gtk.Align.CENTER,
        xalign=0.0 if halign == Gtk.Align.START else 1.0,
    )
    if ellipsize:
        label.set_ellipsize(True)
        label.set_max_width_chars(1)  # forces ellipsizing within the column
    if css_class:
        label.add_css_class(css_class)
    return label


def _bind_label(list_item, text: str) -> None:
    child = list_item.get_child()
    if isinstance(child, Gtk.Label):
        child.set_text(text)


def name_cell() -> tuple[Gtk.SignalListItemFactory, Callable]:
    """Subtitle name column with optional hearing-impaired badge.

    Names longer than 70 characters are truncated with a trailing ellipsis
    so every row stays on one line without GTK's mid-text ellipsizing.
    """

    def setup(list_item) -> None:
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        name = make_label(ellipsize=False)
        name.add_css_class("subtitle-name")
        name.set_hexpand(True)
        badge = Gtk.Label(label="HI", css_classes=["hi-badge"])
        badge.set_visible(False)
        box.append(name)
        box.append(badge)
        box.set_valign(Gtk.Align.CENTER)
        list_item.set_child(box)

    def bind(list_item) -> None:
        sub: SubtitleResult = list_item.get_item().subtitle
        box = list_item.get_child()
        name, badge = box.get_first_child(), box.get_last_child()
        text = sub.name or sub.release_info or "Subtitle"
        if len(text) > 70:
            text = text[:70] + "…"
        name.set_text(text)
        badge.set_visible(sub.hearing_impaired)

    factory = Gtk.SignalListItemFactory()
    factory.connect("setup", lambda _f, item: setup(item))
    factory.connect("bind", lambda _f, item: bind(item))
    return factory, bind

