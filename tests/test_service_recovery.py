"""Recovery must not replace another installation under the same service name."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from sidepulse.service_launch import SERVICE_LABEL, restart_service


@pytest.mark.parametrize('platform', ['darwin', 'linux'])
def test_isolated_home_without_installed_service_never_touches_user_service(tmp_path, platform):
    with patch('sidepulse.service_launch.sys.platform', platform), patch.object(Path, 'home', return_value=tmp_path), \
         patch('sidepulse.service_launch.subprocess.run') as run, \
         patch('sidepulse.service_launch.shutil.which', return_value='/usr/bin/systemctl'):
        result = restart_service()
    assert not result.started and not result.changed
    assert not list(tmp_path.iterdir())
    run.assert_not_called()


@pytest.mark.parametrize('loaded', [True, False])
def test_mac_recovers_registered_service_without_rewriting_launcher(tmp_path, loaded):
    plist = tmp_path / 'Library' / 'LaunchAgents' / f'{SERVICE_LABEL}.plist'
    plist.parent.mkdir(parents=True)
    # Contents deliberately belong to a different installation than this test.
    contents = b'existing installation plist'
    plist.write_bytes(contents)
    replies = [SimpleNamespace(returncode=0 if loaded else 1, stderr='')]
    if not loaded:
        replies.append(SimpleNamespace(returncode=0, stderr=''))
    with patch('sidepulse.service_launch.sys.platform', 'darwin'), patch.object(Path, 'home', return_value=tmp_path), \
         patch('sidepulse.service_launch.os.getuid', return_value=42, create=True), \
         patch('sidepulse.service_launch.subprocess.run', side_effect=replies) as run, \
         patch('sidepulse.service_launch.build_service_launcher_script') as generate:
        result = restart_service()
    assert result.started and not result.changed
    assert plist.read_bytes() == contents
    assert run.call_args_list[0].args[0] == ['launchctl', 'kickstart', '-k', 'gui/42/io.sidepulse.service']
    if not loaded:
        assert run.call_args_list[1].args[0] == ['launchctl', 'bootstrap', 'gui/42', str(plist)]
    assert run.call_count == len(replies)
    generate.assert_not_called()


def test_linux_restarts_existing_unit_without_install_or_reload(tmp_path):
    unit = tmp_path / '.config' / 'systemd' / 'user' / 'sidepulse.service'
    unit.parent.mkdir(parents=True)
    unit.write_text('Existing unit')
    with patch('sidepulse.service_launch.sys.platform', 'linux'), patch.object(Path, 'home', return_value=tmp_path), \
         patch('sidepulse.service_launch.shutil.which', return_value='/bin/systemctl'), \
         patch('sidepulse.service_launch.subprocess.run', return_value=SimpleNamespace(returncode=0, stderr='')) as run:
        result = restart_service()
    assert result.started and not result.changed
    assert unit.read_text() == 'Existing unit'
    assert run.call_args.args[0] == ['/bin/systemctl', '--user', 'restart', 'sidepulse.service']
