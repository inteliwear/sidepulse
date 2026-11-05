from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


@dataclass(frozen=True)
class JetBrainsIDE:
    name: str
    bundle_id: str
    settings_vendor: str
    settings_prefix: str


JETBRAINS_IDES = (
    JetBrainsIDE(
        "IntelliJ IDEA", "com.jetbrains.intellij", "JetBrains", "IntelliJIdea"
    ),
    JetBrainsIDE("PyCharm", "com.jetbrains.pycharm", "JetBrains", "PyCharm"),
    JetBrainsIDE("WebStorm", "com.jetbrains.WebStorm", "JetBrains", "WebStorm"),
    JetBrainsIDE("GoLand", "com.jetbrains.goland", "JetBrains", "GoLand"),
    JetBrainsIDE("PhpStorm", "com.jetbrains.PhpStorm", "JetBrains", "PhpStorm"),
    JetBrainsIDE("Rider", "com.jetbrains.rider", "JetBrains", "Rider"),
    JetBrainsIDE("CLion", "com.jetbrains.CLion", "JetBrains", "CLion"),
    JetBrainsIDE("RubyMine", "com.jetbrains.rubymine", "JetBrains", "RubyMine"),
    JetBrainsIDE("DataGrip", "com.jetbrains.datagrip", "JetBrains", "DataGrip"),
    JetBrainsIDE(
        "Android Studio",
        "com.google.android.studio",
        "Google",
        "AndroidStudio",
    ),
)


def jetbrains_ide_for_project(
    project_path: str | Path | None,
    *,
    home: Path | None = None,
) -> JetBrainsIDE | None:
    if not project_path:
        return None

    home = home or Path.home()
    project = normalize_project_path(project_path, home)
    recent_matches: list[tuple[int, int, JetBrainsIDE]] = []
    for order, (ide, recent_projects) in enumerate(recent_project_files(home)):
        if any(
            path == project or path in project.parents
            for path in read_recent_project_paths(recent_projects, home)
        ):
            try:
                modified_at = recent_projects.stat().st_mtime_ns
            except OSError:
                continue
            recent_matches.append((modified_at, -order, ide))

    if recent_matches:
        return max(recent_matches, key=lambda match: (match[0], match[1]))[2]

    return next(
        (ide for ide in JETBRAINS_IDES if ide_is_installed(ide.bundle_id)),
        None,
    )


def recent_project_files(home: Path) -> tuple[tuple[JetBrainsIDE, Path], ...]:
    result: list[tuple[JetBrainsIDE, Path]] = []
    for ide in JETBRAINS_IDES:
        settings_root = home / "Library" / "Application Support" / ide.settings_vendor
        try:
            settings_dirs = tuple(settings_root.iterdir())
        except OSError:
            continue
        for settings_dir in settings_dirs:
            if not settings_dir.is_dir() or not settings_dir.name.startswith(
                ide.settings_prefix
            ):
                continue
            recent_projects = settings_dir / "options" / "recentProjects.xml"
            if recent_projects.is_file():
                result.append((ide, recent_projects))
    return tuple(result)


def read_recent_project_paths(path: Path, home: Path) -> tuple[Path, ...]:
    try:
        stat = path.stat()
    except OSError:
        return ()
    return _read_recent_project_paths_cached(
        str(path), str(home), stat.st_mtime_ns, stat.st_size
    )


@lru_cache(maxsize=128)
def _read_recent_project_paths_cached(
    path: str,
    home: str,
    _mtime_ns: int,
    _size: int,
) -> tuple[Path, ...]:
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError):
        return ()

    home_path = Path(home)
    paths: list[Path] = []
    for element in root.iter():
        value = element.get("value")
        if not value:
            continue
        candidate = value.replace("$USER_HOME$", str(home_path))
        if candidate.startswith("file://"):
            candidate = candidate.removeprefix("file://")
        project_path = Path(candidate).expanduser()
        if project_path.is_absolute():
            paths.append(project_path.resolve())
    return tuple(paths)


def normalize_project_path(path: str | Path, home: Path) -> Path:
    value = str(path).replace("$USER_HOME$", str(home))
    return Path(value).expanduser().resolve()


def ide_is_installed(bundle_id: str) -> bool:
    try:
        from AppKit import NSWorkspace

        return (
            NSWorkspace.sharedWorkspace().URLForApplicationWithBundleIdentifier_(
                bundle_id
            )
            is not None
        )
    except Exception:
        return False