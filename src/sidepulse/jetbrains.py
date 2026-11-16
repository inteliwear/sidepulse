from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from urllib.parse import unquote


@dataclass(frozen=True)
class JetBrainsIDE:
    name: str
    bundle_id: str
    settings_vendor: str
    settings_prefix: str


@dataclass(frozen=True)
class JetBrainsLaunch:
    ide: JetBrainsIDE
    app_path: Path | None


@dataclass(frozen=True)
class RecentProject:
    path: Path
    timestamp: int | None


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
    JetBrainsIDE(
        "IntelliJ IDEA Community",
        "com.jetbrains.intellij.ce",
        "JetBrains",
        "IdeaIC",
    ),
    JetBrainsIDE(
        "PyCharm Community",
        "com.jetbrains.pycharm.ce",
        "JetBrains",
        "PyCharmCE",
    ),
)


def jetbrains_launch_for_project(
    project_path: str | Path | None,
    *,
    home: Path | None = None,
) -> JetBrainsLaunch | None:
    if not project_path:
        return None

    home = home or Path.home()
    project = normalize_project_path(project_path, home)
    recent_matches: list[tuple[int, int, int, JetBrainsIDE, Path]] = []
    for order, (ide, recent_projects) in enumerate(recent_project_files(home)):
        settings_dir = recent_projects.parent.parent
        try:
            modified_at = recent_projects.stat().st_mtime_ns // 1_000_000
        except OSError:
            continue
        for recent in read_recent_projects(recent_projects, home):
            if recent.path == project or recent.path in project.parents:
                recent_matches.append(
                    (
                        len(recent.path.parts),
                        recent.timestamp if recent.timestamp is not None else modified_at,
                        -order,
                        ide,
                        settings_dir,
                    )
                )

    if not recent_matches:
        return None
    _, _, _, ide, settings_dir = max(recent_matches, key=lambda match: match[:3])
    return JetBrainsLaunch(ide, app_path_for_settings_directory(ide, settings_dir.name))


def recent_project_files(home: Path) -> tuple[tuple[JetBrainsIDE, Path], ...]:
    result: list[tuple[JetBrainsIDE, Path]] = []
    for ide in JETBRAINS_IDES:
        settings_root = home / "Library" / "Application Support" / ide.settings_vendor
        try:
            settings_dirs = tuple(settings_root.iterdir())
        except OSError:
            continue
        for settings_dir in settings_dirs:
            suffix = settings_dir.name.removeprefix(ide.settings_prefix)
            if (
                not settings_dir.is_dir()
                or not suffix
                or not suffix[0].isdigit()
            ):
                continue
            recent_projects = settings_dir / "options" / "recentProjects.xml"
            if recent_projects.is_file():
                result.append((ide, recent_projects))
    return tuple(result)


def read_recent_projects(path: Path, home: Path) -> tuple[RecentProject, ...]:
    try:
        stat = path.stat()
    except OSError:
        return ()
    return _read_recent_projects_cached(
        str(path), str(home), stat.st_mtime_ns, stat.st_size
    )


@lru_cache(maxsize=128)
def _read_recent_projects_cached(
    path: str,
    home: str,
    _mtime_ns: int,
    _size: int,
) -> tuple[RecentProject, ...]:
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError):
        return ()

    home_path = Path(home)
    projects: list[RecentProject] = []
    for component in root.iter("component"):
        if component.get("name") != "RecentProjectsManager":
            continue
        for option in component.findall("option"):
            option_name = option.get("name")
            if option_name == "additionalInfo":
                for entry in option.findall("./map/entry"):
                    project = _recent_project_from_value(
                        entry.get("key"),
                        home_path,
                        _recent_project_timestamp(entry),
                    )
                    if project:
                        projects.append(project)
            elif option_name in {"recentPaths", "openPaths"}:
                for path_option in option.findall("./list/option"):
                    project = _recent_project_from_value(
                        path_option.get("value"), home_path, None
                    )
                    if project:
                        projects.append(project)
    return tuple(projects)


def _recent_project_from_value(
    value: str | None,
    home: Path,
    timestamp: int | None,
) -> RecentProject | None:
    if not value:
        return None
    candidate = value.replace("$USER_HOME$", str(home))
    if re.search(r"\$[^$]+\$", candidate):
        return None
    if candidate.startswith("file://"):
        candidate = unquote(candidate.removeprefix("file://"))
    project_path = Path(candidate).expanduser()
    if not project_path.is_absolute():
        return None
    return RecentProject(project_path.resolve(), timestamp)


def _recent_project_timestamp(entry: ET.Element) -> int | None:
    timestamps = []
    for option in entry.iter("option"):
        if option.get("name") not in {
            "activationTimestamp",
            "projectOpenTimestamp",
        }:
            continue
        try:
            timestamps.append(int(option.get("value", "")))
        except ValueError:
            continue
    return max(timestamps) if timestamps else None


def normalize_project_path(path: str | Path, home: Path) -> Path:
    value = str(path).replace("$USER_HOME$", str(home))
    return Path(value).expanduser().resolve()


def app_path_for_settings_directory(
    ide: JetBrainsIDE,
    settings_directory_name: str,
) -> Path | None:
    for app_path in installed_app_paths_for_bundle_id(ide.bundle_id):
        if app_data_directory_name(app_path) == settings_directory_name:
            return app_path

    running_apps = running_app_paths_for_bundle_id(ide.bundle_id)
    for app_path in running_apps:
        if app_data_directory_name(app_path) == settings_directory_name:
            return app_path
    return running_apps[0] if running_apps else None


def installed_app_paths_for_bundle_id(bundle_id: str) -> tuple[Path, ...]:
    try:
        from AppKit import NSWorkspace

        urls = (
            NSWorkspace.sharedWorkspace().URLsForApplicationsWithBundleIdentifier_(
                bundle_id
            )
            or ()
        )
        return tuple(Path(str(url.path())) for url in urls if url.path())
    except Exception:
        return ()


def running_app_paths_for_bundle_id(bundle_id: str) -> tuple[Path, ...]:
    try:
        from AppKit import NSRunningApplication

        applications = (
            NSRunningApplication.runningApplicationsWithBundleIdentifier_(bundle_id)
            or ()
        )
        return tuple(
            Path(str(url.path()))
            for application in applications
            if (url := application.bundleURL()) is not None and url.path()
        )
    except Exception:
        return ()


def app_data_directory_name(app_path: Path) -> str | None:
    try:
        product_info = app_path / "Contents" / "Resources" / "product-info.json"
        data = json.loads(product_info.read_text())
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    value = data.get("dataDirectoryName")
    return value if isinstance(value, str) else None
