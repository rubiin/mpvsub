#!/usr/bin/env python3
"""Subtitle downloader for mpv — a VLC-style GTK4/Libadwaita popup.

Usage:
    python3 main.py                          # no mpv/file: manual search only
    python3 main.py /path/to/video.mkv       # search for a specific file
    python3 main.py --socket /tmp/mpv.sock   # talk to a running mpv
    python3 main.py --socket /tmp/mpv.sock --file /path/to/video.mkv
"""

from __future__ import annotations

import logging
import sys

# declare GI API versions before anything imports gi.repository
import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from app import SubtitleApp, parse_cli_args  # noqa: E402

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def _want_debug(argv: list[str]) -> bool:
    return "--debug" in argv


def main(argv: list[str]) -> int:
    cli = parse_cli_args(argv)
    level = logging.DEBUG if (cli.debug or _want_debug(argv)) else logging.INFO
    logging.basicConfig(level=level, format=LOG_FORMAT)
    logging.getLogger("PIL").setLevel(logging.WARNING)

    app = SubtitleApp()
    # run(None) would hand GApplication an empty argv; pass ours so
    # --socket/--file survive
    return app.run(sys.argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
