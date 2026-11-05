from __future__ import annotations

import os
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

from sidepulse.jetbrains import JETBRAINS_IDES, jetbrains_ide_for_project


class JetBrainsIDESelectionTests(unittest.TestCase):
    def test_recent_project_and_its_ancestor_select_the_ide(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            project = home / "work" / "project" / "module"
            self.write_recent_projects(
                home,
                "JetBrains",
                "IntelliJIdea2025.1",
                ["$USER_HOME$/work/project"],
            )

            ide = jetbrains_ide_for_project(project, home=home)

        self.assertEqual(ide, JETBRAINS_IDES[0])

    def test_most_recent_matching_ide_settings_file_wins(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            project = home / "work" / "project"
            intellij = self.write_recent_projects(
                home, "JetBrains", "IntelliJIdea2025.1", [str(project)]
            )
            pycharm = self.write_recent_projects(
                home, "JetBrains", "PyCharm2025.1", [str(project)]
            )
            intellij.touch()
            pycharm.touch()
            intellij_time = intellij.stat().st_mtime - 20
            pycharm_time = intellij.stat().st_mtime - 10
            os.utime(intellij, (intellij_time, intellij_time))
            os.utime(pycharm, (pycharm_time, pycharm_time))

            ide = jetbrains_ide_for_project(project, home=home)

        self.assertEqual(ide, JETBRAINS_IDES[1])

    def test_android_studio_recent_projects_are_scanned(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            project = home / "android" / "app"
            self.write_recent_projects(
                home,
                "Google",
                "AndroidStudio2025.1",
                [str(project)],
            )

            ide = jetbrains_ide_for_project(project, home=home)

        self.assertEqual(ide.name, "Android Studio")

    def test_first_installed_ide_is_fallback_without_recent_project_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            with patch(
                "sidepulse.jetbrains.ide_is_installed",
                side_effect=lambda bundle_id: bundle_id == JETBRAINS_IDES[1].bundle_id,
            ):
                ide = jetbrains_ide_for_project(home / "project", home=home)

        self.assertEqual(ide, JETBRAINS_IDES[1])

    def test_returns_none_when_no_ide_is_installed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch("sidepulse.jetbrains.ide_is_installed", return_value=False):
                ide = jetbrains_ide_for_project(Path(tmp) / "project", home=Path(tmp))

        self.assertIsNone(ide)

    def test_malformed_recent_projects_file_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            recent = (
                home
                / "Library"
                / "Application Support"
                / "JetBrains"
                / "IntelliJIdea2025.1"
                / "options"
                / "recentProjects.xml"
            )
            recent.parent.mkdir(parents=True)
            recent.write_text("<application>")
            with patch("sidepulse.jetbrains.ide_is_installed", return_value=False):
                ide = jetbrains_ide_for_project(home / "project", home=home)

        self.assertIsNone(ide)

    def write_recent_projects(
        self,
        home: Path,
        vendor: str,
        settings_dir: str,
        project_paths: list[str],
    ) -> Path:
        recent = (
            home
            / "Library"
            / "Application Support"
            / vendor
            / settings_dir
            / "options"
            / "recentProjects.xml"
        )
        recent.parent.mkdir(parents=True, exist_ok=True)
        root = ET.Element("application")
        component = ET.SubElement(root, "component", name="RecentProjectsManager")
        paths = ET.SubElement(component, "option", name="recentPaths")
        path_list = ET.SubElement(paths, "list")
        for project_path in project_paths:
            ET.SubElement(path_list, "option", value=project_path)
        ET.ElementTree(root).write(recent, encoding="unicode")
        return recent


if __name__ == "__main__":
    unittest.main()