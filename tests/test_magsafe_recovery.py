"""Connection and recovery fixtures do not access the real LED or helper."""
import plistlib
import time
import unittest
from unittest.mock import Mock, call, patch

from sidepulse.magsafe import MagSafeController
from sidepulse.magsafe_connection import read_magsafe_connection
from sidepulse.models import AgentMode


class ConnectionReaderTests(unittest.TestCase):
    def read_ports(self, values):
        io, cf = Mock(), Mock()
        cf.CFDataCreate.return_value = 1
        cf.CFPropertyListCreateWithData.return_value = 2
        cf.CFStringCreateWithCString.return_value = 3
        cf.CFBooleanGetTypeID.return_value = 7
        cf.CFGetTypeID.side_effect = lambda p: 7 if isinstance(values[p - 200], bool) else 8
        cf.CFBooleanGetValue.side_effect = lambda p: values[p - 200]
        def match(_port, _dictionary, iterator):
            iterator._obj.value = 42
            return 0
        io.IOServiceGetMatchingServices.side_effect = match
        io.IOIteratorNext.side_effect = [*range(100, 100 + len(values)), 0]
        io.IORegistryEntryCreateCFProperty.side_effect = [
            200 + i if value is not None else None for i, value in enumerate(values)
        ]
        with (
            patch("sidepulse.magsafe_connection.sys.platform", "darwin"),
            patch("sidepulse.magsafe_connection._frameworks", return_value=(io, cf)),
        ):
            result = read_magsafe_connection()
        matching = plistlib.loads(cf.CFDataCreate.call_args.args[1])
        self.assertEqual(matching["IOPropertyMatch"], {"PortType": 17})
        self.assertEqual(io.IOObjectRelease.call_args_list,
                         [call(i) for i in range(100, 100 + len(values))] + [call(42)])
        return result

    def test_only_boolean_port_connection_is_accepted(self):
        self.assertIs(self.read_ports([False, True]), True)
        self.assertIs(self.read_ports([False, False]), False)
        self.assertIsNone(self.read_ports([]))
        self.assertIsNone(self.read_ports([None, 1]))


class Session:
    def __init__(self, fail=False):
        self.messages = []
        self.closed = False
        self.fail = fail
        self.reply = bytearray(b"RESTORED\n")

    def sendall(self, payload):
        if self.fail:
            raise BrokenPipeError("fixture helper disconnected")
        self.messages.append(payload)

    def recv(self, _size):
        return bytes([self.reply.pop(0)]) if self.reply else b""

    def close(self):
        self.closed = True


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.cable = True
        self.mode = AgentMode.WORKING
        self.sessions = []
        self.service = Mock()
        self.service.status.return_value = 1
        self.service_patch = patch("sidepulse.magsafe.native_service", return_value=self.service)
        self.service_patch.start()
        self.addCleanup(self.service_patch.stop)
        self.select_patch = patch("sidepulse.magsafe.select.select", return_value=([], [], []))
        self.select_patch.start()
        self.addCleanup(self.select_patch.stop)
        self.controller = MagSafeController(
            connection_reader=lambda: self.cable, on_reconnect=self.refresh,
            poll_seconds=0.01, retry_seconds=0.05,
        )
        def open_session():
            session = Session()
            self.sessions.append(session)
            return session
        self.opener = patch.object(self.controller, "_open_session", side_effect=open_session)
        self.opener.start()
        self.addCleanup(self.opener.stop)
        self.addCleanup(self.controller.stop)

    def refresh(self):
        self.controller.update(self.mode)

    def wait_for(self, predicate):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.005)
        self.fail(f"Recovery timed out: {self.controller.last_error}")

    def start(self):
        self.refresh()
        self.controller.start()
        self.wait_for(lambda: self.sessions and self.sessions[-1].messages)

    def test_unplug_retains_intent_and_replug_refreshes_expired_state(self):
        self.start()
        self.assertEqual(self.sessions[0].messages[0], b"working\n")
        self.cable = False
        self.wait_for(lambda: self.sessions[0].closed)
        self.assertIn(b"quit\n", self.sessions[0].messages)
        self.assertFalse(self.controller.ready)
        self.assertTrue(self.controller._thread.is_alive())
        self.assertEqual(self.controller.status_reason, "MagSafe disconnected")
        # The old cached Working state must not be sent after reconnection.
        self.mode = AgentMode.IDLE_READY
        self.cable = True
        self.wait_for(lambda: len(self.sessions) == 2 and self.sessions[1].messages)
        self.assertEqual(self.sessions[1].messages[0], b"idle\n")
        self.service.registerAndReturnError_.assert_not_called()

    def test_unknown_connection_pauses_until_known_connected(self):
        self.cable = None
        self.refresh()
        self.controller.start()
        self.wait_for(lambda: self.controller._observed_connection)
        self.assertEqual(self.sessions, [])
        self.cable = True
        self.wait_for(lambda: bool(self.sessions and self.sessions[0].messages))

    def test_disable_does_not_restart_when_cable_returns(self):
        self.start()
        self.controller.stop()
        self.cable = False
        self.cable = True
        self.assertEqual(len(self.sessions), 1)
        self.assertFalse(self.controller.ready)
        self.assertIsNone(self.controller._thread)

    def test_wake_restarts_with_current_sidepulse_state(self):
        self.start()
        self.controller.stop()
        self.mode = AgentMode.WAITING_FOR_INPUT
        self.controller.start()
        self.wait_for(lambda: len(self.sessions) == 2 and self.sessions[1].messages)
        self.assertEqual(self.sessions[1].messages[0], b"ask\n")

    def test_helper_failure_retries_without_registration(self):
        self.start()
        self.sessions[0].fail = True
        self.wait_for(lambda: self.sessions[0].closed)
        self.mode = AgentMode.COMPLETED
        self.refresh()
        self.wait_for(lambda: len(self.sessions) == 2 and self.sessions[1].messages)
        self.assertEqual(self.sessions[1].messages[0], b"done\n")
        self.assertTrue(self.controller.ready)
        self.service.registerAndReturnError_.assert_not_called()

    def test_reconnect_waits_for_fresh_aggregate(self):
        callback = Mock()
        self.controller._on_reconnect = callback
        self.refresh()
        self.controller.start()
        self.wait_for(lambda: callback.called)
        self.assertEqual(self.sessions, [])
        self.mode = AgentMode.COMPLETED
        self.refresh()
        self.wait_for(lambda: self.sessions and self.sessions[0].messages)
        self.assertEqual(self.sessions[0].messages[0], b"done\n")

    def test_stale_status_does_not_replay_until_updated(self):
        self.controller._on_reconnect = None
        self.controller._updated = time.monotonic() - 60
        self.controller.start()
        self.wait_for(lambda: self.controller.last_error == "Waiting for current SidePulse status.")
        self.assertEqual(self.sessions, [])
        self.controller.update(AgentMode.IDLE_READY)
        self.wait_for(lambda: self.sessions and self.sessions[0].messages)
        self.assertEqual(self.sessions[0].messages[0], b"idle\n")
