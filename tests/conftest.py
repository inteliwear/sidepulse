"""Installation identities created by tests must not alter the user's configuration."""
import pytest
from sidepulse import program_status


@pytest.fixture(autouse=True)
def isolated_publisher(tmp_path, monkeypatch):
    original = program_status.installation_publisher
    monkeypatch.setattr(program_status, 'installation_publisher',
                        lambda path=None: original(path or tmp_path / 'publisher-id'))
