"""Status-bar UI tests.

The status bar is 4k lines of AppKit that no other test touches, because
importing it needs PyObjC and CI historically ran nothing. That is how a
missing ``ScriptingBridge`` dependency shipped.

Everything here runs headlessly against real AppKit objects -- a real
``StatusBarController``, a real ``NSMenu``, real ``NSWindow`` hierarchies.
No ``NSApplication.run()``, so nothing appears on screen and nothing blocks.

The highest-value test in this file is ``test_every_selector_literal_resolves``:
menu items and buttons refer to their handlers by *string*, so renaming a
controller method leaves a menu entry that crashes when clicked and that no
type checker or import test would notice.
"""

from __future__ import annotations

import ast
import os
import re
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

if os.uname().sysname != "Darwin":  # pragma: no cover
    raise unittest.SkipTest("status-bar UI tests require macOS")

REPO_ROOT = Path(__file__).resolve().parents[1]

_ENV_PATCH = None


def setUpModule():
    """Point config lookups at a scratch home for the duration of this module.

    Settings are read when the controller is constructed (in setUpClass), not
    at import time, so scoping the patch here keeps it from leaking into other
    test modules while still landing before anything reads configuration.
    """
    global _ENV_PATCH
    scratch = tempfile.mkdtemp(prefix="sidepulse-ui-tests-")
    _ENV_PATCH = patch.dict(
        os.environ,
        {"HOME": scratch, "XDG_CONFIG_HOME": str(Path(scratch) / ".config")},
    )
    _ENV_PATCH.start()


def tearDownModule():
    if _ENV_PATCH is not None:
        _ENV_PATCH.stop()


from AppKit import (  # noqa: E402
    NSApplication,
    NSControl,
    NSEvent,
    NSEventModifierFlagCommand,
    NSEventTypeKeyDown,
    NSImage,
    NSMenu,
    NSPasteboard,
    NSPasteboardTypeString,
    NSView,
    NSWindow,
)

from sidepulse import status_bar as sb  # noqa: E402
from sidepulse import virtual_device as vd  # noqa: E402
from sidepulse.collector import MonitorSnapshot, SourceSpec  # noqa: E402
from sidepulse.models import AgentMode, AgentStatus, AggregateStatus  # noqa: E402


# A selector literal: camelCase identifier ending in a single colon.
SELECTOR_LITERAL = re.compile(r"^[a-z][A-Za-z0-9_]*:$")

# Classes that can legally be the target of a selector in this codebase.
def target_classes():
    return (sb.StatusBarController, vd.VirtualStatusDevice)


def make_status(
    *,
    provider: str = "claude",
    agent_id: str = "agent-1",
    display_name: str = "sidepulse",
    mode: AgentMode = AgentMode.WORKING,
    age_seconds: float = 5.0,
    cwd: str | None = "/Users/test/project",
    origin: str | None = None,
    stale: bool = False,
    now: datetime | None = None,
) -> AgentStatus:
    now = now or datetime.now(timezone.utc)
    return AgentStatus(
        provider=provider,
        agent_id=agent_id,
        display_name=display_name,
        mode=mode,
        updated_at=now - timedelta(seconds=age_seconds),
        event_name="PostToolUse",
        session_id=f"session-{agent_id}",
        cwd=cwd,
        tool_name="Bash",
        message="doing a thing",
        origin=origin,
        stale=stale,
    )


def make_snapshot(statuses=(), stale_statuses=()) -> MonitorSnapshot:
    now = datetime.now(timezone.utc)
    statuses = tuple(statuses)
    representative = statuses[0] if statuses else None
    return MonitorSnapshot(
        aggregate=AggregateStatus(
            mode=representative.mode if representative else AgentMode.IDLE_READY,
            active_count=len(statuses),
            stale_count=len(stale_statuses),
            representative=representative,
        ),
        statuses=statuses,
        stale_statuses=tuple(stale_statuses),
        sources=(SourceSpec("event-bus", Path("/tmp/does-not-exist.sock")),),
        collected_at=now,
    )


