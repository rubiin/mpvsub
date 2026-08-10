"""Small dialogs: error, help, config, and the account credentials dialog."""

from __future__ import annotations

from typing import Callable

from gi.repository import Adw, GObject, Gtk

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


class CredentialsDialog(Adw.Window):
    """Modal window asking for OpenSubtitles username/password.

    A real window instead of :class:`Adw.AlertDialog` so the entry fields
    reliably receive keyboard focus (an alert dialog's extra child can
    never be focused on some setups). It is presented on its own on first
    run and on demand from the Account… button.

    Emits ``response`` (``"save"`` or ``"cancel"``) before closing and
    ``closed`` when the dialog closes — the same surface the old alert
    dialog exposed, so callers only connect to ``closed``.
    """

    __gsignals__ = {
        "response": (GObject.SignalFlags.RUN_LAST, None, (str,)),
        "closed": (GObject.SignalFlags.RUN_LAST, None, ()),
    }

    def __init__(
        self,
        parent: Gtk.Widget,
        settings: Settings,
        on_saved: Callable[[], None],
    ) -> None:
        super().__init__(title="OpenSubtitles account", modal=True, resizable=False)
        self.set_transient_for(parent)
        self._responded = False
        self._closed_emitted = False

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        content.set_margin_top(24)
        content.set_margin_bottom(24)
        content.set_margin_start(24)
        content.set_margin_end(24)

        heading = Gtk.Label(label="OpenSubtitles account")
        heading.add_css_class("title-1")
        heading.set_halign(Gtk.Align.START)
        content.append(heading)

        body = Gtk.Label(
            label=(
                "OpenSubtitles.com needs a free account for every request — "
                "enter the username and password of your opensubtitles.com "
                "account."
            )
        )
        body.set_wrap(True)
        body.set_xalign(0.0)
        body.add_css_class("dim-label")
        content.append(body)

        grid = Gtk.Grid(column_spacing=8, row_spacing=6)
        grid.set_margin_top(8)

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
        content.append(grid)

        btn_cancel = Gtk.Button(label="Cancel")
        btn_save = Gtk.Button(label="Save")
        btn_save.add_css_class("suggested-action")
        btn_row = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=6,
            halign=Gtk.Align.END,
        )
        btn_row.set_margin_top(8)
        btn_row.append(btn_cancel)
        btn_row.append(btn_save)
        content.append(btn_row)

        self.set_content(content)
        self.set_default_widget(btn_save)

        def _save() -> None:
            settings.username = username.get_text().strip()
            settings.password = password.get_text()
            settings.save()
            on_saved()

        def _on_response(_dialog, response: str) -> None:
            if response == "save":
                _save()

        self.connect("response", _on_response)
        # Enter in either field saves, like the Save button
        username.connect("activate", lambda *_: self.respond("save"))
        password.connect("activate", lambda *_: self.respond("save"))
        btn_cancel.connect("clicked", lambda *_: self.close())
        btn_save.connect("clicked", lambda *_: self.respond("save"))
        # focus the first field once the window is really mapped on screen
        self.connect("map", lambda *_: username.grab_focus())
        self.connect("close-request", self._on_close_request)

    def respond(self, response: str) -> None:
        """Emit ``response`` (once) and close the window."""
        if self._responded:
            return
        self._responded = True
        self.emit("response", response)
        self._emit_closed()
        self.close()

    def _on_close_request(self, *_args) -> bool:
        # closing via the window-close button (no response): report the close
        self._emit_closed()
        return False  # let the default close (hide) proceed

    def _emit_closed(self) -> None:
        """Emit ``closed`` once. Gtk.Window.close() only hides the window in
        GTK 4, so ``destroy`` may never fire — emit from both close paths."""
        if not self._closed_emitted:
            self._closed_emitted = True
            self.emit("closed")


def show_credentials_dialog(
    parent: Gtk.Widget,
    settings: Settings,
    on_saved: Callable[[], None],
) -> CredentialsDialog:
    """Ask for OpenSubtitles username/password in a modal window."""
    dialog = CredentialsDialog(parent, settings, on_saved)
    dialog.present()
    return dialog
