"""Command line entry point."""

from __future__ import annotations

import argparse
import curses
import locale
import os
import sys

from . import __version__, pulse
from .ui import TAB_NAMES, Options, run


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="pavucli",
        description="Terminal volume control for PulseAudio / PipeWire, in the spirit of pavucontrol.",
        epilog="Press ? inside the program for the list of keys.",
    )
    p.add_argument(
        "-t",
        "--tab",
        choices=list(TAB_NAMES),
        default="playback",
        help="tab to open on start (default: playback)",
    )
    p.add_argument(
        "-s",
        "--step",
        type=int,
        default=5,
        metavar="PCT",
        help="volume step in percent for the arrow keys (default: 5)",
    )
    p.add_argument(
        "-m",
        "--max-volume",
        type=int,
        default=150,
        metavar="PCT",
        help="upper limit of the volume bars in percent (default: 150)",
    )
    p.add_argument("--ascii", action="store_true", help="use plain ASCII instead of Unicode block characters")
    p.add_argument("--no-color", action="store_true", help="disable colours (also honours NO_COLOR)")
    p.add_argument("--no-mouse", action="store_true", help="do not capture the mouse")
    p.add_argument("-V", "--version", action="version", version=f"%(prog)s {__version__}")
    args = p.parse_args(argv)
    if not 1 <= args.step <= 100:
        p.error("--step must be between 1 and 100")
    if not 100 <= args.max_volume <= 400:
        p.error("--max-volume must be between 100 and 400")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    locale.setlocale(locale.LC_ALL, "")
    os.environ.setdefault("ESCDELAY", "25")  # snappy Esc key

    try:
        pulse.fetch()
    except pulse.PulseError as exc:
        print(f"pavucli: cannot talk to the sound server: {exc}", file=sys.stderr)
        return 1

    enc = (locale.getpreferredencoding(False) or "").lower().replace("-", "")
    opts = Options(
        tab=TAB_NAMES[args.tab],
        step=args.step,
        max_volume=args.max_volume,
        ascii=args.ascii or enc != "utf8",
        mouse=not args.no_mouse,
        color=not args.no_color and not os.environ.get("NO_COLOR"),
    )
    try:
        curses.wrapper(run, opts)
    except KeyboardInterrupt:
        pass
    return 0
