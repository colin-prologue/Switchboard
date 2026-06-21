"""Zero-token precedent retrieval over the tracked decisions/ directory.
Keyword ranking for now; embeddings are an explicit deferral (spec §10)."""

import os
import re

from sb import store

STOP = {"the", "a", "an", "to", "of", "for", "and", "or", "in", "on", "with",
        "that", "add", "build", "make", "use", "it", "is", "be", "we", "our",
        "this", "how", "should", "work"}


def _keywords(text):
    return {w for w in re.findall(r"[a-z0-9-]+", (text or "").lower())
            if w not in STOP and len(w) > 2}


def _load(lay):
    out = []
    if not os.path.isdir(lay.decisions):
        return out
    for f in sorted(os.listdir(lay.decisions)):
        if f.endswith(".json"):
            try:
                out.append(store.read_json(os.path.join(lay.decisions, f)))
            except (ValueError, FileNotFoundError):
                continue
    return out


def _digest(d):
    return {
        "id": d.get("id"),
        "type": d.get("type"),
        "title": d.get("title"),
        "chosen": d.get("chosen"),
        "status": d.get("status"),
        "tags": d.get("tags", []),
        "timestamp": d.get("timestamp"),
        "reasoning": (d.get("reasoning") or "")[:240],
        "evidence": [e.get("ref") for e in d.get("evidence", [])],
        "superseded_by": d.get("superseded_by"),
    }


def query(lay, tags=None, level=None, status=None, text=None, limit=8,
          include_superseded=False):
    want_tags = set(tags or [])
    want_words = _keywords(text)
    scored = []
    for d in _load(lay):
        if d.get("superseded_by") and not include_superseded:
            continue
        if level and d.get("level") != level:
            continue
        if status and d.get("status") != status:
            continue
        score = 3 * len(want_tags & set(d.get("tags", [])))
        body = " ".join([d.get("title", ""), d.get("context", ""),
                         d.get("reasoning", "")])
        score += len(want_words & _keywords(body))
        if score > 0 or not (want_tags or want_words):
            scored.append((score, d.get("id", ""), d))
    scored.sort(key=lambda s: (-s[0], s[1]))
    return [_digest(d) for _, _, d in scored[:limit]]
