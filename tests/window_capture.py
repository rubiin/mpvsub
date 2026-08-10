#!/usr/bin/env python3
"""Dev tool: render the downloader window to a PNG using REAL API results.

Used to regenerate the README screenshot:

    GDK_BACKEND=x11 DISPLAY=:0 python3 tests/window_capture.py shot.png

Fetches results live from api.opensubtitles.com — no sample data. The API
requires an account for every request now, so set OPENSUBTITLES_USERNAME /
OPENSUBTITLES_PASSWORD (env vars) or it falls back to the empty
("No subtitles found.") state and that is what gets captured.

Requires an X11 display and ImageMagick's `import`.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gio, GLib  # noqa: E402

from cache import SearchCache  # noqa: E402
from models import CliArgs, SearchQuery  # noqa: E402
from opensubtitles_client import OpenSubtitlesClient  # noqa: E402
from settings import Settings  # noqa: E402
from ui.window import SubtitleWindow  # noqa: E402

OUT = sys.argv[1] if len(sys.argv) > 1 else "/tmp/subtitle-downloader.png"

SEARCH_TEXT = "Whatever Happened to My Revolution"


def fetch_real_results() -> list:
    """Live name search against the OpenSubtitles REST API."""
    client = OpenSubtitlesClient(Settings(), SearchCache())
    query = SearchQuery(
        text=SEARCH_TEXT,
        year=2018,
        kind="movie",
        languages=("en",),
    )
    try:
        return asyncio.run(client.search(query, None))
    except Exception as exc:  # noqa: BLE001
        print(f"live search failed: {exc}", file=sys.stderr, flush=True)
        return []


def main() -> int:
    results = fetch_real_results()
    print(f"live search returned {len(results)} results", flush=True)

    app = Adw.Application.new(
        "org.mpvui.WindowCapture", Gio.ApplicationFlags.NON_UNIQUE
    )
    state = {}

    def on_activate(application) -> None:
        window = SubtitleWindow(
            application=application, settings=Settings(), cli=CliArgs()
        )
        window.form.set_title_fields(f"{SEARCH_TEXT} 2018", None, None)
        if results:
            window.result_list.set_items(results)
            window.stack.set_visible_child_name("results")
        else:
            window.stack.set_visible_child_name("empty")
        window.present()
        state["window"] = window

    def capture() -> bool:
        window = state.get("window")
        if window is None:
            return False
        try:
            from gi.repository import GdkX11

            native = window.get_native()
            xid = GdkX11.X11Surface.get_xid(native.get_surface())
            subprocess.run(["import", "-window", str(xid), OUT], check=True)
            print(f"saved {OUT}", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"capture failed: {exc}", file=sys.stderr, flush=True)
        app.quit()
        return False

    app.connect("activate", on_activate)
    GLib.timeout_add(3500, capture)
    GLib.timeout_add(30000, app.quit)  # hard stop — never hang the caller
    app.run([])
    return 0


if __name__ == "__main__":
    sys.exit(main())
