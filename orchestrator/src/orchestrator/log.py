"""Structured logging.

implements: core §13.1–13.2 (logging conventions and sinks)

Stable key=value phrasing; issue-related logs carry issue_id/issue_identifier,
session lifecycle logs carry session_id. Sink is stderr (operator-visible
without a debugger); a failing sink must never crash orchestration.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone


def log(msg: str, **ctx: object) -> None:
    """Emit one structured log line to stderr. Never raises."""
    try:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        fields = " ".join(f"{k}={_fmt(v)}" for k, v in ctx.items() if v is not None)
        sys.stderr.write(f"{ts} {msg}{' ' + fields if fields else ''}\n")
        sys.stderr.flush()
    except Exception:
        pass


def _fmt(v: object) -> str:
    s = str(v)
    if len(s) > 400:  # avoid logging large raw payloads (core §13.1)
        s = s[:400] + "…"
    return f'"{s}"' if " " in s else s
