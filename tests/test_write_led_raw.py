"""Exercise raw-disk safety entirely in memory; never open a real device."""

import contextlib
import os
import plistlib
import stat
import struct
import unittest
from types import SimpleNamespace
from unittest.mock import patch

if os.name != 'posix':
    raise unittest.SkipTest('macOS raw-disk backend requires POSIX file APIs')

from scripts import write_led_raw as raw


def fat12_image(cluster=2, entry_index=0, spc=1):
    image = bytearray(340 * 512)
    struct.pack_into("<HBHBHH", image, 11, 512, spc, 1, 1, 32, 340)
    struct.pack_into("<H", image, 22, 1)
    image[510:512] = b"\x55\xaa"
    image[512:515] = b"\xf8\xff\xff"
    struct.pack_into("<H", image, 512 + cluster + cluster // 2,
                     0xFFF << (4 if cluster & 1 else 0))
    for i in range(entry_index):
        image[1024 + i * 32] = 0xE5
    entry = 1024 + entry_index * 32
    image[entry:entry + 11] = b"LEDS    LED"
    image[entry + 11] = 0x20
    struct.pack_into("<H", image, entry + 26, cluster)
    struct.pack_into("<I", image, entry + 28, 4)
    start = (4 + (cluster - 2) * spc) * 512
    image[start:start + 512] = b"x" * 512
    return image, start, entry


