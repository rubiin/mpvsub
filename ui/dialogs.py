"""Small dialogs used by the main window: the error dialog plus the
"Show help" and "Show config" info dialogs.
"""

from __future__ import annotations

from gi.repository import Adw, Gtk

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
    account = settings.username or "anonymous (search only)"
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
