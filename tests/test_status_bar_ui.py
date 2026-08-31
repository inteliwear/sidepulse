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
    NSImage,
    NSMenu,
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
        self.assert_controls_are_wired(window)

    def test_setup_window_builds(self):
        window = sb.build_setup_window(self.controller)
        self.assertIsNotNone(window)
        self.assert_controls_are_wired(window)

    def test_settings_window_registers_its_fields(self):
        """The controller reads values back out of this dict when saving."""
        self.controller.settings_fields = {}
        sb.build_settings_window(self.controller)
        self.assertTrue(
            self.controller.settings_fields,
            "settings window built no addressable fields; saving would be a no-op",
        )
        for key in (
            "dnd_start_time",
            "dnd_end_time",
            "dnd_status",
        ):
            self.assertIn(key, self.controller.settings_fields)
        self.assertIn("dnd_enabled", self.controller.settings_buttons)
        self.assertIn("dnd_schedule", self.controller.settings_buttons)
        self.assertTrue(self.controller.settings_buttons["dnd_enabled"].isEnabled())

    def test_settings_window_is_not_visible(self):
        window = sb.build_settings_window(self.controller)
        self.assertFalse(window.isVisible(), "building a window must not show it")


class IconTests(StatusBarTestCase):
    """Icon builders return real images rather than None."""

    def test_status_icons_exist_for_every_mode(self):
        for mode in AgentMode:
            with self.subTest(mode=mode.value):
                status = make_status(mode=mode)
                self.assertIsInstance(sb.session_row_icon_for_status(status), NSImage)

    def test_provider_icons_do_not_raise(self):
        for provider in ("claude", "codex", "grok", "opencode", "nonsense"):
            with self.subTest(provider=provider):
                sb.provider_icon_for_provider(provider)

    def test_mapped_providers_look_up_their_own_bundle(self):
        """The generic fallback would absorb a deleted branch without failing.

        provider_app_icon("codex") resolves ChatGPT.app by name on its own, so removing
        the codex branch still yields an icon and no test notices. Pin the bundle that
        each explicit branch reads.
        """
        wanted = {
            "codex": "/Applications/Codex.app",
            "claude": "/Applications/Claude.app",
            "grok": "/Applications/Grok.app",
        }
        for provider, bundle in wanted.items():
            with self.subTest(provider=provider):
                looked_up: list[str] = []
                sb._provider_icon_cache.pop(provider, None)
                self.addCleanup(sb._provider_icon_cache.pop, provider, None)
                with patch.object(
                    sb, "app_icon", lambda path: looked_up.append(path) and None
                ):
                    sb.provider_icon_for_provider(provider)
                self.assertIn(bundle, looked_up)

    def test_unmapped_provider_tries_its_own_app_before_the_glyph(self):
        """The terminal glyph always returns an image, so it hides a missing lookup.

        Assert that an unmapped provider consults its own application first. Without that
        step a new agent silently renders as a bare terminal icon.
        """
        looked_up: list[str] = []
        sb._provider_icon_cache.pop("someagent", None)
        self.addCleanup(sb._provider_icon_cache.pop, "someagent", None)
        with patch.object(sb, "provider_app_icon", lambda p: looked_up.append(p) and None):
            image = sb.provider_icon_for_provider("someagent")
        self.assertEqual(looked_up, ["someagent"])
        self.assertIsInstance(image, NSImage)

    def test_provider_app_icon_resolves_an_installed_app_by_name(self):
        """Pin the name matching itself, which is what makes the generic path work."""
        installed = next(
            (
                name
                for name, path in (
                    ("opencode", "/Applications/OpenCode.app"),
                    ("claude", "/Applications/Claude.app"),
                    ("grok", "/Applications/Grok.app"),
                )
                if Path(path).exists()
            ),
            None,
        )
        if installed is None:  # pragma: no cover
            self.skipTest("no known agent application installed")
        self.assertIsInstance(sb.provider_app_icon(installed), NSImage)
        self.assertIsNone(sb.provider_app_icon("definitely-not-an-installed-app"))

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
