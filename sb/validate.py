"""The single validation choke point. Every task/plan/result/decision write
in the engine calls check() before touching disk."""

import json
import os

from jsonschema import Draft202012Validator

_SCHEMA_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "schemas")

NAMES = {
    "task": "task.schema.json",
    "plan": "plan.schema.json",
    "decision": "decision-record.schema.json",
    "result": "result.schema.json",
}

_cache = {}


def schema(name):
    if name not in _cache:
        with open(os.path.join(_SCHEMA_DIR, NAMES[name]), encoding="utf-8") as f:
            _cache[name] = Draft202012Validator(json.load(f))
    return _cache[name]


def check(name, obj):
    errors = sorted(schema(name).iter_errors(obj), key=lambda e: e.json_path)
    if errors:
        msgs = "; ".join(f"{e.json_path}: {e.message}" for e in errors[:5])
        raise ValueError(f"{name} schema validation failed: {msgs}")
    return obj
