from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from sidepulse.cli import sidepulse_main
from sidepulse import firmware


def package_bytes(product='pro', version='1.1.0', *, nested=True, tamper=False):
    title = firmware.PRODUCT_NAMES[product]
    files = {
        'FIRMWARE.BIN': b'encrypted firmware payload',
        'README.txt': f'{title} firmware update - version {version}\n'.encode(),
        'RELEASE_NOTES.txt': f'# {title} {version}\n'.encode(),
    }
    files['SHA256SUMS.txt'] = ''.join(
        f'{hashlib.sha256(data).hexdigest()}  {name}\n' for name, data in files.items()
    ).encode()
    if tamper:
        files['FIRMWARE.BIN'] = b'corrupt firmware'
    output = io.BytesIO()
    prefix = f'sidepulse-{product}-{version}-ota/' if nested else ''
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, data in files.items():
            archive.writestr(prefix + name, data)
    return output.getvalue()


class FirmwareTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.device = self.make_device('pro')
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()
        self.addCleanup(patch.stopall)
        patch('sys.stdout', self.stdout).start()
        patch('sys.stderr', self.stderr).start()

    def make_device(self, product, version='1.0.5'):
        device = self.root / product
        device.mkdir(exist_ok=True)
        field = 'app_version' if product == 'dot' else 'release_version'
        (device / 'STATUS.TXT').write_bytes(f'{field} {version}\nserial SP-123\n'.encode() + b'\0' * 100)
        return device

    def make_package(self, product='pro', version='1.1.0', **kwargs):
        path = self.root / f'sidepulse-{product}-{version}-ota.zip'
        path.write_bytes(package_bytes(product, version, **kwargs))
        return path

    def upgrade(self, package, *args):
        return sidepulse_main(['firmware', 'upgrade', '--device', str(self.device), '--file', str(package), *args])

    def test_version_reports_both_models_and_json(self):
        self.make_device('dot', '1.1.0')
        with patch.dict('os.environ', {'SIDEPULSE_MOUNT_ROOTS': str(self.root)}):
            self.assertEqual(sidepulse_main(['firmware', 'version', '--json']), 0)
        output = json.loads(self.stdout.getvalue())
        self.assertEqual([(d['model'], d['version']) for d in output], [('SidePulse Dot', '1.1.0'), ('SidePulse Pro', '1.0.5')])
        self.assertEqual(output[0]['serial'], 'SP-123')

    def test_explicit_renamed_drive_uses_status_not_volume_name(self):
        self.assertEqual(sidepulse_main(['firmware', 'version', '--device', str(self.device)]), 0)
        self.assertIn('SidePulse Pro: 1.0.5', self.stdout.getvalue())

    def test_version_uses_uncached_status_reader(self):
        status = b'release_version 1.1.0\nserial SP-123\n'
        with patch.object(firmware, 'read_status_bytes', return_value=status) as read:
            self.assertEqual(sidepulse_main(['firmware', 'version', '--device', str(self.device)]), 0)
        read.assert_called_once_with(self.device.resolve() / 'STATUS.TXT', max_bytes=65536)
        self.assertIn('SidePulse Pro: 1.1.0', self.stdout.getvalue())

    def test_version_fails_if_cache_invalidation_fails(self):
        with patch.object(firmware, 'read_status_bytes', side_effect=OSError('invalidation failed')):
            self.assertEqual(sidepulse_main(['firmware', 'version', '--device', str(self.device)]), 1)
        self.assertIn('invalidation failed', self.stderr.getvalue())

    def test_missing_device_fails_without_download(self):
        with patch.dict('os.environ', {'SIDEPULSE_MOUNT_ROOTS': str(self.root / 'missing')}), patch.object(firmware, 'download') as download:
            self.assertEqual(sidepulse_main(['firmware', 'upgrade']), 1)
            download.assert_not_called()
        self.assertIn('No mounted SidePulse', self.stderr.getvalue())

    def test_multiple_devices_require_selection_before_download(self):
        self.make_device('dot')
        with patch.dict('os.environ', {'SIDEPULSE_MOUNT_ROOTS': str(self.root)}), patch.object(firmware, 'download') as download:
            self.assertEqual(sidepulse_main(['firmware', 'upgrade']), 1)
            download.assert_not_called()
        self.assertIn('--device', self.stderr.getvalue())

    def test_invalid_or_ambiguous_status_is_rejected(self):
        for status in ('hello\n', 'release_version 1.0.5\napp_version 1.0.5\n'):
            (self.device / 'STATUS.TXT').write_text(status)
            with self.assertRaises(firmware.FirmwareError):
                firmware.read_device(self.device)

    def test_older_pro_status_shows_unknown_release(self):
        (self.device / 'STATUS.TXT').write_text('firmware_version 12345\nfirmware_slot A\n')
        self.assertEqual(firmware.read_device(self.device).version, 'unknown')

    def test_copy_preserves_payload_and_existing_programs(self):
        for name in ('LEDS.LED', 'INIT.LED'):
            (self.device / name).write_text('existing program')
        (self.device / 'FIRMWARE.BIN').write_bytes(b'old firmware with a longer payload' * 100)
        with patch.object(firmware.os, 'fsync', wraps=firmware.os.fsync) as sync:
            self.assertEqual(self.upgrade(self.make_package()), 0)
            self.assertGreaterEqual(sync.call_count, 1)
        self.assertEqual((self.device / 'FIRMWARE.BIN').read_bytes(), b'encrypted firmware payload')
        for name in ('LEDS.LED', 'INIT.LED'):
            self.assertEqual((self.device / name).read_text(), 'existing program')
        self.assertIn('transferred', self.stdout.getvalue())
        self.assertIn('confirm installation', self.stdout.getvalue())

    def test_dry_run_verifies_without_writing(self):
        self.assertEqual(self.upgrade(self.make_package(), '--dry-run'), 0)
        self.assertFalse((self.device / 'FIRMWARE.BIN').exists())
        self.assertIn('Would upgrade', self.stdout.getvalue())

    def test_wrong_product_or_corrupt_package_never_writes(self):
        for package in (self.make_package('dot'), self.make_package(tamper=True)):
            self.assertEqual(self.upgrade(package), 1)
            self.assertFalse((self.device / 'FIRMWARE.BIN').exists())

    def test_same_version_is_noop_and_downgrade_is_rejected(self):
        self.assertEqual(self.upgrade(self.make_package(version='1.0.5')), 0)
        self.assertEqual(self.upgrade(self.make_package(version='1.0.4')), 1)
        self.assertFalse((self.device / 'FIRMWARE.BIN').exists())

    def test_status_identity_is_rechecked_after_download(self):
        package = firmware.FirmwarePackage('pro', '1.1.0', b'payload')
        def load(*args):
            (self.device / 'STATUS.TXT').write_text('release_version 1.0.5\nserial DIFFERENT\n')
            return package
        with patch.object(firmware, 'load_package', side_effect=load):
            self.assertEqual(self.upgrade(self.make_package()), 1)
        self.assertFalse((self.device / 'FIRMWARE.BIN').exists())

    def test_failed_flush_is_not_reported_as_success(self):
        with patch.object(firmware.os, 'fsync', side_effect=OSError('write failed')):
            self.assertEqual(self.upgrade(self.make_package()), 1)
        self.assertNotIn('transferred', self.stdout.getvalue())
        self.assertIn('did not finish', self.stderr.getvalue())

    def test_symlink_target_is_rejected(self):
        outside = self.root / 'unrelated'
        outside.write_bytes(b'keep me')
        (self.device / 'FIRMWARE.BIN').symlink_to(outside)
        self.assertEqual(self.upgrade(self.make_package()), 1)
        self.assertEqual(outside.read_bytes(), b'keep me')

    def test_flat_and_nested_packages_are_supported(self):
        for nested in (True, False):
            parsed = firmware.read_package(package_bytes(nested=nested), 'sidepulse-pro-1.1.0-ota.zip')
            self.assertEqual(parsed.version, '1.1.0')
            self.assertEqual(parsed.payload, b'encrypted firmware payload')

    def test_extra_and_traversal_entries_are_rejected(self):
        for extra in ('../outside', 'sidepulse-pro-1.1.0-ota/extra.txt'):
            data = io.BytesIO(package_bytes())
            with zipfile.ZipFile(data, 'a') as archive:
                archive.writestr(extra, 'unwanted')
            with self.assertRaises(firmware.FirmwareError):
                firmware.read_package(data.getvalue(), 'sidepulse-pro-1.1.0-ota.zip')

    def test_renamed_model_and_version_are_rejected(self):
        for filename in ('sidepulse-dot-1.1.0-ota.zip', 'sidepulse-pro-2.0.0-ota.zip'):
            with self.assertRaises(firmware.FirmwareError):
                firmware.read_package(package_bytes(nested=False), filename)

    def test_latest_release_is_selected_numerically(self):
        entries = [{'name': name, 'type': 'dir'} for name in ('v1.9.0', 'v1.10.0', 'v2.0.0-beta')]
        with patch.object(firmware, 'download', return_value=json.dumps(entries).encode()):
            self.assertEqual(firmware.published_versions(), ['1.10.0', '1.9.0'])

    def test_latest_release_is_selected_for_the_connected_model(self):
        data = package_bytes('dot')
        sums = f'{hashlib.sha256(data).hexdigest()}  sidepulse-dot-1.1.0-ota.zip\n'.encode()
        pro_only = f'{"0" * 64}  sidepulse-pro-1.2.0-ota.zip\n'.encode()
        with patch.object(firmware, 'published_versions', return_value=['1.2.0', '1.1.0']), patch.object(firmware, 'download', side_effect=[pro_only, sums, data]):
            self.assertEqual(firmware.load_package('dot', None, None).version, '1.1.0')

    def test_requested_version_never_falls_back_to_another_release(self):
        with patch.object(firmware, 'download', return_value=b'') as download:
            with self.assertRaises(firmware.FirmwareError):
                firmware.load_package('dot', '1.2.0', None)
        self.assertEqual(download.call_count, 1)

    def test_remote_package_is_verified_against_outer_checksum(self):
        data = package_bytes()
        sums = f'{hashlib.sha256(data).hexdigest()}  sidepulse-pro-1.1.0-ota.zip\n'.encode()
        with patch.object(firmware, 'download', side_effect=[sums, data]) as download:
            package = firmware.load_package('pro', '1.1', None)
        self.assertEqual(package.version, '1.1.0')
        self.assertIn('/v1.1.0/SHA256SUMS.txt', download.call_args_list[0].args[0])
        with patch.object(firmware, 'download', side_effect=[sums, data + b'corruption']):
            with self.assertRaises(firmware.FirmwareError):
                firmware.load_package('pro', '1.1.0', None)

    def test_network_failure_and_invalid_zip_have_friendly_errors(self):
        with patch.object(firmware.urllib.request, 'urlopen', side_effect=OSError('offline')):
            self.assertEqual(sidepulse_main(['firmware', 'upgrade', '--device', str(self.device)]), 1)
        self.assertIn('--file', self.stderr.getvalue())
        with self.assertRaises(firmware.FirmwareError):
            firmware.read_package(b'not a zip', 'sidepulse-pro-1.1.0-ota.zip')

    def test_download_size_is_bounded(self):
        with patch.object(firmware.urllib.request, 'urlopen', return_value=io.BytesIO(b'0123456789')):
            with self.assertRaises(firmware.FirmwareError):
                firmware.download('https://example.com', limit=3)

    def test_invalid_version_does_not_download(self):
        with patch.object(firmware, 'download') as download:
            with self.assertRaises(firmware.FirmwareError):
                firmware.load_package('pro', '../other', None)
            download.assert_not_called()