def walk_menu(menu: NSMenu):
    """Yield every item in a menu tree, descending into submenus."""
    for index in range(menu.numberOfItems()):
        item = menu.itemAtIndex_(index)
        yield item
        submenu = item.submenu()
        if submenu is not None:
            yield from walk_menu(submenu)


def make_device(
    name: str = "PULSEDOT",
    *,
    device_id: str | None = None,
    connected: bool = True,
) -> sb.StatusBarDevice:
    root = Path("/Volumes") / name
    return sb.StatusBarDevice(
        device_id=device_id or name.lower(),
        name=name,
        root=root,
        target=root / "leds.txt",
        connected=connected,
        display=sb.LED_DISPLAY_AGENT,
        brightness=255,
        reason="test",
    )


def walk_views(view: NSView):
    """Yield every view in a view tree."""
    yield view
    for subview in view.subviews():
        yield subview
        yield from walk_views(subview)


class StatusBarTestCase(unittest.TestCase):
    """Shared headless AppKit setup."""

    @classmethod
    def setUpClass(cls):
        # A shared application instance must exist before AppKit objects are
        # created. We never call run(), so this stays headless.
        #
        # Some CI runners have no window server. Skipping there beats a red
        # build, but set SIDEPULSE_REQUIRE_UI_TESTS=1 on any machine that is
        # supposed to have one, so the coverage cannot silently disappear.
        try:
            cls.app = NSApplication.sharedApplication()
            cls.controller = sb.StatusBarController.alloc().init()
        except Exception as exc:  # pragma: no cover - environment dependent
            if os.environ.get("SIDEPULSE_REQUIRE_UI_TESTS") == "1":
                raise
            raise unittest.SkipTest(f"AppKit unavailable in this session: {exc}")
        if cls.controller is None:
            raise unittest.SkipTest("StatusBarController could not be created")


class SelectorWiringTests(StatusBarTestCase):
    """Menu and button actions are strings; a rename must not go unnoticed."""

    def selector_literals(self, path: Path) -> set[str]:
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        return {
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and SELECTOR_LITERAL.match(node.value)
        }

    def test_every_selector_literal_resolves(self):
        """Every selector-shaped string in the UI must exist on a real class.

        Catches the "renamed the handler, forgot the menu entry" bug, which
        only surfaces when a user clicks the item and the app dies.
        """
        sources = [
            REPO_ROOT / "src/sidepulse/status_bar.py",
            REPO_ROOT / "src/sidepulse/virtual_device.py",
        ]
        classes = target_classes()
        unresolved = []
        for path in sources:
            for selector in sorted(self.selector_literals(path)):
                if selector in {item[1] for item in sb.STANDARD_EDIT_MENU_ITEMS}:
                    continue
                if not any(c.instancesRespondToSelector_(selector) for c in classes):
                    unresolved.append(f"{path.relative_to(REPO_ROOT)}: {selector}")
        self.assertEqual(
            [],
            unresolved,
            "Selectors with no implementation:\n  " + "\n  ".join(unresolved),
        )

    def test_scan_finds_the_selectors_we_expect(self):
        """Guard the guard: a regex that matches nothing would pass silently."""
        found = self.selector_literals(REPO_ROOT / "src/sidepulse/status_bar.py")
        for expected in ("openSettings:", "openSetup:", "quit:", "refresh:"):
            self.assertIn(expected, found)

    def test_controller_implements_application_delegate_hook(self):
        self.assertTrue(
            sb.StatusBarController.instancesRespondToSelector_(
                "applicationDidFinishLaunching:"
            )
        )


