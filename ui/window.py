"""The main window.

Layout mirrors the classic subtitle-downloader dialog: a labelled search
form on top (language + Search by hash, Title + Search by name, Season and
Episode), a scrollable results list in the middle (~5-6 rows visible), and
Show help / Show config / Download selection / Close at the bottom.  All
network work runs on the asyncio thread via :class:`AsyncRunner`.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from pathlib import Path
from typing import Optional

from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from cache import SearchCache
from ipc import MpvClient, MpvError
from models import CliArgs, SearchQuery, SubtitleResult, VideoInfo
from search import (
    build_query,
    build_query_fields,
    sort_results,
    video_from_path,
)
from opensubtitles_client import OpenSubtitlesClient
from settings import SETTINGS_FILE, Settings
from ui.dialogs import (
    show_config_dialog,
    show_credentials_dialog,
    show_error_dialog,
    show_help_dialog,
)
from ui.result_list import ResultList
from ui.search_bar import SearchCallbacks, SearchForm

log = logging.getLogger(__name__)

CSS = """
.result-list row { border-radius: 8px; min-height: 34px; }
.result-list row:hover { background-color: alpha(@accent_color, 0.10); }
.result-list row:selected { background-color: alpha(@accent_color, 0.22); }
.subtitle-name { font-weight: 600; }
.hi-badge { background-color: alpha(@accent_color, 0.18);
            color: @accent_color; border-radius: 999px;
            padding: 1px 7px; font-size: 10px; font-weight: 700; }
"""


class AsyncRunner:
    """Runs an asyncio event loop on a background thread."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run, name="asyncio", daemon=True
        )

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        try:
            self.loop.call_soon_threadsafe(self.loop.stop)
            self._thread.join(timeout=2.0)
        except Exception:  # noqa: BLE001
            pass

    def submit(self, coro, on_done=None):
        """Run *coro* on the loop; *on_done* is called on the GTK thread with
        the result (or the raised exception)."""
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        if on_done is not None:

            def _callback(done) -> None:
                try:
                    result = done.result()
                except Exception as exc:  # noqa: BLE001
                    result = exc
                GLib.idle_add(self._dispatch, on_done, result)

            future.add_done_callback(_callback)
        return future

    @staticmethod
    def _dispatch(on_done, result) -> None:
        try:
            on_done(result)
        except Exception:  # noqa: BLE001
            log.exception("UI callback failed")


