"""Notification channels for the notify hook (spec §7, PHI-029). Each channel
is a callable (title, body) -> None. macOS notification is the default; ntfy /
Teams can be added here later without touching notify.py."""

import json
import subprocess


def macos(title, body):
    # AppleScript string literals are double-quoted and accept the same
    # backslash escapes json.dumps emits for ", \, \n, \t. But osascript does
    # NOT understand \uXXXX, so json must keep non-ASCII literal — em-dashes in
    # AgDR titles otherwise raise a syntax error and (check=False) drop the
    # notification silently. ensure_ascii=False keeps those characters intact.
    # Best-effort: never raise. check=False swallows a non-zero exit, but NOT a
    # missing-binary FileNotFoundError, so guard OSError too — on a non-macOS
    # worker (or any env without osascript) the channel must degrade to a no-op
    # rather than crash sb notify and wedge the poll loop.
    script = (f"display notification {json.dumps(body, ensure_ascii=False)} "
              f"with title {json.dumps(title, ensure_ascii=False)}")
    try:
        subprocess.run(["osascript", "-e", script], check=False,
                       capture_output=True)
    except OSError:
        pass


def stdout(title, body):
    print(f"[sb notify] {title}: {body}")


def null(title, body):
    return None


def resolve(name):
    return {"macos": macos, "stdout": stdout, "null": null}.get(name, stdout)
