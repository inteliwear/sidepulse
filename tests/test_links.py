from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from sidepulse.cli import sidepulse_main  # noqa: E402
from sidepulse.links import (  # noqa: E402
    IOSLink,
    LinkError,
    iter_sse_messages,
    load_ios_links,
    new_pairing_channel,
    normalize_apns_token,
    pairing_url,
    parse_ios_registration,
    render_terminal_qr,
    remove_ios_link,
    save_ios_links,
    send_ios_program,
    store_ios_link,
)


TOKEN_A = "a" * 64
TOKEN_B = "b" * 64


class LinkStorageTests(unittest.TestCase):
    def test_token_is_normalized_and_validated(self) -> None:
        self.assertEqual(normalize_apns_token("AA " * 32), TOKEN_A)
        for invalid in ("", "a" * 63, "g" * 64):
            with self.subTest(invalid=invalid):
                with self.assertRaises(LinkError):
                    normalize_apns_token(invalid)

    def test_links_round_trip_with_private_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "links.json"
            link = IOSLink("Peter's iPhone", TOKEN_A, linked_at="now")
            save_ios_links((link,), path)
            self.assertEqual(load_ios_links(path), (link,))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_relinking_a_token_updates_instead_of_duplicating(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "links.json"
            store_ios_link(IOSLink("Old name", TOKEN_A), path)
            stored = store_ios_link(IOSLink("New name", TOKEN_A), path)
            self.assertEqual(len(stored), 1)
            self.assertEqual(stored[0].name, "New name")

    def test_remove_ios_link_removes_only_matching_phone(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "links.json"
            first = IOSLink("Phone A", TOKEN_A)
            second = IOSLink("Phone B", TOKEN_B)
            save_ios_links((first, second), path)

            removed = remove_ios_link(TOKEN_A, path)

            self.assertEqual(removed, first)
            self.assertEqual(load_ios_links(path), (second,))

    def test_pairing_channel_is_an_eleven_character_base64url_secret(self) -> None:
        channels = {new_pairing_channel() for _ in range(100)}
        self.assertEqual(len(channels), 100)
        self.assertTrue(all(len(channel) == 11 for channel in channels))
        alphabet = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
        self.assertTrue(all(set(channel) <= alphabet for channel in channels))

    def test_default_pairing_url_contains_only_the_compact_channel(self) -> None:
        channel = "7kP2_xQ9mLs"
        url = pairing_url("https://bridge.sidepulse.io/", channel, "Studio Mac")
        self.assertEqual(url, "sidepulse://p/7kP2_xQ9mLs")

    def test_custom_server_pairing_url_keeps_connection_details(self) -> None:
        url = pairing_url("https://bridge.example.com", "7kP2_xQ9mLs", "Studio Mac")
        self.assertIn("sidepulse://pair?", url)
        self.assertIn("server=https%3A%2F%2Fbridge.example.com", url)
        self.assertIn("channel=7kP2_xQ9mLs", url)
        self.assertIn("sender=Studio+Mac", url)

    def test_pairing_qr_renders_without_printing_the_raw_link(self) -> None:
        url = pairing_url(
            "https://bridge.sidepulse.io",
            "7kP2_xQ9mLs",
            "Studio Mac",
        )
        rendered = render_terminal_qr(url)
        lines = rendered.splitlines()
        self.assertIn("██", rendered)
        self.assertLessEqual(len(lines), 40)
        self.assertLessEqual(max(map(len, lines)), 80)
        self.assertNotIn("sidepulse://", rendered)

    def test_pairing_qr_uses_solid_backgrounds_in_ansi_terminals(self) -> None:
        rendered = render_terminal_qr("sidepulse://p/7kP2_xQ9mLs", ansi=True)
        self.assertIn("\033[107m", rendered)
        self.assertIn("\033[40m", rendered)
        self.assertTrue(all(line.endswith("\033[0m") for line in rendered.splitlines()))

    def test_sse_parser_returns_complete_data_events(self) -> None:
        response = iter(
            [
                b": keepalive\n",
                b"data: first\n",
                b"data: second\n",
                b"\n",
            ]
        )
        self.assertEqual(list(iter_sse_messages(response)), ["first\nsecond"])

    def test_registration_response_becomes_a_link(self) -> None:
        response = json.dumps(
            {
                "v": 1,
                "type": "ios_registration",
                "device": {
                    "name": "Peter's iPhone",
                    "platform": "ios",
                    "bundle_id": "io.sidepulse.ios",
                    "push_token": TOKEN_B.upper(),
                },
            }
        )
        link = parse_ios_registration(response, server="https://bridge.sidepulse.io")
        self.assertEqual(link.name, "Peter's iPhone")
        self.assertEqual(link.token, TOKEN_B)


class WriteFallbackTests(unittest.TestCase):
    def test_write_requires_selection_when_multiple_linked_phones_exist(self) -> None:
        links = (IOSLink("Phone A", TOKEN_A), IOSLink("Phone B", TOKEN_B))
        stderr = io.StringIO()
        with (
            patch("sidepulse.cli.discover_devices", return_value=[]),
            patch("sidepulse.cli.load_ios_links", return_value=links),
            patch("sidepulse.cli.send_ios_program") as send,
            patch("sys.stderr", stderr),
        ):
            result = sidepulse_main(["write", r"off\nrepeat"])

        self.assertEqual(result, 2)
        send.assert_not_called()
        self.assertIn("--to", stderr.getvalue())
        self.assertIn("--all", stderr.getvalue())

    def test_write_all_uses_all_linked_phones(self) -> None:
        links = (IOSLink("Phone A", TOKEN_A), IOSLink("Phone B", TOKEN_B))
        sent: list[tuple[IOSLink, str | None, dict[str, object]]] = []

        def send(link: IOSLink, program: str | None, **kwargs) -> str:
            sent.append((link, program, kwargs))
            return "OK"

        with (
            patch("sidepulse.cli.discover_devices", return_value=[]),
            patch("sidepulse.cli.load_ios_links", return_value=links),
            patch("sidepulse.cli._remote_event_data", return_value={"source": {"name": "Mac"}}),
            patch("sidepulse.cli.send_ios_program", side_effect=send),
        ):
            result = sidepulse_main(["write", r"off\nrepeat", "--all"])

        self.assertEqual(result, 0)
        self.assertEqual([item[0].name for item in sent], ["Phone A", "Phone B"])
        self.assertTrue(all(item[1] == "off\nrepeat" for item in sent))
        self.assertEqual(len({item[2]["event_id"] for item in sent}), 1)

    def test_write_does_not_contact_phone_when_local_device_exists(self) -> None:
        stdout = io.StringIO()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "SidePulseDot"
            root.mkdir()
            candidate = type("Candidate", (), {"root": root, "target": root / "LEDS.LED"})()
            with (
                patch("sidepulse.cli.discover_devices", return_value=[candidate]),
                patch("sidepulse.cli.send_ios_program") as send,
                patch("sys.stdout", stdout),
            ):
                result = sidepulse_main(["write", "off", "--device", str(root)])
            self.assertEqual(result, 0)
            send.assert_not_called()
            self.assertEqual((root / "LEDS.LED").read_text(), "off")

    def test_write_prefers_discovered_local_device_over_phone(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "SidePulseDot"
            root.mkdir()
            candidate = type("Candidate", (), {"root": root, "target": root / "LEDS.LED"})()
            with (
                patch("sidepulse.cli.discover_devices", return_value=[candidate]),
                patch("sidepulse.cli.load_ios_links", return_value=(IOSLink("Phone", TOKEN_A),)),
                patch("sidepulse.cli.send_ios_program") as send,
            ):
                result = sidepulse_main(["write", r"off\nrepeat"])

            self.assertEqual(result, 0)
            send.assert_not_called()
            self.assertEqual((root / "LEDS.LED").read_text(), "off\nrepeat")

    def test_missing_local_and_linked_devices_points_to_link_command(self) -> None:
        stderr = io.StringIO()
        with (
            patch("sidepulse.cli.discover_devices", return_value=[]),
            patch("sidepulse.cli.load_ios_links", return_value=()),
            patch("sys.stderr", stderr),
        ):
            result = sidepulse_main(["write", "off"])
        self.assertEqual(result, 2)
        self.assertIn("sidepulse link", stderr.getvalue())

    def test_notification_prefers_phone_over_local_device(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "SidePulseDot"
            root.mkdir()
            candidate = type("Candidate", (), {"root": root, "target": root / "LEDS.LED"})()
            phone = IOSLink("Peter's iPhone", TOKEN_A)
            with (
                patch("sidepulse.cli.discover_devices", return_value=[candidate]),
                patch("sidepulse.cli.load_ios_links", return_value=(phone,)),
                patch("sidepulse.cli._remote_event_data", return_value={}),
                patch("sidepulse.cli.send_ios_program", return_value="OK") as send,
            ):
                result = sidepulse_main(
                    ["write", "off", "--title", "Build complete", "--message", "All tests passed"]
                )

            self.assertEqual(result, 0)
            self.assertFalse((root / "LEDS.LED").exists())
            send.assert_called_once()
            self.assertEqual(send.call_args.args, (phone, "off"))
            self.assertEqual(send.call_args.kwargs["title"], "Build complete")
            self.assertEqual(send.call_args.kwargs["message"], "All tests passed")

    def test_write_selects_phone_by_short_id(self) -> None:
        phones = (IOSLink("Phone A", TOKEN_A), IOSLink("Phone B", TOKEN_B))
        with (
            patch("sidepulse.cli.discover_devices", return_value=[]),
            patch("sidepulse.cli.load_ios_links", return_value=phones),
            patch("sidepulse.cli._remote_event_data", return_value={}),
            patch("sidepulse.cli.send_ios_program", return_value="OK") as send,
        ):
            result = sidepulse_main(["write", "off", "--to", "bbbb"])

        self.assertEqual(result, 0)
        self.assertEqual(send.call_args.args[:2], (phones[1], "off"))

    def test_notification_cannot_target_local_device(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "SidePulseDot"
            root.mkdir()
            candidate = type("Candidate", (), {"root": root, "target": root / "LEDS.LED"})()
            stderr = io.StringIO()
            with (
                patch("sidepulse.cli.discover_devices", return_value=[candidate]),
                patch("sidepulse.cli.load_ios_links", return_value=()),
                patch("sys.stderr", stderr),
            ):
                result = sidepulse_main(["write", "off", "--message", "Hello", "--to", "local"])

        self.assertEqual(result, 2)
        self.assertIn("cannot display notifications", stderr.getvalue())

    def test_push_uses_same_payload_options_and_prefers_phone(self) -> None:
        phone = IOSLink("Peter's iPhone", TOKEN_A)
        local = type(
            "Candidate",
            (),
            {"root": Path("/Volumes/SidePulseDot"), "target": Path("/Volumes/SidePulseDot/LEDS.LED")},
        )()
        with (
            patch("sidepulse.cli.discover_devices", return_value=[local]),
            patch("sidepulse.cli.load_ios_links", return_value=(phone,)),
            patch("sidepulse.cli._remote_event_data", return_value={}),
            patch("sidepulse.cli.send_ios_program", return_value="OK") as send,
        ):
            result = sidepulse_main(
                [
                    "push",
                    r"off\n#ff0000 pulse",
                    "--title",
                    "Agent needs input",
                    "--message",
                    "Choose a deployment region",
                ]
            )

        self.assertEqual(result, 0)
        self.assertEqual(send.call_args.args, (phone, "off\n#ff0000 pulse"))
        self.assertEqual(send.call_args.kwargs["title"], "Agent needs input")
        self.assertEqual(send.call_args.kwargs["message"], "Choose a deployment region")

    def test_write_all_sends_leds_locally_and_combined_payload_to_phone(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "SidePulseDot"
            root.mkdir()
            candidate = type("Candidate", (), {"root": root, "target": root / "LEDS.LED"})()
            phone = IOSLink("Phone", TOKEN_A)
            with (
                patch("sidepulse.cli.discover_devices", return_value=[candidate]),
                patch("sidepulse.cli.load_ios_links", return_value=(phone,)),
                patch("sidepulse.cli._remote_event_data", return_value={}),
                patch("sidepulse.cli.send_ios_program", return_value="OK") as send,
            ):
                result = sidepulse_main(
                    ["write", "off", "--title", "Done", "--message", "Tests passed", "--all"]
                )

            self.assertEqual(result, 0)
            self.assertEqual((root / "LEDS.LED").read_text(), "off")
            self.assertEqual(send.call_args.args, (phone, "off"))
            self.assertEqual(send.call_args.kwargs["title"], "Done")

    def test_notification_only_write_is_supported(self) -> None:
        phone = IOSLink("Phone", TOKEN_A)
        with (
            patch("sidepulse.cli.discover_devices", return_value=[]),
            patch("sidepulse.cli.load_ios_links", return_value=(phone,)),
            patch("sidepulse.cli._remote_event_data", return_value={}),
            patch("sidepulse.cli.send_ios_program", return_value="OK") as send,
        ):
            result = sidepulse_main(["write", "--title", "Done", "--message", "Tests passed"])

        self.assertEqual(result, 0)
        self.assertEqual(send.call_args.args, (phone, None))

    def test_write_requires_content(self) -> None:
        stderr = io.StringIO()
        with patch("sys.stderr", stderr):
            result = sidepulse_main(["write"])

        self.assertEqual(result, 2)
        self.assertIn("Provide an LED program", stderr.getvalue())

    def test_write_reads_led_program_from_explicit_stdin(self) -> None:
        phone = IOSLink("Peter's iPhone", TOKEN_A)
        with (
            patch("sidepulse.cli.discover_devices", return_value=[]),
            patch("sidepulse.cli.load_ios_links", return_value=(phone,)),
            patch("sidepulse.cli._remote_event_data", return_value={}),
            patch("sidepulse.cli.send_ios_program", return_value="OK") as send,
            patch("sys.stdin", io.StringIO(r"off\nrepeat")),
        ):
            result = sidepulse_main(["write", "-"])

        self.assertEqual(result, 0)
        self.assertEqual(send.call_args.args, (phone, "off\nrepeat"))


class IOSPayloadTests(unittest.TestCase):
    def test_notification_payload_contains_visible_alert_leds_and_metadata(self) -> None:
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b"OK"
        with patch("sidepulse.links.urllib.request.urlopen", return_value=response) as open_url:
            result = send_ios_program(
                IOSLink("Phone", TOKEN_A),
                "off",
                event_id="event-1",
                title="Build complete",
                message="All tests passed",
                data={"source": {"name": "Studio Mac"}},
            )

        self.assertEqual(result, "OK")
        request = open_url.call_args.args[0]
        payload = json.loads(request.data)
        self.assertEqual(payload["leds"], "off")
        self.assertEqual(payload["title"], "Build complete")
        self.assertEqual(payload["body"], "All tests passed")
        self.assertEqual(
            payload["aps"],
            {
                "content-available": 1,
                "alert": {"title": "Build complete", "body": "All tests passed"},
            },
        )
        self.assertEqual(payload["data"]["sidepulse_event_id"], "event-1")
        self.assertEqual(payload["data"]["source"]["name"], "Studio Mac")

    def test_led_only_payload_stays_silent(self) -> None:
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b"OK"
        with patch("sidepulse.links.urllib.request.urlopen", return_value=response) as open_url:
            send_ios_program(IOSLink("Phone", TOKEN_A), "off", event_id="event-1")

        payload = json.loads(open_url.call_args.args[0].data)
        self.assertEqual(payload["aps"], {"content-available": 1})
        self.assertNotIn("title", payload)
        self.assertNotIn("body", payload)


if __name__ == "__main__":
    unittest.main()