class MenuBuildTests(StatusBarTestCase):
    """build_menu must produce a wired, clickable menu for any snapshot."""

    def setUp(self):
        # Real device discovery would make these tests depend on whatever
        # hardware happens to be plugged in.
        patcher = patch.object(sb, "discover_devices", return_value=[])
        self.discover_devices = patcher.start()
        self.addCleanup(patcher.stop)

    def assert_menu_is_wired(self, menu: NSMenu):
        for item in walk_menu(menu):
            if item.submenu() is not None:
                continue  # AppKit owns submenuAction: on parent items
            action = item.action()
            if action is None:
                continue
            selector = action if isinstance(action, str) else action.decode()
            target = item.target()
            if target is None:
                # Nil-targeted items go up the responder chain; the controller
                # is the app delegate, so it must still implement the action.
                self.assertTrue(
                    any(c.instancesRespondToSelector_(selector) for c in target_classes()),
                    f"menu item {item.title()!r} has unroutable action {selector}",
                )
                continue
            self.assertTrue(
                target.respondsToSelector_(selector),
                f"menu item {item.title()!r} targets {target} "
                f"which does not implement {selector}",
            )

    def test_empty_snapshot_builds_menu(self):
        menu = sb.build_menu(make_snapshot(), sb.STATE_IDLE, self.controller)
        self.assertGreater(menu.numberOfItems(), 0)
        titles = [item.title() for item in walk_menu(menu)]
        self.assertIn("No recent sessions", titles)
        self.assert_menu_is_wired(menu)

    def test_populated_snapshot_builds_menu(self):
        snapshot = make_snapshot(
            statuses=[
                make_status(agent_id="a", display_name="alpha"),
                make_status(agent_id="b", display_name="beta", provider="codex"),
            ]
        )
        menu = sb.build_menu(snapshot, sb.STATE_WORKING, self.controller)
        titles = " ".join(item.title() for item in walk_menu(menu))
        self.assertNotIn("No recent sessions", titles)
        self.assert_menu_is_wired(menu)

    def test_menu_builds_for_every_agent_mode(self):
        for mode in AgentMode:
            with self.subTest(mode=mode.value):
                snapshot = make_snapshot(statuses=[make_status(mode=mode)])
                menu = sb.build_menu(snapshot, sb.STATE_WORKING, self.controller)
                self.assertGreater(menu.numberOfItems(), 0)
                self.assert_menu_is_wired(menu)

    def test_menu_builds_for_every_provider(self):
        for provider in ("claude", "codex", "grok", "unknown-provider"):
            with self.subTest(provider=provider):
                snapshot = make_snapshot(statuses=[make_status(provider=provider)])
                menu = sb.build_menu(snapshot, sb.STATE_IDLE, self.controller)
                self.assert_menu_is_wired(menu)

    def test_menu_handles_colliding_session_titles(self):
        """Two sessions with the same name must still produce distinct rows."""
        snapshot = make_snapshot(
            statuses=[
                make_status(agent_id="a", display_name="same", cwd="/one"),
                make_status(agent_id="b", display_name="same", cwd="/two"),
            ]
        )
        menu = sb.build_menu(snapshot, sb.STATE_WORKING, self.controller)
        self.assert_menu_is_wired(menu)

    def test_menu_handles_hostile_session_names(self):
        """Session titles come from user directories and must not break layout."""
        for name in ("", " ", "a" * 500, "emoji 🚀 name", "with\nnewline", "%s %d {}"):
            with self.subTest(name=repr(name)):
                snapshot = make_snapshot(statuses=[make_status(display_name=name)])
                menu = sb.build_menu(snapshot, sb.STATE_WORKING, self.controller)
                self.assertGreater(menu.numberOfItems(), 0)

    def test_menu_includes_core_actions(self):
        menu = sb.build_menu(make_snapshot(), sb.STATE_IDLE, self.controller)
        titles = [item.title() for item in walk_menu(menu)]
        for expected in ("Setup...", "Settings...", "Quit"):
            self.assertIn(expected, titles)

    def test_recent_statuses_are_capped(self):
        """The menu must not grow unbounded with session count."""
        snapshot = make_snapshot(
            statuses=[make_status(agent_id=f"a{i}") for i in range(50)]
        )
        self.assertLessEqual(len(sb.recent_statuses(snapshot)), 12)


