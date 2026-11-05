from __future__ import annotations

import json
import os
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

from sidepulse.jetbrains import (
    JETBRAINS_IDES,
    jetbrains_ide_for_project,
    jetbrains_launch_for_project,
)


class JetBrainsIDESelectionTests(unittest.TestCase):
    def test_recent_project_and_its_ancestor_select_the_ide(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            project = home / "work" / "project" / "module"
            self.write_recent_projects(
                home,
                "JetBrains",
                "IntelliJIdea2025.1",
                {
                    "$USER_HOME$/work/project": 100,
                    "$APPLICATION_CONFIG_DIR$/light-edit": 1000,
                },
            )

            ide = jetbrains_ide_for_project(project, home=home)

        self.assertEqual(ide, JETBRAINS_IDES[0])

    def test_exact_project_beats_ancestor_in_another_ide(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            project = home / "work" / "project"
            self.write_recent_projects(
                home,
                "JetBrains",
                "IntelliJIdea2025.1",
                {str(project.parent): 900},
            )
            self.write_recent_projects(
                home,
                "JetBrains",
                "PyCharm2025.1",
                {str(project): 100},
            )

            ide = jetbrains_ide_for_project(project, home=home)

        self.assertEqual(ide, JETBRAINS_IDES[1])

    def test_newer_activation_timestamp_wins_for_same_project(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            project = home / "work" / "project"
            self.write_recent_projects(
                home,
                "JetBrains",
                "IntelliJIdea2025.1",
                {str(project): 100},
            )
            self.write_recent_projects(
                home,
                "JetBrains",
                "PyCharm2025.1",
                {str(project): 200},
            )

            ide = jetbrains_ide_for_project(project, home=home)

        self.assertEqual(ide, JETBRAINS_IDES[1])

    def test_project_open_timestamp_is_used_when_activation_timestamp_is_missing(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            project = home / "work" / "project"
            self.write_recent_projects(
                home,
                "JetBrains",
                "IntelliJIdea2025.1",
                {str(project): 100},
                timestamp_name="projectOpenTimestamp",
            )

            ide = jetbrains_ide_for_project(project, home=home)

        self.assertEqual(ide, JETBRAINS_IDES[0])

    def test_file_mtime_fallback_uses_milliseconds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            project = home / "work" / "project"
            intellij = self.write_recent_projects(
                home,
                "JetBrains",
                "IntelliJIdea2025.1",
                {str(project): None},
            )
            self.write_recent_projects(
                home,
                "JetBrains",
                "PyCharm2025.1",
                {str(project): 3000},
            )
            os.utime(intellij, (2, 2))

            ide = jetbrains_ide_for_project(project, home=home)

        self.assertEqual(ide, JETBRAINS_IDES[1])

    def test_community_edition_settings_directory_is_distinct(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            project = home / "work" / "project"
            self.write_recent_projects(
                home,
                "JetBrains",
                "IdeaIC2025.1",
                {str(project): 100},
            )

            ide = jetbrains_ide_for_project(project, home=home)

        self.assertEqual(ide.settings_prefix, "IdeaIC")
        self.assertEqual(ide.bundle_id, "com.jetbrains.intellij.ce")

    def test_pycharm_community_settings_directory_is_distinct(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            project = home / "work" / "project"
            self.write_recent_projects(
                home,
                "JetBrains",
                "PyCharmCE2025.1",
                {str(project): 100},
            )

            ide = jetbrains_ide_for_project(project, home=home)

        self.assertEqual(ide.settings_prefix, "PyCharmCE")
        self.assertEqual(ide.bundle_id, "com.jetbrains.pycharm.ce")

    def test_matching_settings_directory_selects_matching_app_installation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            project = home / "work" / "project"
            settings_dir = "IntelliJIdea2025.1"
            self.write_recent_projects(
                home,
                "JetBrains",
                settings_dir,
                {str(project): 100},
            )
            wrong_app = home / "IntelliJ IDEA 2026.3.app"
            matching_app = home / "IntelliJ IDEA 2025.1.app"
            self.write_product_info(wrong_app, "IntelliJIdea2026.3")
            self.write_product_info(matching_app, settings_dir)
            with (
                patch(
                    "sidepulse.jetbrains.installed_app_paths_for_bundle_id",
                    return_value=(wrong_app, matching_app),
                ),
                patch(
                    "sidepulse.jetbrains.running_app_paths_for_bundle_id",
                    return_value=(),
                ),
            ):
                launch = jetbrains_launch_for_project(project, home=home)

        self.assertEqual(launch.app_path, matching_app)

    def test_running_app_is_fallback_when_installed_app_settings_do_not_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            project = home / "work" / "project"
            self.write_recent_projects(
                home,
                "JetBrains",
                "IntelliJIdea2025.1",
                {str(project): 100},
            )
            installed_app = home / "IntelliJ IDEA 2026.3.app"
            running_app = home / "IntelliJ IDEA 2025.1.app"
            self.write_product_info(installed_app, "IntelliJIdea2026.3")
            self.write_product_info(running_app, "IntelliJIdea2025.1")
            with (
                patch(
                    "sidepulse.jetbrains.installed_app_paths_for_bundle_id",
                    return_value=(installed_app,),
                ),
                patch(
                    "sidepulse.jetbrains.running_app_paths_for_bundle_id",
                    return_value=(running_app,),
                ),
            ):
                launch = jetbrains_launch_for_project(project, home=home)

        self.assertEqual(launch.app_path, running_app)

    def test_missing_product_info_keeps_bundle_identifier_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            project = home / "work" / "project"
            self.write_recent_projects(
                home,
                "JetBrains",
                "IntelliJIdea2025.1",
                {str(project): 100},
            )
            app_path = home / "IntelliJ IDEA.app"
            with (
                patch(
                    "sidepulse.jetbrains.installed_app_paths_for_bundle_id",
                    return_value=(app_path,),
                ),
                patch(
                    "sidepulse.jetbrains.running_app_paths_for_bundle_id",
                    return_value=(),
                ),
            ):
                launch = jetbrains_launch_for_project(project, home=home)

        self.assertIsNone(launch.app_path)

    def test_android_studio_recent_projects_are_scanned(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            project = home / "android" / "app"
            self.write_recent_projects(
                home,
                "Google",
                "AndroidStudio2025.1",
                {str(project): 100},
            )

            ide = jetbrains_ide_for_project(project, home=home)

        self.assertEqual(ide.name, "Android Studio")

    def test_legacy_recent_paths_format_is_supported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            project = home / "legacy-project"
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
            root = ET.Element("application")
            component = ET.SubElement(
                root, "component", name="RecentProjectsManager"
            )
            paths = ET.SubElement(component, "option", name="recentPaths")
            path_list = ET.SubElement(paths, "list")
            ET.SubElement(path_list, "option", value=str(project))
            ET.ElementTree(root).write(recent, encoding="unicode")

            ide = jetbrains_ide_for_project(project, home=home)

        self.assertEqual(ide, JETBRAINS_IDES[0])

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
        project_paths: dict[str, int | None],
        *,
        timestamp_name: str = "activationTimestamp",
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
        additional_info = ET.SubElement(
            component, "option", name="additionalInfo"
        )
        project_map = ET.SubElement(additional_info, "map")
        for project_path, activation_timestamp in project_paths.items():
            entry = ET.SubElement(project_map, "entry", key=project_path)
            if activation_timestamp is None:
                continue
            metadata = ET.SubElement(entry, "RecentProjectMetaInfo")
            ET.SubElement(
                metadata,
                "option",
                name=timestamp_name,
                value=str(activation_timestamp),
            )
        ET.ElementTree(root).write(recent, encoding="unicode")
        return recent

    def write_product_info(self, app_path: Path, data_directory_name: str) -> None:
        product_info = app_path / "Contents" / "Resources" / "product-info.json"
        product_info.parent.mkdir(parents=True)
        product_info.write_text(json.dumps({"dataDirectoryName": data_directory_name}))


if __name__ == "__main__":
    unittest.main()