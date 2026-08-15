"""Suite hermeticity for the runtime-freshness tick caller (issue #131).

THIS FILE CARRIES THE AUTOUSE FIXTURE AND NOTHING ELSE — no test functions.
pytest loads `conftest.py` as a plugin and never COLLECTS tests from it (the
default `python_files`, unoverridden in `orchestrator/pyproject.toml`), so a
test placed here would silently never run. The freshness tests live in
`test_freshness_tick.py`.

Why the scrub exists: issue #131 introduces the orchestrator's first read of
`SB_HOME`/`SB_PROJECT_SLUG`, and a dispatched worker session inherits the
launcher's environment (`runner.py` hands the subprocess `dict(os.environ)`),
where `SB_HOME` IS ambiently bound. Ungated, a worker running the allowlisted
suite would fire one real `git fetch` per `_tick()` call across the whole suite
and recompose the PRODUCTION orchestrator's watched config file. The tick's own
skip rule covers the env-less local run; this fixture covers the dispatched one.
`SB_LAUNCH_SHA` is scrubbed for a different reason: every worker inherits a
bound one from run-project.sh (#32), which would turn the pass-through's
unbound-direction assertion red in exactly the environment the scrub exists for.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _scrub_switchboard_launch_env(monkeypatch):
    """Unbind the three launch variables for every test in this suite.

    `raising=False`: absent is the normal case locally. Tests that need the
    preflight to run set them explicitly at a `tmp_path` skeleton.
    """
    for name in ("SB_HOME", "SB_PROJECT_SLUG", "SB_LAUNCH_SHA"):
        monkeypatch.delenv(name, raising=False)
