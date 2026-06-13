import json
import os

from sb import query


def put(lay, rec):
    with open(os.path.join(lay.decisions, f"{rec['id']}.json"), "w",
              encoding="utf-8") as f:
        json.dump(rec, f)


def rec(rid, title, tags, status="approved", superseded_by=None):
    r = {"schema_version": "0.3.0", "id": rid, "type": "human",
         "status": status, "timestamp": "2026-06-12T00:00:00+00:00",
         "title": title, "tags": tags,
         "author": {"kind": "human", "id": "colin"},
         "reasoning": f"reasoning for {title}"}
    if superseded_by:
        r["superseded_by"] = superseded_by
    return r


def test_query_ranks_tag_matches_first(lay):
    put(lay, rec("HDR-101", "Pick a cache design", ["caching", "concurrency"]))
    put(lay, rec("HDR-102", "Name the framework", ["naming"]))
    out = query.query(lay, tags=["caching"], limit=5)
    assert out[0]["id"] == "HDR-101"


def test_query_text_keywords(lay):
    put(lay, rec("HDR-101", "Pick a cache design", []))
    put(lay, rec("HDR-102", "Name the framework", []))
    out = query.query(lay, text="how should the cache work", limit=5)
    assert out and out[0]["id"] == "HDR-101"


def test_superseded_excluded_by_default(lay):
    put(lay, rec("HDR-101", "Old way", ["caching"], superseded_by="HDR-102"))
    put(lay, rec("HDR-102", "New way", ["caching"]))
    ids = [d["id"] for d in query.query(lay, tags=["caching"])]
    assert ids == ["HDR-102"]
    ids = [d["id"] for d in query.query(lay, tags=["caching"],
                                        include_superseded=True)]
    assert set(ids) == {"HDR-101", "HDR-102"}


def test_digest_shape_is_lean(lay):
    put(lay, rec("HDR-101", "Pick a cache design", ["caching"]))
    d = query.query(lay, tags=["caching"])[0]
    assert set(d) == {"id", "type", "title", "chosen", "status", "tags",
                      "timestamp", "reasoning", "evidence", "superseded_by"}
