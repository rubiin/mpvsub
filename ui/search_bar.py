"""The search form — labelled rows mirroring the classic dialog.

    Subtitles language:  [English ▾]            [Search by hash]
    Title:               [Movie title...]       [Search by name]
    Season (series):     [e.g. 1]
    Episode (series):    [e.g. 1]
    IMDB ID:             [e.g. tt1375666]
    Sort by:             [Best match ▾]
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from gi.repository import Gtk

from models import LANGUAGE_CATALOG
from settings import SORT_MODES, Settings


@dataclass
class SearchCallbacks:
    hash_search: Callable[[], None]
    name_search: Callable[[], None]
    language_changed: Callable[[str], None]
    sort_changed: Callable[[str], None]
    sort_direction_changed: Callable[[str], None]


def _parse_int(text: str) -> Optional[int]:
    try:
        return int(text.strip())
    except (TypeError, ValueError):
        return None


class SearchForm(Gtk.Grid):
    """The labelled form rows at the top of the window."""

    def __init__(self, settings: Settings, callbacks: SearchCallbacks) -> None:
        super().__init__(column_spacing=8, row_spacing=6)
        self._settings = settings
        self._cb = callbacks
        #: last title we auto-filled from the media; used to detect edits
        self._last_auto_title = ""
        self.set_margin_top(10)
        self.set_margin_bottom(4)
        self.set_margin_start(12)
        self.set_margin_end(12)

        # language dropdown — the OpenSubtitles language catalog
        self._lang_codes = list(LANGUAGE_CATALOG)
        lang_model = Gtk.StringList.new(list(LANGUAGE_CATALOG.values()))
        self.lang_dropdown = Gtk.DropDown(model=lang_model)
        current = settings.languages[0] if settings.languages else "en"
        self.lang_dropdown.set_selected(self._lang_index(current))
        self.lang_dropdown.set_tooltip_text("Subtitle language")
        self.lang_dropdown.connect(
            "notify::selected",
            lambda *_: self._cb.language_changed(self.language_code()),
        )

        # sort dropdown — the OpenSubtitles API order_by options
        self._sort_keys = [key for key, _label, _api in SORT_MODES]
        sort_model = Gtk.StringList.new(
            [label for _key, label, _api in SORT_MODES]
        )
        self.sort_dropdown = Gtk.DropDown(model=sort_model)
        self.sort_dropdown.set_selected(self._sort_index(settings.sort))
        self.sort_dropdown.set_tooltip_text("Sort results by")
        self.sort_dropdown.connect(
            "notify::selected",
            lambda *_: self._cb.sort_changed(self.sort_code()),
        )

        # sort direction toggle (Asc/Desc — the API order_direction)
        self.sort_dir_button = Gtk.ToggleButton()
        self.sort_dir_button.set_active(settings.sort_direction == "asc")
        self._update_sort_dir_label()
        self.sort_dir_button.set_tooltip_text(
            "Sort direction (ascending / descending)"
        )
        self.sort_dir_button.connect("toggled", self._on_sort_dir_toggled)

        # title / season / episode / imdb fields
        self.title_entry = Gtk.Entry(placeholder_text="Movie title...")
        self.title_entry.connect("activate", lambda *_: self._cb.name_search())
        self.season_entry = Gtk.Entry(placeholder_text="e.g. 1")
        self.episode_entry = Gtk.Entry(placeholder_text="e.g. 1")
        self.imdb_entry = Gtk.Entry(placeholder_text="e.g. tt1375666")

        # action buttons
        self.btn_hash = Gtk.Button(label="Search by hash")
        self.btn_hash.connect("clicked", lambda *_: self._cb.hash_search())
        self.btn_name = Gtk.Button(label="Search by name")
        self.btn_name.connect("clicked", lambda *_: self._cb.name_search())

        rows = (
            ("Subtitles language:", self.lang_dropdown, self.btn_hash),
            ("Title:", self.title_entry, self.btn_name),
            ("Season (series):", self.season_entry, None),
            ("Episode (series):", self.episode_entry, None),
            ("IMDB ID:", self.imdb_entry, None),
            ("Sort by:", self.sort_dropdown, self.sort_dir_button),
        )
        for row, (label_text, widget, button) in enumerate(rows):
            label = Gtk.Label(label=label_text, xalign=1.0)
            label.set_valign(Gtk.Align.CENTER)
            self.attach(label, 0, row, 1, 1)
            widget.set_hexpand(True)
            widget.set_valign(Gtk.Align.CENTER)
            self.attach(widget, 1, row, 1, 1)
            if button is not None:
                button.set_hexpand(False)
                self.attach(button, 2, row, 1, 1)

    # -- accessors ----------------------------------------------------------

    def _lang_index(self, code: str) -> int:
        try:
            return self._lang_codes.index(code)
        except ValueError:
            return 0

    def _sort_index(self, key: str) -> int:
        try:
            return self._sort_keys.index(key)
        except ValueError:
            return 0

    def language_code(self) -> str:
        index = self.lang_dropdown.get_selected()
        if 0 <= index < len(self._lang_codes):
            return self._lang_codes[index]
        return "en"

    def sort_code(self) -> str:
        index = self.sort_dropdown.get_selected()
        if 0 <= index < len(self._sort_keys):
            return self._sort_keys[index]
        return "score"

    def sort_direction(self) -> str:
        return "asc" if self.sort_dir_button.get_active() else "desc"

    def _update_sort_dir_label(self) -> None:
        self.sort_dir_button.set_label(
            "↑ Asc" if self.sort_dir_button.get_active() else "↓ Desc"
        )

    def _on_sort_dir_toggled(self, _button) -> None:
        self._update_sort_dir_label()
        self._cb.sort_direction_changed(self.sort_direction())

    def fields(self) -> tuple[str, Optional[int], Optional[int]]:
        return (
            self.title_entry.get_text().strip(),
            _parse_int(self.season_entry.get_text()),
            _parse_int(self.episode_entry.get_text()),
        )

    def imdb_id(self) -> str:
        return self.imdb_entry.get_text().strip()

    def set_title_fields(
        self,
        title: Optional[str],
        season: Optional[int],
        episode: Optional[int],
    ) -> None:
        """Prefill the fields (None leaves a field untouched)."""
        if title is not None:
            self.title_entry.set_text(title)
        if season is not None:
            self.season_entry.set_text(str(season))
        if episode is not None:
            self.episode_entry.set_text(str(episode))

    def sync_to_media(
        self, title: str, season: Optional[int], episode: Optional[int]
    ) -> None:
        """Update the fields to match the currently playing media.

        If the user has typed their own title, the fields are left alone so
        their manual search isn't clobbered by mpv media changes.
        """
        current = self.title_entry.get_text().strip()
        if current and current != self._last_auto_title:
            return
        self.set_title_fields(title, season, episode)
        # don't carry stale season/episode from a previous episode into a movie
        if season is None and episode is None:
            self.season_entry.set_text("")
            self.episode_entry.set_text("")
        self._last_auto_title = title
