"""Single-instance Adw.Application (HANDLES_COMMAND_LINE).

A second launch from mpv (new socket/file) just re-targets the one window.
"""

from __future__ import annotations

import logging
from typing import Optional

from gi.repository import Adw, Gio

from models import CliArgs
from settings import APP_ID, Settings

log = logging.getLogger(__name__)


class SubtitleApp(Adw.Application):
    def __init__(self) -> None:
        super().__init__(
            application_id=APP_ID,
            flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE,
        )
        self.window = None
        self._cli: Optional[CliArgs] = None

    # -- GApplication overrides --------------------------------------------

    def do_command_line(self, command_line: Gio.ApplicationCommandLine) -> int:
        argv = command_line.get_arguments()
        self._cli = parse_cli_args(argv[1:])
        self.activate()
        return 0

    def do_activate(self) -> None:
        if self.window is None:
            from ui.window import SubtitleWindow

            self.window = SubtitleWindow(
                application=self, settings=Settings.load(), cli=self._cli
            )
        elif self._cli is not None:
            # relaunch from mpv: re-target the existing window
            self.window.apply_cli(self._cli)
        self.window.present()


def parse_cli_args(argv: list[str]) -> CliArgs:
    """Parse CLI args without argparse (GApplication already consumed argv)."""
    cli = CliArgs()
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--socket" and i + 1 < len(argv):
            cli.socket = argv[i + 1]
            i += 2
        elif arg == "--file" and i + 1 < len(argv):
            cli.file = argv[i + 1]
            i += 2
        elif arg == "--query" and i + 1 < len(argv):
            cli.query = argv[i + 1]
            i += 2
        elif arg == "--debug":
            cli.debug = True
            i += 1
        elif arg == "--width" and i + 1 < len(argv):
            cli.width = _to_int(argv[i + 1])
            i += 2
        elif arg == "--height" and i + 1 < len(argv):
            cli.height = _to_int(argv[i + 1])
            i += 2
        elif not arg.startswith("--") and cli.file is None:
            cli.file = arg  # positional video file
            i += 1
        else:
            i += 1
    return cli


def _to_int(value: str) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
