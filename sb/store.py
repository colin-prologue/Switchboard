"""Task file io. Lane transitions are atomic os.rename — the loser of a
claim race gets FileNotFoundError, never a corrupt state."""

import json
import os

from sb import validate
from sb.paths import LANES


def read_json(path):
    with open(path, encoding="utf-8") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError as e:
            raise ValueError(f"corrupt json at {path}: {e}") from e


def write_json(path, obj):
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2)
    os.replace(tmp, path)


def fname(task_id):
    return task_id.replace("/", "_") + ".json"


def task_path(lay, lane, task_id):
    return os.path.join(lay.lane(lane), fname(task_id))


def write_task(lay, lane, task):
    """Validated write. lane and task["status"] are not cross-checked here;
    lane placement is the caller's contract."""
    validate.check("task", task)
    write_json(task_path(lay, lane, task["id"]), task)


def list_tasks(lay, lane):
    d = lay.lane(lane)
    out = []
    for f in sorted(os.listdir(d)):
        if f.endswith(".json"):
            out.append(read_json(os.path.join(d, f)))
    return out


def move_task(lay, src_lane, dst_lane, task_id):
    """Atomic lane transition. False = source already gone (lost a race)."""
    try:
        os.rename(task_path(lay, src_lane, task_id),
                  task_path(lay, dst_lane, task_id))
        return True
    except FileNotFoundError:
        return False


def find_task(lay, task_id):
    """Scan lanes for a task. A file renamed away between lanes mid-scan
    (TOCTOU) is treated as not-in-this-lane; the scan continues."""
    for lane in LANES:
        p = task_path(lay, lane, task_id)
        try:
            return lane, read_json(p)
        except FileNotFoundError:
            continue
    return None, None


def done_ids(lay):
    return {t["id"] for t in list_tasks(lay, "done")}