class RawWriteTests(unittest.TestCase):
    def setUp(self):
        self.image, self.data_offset, self.entry = fat12_image()
        self.capacity = len(self.image)
        self.info = {"DeviceIdentifier": "disk4", "WholeDisk": True}
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.mock("sys.platform", "darwin")
        self.open = self.mock("os.open", return_value=43)
        self.close = self.mock("os.close")
        self.mock("os.fstat", return_value=SimpleNamespace(st_mode=stat.S_IFCHR))
        self.ioctl = self.mock("fcntl.ioctl", side_effect=self.ioctl_result)
        self.mock("subprocess.run", side_effect=lambda *a, **k: SimpleNamespace(
            stdout=plistlib.dumps(self.info)))
        self.mock("os.pread", side_effect=lambda fd, count, offset:
                  bytes(self.image[offset:offset + count]))
        self.pwrite = self.mock("os.pwrite", side_effect=self.write)
        self.mock("os.fsync")

    def mock(self, name, *args, **kwargs):
        return self.stack.enter_context(patch("scripts.write_led_raw." + name,
                                              *args, **kwargs))

    def ioctl_result(self, fd, request, buffer):
        self.assertEqual(fd, 43)
        if request == raw.DKIOCGETBLOCKSIZE:
            return struct.pack("=I", 512)
        self.assertEqual(request, raw.DKIOCGETBLOCKCOUNT)
        return struct.pack("=Q", self.capacity // 512)

    def write(self, fd, data, offset):
        self.assertEqual(fd, 43)
        self.image[offset:offset + len(data)] = data
        return len(data)

    def refused(self, device="/dev/rdisk4", text="off"):
        with self.assertRaises((ValueError, OSError)):
            raw.write_led(device, text)
        self.pwrite.assert_not_called()

    def test_writes_only_data_and_file_size_for_both_paths(self):
        for device in ("/dev/disk4", "/dev/rdisk4"):
            with self.subTest(device=device):
                self.image, start, entry = fat12_image()
                expected = bytearray(self.image)
                payload = b"#ff00ff\n"
                expected[start:start + 512] = payload.ljust(512, b"\0")
                struct.pack_into("<I", expected, entry + 28, len(payload))
                raw.write_led(device, payload.decode())
                self.assertEqual(self.image, expected)
                self.close.assert_called_with(43)

    def test_nondefault_cluster_and_second_root_sector(self):
        self.image, start, entry = fat12_image(cluster=7, entry_index=18, spc=2)
        raw.write_led("/dev/rdisk4", "off")
        self.assertEqual(self.image[start:start + 512], b"off".ljust(512, b"\0"))
        self.assertEqual(struct.unpack_from("<I", self.image, entry + 28)[0], 3)
        self.assertEqual([c.args[2] for c in self.pwrite.call_args_list], [start, 1536])

    def test_large_device_cannot_hide_behind_small_fat_volume(self):
        for size in (300032, 300 * 1024, 1024**4):
            with self.subTest(size=size):
                self.capacity = size
                self.refused()

    def test_largest_allowed_512_byte_device(self):
        self.capacity = 299520
        raw.write_led("/dev/rdisk4", "off")
        self.assertEqual(self.pwrite.call_count, 2)

    def test_unknown_or_zero_capacity(self):
        self.capacity = 0
        self.refused()
        self.ioctl.side_effect = OSError("capacity unavailable")
        self.refused()

    def test_capacity_rechecked_before_write(self):
        with patch.object(raw, "device_size", side_effect=[174080, 300032]):
            self.refused()

    def test_partition_and_nondevice_paths_rejected_before_open(self):
        for path in ("/dev/disk4s1", "/dev/rdisk4s1", "disk4", "/tmp/disk4", "/dev/disk"):
            self.refused(device=path)
        self.open.assert_not_called()

    def test_mounted_invalid_mount_or_nonwhole_device(self):
        for change in ({"MountPoint": "/Volumes/SidePulse"}, {"MountPoint": False},
                       {"WholeDisk": False},
                       {"DeviceIdentifier": "disk5"}):
            with self.subTest(change=change), patch.dict(self.info, change):
                self.refused()
        self.open.assert_not_called()

    def test_explicit_empty_mountpoint_is_unmounted(self):
        self.info["MountPoint"] = ""
        raw.write_led("/dev/rdisk4", "off")
        self.assertEqual(self.pwrite.call_count, 2)

    def test_missing_whole_disk_status_is_rejected(self):
        del self.info["WholeDisk"]
        self.refused()
        self.open.assert_not_called()

    def test_regular_file_rejected(self):
        self.mock("os.fstat", return_value=SimpleNamespace(st_mode=stat.S_IFREG))
        self.refused()

    def test_bad_programs_rejected_before_open(self):
        for text in ("", "x" * 513, "é" * 257, "off\n" * 21, "off\0"):
            self.refused(text=text)
        self.open.assert_not_called()

    def test_full_sector_program(self):
        raw.write_led("/dev/rdisk4", ";" + "x" * 511)
        self.assertEqual(self.image[self.data_offset:self.data_offset + 512],
                         b";" + b"x" * 511)

    def test_malformed_fat_and_missing_file_never_write(self):
        for offset, data in ((510, b"\0\0"), (16, b"\x02"), (13, b"\0"),
                             (19, b"\xff\xff"), (22, b"\0\0"), (28, b"\x01"),
                             (1024, b"\0"), (1050, b"\xff\xff"),
                             (515, b"\0\0"), (1035, b"\x21")):
            with self.subTest(offset=offset):
                self.image, _, _ = fat12_image()
                self.image[offset:offset + len(data)] = data
                self.refused()

    def test_short_read_never_writes(self):
        self.mock("os.pread", return_value=b"")
        self.refused()

    def test_short_data_write_stops_before_directory(self):
        self.pwrite.side_effect = None
        self.pwrite.return_value = 12
        with self.assertRaisesRegex(ValueError, "Short disk write"):
            raw.write_led("/dev/rdisk4", "off")
        self.assertEqual(self.pwrite.call_count, 1)
        self.close.assert_called_once_with(43)

    def test_symlinks_are_not_followed(self):
        self.open.side_effect = OSError("symlink")
        self.refused()
        self.assertTrue(self.open.call_args.args[1] & raw.os.O_NOFOLLOW)


if __name__ == "__main__":
    unittest.main()
