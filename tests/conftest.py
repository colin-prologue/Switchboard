import pytest

from sb import paths


@pytest.fixture
def lay(tmp_path):
    return paths.init(str(tmp_path))
