#!/bin/sh
# In UTM share only /Volumes/SidePulse, writable; run this inside the guest.
set -eu
if [ "$(uname -s)" != Darwin ]; then exit 2; fi
case "$(sysctl -n hw.model)" in VirtualMac*) ;; *) echo 'Guest only.' >&2; exit 2 ;; esac
TARGET=/Volumes/SidePulse
if [ -f "$TARGET/LEDS.LED" ]; then
    echo "$TARGET is already available"
    exit 0
fi
if [ -e "$TARGET" ]; then
    echo "$TARGET exists without LEDS.LED; refusing to cover it." >&2
    exit 1
fi
# Newer UTM releases automount a multi-directory share here.
for SOURCE in '/Volumes/My Shared Files/SidePulse' '/Volumes/My Shared Files'; do
    if [ -f "$SOURCE/LEDS.LED" ]; then
        sudo ln -s "$SOURCE" "$TARGET"
        echo "$TARGET -> $SOURCE"
        exit 0
    fi
done
# Legacy UTM uses a single-directory tag named share.
sudo mkdir "$TARGET"
if sudo mount_virtiofs share "$TARGET"; then
    if [ -f "$TARGET/LEDS.LED" ]; then exit 0; fi
    sudo umount "$TARGET"
fi
sudo rmdir "$TARGET"
echo 'Share /Volumes/SidePulse in UTM, fully shut down and start the guest, then retry.' >&2
exit 1
