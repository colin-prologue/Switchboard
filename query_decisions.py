#!/usr/bin/env python3
"""query_decisions.py — pull relevant precedent from the decision log.

Deterministic and zero-token: it filters the JSON decision records on disk and returns
LEAN digests, so a planning session grounds itself in your team's history without paying
to re-read full records. Used two ways:
  * by the entry harness, to hand the planner relevant prior decisions before it plans;
  * by a worker, to resolve a task's `grounding` list into readable context.

Digest = id, type, title, chosen option, status, tags, timestamp, truncated reasoning,
evidence refs, and supersession — enough to ground a new decision, nothing more.
"""

import argparse
import json
import os
import re

STOP = {"the", "a", "an", "to", "of", "for", "and", "or", "in", "on", "with", "that",
        "add", "build", "make", "use", "it", "is", "be", "we", "our", "this"}


def _load(repo):
    ddir = os.path.join(repo, ".decisions")
    out = []
    if not os.path.isdir(ddir):
        return out
    for f in os.listdir(ddir):
        if f.endswith(".json"):
            try:
                with open(os.path.join(ddir, f), encoding="utf-8") as fh:
                    out.append(json.load(fh))
            except Exception:
                continue
    return out


def _keywords(text):
    return {w for w in re.findall(r"[a-z0-9-]+", (text or "").lower()) if w not in STOP and len(w) > 2}


def _digest(d):
    reasoning = (d.get("reasoning") or "")[:240]
    return {
        "id": d.get("id"),
        "type": d.get("type"),
        "title": d.get("title"),
        "chosen": d.get("chosen"),
        "status": d.get("status"),
        "tags": d.get("tags", []),
        "timestamp": d.get("timestamp"),
        "reasoning": reasoning,
        "evidence": [e.get("ref") for e in d.get("evidence", [])],
        "superseded_by": d.get("superseded_by"),
    }


def query(repo, tags=None, level=None, phase=None, status=None, text=None,
          limit=8, include_superseded=False):
    """Return ranked decision digests. Pure file filtering — no model, no tokens."""
    tags = set(tags or [])
    kw = _keywords(text)
    scored = []
    for d in _load(repo):
        if not include_superseded and (d.get("status") == "superseded" or d.get("superseded_by")):
            continue
        if status and d.get("status") != status:
            continue
        if level and d.get("level") != level:
            continue
        if phase and d.get("phase") != phase:
            continue
        dtags = set(d.get("tags", []))
        score = 0
        score += 3 * len(tags & dtags)                                   # tag overlap, weighted
        if kw:
            hay = _keywords(d.get("title", "")) | dtags
            score += len(kw & hay)                                       # keyword hits
        if not tags and not kw and not (level or phase or status):
            score = 1                                                    # no filters -> recency list
        if score > 0:
            scored.append((score, d.get("timestamp", ""), _digest(d)))
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return [d for _, _, d in scored[:limit]]


def main():
    ap = argparse.ArgumentParser(description="Query the decision log for precedent.")
    ap.add_argument("--repo", default=".")
    ap.add_argument("--tags", default="", help="comma-separated")
    ap.add_argument("--level")
    ap.add_argument("--phase")
    ap.add_argument("--status")
    ap.add_argument("--text", help="free text (e.g. the goal) for keyword matching")
    ap.add_argument("--limit", type=int, default=8)
    ap.add_argument("--include-superseded", action="store_true")
    ap.add_argument("--json", action="store_true", help="emit JSON (for the harness)")
    a = ap.parse_args()

    res = query(os.path.abspath(a.repo),
                tags=[t.strip() for t in a.tags.split(",") if t.strip()],
                level=a.level, phase=a.phase, status=a.status, text=a.text,
                limit=a.limit, include_superseded=a.include_superseded)

    if a.json:
        print(json.dumps(res, indent=2))
    elif not res:
        print("No matching precedent. (Greenfield, or no decisions logged yet.)")
    else:
        for d in res:
            sup = "  [SUPERSEDED]" if d["superseded_by"] else ""
            print(f"- {d['title']}  ({', '.join(d['tags'])}) [{d['status']}]{sup}")
            if d["chosen"]:
                print(f"    chose: {d['chosen']}")
            if d["reasoning"]:
                print(f"    why: {d['reasoning']}")


if __name__ == "__main__":
    main()
