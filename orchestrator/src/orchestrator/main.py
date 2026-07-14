"""CLI entrypoint and host lifecycle.

implements: core §17.7 (CLI contract: positional workflow path, ./WORKFLOW.md
            default, clean startup failure, nonzero exit on failure)
plus the SETUP.md Stage-3 requirement that the binary accepts `--workflow <path>`.
"""

from __future__ import annotations

import argparse
import asyncio
import signal
import sys
from pathlib import Path

from .log import log
from .runner_selector import ClaudeOnlyRunnerSelector, CodexOnlyRunnerSelector
from .scheduler import Orchestrator


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="orchestrator",
        description="Switchboard orchestrator (Symphony-derived; Claude + GitHub Issues)")
    parser.add_argument("workflow_positional", nargs="?", default=None,
                        metavar="path-to-WORKFLOW.md")
    parser.add_argument("--workflow", dest="workflow_flag", default=None,
                        help="path to the composed WORKFLOW.md")
    parser.add_argument(
        "--provider",
        choices=("claude", "codex"),
        default="claude",
        help="execution provider for this process (default: claude)",
    )
    args = parser.parse_args(argv)

    raw = args.workflow_flag or args.workflow_positional or "WORKFLOW.md"
    workflow_path = Path(raw)
    if not workflow_path.is_file():
        log("startup failed", error=f"workflow file not found: {workflow_path}")
        return 2

    selector = (
        CodexOnlyRunnerSelector()
        if args.provider == "codex"
        else ClaudeOnlyRunnerSelector()
    )
    orch = Orchestrator(workflow_path, runner_selector=selector)

    async def _run() -> int:
        loop = asyncio.get_running_loop()
        stop = asyncio.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        runner = asyncio.create_task(orch.run())
        stopper = asyncio.create_task(stop.wait())
        done, _ = await asyncio.wait({runner, stopper},
                                     return_when=asyncio.FIRST_COMPLETED)
        if runner in done:
            exc = runner.exception()
            if exc:
                log("startup failed", error=str(exc))
                return 1
            return 0
        log("shutdown requested")
        await orch.shutdown()
        runner.cancel()
        try:
            await runner
        except (asyncio.CancelledError, Exception):
            pass
        return 0

    return asyncio.run(_run())


if __name__ == "__main__":
    sys.exit(main())
