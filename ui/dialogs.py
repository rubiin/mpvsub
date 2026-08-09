"""Small dialogs: error, help, config, and the account credentials dialog."""

from __future__ import annotations

from typing import Callable

from gi.repository import Adw, GLib, Gtk

from settings import SORT_MODES, Settings


def _sort_label(key: str) -> str:
    for sort_key, label, _api in SORT_MODES:
        if sort_key == key:
            return label
    return key

#: key bindings shown by the help dialog
HELP_ROWS = (
    ("↑ / ↓", "navigate the result list"),
    ("Enter", "download the selected subtitle"),
    ("Double-click", "download that subtitle"),
    ("Ctrl+F", "focus the Title field"),
    ("Ctrl+R", "re-run the last search"),
    ("Esc", "close the window"),
)


def _info_dialog(parent: Gtk.Widget, title: str, body: str) -> None:
    dialog = Adw.AlertDialog.new(title, body)
    dialog.add_response("ok", "OK")
    dialog.set_default_response("ok")
    dialog.present(parent)


def show_error_dialog(parent: Gtk.Widget, title: str, message: str) -> None:
    _info_dialog(parent, title, message)


def show_help_dialog(parent: Gtk.Widget) -> None:
    body = "\n".join(f"{key}  —  {what}" for key, what in HELP_ROWS)
    _info_dialog(parent, "Help — keys", body)


def show_config_dialog(parent: Gtk.Widget, settings: Settings) -> None:
    account = settings.username or "not configured"
    rows = (
        ("Languages", ", ".join(settings.languages) or "en"),
        ("Sort by", _sort_label(settings.sort)),
        ("Account", account),
        ("Download dir", settings.download_dir),
        ("Encoding", settings.encoding),
        ("Max results", str(settings.max_results)),
    )
    body = "\n".join(f"{key}:  {value}" for key, value in rows)
    _info_dialog(parent, "Configuration", body)


def show_credentials_dialog(
    parent: Gtk.Widget,
    settings: Settings,
    on_saved: Callable[[], None],
) -> None:
    """Ask for OpenSubtitles username/password; saves to *settings* on Save.

    Widgets are rebuilt per open because the alert dialog takes ownership
    of its extra child and destroys it on close.
    """
    dialog = Adw.AlertDialog.new(
        "OpenSubtitles account",
        "OpenSubtitles.com needs a free account for every request — enter the "
        "username and password of your opensubtitles.com account.",
    )
    dialog.add_response("cancel", "Cancel")
    dialog.add_response("save", "Save")
    dialog.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
    dialog.set_default_response("save")
    dialog.set_close_response("cancel")

    grid = Gtk.Grid(column_spacing=8, row_spacing=6)
    grid.set_margin_top(12)
    grid.set_margin_bottom(4)

    username = Gtk.Entry(placeholder_text="you@example.com")
    username.set_text(settings.username or "")
    username.set_hexpand(True)
    password = Gtk.Entry(placeholder_text="password")
    password.set_text(settings.password or "")
    password.set_hexpand(True)
    password.set_visibility(False)
    show = Gtk.ToggleButton(label="Show")
    show.set_tooltip_text("Show the password")

    def _on_show_toggled(button) -> None:
        active = button.get_active()
        password.set_visibility(active)
        button.set_label("Hide" if active else "Show")

    show.connect("toggled", _on_show_toggled)

    def _label(text: str) -> Gtk.Label:
        lbl = Gtk.Label(label=text, xalign=1.0)
        lbl.set_valign(Gtk.Align.CENTER)
        return lbl

    grid.attach(_label("Username:"), 0, 0, 1, 1)
    grid.attach(username, 1, 0, 1, 1)
    grid.attach(_label("Password:"), 0, 1, 1, 1)
    grid.attach(password, 1, 1, 1, 1)
    grid.attach(show, 2, 1, 1, 1)

    def _save() -> None:
        settings.username = username.get_text().strip()
        settings.password = password.get_text()
        settings.save()
        on_saved()

    def _on_response(_dialog, response: str) -> None:
        if response == "save":
            _save()

    dialog.connect("response", _on_response)
    # Enter in either field saves, like the Save button
    username.connect("activate", lambda *_: dialog.respond("save"))
    password.connect("activate", lambda *_: dialog.respond("save"))
    dialog.set_extra_child(grid)
    dialog.present(parent)
    GLib.idle_add(username.grab_focus)
    return dialog
