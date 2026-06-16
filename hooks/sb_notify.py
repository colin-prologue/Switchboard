#!/usr/bin/env python3
"""Thin Claude Code hook shim: runs `sb notify` against the repo so the
worker session (or any hook event) pushes new gate/pause/AgDR/stall signals
through the configured channel. Wired as an actual Claude Code hook in Plan 3;
standalone-runnable now. Edge-triggered, so safe to call on every event."""

import sys

from sb.cli import main

if __name__ == "__main__":
    raise SystemExit(main(["notify", *sys.argv[1:]]))
