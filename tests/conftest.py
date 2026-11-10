"""Installation identities created by tests must not alter the user's configuration."""
import pytest
import os
from sidepulse import program_status


@pytest.fixture(autouse=True)
def isolated_windows_profile(tmp_path, monkeypatch):
    if os.name == 'nt':
        # Windows resolves ~ from USERPROFILE, not HOME. Child processes must
        # never fall back to the signed-in account or its running daemon.
        for key, value in {'USERPROFILE': tmp_path, 'HOME': tmp_path,
                           'XDG_CONFIG_HOME': tmp_path / '.config',
                           'XDG_STATE_HOME': tmp_path / '.local/state',
                           'XDG_DATA_HOME': tmp_path / '.local/share'}.items():
            monkeypatch.setenv(key, str(value))


@pytest.fixture(autouse=True)
def isolated_publisher(tmp_path, monkeypatch):
    original = program_status.installation_publisher
    monkeypatch.setattr(program_status, 'installation_publisher',
                        lambda path=None: original(path or tmp_path / 'publisher-id'))


@pytest.fixture
def program_client():
    from contextlib import contextmanager
    import json
    import socket
    @contextmanager
    def connect(path):
        token = None
        if os.name == 'nt':
            endpoint = json.loads(path.read_text())
            client = socket.create_connection(('127.0.0.1', endpoint['port']), timeout=2)
            token = endpoint['token']
        else:
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.settimeout(2)
            client.connect(str(path))
        class AuthenticatedClient:
            def sendall(self, data):
                if token:
                    try:
                        data = json.dumps({'auth': token, 'report': json.loads(data)}).encode()
                    except ValueError:
                        pass  # Deliberately incomplete or malformed client.
                client.sendall(data)
            def __getattr__(self, name):
                return getattr(client, name)
        try:
            yield AuthenticatedClient()
        finally:
            client.close()
    return connect
