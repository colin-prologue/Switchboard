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
        "schema_version": "0.2.0", "outcome": "success", "summary": "ok",
    })
    with pytest.raises(ValueError, match="summary"):
        validate.check("result", {"schema_version": "0.2.0", "outcome": "success"})


def test_unknown_schema_name_raises():
    with pytest.raises(ValueError, match="unknown schema name"):
        validate.check("typo", {})
