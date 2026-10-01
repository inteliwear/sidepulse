from __future__ import annotations

import io
import json
import queue
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sidepulse.cli import sidepulse_main
from sidepulse.links import (
    LinkError, PhoneLink, listen_for_phone_registration, load_phone_links,
    normalize_fcm_token, parse_phone_registration, remove_phone_link,
    send_phone_program, store_phone_link,
)

TOKEN = "fcm_Case_sensitive:opaque_registration_with_underscores_" + "a" * 32


def registration(**overrides):
    device = {"name": "Pixel 10 Pro XL", "platform": "android",
              "package_id": "io.sidepulse.android", "push_token": TOKEN}
    device.update(overrides)
    return json.dumps({"v": 1, "type": "android_registration", "device": device})


class AndroidLinkTests(unittest.TestCase):
    def test_registration_and_opaque_token_preserve_case_and_underscores(self):
        link = parse_phone_registration(registration(), server="https://bridge.sidepulse.io")
        self.assertEqual(link.token, TOKEN)
        self.assertEqual(link.platform, "android")
        self.assertEqual(link.name, "Pixel 10 Pro XL")
        for change in ({"platform": "ios"}, {"package_id": "other.app"}, {"push_token": "a" * 64}):
            with self.subTest(change=change), self.assertRaises(LinkError):
                parse_phone_registration(registration(**change), server="https://bridge.sidepulse.io")

    def test_invalid_sender_tokens_are_rejected_without_exposing_secrets(self):
        for token in ("fcm__" + "a" * 32, TOKEN[:-1], TOKEN[:-32] + "A" * 32,
                      "fcm_" + "x" * 4097 + "_" + "a" * 32,
                      *["fcm_x" + char + "y_" + "a" * 32 for char in "/\\?#%\n é"]):
            with self.subTest(token_length=len(token)), self.assertRaises(LinkError) as error:
                normalize_fcm_token(token)
            self.assertNotIn(token, str(error.exception))

    def test_rekey_preserves_id_and_existing_ios_links(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "links.json"
            path.write_text(json.dumps({"version": 1, "ios": [{"name": "iPhone", "token": "b" * 64}]}))
            iphone = load_phone_links(path)[0]
            pixel = PhoneLink("Pixel", TOKEN)
            updated = PhoneLink("Pixel", TOKEN[:-32] + "c" * 32)
            other = PhoneLink("Other Pixel", TOKEN.replace("underscores", "different"))
            self.assertEqual(pixel.link_id, updated.link_id)
            self.assertNotEqual(pixel.link_id, other.link_id)
            store_phone_link(pixel, path)
            store_phone_link(other, path)
            self.assertEqual(store_phone_link(updated, path), (iphone, updated, other))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(remove_phone_link(updated.token, path), updated)
            self.assertEqual(load_phone_links(path), (iphone, other))

    def test_sse_pairing_ignores_invalid_events_then_links_android(self):
        response = MagicMock()
        response.__enter__.return_value = iter([
            b'data: {"v":1,"type":"unsupported"}\n', b'\n',
            b'data: ' + registration().encode() + b'\n', b'\n',
        ])
        results = queue.Queue()
        with patch("sidepulse.links.urllib.request.urlopen", return_value=response):
            listen_for_phone_registration("https://bridge.sidepulse.io", "7kP2_xQ9mLs",
                                          results, threading.Event(), deadline=time.monotonic() + 1)
        self.assertEqual(results.get_nowait().token, TOKEN)

    def test_fcm_data_payload_has_single_prefix_and_root_event_id(self):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b"OK"
        with patch("sidepulse.links.urllib.request.urlopen", return_value=response) as request:
            send_phone_program(PhoneLink("Pixel", TOKEN), "off", event_id="event-1",
                               title="Build complete", message="Tests passed",
                               data={"source": {"name": "Mac"}, "sidepulse_event_id": "wrong"})
        sent = request.call_args.args[0]
        self.assertEqual(urllib.parse.unquote(sent.full_url), "https://bridge.sidepulse.io/api/leds/" + TOKEN)
        payload = json.loads(sent.data)
        self.assertNotIn("aps", payload)
        self.assertEqual(payload["sidepulse_event_id"], "event-1")
        self.assertEqual(payload["data"]["sidepulse_event_id"], "event-1")
        self.assertEqual(payload["leds"], "off")
        self.assertEqual(payload["body"], "Tests passed")
        self.assertEqual(payload["data"]["source"], {"name": "Mac"})

    def test_http_and_connection_errors_do_not_print_sender_credentials(self):
        url = "https://bridge.sidepulse.io/api/leds/" + TOKEN
        errors = [urllib.error.URLError(url),
                  urllib.error.HTTPError(url, 400, "bad", {}, io.BytesIO(("Rejected " + TOKEN).encode()))]
        for error in errors:
            with patch("sidepulse.links.urllib.request.urlopen", side_effect=error):
                with self.assertRaises(LinkError) as result:
                    send_phone_program(PhoneLink("Pixel", TOKEN), "off")
            self.assertNotIn(TOKEN, str(result.exception))
            self.assertNotIn("a" * 32, str(result.exception))

    def test_installed_cli_routes_by_name_and_unlinks_by_id(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "links.json"
            phone = PhoneLink("Pixel 10 Pro XL", TOKEN)
            store_phone_link(phone, path)
            response = MagicMock()
            response.__enter__.return_value.read.return_value = b"OK"
            with (patch("sidepulse.links.default_links_path", return_value=path),
                  patch("sidepulse.cli.discover_devices", return_value=[]),
                  patch("sidepulse.cli._remote_event_data", return_value={}),
                  patch("sidepulse.links.urllib.request.urlopen", return_value=response) as request,
                  patch("sys.stdout", io.StringIO())):
                self.assertEqual(sidepulse_main(["push", "off", "--to", phone.name]), 0)
                self.assertEqual(json.loads(request.call_args.args[0].data)["title"], "Update")
                self.assertEqual(sidepulse_main(["unlink", phone.link_id]), 0)
                self.assertEqual(load_phone_links(path), ())

    def test_cli_accepts_a_pasted_android_token(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "links.json"
            with (patch("sidepulse.links.default_links_path", return_value=path),
                  patch("sidepulse.cli.ensure_receiver_config"),
                  patch("sidepulse.cli.relay_link_command", return_value="sidepulse link receiver"),
                  patch("sidepulse.cli.render_terminal_qr", return_value="QR"),
                  patch("sidepulse.cli.threading.Thread"),
                  patch("sidepulse.cli.select.select", return_value=([sys.stdin], [], [])),
                  patch("sys.stdin", io.StringIO(TOKEN + "\n")),
                  patch("sys.stdout", io.StringIO())):
                self.assertEqual(sidepulse_main(["link"]), 0)
            self.assertEqual(load_phone_links(path)[0].token, TOKEN)
            self.assertEqual(load_phone_links(path)[0].name, "Android phone")


if __name__ == "__main__":
    unittest.main()