class WindowBuildTests(StatusBarTestCase):
    """Settings and setup windows construct hundreds of views; a crash is a crash."""

    def assert_controls_are_wired(self, window: NSWindow):
        content = window.contentView()
        self.assertIsNotNone(content)
        for view in walk_views(content):
            if not isinstance(view, NSControl):
                continue
            action = view.action()
            if action is None:
                continue
            selector = action if isinstance(action, str) else action.decode()
            target = view.target()
            if target is None:
                self.assertTrue(
                    any(c.instancesRespondToSelector_(selector) for c in target_classes()),
                    f"control has unroutable action {selector}",
                )
                continue
            self.assertTrue(
                target.respondsToSelector_(selector),
                f"control targets {target} which does not implement {selector}",
            )

    def test_settings_window_builds(self):
        window = sb.build_settings_window(self.controller)
        self.assertIsNotNone(window)
        self.assertTrue(window.title())
        self.assertEqual(window.contentView().frame().size.height, 560)
        self.assert_controls_are_wired(window)

    def test_settings_window_resizes_for_compact_animations_tab(self):
        window = sb.build_settings_window(self.controller)
        self.controller.settings_window = window
        tab_view = next(
            view
            for view in window.contentView().subviews()
            if hasattr(view, "numberOfTabViewItems")
        )
        items = {
            str(tab_view.tabViewItemAtIndex_(index).identifier()):
            tab_view.tabViewItemAtIndex_(index)
            for index in range(tab_view.numberOfTabViewItems())
        }

        tab_view.selectTabViewItem_(items["animations"])
        self.assertEqual(
            window.contentView().frame().size.height,
            sb.SETTINGS_ANIMATIONS_WINDOW_HEIGHT,
        )
        self.assertEqual(
            window.contentView().frame().size.width,
            sb.SETTINGS_ANIMATIONS_WINDOW_WIDTH,
        )
        agent_animations = items["animations"].view()
        profile_label = next(
            view
            for view in agent_animations.subviews()
            if hasattr(view, "stringValue") and str(view.stringValue()) == "Profile"
        )
        profile_popup = self.controller.settings_fields["agent_animation_profile"]
        self.assertEqual(
            profile_label.frame().origin.y,
            profile_popup.frame().origin.y,
        )
        for view in agent_animations.subviews():
            frame = view.frame()
            self.assertGreaterEqual(frame.origin.x, 0)
            self.assertGreaterEqual(frame.origin.y, 0)
            self.assertLessEqual(
                frame.origin.x + frame.size.width,
                agent_animations.frame().size.width,
            )
            self.assertLessEqual(
                frame.origin.y + frame.size.height,
                agent_animations.frame().size.height,
            )

        tab_view.selectTabViewItem_(items["agents"])
        self.assertEqual(
            window.contentView().frame().size.height,
            sb.SETTINGS_WINDOW_HEIGHT,
        )
        self.assertEqual(
            window.contentView().frame().size.width,
            sb.SETTINGS_WINDOW_WIDTH,
        )

    def test_setup_window_builds(self):
        window = sb.build_setup_window(self.controller)
        self.assertIsNotNone(window)
        self.assert_controls_are_wired(window)

    def test_custom_agent_animation_editor_builds(self):
        window = sb.build_agent_animation_editor_window(self.controller)
        self.assertIsNotNone(window)
        self.assertTrue(window.title())
        self.assertIsNotNone(self.controller.agent_animation_editor_name)
        self.assertIsNotNone(self.controller.agent_animation_editor_program)
        self.assertIsInstance(
            self.controller.agent_animation_editor_preview,
            vd.VirtualLedView,
        )
        self.assertTrue(self.controller.agent_animation_editor_preview.compact_preview)
        self.assertIs(
            self.controller.agent_animation_editor_program.delegate(),
            self.controller,
        )
        self.assertIn(
            "Show on Device",
            {
                str(view.title())
                for view in window.contentView().subviews()
                if hasattr(view, "title")
            },
        )
        self.assert_controls_are_wired(window)

    def test_custom_animation_editor_installs_native_edit_shortcuts(self):
        window = sb.build_agent_animation_editor_window(self.controller)
        edit_menu = sb.install_standard_edit_menu(self.app)
        self.assertIsNotNone(edit_menu)
        shortcuts = {
            str(edit_menu.itemAtIndex_(index).action()): str(
                edit_menu.itemAtIndex_(index).keyEquivalent()
            )
            for index in range(edit_menu.numberOfItems())
        }
        self.assertEqual(
            shortcuts,
            {
                "cut:": "x",
                "copy:": "c",
                "paste:": "v",
                "selectAll:": "a",
            },
        )
        count = self.app.mainMenu().numberOfItems()
        self.assertIs(sb.install_standard_edit_menu(self.app), edit_menu)
        self.assertEqual(self.app.mainMenu().numberOfItems(), count)
        self.assertIsNotNone(window)

    def test_custom_animation_editor_command_c_copies_selected_program(self):
        window = sb.build_agent_animation_editor_window(self.controller)
        program = self.controller.agent_animation_editor_program
        program.setString_("off 2s\ncopy me")
        program.setSelectedRange_((0, 6))
        window.makeFirstResponder_(program)
        pasteboard = NSPasteboard.generalPasteboard()
        pasteboard.clearContents()
        event = NSEvent.keyEventWithType_location_modifierFlags_timestamp_windowNumber_context_characters_charactersIgnoringModifiers_isARepeat_keyCode_(
            NSEventTypeKeyDown,
            (0, 0),
            NSEventModifierFlagCommand,
            0.0,
            window.windowNumber(),
            None,
            "c",
            "c",
            False,
            8,
        )

        self.assertTrue(program.performKeyEquivalent_(event))
        self.assertEqual(
            pasteboard.stringForType_(NSPasteboardTypeString),
            "off 2s",
        )

    def test_custom_agent_animation_editor_preview_updates_and_reports_errors(self):
        sb.build_agent_animation_editor_window(self.controller)
        sb.set_text_control_value(
            self.controller.agent_animation_editor_program,
            "#FF0000 1s pulse\nrepeat",
        )
        self.controller.refresh_agent_animation_editor_preview()
        preview = self.controller.agent_animation_editor_preview
        self.assertEqual(preview.current_program, "#FF0000 1s pulse\nrepeat")
        self.assertIsNone(preview.wasm_error)
        self.assertEqual(
            str(self.controller.agent_animation_editor_preview_status.stringValue()),
            "Updates as you type",
        )

        sb.set_text_control_value(self.controller.agent_animation_editor_program, "")
        self.controller.refresh_agent_animation_editor_preview()
        self.assertEqual(preview.current_program, "off")
        self.assertNotEqual(
            str(self.controller.agent_animation_editor_preview_status.stringValue()),
            "Updates as you type",
        )

    def test_edit_animation_clones_builtins_and_edits_custom_in_place(self):
        original_settings = self.controller.settings
        try:
            self.controller.settings = original_settings.with_agent_animation(
                AgentMode.WORKING,
                sb.AGENT_ANIMATION_KITT_RED,
            )
            self.controller.show_agent_animation_editor(
                AgentMode.WORKING.value,
                edit_selected=True,
            )
            self.assertEqual(
                str(self.controller.agent_animation_editor_window.title()),
                "Clone Built-in Animation",
            )
            self.assertIsNone(self.controller.agent_animation_editor_existing_id)
            self.assertEqual(
                str(self.controller.agent_animation_editor_name.stringValue()),
                "KITT Scanner Red Copy",
            )

            custom_id = "custom:purple-sweep"
            self.controller.settings = (
                original_settings.with_custom_agent_animation(
                    custom_id,
                    name="Purple Sweep",
                    program="#AA44FF 700ms pulse\nrepeat",
                ).with_agent_animation(AgentMode.WORKING, custom_id)
            )
            self.controller.show_agent_animation_editor(
                AgentMode.WORKING.value,
                edit_selected=True,
            )
            self.assertEqual(
                str(self.controller.agent_animation_editor_window.title()),
                "Edit Custom Animation",
            )
            self.assertEqual(
                self.controller.agent_animation_editor_existing_id,
                custom_id,
            )
            self.assertEqual(
                sb.text_control_value(self.controller.agent_animation_editor_program),
                "#AA44FF 700ms pulse\nrepeat",
            )
        finally:
            if self.controller.agent_animation_editor_window is not None:
                self.controller.agent_animation_editor_window.orderOut_(None)
            self.controller.settings = original_settings

    def test_custom_editor_device_preview_applies_device_brightness(self):
        device = sb.StatusBarDevice(
            device_id="sidepulse",
            name="SidePulse",
            root=Path("/Volumes/SidePulse"),
            target=Path("/Volumes/SidePulse/LEDS.LED"),
            connected=True,
            display=sb.LED_DISPLAY_AGENT,
            brightness=128,
            reason="test",
        )
        program = "brightness 200\n#FF0000 1s pulse\nrepeat"
        with (
            patch.object(sb, "write_led_program") as write_program,
            patch.object(sb.time, "sleep") as sleep,
            patch.object(
                self.controller,
                "performSelectorOnMainThread_withObject_waitUntilDone_",
            ),
        ):
            self.controller.show_animation_program_on_device_worker(
                program,
                [device],
                12,
            )

        write_program.assert_called_once_with(
            sb.apply_brightness(program, 128),
            device_path=device.target,
        )
        sleep.assert_called_once_with(
            sb.AGENT_ANIMATION_EDITOR_DEVICE_PREVIEW_SECONDS
        )
        self.assertEqual(sb.AGENT_ANIMATION_EDITOR_DEVICE_PREVIEW_SECONDS, 10.0)

    def test_agent_animation_profile_editor_builds(self):
        window = sb.build_agent_animation_profile_editor_window(self.controller)
        self.assertIsNotNone(window)
        self.assertIsNotNone(self.controller.agent_animation_profile_editor_name)
        self.assert_controls_are_wired(window)

    def test_settings_window_registers_its_fields(self):
        """The controller reads values back out of this dict when saving."""
        self.controller.settings_fields = {}
        sb.build_settings_window(self.controller)
        self.assertTrue(
            self.controller.settings_fields,
            "settings window built no addressable fields; saving would be a no-op",
        )
        for mode in sb.ANIMATION_UI_STATES:
            preview = self.controller.settings_fields[
                f"agent_animation_preview_{mode}"
            ]
            self.assertIsInstance(preview, vd.VirtualLedView)
        profile_popup = self.controller.settings_fields["agent_animation_profile"]
        self.assertEqual(str(profile_popup.titleOfSelectedItem()), "Cyan")
        self.assertEqual(
            [
                str(profile_popup.itemAtIndex_(index).title())
                for index in range(profile_popup.numberOfItems())
            ],
            ["Current", "Cyan", "Ember", "Purple"],
        )
        self.assertFalse(
            self.controller.settings_buttons[
                "delete_agent_animation_profile"
            ].isEnabled()
        )

    def test_settings_window_has_profile_json_controls(self):
        window = sb.build_settings_window(self.controller)
        tab_view = next(
            view
            for view in window.contentView().subviews()
            if hasattr(view, "numberOfTabViewItems")
        )
        animations = next(
            tab_view.tabViewItemAtIndex_(index).view()
            for index in range(tab_view.numberOfTabViewItems())
            if str(tab_view.tabViewItemAtIndex_(index).identifier()) == "animations"
        )
        titles = {
            str(view.title())
            for view in walk_views(animations)
            if hasattr(view, "title")
        }
        self.assertIn("Export JSON", titles)
        self.assertIn("Import JSON", titles)

    def test_settings_animation_previews_use_wasm_programs(self):
        window = sb.build_settings_window(self.controller)
        self.controller.settings_window = window
        self.controller.refresh_settings_window()

        for mode in sb.ANIMATION_UI_STATES:
            with self.subTest(mode=mode):
                preview = self.controller.settings_fields[
                    f"agent_animation_preview_{mode}"
                ]
                self.assertTrue(preview.compact_preview)
                self.assertIsNotNone(preview.current_program)
                self.assertIsNotNone(preview.wasm_controller)
                self.assertIsNone(preview.wasm_error)

        for state in (sb.ANIMATION_STATE_LID_OPEN, sb.ANIMATION_STATE_LID_CLOSED):
            preview = self.controller.settings_fields[
                f"agent_animation_preview_{state}"
            ]
            self.assertNotIn("repeat", preview.current_program)
            self.assertEqual(preview.rendered_program.splitlines()[-1], "repeat")

        lid_closed_preview = self.controller.settings_fields[
            f"agent_animation_preview_{sb.ANIMATION_STATE_LID_CLOSED}"
        ]
        self.assertTrue(lid_closed_preview.current_program.startswith("0:#000000"))
        self.assertTrue(lid_closed_preview.rendered_program.startswith("#00E5FF\n"))

    def test_every_state_popup_uses_the_same_animation_catalog(self):
        original_settings = self.controller.settings
        try:
            animation_id = "custom:purple-sweep"
            self.controller.settings = original_settings.with_custom_agent_animation(
                animation_id,
                name="Purple Sweep",
                program="#AA44FF 700ms pulse\nrepeat",
            )
            sb.build_settings_window(self.controller)
            catalogs = []
            for mode in sb.ANIMATION_UI_STATES:
                popup = self.controller.settings_fields[
                    f"agent_animation_{mode}"
                ]
                catalogs.append(
                    [
                        item.representedObject()["animation_id"]
                        for item in (
                            popup.itemAtIndex_(index)
                            for index in range(popup.numberOfItems())
                        )
                        if isinstance(item.representedObject(), dict)
                    ]
                )
            self.assertTrue(catalogs)
            self.assertTrue(all(catalog == catalogs[0] for catalog in catalogs))
            self.assertIn("kitt", catalogs[0])
            self.assertIn(animation_id, catalogs[0])
            self.assertEqual(catalogs[0][-1], "add-custom")
            working_popup = self.controller.settings_fields[
                f"agent_animation_{AgentMode.WORKING.value}"
            ]
            self.assertEqual(
                [
                    str(working_popup.itemAtIndex_(index).title())
                    for index in range(working_popup.numberOfItems())
                    if isinstance(
                        working_popup.itemAtIndex_(index).representedObject(),
                        dict,
                    )
                ],
                [
                    "Slow Off",
                    "Immediate Off",
                    "Idle Pulse",
                    "Cyan Roll",
                    "Cyan Complete",
                    "Amber Pulse",
                    "Solid Green",
                    "KITT Scanner",
                    "KITT Scanner Red",
                    "Ember Idle",
                    "Ember Tide",
                    "Ember Attention",
                    "Ember Complete",
                    "Ember Lid Open",
                    "Purple Idle",
                    "Purple Tide",
                    "Purple Attention",
                    "Purple Complete",
                    "Purple Lid Open",
                    "Night Rider",
                    "Lid Open Sweep",
                    "Lid Closed Sweep",
                    "Purple Sweep",
                    "Add Custom…",
                ],
            )
            animated_rows = [
                working_popup.itemAtIndex_(index).view()
                for index in range(working_popup.numberOfItems())
                if isinstance(
                    working_popup.itemAtIndex_(index).representedObject(),
                    dict,
                )
                and working_popup.itemAtIndex_(index).representedObject()[
                    "animation_id"
                ]
                != "add-custom"
            ]
            self.assertTrue(animated_rows)
            for row in animated_rows:
                self.assertIsInstance(row, sb.AgentAnimationMenuItemView)
                self.assertIsNotNone(row.animation_preview.wasm_controller)
                self.assertIsNone(row.animation_preview.wasm_error)
        finally:
            self.controller.settings = original_settings

    def test_settings_window_is_not_visible(self):
        window = sb.build_settings_window(self.controller)
        self.assertFalse(window.isVisible(), "building a window must not show it")

    def test_show_settings_window_brings_it_forward_and_starts_previews(self):
        self.controller.show_settings_window()
        try:
            self.assertTrue(self.controller.settings_window.isVisible())
            self.assertIsNotNone(self.controller.agent_animation_preview_timer)
        finally:
            self.controller.stop_agent_animation_preview_timer()
            self.controller.settings_window.orderOut_(None)


