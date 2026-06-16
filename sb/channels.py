"""Notification channels for the notify hook (spec §7, PHI-029). Each channel
is a callable (title, body) -> None. macOS notification is the default; ntfy /
Teams can be added here later without touching notify.py."""

import json
import subprocess


def macos(title, body):
    # AppleScript string literals are double-quoted; json.dumps gives correct
    # quoting/escaping for the common case. Best-effort: never raise.
    script = (f"display notification {json.dumps(body)} "
              f"with title {json.dumps(title)}")
    subprocess.run(["osascript", "-e", script], check=False,
                   capture_output=True)


def stdout(title, body):
    print(f"[sb notify] {title}: {body}")


def null(title, body):
    return None


def resolve(name):
    return {"macos": macos, "stdout": stdout, "null": null}.get(name, stdout)
