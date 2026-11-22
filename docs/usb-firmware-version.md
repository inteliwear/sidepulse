# Firmware version with Dot storage disabled

`sidepulse firmware version` also discovers Dots over USB when their mass-storage
interface is disabled. `--json` keeps the existing model/version/serial/device
fields and reports the actual firmware version and production serial from the
read-only EP0 GET_DEVICE_INFO request (`0xc0/0x54`, value 0, index `0x5350`, 48 bytes).
Firmware must support the `SPI1` device-info response; older mounted Dots continue
using `STATUS.TXT`.

USB devices appear with a selector such as `/dev/sidepulse-usb/Dot-002-001`. This
is a bus/address selector, not a filesystem path. It can be passed to
`sidepulse firmware version --device ...` while that USB address remains current.
The client never claims an interface, changes storage settings or resets USB to
read the version. Mounted Dots are not duplicated in the results.

`sidepulse firmware upgrade --all` includes storage-off Dots. When a newer
package is available, the CLI enables storage and reconnects the selected Dot,
checks its identity at the new address, and locates its mounted drive. It saves
the original storage preference before copying firmware. The preference takes
effect at the next restart or reconnection. Dry runs and already-current devices
leave storage settings alone. A Dot still requires a host that permits storage
writes. A read-only Pro on macOS can use its READ2ME firmware channel.

Hardware verification: revision-2 Dot SPD-000006 running `storage-info-test`
reported its version and serial through the installed CLI with MSC hidden after
software reconnection. Storage-on reconnection returned to mounted discovery
without a duplicate device. Storage was left disabled after testing.