class SubtitleWindow(Adw.ApplicationWindow):
    def __init__(
        self,
        application: Adw.Application,
        settings: Settings,
        cli: Optional[CliArgs],
    ) -> None:
        super().__init__(
            application=application,
            title="Subtitle downloader",
            default_width=700,
            default_height=500,
        )
        # fixed size like the reference dialog: the results list scrolls
        self.set_resizable(False)
        self.settings = settings
        self._cli = cli

        self._runner = AsyncRunner()
        self._runner.start()
        self.cache = SearchCache(ttl=600)
        self._client = OpenSubtitlesClient(settings, self.cache)
        self._mpv = MpvClient(cli.socket if cli else None)

        self._current_video: Optional[VideoInfo] = None
        self._results_full: list[SubtitleResult] = []
        self._visible: list[SubtitleResult] = []
        self._last_query: Optional[SearchQuery] = None
        self._last_action = "hash"  # "hash" | "name" — re-run target
        self._searching = False
        self._downloading = False
        self._debounce_id = 0
        self._searched_path: Optional[str] = None
        self._alive = True
        self._credentials_dialog_open = False
        self._startup_prompt_done = False

        self._install_css()
        self._build_ui()
        self._install_shortcuts()
        self._connect_mpv()
        self.apply_cli(cli)
        self.connect("map", self._on_mapped)
        self.connect("destroy", self._on_destroy)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _install_css(self) -> None:
        provider = Gtk.CssProvider()
        provider.load_from_string(CSS)
        display = Gdk.Display.get_default()
        if display is not None:
            Gtk.StyleContext.add_provider_for_display(
                display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )

    def _build_ui(self) -> None:
        self._toast_overlay = Adw.ToastOverlay()
        self.set_content(self._toast_overlay)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._toast_overlay.set_child(root)

        # error banner --------------------------------------------------
        self._banner = Adw.Banner(title="")
        if hasattr(self._banner, "set_button_label"):  # libadwaita >= 1.7
            self._banner.set_button_label("Retry")
        else:  # pragma: no cover - libadwaita < 1.7 (add_button API)
            self._actions = Gio.SimpleActionGroup()
            retry = Gio.SimpleAction.new("retry", None)
            retry.connect("activate", lambda *_: self._retry_last_search())
            self._actions.add_action(retry)
            self.insert_action_group("win", self._actions)
            self._banner.add_button("Retry", "win.retry")
        self._banner.connect("button-clicked", lambda *_: self._retry_last_search())
        self._banner.set_revealed(False)
        root.append(self._banner)

        # search form ---------------------------------------------------
        callbacks = SearchCallbacks(
            hash_search=self._search_by_hash,
            name_search=self._search_by_name,
            language_changed=self._on_language_changed,
            sort_changed=self._on_sort_changed,
            sort_direction_changed=self._on_sort_direction_changed,
        )
        self.form = SearchForm(self.settings, callbacks)
        root.append(self.form)

        # stack of states -----------------------------------------------
        self.stack = Gtk.Stack(vexpand=True)
        self.stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)

        self.result_list = ResultList(
            on_activate=self._download_selected,
            on_selection=self._update_download_button,
        )
        self.stack.add_named(self.result_list.widget(), "results")
        self.stack.add_named(self._build_loading_page(), "loading")
        self.stack.add_named(self._build_empty_page(), "empty")
        self.stack.add_named(self._build_error_page(), "error")
        self.stack.set_visible_child_name("empty")
        root.append(self.stack)

        # bottom action bar ---------------------------------------------
        action_bar = Gtk.ActionBar()
        btn_help = Gtk.Button(label="Show help")
        btn_help.connect("clicked", lambda *_: show_help_dialog(self))
        btn_config = Gtk.Button(label="Show config")
        btn_config.connect(
            "clicked", lambda *_: show_config_dialog(self, self.settings)
        )
        btn_account = Gtk.Button(label="Account…")
        btn_account.set_tooltip_text("Set your OpenSubtitles username/password")
        btn_account.connect("clicked", lambda *_: self._open_credentials())
        self.btn_download = Gtk.Button(label="Download selection")
        self.btn_download.add_css_class("suggested-action")
        self.btn_download.set_sensitive(False)
        self.btn_download.connect("clicked", lambda *_: self._download_selected())
        btn_close = Gtk.Button(label="Close")
        btn_close.connect("clicked", lambda *_: self.close())
        action_bar.pack_start(btn_help)
        action_bar.pack_start(btn_config)
        action_bar.pack_start(btn_account)
        action_bar.pack_end(btn_close)
        action_bar.pack_end(self.btn_download)
        root.append(action_bar)

        self._update_download_button()

    def _build_loading_page(self) -> Gtk.Widget:
        box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=12,
            halign=Gtk.Align.CENTER,
            valign=Gtk.Align.CENTER,
        )
        spinner = Gtk.Spinner(spinning=True)
        spinner.set_size_request(40, 40)
        box.append(spinner)
        box.append(Gtk.Label(label="Searching subtitles…"))
        return box

    def _build_empty_page(self) -> Gtk.Widget:
        icon = self._load_icon("subtitle", "document-text-symbolic")
        page = Adw.StatusPage(
            title="No subtitles found.",
            description="Try another language or title.",
        )
        if icon is not None:
            page.set_paintable(icon)
        else:
            page.set_icon_name("document-text-symbolic")
        self._empty_page = page
        return page

    def _build_error_page(self) -> Gtk.Widget:
        self._error_page = Adw.StatusPage(
            icon_name="action-unavailable-symbolic",
            title="Search failed",
            description="",
        )
        return self._error_page

    @staticmethod
    def _load_icon(icon_name: str, fallback: str) -> Optional[Gdk.Texture]:
        display = Gdk.Display.get_default()
        if display is not None:
            theme = Gtk.IconTheme.get_for_display(display)
            if theme.has_icon(icon_name):
                return theme.lookup_icon(
                    icon_name, 96, 1, Gtk.TextDirection.NONE, 0
                ).get_paintable()
        svg = Path(__file__).resolve().parent.parent / "assets" / "subtitle.svg"
        try:
            from gi.repository import GdkPixbuf

            pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(str(svg), 96, 96, True)
            return Gdk.Texture.new_for_pixbuf(pixbuf)
        except Exception:  # noqa: BLE001 - librsvg may be missing
            return None

    def _install_shortcuts(self) -> None:
        controller = Gtk.ShortcutController()
        controller.set_scope(Gtk.ShortcutScope.MANAGED)
        controller.add_shortcut(
            Gtk.Shortcut.new(
                Gtk.ShortcutTrigger.parse_string("<Control>f"),
                Gtk.CallbackAction.new(self._focus_title),
            )
        )
        controller.add_shortcut(
            Gtk.Shortcut.new(
                Gtk.ShortcutTrigger.parse_string("<Control>r"),
                Gtk.CallbackAction.new(self._refresh_action),
            )
        )
        controller.add_shortcut(
            Gtk.Shortcut.new(
                Gtk.ShortcutTrigger.parse_string("Escape"),
                Gtk.CallbackAction.new(self._close_action),
            )
        )
        self.add_controller(controller)

    # ------------------------------------------------------------------
    # mpv integration
    # ------------------------------------------------------------------

    def _connect_mpv(self) -> None:
        self._mpv.on_connect = lambda: GLib.idle_add(self._on_mpv_connected)
        self._mpv.on_disconnect = lambda: GLib.idle_add(self._on_mpv_disconnected)
        # start first so the loop exists before observers are registered
        self._mpv.start(self._runner.loop)
        for prop in ("path", "media-title"):
            self._mpv.observe(prop, self._on_mpv_property)

    def _on_mpv_property(self, _name, _value) -> None:
        GLib.idle_add(self._schedule_media_refresh)

    def _schedule_media_refresh(self) -> None:
        if self._debounce_id:
            GLib.source_remove(self._debounce_id)
        self._debounce_id = GLib.timeout_add(600, self._refresh_media_from_mpv)

    def _on_mpv_connected(self) -> None:
        if not self._alive:
            return
        self._refresh_media_from_mpv()

    def _on_mpv_disconnected(self) -> None:
        pass  # the download toast explains the fallback when mpv is absent

    def _refresh_media_from_mpv(self) -> bool:
        self._debounce_id = 0

        async def _snapshot():
            path = await self._mpv.get_property("path", "")
            title = await self._mpv.get_property("media-title", "")
            return path, title

        def _done(result) -> None:
            if not self._alive or isinstance(result, Exception):
                return
            path, title = result
            if not path:
                return
            local = path if os.path.isfile(path) else ""
            if local:
                video = video_from_path(local)
            elif title:
                video = video_from_path(title)
            else:
                video = video_from_path(os.path.basename(path))
            video.path = path  # keep the original (possibly remote) path
            self._current_video = video
            self._prefill_form(video)
            self._auto_search(video)

        self._runner.submit(_snapshot(), _done)
        return False

    def _auto_search(self, video: VideoInfo) -> None:
        if self._searching or self._searched_path == video.path:
            return
        self._searched_path = video.path
        if video.path and os.path.isfile(video.path):
            self._search_by_hash()
        elif self.form.fields()[0]:
            self._search_by_name()

    def _prefill_form(self, video: VideoInfo) -> None:
        """Sync Title/Season/Episode to the current media.

        The Title field shows the full media file name (extension kept);
        guessit's parsed title is still used for the search itself.
        User-typed titles are preserved, otherwise the fields follow the
        media so they are never stale or empty while mpv is playing.
        """
        title = video.filename
        if video.kind == "episode" and video.season is not None:
            self.form.sync_to_media(title, video.season, video.episode)
        else:
            self.form.sync_to_media(title, None, None)

    # ------------------------------------------------------------------
    # CLI entry
    # ------------------------------------------------------------------

    def apply_cli(self, cli: Optional[CliArgs]) -> None:
        if cli is None:
            return
        if cli.file:
            self._current_video = video_from_path(cli.file)
            self._prefill_form(self._current_video)
            self._searched_path = self._current_video.path
            self._search_by_hash()
        elif cli.query:
            self.form.set_title_fields(cli.query, None, None)
            self._search_by_name()

    # ------------------------------------------------------------------
    # search flow
    # ------------------------------------------------------------------

    def _search_by_hash(self) -> None:
        if self._searching:
            return
        video = self._current_video
        path = video.path if video else ""
        if not path or not os.path.isfile(path):
            self._show_banner(
                "Hash search needs a local file — open one in mpv or pass "
                "a video path on the command line."
            )
            self.stack.set_visible_child_name("empty")
            return
        query = build_query(video, "", self.settings.languages)
        self._last_action = "hash"
        self._start_search(query)

    def _search_by_name(self) -> None:
        if self._searching:
            return
        title, season, episode = self.form.fields()
        imdb = self.form.imdb_id()
        if not title and not imdb:
            self._show_banner("Enter a title (or IMDB id) to search by name.")
            self.stack.set_visible_child_name("empty")
            return
        query = build_query_fields(
            title, season, episode, self.settings.languages, imdb_id=imdb or None
        )
        self._last_action = "name"
        self._start_search(query)

    def _start_search(self, query: SearchQuery) -> None:
        self._searching = True
        self._set_busy(True)
        self.stack.set_visible_child_name("loading")
        self._last_query = query
        self._runner.submit(
            self._client.search(query, self._current_video), self._on_search_done
        )

    def _on_search_done(self, result) -> None:
        if not self._alive:
            return
        self._searching = False
        self._set_busy(False)
        if isinstance(result, Exception):
            self._show_search_error(result)
            return
        self._show_banner("")
        self._results_full = result
        self._apply_view()

    def _apply_view(self) -> None:
        items = sort_results(
            self._results_full,
            self.settings.sort,
            self._current_video,
            self._last_query or SearchQuery(),
        )
        self._visible = items
        self.result_list.set_items(items)
        if self._visible:
            self.stack.set_visible_child_name("results")
        else:
            self._empty_page.set_description(
                "Try another language, title or release."
            )
            self.stack.set_visible_child_name("empty")
        self._update_download_button()

    def _on_language_changed(self, code: str) -> None:
        if not code:
            return
        self.settings.languages = [code]
        self.settings.save()
        if self._results_full or self._current_video is not None:
            self._rerun_last()

    def _on_sort_changed(self, key: str) -> None:
        if not key or key == self.settings.sort:
            return
        self.settings.sort = key
        self.settings.save()
        if self._results_full or self._current_video is not None:
            self._rerun_last()

    def _on_sort_direction_changed(self, direction: str) -> None:
        if direction == self.settings.sort_direction:
            return
        self.settings.sort_direction = direction
        self.settings.save()
        if self._results_full or self._current_video is not None:
            self._rerun_last()

    def _rerun_last(self) -> None:
        if self._last_action == "hash":
            self._search_by_hash()
        else:
            self._search_by_name()

    # ------------------------------------------------------------------
    # download flow
    # ------------------------------------------------------------------

    def _download_selected(self, position: Optional[int] = None) -> None:
        if self._downloading:
            return
        if position is not None:
            if not (0 <= position < len(self._visible)):
                return
            sub = self._visible[position]
        else:
            sub = self.result_list.selected_result()
        if sub is None:
            return
        self._downloading = True
        self._set_busy(True)
        self.btn_download.set_label("Downloading…")
        self._runner.submit(
            self._client.download(
                sub, self._current_video, self._last_query or SearchQuery()
            ),
            self._on_download_done,
        )

    def _on_download_done(self, result) -> None:
        if not self._alive:
            return
        self._downloading = False
        self._set_busy(False)
        self.btn_download.set_label("Download selection")
        if isinstance(result, Exception):
            log.warning("download failed: %s", result)
            show_error_dialog(self, "Download failed", str(result))
            return
        if not result.ok:
            message = result.error or "Unknown error"
            if result.hint:
                message = f"{message}\n\n{result.hint}"
            log.warning("download failed: %s", message)
            show_error_dialog(self, "Download failed", message)
            return
        path = result.path or ""
        if self._mpv.connected and path:
            self._runner.submit(self._load_into_mpv(path), self._on_loaded_into_mpv)
        else:
            saved_to = os.path.dirname(path) or "disk"
            if self._mpv.socket_path:
                self._toast(f"Saved to {saved_to} (mpv not connected)")
            else:
                self._toast(
                    f"Saved to {saved_to} — start mpv with --input-ipc-server "
                    "to auto-load"
                )

    async def _load_into_mpv(self, path: str) -> bool:
        try:
            track_id = await self._mpv.sub_add(path)
            if track_id is not None:
                try:
                    await self._mpv.set_property("sid", track_id)
                except MpvError:
                    pass
            return True
        except MpvError:
            return False

    def _on_loaded_into_mpv(self, result) -> None:
        if not self._alive:
            return
        if isinstance(result, Exception) or result is False:
            self._toast("Saved — mpv connection lost before auto-load")
        else:
            self._toast("Subtitle loaded.")

    # ------------------------------------------------------------------
    # state helpers
    # ------------------------------------------------------------------

    def _set_busy(self, busy: bool) -> None:
        self.form.set_sensitive(not busy)
        self.result_list.widget().set_sensitive(not busy)
        self._update_download_button()

    def _update_download_button(self) -> None:
        has_selection = (
            self.result_list.selected_result() is not None
            and not self._searching
            and not self._downloading
        )
        self.btn_download.set_sensitive(bool(has_selection))

    def _show_banner(self, message: str) -> None:
        self._banner.set_title(message)
        self._banner.set_revealed(bool(message))

    def _show_search_error(self, exc: Exception) -> None:
        message = str(exc) or exc.__class__.__name__
        log.warning("search error: %s", message)
        if any(w in message.lower() for w in ("credential", "login")):
            message = (
                f"{message}\n\nEnter your username/password via the "
                "Account… button."
            )
            self._open_credentials()
        self._show_banner(message)
        self._error_page.set_description(message)
        self.stack.set_visible_child_name("error")

    # ------------------------------------------------------------------
    # credentials prompting
    # ------------------------------------------------------------------

    def _has_credentials(self) -> bool:
        return bool(
            os.environ.get("OPENSUBTITLES_USERNAME")
            or os.environ.get("OPENSUBTITLES_PASSWORD")
            or self.settings.username
            or self.settings.password
        )

    def _on_mapped(self, *_args) -> None:
        GLib.idle_add(self._maybe_show_startup_credentials)

    def _maybe_show_startup_credentials(self) -> None:
        if self._startup_prompt_done:
            return
        self._startup_prompt_done = True
        # first run: no settings file yet and no credentials anywhere — ask
        if not SETTINGS_FILE.exists() and not self._has_credentials():
            self._open_credentials()

    def _open_credentials(self) -> None:
        if self._credentials_dialog_open:
            return  # already showing (e.g. startup prompt + failing search)
        self._credentials_dialog_open = True

        def on_saved() -> None:
            self._toast("Account saved.")
            # a credentials error may have failed the last search — retry it
            if self.stack.get_visible_child_name() == "error":
                self._retry_last_search()

        dialog = show_credentials_dialog(self, self.settings, on_saved=on_saved)
        dialog.connect(
            "closed", lambda *_: setattr(self, "_credentials_dialog_open", False)
        )

    def _retry_last_search(self) -> None:
        self._show_banner("")
        self._rerun_last()

    def _toast(self, message: str) -> None:
        self._toast_overlay.add_toast(Adw.Toast.new(message))

    # ------------------------------------------------------------------
    # shortcuts / lifecycle
    # ------------------------------------------------------------------

    def _focus_title(self, *_args) -> bool:
        self.form.title_entry.grab_focus()
        return True

    def _refresh_action(self, *_args) -> bool:
        self._rerun_last()
        return True

    def _close_action(self, *_args) -> bool:
        self.close()
        return True

    def _on_destroy(self, *_args) -> None:
        self._alive = False
        self.settings.save()
        if self._runner.loop is not None and self._mpv is not None:
            try:
                asyncio.run_coroutine_threadsafe(self._mpv.close(), self._runner.loop)
            except Exception:  # noqa: BLE001
                pass
        self._runner.stop()
