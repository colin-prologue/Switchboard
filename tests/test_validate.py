import pytest

from sb import validate
from tests.helpers import make_task


def test_valid_task_passes():
    validate.check("task", make_task())


def test_invalid_task_raises_with_path():
    bad = make_task()
    bad["status"] = "no-such-status"
    with pytest.raises(ValueError, match="status"):
        validate.check("task", bad)


def test_result_validation():
    validate.check("result", {
        "schema_version": "0.1.0", "outcome": "success", "summary": "ok",
    })
    with pytest.raises(ValueError):
        validate.check("result", {"schema_version": "0.1.0", "outcome": "success"})