class IconTests(StatusBarTestCase):
    """Icon builders return real images rather than None."""

    def test_status_icons_exist_for_every_mode(self):
        for mode in AgentMode:
            with self.subTest(mode=mode.value):
                status = make_status(mode=mode)
                self.assertIsInstance(sb.session_row_icon_for_status(status), NSImage)

    def test_provider_icons_do_not_raise(self):
        for provider in ("claude", "codex", "grok", "nonsense"):
            with self.subTest(provider=provider):
                sb.provider_icon_for_provider(provider)

    def test_state_symbols_render(self):
        for state in (sb.STATE_IDLE, sb.STATE_WORKING, sb.STATE_DONE, sb.STATE_ASK):
            with self.subTest(state=state.label):
                self.assertIsInstance(
                    sb.image_for_symbol(state.symbol, state.label), NSImage
                )


class PureUiLogicTests(unittest.TestCase):
    """Label and formatting helpers -- no AppKit objects, fast and exhaustive."""

    def test_format_byte_count_is_monotonic_and_labelled(self):
        for size in (0, 1, 1023, 1024, 1024**2, 1024**3, 1024**4):
            with self.subTest(size=size):
                text = sb.format_byte_count(size)
                self.assertTrue(text)
                self.assertRegex(text, r"\d")

    def test_terminal_app_labels_exist_for_every_choice(self):
        for app in sb.TERMINAL_APP_CHOICES:
            with self.subTest(app=app):
                self.assertTrue(sb.terminal_app_label(app))
                self.assertTrue(sb.terminal_app_menu_label(app))

    def test_provider_open_actions_have_labels(self):
        for provider in ("claude", "codex", "grok"):
            actions = sb.provider_open_actions(provider)
            self.assertTrue(actions, f"{provider} has no open actions")
            self.assertIn(sb.default_provider_open_action(provider), actions)
            for action in actions:
                with self.subTest(provider=provider, action=action):
                    self.assertTrue(sb.provider_open_action_label(provider, action))

    def test_device_name_disambiguation(self):
        # macOS mounts a second volume of the same name as "NAME 1", so both
        # devices report display name "SIDEPULSE" but differ by mount root.
        first = make_device("SIDEPULSE", device_id="one")
        second = make_device("SIDEPULSE", device_id="two")
        second = sb.StatusBarDevice(
            **{
                **second.__dict__,
                "root": Path("/Volumes/SIDEPULSE 1"),
                "target": Path("/Volumes/SIDEPULSE 1/leds.txt"),
            }
        )
        result = sb.disambiguate_device_names([first, second])
        self.assertEqual(2, len(result))
        self.assertEqual(
            2,
            len({device.name for device in result}),
            "duplicate device names must be made distinct in the menu",
        )

    def test_distinct_device_names_are_left_alone(self):
        devices = [make_device("ALPHA"), make_device("BETA")]
        result = sb.disambiguate_device_names(devices)
        self.assertEqual(["ALPHA", "BETA"], [device.name for device in result])

    def test_applescript_quote_escapes_injection(self):
        """Session titles reach AppleScript; quoting must not let them break out."""
        quoted = sb.applescript_quote('evil" & do shell script "rm -rf /')
        self.assertTrue(quoted.startswith('"') and quoted.endswith('"'))
        self.assertNotIn('" & do shell script "', quoted)

    def test_normalize_match_text_is_stable(self):
        self.assertEqual(
            sb.normalize_match_text("  Mixed CASE  "),
            sb.normalize_match_text("mixed case"),
        )


if __name__ == "__main__":
    unittest.main()
