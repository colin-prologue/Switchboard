"""Dependency-graph guards. With released workers (no held resources),
an acyclic graph cannot deadlock; this is the enqueue-time check."""

from sb import store
from sb.paths import LANES


class CycleError(Exception):
    pass


def assert_acyclic(edge_map):
    state = {}

    def visit(node, path):
        st = state.get(node)
        if st == "done" or node not in edge_map:
            return  # unknown nodes are leaves (e.g. already-done tasks)
        if st == "visiting":
            raise CycleError(" -> ".join(path + [node]))
        state[node] = "visiting"
        for dep in edge_map[node]:
            visit(dep, path + [node])
        state[node] = "done"

    for node in list(edge_map):
        visit(node, [])


def all_edges(lay):
    edges = {}
    for lane in LANES:
        for t in store.list_tasks(lay, lane):
            edges[t["id"]] = list(t.get("context", {}).get("depends_on", []))
    return edges


def assert_addition_ok(lay, new_task, extra_parent_deps=None):
    """Validate the graph stays acyclic if new_task is enqueued (and the
    parent simultaneously gains extra deps, as in sb spawn)."""
    edges = all_edges(lay)
    edges[new_task["id"]] = list(new_task.get("context", {}).get("depends_on", []))
    if extra_parent_deps:
        parent_id, deps = extra_parent_deps
        edges[parent_id] = edges.get(parent_id, []) + list(deps)
    assert_acyclic(edges)
