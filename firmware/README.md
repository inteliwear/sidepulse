# Firmware release notes

Firmware ZIP files are organized by version. Download the ZIP for your SidePulse
model; firmware is specific to each model.

## v1.1.17

| Model | Download |
| --- | --- |
| SidePulse Dot revision 2 | [sidepulse-dot-1.1.17-ota.zip](v1.1.17/sidepulse-dot-1.1.17-ota.zip) |
| SidePulse Pro | [sidepulse-pro-1.1.17-ota.zip](v1.1.17/sidepulse-pro-1.1.17-ota.zip) |

[SHA-256 checksums](v1.1.17/SHA256SUMS.txt).

### SidePulse Dot revision 2

Hide the Dot’s USB drive with `sidepulse storage off` to help meet enterprise policies that restrict USB storage. You can still control the LEDs over USB while the drive is hidden. Use `sidepulse storage on` to show it again. The Dot remembers your choice when you unplug it.

### SidePulse Pro

Improved reliability when waking from sleep or hibernation.

## v1.1.14

| Model | Download |
| --- | --- |
| SidePulse Dot | [sidepulse-dot-1.1.14-ota.zip](v1.1.14/sidepulse-dot-1.1.14-ota.zip) |

[SHA-256 checksums](v1.1.14/SHA256SUMS.txt).

### SidePulse Dot

- Improve direct USB connection compatibility with Android phones.
- Improve drive identification when using multiple Dots.

SidePulse Pro remains on [v1.1.0](v1.1.0/sidepulse-pro-1.1.0-ota.zip).

## v1.1.0

First firmware release distributed in this repository.

| Model | Download |
| --- | --- |
| SidePulse Dot | [sidepulse-dot-1.1.0-ota.zip](v1.1.0/sidepulse-dot-1.1.0-ota.zip) |
| SidePulse Pro | [sidepulse-pro-1.1.0-ota.zip](v1.1.0/sidepulse-pro-1.1.0-ota.zip) |

[SHA-256 checksums](v1.1.0/SHA256SUMS.txt) are included for both ZIP files.

### SidePulse Dot

- Add animation updates on devices with read-only storage, where permitted by
  device management policies.
- Support animation programs up to 512 bytes.
- Reduce power use when the LEDs are off.

### SidePulse Pro

- Improve white balance and dynamic range.
- Add animation, startup settings, and firmware updates on devices with read-only
  storage, where permitted by device management policies.
- Improve update reliability and file preservation across restarts.
- Improve SD-reader compatibility and recovery from interrupted reads.

## Installation

1. Extract the ZIP for your model on your computer.
2. Connect your device and open its drive: `PulseDot` for SidePulse Dot or
   `SidePulse` for SidePulse Pro.
3. Copy only `FIRMWARE.BIN` to the top level of the drive. Choose Replace if the
   file already exists.
4. After copying finishes, leave the device connected and untouched for at least
   10 seconds while it applies the update and restarts.
5. Reconnect the device, then open `STATUS.TXT` and check that `app_version`
   (Dot) or `release_version` (Pro) matches the version you downloaded, such as
   `1.1.0`. Reconnecting avoids a cached copy of `STATUS.TXT` on your computer.

Do not rename `FIRMWARE.BIN` or copy the ZIP itself onto the drive. The other files
in each ZIP are for reference. Normal copy/paste updates require a writable drive;
read-only storage policies may prevent this installation method.
