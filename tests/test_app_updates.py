"""Application revision checks, daily persistence and offline behavior."""
import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from sidepulse.update import BuildInfo, DailyUpdateCheck, UPDATE_CHECK_SECONDS, check_github_main


@pytest.fixture
def history(tmp_path):
    def git(*args):
        return subprocess.check_output(['git', *args], cwd=tmp_path, text=True).strip()
    git('init', '-b', 'main')
    git('config', 'user.name', 'SidePulse Test')
    git('config', 'user.email', 'test@example.invalid')
    git('commit', '--allow-empty', '-m', 'base')
    base = git('rev-parse', 'HEAD')
    git('checkout', '-b', 'app')
    git('commit', '--allow-empty', '-m', 'local app work')
    local = git('rev-parse', 'HEAD')
    git('checkout', 'main')
    git('commit', '--allow-empty', '-m', 'new main work')
    main = git('rev-parse', 'HEAD')
    git('checkout', 'app')
    return BuildInfo('1.0.dev1', local, '2026-10-30T06:30:00Z', tmp_path), base, main


@pytest.mark.parametrize('revision,expected', [('base', 'current'), ('main', 'available'), ('unknown', 'available')])
def test_unpublished_branch_uses_ancestry_instead_of_hash_inequality(history, revision, expected):
    build, base, main = history
    latest = {'base': base, 'main': main, 'unknown': 'f' * 40}[revision]
    with patch('sidepulse.update.github_json', return_value={'object': {'sha': latest}}) as fetch:
        assert check_github_main(build)['status'] == expected
        fetch.assert_called_once()  # Local unpublished commits need no GitHub comparison.


@pytest.mark.parametrize('missing,expected', [(0, 'current'), (3, 'available')])
def test_packaged_build_compares_commits_absent_from_the_build(missing, expected):
    build = BuildInfo('1.0', 'a' * 40, '')
    with patch('sidepulse.update.github_json', side_effect=[
        {'object': {'sha': 'b' * 40}}, {'ahead_by': missing},
    ]):
        assert check_github_main(build)['status'] == expected


def test_daily_result_survives_restart_and_rechecks_after_24_hours(tmp_path):
    build = BuildInfo('1.0', 'a' * 40, '')
    path = tmp_path / 'app-update.json'
    with patch('sidepulse.update.check_github_main', return_value={'status': 'current'}) as check:
        first = DailyUpdateCheck(build, path)
        first.check_if_due(now=100)
        first.thread.join(2)
        assert not first.thread.is_alive()
        second = DailyUpdateCheck(build, path)
        for at in (100, 101, 100 + UPDATE_CHECK_SECONDS - 1):
            second.check_if_due(now=at)
        check.assert_called_once()
        second.check_if_due(now=100 + UPDATE_CHECK_SECONDS)
        second.thread.join(2)
        assert check.call_count == 2


def test_offline_check_is_not_up_to_date_and_does_not_retry_each_tick(tmp_path):
    build = BuildInfo('1.0', 'a' * 40, '')
    with patch('sidepulse.update.check_github_main', side_effect=OSError('offline')) as check:
        checker = DailyUpdateCheck(build, tmp_path / 'cache.json')
        checker.check_if_due(now=100)
        checker.thread.join(2)
        assert checker.state['status'] == 'unavailable'
        checker.check_if_due(now=101)
        restarted = DailyUpdateCheck(build, checker.path)
        restarted.check_if_due(now=200)
        check.assert_called_once()


def test_different_build_does_not_reuse_up_to_date_result(tmp_path):
    path = tmp_path / 'cache.json'
    path.write_text(json.dumps({'version': '1.0', 'revision': 'a' * 40,
                                'status': 'current', 'checked_at': 100}))
    checker = DailyUpdateCheck(BuildInfo('2.0', 'b' * 40, ''), path)
    assert checker.state['status'] == 'not_checked'


def test_invalid_github_reply_does_not_claim_current():
    with patch('sidepulse.update.github_json', return_value={'object': {'sha': 'invalid'}}):
        with pytest.raises(ValueError):
            check_github_main(BuildInfo('1.0', 'a' * 40, ''))


def test_build_date_is_the_recorded_timestamp_not_today():
    from datetime import datetime
    recorded = '2026-10-24T12:30:00+00:00'
    expected = datetime.fromisoformat(recorded).astimezone().strftime('%Y-%m-%d %H:%M %Z')
    assert BuildInfo('1.0', '', recorded).date_label() == 'Built ' + expected
    assert BuildInfo('1.0', '', '').date_label() == 'Build date unavailable'
