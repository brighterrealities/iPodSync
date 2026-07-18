"""iPod filesystem mount control (container owns the mount).

The container mounts the iPod's FAT32 partition itself, so it can also unmount it
cleanly for a safe eject — no host-namespace tricks. Requires CAP_SYS_ADMIN and the
partition device passed in (--device /dev/sdX1).

Mount options mirror what Unraid's Unassigned Devices uses for the iPod, so libgpod
sees identical filename handling.
"""
from __future__ import annotations

import os
import re
import subprocess
import time

# Matches the Unassigned Devices vfat mount for the iPod.
VFAT_OPTS = "rw,nosuid,nodev,relatime,fmask=0000,dmask=0000,allow_utime=0022,codepage=437,iocharset=utf8,shortname=mixed,errors=remount-ro"


class MountError(RuntimeError):
    pass


def find_partition(label: str = "IPOD") -> str:
    """Resolve the iPod partition by FAT label, so the device node (/dev/sdX1) can
    change across replugs without breaking anything. Returns "" if not present."""
    by_label = f"/dev/disk/by-label/{label}"
    if os.path.exists(by_label):
        return os.path.realpath(by_label)
    return ""


def whole_disk(partition: str) -> str:
    """Derive the whole-disk device from a partition, for SCSI SysInfoExtended/eject.
    /dev/sdg1 -> /dev/sdg ; /dev/nvme0n1p1 -> /dev/nvme0n1 ; /dev/mmcblk0p1 -> /dev/mmcblk0."""
    if not partition:
        return ""
    base = os.path.basename(partition)
    base = re.sub(r"p\d+$", "", base) if re.search(r"\d+p\d+$", base) else re.sub(r"\d+$", "", base)
    return "/dev/" + base


def is_mounted(mountpoint: str) -> bool:
    mp = os.path.realpath(mountpoint)
    try:
        with open("/proc/mounts") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2 and os.path.realpath(parts[1]) == mp:
                    return True
    except OSError:
        pass
    return False


def partition_present(partition: str) -> bool:
    return os.path.exists(partition)


def mount_ipod(partition: str, mountpoint: str) -> bool:
    """Mount the iPod partition. Returns True if a mount was performed,
    False if it was already mounted. Raises MountError on failure."""
    if is_mounted(mountpoint):
        return False
    if not partition_present(partition):
        raise MountError(f"partition {partition} not present (is the iPod connected?)")
    os.makedirs(mountpoint, exist_ok=True)
    proc = subprocess.run(
        ["mount", "-t", "vfat", "-o", VFAT_OPTS, partition, mountpoint],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    if proc.returncode != 0:
        raise MountError(f"mount failed: {proc.stdout.strip()}")
    return True


def eject_ipod(mountpoint: str, device: str = "") -> dict:
    """Flush, unmount, then eject so the iPod leaves disk mode and is safe to unplug.

    Unmounting alone leaves the iPod showing "connected / do not disconnect"; a physical
    unplug then looks like a dirty disconnect and the iPod rebuilds on boot. Ejecting the
    SCSI device (STOP UNIT + allow-medium-removal) tells the iPod to exit disk mode and
    return to its menu. Returns {"unmounted": bool, "ejected_via": str|None}.
    """
    unmounted = False
    if is_mounted(mountpoint):
        subprocess.run(["sync"], check=False)
        last = ""
        for _ in range(5):
            proc = subprocess.run(
                ["umount", mountpoint], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
            )
            if proc.returncode == 0:
                unmounted = True
                break
            last = proc.stdout.strip()
            time.sleep(1)
        else:
            raise MountError(f"umount failed: {last}")

    ejected_via = _eject_device(device) if device else None
    return {"unmounted": unmounted, "ejected_via": ejected_via}


def _eject_device(device: str) -> str:
    """Send the iPod out of disk mode. Try `eject` (STOP UNIT + allow removal), then
    fall back to `sg_start --stop`. Best-effort: raises only if all methods fail."""
    if not os.path.exists(device):
        return "device-gone"          # already detached — nothing to do
    subprocess.run(["sync"], check=False)
    last = ""
    for cmd in (["eject", device], ["sg_start", "--stop", device]):
        try:
            proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        except FileNotFoundError:
            continue
        if proc.returncode == 0:
            return cmd[0]
        last = proc.stdout.strip()
    raise MountError(f"eject failed: {last or 'no eject tool available'}")
