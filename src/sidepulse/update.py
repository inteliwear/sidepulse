from __future__ import annotations

import subprocess
import sys
import tempfile
import json
import re
import threading
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import __version__
from .providers import default_state_dir

SETUP_URL = "https://sidepulse.io/setup.sh"
GITHUB_API = 'https://api.github.com/repos/inteliwear/sidepulse'
GITHUB_MAIN_URL = 'https://github.com/inteliwear/sidepulse/commits/main/'
UPDATE_CHECK_SECONDS = 24 * 60 * 60


@dataclass(frozen=True)
class BuildInfo:
    version: str
    revision: str
    built_at: str
    checkout: Path | None = None

    @classmethod
    def current(cls):
        try:
            from ._build_info import revision, built_at
        except ImportError:
            revision, built_at = '', ''
        revision = revision.removeprefix('g')
        if not re.fullmatch(r'[0-9a-f]{7,40}', revision):
            match = re.search(r'\bg([0-9a-f]{7,40})\b', __version__)
            revision = match.group(1) if match else ''
        root = Path(__file__).resolve().parents[2]
        return cls(__version__, revision, built_at, root if (root / '.git').exists() else None)

    def date_label(self):
        try:
            local = datetime.fromisoformat(self.built_at.replace('Z', '+00:00')).astimezone()
            return 'Built ' + local.strftime('%Y-%m-%d %H:%M %Z')
        except ValueError:
            return 'Build date unavailable'


def github_json(path):
    request = urllib.request.Request(GITHUB_API + path, headers={
        'Accept': 'application/vnd.github+json', 'User-Agent': 'SidePulse-update-check',
    })
    with urllib.request.urlopen(request, timeout=5) as response:
        body = response.read(1024 * 1024 + 1)
    if len(body) > 1024 * 1024:
        raise ValueError('GitHub response too large')
    return json.loads(body)


def check_github_main(build):
    """Check ancestry, so a local branch ahead of main is not called outdated."""
    latest = github_json('/git/ref/heads/main')['object']['sha']
    if not isinstance(latest, str) or not re.fullmatch(r'[0-9a-f]{40}', latest):
        raise ValueError('Invalid GitHub main revision')
    if not build.revision:
        raise ValueError('This build has no source revision')
    if latest.startswith(build.revision):
        return {'status': 'current', 'main_revision': latest}
    if build.checkout is not None:
        # Unpublished development commits cannot be compared by GitHub's API.
        result = subprocess.run(['git', 'merge-base', '--is-ancestor', latest, build.revision],
                                cwd=build.checkout, capture_output=True, timeout=5)
        if result.returncode in (0, 1):
            return {'status': 'current' if result.returncode == 0 else 'available',
                    'main_revision': latest}
        shallow = subprocess.run(['git', 'rev-parse', '--is-shallow-repository'],
                                 cwd=build.checkout, capture_output=True, text=True, timeout=5)
        local = subprocess.run(['git', 'cat-file', '-e', f'{build.revision}^{{commit}}'],
                               cwd=build.checkout, capture_output=True, timeout=5)
        if shallow.returncode == 0 and shallow.stdout.strip() == 'false' and local.returncode == 0:
            # A revision absent from complete local history cannot be included.
            return {'status': 'available', 'main_revision': latest}
    comparison = github_json(f'/compare/{build.revision}...{latest}?per_page=1')
    missing = comparison['ahead_by']  # main's commits absent from this build
    if type(missing) is not int or missing < 0:
        raise ValueError('Invalid GitHub comparison')
    return {'status': 'available' if missing else 'current', 'main_revision': latest}


class DailyUpdateCheck:
    """One background check per day, with results retained across restarts."""
    def __init__(self, build=None, path=None):
        self.build = build or BuildInfo.current()
        self.path = path or default_state_dir() / 'app-update.json'
        self.state = {'status': 'not_checked', 'checked_at': 0.0}
        self.in_flight = False
        self.thread = None
        try:
            saved = json.loads(self.path.read_text())
            if (saved.get('version'), saved.get('revision')) == (self.build.version, self.build.revision):
                if saved['status'] in {'current', 'available', 'unavailable'}:
                    float(saved['checked_at'])
                    self.state = saved
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            pass

    def check_if_due(self, now=None):
        now = time.time() if now is None else now
        elapsed = now - float(self.state['checked_at'])
        if self.in_flight or (self.state['status'] != 'not_checked' and 0 <= elapsed < UPDATE_CHECK_SECONDS):
            return
        self.in_flight = True
        self.state = {'status': 'checking', 'checked_at': now}
        self.thread = threading.Thread(target=self._check, args=(now,), name='sidepulse-update', daemon=True)
        self.thread.start()

    def _check(self, now):
        try:
            result = check_github_main(self.build)
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
            result = {'status': 'unavailable'}
        self.state = {**result, 'checked_at': now, 'version': self.build.version, 'revision': self.build.revision}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix('.tmp')
            temporary.write_text(json.dumps(self.state))
            temporary.replace(self.path)
        except OSError:
            pass
        finally:
            self.in_flight = False


def update_installation(*, dry_run: bool = False) -> int:
    print(f"Updating with: curl -fsSL {SETUP_URL} | bash", flush=True)
    if dry_run:
        print("Would run the same installer as one-command setup, using its install path and environment overrides.")
        return 0
    try:
        # Download completely before executing: a failed or interrupted curl
        # must not run a partial installer or appear to succeed via a pipe.
        with tempfile.TemporaryDirectory(prefix="sidepulse-update-") as directory:
            script = Path(directory) / "setup.sh"
            subprocess.run(
                ["curl", "-fsSL", "-H", "Cache-Control: no-cache", SETUP_URL,
                 "-o", str(script)], check=True,
            )
            subprocess.run(["bash", str(script)], check=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"SidePulse update failed: {exc}", file=sys.stderr)
        return 1
    return 0
